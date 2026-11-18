"""S4 生产回测实现守卫：唯一账本必须来自 ml4t，且两档同源。

`ops/backtest.py` 的向量化实现只作数值参照（见 test_v5_s4_allocate_backtest.py），
不注册为生产实现。本文件钉住生产 `ledger@1` 的三条底线：

1. 真的调用 `ml4t.backtest.Engine`，且用 `ExecutionMode.NEXT_BAR` + `calendar=crypto`；
2. `summary` / `full` 是同一次内部结果的两档投影，共享字段逐项相等；
3. V5 的 `caliber.net` 口径生效：tiered 成本高于 none，资金费只在允许时计入；
4. 年化按 `time` 输入推导每年 bar 数（1h 网格 → 8760），不是硬编码 365。
"""

from __future__ import annotations

import numpy as np
import pytest

import v5.capability.backtest_engine as engine
from v5.capability import Call, InputRef, run
from v5.capability.impls import install_default_impls
from v5.capability.panel import array_digest
from v5.capability.runtime import reset_impls
from v5.contracts.errors import E3003, FlowError

# `net_return` 是内存通道（落盘身份仍是 net_return_ref）：认证要逐折复算
# 净表现，只拿 digest 算不出 Sharpe 与回撤。
SHARED = ["net_return_ref", "net_return", "cost_breakdown", "turnover",
          "funding_total", "sharpe", "annualisation_used", "bars_per_year",
          "bars_per_year_source", "missing_price_cells"]
EXTRA = ["ledger_ref", "fills_ref", "equity_ref", "long_short_split",
         "drawdown_episodes", "attribution"]
_NS_PER_HOUR = 3_600_000_000_000
_NS_PER_DAY = 86_400_000_000_000


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _data():
    t_len, n_sym = 6, 3
    price = np.array([
        [100.0, 50.0, 20.0], [101.0, 49.0, 21.0], [103.0, 50.0, 22.0],
        [102.0, 51.0, 23.0], [105.0, 52.0, 24.0], [106.0, 53.0, 25.0],
    ])
    pos = np.array([
        [0.0, 0.0, 0.0], [0.2, -0.2, 0.0], [0.2, -0.2, 0.0],
        [0.1, -0.1, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0],
    ])
    tiers = np.tile(np.arange(n_sym), (t_len, 1))
    # 不同标的费率不同，避免多空腿在同一 rate 上精确抵消，导致测试假阴性。
    funding = np.tile(np.array([0.0001, 0.0002, 0.0003]), (t_len, 1))
    return price, pos, tiers, funding


def _cal(**over):
    cal = dict(cost_tiers=[20, 60, 150], slippage="none", fee_model="taker",
               max_leverage=1.0, initial_margin=0.1, maintenance_margin=0.01,
               liquidation_buffer=0.01)
    cal.update(over)
    return cal


def _run(detail="summary", *, cal=None, include_funding=False, use_funding=False,
         time=None):
    price, pos, tiers, funding = _data()
    inputs = [
        InputRef("position", array_digest(pos), data=pos),
        InputRef("price", array_digest(price), data=price),
        _ref("funding", funding) if use_funding else None,
        InputRef("tiers", array_digest(tiers), data=tiers),
    ]
    if time is not None:
        inputs.append(InputRef("time", array_digest(time), data=time))
    call = Call.create(
        capability="backtest", station="Portfolio", impl="ledger", impl_version="1",
        params={"detail": detail, "include_funding": include_funding},
        inputs=tuple(inputs),
        policy_view={"net": _cal() if cal is None else cal},
    )
    return run(call)


def _ref(kind, data):
    return InputRef(kind, array_digest(data), data=data)


def test_production_backtest_calls_ml4t_with_crypto_calendar(monkeypatch):
    seen: dict[str, object] = {}
    original = engine.Engine

    class Spy(original):  # type: ignore[misc, valid-type]
        def __init__(self, feed, strategy, config, **kwargs):
            seen["config"] = config
            super().__init__(feed, strategy, config, **kwargs)

    monkeypatch.setattr(engine, "Engine", Spy)

    out = _run("summary")

    assert "config" in seen
    cfg = seen["config"]
    assert cfg.execution_mode == engine.ExecutionMode.NEXT_BAR
    assert str(cfg.calendar) == "crypto"
    assert cfg.allow_leverage is False
    assert set(out) == set(SHARED)


