"""因子库 / 晋升 / 巡检 / 体检 / 估算 / 回收：命令层行为。

这些命令的共同点是「读盘后给结论」，所以测试用**手工构造的 ticket 目录**，
不跑真实执行：一次全链路执行要十几秒，而这里要验证的是
「pass 才入库」「重复执行不写第二行」「缺 --yes 拒绝」这类边界。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from v5.app.cli import main
from v5.storage.catalog import Catalog, read_ticket
from v5.storage.gc import apply_gc, plan_gc
from v5.storage.layout import Layout

TID = "20260101T000000Z-abcdef12"


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _ticket(root, *, decision="pass", grade=None):
    """最小可晋升 ticket：Search 给表达式，Evidence 给指标，Verdict 给裁决。"""
    cid = "sha256:" + "a" * 64
    base = root / "runs" / TID
    _write(base / "parcels" / "04-Search-deadbeef" / "payload.json", {
        "candidates": [{
            "candidate_id": cid,
            "generator": "gp_symbolic@1",
            "generators": ["gp_symbolic@1"],
            "payload": {"expression": {"op": "div", "args": [
                {"field": "close"}, {"const": 2}]}},
            "required_fields": ["close"],
            "operators": ["div"],
        }],
        "count": 1, "n_trials": 3, "generators": ["gp_symbolic@1"],
    })
    _write(base / "parcels" / "05-Evidence-11111111" / "payload.json", {
        "per_candidate": [{"candidate_id": cid, "generator": "gp_symbolic@1",
                           "metrics": {"ic": 0.03, "cost_adj_return": 0.01}}],
    })
    _write(base / "parcels" / "06-Verdict-22222222" / "payload.json", {
        "n_trials_used": 3,
        "per_candidate": [{"candidate_id": cid, "decision": decision,
                           "reason_codes": [] if decision == "pass" else ["dsr_fail"],
                           "statistics": {"dsr": 0.97, "hac_t": 3.1, "n_trials": 3,
                                          "family": "f1"}}],
    })
    _write(base / "frozen" / "plan.json", {"plan": "p1"})
    if grade:
        _write(base / "parcels" / "08-Certify-33333333" / "payload.json",
               {"evidence_grade": grade})
    return cid


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------- 巡检
def test_inspect_summarises_each_station(root):
    _ticket(root)
    assert main(["inspect", "--run-id", TID]) == 0


def test_inspect_unknown_station_is_e1008(root):
    _ticket(root)
    from v5.app.cli.inspect import cmd_inspect
    from v5.contracts.errors import FlowError
    with pytest.raises(FlowError) as exc:
        cmd_inspect(type("A", (), {"run_id": TID, "station": "Portfolio",
                                   "full": False})())
    assert exc.value.error.code == "E1008"


def test_read_ticket_rejects_missing_run(root):
    from v5.contracts.errors import FlowError
    with pytest.raises(FlowError) as exc:
        read_ticket(Layout(root), "20990101T000000Z-00000000")
    assert exc.value.error.code == "E1008"


# --------------------------------------------------------------- 晋升
def test_promote_requires_yes(root):
    _ticket(root)
    from v5.contracts.errors import FlowError
    with pytest.raises(FlowError) as exc:
        from v5.app.cli.promote import cmd_promote
        cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                                   "min_grade": "any", "yes": False})())
    assert exc.value.error.code == "E1004"


def test_promote_writes_catalog_and_is_idempotent(root):
    cid = _ticket(root)
    from v5.app.cli.promote import cmd_promote
    args = type("A", (), {"run_id": TID, "candidate": [], "min_grade": "any",
                          "yes": True})()
    first = cmd_promote(args)
    assert first["data"]["counts"]["promoted"] == 1
    assert first["data"]["catalog_total"] == 1

    second = cmd_promote(args)
    assert second["data"]["counts"]["promoted"] == 0
    assert second["data"]["counts"]["already"] == 1
    # 库里仍只有一条（不是两行）
    assert Catalog(Layout(root)).count() == 1
    assert Catalog(Layout(root)).get(cid)["expression"]["op"] == "div"


def test_promote_skips_rejected_candidate(root):
    _ticket(root, decision="reject")
    from v5.app.cli.promote import cmd_promote
    out = cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                                     "min_grade": "any", "yes": True})())
    assert out["data"]["counts"]["promoted"] == 0
    assert out["data"]["skipped"][0]["reason"] == "decision:reject"


def test_promote_gate_on_out_of_sample(root):
    _ticket(root)  # 没有 Certify → grade = in_sample
    from v5.app.cli.promote import cmd_promote
    out = cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                                     "min_grade": "out_of_sample", "yes": True})())
    assert out["data"]["reason"] == "not_out_of_sample"
    assert Catalog(Layout(root)).count() == 0


def test_promote_records_grade_from_certify(root):
    _ticket(root, grade="out_of_sample")
    from v5.app.cli.promote import cmd_promote
    cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                               "min_grade": "out_of_sample", "yes": True})())
    rows = Catalog(Layout(root)).entries()
    assert rows[0]["evidence_grade"] == "out_of_sample"


def test_promote_unknown_candidate_is_e1008(root):
    _ticket(root)
    from v5.app.cli.promote import cmd_promote
    from v5.contracts.errors import FlowError
    with pytest.raises(FlowError) as exc:
        cmd_promote(type("A", (), {"run_id": TID,
                                   "candidate": ["sha256:" + "b" * 64],
                                   "min_grade": "any", "yes": True})())
    assert exc.value.error.code == "E1008"


# --------------------------------------------------------------- 查库
def test_factors_lists_promoted_entry(root):
    _ticket(root)
    from v5.app.cli.promote import cmd_promote
    cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                               "min_grade": "any", "yes": True})())
    from v5.app.cli.factors import cmd_factors
    out = cmd_factors(type("A", (), {"status": "active", "limit": None})())
    assert out["data"]["count"] == 1
    row = out["data"]["factors"][0]
    assert row["expression"]["op"] == "div"
    assert row["statistics"]["dsr"] == 0.97


def test_factors_empty_catalog_is_ok(root):
    from v5.app.cli.factors import cmd_factors
    out = cmd_factors(type("A", (), {"status": "active", "limit": None})())
    assert out["ok"] and out["data"]["count"] == 0


def test_catalog_retire_keeps_history(root):
    cid = _ticket(root)
    from v5.app.cli.promote import cmd_promote
    cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                               "min_grade": "any", "yes": True})())
    cat = Catalog(Layout(root))
    cat.retire(cid, at="2026-01-02T00:00:00Z", note="decayed")
    assert cat.get(cid)["status"] == "retired"
    assert cat.list(status="active") == []
    # 历史行仍在文件里（append-only）
    assert len(cat.path.read_text(encoding="utf-8").strip().splitlines()) == 2


# --------------------------------------------------------------- 体检
def _supply(root, *, symbol="BTC/USDT:USDT", rows=40, gap=False):
    from v5.upstream import store
    ts = pd.date_range("2024-01-01", periods=rows, freq="D", tz="UTC")
    if gap:
        ts = ts.delete(10)
    df = pd.DataFrame({
        store.SYMBOL_COL: symbol,
        store.TIMESTAMP_COL: ts,
        "open": np.arange(rows if not gap else rows - 1, dtype="float64") + 100,
        "high": np.arange(rows if not gap else rows - 1, dtype="float64") + 101,
        "low": np.arange(rows if not gap else rows - 1, dtype="float64") + 99,
        "close": np.arange(rows if not gap else rows - 1, dtype="float64") + 100,
        "volume": np.ones(rows if not gap else rows - 1),
        "vwap": np.arange(rows if not gap else rows - 1, dtype="float64") + 100,
    })
    store.upsert(store.dataset_file(root, "ohlcv", "binanceusdm", "futures",
                                    "BTC_USDT_USDT", "1d"),
                 df, "ohlcv", symbol=symbol)


def test_doctor_reports_clean_supply(root):
    _supply(root)
    from v5.app.cli.doctor import cmd_doctor
    out = cmd_doctor(type("A", (), {"dataset": None, "plan": None})())
    check = out["data"]["checks"][0]
    assert check["dataset"] == "ohlcv" and check["market"] == "futures"
    assert check["passed"] is True
    assert check["rows"] == 40


def test_doctor_plan_symbols_compared_as_file_stems(root):
    """计划的标的集写 `BTC/USDT:USDT`，文件名写 `BTC_USDT_USDT-1d.parquet`。

    两套写法直接相减，会让「数据一个不缺」也报成全部缺失——首次体检就报假
    警报。这里同时钉住两件事：齐全时不报缺；真缺时按权威符号报（人读这个）。
    """
    _supply(root)
    layout = Layout(root)
    layout.schemas.mkdir(parents=True, exist_ok=True)
    layout.symbol_sets.write_text(
        json.dumps({"one": ["BTC/USDT:USDT", "ETH/USDT:USDT"]}), encoding="utf-8")
    plan_path = root / "plan.json"
    plan_path.write_text(
        json.dumps({"supply": {"datasets": ["ohlcv"], "symbols": "one"}}),
        encoding="utf-8")

    from v5.app.cli.doctor import cmd_doctor
    out = cmd_doctor(type("A", (), {"dataset": None, "plan": str(plan_path)})())

    check = out["data"]["plan"]
    # 只有 BTC 有文件；缺的是 ETH，且必须还原成权威符号而不是文件 stem。
    assert check["missing_symbols"] == ["ETH/USDT:USDT"]
    assert check["missing_symbol_count"] == 1


def test_doctor_plan_symbols_all_present_passes(root):
    """齐全即通过：`summary.passed` 不再被假缺失拉成 False。"""
    _supply(root)
    layout = Layout(root)
    layout.schemas.mkdir(parents=True, exist_ok=True)
    layout.symbol_sets.write_text(
        json.dumps({"one": ["BTC/USDT:USDT"]}), encoding="utf-8")
    plan_path = root / "plan.json"
    plan_path.write_text(
        json.dumps({"supply": {"datasets": ["ohlcv"], "symbols": "one"}}),
        encoding="utf-8")

    from v5.app.cli.doctor import cmd_doctor
    out = cmd_doctor(type("A", (), {"dataset": None, "plan": str(plan_path)})())

    assert out["data"]["plan"]["missing_symbol_count"] == 0
    assert out["data"]["summary"]["passed"] is True
    assert out["warnings"] == []


def test_doctor_surfaces_gap(root):
    _supply(root, gap=True)
    from v5.app.cli.doctor import cmd_doctor
    out = cmd_doctor(type("A", (), {"dataset": None, "plan": None})())
    check = out["data"]["checks"][0]
    assert check["gap_count"] >= 1
    assert check["passed"] is False
    assert out["data"]["summary"]["passed"] is False


def test_doctor_without_supply_is_e1008(root):
    from v5.app.cli.doctor import cmd_doctor
    from v5.contracts.errors import FlowError
    with pytest.raises(FlowError) as exc:
        cmd_doctor(type("A", (), {"dataset": None, "plan": None})())
    assert exc.value.error.code == "E1008"


# --------------------------------------------------------------- 估算
def _plan(root, tmp_path, cost_tiers=(10, 25, 60), slippage="none", rebalance=3,
          holdout="reserved"):
    plan = {
        "plan": "p", "baseline": "v1",
        "supply": {"exchange": "binanceusdm", "datasets": ["ohlcv"],
                   "symbols": "s", "frequency": "1d"},
        "caliber": {
            "execution": {"entry": "next_close", "rebalance": rebalance,
                          "annualisation": "calendar_365",
                          "funding": "per_contract_events"},
            "label": {"horizon": 5, "transform": "cs_rank",
                      "overlap_policy": "nw_adjusted"},
            "allocate": {"quantiles": 5, "long_short": True,
                         "weighting": "equal", "max_weight": 0.1},
            "net": {"cost_tiers": list(cost_tiers), "slippage": slippage,
                    "fee_model": "taker", "max_leverage": 1.0},
        },
        "boundary": {"impl": "pit_dynamic_topk",
                     "time_axis": {"mine": "2024-01-01..2024-06-30",
                                   "oos": "2024-07-01..2024-08-31",
                                   "holdout": holdout}},
        "panel": {"impl": "point_in_time_align", "params": {}},
        "target": {"impl": "forward_return_cs"},
        "search": {"algorithms": [{"name": "gp_symbolic", "params": {}}],
                   "prior_pool": [], "trial_family": "f", "seed": 1},
        "evidence": {"impl": "two_stage_funnel", "params": {}},
        "verdict": {"impl": "sentinel", "params": {"dsr_min": 0.95}},
        "portfolio": {"combiner": "equal_weight",
                      "window": {"fit": "2024-02-01..2024-05-20", "embargo": 3,
                                 "eval": "2024-06-01..2024-06-30"}},
        "certify": {"impl": "walk_forward", "params": {"folds": 2, "embargo": 3}},
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return str(path)


def test_estimate_cost_scales_with_turnover(root, tmp_path):
    from v5.app.cli.estimate import cmd_estimate
    plan = _plan(root, tmp_path)
    a = cmd_estimate(type("A", (), {"plan": plan, "turnover": 1.0})())
    b = cmd_estimate(type("A", (), {"plan": plan, "turnover": 0.5})())
    ta = a["data"]["estimate"]["tiers"]
    tb = b["data"]["estimate"]["tiers"]
    assert len(ta) == 3
    # 换手减半 → 每期成本减半（同口径下唯一的关系）
    assert abs(tb[0]["cost_per_period"] * 2 - ta[0]["cost_per_period"]) < 1e-12
    # 每期成本 = turnover × (fee + slip)，10bp 档 fee=1e-3
    assert abs(ta[0]["cost_per_period"] - 0.001) < 1e-12


def test_estimate_tiered_slippage_halves_bp(root, tmp_path):
    from v5.app.cli.estimate import cmd_estimate
    plan = _plan(root, tmp_path, slippage="tiered")
    out = cmd_estimate(type("A", (), {"plan": plan, "turnover": 1.0})())
    row = out["data"]["estimate"]["tiers"][0]
    # tiered: fee=10bp=1e-3, slip=10/2bp=5e-4 → 1.5e-3
    assert abs(row["cost_per_period"] - 1.5e-3) < 1e-12


# --------------------------------------------------------------- 回收
def test_gc_keeps_referenced_and_flags_orphans(root):
    layout = Layout(root)
    kind_dir = layout.fact_kind("parcel_data")
    kind_dir.mkdir(parents=True, exist_ok=True)
    live = "a" * 64
    dead = "b" * 64
    (kind_dir / f"{live}.parquet").write_bytes(b"x" * 10)
    (kind_dir / f"{dead}.parquet").write_bytes(b"y" * 10)
    # 只有 live 被引用
    ref = root / "db" / "trials.jsonl"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(json.dumps({"event_id": f"sha256:{live}"}) + "\n", encoding="utf-8")
    # 登记库是 append-only 真相：GC 前后字节必须一致（AC53 / I51）
    ledger = root / "db" / "ledger.jsonl"
    ledger_before = json.dumps({"kind": "x", "n": 1}) + "\n"
    ledger.write_text(ledger_before, encoding="utf-8")

    plan = plan_gc(layout)
    assert plan["objects"] == 2 and plan["unreferenced"] == 1
    assert plan["candidates"][0]["digest"] == f"sha256:{dead}"

    out = apply_gc(layout)
    assert len(out["removed"]) == 1
    assert not (kind_dir / f"{dead}.parquet").exists()
    assert (kind_dir / f"{live}.parquet").exists()
    # AC53：dry-run 计划与实际删除集合一致；登记库字节数不变
    assert [r["digest"] for r in out["removed"]] == \
        [c["digest"] for c in plan["candidates"]]
    assert ledger.read_text(encoding="utf-8") == ledger_before


# --------------------------------------------------------------- 保留区揭示






# --------------------------------------------------------------- 库管理




# --------------------------------------------------------------- 命令面完整性


# --------------------------------------------------------------- 保留区揭示
def test_holdout_reveals_once(root, tmp_path):
    from v5.app.cli.holdout import cmd_holdout
    plan = _plan(root, tmp_path, holdout="2024-09-01..2024-09-30")
    args = type("A", (), {"plan": plan, "set": [], "no_strict": True,
                          "allow_constitutive_override": False, "yes": True})()
    first = cmd_holdout(args)
    assert first["data"]["holdout"]["first_time"] is True
    assert first["data"]["holdout"]["span"] == "2024-09-01..2024-09-30"

    second = cmd_holdout(args)
    assert second["data"]["holdout"]["first_time"] is False
    assert second["data"]["holdout"]["revealed_value"] == "2024-09-01..2024-09-30"
    # 账本里只写了一行
    events = root / "db" / "events.jsonl"
    assert len(events.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_holdout_reserved_is_rejected(root, tmp_path):
    from v5.app.cli.holdout import cmd_holdout
    from v5.contracts.errors import FlowError
    plan = _plan(root, tmp_path)
    with pytest.raises(FlowError) as exc:
        cmd_holdout(type("A", (), {"plan": plan, "set": [], "no_strict": True,
                                   "allow_constitutive_override": False,
                                   "yes": True})())
    assert exc.value.error.code == "E1004"


def test_holdout_requires_yes(root, tmp_path):
    from v5.app.cli.holdout import cmd_holdout
    from v5.contracts.errors import FlowError
    plan = _plan(root, tmp_path, holdout="2024-09-01..2024-09-30")
    with pytest.raises(FlowError) as exc:
        cmd_holdout(type("A", (), {"plan": plan, "set": [], "no_strict": True,
                                   "allow_constitutive_override": False,
                                   "yes": False})())
    assert exc.value.error.code == "E1004"


# --------------------------------------------------------------- 库管理
def test_factor_db_retire_requires_yes_and_marks_status(root):
    _ticket(root)
    from v5.app.cli.factor_db import cmd_factor_db
    from v5.app.cli.promote import cmd_promote
    from v5.contracts.errors import FlowError
    cid = "sha256:" + "a" * 64
    cmd_promote(type("A", (), {"run_id": TID, "candidate": [],
                               "min_grade": "any", "yes": True})())

    with pytest.raises(FlowError) as exc:
        cmd_factor_db(type("A", (), {"retire": cid, "note": "x", "yes": False})())
    assert exc.value.error.code == "E1004"

    out = cmd_factor_db(type("A", (), {"retire": cid, "note": "decayed",
                                       "yes": True})())
    assert out["data"]["status"] == "retired"
    assert Catalog(Layout(root)).get(cid)["status"] == "retired"


def test_factor_db_without_action_is_read_only(root):
    from v5.app.cli.factor_db import cmd_factor_db
    out = cmd_factor_db(type("A", (), {"retire": None, "note": "", "yes": False})())
    assert out["ok"] and out["data"]["entries"] == 0


# --------------------------------------------------------------- 命令面完整性
def test_every_declared_command_has_an_entry_point(root):
    """命令面清单与分派表必须同集合。

    S7 之前 evaluate/backtest/certify 卡在对象重建（S5-M2）上，只能给一条
    「指路式拒绝」。重建做完后三条已接通；这条守卫改成**正向**断言：声明了
    就必须有入口，避免再出现「文档里有、实现里没有」的静默缺口。
    """
    from v5.app.cli import _DISPATCH
    from v5.app.contract import ALL_COMMANDS

    assert set(ALL_COMMANDS) == set(_DISPATCH)


# --------------------------------------------------------------- stale 判定
def test_supply_staleness_detects_checksum_change(root):
    from v5.app.cli.station import _supply_staleness
    from v5.upstream import store
    _supply(root)
    ds = store.read_dataset(root, "ohlcv", "binanceusdm", "futures", timeframe="1d")
    plan = {"supply": {"exchange": "binanceusdm", "market": "futures",
                       "frequency": "1d", "datasets": ["ohlcv"]}}

    fresh = _supply_staleness(plan, {"supply_pins": {"ohlcv": {
        "checksum": ds.checksum}}}, root)
    assert fresh["checked"] and not fresh["stale"]

    stale = _supply_staleness(plan, {"supply_pins": {"ohlcv": {
        "checksum": "sha256:" + "0" * 64}}}, root)
    assert stale["stale"]
    assert stale["changed"][0]["reason"] == "checksum_changed"


def test_supply_staleness_legacy_ticket_is_not_checked(root):
    from v5.app.cli.station import _supply_staleness
    out = _supply_staleness({"supply": {}}, {}, root)
    assert out["checked"] is False
    assert out["reason"] == "legacy_ticket"
    assert out["stale"] is False


def test_supply_staleness_missing_dataset_is_unreadable(root):
    from v5.app.cli.station import _supply_staleness
    plan = {"supply": {"exchange": "binanceusdm", "market": "futures",
                       "frequency": "1d"}}
    out = _supply_staleness(plan, {"supply_pins": {"ohlcv": {"checksum": "x"}}}, root)
    assert out["stale"]
    assert out["changed"][0]["reason"] == "unreadable"
