"""S3-M2 · observe 两阶段漏斗 + 零分布证据采集。

核心验收（V4 头号 bug 的修复）：
- 行内跨资产置换下，**带资产漂移**的面板不会出现「零分布中位 |t| > 观测 |t|」；
  整行时间轴置换（time_shift）会——实证中位 4.74 > 观测 3.46，empirical_p=0.95。
- 纯噪声面板的 empirical_p 近似均匀（不集中在 0 或 1）。
- Stage A 拒绝的候选不进零分布、标记stage_a_reasons，不冒充"已检验"。
"""

from __future__ import annotations

import numpy as np
import pytest

from v5.capability import Call, InputRef, run
from v5.capability.impls import install_default_impls
from v5.capability.panel import Panel, array_digest
from v5.capability.runtime import reset_impls
from v5.contracts.errors import E3001, E3003, E1004, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _code(fn):
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


def _panel(t=120, n=8, seed=0):
    rng = np.random.default_rng(seed)
    times = START + np.arange(t, dtype="int64") * DAY_NS
    close = 100 + np.cumsum(rng.normal(size=(t, n)), axis=0)
    close = np.abs(close) + 1
    return Panel(times=times, symbols=tuple(f"S{j}" for j in range(n)),
                 fields={"close": close})


def _label(score, valid=None, horizon_bars=1):
    if valid is None:
        valid = np.isfinite(score)
    score = np.asarray(score, dtype="float64")
    return {
        "score": score,
        # 测试夹具里 score 本身就是收益序列（transform=none 的世界）：
        # raw（observe 多空收益的基座）与 score 相同；horizon_bars 必给——
        # observe 对缺跨度 fail-closed（B4）。
        "raw": np.array(score, dtype="float64"),
        "valid_mask": np.asarray(valid, dtype=bool),
        "effective_n": int(valid.sum()),
        "horizon_bars": horizon_bars,
    }


def _candidate_set(cands_specs):
    """specs: list[(cid, values)] → candidate_set with exposures from evaluate-like arrays."""
    cands, exposures = [], {}
    for cid, values in cands_specs:
        values = np.ascontiguousarray(values, dtype="float64")
        cands.append({"candidate_id": cid, "generator": "gp@1",
                      "payload": {"expression": {"field": "close"}},
                      "required_fields": ["close"]})
        exposures[cid] = {
            "values": values,
            "valid_mask": np.isfinite(values),
            "values_ref": array_digest(values),
        }
    return {"candidates": cands, "exposures": exposures, "family": "f1", "count": len(cands)}


def _call(cs, panel, label, *, null_model="within_bar_cross_asset", n_perm=40,
          min_cov=0.0, max_turn=0.0, min_ic=0.0, metrics=None, cost_attr=True,
          seed=7, dedup=None, groups=None, ic_decay=None):
    # max_turnover 的 schema 是 gt:0——「不设限」必须省略该键，不能传 0.0。
    prefilter = {"min_coverage": min_cov, "min_abs_ic": min_ic}
    if max_turn > 0:
        prefilter["max_turnover"] = max_turn
    params = {
        "prefilter": prefilter,
        "null_model": null_model,
        "metrics": metrics or ["ic", "rank_ic", "icir", "gross_sharpe", "hit_rate", "turnover"],
        "cost_attribution": cost_attr,
        "n_permutations": n_perm,
    }
    if dedup is not None:
        params["dedup"] = dedup
    if groups is not None:
        params["groups"] = groups
    if ic_decay is not None:
        params["ic_decay"] = ic_decay
    if ic_decay is not None:
        params["ic_decay"] = ic_decay
    return Call.create(
        capability="observe", station="Evidence", impl="two_stage_funnel",
        impl_version="1", params=params, seed=seed,
        inputs=(
            InputRef("candidate_set", "sha256:" + "0" * 64, data=cs),
            InputRef("panel", panel.digest(), data=panel),
            InputRef("label", "sha256:" + "1" * 64, data=label),
        ),
    )


# --------------------------------------------------------------- 基本产物
def test_observe_emits_metrics_with_method_ids():
    panel = _panel()
    rng = np.random.default_rng(3)
    values = np.cumsum(rng.normal(size=panel.shape), axis=0)  # 趋势型暴露
    label = _label(rng.normal(size=panel.shape))
    out = run(_call(_candidate_set([("c1", values)]), panel, label))
    row = out["per_candidate"][0]
    assert row["candidate_id"] == "c1"
    for key in ("ic", "hac_t", "p_value", "turnover", "gross_sharpe",
                "cost_adj_return", "null_distribution", "empirical_p"):
        assert key in row["metrics"], key
    assert out["method_ids"]["ic"] == "spearman@1"
    assert out["method_ids"]["null"] == "cross_section_permute@1"
    assert out["method_ids"]["null_biased"] is False
    assert len(row["null_distribution"]) == 40


def test_observe_rejects_unknown_null_model():
    panel = _panel()
    values = panel.fields["close"]
    cs = _candidate_set([("c1", values)])
    call = _call(cs, panel, _label(panel.fields["close"] * 0.01))
    from dataclasses import replace
    bad = Call.create(
        capability="observe", station="Evidence", impl="two_stage_funnel",
        impl_version="1", params={**call.params, "null_model": "made_up"},
        seed=7, inputs=call.inputs,
    )
    assert _code(lambda: run(bad)) == E1004


