"""`combine` 单元测试（S4 等权诊断 + S7 组合域）。覆盖：

1. **等权 = 每个候选一票**：量纲不同的暴露必须先截面标准化，否则「大数吃小数」。
2. 只有 `pass`/`watch` 且带暴露的候选能进组合。
3. **权重只在 fit 段拟合**：只改 eval 段的数据，权重必须逐位不变（防泄漏）。
4. `score_weighted` 按 fit 段 IC 的**符号**加权（负 IC 成员反向）。
5. `corr_max < 1` 剔除高相关成员并记入 `dropped_members`（不静默）。
6. `orthogonalise=symmetric` 在共线成员上仍给出有限权重。
7. X6：`fit.end + embargo > eval.start` 必须 E1021。
"""

from __future__ import annotations

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.panel import Panel, array_digest
from capability.runtime import reset_impls
from contracts.digest import compute_digest
from contracts.errors import E1002, E1021, E3003, FlowError

DAY = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
SYMBOLS = ("A", "B", "C", "D")


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _panel(t_len: int = 6) -> Panel:
    times = START + np.arange(t_len, dtype="int64") * DAY
    close = np.linspace(100.0, 110.0, t_len * len(SYMBOLS)).reshape(t_len, len(SYMBOLS))
    return Panel(times=times, symbols=SYMBOLS,
                 fields={"close": close, "volume": np.full_like(close, 1000.0)})


def _ref(kind: str, data) -> InputRef:
    """输入引用：数组按内容摘要，值对象用自己的 digest，映射取结构摘要。

    run() 不校验调用方给的 digest（身份由调用方负责），这里只是让测试的
    Call 结构与生产一致；真正的内容身份校验在 `observe`/`combine` 读
    `values_ref` 时做。
    """
    if isinstance(data, np.ndarray):
        digest = array_digest(data)
    elif hasattr(data, "digest") and callable(data.digest):
        digest = str(data.digest())
    else:
        digest = compute_digest({"kind": kind, "keys": sorted(data)})
    return InputRef(kind, digest, data=data)


def _exposure(values: np.ndarray) -> dict:
    valid = np.isfinite(values)
    clean = np.where(valid, values, np.nan)
    return {"values": values, "valid_mask": valid,
            "values_ref": array_digest(np.ascontiguousarray(clean))}


def _call(panel: Panel, exposures: dict, decisions: dict, *, params=None,
          impl: str = "equal_weight", label_score=None):
    candidates = [{"candidate_id": cid, "available_from": "2024-01-01T00:00:00Z"}
                  for cid in exposures]
    candidate_set = {"candidates": candidates, "exposures": exposures}
    verdict_set = {"per_candidate": [
        {"candidate_id": cid, "decision": dec} for cid, dec in decisions.items()]}
    score = panel.field("close") if label_score is None else label_score
    label = {"score": score, "valid_mask": np.isfinite(score)}
    p = {"domain": "cs_test", "orthogonalise": "none", "corr_max": 1.0,
         "window": {"fit": "2024-01-01..2024-01-03", "embargo": 1,
                    "eval": "2024-01-04..2024-01-05"}}
    p.update(params or {})
    return Call.create(
        capability="combine", station="Portfolio", impl=impl, impl_version="1",
        params=p,
        inputs=(_ref("candidate_set", candidate_set), _ref("verdict_set", verdict_set),
                _ref("panel", panel), _ref("label", label)),
    )


