"""S3-M1：面板容器、截面算子与 label 标签生成。"""

from __future__ import annotations

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.panel import Panel, array_digest
from capability.runtime import reset_impls
from capability.stats import (
    cs_neutralise, cs_rank, cs_rank_zscore, cs_zscore, forward_return,
    rolling_beta, trailing_mean,
)
from contracts.errors import E1003, E1008, E1022, E3003, E4007, FlowError

DAY_NS = 86_400_000_000_000


def _code(fn) -> str | None:
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


def _panel(t_len: int = 40, n: int = 6, seed: int = 0, holdout=None) -> Panel:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (t_len, n)), axis=0))
    open_ = close * (1 + rng.normal(0, 0.002, (t_len, n)))
    volume = rng.uniform(1e3, 1e4, (t_len, n))
    return Panel(
        times=np.arange(t_len, dtype="int64") * DAY_NS,
        symbols=tuple(f"S{i}" for i in range(n)),
        fields={
            "open": open_, "high": np.maximum(open_, close) * 1.001,
            "low": np.minimum(open_, close) * 0.999, "close": close,
            "volume": volume,
        },
        holdout=holdout,
    )


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _label_call(panel: Panel, policy: dict | None = None, **params):
    base = {"horizon": 3, "rebalance": 3, "entry": "next_close",
            "transform": "none", "neutralise": []}
    base.update(params)
    return Call.create(
        capability="label", station="Target", impl="forward_return_cs",
        impl_version="1", params=base,
        inputs=(InputRef("panel", panel.digest(), data=panel),),
        policy_view={"label": dict(policy or {})},
    )


# ---- 面板校验 -----------------------------------------------------------
def test_panel_rejects_duplicate_or_descending_times():
    p = _panel()
    bad = np.array(p.times, copy=True)
    bad[3] = bad[2]
    assert _code(lambda: Panel(times=bad, symbols=p.symbols, fields=p.fields)) == E4007

    rev = np.array(p.times, copy=True)[::-1]
    assert _code(lambda: Panel(times=rev, symbols=p.symbols, fields=p.fields)) == E4007


def test_panel_rejects_bad_shape_and_infinity():
    p = _panel()
    wrong = dict(p.fields)
    wrong["close"] = wrong["close"][:-1]
    assert _code(lambda: Panel(times=p.times, symbols=p.symbols, fields=wrong)) == E4007

    inf_fields = dict(p.fields)
    inf_fields["close"] = inf_fields["close"].copy()
    inf_fields["close"][0, 0] = np.inf
    assert _code(lambda: Panel(times=p.times, symbols=p.symbols,
                              fields=inf_fields)) == E4007


def test_panel_is_read_only_and_order_sensitive():
    p = _panel()
    with pytest.raises(ValueError):
        p.field("close")[0, 0] = 1.0
    swapped = Panel(times=p.times, symbols=tuple(reversed(p.symbols)),
                    fields={k: v[:, ::-1] for k, v in p.fields.items()})
    assert swapped.digest() != p.digest()


def test_panel_slice_and_holdout_mask():
    p = _panel(t_len=20, holdout=(10, 15))
    assert p.holdout_mask()[10:15].all()
    assert not p.holdout_mask()[:10].any()
    sub = p.slice_rows(12, 18)
    assert sub.holdout == (0, 3), "部分重叠按交裁剪，剩下的重叠段仍不可用"
    assert sub.digest() != p.digest()
    assert p.slice_rows(0, 10).holdout is None, "无交集的切片不应带保留区"
    assert p.slice_rows(0, 20).holdout == (10, 15)


# ---- 截面算子 -----------------------------------------------------------
def test_cs_rank_is_ascending_with_average_ties():
    x = np.array([[3.0, 1.0, 2.0, np.nan]])
    out = cs_rank(x)
    assert out[0, 1] < out[0, 2] < out[0, 0]
    assert np.isnan(out[0, 3])

    tie = np.array([[5.0, 5.0, 1.0]])
    r = cs_rank(tie)
    assert r[0, 0] == r[0, 1] > r[0, 2]
    assert r[0, 0] == pytest.approx(5 / 6)


def test_cs_zscore_zero_std_is_nan_row_not_zero():
    x = np.array([[1.0, 1.0, 1.0], [1.0, 2.0, 3.0]])
    out = cs_zscore(x)
    assert np.isnan(out[0]).all(), "std=0 的截面不得除、不得补 0"
    assert out[1].mean() == pytest.approx(0.0, abs=1e-12)
    assert out[1].std(ddof=1) == pytest.approx(1.0)


def test_cs_rank_zscore_bounded():
    x = np.array([[1.0, 5.0, 9.0, np.nan]])
    out = cs_rank_zscore(x)
    assert np.isfinite(out[0, :3]).all()
    assert np.isnan(out[0, 3])