def test_observe_stage_a_drops_weak_candidate_without_null():
    panel = _panel()
    rng = np.random.default_rng(9)
    # 暴露几乎全常 → |IC| 很低，Stage A 应拒绝
    values = np.ones(panel.shape) + rng.normal(size=panel.shape) * 1e-9
    label = _label(rng.normal(size=panel.shape))
    out = run(_call(_candidate_set([("weak", values)]), panel, label, min_ic=0.05))
    row = out["per_candidate"][0]
    assert "abs_ic_below_floor" in row["stage_a_reasons"]
    assert row["null_distribution"] == []  # 被拒候选不进零分布精算
    assert row["metrics"]["stage_a_pass"] is False


def test_observe_requires_exposures_channel():
    panel = _panel()
    cs = {"candidates": [{"candidate_id": "c1"}], "family": "f"}
    assert _code(lambda: run(_call(cs, panel, _label(panel.fields["close"])))) == E3003


def test_observe_detects_tampered_values_ref():
    panel = _panel()
    rng = np.random.default_rng(1)
    values = rng.normal(size=panel.shape)
    cs = _candidate_set([("c1", values)])
    cs["exposures"]["c1"]["values_ref"] = "sha256:" + "0" * 64
    assert _code(lambda: run(_call(cs, panel, _label(rng.normal(size=panel.shape))))) == E3003


def test_observe_missing_required_input_is_e3001():
    panel = _panel()
    cs = _candidate_set([("c1", panel.fields["close"])])
    call = Call.create(
        capability="observe", station="Evidence", impl="two_stage_funnel",
        impl_version="1", params={
            "prefilter": {}, "null_model": "within_bar_cross_asset",
            "metrics": ["ic"], "cost_attribution": False, "n_permutations": 5,
        },
        inputs=(InputRef("candidate_set", "sha256:" + "0" * 64, data=cs),),
    )
    assert _code(lambda: run(call)) == E3001


# --------------------------------------------------------------- 核心 bug 修复
def test_drift_panel_time_shift_null_biased_but_cross_section_not():
    """V4 实证：带漂移面板用整行时间置换 → 零分布自带 alpha（中位 |t| > 观测）；
    行内跨资产置换不会。这是 V5 修复 V4 的判据。"""
    t, n = 200, 12
    times = START + np.arange(t, dtype="int64") * DAY_NS
    drift = np.linspace(-0.05, 0.05, n)
    rng = np.random.default_rng(0)
    # 因子：时序常数，长期做多高漂移资产（截面排序 = drift 排序）。
    values = np.tile(drift, (t, 1))
    # 标签：漂移 + 时序噪声。截面 drift 结构保留（整行置换打不破），但有波动
    # 让 HAC 方差非零，t 才有意义。
    score = drift[None, :] + rng.normal(0, 0.02, (t, n))
    close = 100 * np.exp(np.cumsum(score, axis=0))
    panel = Panel(times=times, symbols=tuple(f"S{j}" for j in range(n)), fields={"close": close})
    label = _label(score)
    cs = _candidate_set([("drift", values)])

    out_cs = run(_call(cs, panel, label, null_model="within_bar_cross_asset",
                       n_perm=60, min_ic=0.0))
    out_ts = run(_call(cs, panel, label, null_model="time_shift",
                       n_perm=60, min_ic=0.0))

    row_cs = out_cs["per_candidate"][0]["metrics"]
    row_ts = out_ts["per_candidate"][0]["metrics"]
    # 两种零分布的empirical_p 都会被真实 alpha 压低，但 time_shift 的零分布
    # 中位 |t| 应显著更高（混进了 drift），method_ids 也标记它为 deprecated。
    assert np.median(row_ts["null_distribution"]) > np.median(row_cs["null_distribution"])
    assert out_ts["method_ids"]["null_biased"] is True
    assert out_ts["method_ids"]["null"] == "whole_row_time_permutation@deprecated"


def test_pure_noise_empirical_p_not_degenerate():
    """纯噪声：empirical_p 不应恒为 0 或 1（近均匀）。"""
    ps = []
    for seed in range(6):
        panel = _panel(t=100, n=10, seed=seed)
        rng = np.random.default_rng(100 + seed)
        values = rng.normal(size=panel.shape)
        label = _label(rng.normal(size=panel.shape))
        out = run(_call(_candidate_set([("noise", values)]), panel, label,
                        n_perm=40, seed=seed))
        row = out["per_candidate"][0]
        if not row["stage_a_reasons"]:
            ps.append(row["metrics"]["empirical_p"])
    if len(ps) >= 3:
        # 纯噪声下 p 值不该全部卡在 1.0（那说明零分布恒大于观测）
        assert np.mean(np.array(ps) < 1.0) > 0.3, ps


