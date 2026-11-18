"""状态机（s0/05-orchestration.md §4）。

六状态、显式转移表。非法转移 → E3005。
两条判定规则：无候选通过=skipped（结果）；折数 0/依赖缺失/重建失败=failed。
reason 是枚举，不允许自由文本。
"""

from __future__ import annotations

from contracts.errors import E3005, FlowError

ALLOWED = {
    "pending": {"running", "skipped"},
    "running": {"done", "failed", "skipped"},
    "done": {"stale"},
    "failed": {"pending"},
    "skipped": set(),
    "stale": {"pending"},
}

REASONS = frozenset({
    "no_candidate_passed", "no_candidates_generated", "upstream_skipped",
    "precondition_unmet", "insufficient_cross_section",
})


class StateMachine:
    def __init__(self) -> None:
        self._state = "pending"
        self._reason: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str | None:
        return self._reason

    def transition(self, to: str, *, reason: str | None = None) -> None:
        if to not in ALLOWED[self._state]:
            raise FlowError(
                E3005,
                f"非法状态转移: {self._state} → {to}",
                hint=f"{self._state} 只允许 → {sorted(ALLOWED[self._state])}",
            )
        if to == "skipped" and (reason is None or reason not in REASONS):
            raise FlowError(E3005, f"skipped 必须带枚举 reason，实得 {reason!r}")
        self._state = to
        self._reason = reason if to == "skipped" else None