def test_trailing_mean_uses_only_past():
    x = np.arange(1, 11, dtype="float64").reshape(-1, 1)
    out = trailing_mean(x, 3)
    assert out[0, 0] == 1
    assert out[1, 0] == 1.5
    assert out[2, 0] == 2
    # 改未来值不影响过去
    changed = x.copy()
    changed[9, 0] = 1e9
    again = trailing_mean(changed, 3)
    assert np.allclose(again[:9], out[:9])


def test_forward_return_values_and_tail():
    price = np.arange(1, 8, dtype="float64").reshape(-1, 1)  # 1..7
    r = forward_return(price, 2)
    assert r[0, 0] == pytest.approx(3 / 1 - 1)
    assert r[4, 0] == pytest.approx(7 / 5 - 1)
    assert np.isnan(r[5:, 0]).all(), "未来不足 horizon 的时点必须是 NaN，不是 0"

    off = forward_return(price, 2, entry_offset=1)
    assert off[0, 0] == pytest.approx(4 / 2 - 1)


def test_forward_return_direction_is_future_over_past():
    """方向必须是「未来价 / 进场价」。写反会得到数值合理但符号相反的标签。"""
    price = np.array([[100.0], [100.0], [90.0], [120.0]])
    r = forward_return(price, 2)
    assert r[0, 0] == pytest.approx(90 / 100 - 1), "下跌的未来必须给出负收益"
    assert r[1, 0] == pytest.approx(120 / 100 - 1)
    assert np.isnan(r[2:, 0]).all()


def test_rolling_beta_recovers_known_slope():
    rng = np.random.default_rng(7)
    market = rng.normal(0, 0.01, 120)
    asset = 2.0 * market
    returns = np.column_stack([asset, asset * 0.5])
    beta = rolling_beta(returns, market, 60)
    assert beta[80, 0] == pytest.approx(2.0, abs=1e-6)
    assert beta[80, 1] == pytest.approx(1.0, abs=1e-6)
    assert np.isnan(beta[0, 0]), "回看不足时必须 NaN，不得回落 1.0"


def test_cs_neutralise_removes_linear_exposure():
    rng = np.random.default_rng(3)
    x = rng.normal(size=(5, 20))
    y = 3.0 * x + rng.normal(0, 0.01, (5, 20))
    resid = cs_neutralise(y, [x])
    assert np.nanmax(np.abs(resid)) < 0.1
    for t in range(5):
        assert np.nanmean(resid[t]) == pytest.approx(0.0, abs=1e-9)


# ---- label --------------------------------------------------------------
def test_label_matches_manual_forward_return():
    panel = _panel(t_len=20, n=4)
    out = run(_label_call(panel, horizon=3, rebalance=3, transform="none"))
    close = panel.field("close")
    # entry=next_close ⇒ 进场在 t+1，出场在 t+4：label[t] = close[t+4]/close[t+1] - 1
    for t in (0, 7, 15):
        assert np.array_equal(
            out["score"][t], close[t + 4] / close[t + 1] - 1.0, equal_nan=True,
        ), f"第 {t} 行必须与手算逐位一致"
    # t+4 <= 19 ⇒ t <= 15：尾部 4 行没有未来价，必须 NaN 且不算有效样本
    assert np.isnan(out["score"][16:]).all()
    assert out["effective_n"] == 16


def test_label_output_refs_are_content_digests():
    panel = _panel()
    out = run(_label_call(panel))
    assert out["label_ref"] == array_digest(out["score"])
    assert out["score_ref"] == out["label_ref"]


def test_label_does_not_leak_future_into_earlier_scores():
    panel = _panel(t_len=30, n=5)
    base = run(_label_call(panel, horizon=3, transform="cs_rank"))
    fields = dict(panel.fields)
    fields["close"] = fields["close"].copy()
    fields["close"][-2:] *= 3.0
    changed = Panel(times=panel.times, symbols=panel.symbols, fields=fields)
    again = run(_label_call(changed, horizon=3, transform="cs_rank"))
    # 前 30-2-3 行不可能看到被改的价格
    k = 30 - 2 - 3
    assert np.allclose(base["score"][:k], again["score"][:k], equal_nan=True)


def test_label_width_drop_over_20pct_is_e3003():
    panel = _panel(t_len=30, n=6)
    # 宽度够：不丢，正常通过
    assert run(_label_call(panel, horizon=3, min_width=6, width_policy="drop"))["effective_n"] > 0
    # 人为制造大面积宽度不足：只有最后一列有值
    fields = dict(panel.fields)
    fields["close"] = np.full_like(fields["close"], np.nan)
    fields["close"][:, 0] = 100.0
    thin = Panel(times=panel.times, symbols=panel.symbols, fields=fields)
    assert _code(lambda: run(_label_call(
        thin, horizon=3, min_width=4, width_policy="drop"))) == E3003


def test_label_holdout_and_tail_are_not_counted_as_width_drops():
    """I61 的 20% 只针对宽度丢弃：保留区 30% + 尾部空行不得触发 E3003。"""
    panel = _panel(t_len=30, n=6, holdout=(20, 29))
    out = run(_label_call(panel, horizon=3, min_width=6, width_policy="drop"))
    assert not out["valid_mask"][20:29].any()
    assert out["effective_n"] == 16, "可消费时点 = 0..15：16 行起窗口就触及保留区"
    assert out["dropped_bars"]["by_reason"]["width_below_min"] == 0


