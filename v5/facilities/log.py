"""跨层设施 · log（s0/02-crosscutting.md §2）。

本模块只做一件事：在成熟序列化链外面套一层**契约闸门**。

分工：

- **闸门归我们**（三方日志库都不提供，也是 F1「禁止模块级 get_logger」的落点）：
  scope 必填关联键（`E2003`）、凭据与结论词（`E2004`）、路径不可自选
  （接口只收 `LogSink`，`Logger` 连 path 属性都没有）、时间只来自注入 clock。
- **序列化归 structlog**：processor 链负责 level 归一、异常栈渲染、JSON 编码。
  比手写 `json.dumps` 多出两件真东西——`format_exc_info`（异常栈，不是 `str(exc)`）
  与可插拔 processor（脱敏/采样/上下文，S5 起会用）。
- **落盘仍归我们**：`structlog.PrintLogger` 写 stdout，`WriteLogger` 只是
  `file.write + flush`；两者都不做「路径由 Layout 给、进程崩溃也不丢」的组合。
  这里用 `WriteLogger` 但由 `Layout` 打开 append fd——"a" 模式在 Windows 上
  也是 `_O_APPEND`，多进程追加不会互相覆盖。

不使用 `structlog.get_logger()`：那是全局配置单例，`structlog.configure()` 一改
全系统跟着变，正好是 F1 要防的事。这里每条 sink 自持一条显式 processor 链。
"""

from __future__ import annotations

import json
from collections import deque
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TextIO

import structlog
from structlog import processors as sp

from v5.contracts.errors import E2003, E2004, E2007, FlowError

from .clock import Clock

# 结论词：结论只能由 Sentinel 写，日志不得出现（s0/02 §2.4）
_CONCLUSION_KEYS = frozenset({
    "pass", "reject", "significant", "verdict", "conclusion", "p_value", "promote",
})
# 凭据字段：命中即 E2004，且不落原文
_SECRET_KEYS = frozenset({
    "token", "secret", "key", "password", "api_key", "authorization", "credential",
})
# 允许进日志的附加键：异常信息由 structlog 的 format_exc_info 消费
_EXC_KEYS = frozenset({"exc_info", "stack_info"})
_FORBIDDEN_KEYS = _SECRET_KEYS | _CONCLUSION_KEYS

# 内存镜像默认上限。磁盘是唯一真相，内存副本只为调试与测试；
# 不设界的话一次长跑会把每一行都再留一份（5000 工位 × 每工位多行）。
DEFAULT_BUFFER = 1000

_REQUIRED_BY_SCOPE: dict[str, frozenset[str]] = {
    "upstream": frozenset({"dataset", "version"}),
    "ticket": frozenset({"ticket_id", "seq"}),
    "station": frozenset({"ticket_id", "station", "call_digest", "seq"}),
    "probe": frozenset({"ticket_id", "station", "call_digest", "seq"}),
}


