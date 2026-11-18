"""跨层设施 · notify（s0/02-crosscutting.md §8）。

执行结束后的外部通知。唯一「允许失败但不影响结论」的设施——因为它不参与
任何判定。这个例外必须显式写在这里，防止被当成普遍先例滥用。

只允许锚点指纹、状态、错误码、耗时；禁止产物内容与凭据。
station / probe 作用域禁止通知（F10：E2001，不是静默忽略）。
失败写进 trace 附加字段，不改变退出码、不改变结论。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from v5.contracts.errors import E2001, E2004, FlowError

_ALLOWED_PAYLOAD_KEYS = frozenset({
    "plan_digest", "blueprint_digest", "policy_digest", "domain_digest",
    "status", "error_code", "seconds",
})
_SECRET_KEYS = frozenset({"token", "secret", "key", "password", "api_key", "credential"})


@dataclass
class NotifyResult:
    delivered: bool
    error: str | None = None
    # F7：被去重/限流抑制时为 True。`delivered=False, deduped=True` 不是
    # 失败——同一告警在窗口内已经发过，静默重复只会训练人忽略告警。
    deduped: bool = False


def build_notifier(scope: str, *, sink=None) -> "Notifier":
    """告警设施的**唯一**构造入口（F1）。

    F7 的去重窗口 3600s / 每窗 10 条是 S6 冻结的告警阈值，写在 dataclass
    默认值里会与 `Notifier(scope=...)` 的「无去重」默认混在一起——同一个类
    两种口径，调用方只能靠猜。这里固定为告警口径，facilities 组装与 pipeline
    都走这一处。
    """
    return Notifier(scope=scope, sink=sink, dedup_window_seconds=3600.0,
                    max_per_window=10)


@dataclass
class Notifier:
    scope: str
    sink: Callable[[dict[str, Any]], None] | None = None
    trace_notes: list[str] = field(default_factory=list)
    timeout_seconds: float = 2.0
    # F7 去重：同一 `dedup_key`（缺省=event）在本窗口内只投递一次。
    # 0 = 关闭（逐条都发）。
    dedup_window_seconds: float = 0.0
    # F7 阈值：滑动窗口内最多投递 N 条，超出的抑制（洪泛保护）。
    # 0 = 不限。阈值只能改判据调整（I52），不许为「减少噪音」静默放宽。
    max_per_window: int = 0
    rate_window_seconds: float = 60.0
    # 时钟注入：F1 禁止设施层自取时间，测试注入冻结时钟。
    clock: Callable[[], float] = time.monotonic
    _last_emit: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _rate: list[float] = field(default_factory=list, init=False, repr=False)

    def emit(self, event: str, payload: dict[str, Any],
             *, dedup_key: str | None = None) -> NotifyResult:
        if self.scope in {"station", "probe"}:
            raise FlowError(E2001, f"notify forbidden in scope={self.scope}")
        bad = (_SECRET_KEYS & payload.keys()) | (set(payload) - _ALLOWED_PAYLOAD_KEYS)
        if bad:
            raise FlowError(E2004, f"forbidden notify payload keys: {sorted(bad)}")
        # 白名单键的值必须是标量：digest/status/error_code/seconds 本就不该是
        # 嵌套结构。只查顶层键会让 {"status":{"api_key":"sk-X"}} 穿透（P1-8），
        # 而且递归扫凭据也挡不住换个字段藏内容——限定标量一次堵死两类。
        for key, value in payload.items():
            if isinstance(value, Mapping) or isinstance(value, (list, tuple, set, frozenset)):
                raise FlowError(
                    E2004, f"notify payload.{key} 必须是标量，得到 {type(value).__name__}",
                    hint="通知只允许摘要/状态/错误码/耗时，不得夹带结构或内容",
                )
        # ---- F7：去重与阈值（在投递之前，抑制不算失败） -----------------
        now = float(self.clock())
        key = str(dedup_key or event)
        if self.dedup_window_seconds > 0:
            last = self._last_emit.get(key)
            if last is not None and (now - last) < self.dedup_window_seconds:
                return NotifyResult(delivered=False, deduped=True)
        if self.max_per_window > 0:
            self._rate = [t for t in self._rate
                          if (now - t) < self.rate_window_seconds]
            if len(self._rate) >= self.max_per_window:
                note = (f"notify throttled ({event}): {self.max_per_window}"
                        f"/{self.rate_window_seconds:g}s window reached")
                self.trace_notes.append(note)
                return NotifyResult(delivered=False, deduped=True)
        if self.sink is None:
            self._last_emit[key] = now
            self._rate.append(now)
            return NotifyResult(delivered=True)  # S0：无外部通道视为静默成功
        try:
            self.sink({"event": event, **payload})
        except Exception as exc:  # 通知失败不影响结论——这是唯一允许的例外
            note = f"notify failed ({event}): {type(exc).__name__}: {exc}"
            self.trace_notes.append(note)
            return NotifyResult(delivered=False, error=note)
        self._last_emit[key] = now
        self._rate.append(now)
        return NotifyResult(delivered=True)