def test_observe_verdict_pipeline():
    """observe 产物可直接喂给 verdict（字段兼容）。"""
    from v5.capability.ops import verdict as v
    panel = _panel()
    rng = np.random.default_rng(5)
    values = np.cumsum(rng.normal(size=panel.shape), axis=0)
    label = _label(rng.normal(size=panel.shape))
    obs = run(_call(_candidate_set([("c1", values)]), panel, label, n_perm=30))
    vcall = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.0, "fdr_max": 0.99, "cost_adj_min": 0.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "2" * 64, data=obs),),
        runtime_facts={"n_trials": 2, "family_cumulative": [r["metrics"]["p_value"]
                                                            for r in obs["per_candidate"]]},
    )
    vout = run(vcall)
    assert vout["per_candidate"][0]["candidate_id"] == "c1"
    assert vout["per_candidate"][0]["decision"] in {"pass", "reject", "watch"}


# --------------------------------------------------------------- review 守卫
def test_cost_adj_return_uses_longshort_not_market_mean():
    """P0：成本闸必须作用在候选自己的多空组合收益上，不是全市场标签均值。"""
    from v5.capability.ops.observe import _longshort
    panel = _panel(t=80, n=8)
    rng = np.random.default_rng(11)
    # 两个质量明显不同的暴露：一个是强排序因子、一个是反序因子
    base = rng.normal(size=(1, panel.shape[1]))
    score = np.tile(np.argsort(np.argsort(base[0])) / panel.shape[1], (panel.shape[0], 1))
    good = np.tile(base, (panel.shape[0], 1))
    bad = -good
    g_ret = _longshort(good, np.isfinite(good), score, np.isfinite(score))[2]
    b_ret = _longshort(bad, np.isfinite(bad), score, np.isfinite(score))[2]
    assert g_ret > 0 and b_ret < 0, (g_ret, b_ret)  # 两者不同且有正负区分
    # observe 的 cost_adj_return 来自 _longshort，不是 np.mean(score[mask])
    out = run(_call(_candidate_set([("good", good)]), panel, _label(score)))
    row = out["per_candidate"][0]["metrics"]
    assert abs(row["cost_adj_return"] - g_ret) < 1e-9
    assert row["cost_adj_return"] != np.mean(score)


def test_stage_a_rejected_candidate_is_watch_not_reject():
    """P1-1：Stage A 拒绝的候选不得被 verdict 判 reject 冒充已检验。"""
    from v5.capability.ops import verdict as v
    panel = _panel()
    rng = np.random.default_rng(13)
    # 近乎常数的暴露 → |IC|≈0，被 Stage A 拒绝
    values = np.ones(panel.shape) + rng.normal(size=panel.shape) * 1e-9
    label = _label(rng.normal(size=panel.shape))
    obs = run(_call(_candidate_set([("weak", values)]), panel, label, min_ic=0.05))
    row = obs["per_candidate"][0]
    assert row["stage_a_reasons"]
    assert row["metrics"]["p_value"] is None  # 未检验：p 值必须缺失
    # 即使 family_cumulative 里塞一个极小的 p，也不得把这个候选判死
    vcall = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.99, "fdr_max": 0.001, "cost_adj_min": 1.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "2" * 64, data=obs),),
        runtime_facts={"n_trials": 1, "family_cumulative": [0.0001]},
    )
    vout = run(vcall)
    vrow = vout["per_candidate"][0]
    assert vrow["decision"] == "watch"
    assert vrow["reason_codes"] == ["insufficient_n"]


def test_block_bootstrap_centers_null_distribution():
    """P1-2：强因子的 block_bootstrap 零分布不得自带 alpha。"""
    t, n = 120, 10
    times = START + np.arange(t, dtype="int64") * DAY_NS
    rng = np.random.default_rng(21)
    # 因子与标签强相关（稳定的正 IC）→ 观测 |t| 很大
    values = rng.normal(size=(t, n))
    score = values + rng.normal(0, 0.3, (t, n))  # 与暴露同步
    panel = Panel(times=times, symbols=tuple(f"S{j}" for j in range(n)),
                  fields={"close": np.abs(values) + 1})
    out = run(_call(_candidate_set([("strong", values)]), panel, _label(score),
                    null_model="block_bootstrap", n_perm=50, min_ic=0.0))
    row = out["per_candidate"][0]
    assert not row["stage_a_reasons"]
    # 中心化后零分布的 |t| 应远小于观测 |t|；未中心化时两者接近（p≈0.5）
    obs_t = abs(row["metrics"]["hac_t"])
    null_med = np.median(row["null_distribution"])
    assert null_med < obs_t, (null_med, obs_t)
    assert row["metrics"]["empirical_p"] < 0.2, row["metrics"]["empirical_p"]


def test_pvalue_df_uses_ic_timepoints_not_grid_cells():
    """P1-3：HAC t 的 p 值自由度取 IC 时点数 T-1，不是格子数 T×N。"""
    from v5.capability.inference import pvalue_from_t
    panel = _panel(t=60, n=10)  # 格子数 = 600，时点数 = 60
    rng = np.random.default_rng(31)
    values = rng.normal(size=panel.shape)
    score = rng.normal(size=panel.shape)
    label = _label(score)
    out = run(_call(_candidate_set([("c", values)]), panel, label, n_perm=5))
    row = out["per_candidate"][0]["metrics"]
    hac_t = row["hac_t"]
    # 期望：df = T-1 = 59，不是 effective_n-1 = 599
    expected = pvalue_from_t(hac_t, df=59)
    wrong = pvalue_from_t(hac_t, df=599)
    assert abs(row["p_value"] - expected) < 1e-9
    # 两者必须真的不同，否则这条守卫没有区分力
    assert abs(expected - wrong) > 1e-6


