"""四数据集采集（19-supply.md §2）：ohlcv / funding / open_interest / basis。

两条来源，同一条落盘路径：

1. **交易所**（ccxt）：分页拉取，逐标的写 `store`；
2. **V4 快照**（`--from-v4`）：把 `data/<exchange>/<market>/**.feather` 读进来，
   列名 `date` → `timestamp`，naive 时间按 UTC 解释。V4 的事件流文件名用的是
   `-funding_rate.feather` 后缀，映射在 `V4_STREAM_SUFFIX`，不扩散到新路径。

市场维度（spot / futures）与数据集的相容性在**连网之前**校验（`store.require_market*`）：
现货没有资金费、没有持仓量，`basis` 的定义域也在合约侧。

为什么两条都要：V4 已有 2000+ 个文件是既有事实，重下既慢又可能拿不到同样区间
（OI 交易所只留 30 天）；而新数据只能从交易所来。两者最终都落到同一份 parquet，
所以读取侧不需要知道数据是导入的还是现采的。

时间语义按数据集分开（19-supply.md §3）：
- bar（ohlcv / basis）：等距，`timeframe` 是身份的一部分；
- 事件流（funding / open_interest / basis）：间隔会变，**不接受** timeframe 参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from contracts.errors import E1004, E1014, E4002, FlowError

from . import store

V4_COLUMN_ALIASES = {
    "date": store.TIMESTAMP_COL,
    "time": store.TIMESTAMP_COL,
    "timestamp": store.TIMESTAMP_COL,
    "fundingRate": "funding_rate",
    "funding_rate": "funding_rate",
    "openInterestAmount": "open_interest",
    "openInterestValue": "open_interest",
    "open_interest": "open_interest",
    "basis": "basis",
}


@dataclass
class FetchReport:
    """一次采集的账。它不进指纹，只是给人看的。"""

    dataset: str
    source: str
    symbols: int = 0
    rows_added: int = 0
    revised: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "symbols": self.symbols,
            "rows_added": self.rows_added,
            "revised": self.revised,
            "skipped": list(self.skipped),
        }


def _window(frame: pd.DataFrame, start: datetime | None, end: datetime | None) -> pd.DataFrame:
    if start is None and end is None:
        return frame
    stamps = frame[store.TIMESTAMP_COL]
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        mask &= stamps >= pd.Timestamp(start)
    if end is not None:
        mask &= stamps <= pd.Timestamp(end)
    return frame.loc[mask]


def _utc(frame: pd.DataFrame) -> pd.DataFrame:
    """date → timestamp，naive 按 UTC 解释。

    V4 落盘一律 UTC 但**不带时区**。直接 `tz_localize("UTC")` 是唯一正确的解释；
    若按本地时区解释，同一份文件在不同机器上会落到不同日期。
    """
    out = frame.rename(columns={k: v for k, v in V4_COLUMN_ALIASES.items() if k in frame.columns})
    if store.TIMESTAMP_COL not in out.columns:
        raise FlowError(
            E1014, f"源文件缺时间列: {list(frame.columns)}", path="timestamp",
            hint="只接受 date / time / timestamp 三种列名",
        )
    stamps = out[store.TIMESTAMP_COL]
    if not pd.api.types.is_datetime64_any_dtype(stamps):
        stamps = pd.to_datetime(stamps, errors="raise")
    if getattr(stamps.dt, "tz", None) is None:
        out[store.TIMESTAMP_COL] = stamps.dt.tz_localize("UTC")
    else:
        out[store.TIMESTAMP_COL] = stamps.dt.tz_convert("UTC")
    return out


def _normalise_columns(frame: pd.DataFrame, dataset: str) -> pd.DataFrame:
    out = _utc(frame)
    wanted = [store.TIMESTAMP_COL, *store.VALUE_COLUMNS[dataset]]
    missing = [c for c in wanted if c not in out.columns]
    if missing:
        raise FlowError(
            E1014, f"{dataset} 缺列 {missing}，源列 {list(frame.columns)}", path="columns",
        )
    return out[wanted]


def v4_ohlcv_path(v4_root: Path, market: str, stem: str, timeframe: str) -> Path:
    """V4 的 ohlcv：`<v4_root>/<market>/<STEM>-<tf>-<market>.feather`。"""
    return Path(v4_root) / market / f"{stem}-{timeframe}-{market}.feather"


V4_STREAM_SUFFIX = {"funding": "funding_rate", "open_interest": "open_interest", "basis": "basis"}


def v4_stream_path(v4_root: Path, market: str, stem: str, dataset: str, timeframe: str) -> Path:
    """V4 的事件流：`<v4_root>/<market>/crypto_streams/<STEM>-<tf>-<kind>.feather`。

    注意 V4 文件名里的 `-1d-` 是**假标签**：funding 文件实际存的是 4h/8h 结算事件。
    导入时只把它当定位串，真实间隔由数据决定（store 会现算）。
    """
    suffix = V4_STREAM_SUFFIX[dataset]
    return Path(v4_root) / market / "crypto_streams" / f"{stem}-{timeframe}-{suffix}.feather"


def _target_file(root, dataset: str, exchange: str, market: str, symbol: str, timeframe: str) -> Path:
    """落盘路径：只有 bar 数据集带 tf。V4 的定位串（`-1d-`）不能带进目标文件名。"""
    return store.dataset_file(
        root, dataset, exchange, market, symbol,
        timeframe if dataset == store.BAR_DATASET else None,
    )


def import_v4(
    root: str | Path,
    v4_root: str | Path,
    datasets: Sequence[str],
    *,
    exchange: str,
    market: str = "futures",
    symbols: Sequence[str] | None = None,
    timeframe: str = "1d",
    start: datetime | None = None,
    end: datetime | None = None,
    revise: bool = False,
) -> list[FetchReport]:
    """从 V4 feather 快照导入。符号必须由调用方给出，不从文件名反推。

    为什么要求符号清单：`1000PEPE_USDT_USDT` 反解成 `1000PEPE/USDT:USDT` 需要猜
    段数，猜错就把两个不同的合约当成一个（V4 里 `1000PEPE` 与 `PEPE` 是两份数据）。
    """
    if not symbols:
        raise FlowError(
            E1004, "从 V4 导入必须给标的清单", path="symbols",
            hint="用 --symbol-set 从 schemas/symbol_sets.json 取，或 --symbols 直接给",
        )
    v4 = Path(v4_root)
    if not v4.exists():
        raise FlowError(E4002, f"V4 数据根不存在: {v4}", path=str(v4))
    # 市场相容在**写盘之前**拦：否则现货符号会被拿去查合约文件，逐个 no_file
    # 之后命令仍然成功，看上去像「这几只没数据」。
    store.require_market(market)
    store.require_market_symbols(market, symbols)
    for dataset in datasets:
        store.require_market_dataset(market, dataset)

    reports: list[FetchReport] = []
    for dataset in datasets:
        report = FetchReport(dataset=dataset, source=f"v4:{v4}")
        by_stem = {store.file_stem(s): s for s in symbols}
        for stem, symbol in by_stem.items():
            if dataset == store.BAR_DATASET:
                source = v4_ohlcv_path(v4, market, stem, timeframe)
            else:
                source = v4_stream_path(v4, market, stem, dataset, timeframe)
            if not source.exists():
                report.skipped.append({"symbol": symbol, "reason": "no_file", "path": str(source)})
                continue
            try:
                frame = pd.read_feather(source)
                frame = _normalise_columns(frame, dataset)
            except FlowError as exc:
                report.skipped.append({"symbol": symbol, "reason": exc.code, "detail": str(exc)})
                continue
            except (OSError, ValueError) as exc:
                report.skipped.append(
                    {"symbol": symbol, "reason": "unreadable", "detail": f"{type(exc).__name__}: {exc}"}
                )
                continue
            frame = _window(frame, start, end)
            if frame.empty:
                report.skipped.append({"symbol": symbol, "reason": "empty_window"})
                continue
            target = _target_file(root, dataset, exchange, market, symbol, timeframe)
            stats = store.upsert(target, frame, dataset, symbol=symbol, revise=revise)
            report.symbols += 1
            report.rows_added += stats["added"]
            report.revised += stats["revised"]
        reports.append(report)
    return reports


# ---------------------------------------------------------------- ccxt 采集
def _paginate(fetch, *, start_ms: int, end_ms: int, limit: int, max_pages: int = 10_000):
    """通用分页：按最后一根的时间戳推进，页不足 limit 视为到头。"""
    since = start_ms
    pages = 0
    while pages < max_pages:
        page = fetch(since, limit)
        pages += 1
        if not page:
            return
        last = None
        for row in page:
            last = row
            yield row
        if last is None:
            return
        stamp = int(last[0] if not isinstance(last, Mapping) else last.get("timestamp", 0))
        if stamp <= since:
            return
        since = stamp + 1
        if since > end_ms:
            return
        if len(page) < limit:
            return


def _ohlcv_frame(exchange, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> pd.DataFrame:
    rows = list(_paginate(
        lambda since, lim: exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=lim),
        start_ms=int(start.timestamp() * 1000), end_ms=int(end.timestamp() * 1000), limit=limit,
    ))
    if not rows:
        return pd.DataFrame(columns=[store.TIMESTAMP_COL, *store.VALUE_COLUMNS["ohlcv"]])
    frame = pd.DataFrame(rows, columns=[store.TIMESTAMP_COL, "open", "high", "low", "close", "volume"])
    frame[store.TIMESTAMP_COL] = pd.to_datetime(frame[store.TIMESTAMP_COL], unit="ms", utc=True)
    return frame


def _funding_frame(exchange, symbol: str, start: datetime, end: datetime, limit: int) -> pd.DataFrame:
    if not hasattr(exchange, "fetch_funding_rate_history"):
        raise FlowError(
            E4002, f"{type(exchange).__name__} 不支持 fetch_funding_rate_history",
            path="funding_rate",
        )
    records: list[dict[str, Any]] = []
    for row in _paginate(
        lambda since, lim: exchange.fetch_funding_rate_history(symbol, since=since, limit=lim),
        start_ms=int(start.timestamp() * 1000), end_ms=int(end.timestamp() * 1000), limit=limit,
    ):
        records.append({"timestamp": row.get("timestamp"), "funding_rate": row.get("fundingRate")})
    frame = pd.DataFrame(records)
    if frame.empty:
        return pd.DataFrame(columns=[store.TIMESTAMP_COL, "funding_rate"])
    frame[store.TIMESTAMP_COL] = pd.to_datetime(frame[store.TIMESTAMP_COL], unit="ms", utc=True)
    return frame


def _oi_frame(exchange, symbol: str, start: datetime, end: datetime, limit: int) -> pd.DataFrame:
    if not hasattr(exchange, "fetch_open_interest_history"):
        raise FlowError(
            E4002, f"{type(exchange).__name__} 不支持 fetch_open_interest_history",
            path="open_interest",
        )
    records: list[dict[str, Any]] = []
    for row in _paginate(
        lambda since, lim: exchange.fetch_open_interest_history(symbol, "1h", since=since, limit=lim),
        start_ms=int(start.timestamp() * 1000), end_ms=int(end.timestamp() * 1000), limit=limit,
    ):
        value = row.get("openInterestAmount")
        if value is None:
            value = row.get("openInterestValue")
        records.append({"timestamp": row.get("timestamp"), "open_interest": value})
    frame = pd.DataFrame(records)
    if frame.empty:
        return pd.DataFrame(columns=[store.TIMESTAMP_COL, "open_interest"])
    frame[store.TIMESTAMP_COL] = pd.to_datetime(frame[store.TIMESTAMP_COL], unit="ms", utc=True)
    return frame


def _basis_frame(perp: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    """basis = perp_close / spot_close − 1，按日期内连接。"""
    left = perp[[store.TIMESTAMP_COL, "close"]].rename(columns={"close": "perp"})
    right = spot[[store.TIMESTAMP_COL, "close"]].rename(columns={"close": "spot"})
    merged = left.merge(right, on=store.TIMESTAMP_COL, how="inner")
    merged = merged[merged["spot"] != 0]
    return pd.DataFrame({
        store.TIMESTAMP_COL: merged[store.TIMESTAMP_COL],
        "basis": merged["perp"] / merged["spot"] - 1.0,
    })


def fetch_ccxt(
    root: str | Path,
    exchange,
    datasets: Sequence[str],
    *,
    exchange_id: str,
    market: str = "futures",
    symbols: Sequence[str] | None = None,
    timeframe: str = "1d",
    start: datetime,
    end: datetime,
    limit: int = 1000,
    spot_exchange=None,
    revise: bool = False,
) -> list[FetchReport]:
    """从交易所采集四类数据。单标的失败不中断其它标的（记 skipped）。"""
    if not symbols:
        raise FlowError(E1004, "采集必须给标的清单", path="symbols")
    store.require_market(market)
    store.require_market_symbols(market, symbols)
    for dataset in datasets:
        store.require_market_dataset(market, dataset)

    reports: list[FetchReport] = []
    for dataset in datasets:
        report = FetchReport(dataset=dataset, source=f"ccxt:{exchange_id}")
        for symbol in symbols:
            try:
                if dataset == store.BAR_DATASET:
                    frame = _ohlcv_frame(exchange, symbol, timeframe, start, end, limit)
                elif dataset == "funding":
                    frame = _funding_frame(exchange, symbol, start, end, limit)
                elif dataset == "open_interest":
                    frame = _oi_frame(exchange, symbol, start, end, limit)
                else:
                    if spot_exchange is None:
                        raise FlowError(
                            E4002, "basis 需要 spot 交易所实例", path="basis",
                            hint="basis 由 perp/spot 收盘价派生，缺一边无法计算",
                        )
                    perp = _ohlcv_frame(exchange, symbol, timeframe, start, end, limit)
                    spot_symbol = symbol.split(":")[0]
                    spot = _ohlcv_frame(spot_exchange, spot_symbol, timeframe, start, end, limit)
                    frame = _basis_frame(perp, spot)
            except FlowError as exc:
                report.skipped.append({"symbol": symbol, "reason": exc.code, "detail": str(exc)})
                continue
            except Exception as exc:  # 断网/限流/交易所结构变化
                report.skipped.append(
                    {"symbol": symbol, "reason": "fetch_failed", "detail": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if frame is None or frame.empty:
                report.skipped.append({"symbol": symbol, "reason": "empty"})
                continue
            frame = _window(_normalise_columns(frame, dataset), start, end)
            if frame.empty:
                report.skipped.append({"symbol": symbol, "reason": "empty_window"})
                continue
            target = _target_file(root, dataset, exchange_id, market, symbol, timeframe)
            stats = store.upsert(target, frame, dataset, symbol=symbol, revise=revise)
            report.symbols += 1
            report.rows_added += stats["added"]
            report.revised += stats["revised"]
        reports.append(report)
    return reports


__all__ = [
    "FetchReport", "import_v4", "fetch_ccxt",
    "v4_ohlcv_path", "v4_stream_path",
]