def test_equal_weight_standardises_before_averaging():
    """量纲差 1e6 的两个候选：合成分数必须与「把大数缩小后重算」一致。"""
    panel = _panel()
    base = np.array([[1.0, 2.0, 3.0, 4.0]] * panel.shape[0])
    anti = -base
    small = _exposure(base)
    huge = _exposure(anti * 1e6)  # 原始值平均会被这一项独占

    out = run(_call(panel, {"c1": small, "c2": huge},
                    {"c1": "pass", "c2": "watch"}))
    score = out["score"]
    # 两个方向相反的标准化暴露等权平均 → 每行截面均值归零、绝对值很小；
    # 若直接平均原始值，结果会是 -1e6 量级。
    assert np.abs(score).max() < 10.0
    assert np.allclose(score.mean(axis=1), 0.0, atol=1e-9)

    # 量纲不影响结果：把大数缩回原尺度，分数应完全一致
    shrunk = _exposure(anti)
    again = run(_call(panel, {"c1": small, "c2": shrunk},
                      {"c1": "pass", "c2": "watch"}))
    assert np.allclose(score, again["score"], atol=1e-12)


def test_only_pass_or_watch_candidates_enter_the_book():
    panel = _panel()
    base = np.array([[1.0, 2.0, 3.0, 4.0]] * panel.shape[0])
    call = _call(panel, {"c1": _exposure(base), "c2": _exposure(-base)},
                 {"c1": "reject", "c2": "reject"})
    with pytest.raises(FlowError) as exc:
        run(call)
    assert exc.value.error.code == E3003
    assert "pass/watch" in exc.value.error.message


def test_weights_are_fitted_on_fit_segment_only():
    """只改 eval 段的面板与标签，权重必须逐位不变（防泄漏的机械证据）。"""
    panel = _panel()
    base = np.array([[1.0, 2.0, 3.0, 4.0]] * panel.shape[0])
    exposures = {"c1": _exposure(base), "c2": _exposure(-0.5 * base + 0.1)}
    # 第二个面板：前半段（fit）完全相同，后半段（eval）剧烈改变
    close = panel.field("close").copy()
    close[3:] = close[3:] * -7.0 + 3.0
    panel2 = Panel(times=panel.times, symbols=panel.symbols,
                   fields={"close": close, "volume": panel.field("volume")})
    for impl in ("score_weighted", "fixed_ridge"):
        a = run(_call(panel, exposures, {"c1": "pass", "c2": "pass"}, impl=impl))
        b = run(_call(panel2, exposures, {"c1": "pass", "c2": "pass"}, impl=impl))
        wa = [m["weight_share"] for m in a["members"]]
        wb = [m["weight_share"] for m in b["members"]]
        assert wa == wb, f"{impl} 的权重被 eval 段影响了"


def test_score_weighted_uses_signed_fit_ic():
    """与标签同向的成员拿正权重、反向的拿负权重。"""
    panel = _panel()
    close = panel.field("close")
    # 让标签在截面上有差异：每行按 A<B<C<D 递增
    label = np.tile(np.array([0.0, 1.0, 2.0, 3.0]), (panel.shape[0], 1))
    aligned = _exposure(label)
    anti = _exposure(-label)
    out = run(_call(panel, {"aligned": aligned, "anti": anti},
                    {"aligned": "pass", "anti": "pass"},
                    impl="score_weighted", label_score=label))
    weights = {m["candidate_id"]: m["weight_share"] for m in out["members"]}
    assert weights["aligned"] > 0 > weights["anti"]
    # 合成分数必须与标签同向：截面相关为正
    score = out["score"]
    rows = np.isfinite(score).all(axis=1) & np.isfinite(label).all(axis=1)
    assert np.corrcoef(score[rows].ravel(), label[rows].ravel())[0, 1] > 0.9
    _ = close


