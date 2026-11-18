"""DSR 口径与成本量纲：两个曾经让结论失真的地方。

一、`sigma_sr`
  旧实现取「试验间 SR 横截面离散度」。它测的是候选之间差异有多大，不是
  「这个 SR 有多少来自运气」。实测后果：同一时间轴、同一因子模板，
  合约 32 候选全 reject、现货 15 候选全 pass，差别只来自候选集构成。
  现默认取单策略采样误差 `1/√(n_obs−1)`（ACCEPTANCE 的 1.53@255 只与它自洽），
  横截面离散度降级为 `sigma_sr_cross` 诊断字段。

二、成本量纲
  标签是 h 根 bar 的跨度收益，每 bar 采一次样。旧实现把「每 h bar 的毛收益」
  与「每 bar 的成本」直接相减，等于把成本低估 h 倍；`gross_sharpe` 用 √365
  年化每 h bar 收益，多报 √h 倍。两处都必须按 h 换算。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.inference import expected_max_sr
from capability.ops.observe import _longshort
from capability.ops.verdict import build as verdict_build
from capability.runtime import reset_impls
from contracts.errors import E1004, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


# ----------------------------------------------------------- 成本量纲
def _ls_fixture(t=400, n=9, seed=3):
    rng = np.random.default_rng(seed)
    values = rng.normal(size=(t, n))
    score = values * 0.1 + rng.normal(size=(t, n)) * 0.05
    valid = np.ones((t, n), dtype=bool)
    return values, valid, score, valid


def test_gross_return_is_per_bar_equivalent():
    values, valid, score, label_valid = _ls_fixture()
    _, _, gr1, _, _, _ = _longshort(values, valid, score, label_valid, 1)
    _, _, gr5, _, _, _ = _longshort(values, valid, score, label_valid, 5)
    # h=5 的每期收益是 5 根 bar 的累计，折成每 bar 恰好是 1/5
    assert gr5 == pytest.approx(gr1 / 5.0, rel=1e-12)


def test_gross_sharpe_annualises_by_independent_periods():
    values, valid, score, label_valid = _ls_fixture()
    _, sh1, _, sr1, _, _ = _longshort(values, valid, score, label_valid, 1)
    _, sh5, _, sr5, _, _ = _longshort(values, valid, score, label_valid, 5)
    # SR 每期不随 h 变（缩放均值与标准差，比值不变）
    assert sr5 == pytest.approx(sr1, rel=1e-12)
    # 年化必须按 365/h 个独立期：h=5 时正好是原来的 1/√5
    assert sh5 == pytest.approx(sh1 / math.sqrt(5.0), rel=1e-12)


def test_label_reports_horizon_bars():
    from capability.ops.label import build as label_build
    t, n = 60, 6
    times = START + np.arange(t, dtype="int64") * DAY_NS
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(size=(t, n)), axis=0)
    from capability.panel import Panel
    panel = Panel(times=times, symbols=tuple(f"S{j}" for j in range(n)),
                  fields={"close": close, "volume": rng.normal(size=(t, n)) + 20})
    call = Call.create(
        capability="label", station="Target", impl="forward_return_cs",
        impl_version="1",
        params={"entry": "next_close", "rebalance": 3, "horizon": "5bar",
                "transform": "cs_rank", "overlap_policy": "nw_adjusted",
                "min_width": 3, "width_policy": "drop", "neutralise": []},
        policy_view={"label": {"horizon": "5bar", "neutralise_window": 20},
                     "execution": {"entry": "next_close", "rebalance": 3}},
        inputs=(InputRef("panel", panel.digest(), data=panel),),
    )
    out = run(call)
    assert out["horizon_bars"] == 5


# ----------------------------------------------------------- DSR 口径
def _observation_set(srs, *, n_obs=1270):
    return {
        "per_candidate": [
            {
                "candidate_id": f"sha256:{i:064x}",
                "metrics": {
                    "sr": float(sr), "sr_n_obs": int(n_obs),
                    "p_value": 0.001, "hac_t": 4.0,
                    "cost_adj_return": 0.05, "empirical_p": 0.01,
                    "null_distribution": [0.5, 0.8, 1.1],
                },
            }
            for i, sr in enumerate(srs)
        ]
    }


def _verdict(params, srs, *, n_trials=None):
    payload = _observation_set(srs)
    call = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.05, "fdr_max": 0.5, "cost_adj_min": 0.0,
                "family": "f", **params},
        runtime_facts={"n_trials": 94},
        inputs=(InputRef("observation_set", "sha256:" + "9" * 64, data=payload),),
    )
    out = run(call)
    return {r["candidate_id"]: r["statistics"] for r in out["per_candidate"]}


def test_default_sigma_is_sampling_error_not_cross_section():
    stats = _verdict({}, [0.02, 0.05, 0.30])
    first = next(iter(stats.values()))
    assert first["sigma_mode"] == "sampling"
    assert first["sigma_sr_used"] == pytest.approx(1.0 / math.sqrt(1269), rel=1e-9)
    # 横截面离散度仍在，但只作诊断
    assert "sigma_sr_cross" in first


def test_sampling_benchmark_matches_documented_scale():
    """n_obs≈1270、n_trials≈94 → 每期门槛 0.085，年化 ≈1.6（不是 7.5）。"""
    stats = _verdict({}, [0.02, 0.05, 0.30])
    first = next(iter(stats.values()))
    expected = (1.0 / math.sqrt(1269)) * math.sqrt(2 * math.log(first["n_trials"]))
    assert first["dsr_benchmark_sr"] == pytest.approx(expected, rel=1e-9)


def test_cross_section_mode_remains_selectable():
    stats = _verdict({"sigma_mode": "cross_section"}, [0.02, 0.05, 0.30])
    first = next(iter(stats.values()))
    assert first["sigma_mode"] == "cross_section"
    # 横截面 σ 比采样误差大得多 → 门槛也高得多
    assert first["sigma_sr_used"] > 1.0 / math.sqrt(1269)


def test_unknown_sigma_mode_is_e1004():
    with pytest.raises(FlowError) as exc:
        _verdict({"sigma_mode": "magic"}, [0.02, 0.05, 0.30])
    assert exc.value.error.code == E1004


def test_expected_max_sr_unchanged():
    # 搜索惩罚必须随试验数上升：这是「诚实计数」的另一面
    assert expected_max_sr(10) < expected_max_sr(100) < expected_max_sr(1000)