def test_dsr_uses_per_period_sr_and_true_nobs():
    """复核 3：DSR 的 sr 必须是未年化每期 Sharpe，n_obs 是多空真实期数 T-1，
    不是年化 sharpe、也不是面板格子数。"""
    panel = _panel(t=80, n=8)
    rng = np.random.default_rng(37)
    values = rng.normal(size=panel.shape)
    score = rng.normal(size=panel.shape)
    from v5.capability.ops.observe import _longshort
    _, annual, _, per_period, n_periods, _ls = _longshort(
        values, np.isfinite(values), score, np.isfinite(score))
    out = run(_call(_candidate_set([("c", values)]), panel, _label(score)))
    m = out["per_candidate"][0]["metrics"]
    assert abs(m["sr"] - per_period) < 1e-9
    assert m["sr_n_obs"] == n_periods
    assert abs(m["gross_sharpe"] - annual) < 1e-9
    assert per_period != annual  # 两者确有区分


def test_stage_a_candidate_excluded_from_bh_family():
    """复核 2：Stage A 未检验候选的 p=None 不得当 1.0 塞进 BH 族，
    也不得在 statistics 里露出 p_value=1.0（没检验就没有 p 值）。"""
    from v5.capability.ops import verdict as v
    # 直接构造 observation_set，隔离 verdict 的族校正逻辑（不依赖 observe 粗筛）
    weak_m = {
        "stage_a_pass": False, "p_value": None, "hac_t": None,
        "cost_adj_return": 0.0, "sr": None, "sr_n_obs": 2,
    }
    good_m = {
        "stage_a_pass": True, "p_value": 0.02, "hac_t": 3.0,
        "cost_adj_return": 0.5, "sr": 0.3, "sr_n_obs": 100,
        "null_distribution": [0.1, 0.2, 0.3], "empirical_p": 0.02,
    }
    obs = {"per_candidate": [
        {"candidate_id": "weak", "metrics": weak_m},
        {"candidate_id": "good", "metrics": good_m},
    ]}
    vcall = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.0, "fdr_max": 1.0, "cost_adj_min": 0.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "2" * 64, data=obs),),
        runtime_facts={"n_trials": 2, "family_cumulative": []},
    )
    vout = run(vcall)
    by_id = {r["candidate_id"]: r for r in vout["per_candidate"]}
    assert by_id["weak"]["decision"] == "watch"
    assert "p_value" not in by_id["weak"]["statistics"]
    assert by_id["weak"]["statistics"]["tested"] is False
    assert by_id["good"]["statistics"]["tested"] is True
    assert "p_value" in by_id["good"]["statistics"]
    # 弱候选不进族：BH 族只有 1 个候选 + n_trials 补 1.0 → pool=[0.02, 1.0]，
    # p_adj = 0.02*2/1 = 0.04；若 weak 的 1.0 混入族，pool=[0.02,1.0,1.0]，
    # p_adj = 0.02*3/1 = 0.06。用 0.04 区分两者。
    assert abs(by_id["good"]["statistics"]["p_adj"] - 0.04) < 1e-9


# ------------------------------------------------- 经济等价判重（s0/04 §3.6）
def test_dedup_default_off_marks_every_candidate_independent():
    """缺省 `corr=1.0` 关闭：既有计划逐位不变，且产物里如实写 `enabled=False`。"""
    panel = _panel()
    base = np.cumsum(np.random.default_rng(1).normal(size=panel.shape), axis=0)
    other = np.cumsum(np.random.default_rng(2).normal(size=panel.shape), axis=0)
    cs = _candidate_set([("c1", base), ("c2", base * 2.0), ("c3", other)])
    out = run(_call(cs, panel, _label(base)))
    red = out["redundancy"]
    assert red["enabled"] is False
    assert red["n_candidates"] == 3 and red["n_effective"] == 3
    assert red["clusters"] == [] and red["max_pair_corr"] is None
    assert all(row["metrics"]["redundant_of"] is None for row in out["per_candidate"])


