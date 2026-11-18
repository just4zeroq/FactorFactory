"""S3-M6 收尾：`flow run --live` 让命令行触发真实能力。

端到端：fetch 采集 parquet → run --live → 八个工位全部走真实能力并落盘，
第七工位 Portfolio（combine→allocate→backtest）与第八工位 Certify
（walk-forward 逐折复算）都在同一条链上，最后产出 out_of_sample 证书。
同时守：默认 run 仍走 dummy、缺供给的 --live 明确失败、展开器不造 schema 外字段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from v5.app.cli import build_parser, main
from v5.app.cli import fetch as fetch_mod
from v5.app.cli.live import expand_station_inputs
from v5.contracts.errors import E1004, E1008, E3003, E3004, FlowError

FROZEN = datetime(2026, 11, 16, tzinfo=timezone.utc)
SYMBOLS = tuple(f"S{i}" for i in range(8))
SET = "cs_live"
N_BARS = 95


@dataclass
class FakeExchange:
    bars: dict[str, list[list[int]]]

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        rows = [list(r) for r in self.bars[symbol] if since is None or r[0] >= since]
        return rows[:limit]


def _bars() -> dict[str, list[list[int]]]:
    out = {}
    for idx, sym in enumerate(SYMBOLS):
        rows = []
        rng = np.random.default_rng(idx + 1)
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
        "plan": "cs_live_a", "baseline": "v1",
        "supply": {"exchange": "binanceusdm", "datasets": ["ohlcv"],
                   "symbols": SET, "frequency": "1d"},
        "caliber": {
            "execution": {"entry": "next_close", "rebalance": 3,
                          "annualisation": "calendar_365", "funding": "per_contract_events"},
            "label": {"horizon": 3, "transform": "cs_rank", "neutralise": [],
                      "overlap_policy": "independent_sample",
                      "min_width": 2, "width_policy": "keep"},
            "allocate": {"quantiles": 3, "long_short": True, "weighting": "equal",
                         "max_weight": 0.2, "net_exposure": 0.0, "turnover_max": 0.35,
                         "min_members": 2},
            "net": {"cost_tiers": [10, 20], "slippage": "none", "fee_model": "taker",
                    "max_leverage": 1.0, "initial_margin": 0.1,
                    "maintenance_margin": 0.005, "liquidation_buffer": 0.01},
        },
        "boundary": {"impl": "pit_dynamic_topk",
                     "params": {"k": 5, "tier_window": 3, "min_coverage": 0.5},
                     "time_axis": {"mine": "2024-01-05..2024-03-01",
                                   "oos": "2024-03-02..2024-04-04",
                                   "holdout": "reserved"}},
        "panel": {"impl": "point_in_time_align", "params": {"drop_unclosed": True}},
        "target": {"impl": "forward_return_cs"},
        "search": {"algorithms": [{"name": "gp_symbolic",
                                   "params": {"population": 5, "generations": 1,
                                              "max_depth": 3}}],
                   "prior_pool": [], "trial_family": "cs_live_a", "seed": 20261116},
        "evidence": {"impl": "two_stage_funnel",
                     "params": {"null_model": "within_bar_cross_asset",
                                "prefilter": {"min_coverage": 0.5},
                                # 保留默认的成本归因：本用例要证明 S4 的净成本链
                                # 在 CLI 端到端链路上真跑（Evidence 落 method_id），
                                # 而不是只跑编排骨架。
                                "cost_attribution": True}},
        # 这里验证的是「前六工位真跑 + Portfolio 的 S4 尾链」，不追求统计
        # 显著；fdr_max=1.0 让随机搜索至少留下一个 watch 候选，避免把一个
        # 统计裁决结果误当成本测试的编排失败。
        "verdict": {"impl": "sentinel",
                    "params": {"dsr_min": 0.01, "fdr_max": 1.0, "cost_adj_min": 0.001}},
        "portfolio": {"combiner": "equal_weight",
                      "window": {"fit": "2024-01-10..2024-02-20", "embargo": 1,
                                 "eval": "2024-02-25..2024-03-01"}},
        "certify": {"impl": "walk_forward", "params": {"folds": 2, "embargo": 1}},
    }


def _seed(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({SET: list(SYMBOLS)}), encoding="utf-8")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(fetch_mod, "_client_factory", lambda _eid: FakeExchange(_bars()))
    monkeypatch.setattr(fetch_mod, "_client_factory", lambda _eid: FakeExchange(_bars()))
    code = main(["fetch", "--datasets", "ohlcv", "--symbol-set", SET,
                 "--start", "2024-01-01T00:00:00+00:00",
                 "--end", f"{(datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=N_BARS)).isoformat()}",
                 "--exchange", "binanceusdm", "--market", "futures", "--limit", "200"])
    assert code == 0
    return path


def _write_parcels(tmp_path, plan_path):
    """返回 run --live 的 (envelope, run_id)。"""
    return main(["run", "--plan", str(plan_path), "--live"])


def _read_run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "runs" / run_id


# ---------------------------------------------------------------- 展开器
def test_expand_flattens_nested_plan_without_schema_extra():
    params, policy_view = expand_station_inputs(_plan())
    # boundary 的 params 摊平 + time_axis 提升 + 确定性默认
    assert params["boundary"]["k"] == 5
    assert params["boundary"]["time_axis"]["mine"].startswith("2024-01-05")
    assert params["boundary"]["mask_policy"] == "pit_dynamic"
    # target 的口径镜像来自 caliber，同一个源
    assert params["target"]["entry"] == "next_close"
    assert params["target"]["horizon"] == 3
    # policy_view 四子块齐全，neutralise_window 只在 policy_view、不进 params
    assert set(policy_view) == {"execution", "label", "allocate", "net"}
    assert "neutralise_window" not in params["target"]
    # evidence 的必填由默认补齐
    assert params["evidence"]["metrics"] == ["ic"]
    assert params["evidence"]["n_permutations"] == 20
    # verdict.family 来自 trial_family
    assert params["verdict"]["family"] == "cs_live_a"


def test_expand_rejects_missing_required_from_caliber():
    plan = _plan()
    del plan["caliber"]["label"]["horizon"]
    with pytest.raises(FlowError) as exc:
        expand_station_inputs(plan)
    assert exc.value.code == E1008


def test_expand_rejects_unknown_combiner_without_silent_downgrade():
    """未知组合器仍在展平期拒绝（E1004）；三个已实现的合成器放行。"""
    plan = _plan()
    plan["portfolio"]["combiner"] = "magic_combiner"
    with pytest.raises(FlowError) as exc:
        expand_station_inputs(plan)
    assert exc.value.code == E1004


def test_expand_passes_s7_combiner_and_window_constraints():
    """S7 组合域：合成器走实现通道，正交/相关上限走参数通道。"""
    plan = _plan()
    plan["portfolio"]["combiner"] = "fixed_ridge"
    plan["portfolio"]["orthogonalise"] = "symmetric"
    plan["portfolio"]["corr_max"] = 0.6
    params, _view = expand_station_inputs(plan)
    assert params["portfolio"]["orthogonalise"] == "symmetric"
    assert params["portfolio"]["corr_max"] == 0.6
    # combiner 不进 params：它必须走 impl 通道，否则换合成器只改参数指纹
    assert "combiner" not in params["portfolio"]


def test_expand_rejects_missing_algorithm():
    plan = _plan()
    plan["search"]["algorithms"] = []
    with pytest.raises(FlowError) as exc:
        expand_station_inputs(plan)
    assert exc.value.code == E1004


# ---------------------------------------------------------------- 端到端
def test_run_live_completes_all_eight_stations(tmp_path, monkeypatch, capsys):
    plan_path = _seed(tmp_path, monkeypatch)
    capsys.readouterr()

    code = main(["run", "--plan", str(plan_path), "--live"])
    out = json.loads(capsys.readouterr().out)
    # S7：八工位全部真跑，Certify 产出 out_of_sample 证书
    assert code == 0 and out["ok"] is True, json.dumps(
        out.get("errors"), ensure_ascii=False)
    assert out["data"]["parcels"] == 8
    assert out["data"]["stage"] == "S7-live-capability"

    runs = [d for d in (tmp_path / "runs").iterdir() if d.is_dir()]
    assert len(runs) == 1
    run_dir = runs[0]
    trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(
        encoding="utf-8").splitlines()]
    by_station = {t["station"]: t for t in trace}
    for station in ("Boundary", "Panel", "Target", "Search", "Evidence",
                    "Verdict", "Portfolio", "Certify"):
        assert by_station[station]["status"] == "done", station

    # 真产物：Boundary 落的是真实掩码 ref（dummy 桩 payload 恒为 {"station","kind"}）
    parcels = list(run_dir.glob("parcels/*-Boundary-*/payload.json"))
    assert parcels
    boundary_payload = json.loads(parcels[0].read_text(encoding="utf-8"))
    assert "observable_mask_ref" in boundary_payload
    assert boundary_payload.get("kind") != "dummy"

    # Search 真产出候选 + n_trials（实测计数，非 0）
    search = [json.loads(p.read_text(encoding="utf-8"))
              for p in run_dir.glob("parcels/*-Search-*/payload.json")][0]
    assert isinstance(search.get("n_trials"), int) and search["n_trials"] >= 1
    assert search.get("count", 0) >= 1

    # S4 净成本链：Evidence 的 per_candidate 不再是 est_cost=0 占位，而是同一
    # caliber 下 allocate+backtest 的真实成本（method_id 必须可追溯）。
    evidence = [json.loads(p.read_text(encoding="utf-8"))
                for p in run_dir.glob("parcels/*-Evidence-*/payload.json")][0]
    rows = evidence["per_candidate"]
    assert rows
    for row in rows:
        attr = row["cost_attribution"]
        assert attr["method_id"] == "allocate+backtest@S4"
        assert attr["estimated_cost"] >= 0.0
        assert set(attr["breakdown"]) == {"fee", "slippage", "funding", "total"}
        # 不能写成 `if gross_return is not None:`——首跑时该字段根本没输出，
        # 断言被整段跳过，「成本闸按毛收益放行」这个 bug 就这样通过了测试。
        # 现在硬断言：毛收益必须在、净收益必须等于毛收益减每期成本。
        gross = row["metrics"]["gross_return"]
        assert gross is not None
        assert row["metrics"]["cost_adj_return"] == pytest.approx(
            gross - attr["estimated_cost"])
        assert attr["net_return"] == pytest.approx(row["metrics"]["cost_adj_return"])

    # S4 第 7 工位：合成分数、目标权重、成本与样本内标记必须同处一个 Parcel。
    portfolio = [json.loads(p.read_text(encoding="utf-8"))
                 for p in run_dir.glob("parcels/*-Portfolio-*/payload.json")][0]
    for key in ("score_ref", "weights_ref", "member_weights_ref", "members",
                "domain_digest", "windows_used", "cost_breakdown", "net_ref",
                "binding_constraints", "evidence_grade"):
        assert key in portfolio, key
    assert portfolio["evidence_grade"] == "in_sample"
    assert portfolio["portfolio_stage"] == "S4_diagnostic"
    assert portfolio["members"]
    assert all(m["candidate_id"] and m["weight_share"] > 0
               for m in portfolio["members"])
    assert {"fee", "slippage", "funding", "total"} <= set(portfolio["cost_breakdown"])

    # S7 第 8 工位：认证段（time_axis.oos）逐折复算，产 out_of_sample 证书。
    cert = [json.loads(p.read_text(encoding="utf-8"))
            for p in run_dir.glob("parcels/*-Certify-*/payload.json")][0]
    for key in ("metrics", "folds_detail", "reproducible_manifest",
                "verdict_refs", "evidence_grade"):
        assert key in cert, key
    assert cert["evidence_grade"] == "out_of_sample"
    metrics = cert["metrics"]
    assert metrics["fold_count"] == 2
    assert metrics["oos_window"] == {"start": "2024-03-02", "end": "2024-04-04"}
    assert metrics["period_count"] > 0
    assert metrics["net_sharpe"] is not None
    assert metrics["bars_per_year_source"] == "time_axis_median"
    assert {"fee", "slippage", "funding", "total"} <= set(metrics["cost_breakdown"])
    # 折边界必须与组合 eval 段严格不重叠（I24）：认证段起点晚于 eval 末端。
    assert metrics["oos_window"]["start"] > portfolio["windows_used"]["eval"].split("..")[-1]
    assert len(cert["folds_detail"]) == metrics["fold_count"]
    for fold in cert["folds_detail"]:
        assert fold["start_index"] < fold["end_index"]
        assert fold["n_bars"] >= 2
        assert "net_ref" in fold and fold["net_ref"].startswith("sha256:")
    # 判据只引用成员，不自算一套「展示用」显著性
    member_ids = {m["candidate_id"] for m in portfolio["members"]}
    assert {ref["candidate_id"] for ref in cert["verdict_refs"]} == member_ids
    assert all(ref["decision"] in {"pass", "watch"} for ref in cert["verdict_refs"])
    assert cert["reproducible_manifest"]["panel_digest"].startswith("sha256:")
    # 证书清单的口径身份必须能与 ticket 对账：`policy_digest` 是编译期那一份
    # （含 baseline_version / constitutive_subset），能力层自己算不出它，只能
    # 由编排层回填。曾经这里放的是 `H(policy_view)` 却冠名 policy_digest，
    # 于是证书永远对不上账、也不报错。
    manifest = cert["reproducible_manifest"]
    assert manifest["policy_digest"] == out["data"]["fingerprints"]["policy_digest"]
    assert manifest["policy_view_digest"].startswith("sha256:")
    assert manifest["policy_digest"] != manifest["policy_view_digest"]


def test_default_run_still_uses_dummy_and_live_flag_is_explicit(
        tmp_path, monkeypatch, capsys):
    plan_path = _seed(tmp_path, monkeypatch)
    capsys.readouterr()
    # 不带 --live：八工位 dummy 全过，仍然成功（S0/S1/S2 兼容）
    code = main(["run", "--plan", str(plan_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True
    assert out["data"]["live"] is False
    run_dir = tmp_path / "runs" / out["run_id"]
    payload = json.loads(next(run_dir.glob("parcels/*-Boundary-*/payload.json"))
                         .read_text(encoding="utf-8"))
    assert payload.get("kind") == "dummy"


def test_live_without_published_supply_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({SET: list(SYMBOLS)}), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    # 没有 fetch：编译期就 E4002（数据根下没有任何 parquet），不会静默走 dummy。
    # 别搞错成 E4001：E4001 是「别名表指向不存在的版本」，而方案 B 之后根本
    # 没有别名表，缺数据就是「文件不存在」。
    code = main(["run", "--plan", str(plan_path), "--live"])
    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert out["errors"][0]["code"] == "E4002"


# ------------------------------------------------- 资金费事件网格（I34/AC34）
class _FundingDS:
    """最小事件数据集：`columns` 是 read_dataset 拼出的长表形态。"""

    def __init__(self, rows):
        # rows: [(symbol, datetime, rate)]
        self.columns = {
            "symbol": [r[0] for r in rows],
            "timestamp": [r[1] for r in rows],
            "funding_rate": [r[2] for r in rows],
        }


class _Stream:
    def __init__(self, times, symbols):
        self.times = np.asarray(times, dtype="int64")
        self.symbols = tuple(symbols)










# ------------------------------------------------- 资金费事件网格（I34/AC34）
class _FundingDS:
    """最小事件数据集：`columns` 是 read_dataset 拼出的长表形态。"""

    def __init__(self, rows):
        # rows: [(symbol, datetime, rate)]
        self.columns = {
            "symbol": [r[0] for r in rows],
            "timestamp": [r[1] for r in rows],
            "funding_rate": [r[2] for r in rows],
        }


class _Stream:
    def __init__(self, times, symbols):
        self.times = np.asarray(times, dtype="int64")
        self.symbols = tuple(symbols)


def test_funding_grid_1h_and_4h_events_converge_to_the_same_bar():
    """AC34：1h 网格 8 条、4h 网格 2 条，区间总额相同 → 同一 bar 金额相等。"""
    from v5.app.cli.live import _funding_grid

    day = 86_400_000_000_000
    t0 = int(np.datetime64("2026-01-01T00:00:00", "ns").astype("int64"))
    times = t0 + np.arange(3, dtype="int64") * day  # 三根日 bar
    stream = _Stream(times, ("A", "B"))

    hourly = []
    for k in range(8):  # 第 0 根 bar 内 8 条 1h 结算，每条 0.0001
        hourly.append(("A", datetime.fromtimestamp(
            (t0 + k * day // 8) / 1e9, tz=timezone.utc), 0.0001))
    four_hourly = [(  # 同一根 bar 内 2 条 4h 结算，总额同为 0.0008
        "A", datetime.fromtimestamp((t0 + 0 * day // 2) / 1e9, tz=timezone.utc),
        0.0004),
        ("A", datetime.fromtimestamp((t0 + 1 * day // 2) / 1e9, tz=timezone.utc),
         0.0004),
    ]

    g1, d1 = _funding_grid(_FundingDS(hourly), stream)
    g4, d4 = _funding_grid(_FundingDS(four_hourly), stream)

    assert d1 == d4 == 0
    # 第 0 根 bar 聚合相等；第 1、2 根 bar 无事件为零；B 全程零事件
    np.testing.assert_allclose(g1, g4)
    assert g1[0, 0] == pytest.approx(0.0008)
    assert g1[1:, :].sum() == 0.0 and g1[:, 1].sum() == 0.0


def test_funding_grid_attributes_events_on_bar_open_to_that_bar():
    """恰在 t_k 的结算属于第 k 根 bar（(t_{k-1}, t_k]），早于首根的丢弃。"""
    from v5.app.cli.live import _funding_grid

    day = 86_400_000_000_000
    t0 = int(np.datetime64("2026-01-01T00:00:00", "ns").astype("int64"))
    times = t0 + np.arange(2, dtype="int64") * day
    stream = _Stream(times, ("A",))
    rows = [
        ("A", datetime.fromtimestamp(t0 / 1e9, tz=timezone.utc), 0.001),       # bar 0
        ("A", datetime.fromtimestamp((t0 + day) / 1e9, tz=timezone.utc), 0.002),  # bar 1
        ("A", datetime.fromtimestamp((t0 - day) / 1e9, tz=timezone.utc), 0.004),  # 面板前
        ("X", datetime.fromtimestamp(t0 / 1e9, tz=timezone.utc), 0.008),     # 面板外
    ]
    grid, dropped = _funding_grid(_FundingDS(rows), stream)
    assert grid[0, 0] == pytest.approx(0.001)
    assert grid[1, 0] == pytest.approx(0.002)
    assert dropped == 2  # 面板前 1 条 + 面板外 1 条


def test_funding_supply_requires_dataset_and_symbols_when_fee_model_asks():
    """口径要求计资金费：缺数据集、缺标的都 E4002；口径没要求时一个字节不读。"""
    from v5.app.cli.live import _funding_supply

    day = 86_400_000_000_000
    t0 = int(np.datetime64("2026-01-01T00:00:00", "ns").astype("int64"))
    stream = _Stream(t0 + np.arange(3, dtype="int64") * day, ("A", "B"))
    plan = {"caliber": {"net": {"fee_model": "taker_with_funding"}}}

    # 缺数据集
    with pytest.raises(FlowError) as e1:
        _funding_supply({}, plan, stream)
    assert e1.value.code == "E4002"

    # 缺面板标的 B
    rows = [("A", datetime.fromtimestamp(t0 / 1e9, tz=timezone.utc), 0.001)]
    with pytest.raises(FlowError) as e2:
        _funding_supply({"funding": _FundingDS(rows)}, plan, stream)
    assert e2.value.code == "E4002"

    # 齐全 → (T,N) 网格
    rows = [("A", datetime.fromtimestamp(t0 / 1e9, tz=timezone.utc), 0.001),
            ("B", datetime.fromtimestamp(t0 / 1e9, tz=timezone.utc), 0.002)]
    grid = _funding_supply({"funding": _FundingDS(rows)}, plan, stream)
    assert grid.shape == (3, 2) and grid[0, 0] == pytest.approx(0.001)

    # 口径没要求（taker）→ 不读、不构造
    plan_taker = {"caliber": {"net": {"fee_model": "taker"}}}
    assert _funding_supply({"funding": _FundingDS([])}, plan_taker, stream) is None


def test_bridge_funding_input_is_aligned_or_none():
    """`_funding_input`：supply 注入才取；形状一致直接用，supply 缺席是 None。"""
    from v5.orchestration.capability_bridge import CapabilityBridge

    grid = np.full((4, 3), 1e-4)

    def supply_fn(station, params):
        assert station == "Evidence"
        return {"funding": grid}

    bridge = CapabilityBridge(supply_fn=supply_fn)
    out = bridge._funding_input(station="Evidence", upstream={}, shape=(4, 3))
    np.testing.assert_allclose(out, grid)

    assert CapabilityBridge()._funding_input(
        station="Evidence", upstream={}, shape=(4, 3)) is None


# ------------------------------------------------- 事件字段注入（D-006）










# ------------------------------------------------- 事件字段注入（D-006）
def _bar_stream(tmp_times, symbols):
    from v5.capability.stream import FieldStream
    n = len(symbols)
    fields = {
        "close": np.full((len(tmp_times), n), 100.0),
        "volume": np.ones((len(tmp_times), n)),
    }
    manifest = {
        "close": {"dataset": "ohlcv", "version": "v1", "unit": "quote",
                  "available_lag": 0},
        "volume": {"dataset": "ohlcv", "version": "v1", "unit": "contracts",
                   "available_lag": 0},
    }
    return FieldStream(times=np.asarray(tmp_times, dtype="int64"),
                       symbols=tuple(symbols), fields=fields, manifest=manifest)


def _event_stream(ev_times, symbols, values, field="funding_rate"):
    from v5.capability.stream import FieldStream
    arr = np.asarray(values, dtype="float64")
    if arr.ndim == 1:
        arr = arr[:, None]
    fields = {field: arr}
    manifest = {field: {"dataset": "funding", "version": "v1", "unit": "rate",
                        "available_lag": 0}}
    return FieldStream(times=np.asarray(ev_times, dtype="int64"),
                       symbols=tuple(symbols), fields=fields, manifest=manifest)


def test_event_field_grid_as_of_forward_fill():
    """事件 → 面板 as-of 前向填充：t 行只拿 ≤ t 的最后观测（PIT）。"""
    from v5.app.cli.live import _event_field_grid

    day = 86_400_000_000_000
    t0 = int(np.datetime64("2026-01-01T00:00:00", "ns").astype("int64"))
    bars = [t0 + k * day for k in range(5)]
    ev_times = [t0, t0 + 2 * day]          # 两个结算事件
    ev = _event_stream(ev_times, ("A",), [0.001, 0.003])
    bars_stream = _bar_stream(bars, ("A", "B"))

    grid, outside = _event_field_grid(ev, bars_stream, "funding_rate")

    assert outside == 0
    assert grid[0, 0] == pytest.approx(0.001)   # bar0 之前最近的事件 = t0
    assert grid[1, 0] == pytest.approx(0.001)   # 尚无新事件 → 沿用
    assert grid[2, 0] == pytest.approx(0.003)   # t2 事件当根生效
    assert grid[3, 0] == pytest.approx(0.003)
    assert np.isnan(grid[:, 1]).all()           # 无事件的标的全 NaN（不造零）


def test_event_field_grid_counts_outside_symbols():
    from v5.app.cli.live import _event_field_grid

    day = 86_400_000_000_000
    t0 = int(np.datetime64("2026-01-01T00:00:00", "ns").astype("int64"))
    ev = _event_stream([t0], ("A", "ZOMBIE"), [[0.001, 0.5]])
    bars_stream = _bar_stream([t0], ("A",))
    grid, outside = _event_field_grid(ev, bars_stream, "funding_rate")
    assert outside == 1
    assert grid[0, 0] == pytest.approx(0.001)


def test_inject_event_fields_noop_without_datasets():
    """没采集事件数据集 → 原流原样返回，不造字段（NaN 语义诚实）。"""
    from v5.app.cli.live import _inject_event_fields

    day = 86_400_000_000_000
    t0 = int(np.datetime64("2026-01-01T00:00:00", "ns").astype("int64"))
    stream = _bar_stream([t0, t0 + day], ("A",))
    merged, notes = _inject_event_fields({}, stream, "cs_x")
    assert merged is stream
    assert notes == {}


# ------------------------------------------------- S7 三条单命令（重算）
def test_single_commands_replay_the_frozen_ticket(tmp_path, monkeypatch, capsys):
    """evaluate / backtest / certify 在已落盘 ticket 上重算，且不改已发布事实。"""
    plan_path = _seed(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["run", "--plan", str(plan_path), "--live"]) == 0
    capsys.readouterr()
    ticket = [d.name for d in (tmp_path / "runs").iterdir() if d.is_dir()][0]

    # backtest：净值摘要必须来自本次重算（combine→allocate→backtest），不是复读
    code = main(["backtest", "--on", ticket])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"], out.get("errors")
    summary = out["data"]["summary"]
    assert summary["periods"] >= 1
    assert {"net_sharpe", "bars_per_year", "annualisation_used",
            "cost_breakdown", "sharpe", "cagr_pct", "max_dd_pct",
            "turnover"} <= set(summary)
    # read_only：重算不得落 steps/ 产物（否则契约里的 read_only 是假的）
    assert not list((tmp_path / "runs" / ticket).glob("steps/*-Portfolio-*"))

    # evaluate：逐候选重算 IC/ICIR；指定候选时只算那一个
    search = [json.loads(p.read_text(encoding="utf-8"))
              for p in (tmp_path / "runs" / ticket).glob("parcels/*-Search-*/payload.json")][0]
    cid = search["candidates"][0]["candidate_id"]
    code = main(["evaluate", "--on", ticket, "--candidate", cid])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"], out.get("errors")
    rows = out["data"]["evaluation"]
    assert len(rows) == 1 and rows[0]["candidate_id"] == cid
    assert rows[0]["status"] == "observed" and rows[0]["stats"]["n_periods"] >= 1
    assert 0.0 <= rows[0]["stats"]["coverage"] <= 1.0

    # certify：契约等级是 writes_ledger，缺 --yes 必须拒绝
    code = main(["certify", "--on", ticket])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["ok"] is False
    code = main(["certify", "--on", ticket, "--yes"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"], out.get("errors")
    cert = out["data"]["certificate"]
    assert cert["evidence_grade"] == "out_of_sample"
    assert cert["folds_detail"]
