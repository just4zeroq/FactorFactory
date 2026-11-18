"""V5 facilities 的 S0 验收（s0/02-crosscutting.md §10 的 F1–F10）。

S0 可验证的：F2（缺设施失败）、F3（凭据拒绝）、F4（rng 派生）、
F8（冻结时钟）、F9（日志名反推）、F10（station notify 越权）。
缓存命中校验（F5/F6）在内存实现上可测存在性部分。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from v5.contracts.errors import E2001, E2003, E2004, FlowError
from v5.facilities import (
    Cache,
    build_facilities,
    frozen_clock,
)
from v5.facilities.log import build_logger, log_path
from v5.facilities.rng import RngFactory


def _err_code(fn):
    with pytest.raises(FlowError) as e:
        fn()
    return e.value.error.code


class TestFacilitiesBuild:
    def test_strict_missing_facility_fails(self):
        """F2：strict 模式缺任意设施 → E2003，退出码 2。"""
        code = _err_code(lambda: build_facilities(
            scope="ticket", ticket_id="t1", master_seed=42,
            overrides={"digest": None},
        ))
        assert code == E2003

    def test_all_seven_facilities_built(self):
        f = build_facilities(scope="ticket", ticket_id="t1", master_seed=42)
        for name in ("log", "trace", "clock", "rng", "digest", "cache", "notify"):
            assert getattr(f, name) is not None

    def test_bad_scope_rejected(self):
        assert _err_code(lambda: build_facilities(
            scope="bogus", ticket_id="t1", master_seed=42,
        )) == E2003


class TestRng:
    def test_f4_same_seed_same_scope_same_derived(self):
        """F4：同 master_seed + 同 scope_path 两次派生相同。"""
        r1 = RngFactory(master_seed=123)
        r2 = RngFactory(master_seed=123)
        assert r1.derive_seed("cs_q4_a:search:gp_symbolic") == r2.derive_seed("cs_q4_a:search:gp_symbolic")

    def test_f4_one_char_change_differs(self):
        r = RngFactory(master_seed=123)
        assert r.derive_seed("scope:a") != r.derive_seed("scope:b")

    def test_independent_instances(self):
        r = RngFactory(master_seed=123)
        a = r.scope("x")
        b = r.scope("x")
        a.random()
        assert a.random() != b.random()  # 各自从同一 seed 起步，不受彼此影响


class TestClock:
    def test_f8_frozen_clock_same_wall(self):
        """F8：冻结时钟下 wall() 恒等。"""
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        c = frozen_clock(at)
        assert c.wall() == at
        assert c.wall() == c.wall()
        assert c.today() == at.date()

    def test_frozen_requires_tz(self):
        with pytest.raises(ValueError):
            frozen_clock(datetime(2026, 1, 1))


class TestLog:
    def test_f3_secret_field_rejected(self):
        """F3：ctx 含 token/secret/key → E2004。"""
        logger = build_logger("ticket", {"ticket_id": "t1", "seq": 1})
        assert _err_code(lambda: logger.event("info", "x", "y", token="abc")) == E2004

    def test_conclusion_field_rejected(self):
        logger = build_logger("ticket", {"ticket_id": "t1", "seq": 1})
        assert _err_code(lambda: logger.event("info", "x", "y", verdict="pass")) == E2004

    def test_missing_required_key_rejected(self):
        assert _err_code(lambda: build_logger("station", {"ticket_id": "t1"})) == E2003

    def test_f9_log_name_from_call_digest(self):
        d = "abcdef0123456789" + "0" * 48
        assert log_path(d) == "abcdef0123456789.jsonl"

    def test_child_logger_shares_entries(self):
        logger = build_logger("ticket", {"ticket_id": "t1", "seq": 1})
        child = logger.bind({"extra": 1})
        child.event("info", "e", "m")
        assert len(logger.entries) == 1


class TestNotify:
    def test_f10_station_notify_forbidden(self):
        """F10：station 作用域 notify.emit → E2001，不是静默忽略。"""
        f = build_facilities(scope="station", ticket_id="t1", master_seed=1,
                             log_keys={"station": "Panel", "call_digest": "0" * 64})
        assert _err_code(lambda: f.notify.emit("done", {"status": "done"})) == E2001

    def test_notify_failure_does_not_raise(self):
        """通知失败只记 trace，不改变结论。"""
        def boom(_):
            raise RuntimeError("offline")
        f = build_facilities(scope="ticket", ticket_id="t1", master_seed=1, notify_sink=boom)
        result = f.notify.emit("done", {"status": "done"})
        assert result.delivered is False
        assert f.notify.trace_notes
        assert result.error is not None

    def test_notify_payload_whitelist(self):
        f = build_facilities(scope="ticket", ticket_id="t1", master_seed=1)
        assert _err_code(lambda: f.notify.emit("done", {"payload_secret": "x"})) == E2004

    def test_nested_secret_in_allowed_key_is_rejected(self):
        """P1-8：{"status": {"api_key": ...}} 不得穿透（值必须是标量）。"""
        f = build_facilities(scope="ticket", ticket_id="t1", master_seed=1)
        for bad in ({"status": {"api_key": "sk-X"}}, {"error_code": ["x"]}):
            assert _err_code(lambda b=bad: f.notify.emit("done", b)) == E2004


class TestCache:
    def test_f5_deleted_file_is_miss(self, tmp_path):
        """F5：命中但文件被删 → 视为未命中，不抛错。"""
        from v5.contracts.digest import compute_digest

        c = Cache(scope="ticket", root=tmp_path)
        f = tmp_path / "p.json"
        f.write_text("{}", encoding="utf-8")
        c.put("call1", "p.json", compute_digest({}))
        assert c.get("call1") is not None
        f.unlink()
        assert c.get("call1") is None

    def test_f5_tampered_file_is_miss(self, tmp_path):
        """F5：文件还在但内容被改 → 未命中（不能把篡改产物当命中返回）。"""
        from v5.contracts.digest import compute_digest

        c = Cache(scope="ticket", root=tmp_path)
        f = tmp_path / "p.json"
        f.write_text("{}", encoding="utf-8")
        c.put("call1", "p.json", compute_digest({}))
        f.write_text('{"tampered": true}', encoding="utf-8")
        assert c.get("call1") is None

    def test_station_cache_readonly(self, tmp_path):
        c = Cache(scope="station", root=tmp_path)
        assert _err_code(lambda: c.put("k", "p", "d")) == E2001
        node_on = Cache(scope="station", root=tmp_path, node_enabled=True)
        assert _err_code(lambda: node_on.put_node("n", "p", "d")) == E2001

    def test_readonly_cache_cannot_invalidate(self, tmp_path):
        """P1-9：只读作用域不能靠 invalidate 删掉 ticket 缓存项。"""
        c = Cache(scope="probe", root=tmp_path)
        assert _err_code(lambda: c.invalidate("call1")) == E2001

    def test_node_cache_off_by_default(self, tmp_path):
        c = Cache(scope="ticket", root=tmp_path)
        c.put_node("n", "p", "d")
        assert c.get_node("n") is None

def test_build_notifier_is_single_f7_configuration():
    """F1：告警设施只有 `build_notifier` 一个构造入口，F7 阈值不再两处写。"""
    from v5.facilities import build_facilities
    from v5.facilities.notify import build_notifier

    n = build_notifier("ticket")
    assert (n.dedup_window_seconds, n.max_per_window) == (3600.0, 10)
    fx = build_facilities(scope="ticket", ticket_id="T1", master_seed=1)
    assert (fx.notify.dedup_window_seconds, fx.notify.max_per_window) == (3600.0, 10)