def test_dedup_clusters_affine_equivalent_scores():
    """`x` 与 `2x+1` 是同一个信号：n_effective 必须把它们算成 1 个。

    内容身份（`H(规范化 AST)`）拦不住这一对——AST 不同、candidate_id 不同，
    于是各计一次 trial，`n_trials` 虚高。这里守的正是那条记账错误的可见化。
    """
    panel = _panel()
    base = np.cumsum(np.random.default_rng(1).normal(size=panel.shape), axis=0)
    other = np.cumsum(np.random.default_rng(2).normal(size=panel.shape), axis=0)
    cs = _candidate_set([
        ("c1", base),
        ("c2", base * 2.0 + 1.0),   # 逐时点秩完全相同 → 相关 = 1
        ("c3", other),              # 独立
    ])
    out = run(_call(cs, panel, _label(base), dedup={"corr": 0.9}))
    red = out["redundancy"]
    assert red["enabled"] is True
    assert red["threshold"] == 0.9
    assert red["n_candidates"] == 3
    assert red["n_effective"] == 2, red
    by_id = {r["candidate_id"]: r["metrics"]["redundant_of"]
             for r in out["per_candidate"]}
    dup = [cid for cid, rep in by_id.items() if rep is not None]
    assert len(dup) == 1 and dup[0] in {"c1", "c2"}
    assert by_id[dup[0]] in {"c1", "c2"} and by_id[dup[0]] != dup[0]
    assert by_id["c3"] is None
    # 代表与簇成员必须自洽：被标记者的 representative 自己不再指向别人
    assert by_id[by_id[dup[0]]] is None


def test_dedup_ignores_candidates_that_failed_stage_a():
    """未过粗筛的候选不进 `n_effective`：它是「没观测」，不是「已判定独立」。"""
    panel = _panel()
    base = np.cumsum(np.random.default_rng(1).normal(size=panel.shape), axis=0)
    # 只保留 1/8 的格有效 → coverage≈0.125，被 min_coverage=0.9 粗筛掉。
    weak = np.full(panel.shape, np.nan)
    weak[:, :1] = base[:, :1]
    cs = _candidate_set([("c1", base), ("weak", weak)])
    out = run(_call(cs, panel, _label(base), min_cov=0.9, dedup={"corr": 0.9}))
    red = out["redundancy"]
    assert red["n_candidates"] == 1
    assert red["n_effective"] == 1
    assert red["n_unobserved"] == 1
    by_id = {r["candidate_id"]: r for r in out["per_candidate"]}
    assert by_id["weak"]["metrics"]["redundant_of"] is None


@pytest.mark.parametrize(
    ("dedup", "code"),
    [
        ({"corr": 1.5}, E1004),
        ({"corr": -0.1}, E1004),
        ({"method": "kendall"}, E1004),
        ({"rep_by": "sharpe_ratio"}, E1004),
    ],
)
def test_dedup_rejects_bad_params(dedup, code):
    panel = _panel()
    base = np.cumsum(np.random.default_rng(1).normal(size=panel.shape), axis=0)
    cs = _candidate_set([("c1", base), ("c2", base * 2.0)])
    assert _code(lambda: run(_call(cs, panel, _label(base), dedup=dedup))) == code


def test_verdict_watches_redundant_candidates_without_rejecting_them():
    """重复因子判 `watch` + `redundant`，且 `n_effective_trials` 只作诊断。

    `reject` 会把它记成「被拒绝的坏因子」并污染族失败计数；`watch` 的后果
    只是不进组合、不占独立发现名额。判据门槛仍用 `n_trials`——用 `n_effective`
    会降低 `expected_max_sr`，属于放宽判据。
    """
    from v5.capability.inference import expected_max_sr

    good = {
        "stage_a_pass": True, "hac_t": 6.0, "p_value": 1e-6,
        "cost_adj_return": 0.004, "sr": 0.30, "sr_n_obs": 300,
        "gross_return": 0.005, "turnover_per_period": 0.2,
        "empirical_p": 0.01, "redundant_of": None,
    }
    dup = {**good, "redundant_of": "c1"}
    obs = {
        "per_candidate": [
            {"candidate_id": "c1", "metrics": good},
            {"candidate_id": "c2", "metrics": dup},
        ],
        "redundancy": {"enabled": True, "n_candidates": 2, "n_effective": 1},
    }
    vcall = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.0, "fdr_max": 1.0, "cost_adj_min": 0.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "2" * 64, data=obs),),
        runtime_facts={"n_trials": 9, "family_cumulative": []},
    )
    by_id = {r["candidate_id"]: r for r in run(vcall)["per_candidate"]}
    assert by_id["c2"]["decision"] == "watch"
    assert by_id["c2"]["reason_codes"] == ["redundant"]
    assert by_id["c2"]["statistics"]["redundant_of"] == "c1"
    # 两个候选的判据口径一致：都是 n_trials=9，n_effective 只落盘
    for cid in ("c1", "c2"):
        assert by_id[cid]["statistics"]["n_trials"] == 9
        assert by_id[cid]["statistics"]["n_effective_trials"] == 1
    # DSR 门槛按 n_trials 算，不按 n_effective
    sigma = 1.0 / np.sqrt(300 - 1)
    assert abs(by_id["c1"]["statistics"]["dsr_benchmark_sr"]
               - expected_max_sr(9, sigma=sigma)) < 1e-12


