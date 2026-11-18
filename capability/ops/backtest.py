"""能力实现 · backtest：成本引擎；summary / full 两档来自同一份账本。

一条链、一处真相（01-capability.md §6）：

    权重 → 成交模拟 → Ledger(唯一真相) ├─ aggregate(summary) → 净表现摘要
                                      └─ aggregate(full)    → 逐笔 + 账本 + 归因

两档**不做两次计算**：先一次性算出 ledger / 逐格成本 / 净收益序列，再由同一个
内部结果投影出两档返回。summary 的六个字段与 full 的同名字段是同一批对象，
因此「判据用的净收益」与「报告里的净收益」在构造上就是同一个数（I12）。

成本只认换手（06-evaluation.md §5.3）：`cost = turnover × (fee_rate + slippage_rate)`，
逐时点、逐档位计算，绝不按名义额固定摊。资金费按每 bar 费率逐 bar 求和，
没有 `funding_period`——结算周期是数据属性（I34），1h/4h/8h 网格由上游聚合。

收益兑现滞后一根 bar（NEXT_BAR）：`net_return[t]` 用 `w[t-1]` 吃
`price[t]/price[t-1]-1`，`t=0` 恒为 0。

年化口径（§5.4）：基准是日历 365 天/年（`calendar_365`，加密 24/7 无休市），
但**每年多少根 bar 由数据频率决定**——1d 是 365，1h 是 8760。所以 sharpe 用
`mean/std × sqrt(bars_per_year)`，`bars_per_year` 从 `time` 输入（UTC 纳秒）
的中位步长推导。缺 `time` 时按日频 365 假设，并在 `bars_per_year_source`
标注 `assumed_daily`：日频是当前唯一供给频率，但接非日频供给却不传 `time`
就是静默按年产 365 次折算，年报 Sharpe 会与实际频率差 sqrt(24) 倍。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from capability.panel import array_digest
from contracts.errors import E1004, E1008, E3001, E3003, FlowError

from capability.timeaxis import CALENDAR_DAYS_PER_YEAR
_ANNUALISATION = CALENDAR_DAYS_PER_YEAR
_NS_PER_DAY = 86_400_000_000_000
_NS_PER_YEAR = _ANNUALISATION * _NS_PER_DAY
_SLIPPAGES = frozenset({"none", "fixed", "tiered"})
_FEE_MODELS = frozenset({"taker", "maker", "taker_with_funding"})
_EPS = 1e-12


# ---------------------------------------------------------------- 口径校验
def _caliber(call) -> dict:
    view = call.policy_view or {}
    block = view.get("net")
    if block is None:
        raise FlowError(
            E1008, "policy_view 缺 net 子块", path="policy_view.net",
            hint="成本口径来自 caliber.net；能力不自选口径",
        )
    if not isinstance(block, Mapping):
        raise FlowError(E1004, "policy_view.net 必须是映射", path="policy_view.net")
    return dict(block)


def _require(cal: Mapping[str, Any], key: str) -> Any:
    if key not in cal:
        raise FlowError(
            E1008, f"caliber.net 缺必填口径 {key}", path=f"policy_view.net.{key}",
        )
    return cal[key]


def _float_field(cal: Mapping[str, Any], key: str, *, gt: float | None = None,
                 ge: float | None = None, le: float | None = None) -> float:
    value = _require(cal, key)
    if isinstance(value, (bool, np.bool_)) or not isinstance(
            value, (int, float, np.integer, np.floating)):
        raise FlowError(
            E1004, f"caliber.net.{key} 必须是实数，得到 {type(value).__name__}",
            path=f"policy_view.net.{key}",
        )
    out = float(value)
    if not math.isfinite(out):
        raise FlowError(E1004, f"caliber.net.{key} 必须是有限值，得到 {value!r}",
                        path=f"policy_view.net.{key}")
    if gt is not None and not out > gt:
        raise FlowError(E1004, f"caliber.net.{key}={out} 必须 > {gt}",
                        path=f"policy_view.net.{key}")
    if ge is not None and not out >= ge:
        raise FlowError(E1004, f"caliber.net.{key}={out} 必须 >= {ge}",
                        path=f"policy_view.net.{key}")
    if le is not None and not out <= le:
        raise FlowError(E1004, f"caliber.net.{key}={out} 必须 <= {le}",
                        path=f"policy_view.net.{key}")
    return out


def _enum_field(cal: Mapping[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = _require(cal, key)
    if not isinstance(value, str) or value not in allowed:
        raise FlowError(
            E1004, f"caliber.net.{key}={value!r} 不在白名单 {sorted(allowed)}",
            path=f"policy_view.net.{key}",
        )
    return value


def _cost_tiers(cal: Mapping[str, Any]) -> list[int]:
    value = _require(cal, "cost_tiers")
    if (not isinstance(value, (list, tuple)) or len(value) == 0
            or any(isinstance(x, (bool, np.bool_)) or not isinstance(x, (int, np.integer))
                   for x in value)):
        raise FlowError(
            E1004, "caliber.net.cost_tiers 必须是非空整数列表（bp，单边）",
            path="policy_view.net.cost_tiers",
        )
    tiers = [int(x) for x in value]
    if any(t < 0 for t in tiers):
        raise FlowError(E1004, "cost_tiers 不得为负", path="policy_view.net.cost_tiers")
    if any(b <= a for a, b in zip(tiers, tiers[1:])):
        raise FlowError(
            E1004, f"cost_tiers 必须严格升序，得到 {tiers}",
            path="policy_view.net.cost_tiers",
        )
    return tiers


# ---------------------------------------------------------------- 输入
def _matrix(data: Any, name: str, *, integer: bool = False) -> np.ndarray:
    if not isinstance(data, np.ndarray) or data.ndim != 2:
        raise FlowError(
            E3003, f"backtest 输入 {name} 必须是二维 ndarray",
            path=f"backtest.inputs.{name}",
        )
    if integer:
        if data.dtype.kind not in "iu":
            raise FlowError(
                E3003, f"backtest 输入 {name} 必须是整数数组，得到 {data.dtype}",
                path=f"backtest.inputs.{name}",
            )
        return np.asarray(data, dtype="int64")
    out = np.asarray(data, dtype="float64")
    if not np.isfinite(out).all():
        # position 不允许 NaN/inf；price 的缺失用 NaN 表达并保持 NaN（见 _returns）。
        if name == "position":
            raise FlowError(
                E3003, "backtest 输入 position 含非有限值",
                path="backtest.inputs.position",
            )
    return out


def _times(data: Any, t_len: int) -> np.ndarray:
    """`time` 输入：UTC 纳秒整数一维（允许 `(T,1)`），必须与 bar 数同长。

    不转 float64：纳秒量级约 1.7e18，超过 float64 的 2^53 精确整数上界，
    转一次就会把相邻 bar 压成同一个时间戳。这里保留 int64。
    """
    arr = np.asarray(data)
    if arr.ndim == 2 and arr.shape[1] == 1:
        arr = arr[:, 0]
    if arr.ndim != 1:
        raise FlowError(
            E3003, f"backtest 输入 time 必须是一维或 (T,1)，得到 {arr.shape}",
            path="backtest.inputs.time",
        )
    if arr.dtype.kind not in "iu":
        raise FlowError(
            E3003, f"backtest 输入 time 必须是整数纳秒，得到 {arr.dtype}",
            path="backtest.inputs.time", hint="UTC 纳秒；不要传 datetime 或浮点秒",
        )
    out = arr.astype("int64", copy=True)
    if out.size != t_len:
        raise FlowError(
            E3003, f"backtest 输入 time 长度 {out.size} 与 bar 数 {t_len} 不符",
            path="backtest.inputs.time",
        )
    if out.size > 1 and np.any(np.diff(out) <= 0):
        raise FlowError(
            E3003, "backtest 输入 time 必须严格递增", path="backtest.inputs.time",
            hint="重复/倒序时间戳会让两根 bar 落到同一个时点",
        )
    return out


def bars_per_year(times: np.ndarray | None, t_len: int) -> tuple[float, str]:
    """每年的 bar 数 = 365 天 ÷ 中位 bar 步长。

    返回 `(bars_per_year, source)`。`source` 只有两个合法值：
    `time_axis_median`（由真实时间轴推导）与 `assumed_daily`（缺时间轴，
    按日频 365 假设）。非等距时间轴（采集缺口、周末缺失）取中位步长——
    标称频率才是「一年几次」的口径，缺口不该把年化基准拉低。
    """
    if times is None or t_len < 2:
        return float(_ANNUALISATION), "assumed_daily"
    steps = np.diff(np.asarray(times, dtype="int64"))
    if steps.size == 0:
        return float(_ANNUALISATION), "assumed_daily"
    step = float(np.median(steps))
    if not math.isfinite(step) or step <= 0.0:
        raise FlowError(
            E3003, f"backtest 时间轴步长非法（中位 {step}）",
            path="backtest.inputs.time",
        )
    value = _NS_PER_YEAR / step
    if not math.isfinite(value) or value < 1.0:
        raise FlowError(
            E3003, f"backtest 时间轴步长 {step} ns 过长，一年不足 1 根 bar",
            path="backtest.inputs.time",
        )
    return float(value), "time_axis_median"


def _inputs(call) -> tuple[np.ndarray, np.ndarray, Any, np.ndarray, Any]:
    got: dict[str, Any] = {}
    for item in call.inputs:
        if item is None:
            continue  # funding / time 的显式占位符
        if item.kind in got:
            raise FlowError(
                E3001, f"backtest 重复输入 {item.kind!r}", path=f"backtest.inputs.{item.kind}",
            )
        got[item.kind] = item.data
    for kind in ("position", "price", "tiers"):
        if kind not in got:
            raise FlowError(
                E3001, f"backtest 缺输入 {kind!r}", path=f"backtest.inputs.{kind}",
            )
    position = _matrix(got["position"], "position")
    price = _matrix(got["price"], "price")
    tiers = _matrix(got["tiers"], "tiers", integer=True)
    if position.shape != price.shape or position.shape != tiers.shape:
        raise FlowError(E3001, "backtest 四路输入形状必须一致", path="backtest.inputs")
    funding = got.get("funding")
    if funding is not None:
        funding = _matrix(funding, "funding")
        if funding.shape != position.shape:
            raise FlowError(E3001, "backtest.funding 形状必须与 position 一致",
                            path="backtest.inputs.funding")
    raw_time = got.get("time")
    time = None if raw_time is None else _times(raw_time, position.shape[0])
    return position, price, funding, tiers, time


# ---------------------------------------------------------------- 逐 bar 计算
def _returns(price: np.ndarray) -> np.ndarray:
    """每 bar 收益；价格非有限或 <=0 视作缺失，收益项取 0（不放宽）。"""
    t_len, n_sym = price.shape
    out = np.zeros((t_len, n_sym), dtype="float64")
    if t_len < 2:
        return out
    prev, cur = price[:-1], price[1:]
    ok = np.isfinite(prev) & np.isfinite(cur) & (prev > 0) & (cur > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(ok, cur / np.where(ok, prev, 1.0) - 1.0, 0.0)
    out[1:] = np.where(ok, r, 0.0)
    return out


def _slippage_bp(cal: Mapping[str, Any]) -> float | None:
    """单边滑点（bp）的显式覆盖；未声明返回 None（沿用旧算法）。

    旧口径把滑点绑死在手续费上（`fixed` = 最低档一半，`tiered` = 逐档一半）。
    那是**一个假设**，不是数据事实：真实滑点取决于下单方式与盘口深度，与
    taker 费率没有因果关系。口径里没有字段表达它，用户就写不出真值，
    净收益被一个隐式常数决定。
    """
    value = cal.get("slippage_bp")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FlowError(
            E1004, f"caliber.net.slippage_bp 必须是数，得到 {value!r}",
            path="policy_view.net.slippage_bp",
        )
    out = float(value)
    if not (out >= 0.0) or out != out:
        raise FlowError(
            E1004, f"caliber.net.slippage_bp={out} 必须是非负数",
            path="policy_view.net.slippage_bp",
        )
    return out


def _rate_grids(tiers: np.ndarray, cost_tiers: list[int], slippage: str,
                slippage_bp: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """逐格 (t,j) 的手续费率 / 滑点率；tier=-1 未入池 → 0（不产生成交）。

    滑点优先级：`slippage="none"` > 显式 `slippage_bp` > `tiered`/`fixed`
    的旧派生规则。旧规则保留为缺省，既有计划逐位不变。
    """
    t_len, n_sym = tiers.shape
    fee = np.zeros((t_len, n_sym), dtype="float64")
    slip = np.zeros((t_len, n_sym), dtype="float64")
    in_pool = tiers >= 0
    mapped = np.where(in_pool, np.minimum(tiers, len(cost_tiers) - 1), 0)
    if slippage == "none":
        fixed_slip = 0.0
    elif slippage_bp is not None:
        fixed_slip = float(slippage_bp) / 10000.0
    else:
        # 旧派生：fixed 以最低档 bp 的一半作全池固定滑点；tiered 逐档取半价。
        fixed_slip = cost_tiers[0] / 10000.0 / 2.0
    if slippage_bp is not None and slippage != "none":
        slip = np.where(in_pool, fixed_slip, 0.0)
    elif slippage == "tiered":
        slip_bp = np.asarray(cost_tiers, dtype="float64") / 2.0
        slip = np.where(in_pool, slip_bp[mapped] / 10000.0, 0.0)
    else:
        slip = np.where(in_pool, fixed_slip, 0.0)
    fee_bp = np.asarray(cost_tiers, dtype="float64")
    fee = np.where(in_pool, fee_bp[mapped] / 10000.0, 0.0)
    return fee, slip


def _drawdown_episodes(equity: np.ndarray) -> list[dict]:
    """从净值曲线给出回撤区间（起点 / 谷底 / 恢复点 / 深度）。"""
    if equity.size == 0:
        return []
    peak = -np.inf
    peaks = np.empty(equity.size, dtype="float64")
    for t, value in enumerate(equity):
        peak = max(peak, float(value))
        peaks[t] = peak
    dd = np.where(peaks > 0, equity / peaks - 1.0, 0.0)
    out: list[dict] = []
    start: int | None = None
    trough = 0
    trough_val = 0.0
    for t, value in enumerate(dd):
        if value < -_EPS:
            if start is None:
                start, trough, trough_val = t, t, float(value)
            elif value < trough_val:
                trough, trough_val = t, float(value)
        elif start is not None:
            out.append({
                "start": int(start), "trough": int(trough), "recovery": int(t),
                "depth": float(trough_val),
            })
            start = None
    if start is not None:
        out.append({
            "start": int(start), "trough": int(trough), "recovery": None,
            "depth": float(trough_val),
        })
    return out


def build(call) -> dict:
    cal = _caliber(call)
    cost_tiers = _cost_tiers(cal)
    slippage = _enum_field(cal, "slippage", _SLIPPAGES)
    slippage_bp = _slippage_bp(cal)
    fee_model = _enum_field(cal, "fee_model", _FEE_MODELS)
    _float_field(cal, "max_leverage", ge=1.0)
    _float_field(cal, "initial_margin", gt=0.0, le=1.0)
    _float_field(cal, "maintenance_margin", gt=0.0)
    _float_field(cal, "liquidation_buffer", ge=0.0)

    detail = str(call.params["detail"])  # 必填枚举，已由 validate_params 校验
    include_funding = bool(call.params.get("include_funding", False))
    position, price, funding, tiers, times = _inputs(call)
    t_len, n_sym = position.shape
    # 向量化版**不做保证金与强平模拟**：`initial_margin` / `maintenance_margin` /
    # `liquidation_buffer` 三个口径字段在这里只校验、不参与计算。若照旧放行
    # 超 1 倍的总敞口，得到的是「加了杠杆却永不爆仓」的净表现——数字看着正常，
    # 结论完全不可信。要求杠杆就换 ml4t 生产实现（`backtest_engine`）。
    gross_now = float(np.abs(position).sum(axis=1).max()) if t_len else 0.0
    if gross_now > 1.0 + _EPS:
        raise FlowError(
            E1004,
            f"ops/backtest 是向量化参照实现，不支持总敞口 {gross_now:.3f} > 1"
            "（无保证金/强平模拟）",
            path="backtest.inputs.position",
            hint="要用杠杆就换 ml4t 实现（backtest_engine），它按 initial_margin 模拟强平",
        )

    # ---- 一次计算：持仓兑现（NEXT_BAR）、换手、逐格成本、资金费 ------------
    prev_w = np.vstack([np.zeros((1, n_sym), dtype="float64"), position[:-1]])
    delta = position - prev_w
    turnover = 0.5 * np.abs(delta).sum(axis=1)
    abs_delta = np.abs(delta)

    fee_rate, slip_rate = _rate_grids(tiers, cost_tiers, slippage, slippage_bp)
    # 每一格的换手贡献是 0.5*|Δw_j|（turnover = Σ_j 0.5*|Δw_j|），
    # 所以逐格成本 = 0.5*|Δw_j| × 该格费率，等价于 turnover × 加权费率（§5.3）。
    turnover_cell = 0.5 * abs_delta
    fee_cell = fee_rate * turnover_cell
    slip_cell = slip_rate * turnover_cell
    fee_bar = fee_cell.sum(axis=1)
    slip_bar = slip_cell.sum(axis=1)

    use_funding = (
        funding is not None and include_funding and fee_model == "taker_with_funding"
    )
    if use_funding:
        fund_cell = prev_w * np.asarray(funding, dtype="float64")
        fund_bar = fund_cell.sum(axis=1)
        funding_total = float(fund_bar.sum())
    else:
        fund_cell = np.zeros((t_len, n_sym), dtype="float64")
        fund_bar = np.zeros(t_len, dtype="float64")
        funding_total = 0.0

    ret = _returns(price)
    gross_bar = (prev_w * ret).sum(axis=1)
    net_return = gross_bar - fee_bar - slip_bar - fund_bar
    net_return[0] = 0.0  # t=0 无上一根持仓，恒为 0
    if not np.isfinite(net_return).all():
        raise FlowError(E3003, "backtest 净收益出现非有限值", path="backtest.outputs.net_return")

    fee_total = float(fee_bar.sum())
    slip_total = float(slip_bar.sum())
    cost_total = fee_total + slip_total + funding_total

    # ---- 年化：只用每期真实净收益；t=0 是结构性 0，不计入样本 --------------
    bpy, bpy_source = bars_per_year(times, t_len)
    periods = net_return[1:]
    degenerate = periods.size < 2
    if degenerate:
        sharpe = 0.0
    else:
        std = float(np.std(periods))
        if not math.isfinite(std) or std <= _EPS:
            sharpe = 0.0
            degenerate = True
        else:
            sharpe = float(np.mean(periods)) / std * math.sqrt(bpy)

    net_return_ref = array_digest(np.ascontiguousarray(net_return))
    net_series = np.ascontiguousarray(net_return, dtype="float64")
    net_series.setflags(write=False)
    turnover_series = np.ascontiguousarray(turnover, dtype="float64")
    turnover_series.setflags(write=False)

    cost_breakdown: dict[str, Any] = {
        "fee": float(fee_total),
        "slippage": float(slip_total),
        "funding": float(funding_total),
        "total": float(cost_total),
    }
    if degenerate:
        # std=0 或样本不足时 sharpe 取 0（不得返回 NaN），并在成本档标注原因。
        cost_breakdown["sharpe_degenerate"] = True

    # summary 的六个字段：两档共用同一批对象，full 不再重算。
    # `net_return` 是**内存通道**（与 `evaluate.values` 同性质）：落盘身份仍是
    # `net_return_ref`。上游（认证）要逐折复算净表现，只拿一个 digest 无法算
    # Sharpe 与回撤；让它自己再跑一遍回测就是第二套成交逻辑。
    summary: dict[str, Any] = {
        "net_return_ref": net_return_ref,
        "net_return": net_series,
        "cost_breakdown": cost_breakdown,
        "turnover": turnover_series,
        "funding_total": float(funding_total),
        "sharpe": float(sharpe),
        "annualisation_used": "calendar_365",
        "bars_per_year": float(bpy),
        "bars_per_year_source": bpy_source,
        # 与 ml4t 生产实现同名字段：向量化版把缺失价当 0 收益（不做成交模拟），
        # 但「缺了多少」必须能看出来，否则「净值偏低」无从归因。
        "missing_price_cells": int(
            (~(np.isfinite(price) & (price > 0.0))).sum()),
    }
    if detail == "summary":
        return summary

    # ---- full：同一份 ledger 的额外聚合 ----------------------------------
    fill_qty = delta
    fill_px = np.where(np.isfinite(price) & (price > 0), price, 0.0)
    ledger = np.stack([
        np.broadcast_to(np.arange(t_len)[:, None], (t_len, n_sym)).astype("float64"),
        np.broadcast_to(np.arange(n_sym)[None, :], (t_len, n_sym)).astype("float64"),
        position, prev_w, fill_qty, fill_px, fee_cell, slip_cell, fund_cell,
    ], axis=-1)
    equity = np.cumprod(1.0 + net_return)

    contrib = prev_w * ret
    long_contrib = float(np.where(prev_w > 0, contrib, 0.0).sum())
    short_contrib = float(np.where(prev_w < 0, contrib, 0.0).sum())

    full = dict(summary)
    full.update({
        "ledger_ref": array_digest(np.ascontiguousarray(ledger)),
        "fills_ref": array_digest(np.ascontiguousarray(fill_qty)),
        "equity_ref": array_digest(np.ascontiguousarray(equity)),
        "long_short_split": {"long": long_contrib, "short": short_contrib},
        "drawdown_episodes": _drawdown_episodes(equity),
        "attribution": {
            "fee": float(fee_total),
            "slippage": float(slip_total),
            "funding": float(funding_total),
            "total": float(cost_total),
        },
    })
    return full
