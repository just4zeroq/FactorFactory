"""跨层设施 · clock（s0/02-crosscutting.md §4）。

墙钟只用于记录；单调钟只用于计时。数据新鲜度判定不得读时钟，
必须用 Pin.acquired_at 与计划时间轴做纯函数比较。

S0 要求支持冻结时钟：测试与重放时给定固定时间，所有 wall() 相同。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class Clock:
    """注入式时钟。业务层只拿到这个实例，不得自行 import time/datetime。"""

    frozen_at: datetime | None = None

    def wall(self) -> datetime:
        """UTC 墙钟，仅用于记录 started_at / created_at 等字段。"""
        if self.frozen_at is not None:
            return self.frozen_at
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        """单调钟，仅用于计时（seconds / 超时）。冻结时钟不影响它。"""
        return time.monotonic()

    def today(self) -> date:
        """仅用于维护链的交易日判定；业务新鲜度判定不得用。"""
        return self.wall().date()


def frozen_clock(at: datetime) -> Clock:
    """构造一个冻结时钟——同输入重放时所有记录时间逐位一致。"""
    if at.tzinfo is None:
        raise ValueError("冻结时钟必须用带时区的 datetime")
    return Clock(frozen_at=at.astimezone(timezone.utc))