def test_verdict_hard_failure_beats_redundant():
    """重复 + DSR 失败 → reject：`redundant` 是软原因，不得掩盖硬失败。"""
    bad = {
        "stage_a_pass": True, "hac_t": 0.1, "p_value": 0.9,
        "cost_adj_return": -0.01, "sr": 0.0, "sr_n_obs": 300,
        "gross_return": -0.0001, "turnover_per_period": 0.5,
        "empirical_p": 0.9, "redundant_of": "c1",
    }
    obs = {"per_candidate": [{"candidate_id": "c2", "metrics": bad}],
           "redundancy": {"n_effective": 1}}
    vcall = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.05, "fdr_max": 0.05, "cost_adj_min": 0.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "2" * 64, data=obs),),
        runtime_facts={"n_trials": 9, "family_cumulative": []},
    )
    rows = {r["candidate_id"]: r for r in run(vcall)["per_candidate"]}
    assert rows["c2"]["decision"] == "reject"
    assert "redundant" in rows["c2"]["reason_codes"]
    assert "cost_fail" in rows["c2"]["reason_codes"]


# ------------------------------------------------- 指标补全（D1 / D3）










# ------------------------------------------------- 指标补全（D1 / D3）
def test_pearson_ic_separates_linear_from_monotone():
    """`ic` 是 rank IC，`pearson_ic` 是线性 IC：单调非线性因子必须分叉。

    只测秩相关会把 `y = x³` 判成完美因子；能不能用线性权重合成，只有
    Pearson 说了算。
    """
    panel = _panel(t=60, n=10)
    x = np.random.default_rng(5).normal(size=panel.shape)
    y = x ** 3
    cs = _candidate_set([("c1", x)])
    out = run(_call(cs, panel, _label(y), metrics=["ic", "pearson_ic"], n_perm=5))
    m = out["per_candidate"][0]["metrics"]
    assert m["ic"] == pytest.approx(1.0, abs=1e-9)      # 秩完全一致
    assert m["pearson_ic"] < 0.9                         # 线性相关明显低
    assert out["method_ids"]["pearson_ic"] == "pearson@1"
    assert out["method_ids"]["ic"] == "spearman@1"


def test_pearson_ic_absent_unless_requested():
    panel = _panel(t=40, n=6)
    x = np.random.default_rng(6).normal(size=panel.shape)
    out = run(_call(_candidate_set([("c1", x)]), panel, _label(x), n_perm=5))
    assert "pearson_ic" not in out["per_candidate"][0]["metrics"]


def test_skew_and_kurtosis_come_from_the_longshort_series():
    """DSR 的肥尾修正输入必须有生产方，且与 `sr` 同源。

    首版 `net_skew`/`net_kurtosis` 全仓没有产出方，DSR 恒按正态 (0,3) 算——
    修正项名义存在、实际空转。
    """
    panel = _panel(t=80, n=8)
    rng = np.random.default_rng(7)
    x = np.cumsum(rng.normal(size=panel.shape), axis=0)
    y = rng.standard_t(df=3, size=panel.shape) * 0.01
    out = run(_call(_candidate_set([("c1", x)]), panel, _label(y), n_perm=5))
    m = out["per_candidate"][0]["metrics"]
    assert "skew" in m and "kurtosis" in m
    assert np.isfinite(m["skew"]) and np.isfinite(m["kurtosis"])
    # 多空组合是 8 个标的的加权平均，厚尾已被聚合抹平——这里只守「真的在算
    # 这条序列的形状」，形状本身的正确性由原语的单测负责。
    assert m["kurtosis"] != 3.0
    assert out["method_ids"]["skew"] == "sample_skew@1"
    assert m["sr"] is not None


def test_sample_shape_matches_scipy_on_fat_tails():
    """原语本身：t(3) 的峰度必须显著高于正态，且与 scipy 逐位一致。"""
    from scipy import stats as st

    from v5.capability.inference import sample_shape

    x = np.random.default_rng(11).standard_t(df=3, size=20000)
    skew, kurt = sample_shape(x)
    assert skew == pytest.approx(float(st.skew(x, bias=False)), rel=1e-12)
    assert kurt == pytest.approx(float(st.kurtosis(x, fisher=False, bias=False)),
                                 rel=1e-12)
    assert kurt > 5.0  # t(3) 理论超额峰度 6
    # 样本不足 / 零方差 → 正态默认值，不抛不 nan
    assert sample_shape(np.array([1.0, 2.0])) == (0.0, 3.0)
    assert sample_shape(np.ones(10)) == (0.0, 3.0)


def test_verdict_uses_skew_kurtosis_in_dsr():
    """同一份证据，填了肥尾的 DSR 必须按传入的形状算，而不是隐式正态。"""
    from v5.capability.inference import deflated_sharpe_ratio, expected_max_sr

    base = {
        "stage_a_pass": True, "hac_t": 4.0, "p_value": 0.001,
        "cost_adj_return": 0.002, "sr": 0.12, "sr_n_obs": 250,
        "gross_return": 0.002, "turnover_per_period": 0.1,
        "empirical_p": 0.01, "redundant_of": None,
    }
    sigma = 1.0 / np.sqrt(249)
    bench = expected_max_sr(20, sigma=sigma)
    fat = {**base, "skew": -1.5, "kurtosis": 12.0}
    obs = {"per_candidate": [{"candidate_id": "c1", "metrics": fat}]}
    vcall = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.0, "fdr_max": 1.0, "cost_adj_min": 0.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "3" * 64, data=obs),),
        runtime_facts={"n_trials": 20, "family_cumulative": []},
    )
    got = run(vcall)["per_candidate"][0]["statistics"]["dsr"]
    expect = deflated_sharpe_ratio(0.12, 250, 20, sr_benchmark=bench,
                                   skewness=-1.5, kurtosis=12.0)
    assert got == pytest.approx(expect, rel=1e-12)


