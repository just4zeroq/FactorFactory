"""S4：allocate / backtest 两个真实能力的单元测试。

直接走 `Call.create + run`（不是裸调 build），因此_params / inputs / outputs 六步
契约也在覆盖范围内。allocate/backtest 是 closed_multi 名字空间，测试内各自登记
一个只用于测试的实现名；`weights` 数组通道由主代理写进 params.json，这里在测试
内注入同一份 schema，使本文件与集成先后无关。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

import v5.capability.params as params_mod
from v5.capability import Call, InputRef, run
from v5.capability.impls import install_default_impls
from v5.capability.ops import allocate as allocate_mod
from v5.capability.ops import backtest as backtest_mod
from v5.capability.panel import array_digest
from v5.capability.params import load_param_schemas
from v5.capability.runtime import register, reset_impls
from v5.contracts.errors import E1003, E1004, E1008, E1013, E3001, E3003, FlowError

ALLOC_IMPL = "s4_test_allocate"
BT_IMPL = "s4_test_backtest"


def _code(fn) -> str | None:
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


@pytest.fixture(autouse=True)
def _impls(monkeypatch):
    reset_impls()
    install_default_impls()
    # 主代理会在 registry/params.json 的 allocate.outputs 增加 "weights": "array"。
    # 这里注入同一份 schema，保证测试在集成前后都能通过。
    doc = load_param_schemas()
    doc["allocate"]["outputs"]["weights"] = "array"
    monkeypatch.setattr(params_mod, "_CACHE", doc)
    register("allocate", ALLOC_IMPL, "1", allocate_mod.build)
    register("backtest", BT_IMPL, "1", backtest_mod.build)
    yield
    reset_impls()


def _ref(kind: str, data) -> InputRef:
    return InputRef(kind, array_digest(data), data=data)


def _alloc_cal(**over):
    cal = dict(quantiles=5, long_short=True, weighting="equal", max_weight=0.4,
               net_exposure=0.0, turnover_max=1.0, min_members=2)
    cal.update(over)
    return cal


def _net_cal(**over):
    cal = dict(cost_tiers=[20, 60, 150], slippage="none", fee_model="taker",
               max_leverage=1.0, initial_margin=0.1, maintenance_margin=0.01,
               liquidation_buffer=0.01)
    cal.update(over)
    return cal


def _run_allocate(score, tradable, tiers, *, cal=None, params=None, price=None):
    inputs = [_ref("score", score), _ref("tradable_mask", tradable), _ref("tiers", tiers)]
    if price is not None:
        inputs.append(_ref("price", price))
    call = Call.create(
        capability="allocate", station="Portfolio", impl=ALLOC_IMPL, impl_version="1",
        params=params or {},
        inputs=tuple(inputs),
        policy_view={} if cal == "missing_block" else
        ({"allocate": cal} if cal is not None else {"allocate": _alloc_cal()}),
    )
    return run(call)


def _run_backtest(position, price, tiers, funding=None, *, detail="summary",
                  cal=None, params=None, time=None):
    inputs = [_ref("position", position), _ref("price", price)]
    inputs.append(None if funding is None else _ref("funding", funding))
    inputs.append(_ref("tiers", tiers))
    if time is not None:
        inputs.append(_ref("time", time))
    p = {"detail": detail}
    p.update(params or {})
    call = Call.create(
        capability="backtest", station="Evidence", impl=BT_IMPL, impl_version="1",
        params=p, inputs=tuple(inputs),
        policy_view={"net": _net_cal() if cal is None else cal},
    )
    return run(call)


# ---------------------------------------------------------------- 1. quantiles 路径
def test_allocate_quantiles_shape_nan_exposure_and_cap():
    rng = np.random.default_rng(7)
    t_len, n_sym = 30, 5
    score = rng.normal(size=(t_len, n_sym))
    score[0, 0] = np.nan  # 缺失格不得进权重
    tradable = np.ones((t_len, n_sym), dtype=bool)
    tiers = np.tile(np.arange(n_sym) % 3, (t_len, 1))

    out = _run_allocate(score, tradable, tiers)
    w = out["weights"]
    assert isinstance(w, np.ndarray) and w.shape == (t_len, n_sym)
    assert w.dtype == np.float64
    assert np.isfinite(w).all()
    assert w[0, 0] == 0.0                       # NaN 清零
    assert np.abs(w).max() <= 0.4 + 1e-12       # max_weight 约束
    # 多空两侧都被 0.4 截断，净敞口仍为 0（双侧对称）
    assert np.allclose(w.sum(axis=1), 0.0, atol=1e-12)
    assert out["weights_ref"] == array_digest(w)
    assert isinstance(out["effective_members"], int)
    assert out["turnover_series"].shape == (t_len,)
    assert "max_weight" in out["binding_constraints"]


def test_allocate_effective_members_is_min_not_mean():
    rng = np.random.default_rng(11)
    score = rng.normal(size=(20, 6))
    tradable = np.ones((20, 6), dtype=bool)
    tiers = np.zeros((20, 6), dtype=int)
    # top_k=2：正常行 2 多 + 2 空 = 4 个持仓；每 5 行的稀疏行只有 3 个成员
    # （仍 >= min_members=2）→ 2 多 + 1 空 = 3 个持仓。
    for t in range(20):
        if t % 5 == 4:
            tradable[t, 3:] = False
    out = _run_allocate(score, tradable, tiers, params={"top_k": 2},
                        cal=_alloc_cal(min_members=2, max_weight=0.5))
    held = np.count_nonzero(out["weights"], axis=1)
    assert held.min() < held.max()
    assert out["effective_members"] == int(held.min())
    assert out["effective_members"] < held.mean()


def test_allocate_dropped_bar_below_min_members_is_all_zero():
    score = np.arange(15, dtype=float).reshape(5, 3)
    tradable = np.ones((5, 3), dtype=bool)
    tradable[2] = [True, False, False]  # 仅 1 个成员 < min_members=2
    tiers = np.zeros((5, 3), dtype=int)
    out = _run_allocate(score, tradable, tiers)
    assert np.all(out["weights"][2] == 0.0)
    dropped = out["dropped_bars"]
    assert dropped["by_reason"]["insufficient_members"] >= 1
    # 原因码之和必须等于 count（别名重复计数会把「丢了多少根」算成两倍）
    assert dropped["count"] == sum(dropped["by_reason"].values())
    assert "min_members" in out["binding_constraints"]


# ---------------------------------------------------------------- 2. top_k 路径
def test_allocate_top_k_fixed_k_and_no_overlap():
    rng = np.random.default_rng(3)
    t_len, n_sym = 20, 5
    score = rng.normal(size=(t_len, n_sym))
    tradable = np.ones((t_len, n_sym), dtype=bool)
    tiers = np.zeros((t_len, n_sym), dtype=int)
    # 某一行只剩 2 个可交易成员（仍 >= min_members），k=2 不得随 N(t) 变大
    tradable[5] = [True, True, False, False, False]

    out = _run_allocate(score, tradable, tiers, params={"top_k": 2},
                        cal=_alloc_cal(max_weight=0.5))
    w = out["weights"]
    n_long = (w > 0).sum(axis=1)
    n_short = (w < 0).sum(axis=1)
    assert np.all(n_long <= 2) and np.all(n_short <= 2)
    assert np.all(n_long[:5] == 2) and np.all(n_short[:5] == 2)
    # 稀疏行：k 仍是 2（多头最多 2），不因 N(t)=2 膨胀；空头让位而无重叠
    assert n_long[5] <= 2 and n_short[5] <= 2
    assert not np.any((w > 0) & (w < 0))


# ---------------------------------------------------------------- 2b. inv_vol 口径
def _two_vol_prices(t_len: int, n_sym: int = 6):
    """价格面板：0 号低波动、1/2 号高波动，其余持平。"""
    rng = np.random.default_rng(23)
    price = np.full((t_len, n_sym), 100.0, dtype="float64")
    price[:, 0] = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.001, t_len))
    price[:, 1] = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.03, t_len))
    price[:, 2] = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.03, t_len))
    return price


def test_allocate_inv_vol_weights_by_price_volatility():
    """`weighting=inv_vol` 必须读价格波动：低波动标的权重更大。"""
    t_len = 40
    price = _two_vol_prices(t_len)
    score = np.zeros((t_len, 6))
    score[:, :3] = 1.0   # 0/1/2 同分进多头，权重差异只来自波动
    score[:, 3:] = -1.0
    mask = np.ones((t_len, 6), dtype=bool)
    tiers = np.zeros((t_len, 6), dtype=int)

    out = _run_allocate(score, mask, tiers, price=price,
                        cal=_alloc_cal(quantiles=2, long_short=False, weighting="inv_vol",
                                       max_weight=0.4, net_exposure=1.0,
                                       turnover_max=1.0, min_members=2))
    w = out["weights"]
    tail = slice(t_len // 2, t_len)  # 跳过波动还没估出来的前几根
    # 低波动（0 号）比两个高波动标的都拿到更多权重；多头满仓、其余为 0。
    assert np.all(w[tail, 0] > w[tail, 1])
    assert np.all(w[tail, 0] > w[tail, 2])
    assert np.allclose(w[tail].sum(axis=1), 1.0, atol=1e-9)
    assert np.allclose(w[tail, 3:], 0.0)


def test_allocate_inv_vol_without_price_fails_closed():
    """没有 price 时不许拿分数波动冒充资产波动：E3003。"""
    score = np.zeros((10, 4))
    mask = np.ones((10, 4), dtype=bool)
    tiers = np.zeros((10, 4), dtype=int)
    assert _code(lambda: _run_allocate(
        score, mask, tiers,
        cal=_alloc_cal(weighting="inv_vol"))) == E3003


# ---------------------------------------------------------------- 2c. cvxpy 约束路径
def test_allocate_constraint_path_uses_cvxpy_once_and_keeps_position_shape():
    rng = np.random.default_rng(19)
    score = rng.normal(size=(6, 6))
    tradable = np.ones((6, 6), dtype=bool)
    tiers = np.tile(np.arange(3), (6, 2))

    out = _run_allocate(score, tradable, tiers,
                        cal=_alloc_cal(mode="constraint", max_weight=0.2,
                                       net_exposure=0.0, turnover_max=0.5))

    w = out["weights"]
    assert isinstance(w, np.ndarray) and w.shape == score.shape
    assert np.isfinite(w).all()
    assert np.abs(w).max() <= 0.2 + 1e-9
    assert np.allclose(w.sum(axis=1), 0.0, atol=1e-6)
    assert np.all(np.abs(w).sum(axis=1) <= 1.0 + 1e-6)
    assert out["weights_ref"] == array_digest(w)
    assert out["turnover_series"].shape == (6,)
    assert "net_exposure" in out["binding_constraints"]
    assert set(out["dropped_bars"]["by_reason"]) == {
        "insufficient_members", "no_tradable", "width_below_min"}
    assert out["dropped_bars"]["count"] == sum(
        out["dropped_bars"]["by_reason"].values())


# ---------------------------------------------------------------- 3. 口径 / 参数错误
def test_allocate_missing_caliber_key_is_e1008():
    bad = _alloc_cal()
    bad.pop("quantiles")
    assert _code(lambda: _run_allocate(np.zeros((5, 2)), np.ones((5, 2), bool),
                                       np.zeros((5, 2), int), cal=bad)) == E1008
    assert _code(lambda: _run_allocate(np.zeros((5, 2)), np.ones((5, 2), bool),
                                       np.zeros((5, 2), int), cal="missing_block")) == E1008


def test_allocate_caliber_field_in_params_is_e1003_and_bad_enum_is_e1004():
    score = np.zeros((5, 2))
    mask = np.ones((5, 2), bool)
    tiers = np.zeros((5, 2), int)
    # 口径字段写进 params → 运行期第 2 步 E1003
    assert _code(lambda: _run_allocate(score, mask, tiers,
                                       params={"max_weight": 0.1})) == E1003
    # 非法枚举 → E1004
    assert _code(lambda: _run_allocate(score, mask, tiers,
                                       cal=_alloc_cal(weighting="not_a_weighting"))) == E1004


# ---------------------------------------------------------------- 4. summary 净收益 / NEXT_BAR
def test_backtest_summary_net_return_and_next_bar_lag(monkeypatch):
    captured: list[np.ndarray] = []
    orig = backtest_mod.array_digest

    def spy(arr):
        captured.append(np.array(arr, copy=True))
        return orig(arr)

    monkeypatch.setattr(backtest_mod, "array_digest", spy)

    price = np.array([[100.0], [120.0], [150.0], [180.0]])
    pos = np.array([[0.0], [1.0], [1.0], [0.0]])
    tiers = np.array([[0], [0], [1], [0]])
    out = _run_backtest(pos, price, tiers,
                        cal=_net_cal(cost_tiers=[20, 60, 150], slippage="tiered"))

    # 手算：prev_w=[0,0,1,1]；ret=[-,0.2,0.25,0.2]；
    # turnover=[0,0.5,0,0.5]；fee=[-,0.001,0,0.001]，slip=[-,0.0005,0,0.0005]
    expected = np.array([0.0, -0.0015, 0.25, 0.1985])
    net = captured[0]
    assert net.shape == (4,)
    assert np.allclose(net, expected, atol=1e-9)

    # NEXT_BAR：第 1 根收益由 w[0]=0 兑现（毛收益 0），不是 w[1]=1；
    # 第 2 根由 w[1]=1 兑现（毛收益 0.25）。
    assert net[1] == pytest.approx(-0.0015, abs=1e-9)
    assert net[2] == pytest.approx(0.25, abs=1e-9)

    cb = out["cost_breakdown"]
    assert cb["fee"] == pytest.approx(0.002, abs=1e-12)
    assert cb["slippage"] == pytest.approx(0.001, abs=1e-12)
    assert cb["total"] == pytest.approx(0.003, abs=1e-12)
    assert cb["funding"] == 0.0
    assert out["annualisation_used"] == "calendar_365"


# ---------------------------------------------------------------- 5. 成本
def test_backtest_tiered_costs_more_than_none():
    price = np.full((6, 1), 100.0)  # 零收益，成本可直接比较
    pos = np.array([[0.0], [1.0], [0.0], [1.0], [0.0], [1.0]])
    tiers = np.zeros((6, 1), dtype=int)
    none_out = _run_backtest(pos, price, tiers,
                             cal=_net_cal(slippage="none", cost_tiers=[20, 60, 150]))
    tier_out = _run_backtest(pos, price, tiers,
                             cal=_net_cal(slippage="tiered", cost_tiers=[20, 60, 150]))
    assert none_out["cost_breakdown"]["slippage"] == 0.0
    assert tier_out["cost_breakdown"]["slippage"] > 0.0
    assert tier_out["cost_breakdown"]["total"] > none_out["cost_breakdown"]["total"] > 0.0
    # cost 来自换手，不是名义额：零收益也不影响成本比较
    assert np.allclose(none_out["turnover"], tier_out["turnover"])


def test_backtest_funding_only_when_enabled_and_model_allows():
    price = np.full((6, 1), 100.0)
    pos = np.ones((6, 1))  # 建仓后不换手 → 手续费为 0
    tiers = np.zeros((6, 1), dtype=int)
    funding = np.full((6, 1), 0.0001)
    on = _run_backtest(pos, price, tiers, funding=funding,
                       cal=_net_cal(fee_model="taker_with_funding"),
                       params={"include_funding": True})
    off = _run_backtest(pos, price, tiers, funding=funding,
                        cal=_net_cal(fee_model="taker_with_funding"))
    other = _run_backtest(pos, price, tiers, funding=funding,
                          cal=_net_cal(fee_model="taker"),
                          params={"include_funding": True})
    # prev_w=[0,1,1,1,1,1] → 5 根持仓 × 0.0001
    assert on["funding_total"] == pytest.approx(5 * 0.0001, abs=1e-12)
    assert off["funding_total"] == 0.0
    assert other["funding_total"] == 0.0


def test_backtest_tier_minus_one_creates_no_trade_cost():
    price = np.full((3, 1), 100.0)
    pos = np.array([[0.0], [1.0], [0.0]])
    all_in = np.zeros((3, 1), dtype=int)
    mid_out = np.array([[0], [-1], [0]])
    cal = _net_cal(slippage="tiered", cost_tiers=[20, 60, 150])
    a = _run_backtest(pos, price, all_in, cal=cal)
    b = _run_backtest(pos, price, mid_out, cal=cal)
    assert b["cost_breakdown"]["total"] < a["cost_breakdown"]["total"]
    assert b["cost_breakdown"]["total"] == pytest.approx(
        a["cost_breakdown"]["total"] / 2.0, abs=1e-12)


# ---------------------------------------------------------------- 6. full vs summary 同源
def test_backtest_full_and_summary_share_the_same_fields():
    price = np.array([[100.0], [200.0], [400.0], [800.0]])
    pos = np.array([[1.0], [0.0], [1.0], [0.0]])
    tiers = np.zeros((4, 1), dtype=int)
    s = _run_backtest(pos, price, tiers, detail="summary")
    f = _run_backtest(pos, price, tiers, detail="full")

    shared = ["net_return_ref", "net_return", "cost_breakdown", "turnover",
              "funding_total", "sharpe", "annualisation_used",
              "bars_per_year", "bars_per_year_source", "missing_price_cells"]
    assert set(s) == set(shared)
    for key in shared:
        if isinstance(s[key], np.ndarray):
            assert np.array_equal(s[key], f[key]), key
        else:
            assert s[key] == f[key], key

    extra = ["ledger_ref", "fills_ref", "equity_ref",
             "long_short_split", "drawdown_episodes", "attribution"]
    assert all(k in f for k in extra)
    for key in ("ledger_ref", "fills_ref", "equity_ref"):
        assert isinstance(f[key], str) and f[key].startswith("sha256:")
    assert isinstance(f["drawdown_episodes"], list)
    assert set(f["long_short_split"]) == {"long", "short"}
    assert {"fee", "slippage", "funding", "total"} <= set(f["attribution"])


def test_backtest_summary_with_leverage_still_walks_the_full_ledger():
    """AC28：声明允许杠杆后请求 summary 也必须走完整账本，不能有捷径路径。

    判据与两档同源共用：summary 的共享项必须逐项等于 full 的同名字段——只要
    summary 走了另一条简化路径（例如按名义额估成本），这里就会分叉。

    注意本实现（向量化参照）**不做强平模拟**，所以总敞口不能超过 1：
    「声明 max_leverage=2 但实际不超」是被允许的（口径允许 ≠ 必须使用），
    真正的杠杆回测由 ml4t 实现负责（见 `test_leverage_above_one_is_rejected`）。
    """
    price = np.array([[100.0], [200.0], [400.0], [800.0]])
    pos = np.array([[1.0], [0.0], [1.0], [0.0]])  # gross 1.0，未使用杠杆
    tiers = np.zeros((4, 1), dtype=int)
    cal = _net_cal(max_leverage=2.0)
    s = _run_backtest(pos, price, tiers, detail="summary", cal=cal)
    f = _run_backtest(pos, price, tiers, detail="full", cal=cal)
    for key in ["net_return_ref", "net_return", "cost_breakdown", "turnover",
                "funding_total", "sharpe", "annualisation_used",
                "bars_per_year", "bars_per_year_source", "missing_price_cells"]:
        if isinstance(s[key], np.ndarray):
            assert np.array_equal(s[key], f[key]), key
        else:
            assert s[key] == f[key], key


def test_leverage_above_one_is_rejected_by_vectorised_backtest():
    """总敞口 > 1 必须 fail-closed：向量化版没有保证金与强平模拟。

    放行的话会得到「加了杠杆却永不爆仓」的净表现——数字看着正常、结论不可信。
    这是能力层的显式降级，不是静默忽略口径字段。
    """
    from v5.contracts.errors import E1004

    price = np.array([[100.0], [200.0], [400.0], [800.0]])
    pos = np.array([[2.0], [0.0], [2.0], [0.0]])
    tiers = np.zeros((4, 1), dtype=int)
    with pytest.raises(FlowError) as exc:
        _run_backtest(pos, price, tiers, detail="summary",
                      cal=_net_cal(max_leverage=2.0))
    assert exc.value.error.code == E1004
    assert "强平" in exc.value.error.message or "杠杆" in exc.value.error.message


# ---------------------------------------------------------------- 7. 年化 sharpe
def test_backtest_sharpe_formula_and_zero_std(monkeypatch):
    captured: list[np.ndarray] = []
    orig = backtest_mod.array_digest

    def spy(arr):
        captured.append(np.array(arr, copy=True))
        return orig(arr)

    monkeypatch.setattr(backtest_mod, "array_digest", spy)

    # 有波动：sharpe = mean/std * sqrt(365)，用每期净收益、真实期数
    price = np.array([[100.0], [200.0], [400.0], [800.0]])  # 每根 +100%
    pos = np.array([[1.0], [0.0], [1.0], [0.0]])  # 总敞口 ≤1（向量化版不支持杠杆）
    tiers = np.zeros((4, 1), dtype=int)
    out = _run_backtest(pos, price, tiers,
                        cal=_net_cal(cost_tiers=[0], slippage="none"))
    net = captured[0]
    periods = net[1:]
    assert periods.size == 3
    manual = float(np.mean(periods)) / float(np.std(periods)) * math.sqrt(365)
    assert out["sharpe"] == pytest.approx(manual, abs=1e-9)
    assert out["bars_per_year"] == 365.0
    assert out["bars_per_year_source"] == "assumed_daily"

    # std=0：sharpe=0，不返回 NaN，并在 cost_breakdown 标注
    flat_price = np.array([[100.0], [200.0], [400.0], [800.0], [1600.0]])
    flat_pos = np.ones((5, 1))
    flat_tiers = np.zeros((5, 1), dtype=int)
    flat = _run_backtest(flat_pos, flat_price, flat_tiers,
                         cal=_net_cal(cost_tiers=[0], slippage="none"))
    assert flat["sharpe"] == 0.0
    assert math.isfinite(flat["sharpe"])
    assert flat["cost_breakdown"].get("sharpe_degenerate") is True


def test_backtest_bars_per_year_follows_the_time_axis():
    """年化基准是 365 天，但每年 bar 数由频率决定：1h → 8760，4h → 2190。"""
    price = np.full((5, 1), 100.0)
    pos = np.ones((5, 1))
    tiers = np.zeros((5, 1), dtype=int)
    base = 1_700_000_000_000_000_000
    for hours, expected in ((1, 365 * 24), (4, 365 * 6)):
        axis = np.array([base + i * hours * 3_600_000_000_000
                         for i in range(5)], dtype="int64")
        out = _run_backtest(pos, price, tiers, time=axis)
        assert out["bars_per_year"] == pytest.approx(expected)
        assert out["bars_per_year_source"] == "time_axis_median"
        # 口径本身没变：年化仍以日历 365 天为基。
        assert out["annualisation_used"] == "calendar_365"


# ---------------------------------------------------------------- 8. 输入 / 参数错误
def test_backtest_input_misalignment_and_missing_detail():
    price = np.full((4, 1), 100.0)
    pos = np.ones((4, 1))
    tiers = np.zeros((4, 1), dtype=int)

    def call_of(inputs, params):
        return Call.create(
            capability="backtest", station="Evidence", impl=BT_IMPL, impl_version="1",
            params=params, inputs=tuple(inputs), policy_view={"net": _net_cal()},
        )

    # 输入错位：position / price 顺序颠倒 → kind 不符 E3001
    swapped = [_ref("price", price), _ref("position", pos), None, _ref("tiers", tiers)]
    assert _code(lambda: run(call_of(swapped, {"detail": "summary"}))) == E3001
    # 缺必填输入 tiers（占位 None）→ E3001
    missing = [_ref("position", pos), _ref("price", price), None, None]
    assert _code(lambda: run(call_of(missing, {"detail": "summary"}))) == E3001
    # detail 缺失 → E1008
    good = [_ref("position", pos), _ref("price", price), None, _ref("tiers", tiers)]
    assert _code(lambda: run(call_of(good, {}))) == E1008
