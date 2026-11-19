"""交易对元数据采集：**通过 ccxt ``load_markets`` 读取交易所 exchangeInfo 原始字段**。

这是 L0 三层里的第一层，唯一真相来源：

- U 本位合约：ccxt ``binanceusdm`` → ``fapi/v1/exchangeInfo``
- 币本位合约：ccxt ``binancecoinm`` → ``dapi/v1/exchangeInfo``
- 现货：ccxt ``binance`` → ``api/v3/exchangeInfo``

**为什么走 ccxt、不再自己封装 REST**：ccxt 的 ``market['info']`` 完整保留了
交易所返回的原始字典（``contractStatus`` / ``underlyingType`` /
``onboardDate`` / ``deliveryDate`` 一个不缺），而 ``load_markets`` 默认不过滤
非活跃合约——下架判定需要的字段全部拿得到。自己再写一遍 HTTP + 代理 + 重试
只是重复 ccxt 已有的能力。注意读的是 ``market['info']`` **原始字典**，不是
ccxt 顶层归一后的 market（后者做了 ``active = status=='TRADING'`` 等加工，
会折叠掉我们判退市需要的语义）。

**下架合约拿不拿得到**：exchangeInfo 返回交易所当前仍配置的合约，
``contractStatus=CLOSE``（或 ``status=BREAK/SETTLING``）即为已下架。实测 FTT
这种 2022 年退市的合约仍在返回列表里。但它不是"有史以来所有合约"的档案——
交易所没有承诺永久保留，所以本模块把每次接口快照落盘成 json 累积：接口里
消失的标的仍留在历史表里，不会被静默抹掉。

采集器可注入（``client`` / ``fetcher``），归一化是纯函数，测试不需要网络。
ccxt 实例的构造由 :mod:`services.market_data` 的交易所适配器负责，本模块只
认"能返回原始 exchangeInfo symbols 的数据源"。

落点：``<root>/base/universe/exchange_info_<market>.json``（原始快照），
由 universe_service 合成 ``markets.jsonl``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts.errors import E1004, E4002, FlowError

from . import universe_service as us

# 交易所状态：这些值表示不再交易。
CLOSED_CONTRACT_STATUSES = frozenset({"CLOSE", "BREAK", "SETTLING", "PENDING_TRADING"})
CLOSED_SYMBOL_STATUSES = frozenset({"CLOSE", "BREAK", "SETTLING", "PRE_TRADING"})
TRADING_CONTRACT_STATUSES = frozenset({"TRADING", "PRE_OPEN"})

# 数据源：返回 ``{"symbols": [原始 exchangeInfo 条目, ...]}``。
SymbolSource = Callable[[], dict[str, Any]]


def snapshot_path(root: str | Path, market: str) -> Path:
    return us.base_root(root) / "universe" / f"exchange_info_{market}.json"


def fetch_via_ccxt(client: Any, *, market: str | None = None) -> dict[str, Any]:
    """用注入的 ccxt client 拉市场清单，提取每笔 ``market['info']`` 原始字段。

    返回 ``{"serverTime": ..., "symbols": [...]}``，结构与交易所 exchangeInfo
    一致，可直接喂给 :func:`normalize` 与 :func:`write_snapshot`。``market``
    只用于错误信息，不影响提取（client 已绑定市场）。
    """
    if client is None:
        raise FlowError(
            E4002, "元数据采集需要 ccxt client", path="exchange.metadata",
            hint="由 market_data.get_adapter(...).make_client() 构造后注入",
        )
    try:
        markets = client.load_markets()
    except Exception as exc:
        raise FlowError(
            E4002, f"ccxt load_markets 失败（market={market}）", path="exchange.metadata",
            context={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    symbols = [
        dict(m["info"]) for m in (markets or {}).values()
        if isinstance(m, Mapping) and isinstance(m.get("info"), Mapping)
    ]
    if not symbols:
        raise FlowError(
            E4002, f"ccxt 返回的市场清单为空（market={market}）", path="exchange.metadata",
            hint="检查交易所实例是否绑定了正确市场（um/cm/spot）",
        )
    return {"serverTime": None, "symbols": symbols}


def write_snapshot(root: str | Path, market: str, data: Mapping[str, Any]) -> Path:
    """把接口原始返回落盘（含抓取时刻），便于复算与审计。"""
    import json

    path = snapshot_path(root, market)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "server_time": data.get("serverTime"),
        "symbols": list(data.get("symbols") or []),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_snapshot(root: str | Path, market: str) -> dict[str, Any] | None:
    import json

    path = snapshot_path(root, market)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _ccxt_symbol(raw: Mapping[str, Any], market: str) -> str | None:
    """交易所 symbol → ccxt 形态。``None`` 表示不纳入 universe。"""
    base = str(raw.get("baseAsset") or "").strip()
    quote = str(raw.get("quoteAsset") or "").strip()
    if not base or not quote:
        return None
    if market in ("um", "cm"):
        contract = str(raw.get("contractType") or "")
        if contract != "PERPETUAL":
            return None  # 交割合约不在 L0 范围（只做永续横截面）
        settle = "USDT" if market == "um" else str(raw.get("marginAsset") or quote)
        if quote != settle and settle != "USD":
            return None
        return f"{base}/{quote}:{settle}"
    return f"{base}/{quote}"


def is_closed(raw: Mapping[str, Any], market: str) -> bool:
    """按交易所原始字段判定是否已下架/停止交易。"""
    status = str(raw.get("status") or "").upper()
    if status in CLOSED_SYMBOL_STATUSES:
        return True
    if market in ("um", "cm"):
        contract = str(raw.get("contractStatus") or "").upper()
        if contract in CLOSED_CONTRACT_STATUSES:
            return True
    return False


def normalize(
    data: Mapping[str, Any], market: str, *, exchange: str = "binanceusdm",
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """原始 exchangeInfo → universe 记录映射 + 统计。

    每条记录保留交易所原始字段，供 universe_service 判定状态与资产类别。
    """
    out: dict[str, dict[str, Any]] = {}
    stats = {"raw": 0, "kept": 0, "skipped_non_perp": 0, "closed": 0, "unsafe_name": 0}
    for raw in data.get("symbols") or []:
        stats["raw"] += 1
        symbol = _ccxt_symbol(raw, market)
        if symbol is None:
            stats["skipped_non_perp"] += 1
            continue
        if not us.is_storable(symbol):
            stats["unsafe_name"] += 1
            continue
        record = us.make_record(
            symbol,
            exchange=exchange,
            market="futures" if market in ("um", "cm") else "spot",
            asset_class=us.classify_asset(
                symbol, underlying_type=str(raw.get("underlyingType") or "") or None,
            ),
            contract_type=str(raw.get("contractType") or "") or None,
            underlying_type=str(raw.get("underlyingType") or "") or None,
        )
        closed = is_closed(raw, market)
        record["exchange_status"] = str(raw.get("status") or "") or None
        record["contract_status"] = str(raw.get("contractStatus") or "") or None
        record["exchange_state"] = "closed" if closed else "trading"
        record["onboard_date"] = str(raw.get("onboardDate") or "")[:10] or None
        record["delivery_date"] = str(raw.get("deliveryDate") or "")[:10] or None
        if closed:
            stats["closed"] += 1
        out[symbol] = record
        stats["kept"] += 1
    return out, stats


def collect(
    root: str | Path,
    market: str,
    *,
    allow_cache: bool = True,
    exchange: str = "binanceusdm",
    client: Any | None = None,
    fetcher: SymbolSource | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], str]:
    """采集并归一化：ccxt 接口优先，失败按 ``allow_cache`` 回退本地快照。

    ``fetcher`` 优先级最高（测试/自定义源），其次 ``client``（ccxt 实例），
    两者都缺省时报错——构造 ccxt 实例是交易所适配器的职责，不在本层。

    返回 ``(records, stats, source)``，``source`` ∈ {api, cache}。
    """
    source = "api"
    try:
        if fetcher is not None:
            data = fetcher()
        elif client is not None:
            data = fetch_via_ccxt(client, market=market)
        else:
            raise FlowError(
                E4002, "元数据采集缺少数据源", path="exchange.metadata",
                hint="传 client=ccxt 实例或 fetcher=callable",
            )
        if not isinstance(data, Mapping) or not isinstance(data.get("symbols"), list):
            raise FlowError(E4002, "数据源返回的 exchangeInfo 结构异常", path="exchange.metadata")
        write_snapshot(root, market, data)
    except FlowError as exc:
        if not allow_cache:
            raise
        cached = load_snapshot(root, market)
        if cached is None:
            raise
        data = cached
        source = "cache"
        payload = exc.error.to_dict() if hasattr(exc, "error") else {"message": str(exc)}
        records, stats = normalize(data, market, exchange=exchange)
        stats["warning"] = (
            "交易所接口不可用，回退本地快照（元数据不是最新的）: "
            f"{payload.get('message', str(exc))[:160]}"
        )
        return records, stats, source
    records, stats = normalize(data, market, exchange=exchange)
    return records, stats, source


__all__ = [
    "CLOSED_CONTRACT_STATUSES", "CLOSED_SYMBOL_STATUSES", "TRADING_CONTRACT_STATUSES",
    "SymbolSource", "fetch_via_ccxt", "write_snapshot", "load_snapshot",
    "snapshot_path", "is_closed", "normalize", "collect",
]
