"""L0 行情**原样**存储层：按数据集/标的落 parquet，保留源文件的全部列。

与主流程 ``upstream.store`` 的分工：

- 主流程 store 是**窄表契约**：一个数据集固定几列值，进 checksum、进挖掘；
- L0 store 是**原始基座**：K线留 quote_volume/count/taker_buy，metrics 留
  toptrader 比率，bookDepth 留 percentage/depth/notional。它不进主流程的
  ``DATASETS`` 白名单，因此不会被 plan 意外引用、也不会改挖掘的 checksum。

目录：``<root>/base/raw/<dataset>/<exchange>/<market>/<STEM>.parquet``

- 一个标的 parquet（不分月文件）：日归档下载按标的分批，收齐追加；
- append 时按 ``timestamp`` 去重（同一天重复跑不产生双份），保留最新值；
- 空表不落盘，避免留一个 0 行文件被误认为"采过但没数据"。

为什么不用主流程 store：它的 ``dataset_file`` 用 ``BAR_DATASET`` 判断 tf 后缀，
且 ``upsert`` 按 ``VALUE_COLUMNS`` 裁列——这里要原样保留，两者语义不同。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from contracts.errors import E1004, E4002, FlowError
from services import universe_service as us
from upstream import store as main_store

RAW_DIRNAME = "raw"
TIMESTAMP_COL = "timestamp"

# 去重键：大多数数据集 timestamp 唯一；bookDepth 同一秒有多档 percentage，
# 只按 timestamp 去重会把同秒的档位删掉（实测 63228 行被压成 5269）。
_DEDUP_KEYS: dict[str, tuple[str, ...]] = {
    "bookDepth": ("timestamp", "percentage"),
}


def _dedup_key(dataset: str, frame: pd.DataFrame) -> list[str]:
    keys = [k for k in _DEDUP_KEYS.get(dataset, (TIMESTAMP_COL,)) if k in frame.columns]
    return keys or [TIMESTAMP_COL]


def raw_root(root: str | Path) -> Path:
    return us.base_root(root) / RAW_DIRNAME


def raw_file(
    root: str | Path,
    dataset: str,
    symbol: str,
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
) -> Path:
    stem = main_store.file_stem(symbol)
    if not us.is_storable(symbol):
        raise FlowError(E1004, f"标的不能安全落盘: {symbol!r}", path="symbols")
    return raw_root(root) / dataset / exchange / market / f"{stem}.parquet"


def append_frame(
    root: str | Path,
    dataset: str,
    symbol: str,
    frame: pd.DataFrame,
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
    revise: bool = False,
) -> dict[str, int]:
    """把一个日归档的解析结果并入标的 parquet。返回 added/replaced/total。"""
    if frame is None or frame.empty:
        return {"added": 0, "replaced": 0, "total": 0}
    if TIMESTAMP_COL not in frame.columns:
        raise FlowError(E4002, f"{dataset} 帧缺 timestamp 列", path="l0.store")

    path = raw_file(root, dataset, symbol, exchange=exchange, market=market)
    path.parent.mkdir(parents=True, exist_ok=True)

    incoming = frame.copy()
    incoming[TIMESTAMP_COL] = pd.to_datetime(incoming[TIMESTAMP_COL], utc=True)
    dedup = _dedup_key(dataset, incoming)
    incoming = incoming.drop_duplicates(subset=dedup, keep="last")

    if path.exists():
        try:
            old = pd.read_parquet(path)
        except Exception as exc:
            raise FlowError(
                E4002, f"L0 parquet 损坏: {path}", path="l0.store",
                context={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        old[TIMESTAMP_COL] = pd.to_datetime(old[TIMESTAMP_COL], utc=True)
        old_count = len(old)
        key = _dedup_key(dataset, old)
        # 用去重键判重叠，兼容 bookDepth 的同秒多档位；缺列则回退到 timestamp。
        key = [k for k in key if k in incoming.columns] or [TIMESTAMP_COL]
        old_keys = pd.MultiIndex.from_frame(old[key]) if len(key) > 1 else old[key[0]]
        new_keys = pd.MultiIndex.from_frame(incoming[key]) if len(key) > 1 else incoming[key[0]]
        overlap = int(new_keys.isin(old_keys).sum())
        if revise:
            # 覆盖模式：新数据替换相同键的旧值。
            combined = pd.concat([old[~old_keys.isin(new_keys)], incoming], ignore_index=True)
            replaced = overlap
        else:
            # append-only（默认）：已有键不被改写，只补新行。
            combined = pd.concat([old, incoming[~new_keys.isin(old_keys)]], ignore_index=True)
            replaced = 0
    else:
        old_count = 0
        replaced = 0
        combined = incoming

    combined = combined.drop_duplicates(subset=_dedup_key(dataset, combined), keep="last")
    combined = combined.sort_values(TIMESTAMP_COL, kind="mergesort").reset_index(drop=True)
    added = max(0, len(combined) - old_count)

    if combined.empty:
        return {"added": 0, "replaced": replaced, "total": 0}

    # 原子写：同盘 tmp + replace，读侧永不读到半个文件。
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return {"added": added, "replaced": replaced, "total": len(combined)}


def read_raw(
    root: str | Path,
    dataset: str,
    symbol: str,
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
    columns: Sequence[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """读取标的的 L0 原始数据；文件不存在返回空 DataFrame。"""
    path = raw_file(root, dataset, symbol, exchange=exchange, market=market)
    if not path.exists():
        return pd.DataFrame()
    try:
        out = pd.read_parquet(path, columns=list(columns) if columns else None)
    except Exception as exc:
        raise FlowError(
            E4002, f"L0 parquet 读取失败: {path}", path="l0.store",
            context={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if TIMESTAMP_COL in out.columns:
        out[TIMESTAMP_COL] = pd.to_datetime(out[TIMESTAMP_COL], utc=True)
        if start is not None:
            out = out.loc[out[TIMESTAMP_COL] >= start]
        if end is not None:
            out = out.loc[out[TIMESTAMP_COL] <= end]
    return out.reset_index(drop=True)


def coverage(
    root: str | Path,
    dataset: str,
    symbols: Sequence[str],
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
) -> dict[str, Any]:
    """汇总数据集的覆盖情况，用于 CLI 回执。"""
    have: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    total_rows = 0
    for symbol in symbols:
        path = raw_file(root, dataset, symbol, exchange=exchange, market=market)
        if not path.exists():
            missing.append(symbol)
            continue
        try:
            meta = pd.read_parquet(path, columns=[TIMESTAMP_COL])
        except Exception:
            missing.append(symbol)
            continue
        if meta.empty:
            missing.append(symbol)
            continue
        stamps = pd.to_datetime(meta[TIMESTAMP_COL], utc=True)
        total_rows += len(stamps)
        have[symbol] = {
            "rows": len(stamps),
            "first": stamps.min().isoformat(),
            "last": stamps.max().isoformat(),
        }
    return {
        "dataset": dataset,
        "symbols": len(have),
        "missing": len(missing),
        "rows": total_rows,
        "first": min((v["first"] for v in have.values()), default=None),
        "last": max((v["last"] for v in have.values()), default=None),
    }


__all__ = [
    "RAW_DIRNAME", "raw_root", "raw_file", "append_frame", "read_raw", "coverage",
]
