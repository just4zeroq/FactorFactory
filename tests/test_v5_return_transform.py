"""return_transform 口径测试：rank / rank_scaled / winsor 的语义与量纲边界。

rank IC 与算术均值收益在厚尾资产上会系统性分歧（observe 用 rank IC 发现、
用算术均值评价组合）。这组口径让「信号→钱」与 IC 同轴。测试钉住三件事：
1. rank 无量纲（±0.5 跨度），gross 与成本不同尺度；
2. rank_scaled 恢复收益率量纲（gross 量级随标签 σ 线性缩放），仍抗厚尾；
3. raw 是历史口径，缺省不变。
"""
from __future__ import annotations

import numpy as np
import pytest

from capability.ops.observe import _longshort


def _fixture(scale: float = 0.02, seed: int = 7):
    rng = np.random.default_rng(seed)
    t_len, n = 40, 12
    # 暴露：前 6 个标的分数高 → 稳定做多；后 6 个做空
    values = np.tile(np.arange(n, dtype="float64"), (t_len, 1))
    values = values + rng.normal(0, 0.01, size=values.shape)
    valid = np.isfinite(values)
    # 标签：高分位组整体有一个正的相对漂移（信号有效），叠加厚尾噪声
    base = np.linspace(-0.5, 0.5, n) * scale
    raw = base + rng.normal(0, scale * 0.1, size=(t_len, n))
    # 注入一个极端左尾到高分位组：rank IC 仍正，但算术均值被拖累
    raw[5, :] -= 3.0 * scale
    label_valid = np.isfinite(raw)
    return values, valid, raw, label_valid


def test_raw_is_default_and_legacy_unchanged():
    values, valid, raw, label_valid = _fixture()
    a = _longshort(values, valid, raw, label_valid, 1)
    b = _longshort(values, valid, raw, label_valid, 1, return_transform="raw")
    for x, y in zip(a[:5], b[:5]):
        assert x == pytest.approx(y, rel=1e-15)
    np.testing.assert_allclose(a[5], b[5])


def test_rank_is_bounded_dimensionless_and_antitail():
    """rank 口径：gross 被限制在 ±1 内（等权多空秩线性组合），与标签绝对尺度无关。"""
    values, valid, raw, label_valid = _fixture(scale=0.02)
    _t, _gs, gr_rank, _sr, _n, ls_rank = _longshort(
        values, valid, raw, label_valid, 1, return_transform="rank")
    # 每期 ls = Σw·centred_rank，Σ|w|=2、|centred|≤0.5 → |ls|≤1
    assert np.nanmax(np.abs(ls_rank)) <= 1.0 + 1e-12
    gr_rank_small = gr_rank
    # 标签整体放大 100 倍：IC 不变，rank 的 gross 也不变（无量纲铁证）
    _t, _gs, gr_rank_big, _sr, _n, _ = _longshort(
        values, valid, raw * 100.0, label_valid, 1, return_transform="rank")
    assert gr_rank_big == pytest.approx(gr_rank_small, rel=1e-9)


def test_rank_scaled_has_return_dimension_and_antitail():
    """rank_scaled：gross 随标签尺度线性缩放（收益率量纲），且不被极端值拖垮。"""
    values, valid, raw, label_valid = _fixture(scale=0.02)
    _, _, gr_s, _, _, ls_s = _longshort(
        values, valid, raw, label_valid, 1, return_transform="rank_scaled")
    _, _, gr_s_big, _, _, _ = _longshort(
        values, valid, raw * 10.0, label_valid, 1, return_transform="rank_scaled")
    # 尺度放大 10 倍 → rank_scaled gross 也放大约 10 倍（收益率量纲）
    assert gr_s_big == pytest.approx(gr_s * 10.0, rel=2e-2)
    # 收益率量纲：量级应落在「几个百分点的截面 σ」而非 ±0.5 的秩
    assert abs(gr_s) < 0.5
    assert np.nanmax(np.abs(ls_s)) < 1.0


def test_rank_ignores_tail_magnitude_raw_does_not():
    """抗厚尾的精确含义：同一标的的负 shock 从 -5σ 加深到 -50σ，
    rank 视角下排序不变 → gross 逐位不变；raw 视角下均值被显著拖累。"""
    values, valid, raw, label_valid = _fixture(scale=0.02)
    mild = raw.copy()
    severe = raw.copy()
    # 最顶部标的（做多侧）在第 5 期给一个极端负收益：两次都排最后，
    # 只是幅度不同（-5σ vs -50σ）。
    top = np.argmax(values[5])
    mild[5, top] = -5.0 * 0.02
    severe[5, top] = -50.0 * 0.02

    _, _, rank_mild, _, _, _ = _longshort(
        values, valid, mild, label_valid, 1, return_transform="rank")
    _, _, rank_severe, _, _, _ = _longshort(
        values, valid, severe, label_valid, 1, return_transform="rank")
    _, _, raw_mild, _, _, _ = _longshort(
        values, valid, mild, label_valid, 1, return_transform="raw")
    _, _, raw_severe, _, _, _ = _longshort(
        values, valid, severe, label_valid, 1, return_transform="raw")

    # rank：只看顺序，shock 幅度从 -5σ 到 -50σ 不改变 gross
    assert rank_severe == pytest.approx(rank_mild, rel=1e-12)
    # raw：幅度加深 10 倍，gross 被显著拖低
    assert raw_severe < raw_mild - 1e-4


def test_unknown_transform_fails_closed():
    values, valid, raw, label_valid = _fixture()
    with pytest.raises(Exception):
        _longshort(values, valid, raw, label_valid, 1, return_transform="bogus")
