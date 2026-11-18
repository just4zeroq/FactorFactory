"""flow snapshot —— 数据快照管理（docs/21 §1）。

子命令：create / list / show / archive。create 只写快照清单，不改 parquet。
"""

from __future__ import annotations

from typing import Any

from contracts.errors import E1004, FlowError
from runtime.store import resolve_data_root
from services.snapshot_service import SnapshotService

from .common import envelope_ok


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


def cmd_snapshot(args: Any) -> dict[str, Any]:
    sub = getattr(args, "snapshot_action", None)
    if not sub:
        raise FlowError(E1004, "snapshot 需要子命令", path="snapshot.action",
                        hint="create / list / show / archive")
    with SnapshotService(resolve_data_root()) as svc:
        if sub == "create":
            manifest = svc.create(
                datasets=_split_csv(args.datasets) or [],
                exchange=args.exchange,
                market=args.market,
                symbols=_split_csv(args.symbols),
                timeframe=args.timeframe,
                parent=args.parent,
                note=args.note or "",
            )
            # P5（§5）：新快照创建后自动 enqueue 全部 active 因子。用惰性 import
            # 避免 snapshot_service ↔ replay_service 的模块级环；--no-enqueue
            # 给只想建清单、暂不排队的场景一个显式出口。没有 active 因子时是空操作。
            enqueued: list[str] = []
            if not getattr(args, "no_enqueue", False):
                from services.replay_service import ReplayService

                with ReplayService(resolve_data_root()) as replay:
                    enqueued = [str(r["replay_id"]) for r in replay.enqueue(
                        snapshot_id=manifest["snapshot_id"], all_active=True)]
            return envelope_ok("snapshot", manifest["snapshot_id"], {
                "snapshot": manifest["snapshot_id"],
                "manifest": manifest,
                "entries": len(manifest["entries"]),
                "auto_enqueued": enqueued,
            })
        if sub == "list":
            rows = svc.list(status=args.status, limit=args.limit)
            return envelope_ok("snapshot", None, {
                "snapshots": rows, "count": len(rows),
            })
        if sub == "show":
            return envelope_ok("snapshot", args.snapshot_id, {
                "snapshot": args.snapshot_id,
                "manifest": svc.manifest(args.snapshot_id),
            })
        if sub == "archive":
            if not args.yes:
                raise FlowError(E1004, "archive 会改快照状态，必须 --yes",
                                path="snapshot.yes")
            manifest = svc.archive(args.snapshot_id)
            return envelope_ok("snapshot", args.snapshot_id, {
                "snapshot": args.snapshot_id, "manifest": manifest,
            })
    raise FlowError(E1004, f"未知 snapshot 子命令 {sub!r}", path="snapshot.action")
