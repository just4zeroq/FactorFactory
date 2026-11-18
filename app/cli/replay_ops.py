"""flow replay —— 因子在新快照上的复盘闭环（docs/21 §2）。

三个子命令：
- `enqueue`：把 active 因子 × 新快照入队（幂等）；
- `run`：执行一条复盘（复用现有能力链，写证书 + 物化 + 报告）；
- `show`：查看复盘状态与报告。

命名说明：`evaluate/backtest/certify` 三条冻结 ticket 重算命令仍在
`app/cli/replay.py`；本模块是 docs/21 设计的**因子级**复盘队列，两者不冲突
（命令名 `replay` 与 Python 模块名 `replay_ops.py` 分开）。
"""

from __future__ import annotations

from typing import Any

from contracts.errors import E1004, FlowError
from runtime.store import resolve_data_root
from services.replay_service import ReplayService

from .common import envelope_ok


def cmd_replay(args: Any) -> dict[str, Any]:
    sub = getattr(args, "replay_action", None)
    if not sub:
        raise FlowError(E1004, "replay 需要子命令", path="replay.action",
                        hint="enqueue / run / show")
    with ReplayService(resolve_data_root()) as svc:
        if sub == "enqueue":
            rows = svc.enqueue(
                snapshot_id=getattr(args, "snapshot", None),
                all_active=bool(getattr(args, "all_active", False)),
                factor_ids=[str(x) for x in (getattr(args, "factor", None) or [])] or None,
            )
            return envelope_ok("replay", None, {
                "enqueued": [str(r["replay_id"]) for r in rows],
                "count": len(rows),
            })
        if sub == "run":
            replay_id = str(args.replay_id)
            result = svc.run(replay_id)
            report = result.get("report") or {}
            decision = str(report.get("decision") or "")
            # §2.3：degraded 告警、retired 下线通知、error 人工介入。走 F7
            # 唯一工厂 + ticket 作用域（station/probe 禁用），payload 只带
            # 允许的摘要键，不夹带指标内容或凭据。失败不影响结论。
            alerts: list[dict[str, Any]] = []
            if decision in ("degraded", "retired", "error"):
                from facilities.notify import build_notifier

                notifier = build_notifier("ticket")
                severity = "warning" if decision == "degraded" else "critical"
                delivered = notifier.emit(
                    f"replay.{decision}",
                    {"status": decision, "error_code": None, "seconds": 0.0},
                    dedup_key=f"replay:{replay_id}",
                )
                alerts.append({
                    "kind": f"replay_{decision}", "severity": severity,
                    "notified": delivered.delivered, "deduped": delivered.deduped,
                })
            return envelope_ok("replay", replay_id, {
                "replay_id": replay_id,
                "status": str(result["status"]),
                "decision": decision,
                "reason_codes": report.get("reason_codes", []),
                "report": report,
                "alerts": alerts,
            })
        if sub == "show":
            replay_id = str(args.replay_id)
            result = svc.show(replay_id)
            return envelope_ok("replay", replay_id, {
                "replay_id": replay_id,
                "status": str(result["status"]),
                "report": result.get("report") or {},
            })
    raise FlowError(E1004, f"未知 replay 子命令 {sub!r}", path="replay.action")


__all__ = ["cmd_replay"]
