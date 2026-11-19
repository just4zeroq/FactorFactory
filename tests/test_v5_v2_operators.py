"""v2 算子扩表（19 → 34）：语义级对拍，不是「能跑就算过」。

扩算子最容易犯的错不是崩，而是**语义悄悄不同**：`ts_rank` 是 pct 还是原始名次、
`slope` 的自变量是从 0 还是从 1 起、`ema` 的种子是首值还是窗口均值。
这些差别不影响「算得出数」，只影响「算出来的是不是那条因子」——所以每条断言
都对着手算值/`numpy.polyfit` 写死，不用 `assert finite` 搪塞。
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.panel import Panel
from capability.registry import default_registry
from capability.runtime import reset_impls
from contracts.errors import E1003, E1004, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
KERNEL = pkg_version("qweave")

# 单标的、7 根 bar 的基准序列：每个算子都对着它手算。
X = np.array([3.0, 1.0, 2.0, 5.0, 4.0, 4.0, 6.0])


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _panel(values: np.ndarray | None = None, *, name: str = "x") -> Panel:
    x = X if values is None else values
    t_len = x.size
    times = START + np.arange(t_len, dtype="int64") * DAY_NS
    return Panel(times=times, symbols=("A",), fields={name: x.reshape(-1, 1)})


def _eval(payload, panel: Panel) -> dict:
    call = Call.create(
        capability="evaluate", station="Evidence", impl="qweave",
        impl_version=KERNEL,
        params={
            "operator_set": default_registry().operators_version,
            "nan_policy": "propagate",
            "node_cache": False,
        },
        inputs=(
            InputRef("payload", "sha256:" + "0" * 64, data=payload),
            InputRef("panel", panel.digest(), data=panel),
        ),
    )
    return run(call)["values"][:, 0]


def _f(name: str = "x") -> dict:
    return {"field": name}


def _c(value) -> dict:
    return {"const": value}


def _op(name: str, *args) -> dict:
    return {"op": name, "args": list(args)}


# ---------------------------------------------------------------- 时序族
def test_ts_rank_is_percentile_of_last_value():
    """qweave 的 ts_rank 是**窗口内末值**的平均名次百分位（并列取平均）。

    手算：t=2 窗口 [3,1,2] 末值 2 → 名次 2/3；t=3 窗口 [1,2,5] 末值 5 → 1.0；
    t=5 窗口 [5,4,4] 末值 4，与另一个 4 并列 → 平均名次 1.5/3 = 0.5。
    """
    out = _eval(_op("ts_rank", _f(), _c(3)), _panel())
    assert np.isnan(out[:2]).all()
    assert out[2:] == pytest.approx([2 / 3, 1.0, 2 / 3, 0.5, 1.0])


def test_ts_argmax_argmin_index_from_window_start():
    """0 基、从窗口**起点**数——不是「距今天几根」。方向反了会让因子整体取负。"""
    hi = _eval(_op("ts_argmax", _f(), _c(3)), _panel())
    lo = _eval(_op("ts_argmin", _f(), _c(3)), _panel())
    assert hi[2:] == pytest.approx([0.0, 2.0, 1.0, 0.0, 2.0])
    assert lo[2:] == pytest.approx([1.0, 0.0, 0.0, 1.0, 0.0])


def test_slope_and_rsquare_match_numpy_polyfit():
    """回归的自变量是 0..d-1（不是日期、不是倒数），与 `np.polyfit` 直接对拍。"""
    panel = _panel()
    slope = _eval(_op("slope", _f(), _c(3)), panel)
    rsq = _eval(_op("rsquare", _f(), _c(3)), panel)
    for t in range(2, X.size):
        w = X[t - 2:t + 1]
        coef = np.polyfit([0, 1, 2], w, 1)
        ss_tot = np.sum((w - w.mean()) ** 2)
        ss_res = np.sum((w - np.polyval(coef, [0, 1, 2])) ** 2)
        assert slope[t] == pytest.approx(coef[0])
        assert rsq[t] == pytest.approx(1 - ss_res / ss_tot)


def test_ema_is_mean_seeded_then_alpha_recursion():
    """首个有效值 = 窗口均值（不是首值）；之后 alpha = 2/(d+1)。

    `ema(3)`：t=2 种子 mean(3,1,2)=2；t=3 → 2+0.5*(5-2)=3.5；
    t=4 → 3.5+0.5*(4-3.5)=3.75；t=5 → 3.75+0.5*(4-3.75)=3.875。
    """
    out = _eval(_op("ema", _f(), _c(3)), _panel())
    assert np.isnan(out[:2]).all()
    assert out[2:] == pytest.approx([2.0, 3.5, 3.75, 3.875, 4.9375])


def test_decay_linear_weights_recent_most():
    """线性衰减：窗口 [3,1,2] 权重 (1,2,3) → (3*1+1*2+2*3)/6 = 11/6。"""
    out = _eval(_op("decay_linear", _f(), _c(3)), _panel())
    assert out[2:] == pytest.approx([11 / 6, 10 / 3, 4.0, 25 / 6, 5.0])


def test_ts_mad_is_mean_absolute_deviation_about_own_mean():
    """t=2 窗口 [3,1,2] 均值 2 → (1+1+0)/3 = 0.6667（不是中位数绝对偏差）。"""
    out = _eval(_op("ts_mad", _f(), _c(3)), _panel())
    assert out[2] == pytest.approx(2 / 3)
    assert np.isfinite(out[2:]).all()


def test_ts_skew_and_kurt_are_finite_on_short_windows():
    out_s = _eval(_op("ts_skew", _f(), _c(4)), _panel())
    out_k = _eval(_op("ts_kurt", _f(), _c(4)), _panel())
    assert np.isfinite(out_s[3:]).all() and np.isfinite(out_k[3:]).all()


# ---------------------------------------------------------------- 逐点族
def test_neg_power_signed_power_semantics():
    panel = _panel()
    assert _eval(_op("neg", _f()), panel) == pytest.approx(-X)
    assert _eval(_op("power", _f(), _c(2.0)), panel) == pytest.approx(X ** 2)
    # signed_power 保号：负值的分数次幂不返回 NaN，而是 -|x|^p。
    neg = _eval(_op("signed_power", _f(), _c(0.5)), _panel(-X))
    assert neg == pytest.approx(-np.sqrt(X))


def test_power_of_negative_base_does_not_nan():
    """Parkinson 波动的写法就是 `power(log(H/L), 2)`，而 log(H/L) 有正有负。

    若内核把负底的偶数次幂算成 NaN，`low_parkinson20` 会变成一条全 NaN 因子，
    且只在某些标的上失败——这是扩算子时最该提前钉住的一条。
    """
    panel = _panel(-X)  # 全负，模拟 log(H/L) < 0 的窗口
    out = _eval(_op("power", _f(), _c(2.0)), panel)
    assert np.isfinite(out).all()
    assert out == pytest.approx(X ** 2)


# ---------------------------------------------------------------- 注册表一致性
def test_new_operators_are_declared_with_window_domain():
    """带参的新算子必须有窗口域，否则 `_int_param` 无处校验、2.5 也能过。"""
    reg = default_registry()
    for name in ("ts_rank", "ts_argmax", "ts_argmin", "ts_skew", "ts_kurt",
                 "ts_mad", "product", "decay_linear", "ema", "slope",
                 "rsquare", "resi"):
        spec = reg.operators[name]
        assert list(spec["param_domain"]) == ["window"]
        assert spec["param_domain"]["window"]["min"] == 1
        assert spec["since"] == "v2"


def test_pointwise_operators_have_no_param_domain():
    reg = default_registry()
    for name in ("neg", "power", "signed_power"):
        assert reg.operators[name]["param_domain"] == {}
        assert reg.operators[name]["since"] == "v2"


def test_window_operators_reject_float_param():
    """`ts_rank(x, 2.5)` 必须在编译期报参数错——窗口是计数，不是值。"""
    panel = _panel()
    with pytest.raises(FlowError) as exc:
        _eval(_op("ts_rank", _f(), _c(2.5)), panel)
    assert exc.value.error.code == E1004


def test_power_takes_expression_not_param():
    """`power` 没有窗口域，末位是**值**：`power(x, 2.5)` 合法，与窗口算子相反。"""
    panel = _panel()
    out = _eval(_op("power", _f(), _c(2.5)), panel)
    assert out == pytest.approx(X ** 2.5)