def test_summary_and_full_share_the_same_fields():
    s = _run("summary")
    f = _run("full")
    assert set(s) == set(SHARED)
    assert set(EXTRA) <= set(f)
    for key in SHARED:
        if isinstance(s[key], np.ndarray):
            assert np.array_equal(s[key], f[key]), key
        else:
            assert s[key] == f[key], key
    for key in ("ledger_ref", "fills_ref", "equity_ref"):
        assert isinstance(f[key], str) and f[key].startswith("sha256:")
    assert isinstance(f["drawdown_episodes"], list)
    assert set(f["long_short_split"]) == {"long", "short"}
    assert {"fee", "slippage", "funding", "total"} <= set(f["attribution"])


# ------------------------------------------------------------ 年化频率
def test_missing_time_falls_back_to_daily_annualisation():
    out = _run("summary")
    assert out["bars_per_year"] == 365.0
    assert out["bars_per_year_source"] == "assumed_daily"
    assert out["annualisation_used"] == "calendar_365"


def test_hourly_time_axis_annualises_with_8760_bars():
    """1h 网格：同一批净收益，年化 Sharpe 必须是日频的 sqrt(24) 倍。

    硬编码 `sqrt(365)` 会让小时线策略的 Sharpe 被系统性低估约 4.9 倍，
    这正是「接非日频前必须补 bar 频率」的那条缺口。
    """
    t_len = 6
    base = 1_700_000_000_000_000_000
    daily = np.array([base + i * _NS_PER_DAY for i in range(t_len)], dtype="int64")
    hourly = np.array([base + i * _NS_PER_HOUR for i in range(t_len)], dtype="int64")
    d = _run("summary", time=daily)
    h = _run("summary", time=hourly)
    assert d["bars_per_year"] == pytest.approx(365.0)
    assert h["bars_per_year"] == pytest.approx(365.0 * 24)
    assert h["bars_per_year_source"] == "time_axis_median"
    assert np.array_equal(d["turnover"], h["turnover"])
    assert h["sharpe"] == pytest.approx(d["sharpe"] * np.sqrt(24.0), rel=1e-12)


def test_irregular_time_axis_uses_median_step():
    """缺口不改变标称频率：中位步长仍是 1h，年化不该被缺口拉低。"""
    base = 1_700_000_000_000_000_000
    steps = [_NS_PER_HOUR] * 3 + [_NS_PER_HOUR * 5, _NS_PER_HOUR]
    axis = np.array([base], dtype="int64")
    for step in steps:
        axis = np.append(axis, axis[-1] + step)
    out = _run("summary", time=axis)
    assert out["bars_per_year"] == pytest.approx(365.0 * 24)
    assert out["bars_per_year_source"] == "time_axis_median"


def test_time_axis_must_match_bar_count():
    base = 1_700_000_000_000_000_000
    short = np.array([base, base + _NS_PER_DAY], dtype="int64")
    with pytest.raises(FlowError) as exc:
        _run("summary", time=short)
    assert exc.value.error.code == E3003


def test_time_axis_must_be_strictly_increasing():
    base = 1_700_000_000_000_000_000
    axis = np.array([base + _NS_PER_DAY] * 3 + [base] * 3, dtype="int64")
    with pytest.raises(FlowError) as exc:
        _run("summary", time=axis)
    assert exc.value.error.code == E3003


def test_v5_tiered_slippage_costs_are_charged():
    none = _run("summary", cal=_cal(slippage="none"))
    tiered = _run("summary", cal=_cal(slippage="tiered"))
    assert none["cost_breakdown"]["slippage"] == 0.0
    assert tiered["cost_breakdown"]["slippage"] > 0.0
    assert tiered["cost_breakdown"]["total"] > none["cost_breakdown"]["total"]
    assert np.array_equal(none["turnover"], tiered["turnover"])


def test_funding_only_when_model_and_switch_allow_it():
    cal = _cal(fee_model="taker_with_funding")
    on = _run("summary", cal=cal, include_funding=True, use_funding=True)
    off = _run("summary", cal=cal, include_funding=False, use_funding=True)
    blocked = _run("summary", cal=_cal(fee_model="taker"),
                   include_funding=True, use_funding=True)
    # 多空腿费率不同，净额可以为负；关键是「真的计了」，不是强制为正。
    assert abs(on["funding_total"]) > 0.0
    assert off["funding_total"] == 0.0
    assert blocked["funding_total"] == 0.0


# --------------------------------------------------- 价格缺失（B1）








# --------------------------------------------------- 滑点出口（B3）




