"""flow fetch —— 四数据集采集（19-supply.md §2 / §7）。

一条命令，两种来源，同一个落点：

```
flow fetch --datasets ohlcv --symbol-set crypto20 --exchange binanceusdm \
           --start 2025-01-01 --end 2025-12-31   # 从交易所增量
flow fetch --datasets ohlcv,funding --symbol-set crypto20 --exchange binanceusdm \
           --from-binance-vision --start 2019-01-01 --end 2026-10-31  # 备份站月度归档
```

没有暂存、没有版本目录、没有别名。写完就是事实：读取时现算 checksum，
它进 `blueprint_digest`，所以「这份结论用的哪份数据」仍可判定。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from contracts.errors import E1004, E4002, FlowError
from runtime.store import resolve_data_root
from storage.layout import Layout
from upstream import fetch as fetcher
from upstream import store

from .common import envelope_ok, load_json_dict


def _parse_dt(text: str | None, label: str) -> datetime | None:
    if text is None or text == "":
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise FlowError(E1004, f"--{label} 不是 RFC3339 时间: {text!r}", path=label) from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _request_symbols(args: Any, layout: Layout) -> tuple[str, ...]:
    raw = getattr(args, "symbols", None)
    named = getattr(args, "symbol_set", None)
    if bool(raw) == bool(named):
        raise FlowError(
            E1004, "fetch 必须且只能给 --symbols 或 --symbol-set 之一", path="symbols",
        )
    if raw:
        symbols = tuple(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))
    else:
        table = load_json_dict(layout.symbol_sets, code=E1004, label="标的集表")
        members = (table or {}).get(named)
        if not isinstance(members, (list, tuple)) or not members:
            raise FlowError(
                E1004, f"未知或空的标的集: {named}", path="symbol_set",
                hint="先建立 schemas/symbol_sets.json 或用 --symbols 直接给出",
            )
        symbols = tuple(dict.fromkeys(str(s) for s in members))
    if not symbols:
        raise FlowError(E1004, "标的集为空", path="symbols")
    return symbols


def _client_factory(exchange_id: str) -> Any:
    """唯一 ccxt 客户端构造点；测试替换本函数注入假客户端。"""
    import ccxt

    cls = getattr(ccxt, exchange_id, None)
    if not (isinstance(cls, type) and issubclass(cls, ccxt.Exchange)):
        raise FlowError(
            E1004, f"不是可实例化的 ccxt 交易所: {exchange_id!r}", path="exchange",
        )
    return cls()


def cmd_fetch(args: Any) -> dict[str, Any]:
    layout = Layout(resolve_data_root())
    data_root = resolve_data_root()
    datasets = [s.strip() for s in str(args.datasets or "").split(",") if s.strip()]
    if not datasets:
        raise FlowError(E1004, "fetch 必须给 --datasets", path="datasets")
    for dataset in datasets:
        if dataset not in ("ohlcv", "funding", "open_interest", "basis"):
            raise FlowError(
                E1004, f"未知数据集: {dataset!r}", path="datasets",
                hint="支持 ohlcv / funding / open_interest / basis（与 plan_schema 同一词表）",
            )
    exchange = str(getattr(args, "exchange", None) or "").strip()
    if not exchange:
        raise FlowError(E1004, "fetch 必须显式给 --exchange", path="exchange")
    market = str(getattr(args, "market", None) or store.DEFAULT_MARKET)
    timeframe = str(getattr(args, "timeframe", None) or "1d")
    symbols = _request_symbols(args, layout)

    # 市场相容校验放在最前面（连网之前）：市场与数据集、市场与标的形态任一处
    # 不自洽，都必须在这里失败，不能等到逐标的 skip 完再报成功。
    for dataset in datasets:
        store.require_market_dataset(market, dataset)
    store.require_market_symbols(market, symbols)

    start = _parse_dt(getattr(args, "start", None), "start")
    end = _parse_dt(getattr(args, "end", None), "end")
    if start and end and start >= end:
        raise FlowError(E1004, "--start 必须早于 --end", path="range")

    from_vision = bool(getattr(args, "from_binance_vision", False))
    if from_vision:
        from upstream import binance_vision

        if start is None or end is None:
            raise FlowError(
                E1004, "从备份站下载必须给 --start 与 --end", path="range",
                hint="区间决定抓哪些月份；备份站按 YYYY-MM 归档，不给区间等于抓全部",
            )
        reports = binance_vision.download_vision(
            data_root, datasets,
            exchange=exchange, market=market, symbols=symbols, timeframe=timeframe,
            start=start, end=end, proxy=getattr(args, "proxy", None),
            revise=bool(getattr(args, "revise", False)),
        )
    else:
        if start is None or end is None:
            raise FlowError(
                E1004, "从交易所采集必须给 --start 与 --end", path="range",
                hint="区间决定抓多少历史；不给区间等于让它抓全部，那是猜",
            )
        factory = getattr(args, "client_factory", None) or _client_factory
        exchange_obj = factory(exchange)
        spot_obj = None
        if "basis" in datasets:
            # basis 由 perp/spot 收盘价派生，必须两边的客户端都在。
            spot_id = str(getattr(args, "spot_exchange", None) or "binance")
            spot_obj = factory(spot_id)
        reports = fetcher.fetch_ccxt(
            data_root, exchange_obj, datasets,
            exchange_id=exchange, market=market, symbols=symbols, timeframe=timeframe,
            start=start, end=end, limit=int(getattr(args, "limit", 1000) or 1000),
            spot_exchange=spot_obj, revise=bool(getattr(args, "revise", False)),
        )

    # 零成功即失败：一次采集如果每个标的都被记成 skipped，写盘为零，命令却
    # 返回 ok=true，就是标准的假成功（用错标的集 / 用错交易所 / 区间无数据都会
    # 落到这里）。已经采到的标的不受影响，只是把「这次什么都没拿到」说清楚。
    empty = [r.dataset for r in reports if r.symbols == 0]
    if empty:
        first = next(r for r in reports if r.symbols == 0)
        raise FlowError(
            E4002,
            f"{empty} 没有任何标的采集成功（{len(first.skipped)} 个标的全部跳过）",
            path="datasets",
            hint=(
                "常见原因：标的集与市场不匹配（现货符号不带 `:USDT`）、"
                "交易所 id 写错、或区间内没有数据。逐个原因见 skipped"
            ),
            context={"skipped_sample": first.skipped[:5]},
        )

    return envelope_ok("fetch", None, {
        "exchange": exchange,
        "market": market,
        "data_root": str(data_root),
        "datasets": [r.to_dict() for r in reports],
    })


__all__ = ["cmd_fetch"]