# ------------------------------------------------- D2 · 分层单调性（groups）




















# ------------------------------------------------- D2 · 分层单调性（groups）
def _rank_returns(t=60, n=10):
    """构造 rank 形状的收益：每行是 0..n-1 的等距台阶（可预测、无噪声）。"""
    base = np.arange(n, dtype="float64")
    return np.tile(base, (t, 1))


def test_group_returns_absent_unless_requested():
    panel = _panel()
    raw = _rank_returns(t=panel.shape[0], n=panel.shape[1])
    out = run(_call(_candidate_set([("c1", raw)]), panel, _label(raw)))
    metrics = out["per_candidate"][0]["metrics"]
    assert "group_returns" not in metrics
    assert "monotonicity" not in metrics
    assert "group_periods" not in metrics


def test_group_returns_are_monotone_when_score_matches_return():
    panel = _panel()
    raw = _rank_returns(t=panel.shape[0], n=panel.shape[1])
    out = run(_call(_candidate_set([("c1", raw)]), panel, _label(raw),
                    metrics=["group_returns", "monotonicity"], groups=5))
    metrics = out["per_candidate"][0]["metrics"]
    groups = metrics["group_returns"]
    assert len(groups) == 5
    assert groups == sorted(groups)
    assert metrics["monotonicity"] == pytest.approx(1.0)
    # 台阶等距 → 组均值应随组序线性上升；这里只钉住方向与端点关系
    assert groups[-1] > groups[0]
    assert metrics["group_periods"] == panel.shape[0]


def test_group_returns_flip_for_inverted_score():
    panel = _panel()
    raw = _rank_returns(t=panel.shape[0], n=panel.shape[1])
    out = run(_call(_candidate_set([("c1", -raw)]), panel, _label(raw),
                    metrics=["group_returns", "monotonicity"], groups=5))
    metrics = out["per_candidate"][0]["metrics"]
    assert metrics["group_returns"] == sorted(metrics["group_returns"], reverse=True)
    assert metrics["monotonicity"] == pytest.approx(-1.0)


def test_group_returns_expose_shape_that_rank_ic_cannot_see():
    """同一个 rank IC 下，组收益形状可以完全不同——这才是要分组收益的原因。

    面板 A：收益是等距台阶；面板 B：把最高分组的收益放大十倍。B 是 A 的**单调
    变换**，Spearman IC 必然逐位相同，`group_returns` 与 `gross_return` 却差一个
    量级。只看 IC 会把两者当成同一个因子。
    """
    panel = _panel()
    t_len, n_sym = panel.shape
    raw_a = _rank_returns(t=t_len, n=n_sym)
    raw_b = raw_a.copy()
    raw_b[:, -2:] *= 10.0  # 最高两档（= 最高分组）收益放大十倍

    cs_a = _candidate_set([("a", raw_a)])
    cs_b = _candidate_set([("b", raw_b)])
    out_a = run(_call(cs_a, panel, _label(raw_a),
                      metrics=["ic", "group_returns", "monotonicity"], groups=5))
    out_b = run(_call(cs_b, panel, _label(raw_b),
                      metrics=["ic", "group_returns", "monotonicity"], groups=5))
    ma = out_a["per_candidate"][0]["metrics"]
    mb = out_b["per_candidate"][0]["metrics"]
    assert ma["ic"] == pytest.approx(mb["ic"])
    assert ma["monotonicity"] == pytest.approx(mb["monotonicity"])
    assert mb["group_returns"][-1] > 5 * ma["group_returns"][-1]
    assert mb["gross_return"] > ma["gross_return"]


def test_monotonicity_requires_explicit_group_count():
    panel = _panel()
    raw = _rank_returns(t=panel.shape[0], n=panel.shape[1])
    call = _call(_candidate_set([("c1", raw)]), panel, _label(raw),
                 metrics=["monotonicity"])
    assert _code(lambda: run(call)) == "E1008"


def test_group_count_above_symbol_count_is_rejected():
    panel = _panel(n=8)
    raw = _rank_returns(t=panel.shape[0], n=panel.shape[1])
    call = _call(_candidate_set([("c1", raw)]), panel, _label(raw),
                 metrics=["monotonicity"], groups=9)
    assert _code(lambda: run(call)) == "E1004"


@pytest.mark.parametrize("bad", [1, 0, -3, 2.5, "5", True])
def test_group_count_rejects_bad_values(bad):
    panel = _panel()
    raw = _rank_returns(t=panel.shape[0], n=panel.shape[1])
    call = _call(_candidate_set([("c1", raw)]), panel, _label(raw), groups=bad)
    assert _code(lambda: run(call)) == "E1004"


