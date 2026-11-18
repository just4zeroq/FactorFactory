"""跨层设施 · 容器与注入入口（s0/02-crosscutting.md §1）。

七个设施全部依赖注入，构造一次，逐层向下传。
禁止模块级获取（get_logger / datetime.now / random.random）——绕过注入即 E2007。

对外只暴露 Facilities 与 build_facilities；具体设施从各自子模块导入。
全局便捷访问器 get_log/install_log/reset_log 只解析组合根注入的实例，
不构造、不配置——构造仍在应用启动处，故不违反 F1（见 s0/02 §2.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from v5.contracts.errors import E2003, FlowError

from .cache import Cache
from .clock import Clock, frozen_clock
from .digest_svc import DigestService
from .log import (
    JsonlLogSink, Logger, StructlogLogSink,
    build_logger, get_log, install_log, reset_log,
)
from .notify import Notifier, build_notifier
from .rng import RngFactory
from .trace import TraceLog

__all__ = [
    "Facilities", "build_facilities", "Clock", "frozen_clock", "RngFactory",
    "DigestService", "Logger", "JsonlLogSink", "StructlogLogSink", "TraceLog",
    "Cache", "Notifier", "get_log", "install_log", "reset_log",
]

# S0 strict 模式必需的七个设施
REQUIRED_FACILITIES = frozenset({
    "log", "trace", "clock", "rng", "digest", "cache", "notify",
})

# strict 校验用：只查 is None 会让 object()/str 蒙混过关（F2）。
_FACILITY_TYPES: dict[str, tuple[type, ...]] = {
    "log": (Logger,), "trace": (TraceLog,), "clock": (Clock,),
    "rng": (RngFactory,), "digest": (DigestService,), "cache": (Cache,),
    "notify": (Notifier,),
}


@dataclass
class Facilities:
    log: Logger
    trace: TraceLog
    clock: Clock
    rng: RngFactory
    digest: DigestService
    cache: Cache
    notify: Notifier


def build_facilities(
    *,
    scope: str,
    ticket_id: str,
    master_seed: int,
    log_keys: dict[str, Any] | None = None,
    root: Path | None = None,
    frozen_at: datetime | None = None,
    notify_sink: Callable[[dict[str, Any]], None] | None = None,
    strict: bool = True,
    overrides: dict[str, Any] | None = None,
    log_sink: Any | None = None,
) -> Facilities:
    """构造七个设施。strict 模式缺任一设施即失败（F2，退出码 2）。

    S0 中七个设施都有默认实现；`overrides` 可注入替身用于拒绝测试
    （例如注入 None 的 digest 验证 strict 失败）。
    """
    if scope not in {"upstream", "ticket", "station", "probe"}:
        raise FlowError(E2003, f"unknown facilities scope: {scope}")

    parts: dict[str, Any] = {
        "clock": frozen_clock(frozen_at) if frozen_at is not None else Clock(),
        "rng": RngFactory(master_seed=master_seed),
        "digest": DigestService(),
        "trace": TraceLog(),
        "cache": Cache(scope=scope, root=root or Path(".")),
        "notify": build_notifier(scope, sink=notify_sink),
    }
    keys = {"ticket_id": ticket_id, "seq": 0}
    # log_keys 只提供 station/call_digest 等附加键；ticket_id/seq 是组合根
    # 已经给的真值，必须优先，不能被配置覆盖造成日志归属串线。
    for k, v in (log_keys or {}).items():
        keys.setdefault(k, v)
    # station/probe scope 的 station 与 call_digest 必须由调用方给真值。
    # 曾经的 setdefault("0"*64) 会给日志造一个假 call_digest——日志与被调用的
    # 实现再也对不上，属于"可归属"的反面；缺就让它 E2003。
    clock = parts["clock"]
    parts["log"] = build_logger(
        scope=scope, keys=keys, sink=log_sink, clock=clock.wall,
    )

    if overrides:
        for name, value in overrides.items():
            if name in REQUIRED_FACILITIES:
                parts[name] = value

    if strict:
        invalid = [
            name for name in REQUIRED_FACILITIES
            if parts.get(name) is None or not isinstance(parts[name], _FACILITY_TYPES[name])
        ]
        if invalid:
            raise FlowError(
                E2003,
                f"strict mode: 设施缺失或类型不符: {sorted(invalid)}",
                hint="缺 digest/rng 或拿 object() 冒充时不得先跑起来再说",
            )

    return Facilities(**parts)
