"""V5 日志落盘测试（s0/02 §2）。

验证：注入 sink 后每条立即落盘、路径由 Layout 唯一定义、
时间来自注入 clock、凭据/结论词拦截仍生效、close 后拒绝写入。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from v5.contracts.errors import E2003, E2004, E2007, FlowError
from v5.facilities import build_facilities, get_log, install_log, reset_log
from v5.facilities.log import (
    DEFAULT_BUFFER, JsonlLogSink, _forbidden_keys, build_logger, log_path,
)
from v5.storage.layout import Layout

_FROZEN = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def _err_code(fn):
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


def test_log_path_from_call_digest():
    # F9：去算法前缀后取 hex 前 16 位；冒号不能进 Windows 文件名
    assert log_path("sha256:abcdef0123456789REST") == "abcdef0123456789.jsonl"
    assert log_path("abcdef0123456789REST") == "abcdef0123456789.jsonl"


def test_layout_log_file_uses_call_digest(tmp_path):
    L = Layout(tmp_path)
    p = L.log_file("run-1", "sha256:abcdef0123456789ff")
    assert p == tmp_path / "runs" / "run-1" / "logs" / "abcdef0123456789.jsonl"


def test_injected_sink_writes_each_event(tmp_path):
    sink = JsonlLogSink(tmp_path / "runs" / "r1" / "logs" / "x.jsonl")
    logger = build_logger(
        "station",
        {"ticket_id": "T1", "seq": 1, "station": "Panel", "call_digest": "sha256:" + "a" * 64},
        sink=sink,
        clock=lambda: _FROZEN,
    )
    logger.event("info", "panel.built", "rows=20", rows=20)
    logger.event("warn", "panel.thin", "width low", width=3)

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["at"] == "2026-11-16T12:00:00+00:00"
    assert first["level"] == "info" and first["event"] == "panel.built"
    assert first["station"] == "Panel" and first["ticket_id"] == "T1"
    assert first["rows"] == 20


def test_logger_has_no_path_attribute(tmp_path):
    """Logger 只拿到 sink.write，无法得知或改路径（s0/02 §2.4）。"""
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    assert not hasattr(logger, "path")
    assert not hasattr(logger, "_path")


def test_close_then_event_rejected(tmp_path):
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    logger.close()
    assert _err_code(lambda: logger.event("info", "x", "y")) == E2004


def test_build_facilities_with_sink_wires_clock(tmp_path):
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    fx = build_facilities(
        scope="ticket", ticket_id="T1", master_seed=1,
        frozen_at=_FROZEN, log_sink=sink,
    )
    fx.log.event("info", "run.start", "begin")
    rec = json.loads(sink.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["at"] == "2026-11-16T12:00:00+00:00"


def test_secret_and_verdict_still_blocked_with_sink(tmp_path):
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    assert _err_code(lambda: logger.event("info", "e", "m", api_key="abc")) == E2004
    assert _err_code(lambda: logger.event("info", "e", "m", verdict="pass")) == E2004
    assert not sink.path.exists()


# ---------------------------------------------------------------- structlog 接入
def test_nested_secret_is_rejected_not_written(tmp_path):
    """嵌套结构才是真实调用最常见的样子；只扫顶层等于把密钥写进 jsonl。"""
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    for ctx in (
        {"req": {"api_key": "sk-live-1"}},
        {"batch": [{"token": "abc"}]},
        {"inner": {"deeper": {"secret": "s"}}},
    ):
        assert _err_code(lambda c=ctx: logger.event("info", "e", "m", **c)) == E2004
    assert not sink.path.exists()


def test_deeply_nested_secret_is_rejected_not_written(tmp_path):
    """P0 回归：深度截断后 return set() 等于静默放行，9 层嵌套的 api_key 会明文落盘。"""
    sink = JsonlLogSink(tmp_path / "deep.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)

    def nest(depth, leaf):
        obj = leaf
        for _ in range(depth):
            obj = {"a": obj}
        return obj

    for depth in (9, 16):
        ctx = nest(depth, {"api_key": "sk-DEEP"})
        assert _err_code(lambda c=ctx: logger.event("info", "e", "m", **c)) == E2004
    assert not sink.path.exists()


def test_nested_list_secret_is_rejected_not_written(tmp_path):
    """P0 回归：list 只扫直接 Mapping 子元素时，[[{...}]] 会穿透 E2004。"""
    sink = JsonlLogSink(tmp_path / "list.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    for ctx in (
        {"a": [[{"api_key": "sk-LIST"}]]},
        {"a": ([[{"secret": "s"}]],)},
    ):
        assert _err_code(lambda c=ctx: logger.event("info", "e", "m", **c)) == E2004
    assert not sink.path.exists()


def test_self_referential_ctx_terminates_and_still_scans():
    """自引用靠 visited id 终止，不能靠「超深度放行」——后者会漏扫整棵子树。"""
    cycle: dict[str, Any] = {"a": {}}
    cycle["a"]["self"] = cycle
    assert _forbidden_keys(cycle) == set()
    cycle["a"]["api_key"] = "sk-CYCLE"
    assert _forbidden_keys(cycle) == {"api_key"}


def test_exception_traceback_is_rendered(tmp_path):
    """三方链的价值之一：异常栈，而不是 str(exc) 一行。"""
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    try:
        raise ValueError("boom-detail")
    except ValueError:
        logger.event("error", "station.failed", "capture", exc_info=True)
    rec = json.loads(sink.path.read_text(encoding="utf-8").splitlines()[0])
    assert "Traceback (most recent call last)" in rec["exception"]
    assert "ValueError: boom-detail" in rec["exception"]


def test_level_warn_is_not_normalized(tmp_path):
    """契约的 level 取值是 debug/info/warn/error；不得被归一成 warning。"""
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    logger.event("warn", "e", "m")
    rec = json.loads(sink.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["level"] == "warn"


def test_non_serializable_ctx_fails_loudly(tmp_path):
    """`default=str` 会把对象地址写进日志——同输入不同输出。"""
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)

    class Opaque:
        pass

    try:
        logger.event("info", "e", "m", obj=Opaque())
    except TypeError as exc:
        assert "Opaque" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("不可序列化对象必须报错")


def test_two_sinks_append_same_file(tmp_path):
    """"a" 模式 = O_APPEND：同 ticket 的多个 sink 实例互相追加，不覆盖。"""
    p = tmp_path / "x.jsonl"
    for i in range(2):
        s = JsonlLogSink(p)
        build_logger("ticket", {"ticket_id": "T", "seq": i}, sink=s).event("info", "e", f"m{i}")
        s.close()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_memory_buffer_is_bounded(tmp_path):
    """磁盘是唯一真相；内存副本无界会随长跑线性增长。"""
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0})
    for i in range(DEFAULT_BUFFER + 50):
        logger.event("info", "e", str(i))
    assert len(logger.entries) == DEFAULT_BUFFER


# ---------------------------------------------------------------- 全局便捷访问器
def test_get_log_without_install_fails_closed():
    """F1 例外边界：未注入即 E2007，绝不返回静默丢弃的 no-op。"""
    assert _err_code(get_log) == E2007


def test_install_resolve_reset_and_level_helpers(tmp_path):
    """组合根装一次，任意处 get 到同一实例；info/warn/error 落到同一 sink。"""
    sink = JsonlLogSink(tmp_path / "process.jsonl")
    logger = build_logger(
        "ticket", {"ticket_id": "T", "seq": 0}, sink=sink, clock=lambda: _FROZEN,
    )
    token = install_log(logger)
    try:
        assert get_log() is logger
        get_log().info("run.start", "begin")
        get_log().warn("cache.stale", "stale")
        get_log().error("run.failed", "boom", exc_info=False)
    finally:
        reset_log(token)

    recs = [json.loads(line) for line in sink.path.read_text(encoding="utf-8").splitlines()]
    assert [r["level"] for r in recs] == ["info", "warn", "error"]
    assert [r["event"] for r in recs] == ["run.start", "cache.stale", "run.failed"]
    # reset 后回到未安装状态
    assert _err_code(get_log) == E2007


def test_install_rejects_closed_logger(tmp_path):
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0},
                          sink=JsonlLogSink(tmp_path / "x.jsonl"))
    logger.close()
    assert _err_code(lambda: install_log(logger)) == E2004


# ---------------------------------------------------------------- P1 回归
def test_forbidden_key_in_keys_and_bind_is_rejected(tmp_path):
    """P1-3：keys/bind 随每条日志落盘，必须与 ctx 同受闸门。"""
    sink = JsonlLogSink(tmp_path / "keys.jsonl")
    assert _err_code(lambda: build_logger(
        "ticket", {"ticket_id": "T", "seq": 0, "api_key": "sk-KEYS"}, sink=sink,
    )) == E2004
    ok = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    assert _err_code(lambda: ok.bind({"password": "p"})) == E2004
    # 嵌套也一样
    assert _err_code(lambda: ok.bind({"req": [[{"token": "t"}]]})) == E2004
    assert not sink.path.exists()


def test_ctx_cannot_override_correlation_keys(tmp_path):
    """P1-5：调用方临时传 ticket_id/station 会串线，必须拒绝而非静默覆盖。"""
    sink = JsonlLogSink(tmp_path / "ctx.jsonl")
    logger = build_logger(
        "station", {"ticket_id": "T1", "seq": 1, "station": "Panel",
                    "call_digest": "sha256:" + "a" * 64},
        sink=sink,
    )
    for key in ("ticket_id", "station", "call_digest", "seq"):
        assert _err_code(lambda k=key: logger.event("info", "e", "m", **{k: "OTHER"})) == E2003
    logger.event("info", "e", "m", rows=3)  # 普通附加字段不受影响
    rec = json.loads(sink.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["ticket_id"] == "T1" and rec["station"] == "Panel"


def test_numpy_and_pandas_values_serialize(tmp_path):
    """P1-4：size>1 的数组/Series 的 item() 会抛 ValueError，必须先 tolist()。"""
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    sink = JsonlLogSink(tmp_path / "np.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    logger.event("info", "e", "m", arr=np.array([1, 2, 3]), series=pd.Series([4, 5]))
    rec = json.loads(sink.path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["arr"] == [1, 2, 3]
    assert rec["series"] == [4, 5]
