"""v2 内置因子的闭式对拍：因子不是「能算出数」就算对，要有可手算的锚。

`low_parkinson20` 选它做锚是因为它有闭式解：若每根 bar 的 H/L 恒为 e^r，
则 ln(H/L)=r，σ = r/(2√ln2)——任何一处窗口/常数/开方写错都会立刻偏离。

注意每个用例都至少给 **3 个标的**：`evaluate` 对常数暴露（极差 0）会报 E3003，
单标的的闭式解恰好是常数，会被正确拒掉。这不是测试的麻烦，是那层防护在起作用。
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.factors import BUILTIN_FACTORS, factor_ast, validate_all
from capability.impls import install_default_impls
from capability.panel import Panel
from capability.registry import default_registry
from capability.runtime import reset_impls
from contracts.errors import FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
KERNEL = pkg_version("qweave")
_INV_2SQRT_LN2 = 1.0 / (2.0 * np.sqrt(np.log(2.0)))  # 1/(2√ln2) ≈ 0.600561


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _panel(columns: dict[str, list[np.ndarray]]) -> Panel:
    """`{字段: [标的0 的时间序列, 标的1 …]}` → 面板。"""
    series = next(iter(columns.values()))
    n_bars, n_sym = series[0].size, len(series)
    times = START + np.arange(n_bars, dtype="int64") * DAY_NS
    return Panel(
        times=times,
        symbols=tuple(f"S{i}" for i in range(n_sym)),
        fields={k: np.stack(v, axis=1) for k, v in columns.items()},
    )


def _eval_factor(name: str, panel: Panel) -> np.ndarray:
    call = Call.create(
        capability="evaluate", station="Evidence", impl="qweave",
        impl_version=KERNEL,
        params={
            "operator_set": default_registry().operators_version,
            "nan_policy": "propagate",
            "node_cache": False,
        },
        inputs=(
            InputRef("payload", "sha256:" + "0" * 64, data=factor_ast(name)),
            InputRef("panel", panel.digest(), data=panel),
        ),
    )
    return run(call)["values"]


def test_builtin_pool_validates_as_a_whole():
    result = validate_all()
    assert result["count"] == len(BUILTIN_FACTORS) == 59
    for name in ("low_parkinson20", "near_high60", "funding_zscore7", "trend_r2_20",
                 "vol_ret_corr10", "vol_level_corr10", "near_low10", "short_std5",
                 "near_high5", "vol_close5_gap", "vol_chg_ret_corr6",
                 "vol_high_rank_corr3", "vol_price_pos_corr6",
                 "days_since_high60", "days_since_low60", "ret_skew20", "ret_kurt20",
                 "ret_mad20", "trend_slope20", "trend_resi20", "ema_trend10_30",
                 "vol_of_vol20", "up_day_ratio20", "funding_curve21_7",
                 "oi_change5", "oi_funding_corr10"):
        assert name in result["names"]


def test_low_parkinson_matches_closed_form():
    """H/L ≡ e^r → ln(H/L)=r → σ = r/(2√ln2)。

    三个标的取 r = 1.0 / 1.5 / 2.0：既验证闭式解，也保证截面有区分度
    （否则会被「常数暴露」拦下）。
    """
    n, ratios = 40, (1.0, 1.5, 2.0)
    cols_close, cols_high, cols_low = [], [], []
    for r in ratios:
        close = np.linspace(100.0, 140.0, n)
        cols_close.append(close)
        cols_high.append(close * np.exp(r))
        cols_low.append(close)
    out = _eval_factor("low_parkinson20", _panel(
        {"close": cols_close, "high": cols_high, "low": cols_low}))
    assert np.isnan(out[:19]).all()
    for j, r in enumerate(ratios):
        assert out[19:, j] == pytest.approx(-r * _INV_2SQRT_LN2)


def test_near_high60_is_close_over_running_max_high():
    """单调上行时 60 日最高价 = 当期 high，故比值恒为 close/(close+1)。"""
    n = 70
    cols_close, cols_high = [], []
    for scale in (1.0, 1.2, 1.4):
        close = np.linspace(100.0, 169.0, n) * scale
        cols_close.append(close)
        cols_high.append(close + 1.0)
    out = _eval_factor("near_high60", _panel({"close": cols_close, "high": cols_high}))
    assert np.isnan(out[:59]).all()
    assert out[59:, 0] == pytest.approx(cols_close[0][59:] / (cols_close[0][59:] + 1.0))


def test_trend_r2_is_one_on_perfect_line():
    """完全线性的价格 → R²=1；`rsquare` 口径写错时最先破的一条。

    另外两个标的给不同程度的弯曲，让截面不平坦，同时验证 R² 对弯曲敏感。
    """
    n = 40
    t = np.arange(n, dtype="float64")
    line = 100.0 + t
    curve = 100.0 + t + 0.5 * t ** 2
    noisy = 100.0 + t + np.sin(t) * 5.0
    out = _eval_factor("trend_r2_20", _panel({"close": [line, curve, noisy]}))
    assert out[19:, 0] == pytest.approx(1.0)
    # 二次曲线在 20 根窗口内局部就很像直线（R²≈0.94~0.99），所以只断言
    # 「严格小于 1 且低于直线标的」——拿 0.9 当阈值会把正确的 R² 判成错。
    assert (out[19:, 1] < 1.0 - 1e-6).all()
    assert (out[19:, 2] < 1.0 - 1e-6).all()
    assert (out[19:, 1] < out[19:, 0]).all()
    assert (out[19:, 2] < out[19:, 0]).all()


def test_funding_zscore_needs_futures_field():
    """`funding_zscore7` 引用 funding_rate：现货面板上求值必须失败，不是给 0。"""
    n = 80
    close = [100.0 + np.arange(n, dtype="float64") * s for s in (1.0, 1.2, 1.4)]
    with pytest.raises(FlowError):
        _eval_factor("funding_zscore7", _panel({"close": close}))


def test_all_builtin_factors_are_well_formed():
    from capability.factors import _check_ast

    reg = default_registry()
    for name in BUILTIN_FACTORS:
        _check_ast(factor_ast(name), reg, f"factors.{name}")


# --- v3：从 qweave 迁入的 9 个因子 ---------------------------------------
_QW_IMPORTS = (
    "vol_ret_corr10", "vol_level_corr10", "near_low10", "short_std5",
    "near_high5", "vol_close5_gap", "vol_chg_ret_corr6",
    "vol_high_rank_corr3", "vol_price_pos_corr6",
)


def test_near_low10_is_rolling_low_over_close():
    """窗口内 low 恒定时应等于 low/close，且三个标的的比值不能相同。"""
    n = 40
    lows = [np.full(n, 100.0 * s) for s in (1.0, 1.1, 1.2)]
    closes = [np.full(n, 110.0 * s * f) for s, f in zip((1.0, 1.1, 1.2), (1.0, 1.02, 1.04))]
    out = _eval_factor("near_low10", _panel({"low": lows, "close": closes}))
    assert np.isnan(out[:9]).all()
    for j in range(3):
        assert out[9:, j] == pytest.approx(lows[j][0] / closes[j][0])


def test_short_std5_ranks_volatility_inversely():
    """取负后，波动小的标的分数必须更高——写反方向会被这一条抓住。"""
    n = 60
    t = np.arange(n, dtype="float64")
    amps = (1.0, 3.0, 8.0)
    closes = [100.0 + t * 0.1 + a * np.sin(t * 0.7) for a in amps]
    out = _eval_factor("short_std5", _panel({"close": closes}))
    assert np.isfinite(out[9:]).all()
    assert (out[9:, 0] > out[9:, 1]).all()
    assert (out[9:, 1] > out[9:, 2]).all()


def test_qweave_imports_evaluate_on_ohlcv_panel():
    """9 个迁入因子必须都能在生产求值路径上算出有限值，不能只过 AST 校验。"""
    n = 80
    rng = np.random.default_rng(20261119)
    cols: dict[str, list[np.ndarray]] = {}
    for key in ("open", "high", "low", "close", "volume"):
        cols[key] = []
    for j, scale in enumerate((1.0, 1.2, 1.5)):
        close = 100.0 * scale + np.cumsum(rng.normal(0.0, 0.8 * scale, n))
        spread = np.abs(rng.normal(1.0, 0.3, n)) * scale
        cols["close"].append(close)
        cols["open"].append(close - rng.normal(0.0, 0.4, n) * scale)
        cols["high"].append(close + spread)
        cols["low"].append(close - spread)
        cols["volume"].append(np.abs(rng.normal(1000.0, 300.0, n)) * scale)
    panel = _panel(cols)
    for name in _QW_IMPORTS:
        out = _eval_factor(name, panel)
        assert out.shape == (n, 3), name
        assert np.isfinite(out[20:]).any(), f"{name} 在成熟窗口后全为 NaN"


# --- v4：按族补的加密原生因子 -------------------------------------------
_V4_FACTORS = (
    "days_since_high60", "days_since_low60", "ret_skew20", "ret_kurt20",
    "ret_mad20", "trend_slope20", "trend_resi20", "ema_trend10_30",
    "vol_of_vol20", "up_day_ratio20", "funding_curve21_7",
    "oi_change5", "oi_funding_corr10",
)


def _full_panel(n: int = 90) -> Panel:
    """九个字段都有值的合成面板：衍生品因子也要能走同一条求值路径。"""
    rng = np.random.default_rng(4242)
    cols: dict[str, list[np.ndarray]] = {k: [] for k in
        ("open", "high", "low", "close", "volume", "vwap", "funding_rate", "basis", "open_interest")}
    for scale in (1.0, 1.3, 1.7):
        close = 100.0 * scale + np.cumsum(rng.normal(0.0, 0.9, n))
        spread = np.abs(rng.normal(1.0, 0.3, n)) * scale
        cols["close"].append(close)
        cols["open"].append(close - rng.normal(0.0, 0.3, n) * scale)
        cols["high"].append(close + spread)
        cols["low"].append(close - spread)
        cols["volume"].append(np.abs(rng.normal(2000.0, 400.0, n)) * scale)
        cols["vwap"].append(close)
        cols["funding_rate"].append(rng.normal(0.0001, 0.0002, n))
        cols["basis"].append(rng.normal(0.0, 0.002, n))
        cols["open_interest"].append(1000.0 * scale + np.arange(n) * 3.0)
    return _panel(cols)


def test_days_since_high_is_measured_back_from_today():
    """ts_argmax 从窗口起点数，写反会让「距前高」整体反向。"""
    n = 70
    spikes = (50, 55, 60)
    high = []
    close = []
    for spike in spikes:
        h = np.full(n, 100.0)
        h[spike] = 150.0
        high.append(h)
        close.append(np.full(n, 100.0))
    out = _eval_factor("days_since_high60", _panel({"high": high, "close": close}))
    # t=69 的窗口是 [10..69]，故距前高 = 69 - spike
    assert out[69] == pytest.approx([19.0, 14.0, 9.0])


def test_trend_slope_normalises_by_price():
    """close = a + t → slope 恒为 1，除以价格后应等于 1/close。"""
    n = 40
    slopes = (1.0, 2.0, 3.0)
    closes = [100.0 + np.arange(n, dtype="float64") * a for a in slopes]
    out = _eval_factor("trend_slope20", _panel({"close": closes}))
    expected = np.stack([a / c[19:] for a, c in zip(slopes, closes)], axis=1)
    assert out[19:] == pytest.approx(expected)


def test_up_day_ratio_counts_direction_not_magnitude():
    """同样涨幅：连续小涨的上行天数比例应高于一根大阳线加横盘。"""
    n = 40
    smooth = 100.0 + np.arange(n, dtype="float64") * 1.0
    jumpy = np.full(n, 100.0); jumpy[10] = 140.0
    flat = np.full(n, 100.0)
    out = _eval_factor("up_day_ratio20", _panel(
        {"close": [smooth, jumpy, flat]}))
    assert (out[30:, 0] > out[30:, 1]).all() and (out[30:, 0] > out[30:, 2]).all()


def test_v4_factors_evaluate_on_full_panel():
    for name in _V4_FACTORS:
        out = _eval_factor(name, _full_panel())
        assert out.shape == (90, 3), name
        assert np.isfinite(out[30:]).any(), f"{name} 成熟窗口后全为 NaN"
