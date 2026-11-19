"""L0 行情增量补充：用 ccxt 把日归档之后的新数据补进同一份 L0 parquet。

与备份站日归档的分工：

- ``upstream.vision_daily`` 负责**历史初始化**：按日拉 2020 至今的静态归档；
- 本模块负责**增量**：归档有 1 天发布延迟，最近 1~2 天用 ccxt 补上。

两类数据要分开对待：

1. **历史型**（K线四类 / trades / funding / open_interest）：ccxt 带 ``since``
   翻页，能补回明确时间区间的真历史；
2. **快照型**（bookTicker / bookDepth）：ccxt 只能拿"当前"一个截面，没有历史。
   落盘时按当前 UTC 打时间戳，**每次调用只记一个点**——把它当采样序列，
   不要误当成完整历史。metrics 没有 ccxt 统一接口，用交易所原生 REST。

所有类型都写进 ``l0_store`` 的同一套 parquet，读取侧无需知道数据来自归档还是 ccxt。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from contracts.errors import E1004, E4002, FlowError
from services import l0_store
from services import universe_service as us
from upstream import vision_daily as vd

# K线类：ccxt 方法名与备份站数据集名的对应。
KLINE_METHODS: dict[str, str] = {
    "klines": "fetchOHLCV",
    "indexPriceKlines": "fetchIndexOHLCV",
    "markPriceKlines": "fetchMarkOHLCV",
    "premiumIndexKlines": "fetchPremiumIndexOHLCV",
}

# 快照类：只能取当前截面。
SNAPSHOT_DATASETS = ("bookTicker", "bookDepth")

# 原生 REST：metrics 对应币安 futures data 的 `fapi/v1/metrics`（逐 30 分钟）。
NATIVE_ENDPOINTS: dict[str, str] = {
    "metrics": "https://fapi.binance.com/futures/data/metrics",
}

# 备份站归档里 K 线的列顺序（ccxt OHLCV 只有 6 列，其余留 NaN）。
_KLINE_EXTRA = ("quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore")


def _ccxt_client(proxy: str | None, *, market: str = "futures") -> Any:
    import ccxt

    opts: dict[str, Any] = {
        "timeout": 25000, "enableRateLimit": True,
        "options": {"defaultType": "future" if market == "futures" else "spot"},
    }
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
        opts["aiohttp_trust_env"] = False
    return ccxt.binanceusdm(opts) if market == "futures" else ccxt.binance(opts)


def _kline_frame(rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """ccxt OHLCV → 与备份站同列的 DataFrame。"""
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(
        [list(row) for row in rows],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    out[TIMESTAMP_COL] = pd.to_datetime(out[TIMESTAMP_COL], unit="ms", utc=True)
    for col in _KLINE_EXTRA:
        out[col] = float("nan")
    out["ignore"] = 0
    return out


def _trades_frame(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """ccxt trade → 备份站 trades 列契约。"""
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame([
        {
            "id": r.get("id"), "price": r.get("price"), "qty": r.get("amount"),
            "quote_qty": r.get("cost"),
            "timestamp": pd.to_datetime(r.get("timestamp"), unit="ms", utc=True),
            "is_buyer_maker": r.get("side") == "sell",  # ccxt: sell = taker sell = buyer maker
        }
        for r in rows
    ])
    return out


def _event_frame(rows: Sequence[Mapping[str, Any]], value_key: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([
        {"timestamp": pd.to_datetime(r.get("timestamp"), unit="ms", utc=True),
         value_key: r.get(value_key)}
        for r in rows
    ])


TIMESTAMP_COL = "timestamp"


def _snapshot_frame(book: Mapping[str, Any], dataset: str) -> pd.DataFrame:
    """当前盘口 → 单行快照。``percentage``/``depth`` 留空，bbo 值写进列名。"""
    now = datetime.now(timezone.utc)
    if dataset == "bookTicker":
        return pd.DataFrame([{
            TIMESTAMP_COL: now,
            "bid_price": book.get("bid"), "bid_qty": book.get("bidVolume"),
            "ask_price": book.get("ask"), "ask_qty": book.get("askVolume"),
        }])
    # bookDepth：把前 N 档摊平成多行，列对齐备份站的 percentage/depth/notional。
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    rows = [
        {TIMESTAMP_COL: now, "percentage": -0.0, "depth": q, "notional": p * q,
         "side": "bid", "price": p}
        for p, q in bids[:20]
    ] + [
        {TIMESTAMP_COL: now, "percentage": 0.0, "depth": q, "notional": p * q,
         "side": "ask", "price": p}
        for p, q in asks[:20]
    ]
    return pd.DataFrame(rows)


def _fetch_ccxt(
    client: Any,
    dataset: str,
    symbol: str,
    *,
    timeframe: str,
    since_ms: int | None,
    limit: int,
) -> pd.DataFrame:
    method_name = KLINE_METHODS.get(dataset)
    if method_name:
        method = getattr(client, method_name, None)
        if method is None or not client.has.get(method_name):
            raise FlowError(
                E4002, f"ccxt 不支持 {dataset}", path="l0.incremental",
                hint=f"方法 {method_name} 不可用",
            )
        rows = method(symbol, timeframe, since=since_ms, limit=limit)
        return _kline_frame(rows)
    if dataset == "trades":
        return _trades_frame(client.fetchTrades(symbol, since=since_ms, limit=limit))
    if dataset == "funding":
        return _event_frame(
            client.fetchFundingRateHistory(symbol, since=since_ms, limit=limit),
            "fundingRate",
        ).rename(columns={"fundingRate": "funding_rate"})
    if dataset == "open_interest":
        return _event_frame(
            client.fetchOpenInterestHistory(symbol, timeframe, since=since_ms, limit=limit),
            "openInterest",
        ).rename(columns={"openInterest": "open_interest"})
    if dataset == "bookTicker":
        tickers = client.fetchBidsAsks([symbol])
        book = tickers.get(symbol) or {}
        return _snapshot_frame(book, dataset)
    if dataset == "bookDepth":
        return _snapshot_frame(client.fetchOrderBook(symbol, limit=20), dataset)
    raise FlowError(
        E1004, f"ccxt 增量不支持数据集 {dataset!r}", path="datasets",
        hint=f"可用历史型: {', '.join(KLINE_METHODS)} / trades / funding / open_interest；"
             f"快照型: {', '.join(SNAPSHOT_DATASETS)}",
    )


def _native_metrics(
    symbol: str, *, since_ms: int | None, limit: int,
    proxy: str | None, timeout: float,
) -> pd.DataFrame:
    """metrics 无 ccxt 统一接口，走币安原生 REST。"""
    import urllib.parse
    import urllib.request

    spec = vd.DAILY_DATASETS["metrics"]
    params = {
        "symbol": vd._vision_symbol(symbol),
        "period": "15m",
        "limit": min(int(limit), 1000),
    }
    if since_ms is not None:
        params["startTime"] = int(since_ms)
    url = NATIVE_ENDPOINTS["metrics"] + "?" + urllib.parse.urlencode(params)
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(
        urllib.request.Request(url, headers={"User-Agent": "factorfactory/1.0"}),
        timeout=timeout,
    ) as resp:
        import json
        rows = json.loads(resp.read().decode("utf-8"))
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    colmap = {c: c for c in out.columns}
    out = out.rename(columns=colmap)
    out[TIMESTAMP_COL] = pd.to_datetime(out["create_time"], unit="ms", utc=True)
    return out


def start_from_l0(
    root: str | Path,
    dataset: str,
    symbol: str,
    *,
    exchange: str,
    market: str,
    fallback_days: int = 3,
) -> int | None:
    """从 L0 最后一根 bar 推出 ccxt 增量起点（毫秒）。

    ``fallback_days`` 是回看余量：归档末端可能有缺口，重拉几天由 store 按
    timestamp 去重吸收，比漏掉几天安全。
    """
    path_rows = l0_store.read_raw(
        root, dataset, symbol, exchange=exchange, market=market,
        columns=[TIMESTAMP_COL],
    )
    if path_rows.empty:
        return None
    last = pd.to_datetime(path_rows[TIMESTAMP_COL], utc=True).max()
    return int((last - pd.Timedelta(days=fallback_days)).timestamp() * 1000)


def incremental_update(
    root: str | Path,
    datasets: Sequence[str],
    *,
    symbols: Sequence[str],
    exchange: str = "binanceusdm",
    market: str = "futures",
    timeframe: str = "1d",
    limit: int = 500,
    since_days: int = 3,
    proxy: str | None = None,
    client: Any | None = None,
    writer: Callable[..., dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """按数据集/标的做 ccxt 增量，写入 L0。返回每类的汇总。

    ``client`` / ``writer`` 可注入，测试不需要网络与磁盘。
    """
    from services import l0_store as default_writer

    names = list(dict.fromkeys(str(d).strip() for d in datasets if str(d).strip()))
    if not names:
        raise FlowError(E1004, "必须给至少一个数据集", path="datasets")
    if not symbols:
        raise FlowError(E1004, "必须给标的清单", path="symbols")

    allowed = set(KLINE_METHODS) | {"trades", "funding", "open_interest"} \
        | set(SNAPSHOT_DATASETS) | {"metrics"}
    unknown = [d for d in names if d not in allowed]
    if unknown:
        raise FlowError(
            E1004, f"ccxt 增量不支持 {unknown}", path="datasets",
            hint=f"可用: {', '.join(sorted(allowed))}",
        )

    ex = client if client is not None else _ccxt_client(proxy, market=market)
    write = writer or default_writer.append_frame
    out: list[dict[str, Any]] = []

    for dataset in names:
        rows_total = added_total = 0
        failed: list[str] = []
        for symbol in symbols:
            try:
                since_ms = start_from_l0(
                    root, dataset, symbol, exchange=exchange, market=market,
                    fallback_days=since_days,
                )
                if dataset == "metrics":
                    frame = _native_metrics(symbol, since_ms=since_ms, limit=limit,
                                            proxy=proxy, timeout=25.0)
                else:
                    # 快照型没有 since 语义，传 None。
                    since = None if dataset in SNAPSHOT_DATASETS else since_ms
                    frame = _fetch_ccxt(
                        ex, dataset, symbol, timeframe=timeframe,
                        since_ms=since, limit=limit,
                    )
                if frame.empty:
                    continue
                stats = write(
                    root, dataset, symbol, frame,
                    exchange=exchange, market=market, revise=True,
                )
                rows_total += len(frame)
                added_total += int(stats.get("added", 0))
            except Exception as exc:
                failed.append(f"{symbol}: {type(exc).__name__}: {exc}")
        out.append({
            "dataset": dataset,
            "symbols": len(symbols) - len(failed),
            "rows_fetched": rows_total,
            "rows_added": added_total,
            "failed": len(failed),
            "errors": failed[:10],
        })
    return out


__all__ = [
    "KLINE_METHODS", "SNAPSHOT_DATASETS", "NATIVE_ENDPOINTS",
    "incremental_update", "start_from_l0",
]
