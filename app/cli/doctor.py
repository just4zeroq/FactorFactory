"""flow doctor —— 供给体检（只读）。

扫数据根下所有 `<dataset>/<exchange>/<market>/`，逐个现算身份并回答四个问题：

1. **有没有数据**：文件数、标的数、总行数；
2. **覆盖到哪**：`coverage` 起止、`interval_ns`（实测中位步长）、`end_closed`；
3. **有没有洞**：`gaps` 逐段列出（缺口不是"少几行"，是"这段时间的结论不可信"）；
4. **能不能用**：`passed` = 末端闭合 且 无缺口。

三条与「成功」有关的判断都写在这里，不让调用方自己猜：

- 目录存在但读出来零行 → `empty`，不是 ok；
- 有缺口 → `reason_code=E4002`，不是 warning；
- 标的行数差异过大（少于中位数的 50%）→ 单独列出，因为分位/覆盖率的分母
  会被它们拖偏。

`--plan` 时额外做「计划要的标的 vs 实际有的标的」比对：缺一个就报出来，
因为 `read` 在给清单时会 fail-closed，等到 run 才发现会浪费一次完整执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contracts.errors import E1008, FlowError
from runtime.store import resolve_data_root
from upstream import store

from .common import envelope_ok, load_plan

# ohlcv 的 timeframe 只从已知集合里认：文件名里的 '-' 也可能是符号的一部分，
# 用 rpartition 盲切会把 `BTC-USD-1d` 这类名字切错。
_KNOWN_TF = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")


def _tf_of(path: Path) -> str | None:
    stem = path.stem
    head, sep, tail = stem.rpartition("-")
    if sep and head and tail in _KNOWN_TF:
        return tail
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _datasets_in(root: Path) -> list[str]:
    return [d for d in store.DATASETS if (root / d).is_dir()]


def _check_one(root: Path, dataset: str, exchange: str, market: str,
               timeframe: str | None) -> dict[str, Any]:
    ds = store.read_dataset(root, dataset, exchange, market, timeframe=timeframe)
    bounds = dict(ds.per_symbol_bounds)
    rows = {sym: int(b[2]) for sym, b in bounds.items() if isinstance(b, (tuple, list))}
    median = 0
    thin: list[dict[str, Any]] = []
    if rows:
        ordered = sorted(rows.values())
        median = ordered[len(ordered) // 2]
        for sym, n in sorted(rows.items()):
            if median and n * 2 < median:
                thin.append({"symbol": sym, "rows": n})
    gaps = [
        {"symbol": g.get("symbol"), "start": _iso(g.get("start")),
         "end": _iso(g.get("end")), "missing": g.get("missing")}
        for g in ds.gaps
    ]
    return {
        "dataset": dataset,
        "exchange": exchange,
        "market": market,
        "timeframe": timeframe,
        "symbols": len(ds.symbols),
        "rows": int(ds.rows),
        "version": ds.version,
        "coverage": {"start": _iso(ds.coverage["start"]), "end": _iso(ds.coverage["end"])},
        "interval_ns": int(ds.interval_ns),
        "end_closed": bool(ds.end_closed),
        "gaps": gaps[:20],
        "gap_count": len(gaps),
        "median_rows": median,
        "thin_symbols": thin[:10],
        "passed": bool(ds.passed),
        "reason_code": ds.reason_code,
    }


def _plan_symbols(root: Path, plan: dict[str, Any]) -> tuple[str, list[str]] | None:
    """计划声明的标的集名 → 成员清单。标的集表不在就返回 None（不阻塞体检）。"""
    supply = plan.get("supply") or {}
    name = str(supply.get("symbols") or "")
    if not name:
        return None
    from storage.layout import Layout
    path = Layout(root).symbol_sets
    if not path.exists():
        return None
    import json
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    members = (table.get("sets") or table).get(name)
    if isinstance(members, dict):
        members = members.get("symbols")
    if not isinstance(members, list):
        return None
    return name, [str(x) for x in members]


def cmd_doctor(args: Any) -> dict[str, Any]:
    root = Path(resolve_data_root())
    datasets = _datasets_in(root)
    if not datasets:
        raise FlowError(
            E1008, f"数据根下没有任何已采集数据集: {root}", path="doctor.data_root",
            hint="先 flow fetch --datasets ohlcv --exchange <id> --start .. --end ..",
        )

    checks: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    only = getattr(args, "dataset", None)
    if only:
        wanted = [x.strip() for x in str(only).split(",") if x.strip()]
        unknown = [d for d in wanted if d not in store.DATASETS]
        if unknown:
            raise FlowError(E1008, f"未知数据集 {unknown}", path="doctor.dataset",
                            hint=f"可用: {list(store.DATASETS)}")
        datasets = [d for d in datasets if d in wanted]

    for dataset in datasets:
        base = root / dataset
        for exchange_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for market_dir in sorted(p for p in exchange_dir.iterdir() if p.is_dir()):
                files = sorted(market_dir.glob("*.parquet"))
                if not files:
                    continue
                tfs = {_tf_of(f) for f in files}
                targets: list[str | None] = (
                    sorted(t for t in tfs if t) if dataset == store.BAR_DATASET else [None]
                )
                for tf in targets:
                    try:
                        check = _check_one(root, dataset, exchange_dir.name,
                                           market_dir.name, tf)
                    except FlowError as exc:
                        warnings.append({
                            "dataset": dataset, "exchange": exchange_dir.name,
                            "market": market_dir.name, "timeframe": tf,
                            "code": exc.error.code, "message": exc.error.message,
                        })
                        continue
                    checks.append(check)
                    if not check["passed"]:
                        warnings.append({
                            "dataset": dataset, "exchange": exchange_dir.name,
                            "market": market_dir.name, "timeframe": tf,
                            "code": check["reason_code"],
                            "message": ("末端未闭合" if not check["end_closed"]
                                        else f"{check['gap_count']} 段缺口"),
                        })
                    if check["thin_symbols"]:
                        warnings.append({
                            "dataset": dataset, "market": market_dir.name,
                            "timeframe": tf, "code": "E4002",
                            "message": f"{len(check['thin_symbols'])} 个标的行数不足中位数一半",
                            "symbols": [x["symbol"] for x in check["thin_symbols"]],
                        })

    plan_check = None
    plan_path = getattr(args, "plan", None)
    if plan_path:
        plan = load_plan(args)
        found = _plan_symbols(root, plan)
        supply = plan.get("supply") or {}
        want_ds = [str(x) for x in (supply.get("datasets") or [])]
        present = {c["dataset"] for c in checks}
        missing_ds = [d for d in want_ds if d not in present]
        missing_symbols: list[str] = []
        if found:
            set_name, members = found
            present_symbols: set[str] = set()
            for c in checks:
                if c["dataset"] == store.BAR_DATASET:
                    present_symbols |= set(store.scan_symbols(
                        root, store.BAR_DATASET, c["exchange"], c["market"],
                        c["timeframe"]))
            # `scan_symbols` 给的是**文件名 stem**（`BTC_USDT_USDT`），不是权威
            # 符号（`BTC/USDT:USDT`）。两个集合直接相减，会让「数据一个不缺」
            # 也报成全部缺失——把成员先映射到 stem 再比，报错时还原成权威符号
            # （人读的是后者）。
            want = {store.file_stem(str(m)): str(m) for m in members}
            missing_symbols = sorted(
                want[stem] for stem in set(want) - present_symbols
            )
        plan_check = {
            "plan": str(plan.get("plan") or ""),
            "declared_datasets": want_ds,
            "missing_datasets": missing_ds,
            "symbol_set": found[0] if found else None,
            "missing_symbols": missing_symbols[:20],
            "missing_symbol_count": len(missing_symbols),
        }
        if missing_ds or missing_symbols:
            warnings.append({
                "code": "E4002", "dataset": ",".join(missing_ds) or None,
                "message": (f"计划声明的数据集未采集: {missing_ds}" if missing_ds
                            else f"标的集缺 {len(missing_symbols)} 个成员"),
            })

    passed = bool(checks) and all(c["passed"] for c in checks) and not warnings
    return envelope_ok("doctor", None, {
        "data_root": str(root),
        "datasets": datasets,
        "checks": checks,
        "warnings": warnings,
        "summary": {
            "datasets": len(datasets),
            "groups": len(checks),
            "rows": sum(c["rows"] for c in checks),
            "passed": passed,
        },
        "plan": plan_check,
    }, warnings=warnings)
