"""跨层设施 · trace（s0/02-crosscutting.md §3）。

每工位一条执行记录，只追加。失败也必须有一条——没有 trace 的失败
等于系统没留下任何「发生过这件事」的证据。

trace 不写结论、不写指标值：它是执行事实记录，不是报告。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TraceRecord:
    seq: int
    station: str
    station_revision: str
    impl: str
    impl_version: str
    params_digest: str
    inputs: list[tuple[str, str]]
    call_digest: str
    parcel_digest: str | None
    status: str  # done / failed / skipped
    origin: str  # run / step / resume / cache
    seconds: float
    error_code: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"done", "failed", "skipped"}:
            raise ValueError(f"bad trace status: {self.status}")
        if self.origin not in {"run", "step", "commit", "resume", "cache"}:
            raise ValueError(f"bad trace origin: {self.origin}")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed trace must carry error_code")
        if self.status == "skipped" and self.skip_reason is None:
            raise ValueError("skipped trace must carry skip_reason")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TraceLog:
    """只追加的 trace 序列。"""

    def __init__(self) -> None:
        self._records: list[TraceRecord] = []

    def append(self, record: TraceRecord) -> None:
        expected_seq = len(self._records) + 1
        if record.seq != expected_seq:
            raise ValueError(f"trace seq gap: got {record.seq}, expected {expected_seq}")
        self._records.append(record)

    def records(self) -> list[TraceRecord]:
        return list(self._records)
