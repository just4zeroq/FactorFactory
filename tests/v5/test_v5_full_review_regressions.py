"""分模块完整 review 的回归守卫：每条对应一条已确认并修复的缺陷。"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from v5.app.render.ndjson import render_ndjson
from v5.contracts.digest import plan_digest
from v5.contracts.errors import E2003, E2004, E5004, FlowError
from v5.facilities import build_facilities
from v5.facilities.log import JsonlLogSink, build_logger
from v5.runtime.compiler import compile_plan
from v5.runtime.charter import load_plan
from v5.storage.index import IndexDB
from v5.storage.ledger import Ledger
from v5.storage.layout import Layout
from v5.storage.objects import ObjectStore
from v5.upstream.store import payload_checksum

PLAN = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "v5" / "plan_valid.json"


def _err_code(fn):
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


# ---------------------------------------------------------------- P0 checksum
def test_payload_checksum_handles_more_than_inline_limit():
    """真实面板轻松超过 8192 行；checksum 不得撞 MAX_INLINE_SEQUENCE。"""
    n = 8300
    frame = pd.DataFrame({
        "symbol": ["BTC"] * n,
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC"),
        "close": [float(i) for i in range(n)],
    })
    digest = payload_checksum(frame)
    assert digest.startswith("sha256:")
    flipped = frame.assign(close=[float(i + 1) for i in range(n)])
    assert payload_checksum(flipped) != digest
    # 行序无关：倒序输入必须得到同一指纹（这是「内容身份」的定义）。
    assert payload_checksum(frame.iloc[::-1].reset_index(drop=True)) == digest


# ---------------------------------------------------------------- compiler
def test_compile_is_pure_and_plan_digest_is_raw():
    """--set 不得污染入参，也不得混进 plan_digest（H(plan_raw)）。"""
    plan = load_plan(PLAN)
    snapshot = copy.deepcopy(plan)
    clean = compile_plan(baseline=None, plan=plan, strict=True)
    assert clean.fingerprints["plan_digest"] == plan_digest(plan)
    patched = compile_plan(
        baseline=None, plan=plan, strict=True,
        overrides={"boundary.params.k": 61}, allow_constitutive_override=True,
    )
    assert plan == snapshot, "compile_plan 就地改了调用方的 plan"
    assert patched.fingerprints["plan_digest"] == clean.fingerprints["plan_digest"]
    # 再次无覆盖编译必须回到原文结果
    assert compile_plan(baseline=None, plan=plan).fingerprints["plan_digest"] == clean.fingerprints["plan_digest"]


def test_no_strict_override_still_checks_cross_field():
    code = _err_code(lambda: compile_plan(
        baseline=None, plan=load_plan(PLAN), strict=False,
        overrides={"caliber.net.maintenance_margin": 0.9},
        allow_constitutive_override=True,
    ))
    assert code in {"E1004", "E1005", "E1013"}, code


# ---------------------------------------------------------------- ledger
def test_ledger_dedup_across_instances(tmp_path):
    L = Layout(tmp_path)
    a, b = Ledger(L), Ledger(L)
    rec = {"event_id": "once-1", "kind": "events", "ticket_id": "T", "recorded_at": "x"}
    a.append("events", rec)
    assert _err_code(lambda: b.append("events", rec)) == "E3006"
    assert len(Ledger(L).records("events")) == 1


def test_ledger_records_survive_torn_line(tmp_path):
    L = Layout(tmp_path)
    path = L.register("events")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event_id":"ok"}\n{"event_id":"broken"\n', encoding="utf-8")
    assert [r["event_id"] for r in Ledger(L).records("events")] == ["ok"]
    idx = IndexDB(L)
    idx.reconcile(Ledger(L))
    assert idx.assert_consistent(Ledger(L))["events"]["ok"] is True


# ---------------------------------------------------------------- index
def test_index_consistency_checks_objects_table(tmp_path):
    L = Layout(tmp_path)
    store = ObjectStore(L)
    ref = store.write_json("reports", {"a": 1})
    idx = IndexDB(L)
    idx.reconcile(Ledger(L))
    assert idx.assert_consistent(Ledger(L))["objects"]["ok"] is True
    # 篡改索引内容（行数不变）必须被发现
    with idx._connect() as con:
        con.execute("UPDATE objects SET digest='sha256:tampered'")
    assert _err_code(lambda: idx.assert_consistent(Ledger(L))) == E5004
    assert ref.schema_version == 1
    idx.reconcile(Ledger(L))
    assert idx.query("SELECT schema_version FROM objects")[0]["schema_version"] == 1


# ---------------------------------------------------------------- ndjson
def test_ndjson_always_emits_envelope_line():
    env = {"ok": True, "command": "stations", "run_id": "R", "exit_code": 0,
           "warnings": [], "data": {"stations": ["a", "b"], "count": 2}}
    lines = render_ndjson(env).splitlines()
    assert json.loads(lines[0]) == env
    assert json.loads(lines[1])["_kind"] == "stations"


# ---------------------------------------------------------------- log
def test_tolist_secret_is_rejected_before_serialization(tmp_path):
    class Carrier:
        def tolist(self):
            return [{"api_key": "sk-TO-LIST"}]

    sink = JsonlLogSink(tmp_path / "x.jsonl")
    logger = build_logger("ticket", {"ticket_id": "T", "seq": 0}, sink=sink)
    assert _err_code(lambda: logger.event("info", "e", "m", payload=Carrier())) == E2004
    assert not sink.path.exists()


def test_sink_rejects_write_after_close(tmp_path):
    sink = JsonlLogSink(tmp_path / "x.jsonl")
    sink.close()
    assert _err_code(lambda: sink.write({"level": "info", "event": "e"})) == E2004


# ---------------------------------------------------------------- facilities
def test_strict_rejects_wrong_type_facility():
    assert _err_code(lambda: build_facilities(
        scope="ticket", ticket_id="T", master_seed=1,
        overrides={"digest": object()},
    )) == E2003


def test_log_keys_cannot_override_ticket_id(tmp_path):
    fx = build_facilities(
        scope="ticket", ticket_id="REAL", master_seed=1,
        log_keys={"ticket_id": "FAKE", "seq": 999},
    )
    assert fx.log.keys["ticket_id"] == "REAL"
    assert fx.log.keys["seq"] == 0


# ---------------------------------------------------------------- regex
def test_ticket_id_rejects_trailing_newline(tmp_path):
    from v5.runtime.store import StoreContext
    ctx = StoreContext.from_root(tmp_path)
    for bad in ("ok\n", "a/b", "..", ""):
        assert _err_code(lambda b=bad: ctx.for_ticket(b)) == E5004


def test_supply_path_segment_rejects_trailing_newline(tmp_path):
    """外部输入（数据集/交易所/市场/标的）必须是单路径段。

    `$` 在 Python 正则里允许末尾一个 `\\n`：用 match+`$` 会让 "ohlcv\\n" 通过，
    随后透出裸 OSError 而不是可诊断的错误码。
    """
    from v5.upstream import store

    for bad in ("ohlcv\n", "../ohlcv", "ohlcv/x"):
        assert _err_code(lambda b=bad: store.dataset_file(
            tmp_path, b, "binanceusdm", "futures", "BTC/USDT:USDT", "1d")) == "E1004"
    for bad in ("futures\n", "..", "spot/futures"):
        assert _err_code(lambda b=bad: store.dataset_file(
            tmp_path, "ohlcv", "binanceusdm", b, "BTC/USDT:USDT", "1d")) == "E1004"
    # 标的也是外部输入：不能靠文件名逃出数据根
    assert _err_code(lambda: store.dataset_file(
        tmp_path, "ohlcv", "binanceusdm", "futures", "../../evil", "1d")) == "E1004"
