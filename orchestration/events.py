"""三类一次性事件（s0/05-orchestration.md §5）。

Reveal / Budget / Publish 绑定一次执行。已存在 → 返回旧值，不重复触发。
这是「计数单调」与「揭示一次」的结构保证。

- Reveal 在 Boundary 之前；单步缺记录 → E3006，不自行揭示
- Budget 在 CandidateParcel 落盘后；值=实测去重候选数
- Publish 在 Certify 通过后

**两种后端**：

- 不传 `ledger` 时纯内存（单测、`--probe` 用），语义与旧实现完全一致；
- 传 `ledger` 时每条事件追加到 `db/events.jsonl`，这是 AC8「跨进程不重复」的
  唯一落地方式。跨进程的第二次触发只有两种正确结局，都不能是「静默再写一份」：
  进程内命中内存 → 返回旧值；另一进程已写过 → 读回那条旧记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.errors import E3006, FlowError


@dataclass(frozen=True)
class EventRecord:
    kind: str
    key: str
    value: int | str


class EventLedger:
    def __init__(self, ledger: Any | None = None) -> None:
        """`ledger` 是 `storage.ledger.Ledger`（可选）。

        只依赖它的 `append(register, record)` 与 `get(register, event_id)`，
        不 import 存储层：编排层可以依赖存储层（只有能力层被禁止）。
        """
        self._records: dict[tuple[str, str], EventRecord] = {}
        self._ledger = ledger

    def reveal(self, ticket_id: str, window_id: str) -> EventRecord:
        return self._once("Reveal", f"{ticket_id}:{window_id}", window_id)

    def budget(self, ticket_id: str, family: str, count: int) -> EventRecord:
        return self._once("Budget", f"{ticket_id}:{family}", count)

    def publish(self, ticket_id: str, digest: str) -> EventRecord:
        return self._once("Publish", f"{ticket_id}:publish", digest)

    def _once(self, kind: str, key: str, value: int | str) -> EventRecord:
        k = (kind, key)
        if k in self._records:
            return self._records[k]  # 已存在 → 返回旧值，不重新触发
        rec = EventRecord(kind=kind, key=key, value=value)
        if self._ledger is not None:
            event_id = f"{kind}:{key}"
            try:
                self._ledger.append("events", {
                    "event_id": event_id, "kind": kind, "key": key,
                    "ticket_id": key.split(":", 1)[0], "value": value,
                })
            except FlowError as exc:
                if exc.error.code != E3006:
                    raise
                # 跨进程重复：另一实例已经写过。返回那条旧记录，不改写值——
                # 后到的执行不得覆盖先到的计数 / 揭示 / 发布。
                prior = self._ledger.get("events", event_id)
                if prior is None:
                    raise
                rec = EventRecord(kind=kind, key=key, value=prior.get("value", value))
        self._records[k] = rec
        return rec

    def get(self, kind: str, key: str) -> EventRecord | None:
        rec = self._records.get((kind, key))
        if rec is not None:
            return rec
        if self._ledger is None:
            return None
        # 本进程没触发过，但磁盘上有（例如 run 之后另起进程跑单步 station）。
        prior = self._ledger.get("events", f"{kind}:{key}")
        if prior is None:
            return None
        rec = EventRecord(kind=kind, key=key, value=prior.get("value", ""))
        self._records[(kind, key)] = rec
        return rec

    def require_reveal(self, ticket_id: str, window_id: str) -> EventRecord:
        """单步模式用：缺 Reveal 记录 → E3006，不自行揭示。"""
        rec = self.get("Reveal", f"{ticket_id}:{window_id}")
        if rec is None:
            raise FlowError(
                E3006,
                f"单步缺 Reveal 记录: {ticket_id}:{window_id}",
                hint="单步不得自行揭示；先跑整体执行",
            )
        return rec
