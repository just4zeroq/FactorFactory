"""统一行情取数门面：多交易所 + 三原语（L0 与挖掘流程共用）。

定位：**只取数，不落盘**。调用方拿到 DataFrame 后自行决定写 L0 parquet、
装配 L1，还是直接进内存做研究。落盘职责留在 services/l0_store 与各 CLI。

三层来源，各司其职：

- **元数据**：ccxt ``load_markets``（交易所 ``exchangeInfo`` 原始字段），
  交给 :mod:`services.exchange_metadata` 归一化；
- **历史全量**：data.binance.vision 归档，优先月度（请求量少约 30 倍）、
  无月归档的回落日归档，:mod:`upstream.vision_daily`；
- **区间/最近段**：历史走归档，最近 ``ccxt_lag_days`` 天用 ccxt 增量补，
  :mod:`services.l0_incremental` 的取数函数。

三个原语：

    fetch_metadata(exchange, market)                       -> MetadataResult
    fetch_history(symbol, dataset, start, end)             -> FetchResult   # 全历史归档
    fetch_range(symbol, dataset, start, end)               -> FetchResult   # 归档 + ccxt 补尾

多交易所：:class:`ExchangeAdapter` 封装「ccxt 实例 id / vision 路径前缀 /
符号字形 / store 市场」的差异，注册在 :data:`ADAPTERS`。当前只注册币安；
新增交易所只需加一个适配器，三原语不动。

**timeframe** 对 K 线类数据集（klines / indexPriceKlines / markPriceKlines /
premiumIndexKlines）生效，透传到归档 URL 与 ccxt ``fetchOHLCV``；非 K 线
数据集忽略它（其归档路径不含周期段）。

本模块不进 capability：取数带 IO 与网络副作用，capability 是纯计算无权层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from contracts.errors import E1004, E4002, FlowError

from . import exchange_metadata as em
from . import l0_incremental as inc
from upstream import vision_daily as vd


# ---------------------------------------------------------------- 数据集能力配置
class SourceKind(str, Enum):
    """一个源对某数据集能提供什么形态的数据。"""

    HISTORY = "history"        # 带时间的历史序列（可按区间翻页）
    SNAPSHOT = "snapshot"      # 只有当前截面，无历史区间
    NATIVE_REST = "native_rest"  # 交易所原生 REST（ccxt 无统一接口）
    NONE = "none"              # 该源不支持


@dataclass(frozen=True)
class SourceCapability:
    """单个源对单个数据集的能力声明。"""

    kind: SourceKind
    # 源内部的数据集名（ccxt 方法名 / vision 归档名）；None 表示按统一名。
    name: str | None = None
    note: str = ""
    # vision 归档粒度：daily / monthly。日路径 404 的数据集（fundingRate）走月度。
    granularity: str = "daily"

    @property
    def supported(self) -> bool:
        return self.kind is not SourceKind.NONE

    @property
    def has_history(self) -> bool:
        return self.kind in (SourceKind.HISTORY, SourceKind.NATIVE_REST)

    @property
    def monthly(self) -> bool:
        return self.granularity == "monthly"


@dataclass(frozen=True)
class DatasetSpec:
    """一个数据集在两个源上的能力配置。新增数据集只改这里。"""

    name: str
    kline: bool                       # 是否吃 timeframe
    vision: SourceCapability
    ccxt: SourceCapability

    @property
    def has_vision_history(self) -> bool:
        return self.vision.has_history

    @property
    def has_ccxt_history(self) -> bool:
        return self.ccxt.has_history

    @property
    def from_both(self) -> bool:
        """两源都能给历史 → 范围取数可互补（归档打底 + ccxt 补尾）。"""
        return self.has_vision_history and self.has_ccxt_history


# 统一数据集注册表。vision 侧历史初始化优先走月归档（请求量少约 30 倍）：
# K 线四类 + aggTrades/trades + fundingRate 都有月度；bookDepth/metrics/bookTicker
# 月度 404 或稀疏，仍走日归档。ccxt 侧能力按 4.5.93 实测。
DATASET_SPECS: dict[str, DatasetSpec] = {
    "klines": DatasetSpec(
        "klines", True,
        SourceCapability(SourceKind.HISTORY, granularity="monthly"),
        SourceCapability(SourceKind.HISTORY, "fetchOHLCV"),
    ),
    "indexPriceKlines": DatasetSpec(
        "indexPriceKlines", True,
        SourceCapability(SourceKind.HISTORY, granularity="monthly"),
        SourceCapability(SourceKind.HISTORY, "fetchIndexOHLCV"),
    ),
    "markPriceKlines": DatasetSpec(
        "markPriceKlines", True,
        SourceCapability(SourceKind.HISTORY, granularity="monthly"),
        SourceCapability(SourceKind.HISTORY, "fetchMarkOHLCV"),
    ),
    "premiumIndexKlines": DatasetSpec(
        "premiumIndexKlines", True,
        SourceCapability(SourceKind.HISTORY, granularity="monthly"),
        SourceCapability(SourceKind.HISTORY, "fetchPremiumIndexOHLCV"),
    ),
    "trades": DatasetSpec(
        "trades", False,
        SourceCapability(SourceKind.HISTORY, granularity="monthly"),
        SourceCapability(SourceKind.HISTORY, "fetchTrades"),
    ),
    "bookDepth": DatasetSpec(
        "bookDepth", False,
        SourceCapability(SourceKind.HISTORY),
        SourceCapability(SourceKind.SNAPSHOT, "fetchOrderBook", "ccxt 仅当前盘口"),
    ),
    "bookTicker": DatasetSpec(
        "bookTicker", False,
        SourceCapability(SourceKind.HISTORY, note="日归档已实测，但稀疏（新币/活动期）"),
        SourceCapability(SourceKind.SNAPSHOT, "fetchBidsAsks", "ccxt 仅当前最优档"),
    ),
    "metrics": DatasetSpec(
        "metrics", False,
        SourceCapability(SourceKind.HISTORY),
        SourceCapability(SourceKind.NATIVE_REST, "fapi/v1/metrics", "交易所原生 REST"),
    ),
    "funding": DatasetSpec(
        "funding", False,
        SourceCapability(
            SourceKind.HISTORY, "fundingRate", "日路径 404，月度归档可用",
            granularity="monthly",
        ),
        SourceCapability(SourceKind.HISTORY, "fetchFundingRateHistory"),
    ),
    "open_interest": DatasetSpec(
        "open_interest", False,
        SourceCapability(SourceKind.NONE, note="无归档"),
        SourceCapability(SourceKind.HISTORY, "fetchOpenInterestHistory"),
    ),
}

# 兼容旧引用的派生常量（由配置表生成，不另立真相）。
KLINE_DATASETS: tuple[str, ...] = tuple(d for d, s in DATASET_SPECS.items() if s.kline)
VISION_DATASETS: tuple[str, ...] = tuple(
    d for d, s in DATASET_SPECS.items() if s.vision.supported
)
CCXT_HISTORY_DATASETS: tuple[str, ...] = tuple(
    d for d, s in DATASET_SPECS.items() if s.has_ccxt_history
)
_CCXT_SNAPSHOT_DATASETS: tuple[str, ...] = tuple(
    d for d, s in DATASET_SPECS.items() if s.ccxt.kind is SourceKind.SNAPSHOT
)

# 范围取数的源选择。
SOURCE_AUTO = "auto"      # 按配置自动编排（两源互补 / 单源）
SOURCE_VISION = "vision"  # 强制只走归档
SOURCE_CCXT = "ccxt"      # 强制只走 ccxt
_SOURCE_CHOICES = (SOURCE_AUTO, SOURCE_VISION, SOURCE_CCXT)


def get_spec(dataset: str) -> DatasetSpec:
    """取数据集能力配置，未知数据集报错并列出可用项。"""
    name = str(dataset or "").strip()
    spec = DATASET_SPECS.get(name)
    if spec is None:
        raise FlowError(
            E1004, f"未知数据集 {name!r}", path="datasets",
            hint=f"可用: {', '.join(sorted(DATASET_SPECS))}",
        )
    return spec


# ---------------------------------------------------------------- 交易所适配器
@dataclass(frozen=True)
class ExchangeAdapter:
    """一个交易所的差异集合。三原语只认这个接口，不认具体交易所。"""

    key: str
    # 市场键 → ccxt 交易所类 id。当前接受 futures / spot（沿用 L0 既有语义）。
    ccxt_ids: Mapping[str, str]
    # 市场键 → data vision 路径市场段（futures → futures/um）。
    vision_markets: Mapping[str, str]
    # 市场键 → 元数据市场键（futures → um，喂给 exchange_metadata.normalize）。
    metadata_markets: Mapping[str, str]

    def make_client(
        self, market: str, *, proxy: str | None = None, timeout: float = 30.0,
    ) -> Any:
        """构造绑定到该市场、带代理与限流的 ccxt 实例。"""
        import ccxt

        ccxt_id = self.ccxt_ids.get(market)
        if ccxt_id is None:
            raise FlowError(
                E1004, f"{self.key} 不支持市场 {market!r}", path="market",
                hint=f"可用: {', '.join(self.ccxt_ids)}",
            )
        cls = getattr(ccxt, ccxt_id, None)
        if cls is None:
            raise FlowError(E4002, f"ccxt 无交易所 {ccxt_id!r}", path="exchange")
        opts: dict[str, Any] = {
            "timeout": int(timeout * 1000),
            "enableRateLimit": True,
            "options": {"defaultType": "future" if market == "futures" else "spot"},
        }
        if proxy:
            opts["proxies"] = {"http": proxy, "https": proxy}
            opts["aiohttp_trust_env"] = False
        return cls(opts)

    def vision_market(self, market: str) -> str:
        value = self.vision_markets.get(market)
        if value is None:
            raise FlowError(E1004, f"{self.key} 无归档市场 {market!r}", path="market")
        return value

    def metadata_market(self, market: str) -> str:
        value = self.metadata_markets.get(market)
        if value is None:
            raise FlowError(E1004, f"{self.key} 无元数据市场 {market!r}", path="market")
        return value

    def vision_symbol(self, symbol: str, market: str) -> str:
        """ccxt 符号 → 归档字形。币安是去斜杠大写；其它交易所覆写此方法。"""
        return vd._vision_symbol(symbol)


def _binance_adapter() -> ExchangeAdapter:
    return ExchangeAdapter(
        key="binance",
        ccxt_ids={"futures": "binanceusdm", "spot": "binance"},
        vision_markets={"futures": "futures", "spot": "spot"},
        metadata_markets={"futures": "um", "spot": "spot"},
    )


#: 已注册交易所。新增交易所在此加一项即可，三原语无需改动。
ADAPTERS: dict[str, ExchangeAdapter] = {"binance": _binance_adapter()}


def get_adapter(exchange: str) -> ExchangeAdapter:
    key = str(exchange or "binance").strip().lower()
    adapter = ADAPTERS.get(key)
    if adapter is None:
        raise FlowError(
            E1004, f"未知交易所 {exchange!r}", path="exchange",
            hint=f"已注册: {', '.join(sorted(ADAPTERS))}",
        )
    return adapter


def register_adapter(adapter: ExchangeAdapter) -> None:
    """注册自定义交易所适配器（供扩展，不需要改三原语）。"""
    ADAPTERS[adapter.key] = adapter


def normalize_market(market: str) -> str:
    """把常见别名归一到 futures / spot。cm（币本位）暂不支持归档，明确拒绝。"""
    value = str(market or "futures").strip().lower()
    if value in ("futures", "um", "perp", "perpetual"):
        return "futures"
    if value == "spot":
        return "spot"
    raise FlowError(
        E1004, f"不支持的市场 {market!r}", path="market",
        hint="当前支持 futures / spot；币本位 cm 未接入日归档",
    )


# ---------------------------------------------------------------- 结果对象
@dataclass
class MetadataResult:
    exchange: str
    market: str
    records: dict[str, dict[str, Any]]
    stats: dict[str, Any]
    source: str  # api / cache


@dataclass
class FetchResult:
    """一次取数的结果。``frame`` 为空时看 ``report`` 的 missing/errors 判断原因。"""

    dataset: str
    symbol: str
    exchange: str
    market: str
    timeframe: str | None
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    rows: int = 0
    days: int = 0
    missing: int = 0
    errors: int = 0
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset, "symbol": self.symbol,
            "exchange": self.exchange, "market": self.market,
            "timeframe": self.timeframe, "rows": self.rows, "days": self.days,
            "missing": self.missing, "errors": self.errors,
            "sources": sorted(set(self.sources)), "warnings": self.warnings,
        }


# ---------------------------------------------------------------- 周期与数据集校验
def needs_timeframe(dataset: str) -> bool:
    return get_spec(dataset).kline


def _resolve_timeframe(dataset: str | DatasetSpec, timeframe: str | None) -> str | None:
    """K 线类必须有合法周期；非 K 线类忽略。接受数据集名或 spec。"""
    spec = get_spec(dataset) if isinstance(dataset, str) else dataset
    if not spec.kline:
        return None
    text = str(timeframe or "1d").strip().lower()
    if not text:
        raise FlowError(E1004, f"{spec.name} 需要 timeframe", path="timeframe")
    return text


@dataclass(frozen=True)
class SourcePlan:
    """范围取数的源编排结果：用哪些源、各自覆盖哪段、为什么。"""

    use_vision: bool
    use_ccxt: bool
    note: str

    @property
    def both(self) -> bool:
        return self.use_vision and self.use_ccxt


def plan_sources(dataset: str, source: str, use_vision: bool) -> SourcePlan:
    """按能力配置决定范围取数走哪些源。

    - ``source=vision`` / ``ccxt``：强制单源，不支持则明确报错；
    - ``source=auto``：两源都有历史 → 互补；否则选唯一有历史的源；
      快照类 → ccxt 取当前截面，vision 若有历史也取；
    - ``use_vision=False`` 是历史遗留的"只走 ccxt"开关（等价 source=ccxt）。
    """
    spec = get_spec(dataset)
    choice = str(source or SOURCE_AUTO).strip().lower()
    if choice not in _SOURCE_CHOICES:
        raise FlowError(
            E1004, f"未知取数源 {source!r}", path="source",
            hint=f"可用: {', '.join(_SOURCE_CHOICES)}",
        )
    if not use_vision and choice == SOURCE_AUTO:
        choice = SOURCE_CCXT  # 兼容旧开关

    if choice == SOURCE_VISION:
        if not spec.vision.supported:
            raise FlowError(
                E1004, f"{spec.name} 没有归档源（{spec.vision.note or '不支持'}）", path="source",
                hint="该数据集请用 source=ccxt 或 source=auto",
            )
        return SourcePlan(True, False, "强制归档源")

    if choice == SOURCE_CCXT:
        if not spec.ccxt.supported:
            raise FlowError(
                E1004, f"{spec.name} 没有 ccxt 源", path="source",
                hint="该数据集请用 source=vision 或 source=auto",
            )
        return SourcePlan(False, True, "强制 ccxt 源")

    # auto
    if spec.ccxt.kind is SourceKind.SNAPSHOT:
        # 快照类（bookDepth/bookTicker）：ccxt 给当前截面、vision 给归档历史。
        # 必须放在 has_vision_history 之前，否则会被「仅归档」分支吞掉，
        # 导致 ccxt 当前截面永远取不到（与双源映射图不一致）。
        return SourcePlan(spec.vision.supported, True, "归档历史 + ccxt 当前截面")
    if spec.from_both:
        return SourcePlan(True, True, "双源互补：归档打底 + ccxt 补尾")
    if spec.has_vision_history:
        return SourcePlan(True, False, "仅归档有历史")
    if spec.has_ccxt_history:
        return SourcePlan(False, True, "仅 ccxt 有历史")
    raise FlowError(E1004, f"{spec.name} 没有任何可用取数源", path="datasets")


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise FlowError(E1004, f"非法日期 {value!r}（用 YYYY-MM-DD）", path="range") from exc


# ---------------------------------------------------------------- 原语一：元数据
def fetch_metadata(
    exchange: str = "binance",
    market: str = "futures",
    *,
    root: str | Path,
    proxy: str | None = None,
    timeout: float = 30.0,
    allow_cache: bool = True,
    client: Any | None = None,
) -> MetadataResult:
    """拉全市场交易对元数据（含已下架状态），走 ccxt ``load_markets``。

    ``client`` 可注入（测试或复用连接）；缺省用交易所适配器构造。
    ``root`` 用于写原始快照与失败回退。
    """
    ad = get_adapter(exchange)
    mkt = normalize_market(market)
    ex = client if client is not None else ad.make_client(mkt, proxy=proxy, timeout=timeout)
    records, stats, source = em.collect(
        root, ad.metadata_market(mkt), client=ex, allow_cache=allow_cache,
        exchange=ad.ccxt_ids[mkt],
    )
    return MetadataResult(exchange=ad.key, market=mkt, records=records, stats=stats, source=source)


# ---------------------------------------------------------------- 原语二：全历史
def fetch_history(
    symbol: str,
    dataset: str,
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    exchange: str = "binance",
    market: str = "futures",
    timeframe: str = "1d",
    root: str | Path | None = None,
    proxy: str | None = None,
    timeout: float = 60.0,
    retries: int = 6,
    workers: int = 8,
    verify: bool = True,
    downloader: Callable[[str], bytes] | None = None,
    sink: Callable[[str, str, str, pd.DataFrame], None] | None = None,
) -> FetchResult:
    """从 data vision 归档拉指定区间的全部历史（原样、带 checksum）。

    历史初始化优先走**月归档**（K 线四类 / 逐笔 / 资金费），一个月一个 zip，
    请求量比逐日少约 30 倍；仅 bookDepth/metrics/bookTicker 等无月归档的回落日归档。

    给了 ``sink`` 就逐段流式交给它（``sink(dataset, symbol, period, frame)``，
    ``period`` 是 ``YYYY-MM`` 或 ``YYYY-MM-DD``），不在内存里堆全量——用于 L0
    落盘；否则返回合并后的 DataFrame。
    """
    ad = get_adapter(exchange)
    mkt = normalize_market(market)
    spec = get_spec(dataset)
    if not spec.vision.supported:
        raise FlowError(
            E1004, f"{spec.name} 没有归档源，不能用 fetch_history", path="datasets",
            hint=f"该数据集归档状态: {spec.vision.note or '不支持'}；请用 fetch_range(source=ccxt)",
        )
    name = spec.name
    vname = spec.vision.name or name  # 统一名 → vision 内部名（funding → fundingRate）
    monthly = spec.vision.monthly
    if monthly:
        vd.validate_monthly([vname], mkt)
    else:
        vd.validate_datasets([vname], mkt)
    tf = _resolve_timeframe(spec, timeframe)
    lo, hi = _as_date(start), _as_date(end)

    collected: list[tuple[str, pd.DataFrame]] = []

    def collecting(ds: str, sym: str, period: str, frame: pd.DataFrame) -> None:
        collected.append((period, frame))
        if sink is not None:
            # 落盘一律用统一数据集名；归档内部名（funding → fundingRate）
            # 只是 URL/解析用的别名，泄漏到 L0 路径会写出第二份孤立数据。
            sink(name, sym, period, frame)

    common = dict(
        symbols=[symbol],
        vision_symbols={symbol: ad.vision_symbol(symbol, mkt)},
        market=ad.vision_market(mkt), sink=collecting,
        downloader=downloader, proxy=proxy, timeout=timeout,
        retries=retries, workers=workers, verify=verify,
    )
    if monthly:
        reports = vd.download_monthly(
            [vname], months=vd.month_range(lo, hi),
            timeframe=tf or "1d", **common,
        )
        registry = vd.MONTHLY_DATASETS
    else:
        reports = vd.download_daily(
            [vname], days=vd.day_range(lo, hi), timeframe=tf or "1d", **common
        )
        registry = vd.DAILY_DATASETS
    rep = reports[0]
    result = FetchResult(
        dataset=name, symbol=symbol, exchange=ad.key, market=mkt, timeframe=tf,
        rows=rep.rows, days=rep.days, missing=rep.missing, errors=rep.errors,
        sources=["vision"],
    )
    arch_spec = registry[vname]
    if not arch_spec.verified:
        result.warnings.append(f"{name}: {arch_spec.note}")
    if rep.missing:
        result.warnings.append(f"{rep.missing} 天无归档（未上市/未发布）")
    if rep.errors:
        result.warnings.append(f"{rep.errors} 天网络失败，重跑同命令可补齐")
    if sink is None and collected:
        collected.sort(key=lambda pair: pair[0])
        result.frame = _concat_dedup([frame for _, frame in collected], name)
        result.rows = len(result.frame)
    return result


# ---------------------------------------------------------------- 原语三：指定区间
def fetch_range(
    symbol: str,
    dataset: str,
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    exchange: str = "binance",
    market: str = "futures",
    timeframe: str = "1d",
    source: str = SOURCE_AUTO,
    proxy: str | None = None,
    timeout: float = 30.0,
    limit: int = 1000,
    ccxt_lag_days: int = 3,
    use_vision: bool = True,
    verify: bool = True,
    client: Any | None = None,
    downloader: Callable[[str], bytes] | None = None,
) -> FetchResult:
    """取指定时间范围的行情，源由 :func:`plan_sources` 按能力配置编排。

    - ``source=auto``（默认）：两源都有历史 → 归档覆盖 ``[start, end-lag]``、
      ccxt 补最近 ``ccxt_lag_days`` 天；只有一源有历史 → 全走该源；
      快照类 → ccxt 取当前截面（并告警无历史）。
    - ``source=vision`` / ``ccxt``：强制单源，不支持则报错。
    - ``use_vision=False`` 是历史遗留开关，等价 ``source=ccxt``。

    重叠段按 timestamp 去重；所有路径都透传 ``proxy``。
    """
    ad = get_adapter(exchange)
    mkt = normalize_market(market)
    spec = get_spec(dataset)
    name = spec.name
    tf = _resolve_timeframe(spec, timeframe)
    lo, hi = _as_date(start), _as_date(end)
    if hi < lo:
        raise FlowError(E1004, "start 不能晚于 end", path="range")

    plan = plan_sources(name, source, use_vision)
    result = FetchResult(
        dataset=name, symbol=symbol, exchange=ad.key, market=mkt, timeframe=tf,
    )
    result.warnings.append(f"源编排: {plan.note}")
    frames: list[pd.DataFrame] = []
    lag_days = max(0, int(ccxt_lag_days))

    # ---- 归档段 -------------------------------------------------------
    arch_end = hi - timedelta(days=lag_days) if plan.both else hi
    if plan.use_vision and lo <= arch_end:
        hist = fetch_history(
            symbol, name, lo, arch_end, exchange=exchange, market=mkt,
            timeframe=tf or "1d", proxy=proxy, timeout=timeout,
            verify=verify, downloader=downloader,
        )
        if not hist.frame.empty:
            frames.append(hist.frame)
        result.missing += hist.missing
        result.errors += hist.errors
        result.days += hist.days
        result.sources.append("vision")
        result.warnings.extend(hist.warnings)

    # ---- ccxt 段 ------------------------------------------------------
    if plan.use_ccxt:
        if spec.ccxt.kind is SourceKind.SNAPSHOT:
            result.warnings.append(
                f"{name}: ccxt 只有当前截面，无历史区间；历史请用 source=vision"
            )
            result.sources.append("ccxt-snapshot")
        else:
            result.sources.append("ccxt")

        ex = client if client is not None else ad.make_client(mkt, proxy=proxy, timeout=timeout)
        since_ms = None
        if spec.ccxt.kind is SourceKind.HISTORY:
            since_dt = datetime.combine(lo, datetime.min.time(), tzinfo=timezone.utc)
            since_ms = int(since_dt.timestamp() * 1000)
        try:
            if spec.ccxt.kind is SourceKind.NATIVE_REST:
                frame = inc._native_metrics(
                    symbol, since_ms=since_ms, limit=limit, proxy=proxy, timeout=timeout,
                )
            else:
                frame = inc._fetch_ccxt(
                    ex, name, symbol, timeframe=tf or "1d",
                    since_ms=since_ms, limit=limit,
                )
            if not frame.empty:
                # 只保留落在 [lo, hi] 的行；快照类时间戳就是当前时刻，可能落在区间外。
                if "timestamp" in frame.columns:
                    lo_ts = pd.Timestamp(lo, tz="UTC")
                    hi_ts = pd.Timestamp(hi + timedelta(days=1), tz="UTC")
                    stamps = pd.to_datetime(frame["timestamp"], utc=True)
                    if spec.ccxt.kind is SourceKind.SNAPSHOT:
                        # 快照没有历史区间语义：不裁剪，直接作为当前值纳入。
                        pass
                    else:
                        frame = frame.loc[(stamps >= lo_ts) & (stamps < hi_ts)]
                if not frame.empty:
                    frames.append(frame)
        except Exception as exc:
            result.errors += 1
            result.warnings.append(f"ccxt 增量失败: {type(exc).__name__}: {exc}")

    # 归档段是整日/整月拉的，可能越出 [lo, hi]（月度尤其明显）；统一裁切到区间内。
    # 快照帧（bookDepth/bookTicker 的 ccxt 当前截面）时间戳在当下，若被裁掉则保留原帧。
    lo_ts = pd.Timestamp(lo, tz="UTC")
    hi_ts = pd.Timestamp(hi + timedelta(days=1), tz="UTC")
    trimmed: list[pd.DataFrame] = []
    for frame in frames:
        if "timestamp" in frame.columns and spec.ccxt.kind is not SourceKind.SNAPSHOT:
            stamps = pd.to_datetime(frame["timestamp"], utc=True)
            clipped = frame.loc[(stamps >= lo_ts) & (stamps < hi_ts)]
            if clipped.empty:
                continue
            trimmed.append(clipped)
        else:
            trimmed.append(frame)
    result.frame = _concat_dedup(trimmed, name)
    result.rows = len(result.frame)
    return result


def _concat_dedup(frames: Sequence[pd.DataFrame], dataset: str) -> pd.DataFrame:
    """合并多段取数结果：按时间戳排序、复合键去重。"""
    usable = [f for f in frames if f is not None and not f.empty]
    if not usable:
        return pd.DataFrame()
    out = pd.concat(usable, ignore_index=True)
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        key = ["timestamp", "percentage"] if dataset == "bookDepth" else ["timestamp"]
        key = [k for k in key if k in out.columns] or ["timestamp"]
        out = out.drop_duplicates(subset=key, keep="last")
        out = out.sort_values("timestamp", kind="mergesort")
    return out.reset_index(drop=True)


__all__ = [
    "SourceKind", "SourceCapability", "DatasetSpec", "DATASET_SPECS", "get_spec",
    "SourcePlan", "plan_sources",
    "SOURCE_AUTO", "SOURCE_VISION", "SOURCE_CCXT",
    "KLINE_DATASETS", "VISION_DATASETS", "CCXT_HISTORY_DATASETS",
    "ExchangeAdapter", "ADAPTERS", "get_adapter", "register_adapter",
    "normalize_market", "needs_timeframe",
    "MetadataResult", "FetchResult",
    "fetch_metadata", "fetch_history", "fetch_range",
]
