"""基于 L0 日 K 生成挖掘用币池。

币池不是「用户手写的名单」，而是**从数据里算出来的筛选结论**：每个币为什么
进、为什么没进，都带可复算的指标与原因码。这样换一次阈值能看出差异，而不是
名单悄悄变了却没人知道。

三个判据的取舍：
- **历史长度**用归档月（`listed_at`）而不是第一根 bar。备份站当月就有文件，
  数据侧的缺口不该把一个 5 年的币判成 3 个月。
- **流动性**用 taker 成交额 `close * volume` 的 trailing 中位数。均值会被单日
  爆量拉高，把「其实没什么量」的币放进池子。
- **退市**用「最后一根 bar 距今天数」，而不是「文件里出现了 delisted 标记」——
  L0 里已下架的币就是文件不再更新，没有别的信号。

落点：`<data_root>/schemas/symbol_sets.json`（给 plan 引用）与
`<data_root>/base/universe/pool_<name>.json`（审计：逐币指标与原因）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from contracts.errors import E1004, E4002, FlowError
from runtime.store import resolve_data_root
from services import universe_service as us
from storage.layout import Layout
from upstream import store

#: 判定原因码。顺序即优先级：一个币只记第一个命中的原因，避免「既短又没量」
#: 出现在两个桶里、统计时被重复计数。
REASON_NOT_IN_L0 = "not_in_l0"
REASON_UNREADABLE = "unreadable"
REASON_DELISTED = "delisted"
REASON_SHORT_HISTORY = "short_history"
REASON_LOW_LIQUIDITY = "low_liquidity"
REASON_LOW_COVERAGE = "low_coverage"
REASON_ASSET_CLASS = "asset_class_excluded"
REASON_STABLE = "stablecoin"

_REASON_ORDER = (
    REASON_NOT_IN_L0,
    REASON_UNREADABLE,
    REASON_DELISTED,
    REASON_SHORT_HISTORY,
    REASON_LOW_LIQUIDITY,
    REASON_LOW_COVERAGE,
    REASON_ASSET_CLASS,
    REASON_STABLE,
)


@dataclass(frozen=True)
class PoolCriteria:
    """币池判据。默认值对应「能做日频横截面的普通加密永续」。"""

    min_days: int = 365
    min_adv: float = 1_000_000.0
    adv_window: int = 90
    max_last_bar_age_days: int = 7
    min_coverage: float = 0.90
    allow_asset_classes: tuple[str, ...] = ("crypto",)
    exclude_stable: bool = True

    def __post_init__(self) -> None:
        if self.min_days < 0:
            raise FlowError(E1004, "min_days 不能为负", path="min_days")
        if self.min_adv < 0:
            raise FlowError(E1004, "min_adv 不能为负", path="min_adv")
        if self.adv_window < 1:
            raise FlowError(E1004, "adv_window 至少 1", path="adv_window")
        if not 0.0 <= self.min_coverage <= 1.0:
            raise FlowError(E1004, "min_coverage 必须在 [0,1]", path="min_coverage")
        bad = [c for c in self.allow_asset_classes if c not in us.ASSET_CLASSES]
        if bad:
            raise FlowError(
                E1004, f"未知资产类别 {bad}", path="allow_asset_classes",
                hint=f"允许值: {', '.join(us.ASSET_CLASSES)}",
            )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["allow_asset_classes"] = list(self.allow_asset_classes)
        return data


def _day(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).date().isoformat()
    return str(value)[:10]


def analyze_symbol(frame: pd.DataFrame, *, adv_window: int) -> dict[str, Any]:
    """单标的的日 K 指标。输入须含 `timestamp/close/volume`。"""
    if frame.empty:
        return {"bars": 0, "first_bar": None, "last_bar": None, "days": 0,
                "adv": 0.0, "coverage": 0.0, "zero_volume_ratio": 1.0}

    ordered = frame.sort_values(store.TIMESTAMP_COL, kind="mergesort")
    stamps = pd.to_datetime(ordered[store.TIMESTAMP_COL], utc=True, errors="coerce")
    close = pd.to_numeric(ordered["close"], errors="coerce")
    volume = pd.to_numeric(ordered["volume"], errors="coerce")
    valid = stamps.notna() & close.notna() & volume.notna()
    bars = int(valid.sum())
    if bars == 0:
        return {"bars": 0, "first_bar": None, "last_bar": None, "days": 0,
                "adv": 0.0, "coverage": 0.0, "zero_volume_ratio": 1.0}

    stamps = stamps[valid]
    close = close[valid]
    volume = volume[valid]
    dollar = (close * volume).to_numpy(dtype="float64")
    window = dollar[-int(adv_window):] if adv_window < dollar.size else dollar
    adv = float(np.nanmedian(window)) if window.size else 0.0

    first = stamps.min()
    last = stamps.max()
    span_days = int((last - first).days) + 1
    return {
        "bars": bars,
        "first_bar": first.date().isoformat(),
        "last_bar": last.date().isoformat(),
        "days": span_days,
        "adv": adv if np.isfinite(adv) else 0.0,
        "coverage": float(bars) / float(span_days) if span_days > 0 else 0.0,
        "zero_volume_ratio": float((volume <= 0).mean()),
    }


def read_metrics(
    root: str | Path,
    symbol: str,
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
    timeframe: str = "1d",
    adv_window: int = 90,
) -> dict[str, Any]:
    """从 L0 读一个标的的指标。文件缺失返回 `not_in_l0` 而不是抛错。"""
    try:
        path = store.dataset_file(
            root, store.BAR_DATASET, exchange, market, symbol, timeframe,
        )
    except FlowError as exc:
        return {"reason": REASON_UNREADABLE, "detail": str(exc)}
    if not path.exists():
        return {"reason": REASON_NOT_IN_L0, "path": str(path)}
    try:
        frame = pd.read_parquet(
            path, columns=[store.TIMESTAMP_COL, "close", "volume"],
        )
    except Exception as exc:  # 文件损坏 / 列缺失，都不能伪装成「没数据」
        return {"reason": REASON_UNREADABLE, "detail": f"{type(exc).__name__}: {exc}"}
    return analyze_symbol(frame, adv_window=adv_window)


def build_pool(
    records: Mapping[str, Mapping[str, Any]],
    metrics: Mapping[str, Mapping[str, Any]],
    criteria: PoolCriteria,
    *,
    now: datetime | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """按判据把每个标的归入 eligible / rejected，并给唯一原因码。"""
    stamp_dt = now or datetime.now(timezone.utc)
    stamp = generated_at or stamp_dt.isoformat()
    today = stamp_dt.date()

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for symbol in sorted(records):
        rec = records[symbol]
        asset_class = str(rec.get("asset_class") or "crypto")
        reasons: list[str] = []

        m = metrics.get(symbol) or {}
        if m.get("reason") in (REASON_NOT_IN_L0, REASON_UNREADABLE):
            reasons.append(str(m["reason"]))
        else:
            last_bar = m.get("last_bar")
            age = us.age_days(last_bar, stamp)
            listed = rec.get("listed_at") or m.get("first_bar")
            listed_days = us.age_days(listed, stamp)

            if rec.get("status") == us.STATUS_DELISTED:
                reasons.append(REASON_DELISTED)
            elif age is None or age > criteria.max_last_bar_age_days:
                reasons.append(REASON_DELISTED)
            elif listed_days is None or listed_days < criteria.min_days:
                reasons.append(REASON_SHORT_HISTORY)
            elif float(m.get("adv") or 0.0) < criteria.min_adv:
                reasons.append(REASON_LOW_LIQUIDITY)
            elif float(m.get("coverage") or 0.0) < criteria.min_coverage:
                reasons.append(REASON_LOW_COVERAGE)

        # 结构性原因排在数据判据之后：一个稳定币成交量低，读者该看到「稳定币」，
        # 而不是被引去查成交额。
        if asset_class not in criteria.allow_asset_classes:
            reasons.append(REASON_ASSET_CLASS)
        if criteria.exclude_stable and asset_class == "stable":
            reasons.append(REASON_STABLE)

        hit = next((r for r in _REASON_ORDER if r in reasons), None)
        row = {
            "symbol": symbol,
            "asset_class": asset_class,
            "bars": int(m.get("bars") or 0),
            "first_bar": m.get("first_bar"),
            "last_bar": m.get("last_bar"),
            "listed_at": rec.get("listed_at"),
            "listed_days": us.age_days(rec.get("listed_at") or m.get("first_bar"), stamp),
            "adv": round(float(m.get("adv") or 0.0), 2),
            "coverage": round(float(m.get("coverage") or 0.0), 6),
            "zero_volume_ratio": round(float(m.get("zero_volume_ratio") or 0.0), 6),
            "status": rec.get("status"),
        }
        if hit is None:
            row["rank"] = 0  # 稍后按 adv 重排
            eligible.append(row)
        else:
            row["reason"] = hit
            rejected.append(row)

    eligible.sort(key=lambda r: (-r["adv"], r["symbol"]))
    for i, row in enumerate(eligible, 1):
        row["rank"] = i
    rejected.sort(key=lambda r: (r["reason"], r["symbol"]))

    return {
        "criteria": criteria.to_json(),
        "generated_at": stamp,
        "eligible": eligible,
        "rejected": rejected,
        "summary": {
            "eligible": len(eligible),
            "rejected": len(rejected),
            "by_reason": _count_by_reason(rejected),
            "by_asset_class": _count_classes(eligible),
            "as_of": today.isoformat(),
        },
    }


def _count_by_reason(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(row.get("reason"))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _count_classes(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(row.get("asset_class"))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def write_pool(root: str | Path | None, pool: Mapping[str, Any], name: str) -> dict[str, Path]:
    """写审计文件与 symbol_sets.json 的一个集合。集合为空则不写名单。

    空名单写进 symbol_sets.json 会让 plan 静默跑到 0 个标的上——那比报错更糟。
    """
    base = Path(root) if root is not None else resolve_data_root()
    audit_dir = us.base_root(base).joinpath("universe")
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit = audit_dir / f"pool_{name}.json"
    audit.write_text(json.dumps(pool, ensure_ascii=False, indent=1), encoding="utf-8")

    members = [r["symbol"] for r in pool["eligible"]]
    if not members:
        raise FlowError(
            E4002, f"币池 {name} 为空，拒绝写入 symbol_sets.json", path="base.pool",
            hint=f"阈值可能过严，审计见 {audit}",
        )

    sets_path = Layout(base).schemas / "symbol_sets.json"
    table: dict[str, Any] = {}
    if sets_path.exists():
        table = json.loads(sets_path.read_text(encoding="utf-8"))
    table[name] = members
    sets_path.parent.mkdir(parents=True, exist_ok=True)
    sets_path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"audit": audit, "symbol_sets": sets_path}


__all__ = [
    "PoolCriteria", "REASON_NOT_IN_L0", "REASON_UNREADABLE", "REASON_DELISTED",
    "REASON_SHORT_HISTORY", "REASON_LOW_LIQUIDITY", "REASON_LOW_COVERAGE",
    "REASON_ASSET_CLASS", "REASON_STABLE",
    "analyze_symbol", "read_metrics", "build_pool", "write_pool",
]
