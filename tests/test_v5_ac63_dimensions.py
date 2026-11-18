"""D-003 / AC63：已知答案对拍前的量纲修复固化（06 §5.6）。

四个修复，每条一组回归守卫：

1. **量纲**：observe 的多空收益序列用变换前 `raw`，不用 `cs_rank` 后的
   `score`——否则 `gross_return − 成本` 是秩减收益的量纲混拼。
2. **对齐**：`ls_t = Σ w_t · raw_t`，不带 +1 移位（entry offset 已在标签里，
   与回测链 NEXT_BAR 同构）。
3. **rebalance 生效**：allocate/observe 同一网格冻结目标权重，`rebalance=5`
   真的是 5 根 bar 持有。
4. **B4**：observe 对 `horizon_bars` / `raw` 缺失 fail-closed（桥漏传字段时
   不得静默按 h=1 年化）。

外加一条独立复算测试：手写 pandas 参考实现 vs 流水线 observe 链，
`1e-9` 相对误差——AC63 黄金面板对拍用的就是这套参考实现。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from capability.panel import array_digest
from contracts.errors import E3003, FlowError

START = 1_700_000_000_000_000_000  # 2023-11-14T00:00Z 附近的固定 ns 时间戳
DAY_NS = 86_400_000_000_000


def _panel(t=120, n=9, seed=3, fields=("close",)):
    rng = np.random.default_rng(seed)
    times = START + np.arange(t, dtype="int64") * DAY_NS
    close = 100 + np.cumsum(rng.normal(size=(t, n)), axis=0)
    close = np.abs(close) + 1.0
    return times, close


# ---------------------------------------------------------------- 1. 量纲
def test_longshort_computes_on_raw_not_transformed_score():
    """多空收益必须基于 raw 手算复拍；喂变换后 score 会得到不同的（错误）值。

    权重来自暴露排序、收益来自 raw——变换不进多空收益（06 §5.6）。
    链级的接线由 test_observe_ic_changes_with_transform_but_gross_does_not 守卫。
    """
    from capability.ops.observe import _longshort
    from capability.stats import cs_rank

    _, close = _panel()
    t_len, n = close.shape
    rng = np.random.default_rng(11)
    values = rng.normal(size=(t_len, n))          # 暴露
    raw = close[1:] / close[:-1].mean() - 1.0     # 收益量纲序列
    raw = np.vstack([raw, raw[-1:]])
    valid = np.isfinite(values)
    label_valid = np.isfinite(raw)

    turnover, gs, gr, sr, n_periods, _ls = _longshort(values, valid, raw, label_valid, 1)

    # 手算参考：权重 = 暴露截面前/后 30% 等权；ls_t = Σ w_t · raw_t
    k = max(1, n // 3)
    weights = np.zeros((t_len, n), dtype="float64")
    for t in range(t_len):
        idx = np.flatnonzero(valid[t] & np.isfinite(raw[t]))
        if idx.size < 2:
            continue
        order = idx[np.argsort(values[t, idx], kind="stable")]
        weights[t, order[-k:]] = 1.0 / k
        weights[t, order[:k]] = -1.0 / k
    ls = (weights * raw).sum(axis=1)
    ls = ls[np.isfinite(ls)]
    exp_sr = float(np.mean(ls)) / float(np.std(ls))
    exp_gs = exp_sr * np.sqrt(365.0 / 1)
    exp_gr = float(np.mean(ls)) / 1
    exp_turn = 0.5 * float(np.abs(np.diff(weights, axis=0)).sum())

    assert gs == pytest.approx(exp_gs, rel=1e-12)
    assert gr == pytest.approx(exp_gr, rel=1e-12)
    assert sr == pytest.approx(exp_sr, rel=1e-12)
    assert turnover == pytest.approx(exp_turn, rel=1e-12)
    assert n_periods == ls.size

    # 反证：若实现把变换后 score 当收益（旧 bug），值必然不同
    _, gs_wrong, gr_wrong, _, _, _ = _longshort(
        values, valid, cs_rank(raw), label_valid, 1)
    assert gs_wrong != pytest.approx(gs, rel=1e-6)


def test_observe_ic_changes_with_transform_but_gross_does_not():
    """链级：变换只进 IC（rank IC），gross 指标对变换不变。

    同一 raw、同一暴露：score=raw 与 score=cs_rank(raw) 两条 observe 链的
    gross_return/gross_sharpe/sr/turnover 必须逐位一致；IC 允许不同
    （pearson vs spearman 语义），但两者必须都有限。
    """
    from capability.panel import Panel
    from capability.ops.observe import build as observe_build
    from capability.stats import cs_rank
    from tests.test_v5_observe import _call

    times, close = _panel(t=150, n=9)
    rng = np.random.default_rng(5)
    values = rng.normal(size=close.shape)
    cid = "sha256:" + "a" * 64
    cs = {"candidates": [{"candidate_id": cid, "generator": "gp@1",
                          "payload": {"expression": {"field": "close"}}}],
          "exposures": {cid: {"values": values,
                              "valid_mask": np.isfinite(values),
                              "values_ref": array_digest(np.ascontiguousarray(values))}},
          "family": "f", "count": 1}
    panel = Panel(times=times, symbols=tuple(f"S{j}" for j in range(close.shape[1])),
                  fields={"close": close})

    raw = np.where(np.isfinite(values), values, np.nan)  # 收益量纲的占位序列
    valid2d = np.isfinite(values)

    def run(score):
        label = {"score": score, "raw": raw.copy(), "valid_mask": valid2d.copy(),
                 "effective_n": 150, "horizon_bars": 1}
        out = observe_build(_call(cs, panel, label, cost_attr=False))
        return out["per_candidate"][0]["metrics"]

    m_raw = run(raw.copy())
    m_rank = run(cs_rank(raw))
    for key in ("gross_return", "gross_sharpe", "sr", "turnover"):
        assert m_rank[key] == pytest.approx(m_raw[key], rel=1e-12), key
    assert np.isfinite(m_raw["ic"]) and np.isfinite(m_rank["ic"])


# ---------------------------------------------------------------- 2. 对齐
def test_longshort_pairs_position_t_with_label_t_not_t_plus_one():
    """`ls_t = Σ w_t · raw_t`：收益尖峰必须出现在持仓所在行，不得后移一格。"""
    from capability.ops.observe import _longshort

    t_len, n = 20, 6
    values = np.tile(np.arange(n, dtype="float64"), (t_len, 1))  # 排序恒定
    valid = np.ones((t_len, n), dtype=bool)
    raw = np.zeros((t_len, n), dtype="float64")
    raw[3, :] = np.arange(n, dtype="float64") * 0.001  # 跨标的不同，LS 才有非零贡献
    label_valid = np.ones((t_len, n), dtype=bool)

    turnover, gross_sharpe, gross_return, sr, n_periods, _ls = _longshort(
        values, valid, raw, label_valid, 1)

    # 旧实现 `w_t·raw_{t+1}` 会把尖峰算在第 4 行 → gross_return 仍非零但
    # n_periods 少 1 且序列错位；这里直接断言：只有第 3 行贡献收益。
    weights = np.zeros((t_len, n), dtype="float64")
    k = max(1, n // 3)
    weights[:, -k:] = 1.0 / k
    weights[:, :k] = -1.0 / k
    expected_ls = (weights * raw).sum(axis=1)
    assert abs(expected_ls[3]) > 0 and expected_ls[4] == 0.0
    # 全序列均值复算一致（决定论）
    ls = expected_ls[np.isfinite(expected_ls)]
    assert gross_return == pytest.approx(float(np.mean(ls)) / 1, rel=1e-12)
    assert n_periods == t_len


# ---------------------------------------------------------------- 3. rebalance
def test_longshort_freezes_weights_on_rebalance_grid():
    from capability.ops.observe import _longshort

    t_len, n = 30, 9
    rng = np.random.default_rng(9)
    values = rng.normal(size=(t_len, n))
    valid = np.isfinite(values)
    raw = rng.normal(size=(t_len, n)) * 0.01
    label_valid = np.ones((t_len, n), dtype=bool)

    _, _, _, _, _, _ = _longshort(values, valid, raw, label_valid, 1, rebalance=5)
    # 权重网格冻结：1..4 行与 0 行相同，5 行允许不同
    # （_longshort 不返回权重，改由端到端 allocate 测试覆盖权重断言；
    #   这里验证 turnover 显著小于逐 bar 版本）
    turn_frozen, gs_f, gr_f, sr_f, np_f, _ls = _longshort(
        values, valid, raw, label_valid, 1, rebalance=5)
    turn_bar, _, _, _, _, _ = _longshort(values, valid, raw, label_valid, 1)
    assert turn_frozen < turn_bar  # 持仓 5 根 bar 才动一次


def test_allocate_freezes_weights_between_rebalance_points():
    """allocate：rebalance=5 → 权重 1..4 行 == 0 行；网格行成员不足 → 持仓不动。"""
    from capability import Call, InputRef
    from capability.ops import allocate as allocate_mod
    from capability.impls import register, _has

    impl_name = "ac63_test_allocate"
    if not _has("allocate") or True:  # 测试本地 impl，避免依赖默认装配顺序
        register("allocate", impl_name, "1", allocate_mod.build)

    t_len, n = 21, 9
    rng = np.random.default_rng(4)
    score = rng.normal(size=(t_len, n))
    tradable = np.ones((t_len, n), dtype=bool)
    tiers = np.zeros((t_len, n), dtype="int64")

    policy = {
        "allocate": {"quantiles": 3, "long_short": True, "weighting": "equal",
                     "max_weight": 0.24, "net_exposure": 0.0,
                     "turnover_max": 0.9, "min_members": 4},
        "execution": {"rebalance": 5},
    }

    def make_call(tradable_arg, rebalance):
        pol = dict(policy)
        pol["execution"] = {"rebalance": rebalance}
        return Call.create(
            capability="allocate", station="Portfolio", impl=impl_name,
            impl_version="1", params={},
            inputs=(
                InputRef("score", array_digest(np.ascontiguousarray(score)), data=score),
                InputRef("tradable_mask", array_digest(np.ascontiguousarray(tradable_arg)),
                         data=tradable_arg),
                InputRef("tiers", array_digest(np.ascontiguousarray(tiers)), data=tiers),
            ),
            policy_view=pol,
        )

    from capability.ops.allocate import build as allocate_build
    out = allocate_build(make_call(tradable, 5))
    w = out["weights"]
    assert (w[1:5] == w[0]).all()               # 冻结（逐元素；array_equal 不广播）
    assert not (w[5] == w[0]).all()             # 网格点重算
    ts = out["turnover_series"]
    assert np.all(ts[1:5] == 0.0)               # 冻结行零换手

    # 网格行成员不足：持仓不动（不斩仓），dropped 只记网格行
    tradable2 = tradable.copy()
    tradable2[10, :] = False                    # 第 10 行（网格行）全不可交易
    out2 = allocate_build(make_call(tradable2, 5))
    w2 = out2["weights"]
    assert (w2[10] == w2[9]).all()              # 网格行算不出 → 持仓不动
    assert out2["dropped_bars"]["count"] == 1   # 只记网格行
    # rebalance=1 保持旧行为：丢弃行清仓
    out3 = allocate_build(make_call(tradable2, 1))
    assert np.all(out3["weights"][10] == 0.0)   # 旧语义：丢弃行清仓


# ---------------------------------------------------------------- 4. B4 fail-closed
def test_observe_requires_horizon_bars_and_raw():
    """缺 horizon_bars / 缺 raw 一律 E3003——不得静默按 h=1 年化或退回 score。"""
    from capability.panel import Panel, array_digest
    from capability.ops.observe import build as observe_build

    times, close = _panel(t=60, n=9)
    rng = np.random.default_rng(2)
    values = rng.normal(size=close.shape)
    cid = "sha256:" + "b" * 64
    cs = {"candidates": [{"candidate_id": cid, "generator": "gp@1",
                          "payload": {"expression": {"field": "close"}}}],
          "exposures": {cid: {"values": values,
                              "valid_mask": np.isfinite(values),
                              "values_ref": array_digest(np.ascontiguousarray(values))}},
          "family": "f", "count": 1}
    panel = Panel(times=times, symbols=tuple(f"S{j}" for j in range(close.shape[1])),
                  fields={"close": close})
    raw = np.isfinite(values).astype("float64")  # 占位收益
    base = {
        "score": raw.copy(), "raw": raw.copy(),
        "valid_mask": np.isfinite(values),
        "effective_n": 60, "horizon_bars": 1,
    }
    from tests.test_v5_observe import _call
    call = _call(cs, panel, base)
    observe_build(call)  # 完整字段 → 通过

    missing_h = {k: v for k, v in base.items() if k != "horizon_bars"}
    with pytest.raises(FlowError) as e1:
        observe_build(_call(cs, panel, missing_h))
    assert e1.value.code == "E3003"

    missing_raw = {k: v for k, v in base.items() if k != "raw"}
    with pytest.raises(FlowError) as e2:
        observe_build(_call(cs, panel, missing_raw))
    assert e2.value.code == "E3003"


# ---------------------------------------------------------------- 5. 独立复算
def test_reference_recomputation_matches_observe_chain():
    """手写 pandas 参考实现 vs 流水线 label+observe：`1e-9` 相对误差。

    AC63 黄金面板对拍的容差依据：同数据同口径的决定论量，只允许浮点
    求和顺序差。参考实现不 import v5 的 observe/label 实现。
    """
    from capability.panel import Panel, array_digest
    from capability.ops.observe import build as observe_build
    from tests.test_v5_observe import _call

    t_len, n = 200, 9
    rng = np.random.default_rng(42)
    times = START + np.arange(t_len, dtype="int64") * DAY_NS
    close = 100.0 * np.exp(np.cumsum(rng.normal(size=(t_len, n)), axis=0) * 0.01)
    symbols = tuple(f"S{j}" for j in range(n))
    panel = Panel(times=times, symbols=symbols, fields={"close": close})

    h, entry_offset = 5, 1
    # ---- 参考实现（pandas，独立写法）--------------------------------
    df = pd.DataFrame(close)
    exposure = df / df.shift(60) - 1.0                     # mom60
    label_ref = df.shift(-(entry_offset + h)) / df.shift(-entry_offset) - 1.0
    k = n // 3
    weights_mat = np.zeros((t_len, n))
    for t in range(t_len):
        row = exposure.iloc[t]
        ok = row.notna() & np.isfinite(label_ref.iloc[t])
        if int(ok.sum()) < 2:
            continue                                  # 该行清仓（与 period=1 语义一致）
        order = row[ok].sort_values(kind="stable")
        w = pd.Series(0.0, index=row.index)
        w[order.index[-k:]] = 1.0 / k
        w[order.index[:k]] = -1.0 / k
        weights_mat[t] = w.to_numpy()
    ref_turn = 0.5 * float(np.abs(np.diff(weights_mat, axis=0)).sum())
    ls_series = (weights_mat * label_ref.to_numpy()).sum(axis=1)
    ls_series = ls_series[np.isfinite(ls_series)]
    ref_mean = float(ls_series.mean())
    ref_std = float(ls_series.std())
    ref_sr = ref_mean / ref_std
    ref_gs = ref_sr * np.sqrt(365.0 / h)
    ref_gr = ref_mean / h

    # ---- 流水线链（evaluate 语义的 mom60 暴露 + label raw + observe）----
    values = (close / np.vstack([np.full((60, n), np.nan), close[:-60]]) - 1.0)
    cid = "sha256:" + "c" * 64
    cs = {"candidates": [{"candidate_id": cid, "generator": "prior:mom60",
                          "payload": {"expression": {"op": "ret",
                                                     "args": [{"field": "close"}, {"const": 60}]}}}],
          "exposures": {cid: {"values": np.ascontiguousarray(values),
                              "valid_mask": np.isfinite(values),
                              "values_ref": array_digest(np.ascontiguousarray(values))}},
          "family": "f", "count": 1}
    label = {
        "score": np.where(np.isfinite(label_ref.to_numpy()),
                          label_ref.to_numpy(), np.nan),
        "raw": np.where(np.isfinite(label_ref.to_numpy()),
                        label_ref.to_numpy(), np.nan),
        "valid_mask": np.isfinite(label_ref.to_numpy()),
        "effective_n": int(np.isfinite(label_ref.to_numpy()).all(axis=1).sum()),
        "horizon_bars": h,
    }
    out = observe_build(_call(cs, panel, label, cost_attr=False))
    m = out["per_candidate"][0]["metrics"]

    assert m["gross_sharpe"] == pytest.approx(ref_gs, rel=1e-9)
    assert m["gross_return"] == pytest.approx(ref_gr, rel=1e-9)
    assert m["sr"] == pytest.approx(ref_sr, rel=1e-9)
    assert m["turnover"] == pytest.approx(ref_turn, rel=1e-9)
    assert m["sr_n_obs"] == ls_series.size
