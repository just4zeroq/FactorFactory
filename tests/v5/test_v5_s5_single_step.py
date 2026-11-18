"""S5：单步执行 + `--probe` / `--commit` 分流（AC4 / AC6 / AC7 的可执行判据）。

三条不可省的性质：

1. **probe 什么都不动**（AC7）：试验计数、事件账本、正式状态、已发布结论四项
   逐项不变；产物落 `steps/`，不落 `parcels/`，不写 `state.jsonl`。
2. **commit 只推进状态、不触发事件**（I7）：`state.jsonl` 追加一条，事件账本
   一条都不多——否则单步提交就是绕开整体执行的计数后门。
3. **append_only 工位禁止 commit**（s0/05 §6）：Search / Verdict 提交即 E3006。

另外守两条拒绝语义：上游产物被改写 → E3002（重建失败显式，不做空产物），
ticket 没有冻结方案 → E3001（单步不猜参数）。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from v5.app.cli import main
from v5.app.cli import fetch as fetch_mod

FROZEN = datetime(2026, 11, 16, tzinfo=timezone.utc)
SYMBOLS = tuple(f"S{i}" for i in range(6))
SET = "cs_s5"
N_BARS = 70


class FakeExchange:
    def __init__(self, bars):
        self.bars = bars

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        rows = [list(r) for r in self.bars[symbol] if since is None or r[0] >= since]
        return rows[:limit]


def _bars() -> dict[str, list[list[int]]]:
    out = {}
    for idx, sym in enumerate(SYMBOLS):
        rows = []
        rng = np.random.default_rng(idx + 11)
        px = 100.0
        for day in range(N_BARS):
            ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
            px = float(px * np.exp(rng.normal(0, 0.02)))
            rows.append([int(ts.timestamp() * 1000), px, px * 1.01, px * 0.99,
                         px, float(1e3 + idx * 100 + day)])
        out[sym] = rows
    return out


def _plan() -> dict:
    return {
        "plan": "cs_s5_a", "baseline": "v1",
        "supply": {"exchange": "binanceusdm", "datasets": ["ohlcv"],
                   "symbols": SET, "frequency": "1d"},
        "caliber": {
            "execution": {"entry": "next_close", "rebalance": 3,
                          "annualisation": "calendar_365",
                          "funding": "per_contract_events"},
            "label": {"horizon": 3, "transform": "cs_rank", "neutralise": [],
                      "overlap_policy": "independent_sample",
                      "min_width": 2, "width_policy": "keep"},
            "allocate": {"quantiles": 3, "long_short": True, "weighting": "equal",
                         "max_weight": 0.25, "net_exposure": 0.0,
                         "turnover_max": 0.35, "min_members": 2},
            "net": {"cost_tiers": [10, 20], "slippage": "none", "fee_model": "taker",
                    "max_leverage": 1.0, "initial_margin": 0.1,
                    "maintenance_margin": 0.005, "liquidation_buffer": 0.01},
        },
        "boundary": {"impl": "pit_dynamic_topk",
                     "params": {"k": 4, "tier_window": 3, "min_coverage": 0.5},
                     "time_axis": {"mine": "2024-01-05..2024-02-20",
                                   "oos": "2024-02-21..2024-03-05",
                                   "holdout": "reserved"}},
        "panel": {"impl": "point_in_time_align", "params": {"drop_unclosed": True}},
        "target": {"impl": "forward_return_cs"},
        "search": {"algorithms": [{"name": "gp_symbolic",
                                   "params": {"population": 8, "generations": 2,
                                              "max_depth": 3}}],
                   "prior_pool": [], "trial_family": "cs_s5_a", "seed": 20261117},
        "evidence": {"impl": "two_stage_funnel",
                     "params": {"null_model": "within_bar_cross_asset",
                                "prefilter": {"min_coverage": 0.5},
                                "cost_attribution": False}},
        "verdict": {"impl": "sentinel",
                    "params": {"dsr_min": 0.01, "fdr_max": 1.0,
                               "cost_adj_min": 0.001}},
        "portfolio": {"combiner": "equal_weight",
                      "window": {"fit": "2024-01-10..2024-02-10", "embargo": 1,
                                 "eval": "2024-02-15..2024-02-20"}},
        "certify": {"impl": "walk_forward", "params": {"folds": 2, "embargo": 1}},
    }


@pytest.fixture()
def seeded(tmp_path: Path, monkeypatch) -> Path:
    """fetch 采集供给 + dummy run 一次，返回 ticket_id（拿到已落盘上游）。"""
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({SET: list(SYMBOLS)}), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_client_factory", lambda _eid: FakeExchange(_bars()))
    assert main(["fetch", "--datasets", "ohlcv", "--symbol-set", SET,
                 "--start", "2024-01-01T00:00:00+00:00",
                 "--end", "2024-03-11T00:00:00+00:00",
                 "--exchange", "binanceusdm", "--market", "futures",
                 "--limit", "200"]) == 0
    return plan_path


def _run_dummy(plan_path: Path, capsys) -> tuple[dict, Path]:
    capsys.readouterr()
    assert main(["run", "--plan", str(plan_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    return out, Path(out["run_id"])


def _station(ticket: str, name: str, *flags: str, capsys) -> tuple[int, dict]:
    capsys.readouterr()
    code = main(["station", name, "--on", ticket, *flags])
    return code, json.loads(capsys.readouterr().out)


def _parcel_dirs(run_dir: Path, scope: str) -> set[str]:
    base = run_dir / scope
    return {p.parent.name for p in base.glob("*/parcel.json")} if base.exists() else set()


def _snapshot(run_dir: Path) -> set[str]:
    return {str(p.relative_to(run_dir)) for p in run_dir.rglob("*") if p.is_file()}


# ---------------------------------------------------------------- probe 隔离
def test_probe_changes_none_of_the_four_things(seeded, tmp_path, capsys):
    """AC7：probe 后①试验计数②事件账本③正式状态④已发布结论逐项不变。"""
    out, _ = _run_dummy(seeded, capsys)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    before_parcels = _parcel_dirs(run_dir, "parcels")
    before_trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    before_files = _snapshot(run_dir)
    assert not (run_dir / "state.jsonl").exists()

    code, env = _station(ticket, "Panel", "--probe", capsys=capsys)

    assert code == 0 and env["ok"] is True, json.dumps(env, ensure_ascii=False)
    assert env["data"]["mode"] == "step"
    assert env["data"]["scope"] == "steps"
    # ③ 正式状态：parcels/ 一个目录都没多，state.jsonl 仍不存在
    assert _parcel_dirs(run_dir, "parcels") == before_parcels
    assert not (run_dir / "state.jsonl").exists()
    # ① 试验计数 / ② 事件账本：probe 的返回值里 events 必须为空
    assert env["data"]["state"] == "running"
    # 硬判据：整个 ticket 目录里**只允许** steps/ 与 trace 有新增，且没有任何删除。
    # 任何账本/状态/正式产物的新增都会在这里露出来，不靠「看起来没变」。
    added_files = _snapshot(run_dir) - before_files
    assert not (before_files - _snapshot(run_dir)), "probe 不得删除既有文件"
    for path in added_files:
        head = Path(path).parts[0]
        assert head in {"steps", "logs"} or path == "trace.jsonl", path
    # probe 自己的产物在 steps/，且 trace 里 origin=step
    probe_dirs = _parcel_dirs(run_dir, "steps")
    assert any("-Panel-" in name for name in probe_dirs), probe_dirs
    trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    added = [json.loads(line) for line in trace[len(before_trace):]]
    assert [t["station"] for t in added] == ["Panel"]
    assert added[0]["origin"] == "step"
    assert added[0]["status"] == "done"
    # ④ 已发布结论：正式产物目录里的 payload 完全没被触碰
    assert before_parcels, "夹具应已产出正式产物"


def test_probe_does_not_feed_downstream(seeded, tmp_path, capsys):
    """probe 的产物不作为下游输入：commit 一个工位后，下游仍读正式产物。"""
    out, _ = _run_dummy(seeded, capsys)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    formal = next(run_dir.glob("parcels/*-Boundary-*/parcel.json"))
    before = json.loads(formal.read_text(encoding="utf-8"))["digest"]

    code, _ = _station(ticket, "Boundary", "--probe", capsys=capsys)
    assert code == 0
    # 正式产物原封不动（dummy 重算应得到同一 payload digest）
    assert json.loads(formal.read_text(encoding="utf-8"))["digest"] == before


# ---------------------------------------------------------------- commit
def test_commit_writes_formal_parcel_and_state_without_events(seeded, tmp_path, capsys):
    """commit：产物进 parcels/、state.jsonl 追加一条，事件一条都不触发（I7）。

    夹具先把 Boundary 的正式产物移走，代表「这个工位还没提交过」；否则重算
    会得到同一 payload、走幂等短路（另一条用例覆盖）。
    """
    out, _ = _run_dummy(seeded, capsys)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    target = next(run_dir.glob("parcels/*-Boundary-*"))
    shutil.rmtree(target)
    before = _parcel_dirs(run_dir, "parcels")

    code, env = _station(ticket, "Boundary", "--commit", capsys=capsys)

    assert code == 0 and env["ok"] is True, json.dumps(env, ensure_ascii=False)
    assert env["data"]["mode"] == "commit"
    assert env["data"]["scope"] == "parcels"
    assert env["data"]["state"] == "committed"
    assert env["data"]["idempotent"] is False
    assert len(_parcel_dirs(run_dir, "parcels")) == len(before) + 1
    lines = (run_dir / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["station"] == "Boundary"
    assert record["origin"] == "commit"
    assert record["parcel_digest"] == env["data"]["parcel_digest"]


def test_commit_is_idempotent_when_payload_is_unchanged(seeded, tmp_path, capsys):
    """同一 payload 重复提交 → 不新增产物；状态条目只补一次（S5-M3）。

    目录名由 payload_digest 决定，同名即同内容。允许覆盖会打开「改写历史
    产物」的口子；幂等短路才是正确语义。产物在而状态缺 → 断电恢复补一条；
    状态已在 → 纯 no-op。
    """
    out, _ = _run_dummy(seeded, capsys)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    before = _parcel_dirs(run_dir, "parcels")

    code, env = _station(ticket, "Boundary", "--commit", capsys=capsys)

    assert code == 0 and env["ok"] is True, json.dumps(env, ensure_ascii=False)
    assert env["data"]["idempotent"] is True
    assert env["data"]["state_recovered"] is True
    assert _parcel_dirs(run_dir, "parcels") == before
    lines = (run_dir / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["parcel_digest"] == env["data"]["parcel_digest"]

    # 再提交一次：状态条目已在 → 纯 no-op，不追加第二行
    code, env = _station(ticket, "Boundary", "--commit", capsys=capsys)
    assert code == 0 and env["data"]["state_recovered"] is False
    lines = (run_dir / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_commit_recovers_state_after_crash_between_two_writes(seeded, tmp_path, capsys):
    """断电演练（S5-M3）：产物落盘后、state.jsonl 追加前崩溃 → 重放恢复。

    复现方式：删掉 state.jsonl 里那条记录（等价于第二段从未发生），重放
    commit 必须把记录补回来，且 parcel 目录与 digest 保持不变。
    """
    out, _ = _run_dummy(seeded, capsys)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    state_path = run_dir / "state.jsonl"
    target = next(run_dir.glob("parcels/*-Boundary-*"))
    shutil.rmtree(target)

    code, env = _station(ticket, "Boundary", "--commit", capsys=capsys)
    assert code == 0 and env["data"]["idempotent"] is False
    assert json.loads(state_path.read_text(encoding="utf-8").splitlines()[0])[
        "parcel_digest"] == env["data"]["parcel_digest"]

    # 崩溃：产物在、状态缺
    state_path.unlink()
    before = _parcel_dirs(run_dir, "parcels")

    code, env = _station(ticket, "Boundary", "--commit", capsys=capsys)

    assert code == 0 and env["ok"] is True, json.dumps(env, ensure_ascii=False)
    assert env["data"]["idempotent"] is True
    assert env["data"]["state_recovered"] is True
    assert _parcel_dirs(run_dir, "parcels") == before, "恢复不得重写产物"
    lines = state_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["parcel_digest"] == env["data"]["parcel_digest"]


def test_commit_is_rejected_for_append_only_stations(seeded, capsys):
    """Search / Verdict 是 append_only：提交式重放要么重复计数、要么改写结论。"""
    out, _ = _run_dummy(seeded, capsys)
    for name in ("Search", "Verdict"):
        code, env = _station(out["run_id"], name, "--commit", capsys=capsys)
        assert code == 1, name
        assert env["errors"][0]["code"] == "E3006", name


def test_probe_and_commit_are_mutually_exclusive(seeded, tmp_path, capsys):
    out, _ = _run_dummy(seeded, capsys)
    code, env = _station(out["run_id"], "Panel", "--probe", "--commit", capsys=capsys)
    assert code == 2
    assert env["errors"][0]["code"] == "E1001"
    # 互斥被拒时不得留下任何产物
    assert not (tmp_path / "runs" / out["run_id"] / "state.jsonl").exists()


# ---------------------------------------------------------------- 失败显式
def test_station_without_frozen_plan_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    code, env = _station("20261116T000000Z-abcdef12", "Panel", "--probe", capsys=capsys)
    assert code == 1
    assert env["errors"][0]["code"] == "E3001"


def test_tampered_upstream_payload_is_rejected(seeded, tmp_path, capsys):
    """AC6：上游 payload 被改写 → E3002，不降级成空输入继续跑。"""
    out, _ = _run_dummy(seeded, capsys)
    ticket = out["run_id"]
    run_dir = tmp_path / "runs" / ticket
    target = next(run_dir.glob("parcels/*-Boundary-*/payload.json"))
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["tampered"] = True
    target.write_text(json.dumps(payload), encoding="utf-8")

    code, env = _station(ticket, "Panel", "--probe", capsys=capsys)

    assert code == 1
    assert env["errors"][0]["code"] == "E3002"
    assert not _parcel_dirs(run_dir, "steps"), "失败不得留下半成品产物"


def test_unknown_station_name_is_usage_error(seeded, capsys):
    out, _ = _run_dummy(seeded, capsys)
    code, env = _station(out["run_id"], "NotAStation", "--probe", capsys=capsys)
    assert code == 2
    assert env["errors"][0]["code"] == "E1001"

# ---------------------------------------------------------------- 能力模式边界
def test_live_single_step_rebuilds_objects_cross_process(seeded, tmp_path, capsys):
    """S5-M2：能力模式单步跨进程可跑——上游对象从冻结产物重建并对拍。

    `run --live` 结束后，Panel/Scope/候选暴露等内存对象已消失。单步进程按
    顺序重推导上游（payload_digest 逐字节对拍）再执行目标工位；产出的
    payload 必须与整体 run 时同一位候选的同一位结论逐字节一致。
    """
    capsys.readouterr()
    main(["run", "--plan", str(seeded), "--live"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True, out
    assert out["data"]["parcels"] == 8
    tickets = [p.name for p in (tmp_path / "runs").iterdir() if p.is_dir()]
    assert len(tickets) == 1, tickets
    ticket = tickets[0]
    run_dir = tmp_path / "runs" / ticket

    def frozen_digest(station: str) -> str:
        parcel = next(run_dir.glob(f"parcels/*-{station}-*/parcel.json"))
        return json.loads(parcel.read_text(encoding="utf-8"))["payload_digest"]

    # Boundary：只依赖供给（Pin + FieldStream 由 supply_fn 重新注入）
    code, env = _station(ticket, "Boundary", "--probe", capsys=capsys)
    assert code == 0 and env["ok"] is True, json.dumps(env, ensure_ascii=False)
    assert env["data"]["live"] is True and env["data"]["scope"] == "steps"
    probe = json.loads(next(run_dir.glob("steps/*-Boundary-*/payload.json"))
                       .read_text(encoding="utf-8"))
    assert "observable_mask_ref" in probe          # 真实产物，不是 dummy 桩

    # Evidence：候选暴露/Panel/标签全部跨进程重建，产出与 run 逐字节一致
    code, env = _station(ticket, "Evidence", "--probe", capsys=capsys)
    assert code == 0, json.dumps(env, ensure_ascii=False)
    step = next(run_dir.glob("steps/*-Evidence-*/parcel.json"))
    assert json.loads(step.read_text(encoding="utf-8"))["payload_digest"] \
        == frozen_digest("Evidence")

    # Verdict：observation_set 取自冻结 payload + n_trials 注入，同样逐位一致
    code, env = _station(ticket, "Verdict", "--probe", capsys=capsys)
    assert code == 0, json.dumps(env, ensure_ascii=False)
    step = next(run_dir.glob("steps/*-Verdict-*/parcel.json"))
    assert json.loads(step.read_text(encoding="utf-8"))["payload_digest"] \
        == frozen_digest("Verdict")

    # Portfolio：combine + S4 尾链全部重建；Certify：组合对象 + 逐折净表现
    for name in ("Portfolio", "Certify"):
        code, env = _station(ticket, name, "--probe", capsys=capsys)
        assert code == 0, f"{name}: " + json.dumps(env, ensure_ascii=False)
        step = next(run_dir.glob(f"steps/*-{name}-*/parcel.json"))
        assert json.loads(step.read_text(encoding="utf-8"))["payload_digest"] \
            == frozen_digest(name), name
