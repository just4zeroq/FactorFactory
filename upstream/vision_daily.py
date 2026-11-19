"""data.binance.vision 归档下载（L0 数据基座专用，独立于 flow 主流程）。

与 ``upstream/binance_vision.py`` 的区别：那是月度归档、只做 ohlcv/funding、
写进主流程 store；这里同时支持日路径 ``data/<market>/daily/`` 与月路径
``data/<market>/monthly/``，拉取原始行情并**保留源文件的全部列**
（quote_volume / count / taker_buy / notional 等），不写主流程的
``store.DATASETS`` 契约——主流程的 checksum 与字段约束不动。

**历史初始化优先走月归档**：一个月一个 zip（含整月），K 线/逐笔/资金费都覆盖，
请求量比逐日下载少约 30 倍。只有月归档缺失的数据集（bookDepth/metrics/bookTicker）
才回落到日归档。两条路径共用同一套 checksum 与解析内核。

目录入口（用户确认）：
- 日：``https://data.binance.vision/?prefix=data/futures/um/daily/`` 与 ``.../spot/daily/``
- 月：``https://data.binance.vision/?prefix=data/futures/um/monthly/`` 与 ``.../spot/monthly/``

这些页面是 S3 目录浏览页，靠 JS 调 S3 List API 填充；在本环境该 API 被代理拦截，
无法枚举目录。但归档文件名是**确定性模板**（已逐类实测 HTTP 200），
所以直接按模板构造 URL，无需列目录。

实测确认的日归档（2024-03-30 / 2026-10-10 样本）：

====================  ===============================================================
类型                  路径模板（相对 data/<market>/daily/）
====================  ===============================================================
klines                klines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM-DD>.zip
indexPriceKlines      indexPriceKlines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM-DD>.zip
markPriceKlines       markPriceKlines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM-DD>.zip
premiumIndexKlines    premiumIndexKlines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM-DD>.zip
aggTrades             aggTrades/<SYM>/<SYM>-aggTrades-<YYYY-MM-DD>.zip
trades                trades/<SYM>/<SYM>-trades-<YYYY-MM-DD>.zip
bookDepth             bookDepth/<SYM>/<SYM>-bookDepth-<YYYY-MM-DD>.zip
metrics               metrics/<SYM>/<SYM>-metrics-<YYYY-MM-DD>.zip
bookTicker            bookTicker/<SYM>/<SYM>-bookTicker-<YYYY-MM-DD>.zip
====================  ===============================================================

月度归档（相对 ``data/<market>/monthly/``，历史初始化优先用，请求量少约 30 倍）：

====================  ===============================================================
类型                  路径模板
====================  ===============================================================
klines                klines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM>.zip
indexPriceKlines      indexPriceKlines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM>.zip
markPriceKlines       markPriceKlines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM>.zip
premiumIndexKlines    premiumIndexKlines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM>.zip
aggTrades             aggTrades/<SYM>/<SYM>-aggTrades-<YYYY-MM>.zip
trades                trades/<SYM>/<SYM>-trades-<YYYY-MM>.zip
fundingRate           fundingRate/<SYM>/<SYM>-fundingRate-<YYYY-MM>.zip
====================  ===============================================================

仅日归档（月度 404 或稀疏）：bookDepth / metrics / bookTicker。

实测记录（用户给的样本 + 本地下载校验）：
- ``bookTicker``：``1000BONKUSDT-bookTicker-2024-03-30.zip`` 与
  ``BTCUSDT-bookTicker-2024-03-30.zip`` 均 200/206，CHECKSUM 齐全。
  此前用 BTCUSDT 近期日期探测 404 是**该日无归档**，不是模板错。
  归档是稀疏的（新币上线、特定活动期才有），404 记 missing 即可。
  CSV 列：update_id / best_bid_price / best_bid_qty / best_ask_price /
  best_ask_qty / transaction_time / event_time —— 时间语义取 ``event_time``。
- ``fundingRate``：日路径 404，只有月度归档（2026-10 样本 206 + CHECKSUM 200）。
  CSV 列：calc_time / funding_interval_hours / last_funding_rate。
  与主流程 ``binance_vision.py`` 的窄表解析（毫秒、裁剪、写 store）不同，
  这里保留原列并统一成 UTC ``timestamp``，供 L0 原样基座使用。

时间语义：K线/逐笔/资金费/bookTicker 是 epoch（按位数判断 ms/us/ns），
bookDepth/metrics 是 ``YYYY-MM-DD HH:MM:SS`` 字符串。统一产出 UTC 感知的
``timestamp`` 列，其余列**原样保留**——L0 是原始数据基座，不是给挖掘用的窄表。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from contracts.errors import E1004, E4002, FlowError

BASE_URL = "https://data.binance.vision"
TIMESTAMP_COL = "timestamp"

# 数据集注册表：name → spec。新增类型只改这里，不改下载/解析逻辑。
_KLINE_COLS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
)


@dataclass(frozen=True)
class DailySpec:
    """一类日归档的元信息。

    ``path`` 是相对 ``data/<market>/daily/`` 的模板，``{sym}``/``{tf}``/``{day}``
    由 ``daily_url`` 填充。``time_col`` 是源文件里的时间列名；``time_kind``
    决定解析方式。``needs_tf`` 表示路径里带 timeframe（K线类）。
    """

    path: str
    time_col: str
    time_kind: str  # "epoch" | "string"
    value_cols: tuple[str, ...]
    markets: tuple[str, ...] = ("futures",)
    needs_tf: bool = False
    verified: bool = True
    note: str = ""


DAILY_DATASETS: dict[str, DailySpec] = {
    "klines": DailySpec(
        path="klines/{sym}/{tf}/{sym}-{tf}-{day}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        markets=("spot", "futures"), needs_tf=True,
    ),
    "indexPriceKlines": DailySpec(
        path="indexPriceKlines/{sym}/{tf}/{sym}-{tf}-{day}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        needs_tf=True,
    ),
    "markPriceKlines": DailySpec(
        path="markPriceKlines/{sym}/{tf}/{sym}-{tf}-{day}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        needs_tf=True,
    ),
    "premiumIndexKlines": DailySpec(
        path="premiumIndexKlines/{sym}/{tf}/{sym}-{tf}-{day}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        needs_tf=True,
    ),
    "aggTrades": DailySpec(
        path="aggTrades/{sym}/{sym}-aggTrades-{day}.zip",
        time_col="transact_time", time_kind="epoch",
        value_cols=("agg_trade_id", "price", "quantity", "first_trade_id",
                    "last_trade_id", "transact_time", "is_buyer_maker"),
        markets=("spot", "futures"),
    ),
    "trades": DailySpec(
        path="trades/{sym}/{sym}-trades-{day}.zip",
        time_col="time", time_kind="epoch",
        value_cols=("id", "price", "qty", "quote_qty", "time", "is_buyer_maker"),
        markets=("spot", "futures"),
    ),
    "bookDepth": DailySpec(
        path="bookDepth/{sym}/{sym}-bookDepth-{day}.zip",
        time_col="timestamp", time_kind="string",
        value_cols=("timestamp", "percentage", "depth", "notional"),
    ),
    "metrics": DailySpec(
        path="metrics/{sym}/{sym}-metrics-{day}.zip",
        time_col="create_time", time_kind="string",
        value_cols=("create_time", "symbol", "sum_open_interest",
                    "sum_open_interest_value", "count_toptrader_long_short_ratio",
                    "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                    "sum_taker_long_short_vol_ratio"),
    ),
    "bookTicker": DailySpec(
        path="bookTicker/{sym}/{sym}-bookTicker-{day}.zip",
        time_col="event_time", time_kind="epoch",
        value_cols=("update_id", "best_bid_price", "best_bid_qty",
                    "best_ask_price", "best_ask_qty", "transaction_time", "event_time"),
        note="归档稀疏（新币/活动期才有），404 记 missing；真实列来自 2024-03-30 样本",
    ),
}


@dataclass(frozen=True)
class MonthlySpec:
    """一类月归档的元信息（结构与 :class:`DailySpec` 对齐，日期占位是 ``{month}``）。

    ``needs_tf`` 表示路径里带 timeframe 段（K 线类月度归档同日归档，形如
    ``klines/<SYM>/<tf>/<SYM>-<tf>-<YYYY-MM>.zip``）。
    """

    path: str
    time_col: str
    time_kind: str  # "epoch" | "string"
    value_cols: tuple[str, ...]
    markets: tuple[str, ...] = ("futures",)
    needs_tf: bool = False
    verified: bool = True
    note: str = ""


# 月归档注册表。实测（2025-01 样本）：K 线四类 + aggTrades + trades + fundingRate
# 都有月度；bookDepth / metrics 月度 404（只有日归档），bookTicker 归档稀疏且无月度。
# K 线月度包是单 CSV、列与日包完全一致，parse_monthly 直接复用同一套解析内核。
MONTHLY_DATASETS: dict[str, MonthlySpec] = {
    "klines": MonthlySpec(
        path="klines/{sym}/{tf}/{sym}-{tf}-{month}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        markets=("spot", "futures"), needs_tf=True,
    ),
    "indexPriceKlines": MonthlySpec(
        path="indexPriceKlines/{sym}/{tf}/{sym}-{tf}-{month}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        needs_tf=True,
    ),
    "markPriceKlines": MonthlySpec(
        path="markPriceKlines/{sym}/{tf}/{sym}-{tf}-{month}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        needs_tf=True,
    ),
    "premiumIndexKlines": MonthlySpec(
        path="premiumIndexKlines/{sym}/{tf}/{sym}-{tf}-{month}.zip",
        time_col="open_time", time_kind="epoch", value_cols=_KLINE_COLS,
        needs_tf=True,
    ),
    "aggTrades": MonthlySpec(
        path="aggTrades/{sym}/{sym}-aggTrades-{month}.zip",
        time_col="transact_time", time_kind="epoch",
        value_cols=("agg_trade_id", "price", "quantity", "first_trade_id",
                    "last_trade_id", "transact_time", "is_buyer_maker"),
        markets=("spot", "futures"),
    ),
    "trades": MonthlySpec(
        path="trades/{sym}/{sym}-trades-{month}.zip",
        time_col="time", time_kind="epoch",
        value_cols=("id", "price", "qty", "quote_qty", "time", "is_buyer_maker"),
        markets=("spot", "futures"),
    ),
    "fundingRate": MonthlySpec(
        path="fundingRate/{sym}/{sym}-fundingRate-{month}.zip",
        time_col="calc_time", time_kind="epoch",
        value_cols=("calc_time", "funding_interval_hours", "last_funding_rate"),
    ),
}


def validate_monthly(datasets: Any, market: str = "futures") -> list[str]:
    """月归档数据集名/市场校验（与日归档同一套错误码）。"""
    names = [str(d).strip() for d in datasets] if datasets else []
    if not names:
        raise FlowError(E1004, "必须给至少一个数据集", path="datasets")
    bad = [d for d in names if d not in MONTHLY_DATASETS]
    if bad:
        raise FlowError(
            E1004, f"未知月度归档数据集 {bad}", path="datasets",
            hint=f"可用: {', '.join(MONTHLY_DATASETS)}",
        )
    if market not in ("futures", "spot"):
        raise FlowError(E1004, f"未知市场 {market!r}", path="market")
    incompatible = [d for d in names if market not in MONTHLY_DATASETS[d].markets]
    if incompatible:
        raise FlowError(
            E1004, f"{market} 市场没有 {incompatible} 月度归档", path="datasets",
            hint=f"该市场可用: {', '.join(d for d in MONTHLY_DATASETS if market in MONTHLY_DATASETS[d].markets)}",
        )
    return list(dict.fromkeys(names))


def validate_datasets(datasets: Any, market: str = "futures") -> list[str]:
    """校验数据集名与市场的相容性，返回规范化后的列表。"""
    names = [str(d).strip() for d in datasets] if datasets else []
    if not names:
        raise FlowError(E1004, "必须给至少一个数据集", path="datasets")
    bad = [d for d in names if d not in DAILY_DATASETS]
    if bad:
        raise FlowError(
            E1004, f"未知日归档数据集 {bad}", path="datasets",
            hint=f"可用: {', '.join(DAILY_DATASETS)}",
        )
    if market not in ("spot", "futures"):
        raise FlowError(E1004, f"未知市场 {market!r}", path="market")
    incompatible = [d for d in names if market not in DAILY_DATASETS[d].markets]
    if incompatible:
        raise FlowError(
            E1004, f"{market} 市场没有 {incompatible} 归档", path="datasets",
            hint=f"该市场可用: {', '.join(d for d in DAILY_DATASETS if market in DAILY_DATASETS[d].markets)}",
        )
    return list(dict.fromkeys(names))


def market_prefix(market: str) -> str:
    """市场 → 备份站路径段。U 本位合约在 ``futures/um`` 下（币本位另是 ``futures/cm``）。"""
    if market == "futures":
        return "futures/um"
    if market == "spot":
        return "spot"
    raise FlowError(E1004, f"未知市场 {market!r}", path="market")


def daily_url(
    dataset: str, symbol: str, day: str | date,
    *, timeframe: str = "1d", market: str = "futures",
) -> str:
    """按日模板构造归档 URL。``symbol`` 用币安字形（BTCUSDT），``day`` 用 YYYY-MM-DD。"""
    spec = DAILY_DATASETS.get(dataset)
    if spec is None:
        raise FlowError(E1004, f"未知数据集 {dataset!r}", path="datasets")
    if market not in spec.markets:
        raise FlowError(
            E1004, f"{market} 市场没有 {dataset} 归档", path="market",
        )
    day_text = day.isoformat() if isinstance(day, date) else str(day)
    path = spec.path.format(sym=_vision_symbol(symbol), tf=str(timeframe), day=day_text)
    return f"{BASE_URL}/data/{market_prefix(market)}/daily/{path}"


def day_range(start: date, end: date) -> list[date]:
    """闭区间内的日期序列。``end`` 含当天。"""
    if end < start:
        raise FlowError(E1004, "start 不能晚于 end", path="range")
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=1)
    return out


def month_range(start: date, end: date) -> list[str]:
    """闭区间内的月份键 ``YYYY-MM``（含首尾各自所在月）。"""
    if end < start:
        raise FlowError(E1004, "start 不能晚于 end", path="range")
    out: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def monthly_url(
    dataset: str, symbol: str, month: str, *, market: str = "futures",
    timeframe: str = "1d",
) -> str:
    """按月模板构造归档 URL。``month`` 是 ``YYYY-MM``，symbol 用币安字形。

    K 线类（``needs_tf``）路径带 ``<tf>`` 段，用 ``timeframe`` 填充；非 K 线忽略。
    """
    spec = MONTHLY_DATASETS.get(dataset)
    if spec is None:
        raise FlowError(E1004, f"未知月度数据集 {dataset!r}", path="datasets")
    if market not in spec.markets:
        raise FlowError(E1004, f"{market} 市场没有 {dataset} 月度归档", path="market")
    fields: dict[str, str] = {"sym": _vision_symbol(symbol), "month": str(month)}
    if spec.needs_tf:
        fields["tf"] = str(timeframe or "1d").strip().lower()
    path = spec.path.format(**fields)
    return f"{BASE_URL}/data/{market_prefix(market)}/monthly/{path}"


def _http_get(url: str, *, timeout: float, retries: int, proxy: str | None) -> bytes:
    """带代理与重试的 GET。404 原样抛出，由调用方按缺档处理。"""
    import time
    import urllib.error
    import urllib.request

    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "factorfactory/1.0"})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except Exception as exc:
            last = exc
        if attempt < retries - 1:
            time.sleep(min(1.5 * (2 ** min(attempt, 3)), 15.0))
    raise FlowError(
        E4002, f"下载失败（重试 {retries} 次）: {url}", path="source.binance_vision",
        context={"error": f"{type(last).__name__}: {last}" if last else "unknown"},
    )


Downloader = Callable[[str], bytes]


def _verify_checksum(blob: bytes, url: str, get: Downloader) -> None:
    """下载 ``<url>.CHECKSUM`` 并比对 sha256；不符即拒绝使用。"""
    try:
        raw = get(url + ".CHECKSUM")
    except Exception as exc:
        raise FlowError(
            E4002, f"缺校验文件: {url}.CHECKSUM", path="source.binance_vision",
            context={"error": f"{type(exc).__name__}: {exc}"},
        ) from None
    expected = raw.decode("utf-8", errors="replace").splitlines()[0].strip().split()[0] \
        if raw else ""
    if not expected:
        raise FlowError(E4002, f"校验文件为空: {url}.CHECKSUM", path="source.binance_vision")
    actual = hashlib.sha256(blob).hexdigest()
    if actual.lower() != expected.lower():
        raise FlowError(
            E4002, f"sha256 不匹配: {url}", path="source.binance_vision",
            context={"expected": expected, "actual": actual},
        )


def _to_utc(values: pd.Series, kind: str) -> pd.Series:
    """时间列 → UTC 感知时间戳。

    epoch 按位数判断单位：13 位 ms、16 位 us、19 位 ns。
    近年 aggTrades 有微秒样本，写死 ``unit='ms'`` 会把时间落到 1970 年且不报错。
    """
    if kind == "string":
        return pd.to_datetime(values, utc=True, errors="coerce")
    numeric = pd.to_numeric(values, errors="coerce")
    magnitude = numeric.dropna().abs().median() if not numeric.dropna().empty else 0
    if magnitude >= 1e17:
        unit = "ns"
    elif magnitude >= 1e14:
        unit = "us"
    else:
        unit = "ms"
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")


def _parse_archive(
    dataset: str, blob: bytes, spec: DailySpec | MonthlySpec,
) -> pd.DataFrame:
    """解析一个归档 zip → 带 UTC ``timestamp`` 列的原始 DataFrame。

    日/月归档共用：列名按源文件保留（不重命名/裁剪），只把 ``spec.time_col``
    统一成 ``timestamp``。时间列以注册表为唯一真相，不在解析里硬编码数据集分支。
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise FlowError(E4002, f"{dataset} zip 内无 CSV", path="source.binance_vision")
        payload = archive.read(names[0])

    first = payload.split(b"\n", 1)[0].split(b",", 1)[0].strip()
    has_header = not first.isdigit()
    raw = pd.read_csv(io.BytesIO(payload), header=0 if has_header else None)
    if has_header:
        # 带表头时列名以文件为准；缺时间列则报错，不猜。
        if spec.time_col not in raw.columns:
            raise FlowError(
                E4002, f"{dataset} CSV 缺时间列 {spec.time_col}，实际 {list(raw.columns)}",
                path="source.binance_vision",
            )
    else:
        if raw.shape[1] < len(spec.value_cols):
            raise FlowError(
                E4002, f"{dataset} CSV 无表头且列数不足: {raw.shape[1]} < {len(spec.value_cols)}",
                path="source.binance_vision",
            )
        raw = raw.iloc[:, : len(spec.value_cols)].copy()
        raw.columns = list(spec.value_cols)

    out = raw.copy()
    stamps = _to_utc(out[spec.time_col], spec.time_kind)
    out[TIMESTAMP_COL] = stamps
    if spec.time_col != TIMESTAMP_COL:
        out = out.drop(columns=[spec.time_col])
    out = out.loc[out[TIMESTAMP_COL].notna()].copy()
    return out.reset_index(drop=True)


