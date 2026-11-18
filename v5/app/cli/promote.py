"""flow promote —— 把通过裁决的候选写进因子库（writes_facts + writes_ledger）。

三条规则：

1. **只认裁决结果**：能不能入库由 Verdict 的 `decision` 决定（`pass` / `watch`），
   不由调用方「我觉得这个不错」决定。`--candidate` 只能**缩小**范围，不能把
   `reject` 的候选拉进库——那等于绕过唯一裁判。
2. **表达式必须来自 Search 产物**：按 `candidate_id` join。库里没有表达式就
   只剩一个哈希，既不可复算也不可当先验池；找不到 → E3003，不凭 id 入库。
3. **重复执行不是错误**：同一 `factor_id` 已在库就跳过并如实报告，不写第二行。

证据等级（`evidence_grade`）取自 Certify 产物：`out_of_sample` 才代表「样本外
认证过」。run 因零晋升而跳过时没有 Certify，落库条目就是 `in_sample`——这个
区别必须留在库里，否则「回测过」会被读成「认证过」。
"""

from __future__ import annotations

from typing import Any

from v5.app.contract import require_confirmation
from v5.contracts.errors import E1008, E3003, FlowError
from v5.facilities.clock import Clock
from v5.storage.catalog import Catalog, read_ticket
from v5.storage.layout import Layout
from v5.runtime.store import resolve_data_root

from .common import envelope_ok

_PROMOTABLE = frozenset({"pass", "watch"})
_GRADES = ("any", "out_of_sample", "in_sample")


def _grade_of(payloads: dict[str, Any]) -> str:
    """本次执行的证据等级。没有 Certify 就是样本内。"""
    cert = payloads.get("Certify")
    if isinstance(cert, dict):
        grade = cert.get("evidence_grade")
        if isinstance(grade, str) and grade:
            return grade
    return "in_sample"


def cmd_promote(args: Any) -> dict[str, Any]:
    require_confirmation("promote", args)
    layout = Layout(resolve_data_root())
    info = read_ticket(layout, args.run_id)
    payloads = info["payloads"]
    grade = _grade_of(payloads)

    min_grade = str(getattr(args, "min_grade", "any") or "any")
    if min_grade not in _GRADES:
        raise FlowError(
            E1008, f"未知 --min-grade {min_grade!r}", path="promote.min_grade",
            hint=f"可用: {list(_GRADES)}",
        )
    if min_grade == "out_of_sample" and grade != "out_of_sample":
        return envelope_ok("promote", args.run_id, {
            "promoted": [], "skipped": [],
            "counts": {"candidates": 0, "promoted": 0, "already": 0, "skipped": 0},
            "evidence_grade": grade, "reason": "not_out_of_sample",
        })

    verdict = payloads.get("Verdict") or {}
    evidence = payloads.get("Evidence") or {}
    search = payloads.get("Search") or {}
    if not isinstance(verdict, dict) or not verdict.get("per_candidate"):
        raise FlowError(
            E3003, "本次执行没有裁决结果，无法晋升",
            path="promote.run_id",
            hint="先跑完整执行；零候选被拒时跳过语义会让 Verdict 为空",
        )

    metrics_by_id = {
        str(c.get("candidate_id")): c
        for c in (evidence.get("per_candidate") or [])
        if isinstance(c, dict) and c.get("candidate_id")
    }
    ast_by_id = {
        str(c.get("candidate_id")): c
        for c in (search.get("candidates") or [])
        if isinstance(c, dict) and c.get("candidate_id")
    }

    wanted: set[str] | None = None
    raw_wanted = getattr(args, "candidate", None) or []
    if raw_wanted:
        wanted = {str(x) for x in raw_wanted}
        missing = sorted(wanted - set(ast_by_id))
        if missing:
            raise FlowError(
                E1008, f"--candidate 不在本次执行的候选集里: {missing[:3]}",
                path="promote.candidate",
            )

    catalog = Catalog(layout)
    now = Clock().wall().isoformat().replace("+00:00", "Z")
    plan_name = ""
    frozen_plan = info["frozen"].get("plan")
    if isinstance(frozen_plan, dict):
        plan_name = str(frozen_plan.get("plan") or "")

    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    already = 0
    seen: set[str] = set()

    for row in verdict["per_candidate"]:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("candidate_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        decision = str(row.get("decision") or "")
        if wanted is not None and cid not in wanted:
            skipped.append({"factor_id": cid, "reason": "not_selected"})
            continue
        if decision not in _PROMOTABLE:
            skipped.append({"factor_id": cid, "reason": f"decision:{decision}"})
            continue
        source = ast_by_id.get(cid)
        if source is None or not isinstance(source.get("payload"), dict) \
                or source["payload"].get("expression") is None:
            # 裁决说 pass、搜索产物里却没有表达式：不是「跳过」，是数据不完整
            raise FlowError(
                E3003, f"候选 {cid[:19]} 在 Search 产物里没有表达式",
                path="promote.candidate",
                hint="Search payload 被裁剪或缺 candidates 数组时无法入库",
            )
        mrow = metrics_by_id.get(cid) or {}
        entry = {
            "factor_id": cid,
            "name": str((mrow.get("generator") or source.get("generator") or "factor"))
                        .split("@")[0] + ":" + cid.split(":")[-1][:8],
            "expression": source["payload"]["expression"],
            "required_fields": source.get("required_fields") or [],
            "operators": source.get("operators") or [],
            "generators": source.get("generators") or [source.get("generator", "")],
            "origin": {"ticket_id": args.run_id, "plan": plan_name,
                       "family": str((row.get("statistics") or {}).get("family", ""))},
            "evidence_grade": grade,
            "statistics": dict(row.get("statistics") or {}),
            "metrics": dict(mrow.get("metrics") or {}),
            "status": "active",
            "promoted_at": now,
        }
        stored, fresh = catalog.add(entry)
        if fresh:
            promoted.append({"factor_id": cid, "name": stored["name"],
                             "decision": decision})
        else:
            already += 1
            skipped.append({"factor_id": cid, "reason": "already_in_catalog"})

    return envelope_ok("promote", args.run_id, {
        "promoted": promoted,
        "skipped": skipped,
        "counts": {"candidates": len(seen), "promoted": len(promoted),
                   "already": already, "skipped": len(skipped) - already},
        "evidence_grade": grade,
        "catalog_total": catalog.count(),
    })