# --------------------------------------------------- 价格缺失（B1）
def _call_with_price(price, pos, tiers):
    return Call.create(
        capability="backtest", station="Portfolio", impl="ledger", impl_version="1",
        params={"detail": "summary", "include_funding": False},
        inputs=(
            InputRef("position", array_digest(pos), data=pos),
            InputRef("price", array_digest(price), data=price),
            None,
            InputRef("tiers", array_digest(tiers), data=tiers),
        ),
        policy_view={"net": _cal()},
    )


def test_missing_price_cells_are_tolerated_and_counted():
    """个别标的个别 bar 无价 → 不建价、不成交，**不是**整条回测失败。

    旧实现要求全量有限正数，扩标的（未上市/停牌/退市）时整条回测直接 E3003。
    缺失格的语义是「这根 bar 没交易」，与真实停牌一致。
    """
    price, pos, tiers, _ = _data()
    price = price.copy()
    price[2, 1] = np.nan          # S1 在第 3 根 bar 无价
    price[4, 2] = np.nan
    out = run(_call_with_price(price, pos, tiers))
    assert out["missing_price_cells"] == 2
    assert np.isfinite(out["net_return"]).all()


def test_symbol_with_no_price_at_all_is_rejected():
    """整列没有有效价 = 标的集/区间写错，不是行情缺失。"""
    price, pos, tiers, _ = _data()
    price = price.copy()
    price[:, 2] = np.nan
    with pytest.raises(FlowError) as exc:
        run(_call_with_price(price, pos, tiers))
    assert exc.value.error.code == E3003
    assert "没有任何有效价格" in exc.value.error.message


def test_bar_with_no_price_at_all_is_rejected():
    """整根 bar 全标的无价 = 时间轴断裂，不是停牌。

    ml4t 会把两侧拼成相邻 bar，净值曲线跨过一段不存在的行情却看不出来——
    这是必须停的一类缺口。
    """
    price, pos, tiers, _ = _data()
    price = price.copy()
    price[3, :] = np.nan
    with pytest.raises(FlowError) as exc:
        run(_call_with_price(price, pos, tiers))
    assert exc.value.error.code == E3003
    assert "全部标的都没有价格" in exc.value.error.message


# --------------------------------------------------- 滑点出口（B3）
def test_explicit_slippage_bp_overrides_derived_value():
    """`caliber.net.slippage_bp` 一旦给出就覆盖「手续费一半」的派生规则。

    派生规则是假设：真实滑点取决于下单方式与盘口深度，与 taker 费率没有
    因果关系。没有出口字段时，净收益被一个隐式常数决定且写不进报告。
    """
    base = _run("summary", cal=_cal(slippage="fixed"))
    explicit = _run("summary", cal=_cal(slippage="fixed", slippage_bp=10.0))
    # fixed 派生 = 最低档 20bp 的一半 = 10bp —— 数值相同，用它验证等价性
    assert explicit["cost_breakdown"]["slippage"] == pytest.approx(
        base["cost_breakdown"]["slippage"], rel=1e-12)
    # 换成 30bp：滑点成本必须正好是 3 倍
    higher = _run("summary", cal=_cal(slippage="fixed", slippage_bp=30.0))
    assert higher["cost_breakdown"]["slippage"] == pytest.approx(
        explicit["cost_breakdown"]["slippage"] * 3.0, rel=1e-12)
    # slippage=none 仍然为 0，显式值不得把它「救回来」
    off = _run("summary", cal=_cal(slippage="none", slippage_bp=99.0))
    assert off["cost_breakdown"]["slippage"] == 0.0


def test_derived_slippage_unchanged_when_bp_absent():
    """缺省路径逐位不变：既有计划的净收益不因新增可选字段而漂移。"""
    a = _run("summary", cal=_cal(slippage="tiered"))
    b = _run("summary", cal=_cal(slippage="tiered", slippage_bp=None))
    assert a["cost_breakdown"] == b["cost_breakdown"]

def test_synthetic_symbol_names_do_not_enter_committed_bytes(monkeypatch):
    """B4：换掉 ml4t 中间帧的 asset 名，所有落盘字节必须逐位一致。

    这不是「收益低所以不改」的托辞：symbols 只做 fills→列下标的临时字典键，
    `fills_ref` 落的是数值数组摘要。把这件事写成断言，才是真的闭合。
    """
    baseline = _run(detail="full")
    monkeypatch.setattr(engine, "_symbol_names",
                        lambda n: ("BTCUSDT", "ETHUSDT", "SOLUSDT")[:n])
    renamed = _run(detail="full")
    assert renamed.keys() == baseline.keys()
    for key in baseline:
        left, right = baseline[key], renamed[key]
        if isinstance(left, np.ndarray):
            np.testing.assert_array_equal(right, left, err_msg=key)
        else:
            assert right == left, key

