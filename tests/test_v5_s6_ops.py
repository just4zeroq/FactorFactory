"""S6 运维四件 + reconcile + 告警（AC51 的磁盘满演练 / F5 / F7 / 15-operations §3）。

覆盖：

1. **F5** cache 容量上限 + LRU 淘汰：键表无界 = 长跑内存线性增长；
   淘汰只丢键表、不动文件（文件归 GC 管，缓存无权碰）。
2. **F7** notify 去重（同 key 窗口内只投递一次）与阈值（滑动窗口限流）。
3. **日志轮转**：process.jsonl 超限级联顺移，备份份数有界。
4. **磁盘满演练**（AC51 六类之一）：写入 OSError → E5001 结构化错误；
   trace 留 failed 记录；不产半成品 parcel.json。
5. **reconcile**：登记/产物链/对象三方核对；原生 content-digest ref 不误报。
6. **pipeline 告警**：零候选 / n_trials 突降 / DSR 全失败 → 信封 + Notifier。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.cli import main
from test_v5_s5_single_step import _plan
from facilities.cache import Cache
import pytest
import pytest
from facilities.notify import Notifier


# ---------------------------------------------------------------- F5 cache


def test_f5_cache_evicts_lru_over_capacity(tmp_path):
    from contracts.digest import blob_digest
    cache = Cache(scope="ticket", root=tmp_path, max_entries=2)
    a, b, c = tmp_path / "rel_a", tmp_path / "rel_b", tmp_path / "rel_c"
    for p in (a, b, c):
        p.write_bytes(b"x")
    cache.put("ka", "rel_a", blob_digest(b"x"))
    cache.put("kb", "rel_b", blob_digest(b"x"))
    # 访问 a → b 成为 LRU 队首
    assert cache.get("ka") is not None
    cache.put("kc", "rel_c", blob_digest(b"x"))
    assert cache.evictions == 1
    assert cache.get("kb") is None          # b 被淘汰（文件仍在）
    assert b.exists()
    assert cache.get("ka") is not None      # 刚访问过的 a 幸存
    assert cache.get("kc") is not None


def test_f5_cache_rejects_zero_capacity():
    with pytest.raises(Exception):
        Cache(scope="ticket", root=tmp_path_dir(), max_entries=0)


def tmp_path_dir():
    import tempfile
    return Path(tempfile.mkdtemp())


# ---------------------------------------------------------------- F7 notify


def test_f7_dedup_suppresses_same_key_within_window():
    seen: list[dict] = []
    n = Notifier(scope="ticket", sink=seen.append,
                 dedup_window_seconds=60.0, clock=lambda: 1000.0)
    r1 = n.emit("alert.zero_candidates", {"status": "warn"})
    r2 = n.emit("alert.zero_candidates", {"status": "warn"})
    r3 = n.emit("alert.zero_candidates", {"status": "warn"}, dedup_key="other")
    assert r1.delivered and not r1.deduped
    assert not r2.delivered and r2.deduped and r2.error is None
    assert r3.delivered                       # 不同 key 不受去重影响
    assert len(seen) == 2                     # 第二条被抑制，未投递


def test_f7_dedup_window_expires():
    seen: list[dict] = []
    now = [1000.0]
    n = Notifier(scope="ticket", sink=seen.append,
                 dedup_window_seconds=60.0, clock=lambda: now[0])
    assert n.emit("alert.x", {"status": "info"}).delivered
    now[0] += 30.0
    assert n.emit("alert.x", {"status": "info"}).deduped
    now[0] += 31.0                            # 窗口外 → 再投递
    assert n.emit("alert.x", {"status": "info"}).delivered
    assert len(seen) == 2


def test_f7_threshold_throttles_flood_within_window():
    seen: list[dict] = []
    now = [0.0]
    n = Notifier(scope="ticket", sink=seen.append, max_per_window=2,
                 rate_window_seconds=60.0, clock=lambda: now[0])
    assert n.emit("a1", {"status": "info"}).delivered
    assert n.emit("a2", {"status": "info"}).delivered
    r3 = n.emit("a3", {"status": "info"})     # 窗口内第 3 条 → 限流
    assert not r3.delivered and r3.deduped
    assert any("throttled" in note for note in n.trace_notes)
    now[0] += 61.0                            # 新窗口 → 放行
    assert n.emit("a4", {"status": "info"}).delivered
    assert len(seen) == 3


# ---------------------------------------------------------------- 日志轮转


def test_log_rotation_shifts_backups(tmp_path):
    from facilities.log import StructlogLogSink
    path = tmp_path / "logs" / "process.jsonl"
    sink = StructlogLogSink(path, max_bytes=200, backups=2)
    for i in range(30):
        sink.write({"level": "info", "event": f"e{i}",
                    "message": "x" * 30})
    sink.close()
    assert path.exists()
    assert (path.parent / "process.jsonl.1").exists(), "至少轮转出一班"
    assert not (path.parent / "process.jsonl.3").exists(), "备份数有界"
    # 每个备份都是合法 jsonl 行
    for f in (path, path.parent / "process.jsonl.1", path.parent / "process.jsonl.2"):
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)


# ---------------------------------------------------------------- 磁盘满演练


def test_disk_full_is_structured_e5001_with_failed_trace(seeded, tmp_path, capsys,
                                                         monkeypatch):
    """AC51：磁盘满注入 → E5001 + failed trace + 无半成品 parcel。

    复现方式：让 payload 落盘那一步抛 OSError(ENOSPC)。run 必须把存储
    故障如实报告为 E5001（而不是 E3004「未预期异常」），ticket 目录保留，
    不留下半成品 parcel.json（写盘是原子 replace + 全部就绪才写 parcel.json）。
    """
    import os
    from runtime.store import TicketWriter

    capsys.readouterr()
    assert main(["run", "--plan", str(seeded), "--live"]) == 0
    out = json.loads(capsys.readouterr().out)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    layout = tmp_path
    import shutil
    from storage.layout import Layout
    writer = TicketWriter(Layout(layout), ticket)
    # run 已提交过 Boundary；先移走正式产物，否则 commit 幂等短路、
    # 磁盘满注入根本不会被触发。
    target = next(run_dir.glob("parcels/*-Boundary-*"))
    shutil.rmtree(target)

    # 在 write_bytes 的 try 块**内部**注入失败（os.replace 那一步）：
    # 打点在外面会绕过 E5001 映射，测的就不是真实路径了。
    real_replace = os.replace

    def full_replace(src, dst, *a, **kw):
        if str(dst).endswith("payload.json"):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", full_replace)

    capsys.readouterr()
    code = main(["station", "Boundary", "--commit", "--on", ticket])
    assert code == 1
    env = json.loads(capsys.readouterr().out)
    assert env["errors"][0]["code"] == "E5001", json.dumps(env, ensure_ascii=False)
    # trace 留下 failed 记录（trace.jsonl 只追加在 ticket 根）
    trace_path = run_dir / "trace.jsonl"
    assert trace_path.exists(), "失败也必须有一条 trace（15-operations §2）"
    last = trace_path.read_text(encoding="utf-8").splitlines()[-1]
    assert json.loads(last)["status"] == "failed"
    assert json.loads(last)["error_code"] == "E5001"
    # 无半成品 parcel.json（写盘原子 replace + 全部就绪才写 parcel.json；
    # 失败残留的空目录不算产物——glob parcel.json 为空即无产物）
    residue = [d for d in (run_dir / "parcels").glob("01-Boundary-*")
               if (d / "parcel.json").exists()]
    assert not residue, f"半成品 parcel 不得残留: {residue}"
    # 恢复写盘后重放成功（处置路径：清理空间后重跑）
    monkeypatch.setattr(os, "replace", real_replace)
    capsys.readouterr()
    code = main(["station", "Boundary", "--commit", "--on", ticket])
    assert code == 0, capsys.readouterr().out


# ---------------------------------------------------------------- reconcile


def test_reconcile_clean_on_fresh_root(seeded, tmp_path, capsys):
    capsys.readouterr()
    assert main(["run", "--plan", str(seeded), "--live"]) == 0
    capsys.readouterr()
    code = main(["reconcile"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is True and env["data"]["clean"] is True
    assert env["data"]["parcels"] >= 8
    # 原生 content-digest ref（panel_ref/score_ref）不算 object_missing
    kinds = {v["kind"] for v in env["data"]["violations"]}
    assert not ({"object_missing", "payload_digest_mismatch"} & kinds)


def test_reconcile_detects_tampered_payload(seeded, tmp_path, capsys):
    capsys.readouterr()
    main(["run", "--plan", str(seeded), "--live"])
    out = json.loads(capsys.readouterr().out)
    run_dir = tmp_path / "runs" / out["run_id"]
    payload = next(run_dir.glob("parcels/*-Target-*/payload.json"))
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["tampered"] = True
    payload.write_text(json.dumps(data), encoding="utf-8")

    capsys.readouterr()
    code = main(["reconcile"])
    assert code == 3, "发现损坏 → 退出码 3（契约违反）"
    env = json.loads(capsys.readouterr().out)
    assert env["errors"][0]["code"] == "E2005"
    ctx = env["errors"][0].get("context") or {}
    kinds = {v["kind"] for v in (ctx.get("violations") or [])}
    assert "payload_digest_mismatch" in kinds


# ---------------------------------------------------------------- pipeline 告警


def test_pipeline_alerts_on_zero_candidates(seeded, tmp_path, capsys, monkeypatch):
    """零候选 → warn 告警进信封并经 Notifier 投递（15-operations §3）。"""
    from app.cli import pipeline as pipeline_mod

    capsys.readouterr()
    assert main(["run", "--plan", str(seeded), "--live"]) == 0
    json.loads(capsys.readouterr().out)
    from storage.layout import Layout
    layout = Layout(tmp_path)
    run_data = {"state": "skipped", "skip_reason": "no_candidate_passed"}
    alerts = pipeline_mod.collect_alerts(run_data, layout)
    kinds = [a["kind"] for a in alerts]
    assert "zero_candidates" in kinds
    delivered = pipeline_mod._emit_alerts(alerts)
    assert delivered[0]["notified"] is True
    assert delivered[0]["deduped"] is False


def test_pipeline_alerts_on_n_trials_drop(seeded, tmp_path, capsys):
    """n_trials 较上一轮降幅 >50% → warn（计数链断裂，15-operations §4-7）。"""
    from app.cli import pipeline as pipeline_mod

    capsys.readouterr()
    main(["run", "--plan", str(seeded), "--live"])
    json.loads(capsys.readouterr().out)
    # 构造历史：更老的一轮 100，本轮（刚落盘的 Search）实测 <50
    from storage.layout import Layout
    layout = Layout(tmp_path)
    current = sorted(layout.runs.glob("*/parcels/04-Search-*/payload.json"))
    assert current, "需要本轮已落盘的 Search 产物"
    old_ticket = layout.runs / "20240101T000000Z-history01"
    old_parcel = old_ticket / "parcels" / "04-Search-aaa"
    old_parcel.mkdir(parents=True)
    (old_parcel / "payload.json").write_text('{"n_trials": 100}', encoding="utf-8")
    alerts = pipeline_mod.collect_alerts({"state": "done"}, layout)
    assert any(a["kind"] == "n_trials_drop" for a in alerts), alerts

# ---------------------------------------------------------------- 夹具（与 s5 同源）
FROZEN = "2024-03-11T00:00:00+00:00"


class _FakeExchange:
    def __init__(self, bars):
        self.bars = bars

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        rows = [list(r) for r in self.bars[symbol] if since is None or r[0] >= since]
        return rows[:limit]


def _bars():
    from datetime import datetime, timedelta
    out = {}
    for idx, sym in enumerate(SYMBOLS := tuple(f"S{i}" for i in range(6))):
        rows = []
        rng = np.random.default_rng(idx + 11)
        px = 100.0
        for day in range(70):
            ts = datetime(2024, 1, 1, tzinfo=None) + timedelta(days=day)
            px = float(px * np.exp(rng.normal(0, 0.02)))
            rows.append([int(ts.timestamp() * 1000), px, px * 1.01, px * 0.99,
                         px, float(1e3 + idx * 100 + day)])
        out[sym] = rows
    return out


@pytest.fixture()
def seeded(tmp_path: Path, monkeypatch) -> Path:
    """fetch 采集供给 + 提供 plan 文件（run 由各测试自行决定怎么跑）。"""
    from datetime import datetime, timedelta, timezone
    from app.cli import fetch as fetch_mod
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    symbols = tuple(f"S{i}" for i in range(6))
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({"cs_s5": list(symbols)}), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_client_factory", lambda _eid: _FakeExchange(_bars()))
    assert main(["fetch", "--datasets", "ohlcv", "--symbol-set", "cs_s5",
                 "--start", "2024-01-01T00:00:00+00:00",
                 "--end", "2024-03-11T00:00:00+00:00",
                 "--exchange", "binanceusdm", "--market", "futures",
                 "--limit", "200"]) == 0
    return plan_path

# ---------------------------------------------------------------- 夹具（与 s5 同源）
FROZEN = "2024-03-11T00:00:00+00:00"


class _FakeExchange:
    def __init__(self, bars):
        self.bars = bars

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        rows = [list(r) for r in self.bars[symbol] if since is None or r[0] >= since]
        return rows[:limit]


def _bars():
    from datetime import datetime, timedelta
    out = {}
    for idx, sym in enumerate(SYMBOLS := tuple(f"S{i}" for i in range(6))):
        rows = []
        rng = np.random.default_rng(idx + 11)
        px = 100.0
        for day in range(70):
            ts = datetime(2024, 1, 1, tzinfo=None) + timedelta(days=day)
            px = float(px * np.exp(rng.normal(0, 0.02)))
            rows.append([int(ts.timestamp() * 1000), px, px * 1.01, px * 0.99,
                         px, float(1e3 + idx * 100 + day)])
        out[sym] = rows
    return out


@pytest.fixture()
def seeded(tmp_path: Path, monkeypatch) -> Path:
    """fetch 采集供给 + 提供 plan 文件（run 由各测试自行决定怎么跑）。"""
    from datetime import datetime, timedelta, timezone
    from app.cli import fetch as fetch_mod
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    symbols = tuple(f"S{i}" for i in range(6))
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({"cs_s5": list(symbols)}), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_client_factory", lambda _eid: _FakeExchange(_bars()))
    assert main(["fetch", "--datasets", "ohlcv", "--symbol-set", "cs_s5",
                 "--start", "2024-01-01T00:00:00+00:00",
                 "--end", "2024-03-11T00:00:00+00:00",
                 "--exchange", "binanceusdm", "--market", "futures",
                 "--limit", "200"]) == 0
    return plan_path
