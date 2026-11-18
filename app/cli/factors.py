"""flow factors —— 查因子库（只读）。

输出按 `factor_id` 折叠后的当前条目。默认只列 `active`；`--status retired`
看下架记录，`--status all` 看全部。
"""

from __future__ import annotations

from typing import Any

from contracts.errors import E1008, FlowError
from runtime.store import resolve_data_root
from storage.catalog import Catalog
from storage.layout import Layout

from .common import envelope_ok


def _row(entry: dict[str, Any]) -> dict[str, Any]:
    """库条目的展示投影：表达式与统计原样带出，便于直接核对。"""
    stats = entry.get("statistics") or {}
    return {
        "factor_id": entry.get("factor_id"),
        "name": entry.get("name"),
        "status": entry.get("status", "active"),
        "expression": entry.get("expression"),
        "required_fields": list(entry.get("required_fields") or []),
        "operators": list(entry.get("operators") or []),
        "generators": list(entry.get("generators") or []),
        "evidence_grade": entry.get("evidence_grade"),
        "origin": dict(entry.get("origin") or {}),
        "promoted_at": entry.get("promoted_at"),
        "statistics": {
            k: stats.get(k) for k in ("dsr", "hac_t", "p_adj", "cost_adj_return",
                                      "n_trials", "family") if k in stats
        },
    }


def cmd_factors(args: Any) -> dict[str, Any]:
    layout = Layout(resolve_data_root())
    catalog = Catalog(layout)
    status = str(getattr(args, "status", "active") or "active")
    limit = getattr(args, "limit", None)
    if status == "all":
        rows = catalog.entries()
        if limit is not None and limit >= 0:
            rows = rows[:limit]
    else:
        if status not in ("active", "retired"):
            raise FlowError(
                E1008, f"未知 --status {status!r}", path="factors.status",
                hint="可用: active / retired / all",
            )
        rows = catalog.list(status=status, limit=limit)
    return envelope_ok("factors", None, {
        "factors": [_row(r) for r in rows],
        "count": len(rows),
        "catalog_total": catalog.count(),
        "path": str(catalog.path),
    })
