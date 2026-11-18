"""S3-M2 · verdict 唯一裁决 + inference 统计原语。

两组验收：
1. **逐位对拍**：inference 的每个原语与 V4 `core.evaluation.significance`
   的对应函数在同一输入上给出相同数值（含三处已修 bug 的语义）。
2. **契约行为**：decision 三值、reason_codes 可枚举、n_trials 来源、
   BH 族、DSR 门槛、成本/零分布闸、空集与重复候选拒绝。
"""

from __future__ import annotations

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.inference import (
    bh_fdr, by_fdr, deflated_sharpe_ratio, empirical_p, expected_max_sr,
    holm, ic_series, newey_west_tstat, pvalue_from_t,
)
from capability.runtime import reset_impls
from contracts.errors import E2003, E3003, FlowError

# V4 生产实现——逐位对拍的基准（不是 mock）。
# 冻结在 `tests/reference/v4/`：V4 已不进生产，但它作为独立数值 oracle
# 仍然有效——V5 的统计原语必须与它逐位一致（含三处已修 bug 的语义）。
from reference.v4 import significance as v4


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


# --------------------------------------------------------------- 对拍：原语
def test_inference_matches_v4_bh_holm():
    p = [0.01, 0.04, 0.03, 0.5, 0.2, 0.001]
    np.testing.assert_allclose(bh_fdr(p), v4.bh_fdr(p), rtol=0, atol=0)
    np.testing.assert_allclose(holm(p), v4.holm(p), rtol=0, atol=0)


def test_inference_matches_v4_by_fdr():
    p = [0.01, 0.04, 0.03, 0.5, 0.2, 0.001]
    # V4 与 V5 都用 scipy.stats.false_discovery_control(method="by")；
    # 对拍 scipy 本体即证明两边同源、版本未漂移（statsmodels 只作 dev 参考）。
    from scipy.stats import false_discovery_control
    np.testing.assert_allclose(by_fdr(p), false_discovery_control(p, method="by"),
                               rtol=0, atol=0)


def test_inference_matches_v4_empirical_p():
    null = np.linspace(0.5, 5.0, 60)
    for value in (3.46, 0.0, 4.74, 100.0):
        assert empirical_p(value, null) == v4.empirical_p(value, null)
    # 非有限观测 → 1.0，不给 NaN t 配假显著
    assert empirical_p(float("nan"), null) == 1.0
    assert empirical_p(3.0, []) == 1.0


def test_inference_matches_v4_dsr():
    for sharpe, n, trials in [(0.05, 1187, 255), (0.09, 610, 787), (0.2, 300, 1)]:
        assert deflated_sharpe_ratio(sharpe, n, trials) == v4.deflated_sharpe_ratio(
            sharpe, n, trials)
    # E-01：var_term<=0 fail-closed → 0.0（不是虚假的 1.0）
    assert deflated_sharpe_ratio(10.0, 100, 5, skewness=10.0, kurtosis=1.0) == 0.0


def test_inference_matches_v4_dsr_with_sigma_sr():
    assert deflated_sharpe_ratio(0.05, 1187, 255, sigma_sr=0.3) == \
        v4.deflated_sharpe_ratio(0.05, 1187, 255, sigma_sr=0.3)


def test_inference_matches_v4_hac_t_and_ic():
    rng = np.random.default_rng(11)
    f = rng.normal(size=(120, 6))
    r = rng.normal(size=(120, 6))
    np.testing.assert_allclose(ic_series(f, r), v4.ic_series(f, r), rtol=0, atol=0)
    assert newey_west_tstat(ic_series(f, r)) == v4.newey_west_tstat(v4.ic_series(f, r))
    assert newey_west_tstat([0.1] * 3) == v4.newey_west_tstat([0.1] * 3)


def test_dsr_bar_rises_with_trials_not_a_knob():
    # n_trials=255 → 门槛年化 ~1.53（252 口径，已知答案；365 口径为 ~1.85）
    bar = expected_max_sr(255, sigma=1 / np.sqrt(1186))
    assert bar * np.sqrt(252) == pytest.approx(1.53, abs=0.05)
    assert expected_max_sr(5000) > expected_max_sr(255)


# --------------------------------------------------------------- verdict 调用
def _obs(rows):
    return {"per_candidate": rows, "method_ids": {"hac_t": "nw@1"}}


def _row(cid, *, p=0.01, t=3.0, cost=0.001, sr=0.05, nobs=1000,
         null=None, emp=None, **extra):
    m = {
        "hac_t": t, "p_value": p, "cost_adj_return": cost,
        "sr": sr, "sr_n_obs": nobs, "ir": 0.2, "n_obs": nobs,
    }
    if null is not None:
        m["null_distribution"] = list(null)
    if emp is not None:
        m["empirical_p"] = emp
    m.update(extra)
    return {"candidate_id": cid, "metrics": m}


def _call(observation, *, facts=None, **params):
    base = {"dsr_min": 0.0, "fdr_max": 0.1, "cost_adj_min": 0.0, "family": "f1"}
    base.update(params)
    facts = {"n_trials": 255, **(facts or {})}
    return Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params=base,
        inputs=(InputRef("observation_set", "sha256:" + "0" * 64, data=observation),),
        runtime_facts=facts,
    )


