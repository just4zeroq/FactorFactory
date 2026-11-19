"""搜索 fitness 的 raw_gross 口径测试。

raw_gross 用分位多空真实算术收益做搜索目标，修复 rank_ic 在厚尾下的假阳性：
rank IC 正但真实组合亏的因子（low_vol20/ts_efficiency10）不应被 GP 高分选中。
"""
from __future__ import annotations

import numpy as np
import pytest

from capability.ops.search import _gross_from_raw, _make_fitness
from contracts.errors import FlowError


def test_gross_direction_follows_exposure():
    """暴露与收益同向 → gross 正；反向 → gross 负（取绝对值前）。"""
    rng = np.random.default_rng(0)
    t, n = 20, 9
    values = rng.normal(size=(t, n))
    # raw 与 values 完全同向（按 values 排序即按 raw 排序）
    raw = values.copy()
    g_pos = _gross_from_raw(values, np.isfinite(values), raw)
    g_neg = _gross_from_raw(values, np.isfinite(values), -raw)
    assert g_pos > 0
    assert g_neg < 0
    assert g_pos == pytest.approx(-g_neg, rel=1e-10)


def test_gross_uses_real_return_not_rank():
    """关键：两套因子 rank IC 相同（都单调），但真实收益不同 → gross 必须不同。"""
    t, n = 30, 9
    values = np.tile(np.arange(n, dtype=float), (t, 1))  # 固定暴露序
    valid = np.isfinite(values)
    # 情形 A：高分位组真实收益低（低波陷阱，rank 仍单调）
    raw_trap = -values * 0.01 + np.linspace(0, 0.1, t)[:, None]
    # 情形 B：高分位组真实收益高
    raw_good = values * 0.01 + np.linspace(0, 0.1, t)[:, None]
    g_trap = _gross_from_raw(values, valid, raw_trap)
    g_good = _gross_from_raw(values, valid, raw_good)
    assert g_trap < 0 < g_good, (g_trap, g_good)


def test_gross_antitail_like_observe():
    """极端值幅度不影响 gross（等权分位，非均值权重）——与 observe 同源。"""
    t, n = 15, 6
    values = np.tile(np.arange(n, dtype=float), (t, 1))
    raw = values * 0.01 + np.zeros((t, n))
    mild = raw.copy(); severe = raw.copy()
    severe[3, -1] -= 50.0  # 顶部标的极端负值
    g_mild = _gross_from_raw(values, np.isfinite(values), mild)
    g_severe = _gross_from_raw(values, np.isfinite(values), severe)
    # 极端值只要不改变分位归属（仍是最差组），gross 变化仅是那一格 1/k 权重
    assert abs(g_severe - g_mild) < 5.0  # 50 * (1/2) / 15 ≈ 1.67，有限影响


def test_gross_handles_insufficient_width():
    """截面宽度不足 2k 时该期跳过，不硬凑。"""
    values = np.array([[1.0, np.nan, np.nan, np.nan]])
    raw = np.array([[0.01, np.nan, np.nan, np.nan]])
    assert _gross_from_raw(values, np.isfinite(values), raw) == -1.0


def test_fitness_mode_validation():
    """fitness_mode 必须是已知值；raw_gross 缺 raw 时 fail-closed。"""
    from capability.panel import Panel
    import numpy as np

    score = np.random.default_rng(1).normal(size=(10, 5))
    with pytest.raises(FlowError):
        _make_fitness(None, None, score, mode="bogus")
    with pytest.raises(FlowError):
        _make_fitness(None, None, score, mode="raw_gross", raw=None)