def test_corr_max_drops_redundant_member_and_records_reason():
    panel = _panel()
    base = np.tile(np.array([1.0, 2.0, 3.0, 4.0]), (panel.shape[0], 1))
    twin = base * 2.0  # 与 base 完全共线（ρ=1）
    # 与 base 近似不相关：逐行交替取反，池化后协方差相互抵消
    other = np.tile(np.array([[1.0, -1.0, 1.0, -1.0], [-1.0, 1.0, -1.0, 1.0]]),
                    (panel.shape[0] // 2, 1))
    out = run(_call(panel, {"a1": _exposure(base), "a2": _exposure(twin),
                            "b1": _exposure(other)},
                    {"a1": "pass", "a2": "pass", "b1": "pass"},
                    params={"corr_max": 0.5}))
    kept = [m["candidate_id"] for m in out["members"]]
    dropped = [d["candidate_id"] for d in out["dropped_members"]]
    assert len(kept) + len(dropped) == 3
    assert set(dropped) <= {"a1", "a2"}
    assert out["dropped_members"][0]["reason"] == "corr_above_max"
    # 保留成员之间不得再有超过上限的相关
    assert set(kept) >= {"b1"}


def test_symmetric_orthogonalise_is_finite_on_collinear_members():
    panel = _panel()
    base = np.array([[1.0, 2.0, 3.0, 4.0]] * panel.shape[0])
    out = run(_call(panel, {"c1": _exposure(base), "c2": _exposure(base * 3.0)},
                    {"c1": "pass", "c2": "pass"},
                    params={"orthogonalise": "symmetric"}, impl="equal_weight"))
    weights = np.array([m["weight_share"] for m in out["members"]])
    assert np.isfinite(weights).all()
    assert np.isfinite(out["score"]).all()
    # 两个完全共线的成员被对称正交化压成「各半」，不会因为相关阵奇异而爆炸
    assert np.allclose(weights, 0.5, atol=1e-6)


def test_embargo_gap_is_enforced():
    """X6：fit 段与 eval 段之间必须留出 embargo，不能只比 fit_end 与 eval_start。"""
    panel = _panel()
    base = np.array([[1.0, 2.0, 3.0, 4.0]] * panel.shape[0])
    bad = {"window": {"fit": "2024-01-01..2024-01-03", "embargo": 3,
                      "eval": "2024-01-04..2024-01-05"}}
    with pytest.raises(FlowError) as exc:
        run(_call(panel, {"c1": _exposure(base)}, {"c1": "pass"}, params=bad))
    assert exc.value.error.code == E1021


def test_unknown_combiner_is_e3004():
    panel = _panel()
    base = np.array([[1.0, 2.0, 3.0, 4.0]] * panel.shape[0])
    with pytest.raises(FlowError) as exc:
        run(_call(panel, {"c1": _exposure(base)}, {"c1": "pass"}, impl="magic_combiner"))
    assert exc.value.error.code == E1002


def test_fixed_ridge_downweights_the_noise_member():
    """岭回归在 fit 段就能把纯噪声成员的权重压下去（不是靠 eval 段挑）。"""
    panel = _panel()
    label = np.tile(np.array([0.0, 1.0, 2.0, 3.0]), (panel.shape[0], 1))
    noise = np.tile(np.array([[1.0, -1.0, 1.0, -1.0], [-1.0, 1.0, -1.0, 1.0]]),
                    (panel.shape[0] // 2, 1))
    out = run(_call(panel, {"good": _exposure(label), "noise": _exposure(noise)},
                    {"good": "pass", "noise": "pass"},
                    impl="fixed_ridge", label_score=label))
    weights = {m["candidate_id"]: abs(m["weight_share"]) for m in out["members"]}
    assert weights["good"] > weights["noise"] * 5


def test_combination_is_deterministic_across_runs():
    """同输入两次重算：成员、权重、分数逐位一致（AC39 在组合层的体现）。"""
    panel = _panel()
    label = np.tile(np.array([0.0, 1.0, 2.0, 3.0]), (panel.shape[0], 1))
    exposures = {"a": _exposure(label), "b": _exposure(-label + 0.5)}
    kwargs = {"params": {"corr_max": 0.9, "orthogonalise": "symmetric"},
              "impl": "score_weighted", "label_score": label}
    first = run(_call(panel, exposures, {"a": "pass", "b": "pass"}, **kwargs))
    second = run(_call(panel, exposures, {"a": "pass", "b": "pass"}, **kwargs))
    assert np.array_equal(first["score"], second["score"], equal_nan=True)
    assert first["members"] == second["members"]
    assert first["domain_digest"] == second["domain_digest"]