def parse_daily(dataset: str, blob: bytes) -> pd.DataFrame:
    """解析一个日归档 zip → 带 UTC ``timestamp`` 的原始 DataFrame。"""
    spec = DAILY_DATASETS.get(dataset)
    if spec is None:
        raise FlowError(E1004, f"未知数据集 {dataset!r}", path="datasets")
    return _parse_archive(dataset, blob, spec)


def parse_monthly(dataset: str, blob: bytes) -> pd.DataFrame:
    """解析一个月度归档 zip → 带 UTC ``timestamp`` 的原始 DataFrame。"""
    spec = MONTHLY_DATASETS.get(dataset)
    if spec is None:
        raise FlowError(E1004, f"未知月度数据集 {dataset!r}", path="datasets")
    return _parse_archive(dataset, blob, spec)


def _is_missing(exc: Exception) -> bool:
    if getattr(exc, "code", None) == 404:
        return True
    return isinstance(exc, FlowError) and "404" in str(exc)


@dataclass
class DailyReport:
    """单数据集的下载汇总。"""

    dataset: str
    days: int = 0
    rows: int = 0
    missing: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset, "days": self.days, "rows": self.rows,
            "missing": self.missing, "errors": self.errors,
        }


def download_daily(
    datasets: list[str],
    *,
    symbols: list[str],
    vision_symbols: dict[str, str],
    days: list[date],
    timeframe: str = "1d",
    market: str = "futures",
    sink: Callable[[str, str, str, pd.DataFrame], None],
    downloader: Downloader | None = None,
    proxy: str | None = None,
    timeout: float = 60.0,
    retries: int = 6,
    workers: int = 8,
    verify: bool = True,
) -> list[DailyReport]:
    """按日模板下载并交给 ``sink(dataset, ccxt_symbol, day, frame)`` 落盘。

    404 计 missing（未上市/未归档，正常）；网络失败计 errors（可重跑补齐）；
    checksum 不符直接抛出（数据损坏必须 fail-closed）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not symbols:
        raise FlowError(E1004, "必须给标的清单", path="symbols")
    if not days:
        raise FlowError(E1004, "日期区间为空", path="range")

    get: Downloader = downloader or (
        lambda url: _http_get(url, timeout=timeout, retries=retries, proxy=proxy)
    )

    def fetch_one(dataset: str, symbol: str, day: date) -> tuple[pd.DataFrame | None, str]:
        sym = vision_symbols.get(symbol) or _vision_symbol(symbol)
        url = daily_url(dataset, sym, day, timeframe=timeframe, market=market)
        try:
            blob = get(url)
        except Exception as exc:
            return None, "missing" if _is_missing(exc) else "error"
        if verify:
            _verify_checksum(blob, url, get)
        return parse_daily(dataset, blob), "ok"

    reports: list[DailyReport] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for dataset in datasets:
            report = DailyReport(dataset=dataset)
            # 按标的分批：一个标的的所有日并发，收齐即 sink，进程中断时已完成标的已落盘。
            for symbol in symbols:
                futures = {
                    pool.submit(fetch_one, dataset, symbol, day): day
                    for day in days
                }
                frames: list[tuple[date, pd.DataFrame]] = []
                for future in as_completed(futures):
                    frame, status = future.result()
                    if status == "missing":
                        report.missing += 1
                    elif status == "error":
                        report.errors += 1
                    elif frame is not None and not frame.empty:
                        frames.append((futures[future], frame))
                if frames:
                    frames.sort(key=lambda pair: pair[0])
                    for day, frame in frames:
                        sink(dataset, symbol, day.isoformat(), frame)
                        report.rows += len(frame)
                    report.days += len(frames)
            reports.append(report)
    return reports


def download_monthly(
    datasets: list[str],
    *,
    symbols: list[str],
    vision_symbols: dict[str, str],
    months: list[str],
    timeframe: str = "1d",
    market: str = "futures",
    sink: Callable[[str, str, str, pd.DataFrame], None],
    downloader: Downloader | None = None,
    proxy: str | None = None,
    timeout: float = 60.0,
    retries: int = 6,
    workers: int = 8,
    verify: bool = True,
) -> list[DailyReport]:
    """按月模板下载并交给 ``sink(dataset, ccxt_symbol, month, frame)`` 落盘。

    与 :func:`download_daily` 同语义：404 计 missing（未上市/未归档），
    网络失败计 errors，checksum 不符直接抛（fail-closed）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not symbols:
        raise FlowError(E1004, "必须给标的清单", path="symbols")
    if not months:
        raise FlowError(E1004, "月份区间为空", path="range")

    get: Downloader = downloader or (
        lambda url: _http_get(url, timeout=timeout, retries=retries, proxy=proxy)
    )

    def fetch_one(dataset: str, symbol: str, month: str) -> tuple[pd.DataFrame | None, str]:
        sym = vision_symbols.get(symbol) or _vision_symbol(symbol)
        url = monthly_url(dataset, sym, month, market=market, timeframe=timeframe)
        try:
            blob = get(url)
        except Exception as exc:
            return None, "missing" if _is_missing(exc) else "error"
        if verify:
            _verify_checksum(blob, url, get)
        return parse_monthly(dataset, blob), "ok"

    reports: list[DailyReport] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for dataset in datasets:
            report = DailyReport(dataset=dataset)
            for symbol in symbols:
                futures = {
                    pool.submit(fetch_one, dataset, symbol, m): m for m in months
                }
                frames: list[tuple[str, pd.DataFrame]] = []
                for future in as_completed(futures):
                    frame, status = future.result()
                    if status == "missing":
                        report.missing += 1
                    elif status == "error":
                        report.errors += 1
                    elif frame is not None and not frame.empty:
                        frames.append((futures[future], frame))
                if frames:
                    frames.sort(key=lambda pair: pair[0])
                    for month, frame in frames:
                        sink(dataset, symbol, month, frame)
                        report.rows += len(frame)
                    report.days += len(frames)
            reports.append(report)
    return reports


def _vision_symbol(ccxt_symbol: str) -> str:
    """符号 → 币安字形。兼容 ccxt 形态与已是币安字形的输入。

    ``BTC/USDT:USDT`` → ``BTCUSDT``；``BTCUSDT`` 原样返回（幂等）。
    """
    text = str(ccxt_symbol).strip()
    if "/" not in text:
        # 已经是币安字形（universe 里没探到、直接给了大写字形）。
        if not text.isalnum() or not text:
            raise FlowError(E1004, f"非法币安字形: {ccxt_symbol!r}", path="symbols")
        return text.upper()
    parts = text.split("/")
    if not parts[0] or not parts[1]:
        raise FlowError(E1004, f"无法推出币安字形: {ccxt_symbol!r}", path="symbols")
    return (parts[0] + parts[1].split(":", 1)[0]).upper()


__all__ = [
    "BASE_URL", "DAILY_DATASETS", "DailySpec", "DailyReport", "Downloader",
    "MONTHLY_DATASETS", "MonthlySpec", "validate_monthly",
    "monthly_url", "month_range", "parse_monthly", "download_monthly",
    "validate_datasets", "market_prefix", "daily_url", "day_range", "parse_daily",
    "download_daily", "_vision_symbol",
]