def test_label_requires_overlap_policy_when_rebalance_shorter_than_horizon():
    panel = _panel()
    assert _code(lambda: run(_label_call(panel, horizon=10, rebalance=2))) == E1022
    ok = run(_label_call(panel, horizon=10, rebalance=2,
                         overlap_policy="independent_sample"))
    assert ok["effective_n"] <= panel.shape[0] // 10 + 1


def test_label_independent_sample_keeps_nonoverlapping_bars():
    panel = _panel(t_len=30, n=4)
    out = run(_label_call(panel, horizon=5, rebalance=5,
                          overlap_policy="independent_sample"))
    kept = np.flatnonzero(out["valid_mask"])
    assert len(kept) == out["effective_n"]
    assert np.all(np.diff(kept) == 5)


def test_label_holdout_is_excluded():
    panel = _panel(t_len=30, holdout=(20, 26))
    out = run(_label_call(panel))
    assert not out["valid_mask"][20:26].any()
    assert np.isnan(out["score"][20:26]).all()
    # 窗口触及保留区的行同样不可用：t=16 的标签用 close[20]，那根在保留区里
    assert not out["valid_mask"][16:20].any()
    assert out["dropped_bars"]["by_reason"]["holdout_leak"] == 4
    # 窗口触及保留区的行同样不可用：t=16 的标签用 close[20]，那根在保留区里
    assert not out["valid_mask"][16:20].any()
    assert out["dropped_bars"]["by_reason"]["holdout_leak"] == 4


def test_label_beta_neutralisation_needs_window():
    panel = _panel()
    # 窗口是口径字段（caliber.label.neutralise_window），未声明即 E1008
    assert _code(lambda: run(_label_call(panel, neutralise=["beta"]))) == E1008
    out = run(_label_call(panel, policy={"neutralise_window": 10},
                          neutralise=["beta"]))
    assert out["effective_n"] > 0


def test_label_neutralise_window_is_not_a_param():
    """口径字段不得从能力参数进——否则同一次执行会有两个窗口来源。"""
    panel = _panel()
    assert _code(lambda: run(_label_call(
        panel, neutralise=["beta"], neutralise_window=10))) == E1003


def test_label_is_deterministic():
    panel = _panel(seed=11)
    a = run(_label_call(panel, transform="cs_rank_zscore"))
    b = run(_label_call(panel, transform="cs_rank_zscore"))
    assert a["score_ref"] == b["score_ref"]
    assert np.allclose(a["score"], b["score"], equal_nan=True)


def test_label_missing_panel_input_fails_closed():
    call = Call.create(
        capability="label", station="Target", impl="forward_return_cs",
        impl_version="1",
        params={"horizon": 3, "rebalance": 3, "entry": "next_close",
                "transform": "none", "neutralise": []},
        inputs=(InputRef("panel", "sha256:" + "0" * 64),),
    )
    assert _code(lambda: run(call)) == E3003


# --------------------------------------------- IC 衰减的多跨度标签（D4 落点）
def test_build_raw_by_h_matches_label_raw_at_the_caliber_horizon():
    """衰减曲线在「口径跨度」那一档必须与主标签逐位相同。

    这是"未来收益只有一处实现"的可执行证明：两个入口走的是同一个
    `forward_raw`，所以 entry / 中性化 / 保留区掩码不可能悄悄分叉。谁哪天在
    衰减里自己写一遍 `forward_return`，这条断言就会红。
    """
    from capability.ops.label import build_raw_by_h

    panel = _panel(t_len=40, n=6)
    out = run(_label_call(panel))  # horizon=3, entry=next_close
    got = build_raw_by_h(
        panel, {"execution": {"entry": "next_close"}, "label": {"neutralise": []}},
        [3],
    )["3"]
    np.testing.assert_allclose(got, out["raw"], rtol=0, atol=0, equal_nan=True)


def test_build_raw_by_h_tightens_the_mask_as_horizon_grows():
    """跨度越长，伸进保留区的行越多——每个 h 必须用自己的 reach 重算掩码。"""
    from capability.ops.label import build_raw_by_h

    panel = _panel(t_len=60, n=6, holdout=(40, 45))
    got = build_raw_by_h(
        panel, {"execution": {"entry": "next_close"}, "label": {"neutralise": []}},
        [1, 20],
    )
    rows_h1 = int(np.isfinite(got["1"]).any(axis=1).sum())
    rows_h20 = int(np.isfinite(got["20"]).any(axis=1).sum())
    assert rows_h20 < rows_h1
    # 保留区的那几行本身在两种跨度下都必须是 NaN
    assert not np.isfinite(got["1"][40:45]).any()
    assert not np.isfinite(got["20"][40:45]).any()
