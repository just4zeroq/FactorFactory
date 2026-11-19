"""flow scorecard —— 按 run 前冻结的评价档位生成评分卡（只读）。

`verdict` 仍是唯一裁判、`certify` 仍是唯一 OOS 结论来源；本命令不重算任何
统计量，只把两次产物按预注册档位摊开成一张卡，让「为什么不能晋升」有具体
位置。档位来自冻结 ticket 的 `plan.evaluation`，run 后改阈值不生效。

默认只给每行的失败项；`--full` 带完整 checks（含 rule/threshold/value），
避免一次把整份候选集倒进终端。`--candidate` 收窄到单个候选。
"""

from __future__ import annotations

from typing import Any

from contracts.errors import E1008, FlowError
from runtime.store import resolve_data_root
from services.evaluation import build_scorecard
from storage.catalog import read_ticket
from storage.layout import Layout

from .common import envelope_ok


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    """摘要：保留结论字段，checks 只留未通过的项。"""
    out = {k: row.get(k) for k in (
        "candidate_id", "decision", "eligible_for_promotion",
        "rank_ic", "hlz_hac_t", "icir", "monotonicity", "wfe",
        "oos_decay", "is_sharpe_per_period", "oos_sharpe_per_period",
    )}
    out["failed_checks"] = [
        {"layer": c.get("layer"), "name": c.get("name"),
         "value": c.get("value"), "threshold": c.get("threshold")}
        for c in row.get("checks") or [] if c.get("pass") is not True
    ]
    return out


def cmd_scorecard(args: Any) -> dict[str, Any]:
    layout = Layout(resolve_data_root())
    info = read_ticket(layout, args.run_id)
    card = build_scorecard(info)

    wanted = getattr(args, "candidate", None)
    rows = card["candidates"]
    if wanted:
        rows = [r for r in rows if r.get("candidate_id") == wanted]
        if not rows:
            raise FlowError(
                E1008, f"本次执行没有候选 {wanted!r} 的评分卡",
                path="scorecard.candidate",
                hint="用 flow inspect --run-id ... 查看本次的 candidate_id",
            )

    full = bool(getattr(args, "full", False))
    return envelope_ok("scorecard", args.run_id, {
        "run_id": args.run_id,
        "schema": card["schema"],
        "status": card["status"],
        "profile": card["profile"],
        "eligible_candidates": card["eligible_candidates"],
        "layers": card["layers"],
        "regime_subsamples": card["regime_subsamples"],
        "no_shopping_contract": card["no_shopping_contract"],
        "candidates": rows if full else [_brief(r) for r in rows],
        "plan": (info["frozen"].get("plan") or {}).get("plan"),
        "root": info["root"],
    }, warnings=[str(w) for w in card.get("warnings") or []])