def _json_default(obj: Any) -> Any:
    """只接受能确定性还原的类型；其余**报错**而不是 `default=str`。

    `default=str` 会把任意对象静默变成 `<Foo object at 0x7f...>`——地址进日志，
    同输入不同输出，正是可复现性最怕的那种"看起来写下来了"。
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    # 先试 tolist()：numpy 标量/数组与 pandas Series 都有，且 size>1 时
    # item() 会抛 ValueError（只允许 size==1）。反过来先 item() 会把多元素
    # 数组误杀成不可序列化（P1-4）。tolist() 对 0-d 返回标量、对 n-d 返回
    # 嵌套 list，两者都能被 json.dumps 正常编码。
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "item"):  # 仅有 item() 的标量型对象
        return obj.item()
    raise TypeError(
        f"日志不接受不可序列化类型 {type(obj).__name__}；"
        "请在调用处显式转成 str/float/int，不要让对象地址进日志"
    )


class LogSink(Protocol):
    """只进注入：Logger 拿到它即可写，无法得知也无法改路径。"""

    def write(self, record: Mapping[str, Any]) -> None: ...
    def close(self) -> None: ...


@dataclass
class StructlogLogSink:
    """jsonl 落盘：structlog processor 链 + Layout 给的 append fd。

    `extra_processors` 在 JSON 渲染之前插入，用于将来加脱敏/采样；
    渲染器固定为 JSONRenderer（稳定键序、ensure_ascii=False）。

    日志轮转（15-operations §2，I16）：`max_bytes > 0` 时按大小轮转，
    当前文件超限即重命名为 `.1` 并级联顺移（共 `backups` 份），新记录写
    回主文件。轮转在写路径上检查，不依赖进程重启；多进程并发追加时轮转
    与 stdlib RotatingFileHandler 有同样的交错限制——生产入口单写者
    （租约 E5002 保证），这里不为并发再加锁。
    """

    path: Path
    extra_processors: Sequence[Callable[..., Any]] = ()
    buffer_size: int = DEFAULT_BUFFER
    max_bytes: int = 32 * 1024 * 1024
    backups: int = 2
    _file: TextIO | None = field(default=None, init=False, repr=False)
    _bound: Any = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError(f"log sink 路径必须是 Path，得到 {type(self.path).__name__}")
        if self.max_bytes < 0 or self.backups < 0:
            raise FlowError(E2003, "log 轮转参数非法（max_bytes/backups >= 0）")
        if self.max_bytes == 0:
            self.max_bytes = 0  # 0 = 不轮转，保留旧行为
        self._writer: _RotatingWriter | None = None

    # ---- 内部：惰性打开 append fd 并装配 processor 链 -------------------
    def _ensure(self) -> Any:
        if self._closed:
            raise FlowError(E2004, "cannot write to a closed log sink")
        if self._bound is not None:
            return self._bound
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.max_bytes > 0:
            # "a" → O_APPEND：多进程追加不覆盖，进程崩溃时已 flush 的行仍在
            self._writer = _RotatingWriter(self.path, self.max_bytes, self.backups)
            writer = structlog.WriteLogger(self._writer)  # 每条 write+flush
        else:
            self._file = open(self.path, "a", encoding="utf-8", newline="\n")
            writer = structlog.WriteLogger(self._file)  # 每条 write+flush
        self._bound = structlog.wrap_logger(
            writer,
            processors=[
                *self.extra_processors,
                sp.format_exc_info,
                sp.JSONRenderer(
                    serializer=json.dumps,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=_json_default,
                ),
            ],
            # 全部显式传入：不吃 structlog.configure() 的全局默认值
            wrapper_class=structlog.BoundLogger,
            context_class=dict,
            cache_logger_on_first_use=False,
        )
        return self._bound

    def write(self, record: Mapping[str, Any]) -> None:
        fields = dict(record)
        # level/event 走 structlog 的定位参数；level 不交给 add_log_level 归一，
        # 否则契约里的 `warn` 会被改写成 `warning`。
        level = str(fields.pop("level", "info"))
        event = fields.pop("event", None)
        if event is None:
            raise FlowError(E2003, "log record 缺 event 字段", path="event")
        method = {"warn": "warn", "warning": "warn"}.get(level, level)
        if method not in {"debug", "info", "warn", "error"}:
            raise FlowError(E2003, f"unknown log level: {level}")
        # level 由我们自己带回 event_dict：不用 add_log_level，因为它会把
        # 契约里的 `warn` 归一成 `warning`。
        getattr(self._ensure(), method)(event, level=level, **fields)

    def close(self) -> None:
        if self._closed:
            return
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._bound = None
        self._closed = True


class _RotatingWriter:
    """structlog WriteLogger 需要的 `write(text)` + `flush()` 协议实现，
    每条记录写完后检查大小并按需轮转（`.jsonl` → `.1` → `.2` 级联）。"""

    def __init__(self, path: Path, max_bytes: int, backups: int) -> None:
        self._path = path
        self._max = int(max_bytes)
        self._backups = int(backups)
        self._file = open(path, "a", encoding="utf-8", newline="\n")

    def write(self, text: str) -> None:
        self._file.write(text)
        self._file.flush()
        if self._max > 0 and self._path.stat().st_size >= self._max:
            self._rotate()

    def flush(self) -> None:
        self._file.flush()

    def _rotate(self) -> None:
        self._file.close()
        oldest = self._path.with_suffix(self._path.suffix + f".{self._backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self._backups - 1, 0, -1):
            src = self._path.with_suffix(self._path.suffix + f".{i}")
            dst = self._path.with_suffix(self._path.suffix + f".{i + 1}")
            if src.exists():
                src.replace(dst)
        if self._path.exists():
            self._path.replace(
                self._path.with_suffix(self._path.suffix + ".1"))
        self._file = open(self._path, "a", encoding="utf-8", newline="\n")

    def close(self) -> None:
        try:
            self._file.flush()
            self._file.close()
        except Exception:  # noqa: BLE001 - close 阶段的二次失败不再抛
            pass


# 旧名保留：外部按 "jsonl sink" 引用，实现已换成 structlog 链。
JsonlLogSink = StructlogLogSink


@dataclass
class LogEntry:
    level: str
    event: str
    message: str
    ctx: dict[str, Any]
    at: datetime | None = None


@dataclass
class Logger:
    scope: str
    keys: dict[str, Any]
    entries: deque[LogEntry] = field(default_factory=lambda: deque(maxlen=DEFAULT_BUFFER))
    closed: bool = False
    sink: LogSink | None = None
    clock: Callable[[], datetime] | None = None
    # 每个 Logger 自持一个兜底时钟，不用模块级单例：模块级实例被多线程/
    # 多 Task 共享，冻结测试之间会互相污染。
    _fallback_clock: Clock = field(default_factory=Clock, repr=False, compare=False)

    def __post_init__(self) -> None:
        required = _REQUIRED_BY_SCOPE.get(self.scope)
        if required is None:
            raise FlowError(E2003, f"unknown log scope: {self.scope}")
        missing = required - self.keys.keys()
        if missing:
            raise FlowError(
                E2003,
                f"log scope={self.scope} missing required keys: {sorted(missing)}",
                hint="日志缺关联键等于无法归属的噪声",
            )
        # keys 随每条日志落盘，必须与 ctx 同受凭据闸门；否则密钥经构造或 bind
        # 进来后每条记录都会明文写出（P1-3）。
        bad = _forbidden_keys(self.keys)
        if bad:
            raise FlowError(
                E2004, f"forbidden log keys: {sorted(bad)}",
                hint="凭据与结论不得进日志；结论只能由 Sentinel 写",
            )

    def bind(self, extra: Mapping[str, Any]) -> "Logger":
        """派生一个带额外关联键的子 logger（共享条目缓冲与 sink）。

        extra 与 keys 走同一闸门（构造子实例时由 __post_init__ 统一校验）；
        不得让 bind 成为绕过凭据检查的后门。
        """
        child = Logger(
            scope=self.scope, keys={**self.keys, **extra}, entries=self.entries,
            sink=self.sink, clock=self.clock,
        )
        child._fallback_clock = self._fallback_clock
        return child

    # ---- 便捷级别入口（只解析本实例，不自行构造；F1 合规）----------------
    def debug(self, event: str, message: str, **ctx: Any) -> None:
        self.event("debug", event, message, **ctx)

    def info(self, event: str, message: str, **ctx: Any) -> None:
        self.event("info", event, message, **ctx)

    def warn(self, event: str, message: str, **ctx: Any) -> None:
        self.event("warn", event, message, **ctx)

    def error(self, event: str, message: str, **ctx: Any) -> None:
        self.event("error", event, message, **ctx)

    def event(self, level: str, event: str, message: str, **ctx: Any) -> None:
        if self.closed:
            raise FlowError(E2004, "cannot log to a closed logger")
        if level not in {"debug", "info", "warn", "error"}:
            raise FlowError(E2003, f"unknown log level: {level}")
        # 先在**扫描前**物化 tolist()/item()：否则自定义对象在扫描时是
        # 不透明值，序列化时才展开成 [{"api_key": ...}]，凭据闸门被绕过。
        ctx = {k: _materialize(v, set()) for k, v in ctx.items()}
        bad = _forbidden_keys(ctx)
        if bad:
            raise FlowError(
                E2004,
                f"forbidden log fields: {sorted(bad)}",
                hint="凭据与结论不得进日志；结论只能由 Sentinel 写",
            )
        # 时间一律走注入的 clock。未注入时退回本实例的 Clock（仍是设施，
        # 不直接 datetime.now），组合根（build_facilities）永远注入。
        # 关联键优先于 ctx：调用方临时传个 ticket_id/station 不得把归属改写，
        # 否则 trace 串线、缓存串线，事后无法从日志还原是谁写的（P1-5）。
        overlapping = self.keys.keys() & ctx.keys()
        if overlapping:
            raise FlowError(
                E2003, f"ctx 不得覆盖关联键: {sorted(overlapping)}",
                hint="需要新关联键请用 bind(extra)，不要在 event 的 ctx 里传同名键",
            )
        at = self.clock() if self.clock is not None else self._fallback_clock.wall()
        self.entries.append(LogEntry(level=level, event=event, message=message, ctx=dict(ctx), at=at))
        if self.sink is not None:
            self.sink.write({
                "at": at.isoformat(),
                "level": level,
                "event": event,
                "message": message,
                **self.keys,
                **ctx,
            })

    def close(self) -> None:
        """close 前必须 flush——崩溃时最需要日志。WriteLogger 已逐条 flush。"""
        if self.sink is not None:
            self.sink.close()
        self.closed = True


def _materialize(value: Any, visited: set[int]) -> Any:
    """在凭据扫描之前展开 tolist()/item()，杜绝"扫描时是对象、落盘时是密钥字典"。

    只展开一次（物化后的 list/dict 里若还有同类对象，交给 _forbidden_keys
    的递归 walk 再次展开），用 id 防自引用。
    """
    if isinstance(value, (str, bytes, bytearray, int, float, bool)) or value is None:
        return value
    marker = id(value)
    if marker in visited:
        return value
    visited.add(marker)
    if isinstance(value, Mapping):
        return {k: _materialize(v, visited) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_materialize(v, visited) for v in value]
    for attr in ("tolist", "item"):
        hook = getattr(value, attr, None)
        if callable(hook):
            try:
                return _materialize(hook(), visited)
            except (TypeError, ValueError, OverflowError):
                continue
    return value


def _forbidden_keys(ctx: Mapping[str, Any]) -> set[str]:
    """递归扫描凭据/结论词，任意深度都扫——深度截断等于静默放行。

    只扫顶层是漏的：`log.event("e","m", req={"api_key": "sk-..."})` 会把
    密钥原样写进 jsonl——嵌套结构才是真实调用最常见的样子。也不能加
    「超过 N 层就 return set()」：那样 9 层嵌套里的 api_key 照样落盘。
    这里不设深度上限，改用已访问容器 id 集合防自引用死循环：同一容器
    只扫一次，扫过即跳过（其内容已经检查过，不会漏）。

    list/tuple/set/frozenset 的每个元素也递归进去：不能只扫元素里直接的
    Mapping，`{"a": [[{"api_key": ...}]]}` 这类嵌套 list 必须同样拦下。
    """
    found: set[str] = set()
    visited: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)
            for key, value in node.items():
                name = str(key).lower()
                if name in _FORBIDDEN_KEYS and name not in _EXC_KEYS:
                    found.add(name)
                walk(value)
        elif isinstance(node, (list, tuple, set, frozenset)):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)
            for item in node:
                walk(item)

    # 统一先物化：keys/bind 路径与 ctx 路径都走同一个闸门，
    # tolist()/item() 里藏的凭据不能在扫描后才展开。
    walk(_materialize(ctx, set()))
    return found


def log_path(call_digest: str) -> str:
    """日志文件名由 call_digest 反推（F9）：去算法前缀后取 hex 前 16 位。

    注意不能对带前缀的完整串取 [:16]——会得到 ``sha256:abcdef012``，
    冒号在 Windows 是非法文件名字符。契约里的 ``call_digest[:16]``
    指的是摘要值本身，不含 ``sha256:`` 前缀。
    """
    short = call_digest.split(":", 1)[-1][:16]
    return f"{short}.jsonl"


def build_logger(
    scope: str,
    keys: Mapping[str, Any],
    *,
    sink: LogSink | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Logger:
    return Logger(scope=scope, keys=dict(keys), sink=sink, clock=clock)


# ---------------------------------------------------------------------------
# 全局便捷访问器（s0/02 §2.1 的 F1 例外）
# ---------------------------------------------------------------------------
# 这里**不构造**任何 logger：实例只能由组合根（应用启动处）用 install_log
# 放进当前上下文，其余各处通过 get_log() 只读解析。ContextVar 而非模块变量：
# 异步任务/多线程各自隔离，测试可用 token 精确还原，不会跨用例污染。
# 未 install 时 get_log() 抛 E2007——绝不返回静默丢弃的 no-op 替身。
_INSTALLED_LOG: ContextVar[Logger | None] = ContextVar("v5_log", default=None)


def install_log(logger: Logger) -> Token[Logger | None]:
    """把已构造的 logger 注入当前上下文；只在应用启动处调用。

    返回 token，进程/测试收尾时交还 ``reset_log``。拒绝 closed 实例——
    安装一个写不进去的 logger 等于让日志静默消失。
    """
    if not isinstance(logger, Logger):
        raise TypeError(f"install_log 只接受 Logger，得到 {type(logger).__name__}")
    if logger.closed:
        raise FlowError(E2004, "cannot install a closed logger")
    return _INSTALLED_LOG.set(logger)


def get_log() -> Logger:
    """解析组合根已注入的 logger；未注入即 E2007（fail-closed）。

    这是解析，不是获取：本函数不创建、不配置、不改写全局 logger，
    因此不违反 F1「禁止模块级自行取得设施」。
    """
    logger = _INSTALLED_LOG.get()
    if logger is None:
        raise FlowError(
            E2007,
            "log 未由组合根注入；请在应用启动处调用 install_log",
            hint="禁止模块级自行构造 logger（F1/E2007）",
        )
    return logger


def reset_log(token: Token[Logger | None]) -> None:
    """把当前上下文的 logger 还原到 install 之前（进程退出/测试收尾）。"""
    _INSTALLED_LOG.reset(token)