def test_verdict_passes_genuinely_strong_candidate():
    # n_trials=10（小族，A4 只补 9 个历史位）：p=0.001 → BH 后 0.01，过 0.05 闸。
    # null 全低、观测 t=6 → empirical_p=1/61；sr=0.3 过 DSR_floor=0。
    null = np.linspace(0.0, 2.0, 60)
    out = run(_call(_obs([_row("c1", p=0.001, t=6.0, cost=0.01,
                              null=null, sr=0.3, nobs=100)]),
                    facts={"n_trials": 10}, dsr_min=0.0, fdr_max=0.05))
    assert out["n_trials_used"] == 10
    row = out["per_candidate"][0]
    assert row["decision"] == "pass", row
    assert row["reason_codes"] == []


def test_verdict_a4_safety_net_prevents_single_trial_gaming():
    # 只声明 1 个候选却写 255 次试验：A4 用 1.0 补齐，p=0.001 被修正到 0.255
    # ——"只判一个却声明很大 n_trials"不能让 fdr 闸坍缩成 1 而假通过。
    out = run(_call(_obs([_row("c1", p=0.001, t=6.0, cost=0.01,
                              null=np.linspace(0, 2, 60), sr=0.3)]),
                    dsr_min=0.0, fdr_max=0.05))
    assert out["per_candidate"][0]["statistics"]["p_adj"] == pytest.approx(0.255)
    assert "fdr_fail" in out["per_candidate"][0]["reason_codes"]


def test_verdict_rejects_weak_candidate_with_all_reasons():
    null = np.linspace(5.0, 9.0, 60)  # 观测 t 远低于零分布 → null_fail
    out = run(_call(_obs([_row("weak", p=0.9, t=0.2, cost=-0.01,
                              null=null, sr=-0.1, nobs=100)]),
                    dsr_min=0.99, fdr_max=0.05, cost_adj_min=0.0))
    row = out["per_candidate"][0]
    assert row["decision"] == "reject"
    assert set(row["reason_codes"]) == {"dsr_fail", "fdr_fail", "cost_fail", "null_fail"}


def test_verdict_watches_when_evidence_incomplete():
    # 缺 p_value → insufficient_n → watch，不是 fail-open
    row = _row("c1")
    del row["metrics"]["p_value"]
    out = run(_call(_obs([row])))
    assert out["per_candidate"][0]["decision"] == "watch"
    assert out["per_candidate"][0]["reason_codes"] == ["insufficient_n"]


def test_verdict_watches_when_no_null_distribution():
    # 用小族让 fdr 通过，隔离出"缺零分布"这一条 → watch
    out = run(_call(_obs([_row("c1", p=0.001, t=6.0, cost=0.01, sr=0.3)]),
                    facts={"n_trials": 10}, dsr_min=0.0))
    assert out["per_candidate"][0]["decision"] == "watch"
    assert "insufficient_n" in out["per_candidate"][0]["reason_codes"]


def test_verdict_bh_family_uses_cumulative_p():
    # 本批 1 个 p=0.04；历史族里塞 9 个 1.0 → 调整后 p 变大，可能翻成 fdr_fail
    facts = {"n_trials": 10, "family_cumulative": [0.04] + [1.0] * 9}
    out = run(_call(_obs([_row("c1", p=0.04, t=3.0, cost=0.01,
                              null=np.linspace(0, 2, 60), sr=0.3)]),
                    facts=facts, dsr_min=0.0, fdr_max=0.05))
    assert out["per_candidate"][0]["statistics"]["p_adj"] > 0.04


def test_verdict_p_value_zero_not_promoted_to_one():
    out = run(_call(_obs([_row("c1", p=0.0, t=6.0, cost=0.01,
                              null=np.linspace(0, 2, 60), sr=0.3)]),
                    dsr_min=0.0))
    assert out["per_candidate"][0]["statistics"]["p_value"] == 0.0


def test_verdict_n_trials_from_facts_not_params():
    # 写进 params 是 schema 之外的字段 → E1003（不许把计数当旋钮）
    bad = _call(_obs([_row("c1")]))
    from dataclasses import replace
    call = replace(bad, params={**bad.params, "n_trials": 1})
    call = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params=call.params, inputs=call.inputs, runtime_facts=call.runtime_facts,
    )
    assert _code(lambda: run(call)) == "E1003"


def test_verdict_missing_n_trials_is_e2003():
    call = Call.create(
        capability="verdict", station="Verdict", impl="sentinel", impl_version="1",
        params={"dsr_min": 0.0, "fdr_max": 0.1, "cost_adj_min": 0.0, "family": "f1"},
        inputs=(InputRef("observation_set", "sha256:" + "0" * 64,
                         data=_obs([_row("c1")])),),
        runtime_facts={},
    )
    assert _code(lambda: run(call)) == E2003


def test_verdict_rejects_empty_and_duplicate_candidates():
    assert _code(lambda: run(_call(_obs([])))) == E3003
    dup = _obs([_row("c1"), _row("c1")])
    assert _code(lambda: run(_call(dup))) == E3003


def test_verdict_reason_codes_are_enumerable():
    out = run(_call(_obs([_row("weak", p=0.9, t=0.2, cost=-0.01, sr=-0.1)]),
                    dsr_min=0.99, fdr_max=0.05))
    codes = out["per_candidate"][0]["reason_codes"]
    allowed = {"dsr_fail", "fdr_fail", "cost_fail", "null_fail", "insufficient_n"}
    assert set(codes) <= allowed
    assert codes == sorted(codes, key=lambda r: ["dsr_fail", "fdr_fail", "cost_fail",
                                                  "null_fail", "insufficient_n"].index(r))