def test_group_stats_skip_periods_too_narrow_to_split():
    """某期有效标的不足 G 组时整期跳过，并把可用期数如实报出来。"""
    from v5.capability.ops.observe import _group_stats

    values = np.array([[0.0, 1.0, 2.0, np.nan],
                       [0.0, 1.0, 2.0, 3.0]])
    valid = np.isfinite(values)
    raw = np.array([[1.0, 2.0, 3.0, 4.0],
                    [1.0, 2.0, 3.0, 4.0]])
    label_valid = np.ones_like(valid)
    means, mono, periods = _group_stats(values, valid, raw, label_valid, 4, 1)
    assert periods == 1  # 第一行只有 3 个有效标的，分不出 4 组
    assert len(means) == 4
    assert mono == pytest.approx(1.0)


def test_group_stats_report_unmeasurable_instead_of_zero():
    """一期都分不出组时返回空列表，不用 0 冒充「测过了，不单调」。"""
    from v5.capability.ops.observe import _group_stats

    values = np.array([[0.0, 1.0, 2.0]])
    valid = np.isfinite(values)
    raw = np.array([[1.0, 2.0, 3.0]])
    means, mono, periods = _group_stats(values, valid, raw, np.ones_like(valid), 5, 1)
    assert (means, mono, periods) == ([], 0.0, 0)


# ------------------------------------------------------ D4 · IC 衰减曲线
def _alternating_panel(t_len=80, n=8, eps=0.01):
    """价格按「奇偶 bar 反向」的确定序列演化，幅度逐标的递增。

    这样构造的好处：1 根 bar 上收益正是 `sign*amp`，与暴露完全同序（IC=+1）；
    2 根 bar 上两段反向收益相加抵消，只剩二阶项 `-(amp·eps)²`，符号逐行交替，
    平均 IC 塌到约 0；3 根 bar 又回到同序。**同一条曲线在三个跨度上完全不同**，
    正是"每个跨度必须真的重算未来收益"的证据——只复制 h=1 的实现只能给出一条
    平线，那种实现过不了这个测试。
    """
    amp = np.arange(1, n + 1, dtype="float64") / n
    signs = np.where(np.arange(t_len) % 2 == 0, 1.0, -1.0)
    step = signs[:, None] * amp[None, :] * eps
    close = 100.0 * np.cumprod(1.0 + step, axis=0)
    panel = Panel(times=START + np.arange(t_len, dtype="int64") * DAY_NS,
                  symbols=tuple(f"S{j}" for j in range(n)),
                  fields={"close": close})
    # 暴露 = h=1 那笔交易的真实收益方向（完美预知）。`entry=next_close` 下
    # 进场在 t+1、出场在 t+2，所以 h=1 的收益是 `step[t+2]`——写成 `step[t+1]`
    # 会让整条曲线符号整体翻转（符号错误恰好是这类曲线最该被盯住的东西）。
    values = np.zeros((t_len, n), dtype="float64")
    values[:-2] = np.roll(signs, -2)[:-2, None] * amp[None, :]
    return panel, values


def test_ic_decay_recomputes_forward_returns_per_horizon():
    panel, values = _alternating_panel()
    out = run(_call(_candidate_set([("c1", values)]), panel, _label(values),
                    metrics=["ic_decay"], ic_decay=[1, 2, 3]))
    decay = out["per_candidate"][0]["metrics"]["ic_decay"]
    assert set(decay) == {"1", "2", "3"}
    assert decay["1"] > 0.9          # 1 根 bar：方向信号，完美同序
    assert abs(decay["2"]) < 0.2     # 2 根 bar：两段反向收益抵消，几乎无预测力
    assert decay["3"] > 0.9          # 3 根 bar：又回到同序


def test_ic_decay_absent_unless_requested():
    panel, values = _alternating_panel(t_len=40, n=6)
    out = run(_call(_candidate_set([("c1", values)]), panel, _label(values)))
    assert "ic_decay" not in out["per_candidate"][0]["metrics"]
    # 给了跨度却没请求指标 = 配了不用：产物里也不得留一个 None 字段
    out2 = run(_call(_candidate_set([("c1", values)]), panel, _label(values),
                     ic_decay=[1, 5]))
    assert "ic_decay" not in out2["per_candidate"][0]["metrics"]


def test_ic_decay_requires_explicit_horizons():
    panel, values = _alternating_panel(t_len=40, n=6)
    call = _call(_candidate_set([("c1", values)]), panel, _label(values),
                 metrics=["ic_decay"])
    assert _code(lambda: run(call)) == "E1008"


@pytest.mark.parametrize("bad", [[], [0], [-2], [1.5], [True], "5", 5, {"h": 1}])
def test_ic_decay_rejects_bad_values(bad):
    panel, values = _alternating_panel(t_len=40, n=6)
    call = _call(_candidate_set([("c1", values)]), panel, _label(values),
                 ic_decay=bad)
    assert _code(lambda: run(call)) == "E1004"


def test_ic_decay_deduplicates_horizons_in_order():
    panel, values = _alternating_panel(t_len=40, n=6)
    out = run(_call(_candidate_set([("c1", values)]), panel, _label(values),
                    metrics=["ic_decay"], ic_decay=[2, 1, 2, 1]))
    assert list(out["per_candidate"][0]["metrics"]["ic_decay"]) == ["2", "1"]
