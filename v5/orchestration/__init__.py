"""V5 编排层 · 八工位规格、唯一执行器、状态机、事件（s0/05）。

编排层是唯一有权「开执行 / 计数 / 揭示 / 发布」的层。
能力层无权：不读配置、不写账本、不产结论。
整体跑与单步跑共用同一个 execute()——不允许第二份逻辑。
"""

from .station import STATION_SPECS, StationSpec
from .state import StateMachine
from .events import EventLedger
from .executor import Executor, ExecuteResult

__all__ = [
    "STATION_SPECS", "StationSpec", "StateMachine",
    "EventLedger", "Executor", "ExecuteResult",
]
