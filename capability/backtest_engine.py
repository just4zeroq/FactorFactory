"""V5 `backtest` 生产实现：ml4t 目标权重引擎 + V5 成本口径。

边界（docs/07-components.md §3、s0/04 §3.11）：

* **成交、持仓、权益曲线来自 ml4t-backtest**。V5 不重写撮合循环；策略只把
  `allocate` 已经算好的 `(T,N)` 目标权重喂给 `TargetWeightExecutor`，由引擎按
  `ExecutionMode.NEXT_BAR` 在下一根 bar 成交。
* **成本口径仍只有 V5 的 `caliber.net`**。ml4t 的 commission/slippage 置零，
  逐笔成交取回后按 `tiers` 对应的 `cost_tiers` 计算 V5 的
  `turnover × (fee + slippage)`，资金费按 `prev_w × funding` 逐 bar 累计。
  引擎负责「能不能成交、何时成交」，V5 负责「这笔成交按什么口径计费」。
* **两档同源**：先构造一份内部结果（净收益、逐格成本、权益、fills），再由
  `detail` 投影出 `summary` / `full`。`full` 不是第二次回测。

`ops/backtest.py` 的向量化实现保留作纯数值参照与单元测试夹具，不再注册为生产
实现；生产唯一实现是本模块的 `build`（注册名 `ledger@1`）。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd
import polars as pl
from ml4t.backtest import BacktestConfig, DataFeed, Engine, Strategy
from ml4t.backtest.config import (
    CommissionType,
    ExecutionMode,
    ExecutionPrice,
    SlippageType,
)
from ml4t.backtest.execution.rebalancer import RebalanceConfig, TargetWeightExecutor

from capability.panel import array_digest
from contracts.errors import E3003, FlowError

from .ops.backtest import (
    _caliber,
    _cost_tiers,
    _drawdown_episodes,
    _enum_field,
    _float_field,
    _inputs,
    bars_per_year,
)
from capability.params import implementation_defaults

# G3：初始资金与合成轴基准时间进 registry/params.json 的 defaults。
# 这两个值不是策略口径：initial_cash 只是把 qty*px 折成权重的计量单位，且
# 必须和 BacktestConfig 用同一个数；base_ts 只在调用方没给 times 时造轴。
_DEFAULT_BACKTEST = implementation_defaults("backtest")
_INITIAL_CASH = float(_DEFAULT_BACKTEST["initial_cash"])
_BASE_TS = pd.Timestamp(str(_DEFAULT_BACKTEST["base_ts"]), tz="UTC")
_DAY = pd.Timedelta(days=1)
_EPS = 1e-12


class _TargetWeightStrategy(Strategy):
    """把预计算的目标权重按 bar 顺序交给 ml4t 的 rebalancer。

    权重行 `t` 在第 `t` 根市场事件上提交，`ExecutionMode.NEXT_BAR` 保证它在
    `t+1` 成交。这里不做任何排序或方向判断——方向已经由证据/裁决/组合决定。
    """

    def __init__(
        self,
        weights: np.ndarray,
        symbols: tuple[str, ...],
        *,
        allow_short: bool,
        max_gross_leverage: float,
    ) -> None:
        self.weights = weights
        self.symbols = symbols
        self.i = 0
        self.executor = TargetWeightExecutor(RebalanceConfig(
            allow_short=allow_short,
            allow_fractional=True,
            max_single_weight=1.0,
            max_gross_leverage=max_gross_leverage,
            cancel_before_rebalance=True,
        ))

    def on_data(self, timestamp, data, context, broker) -> None:  # noqa: D401
        if self.i >= len(self.weights):
            return
        target = {symbol: float(self.weights[self.i, j])
                  for j, symbol in enumerate(self.symbols)}
        # 数据里出现但目标未列出的标的按 0 处理：必须显式平仓，不能静默留仓。
        for symbol in data:
            target.setdefault(symbol, 0.0)
        self.executor.execute(target, data, broker, timestamp=timestamp)
        self.i += 1


def _axis(times: np.ndarray | None, t_len: int) -> np.ndarray:
    """回测时间轴（UTC 纳秒）。

    有 `time` 输入就用真实时间戳——ml4t 按它排序成交，`NEXT_BAR` 的「下一根」
    也由它定义。缺 `time` 时造一条等距日频轴，只为兼容日频调用方；非日频供给
    必须传 `time`，否则成交时间与实际数据无关（年化另见 `bars_per_year`）。
    """
    if times is not None:
        return np.asarray(times, dtype="int64")
    return np.array([(_BASE_TS + i * _DAY).value for i in range(t_len)],
                    dtype="int64")


def _timestamps(axis: np.ndarray) -> list[pd.Timestamp]:
    return list(pd.to_datetime(axis, unit="ns", utc=True))


def _index_of(ts: Any, axis: np.ndarray) -> int:
    """ml4t 成交时间 → bar 下标；按最近邻匹配，容差半步长。

    不用 `(stamp - base) // step`：真实时间轴可能非等距（缺口、换月），除法
    会在缺口后整体错位。也不用精确相等：库内部可能把时间戳截断到秒级精度。
    """
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    ns = int(stamp.value)
    if axis.size == 0:
        raise FlowError(E3003, "ml4t 返回成交但回测窗口为空",
                        path="backtest.engine.fills")
    tol = int(np.min(np.diff(axis))) // 2 if axis.size > 1 else 0
    right = int(np.searchsorted(axis, ns, side="left"))
    lo = max(right - 1, 0)
    hi = min(right, axis.size - 1)
    pick = lo if abs(int(axis[lo]) - ns) <= abs(int(axis[hi]) - ns) else hi
    if abs(int(axis[pick]) - ns) > tol:
        raise FlowError(E3003, "ml4t 返回的成交时间不在本次回测窗口",
                        path="backtest.engine.fills")
    return pick


def _symbol_names(n_sym: int) -> tuple[str, ...]:
    """B4：合成标的名的**唯一**入口。

    backtest 的输入契约只有 `position` / `price` 数值矩阵，没有标的名；ml4t
    只在中间 fills dataframe 里用 asset 做字典键，回填时又按 `by_symbol` 映射
    回列下标，所以名字文本不进任何落盘字节。这里集中一处是为了让「将来从
    Panel 取真实标的名」只改一个函数，而不是散在价格帧与资金费帧两处。
    """
    return tuple(f"A{j}" for j in range(n_sym))


def _price_frame(price: np.ndarray, timestamps: list[pd.Timestamp],
                 symbols: tuple[str, ...]) -> tuple[pl.DataFrame, int]:
    """行情帧 + 缺失格数。

    open/high/low 取 close、volume 取常数是**有意的**：生产撮合只用
    `ExecutionPrice.CLOSE`，这三列不参与任何成交判断，也不进落盘对象
    （`fills_ref` 是 `(T,N)` 数值数组的摘要，不含列/资产名）。若将来支持
    intrabar 撮合，必须扩展 backtest 的 inputs 契约传真实 OHLCV，而不是在
    这里偷偷合成一个看起来像真实行情的帧。

    **不做「全量有限正数」的硬校验**：那会让一个未上市/停牌/退市的标的把整条
    回测判死（当前 20 永续全量完整所以没暴露，扩标的必炸）。缺失格的语义是
    「这个标的这根 bar 没有价格」——不建价、不成交、持仓沿用，与真实停牌一致。

    两个仍然 fail-closed 的边界：
    1. 某标的**整列**没有有效价：它在回测区间内从未存在过，这不是缺失而是配置
       错误（标的集/区间写错），权重永远无法执行；
    2. 某根 bar **全标的**都没价：时间轴断了，ml4t 会把两侧拼成相邻 bar，
       净值曲线跨过一段不存在的行情却看不出来。
    """
    t_len, n_sym = price.shape
    finite = np.isfinite(price) & (price > 0.0)
    dead = np.flatnonzero(~finite.any(axis=0))
    if dead.size:
        names = ", ".join(symbols[j] for j in dead[:5])
        raise FlowError(
            E3003,
            f"{dead.size} 个标的在回测区间内没有任何有效价格（{names}）",
            path="backtest.inputs.price",
            hint="整列无价是标的集/区间写错，不是行情缺失；换标的集或缩短区间",
        )
    empty = np.flatnonzero(~finite.any(axis=1))
    if empty.size:
        raise FlowError(
            E3003,
            f"{empty.size} 根 bar 上全部标的都没有价格（首根 index={int(empty[0])}）",
            path="backtest.inputs.price",
            hint="时间轴断裂会让净值曲线跨过一段不存在的行情；这是数据缺口，不是停牌",
        )
    rows = []
    for t, stamp in enumerate(timestamps):
        for j, symbol in enumerate(symbols):
            if not finite[t, j]:
                continue
            px = float(price[t, j])
            rows.append({
                "timestamp": stamp, "asset": symbol, "close": px,
                "open": px, "high": px, "low": px, "volume": 1000.0,
            })
    return pl.from_pandas(pd.DataFrame(rows)), int((~finite).sum())


def _funding_frame(funding: np.ndarray | None, timestamps: list[pd.Timestamp],
                   symbols: tuple[str, ...]) -> pl.DataFrame | None:
    if funding is None:
        return None
    rows = []
    for t, stamp in enumerate(timestamps):
        for j, symbol in enumerate(symbols):
            rows.append({"timestamp": stamp, "asset": symbol,
                         "rate": float(funding[t, j])})
    return pl.from_pandas(pd.DataFrame(rows))


def _slippage_bp(cal: Mapping[str, Any]) -> float | None:
    """单边滑点（bp）的显式覆盖；未声明返回 None（沿用旧派生规则）。

    旧口径把滑点绑死在手续费上（`fixed` = 最低档一半，`tiered` = 逐档一半）。
    那是假设而非事实：真实滑点取决于下单方式与盘口深度，与 taker 费率没有
    因果关系。没有出口字段，用户就写不出真值，净收益被隐式常数决定。
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
    if not (out >= 0.0):
        raise FlowError(
            E1004, f"caliber.net.slippage_bp={out} 必须是非负数",
            path="policy_view.net.slippage_bp",
        )
    return out


def _rate_grids(tiers: np.ndarray, cost_tiers: list[int],
                slippage: str,
                slippage_bp: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """逐格费率 / 滑点率。优先级：`none` > 显式 `slippage_bp` > 旧派生规则。"""
    mapped = np.where(tiers >= 0, np.minimum(tiers, len(cost_tiers) - 1), 0)
    fee = np.where(tiers >= 0,
                   np.asarray(cost_tiers, dtype="float64")[mapped] / 10000.0, 0.0)
    if slippage == "none":
        slip = np.zeros(tiers.shape, dtype="float64")
    elif slippage_bp is not None:
        slip = np.where(tiers >= 0, float(slippage_bp) / 10000.0, 0.0)
    elif slippage == "tiered":
        slip = np.where(tiers >= 0,
                        np.asarray(cost_tiers, dtype="float64")[mapped] / 20000.0, 0.0)
    else:  # fixed
        slip = np.where(tiers >= 0, float(cost_tiers[0]) / 20000.0, 0.0)
    return fee, slip


def _fill_weights(result: Any, axis: np.ndarray, n_sym: int,
                  symbols: tuple[str, ...]) -> np.ndarray:
    """ml4t fills → `(T,N)` 带符号成交权重（以初始权益折算）。"""
    out = np.zeros((axis.size, n_sym), dtype="float64")
    fills = result.to_fills_dataframe()
    if fills.height == 0:
        return out
    by_symbol = {s: j for j, s in enumerate(symbols)}
    frame = fills.to_pandas()
    for ts, symbol, qty, px in zip(
            frame["timestamp"], frame["asset"],
            frame["signed_quantity"], frame["price"], strict=False):
        j = by_symbol.get(str(symbol))
        if j is None:
            continue
        out[_index_of(ts, axis), j] += float(qty) * float(px) / _INITIAL_CASH
    return out


def build(call) -> dict:
    cal = _caliber(call)
    cost_tiers = _cost_tiers(cal)
    slippage = _enum_field(cal, "slippage", frozenset({"none", "fixed", "tiered"}))
    slippage_bp = _slippage_bp(cal)
    fee_model = _enum_field(cal, "fee_model",
                            frozenset({"taker", "maker", "taker_with_funding"}))
    max_leverage = _float_field(cal, "max_leverage", ge=1.0)
    initial_margin = _float_field(cal, "initial_margin", gt=0.0, le=1.0)
    maintenance_margin = _float_field(cal, "maintenance_margin", gt=0.0)
    liquidation_buffer = _float_field(cal, "liquidation_buffer", ge=0.0)

    detail = str(call.params["detail"])
    include_funding = bool(call.params.get("include_funding", False))
    position, price, funding, tiers, times = _inputs(call)
    t_len, n_sym = position.shape
    if t_len < 2 or n_sym < 1:
        raise FlowError(E3003, "backtest 至少需要 2 根 bar 与 1 个标的",
                        path="backtest.inputs")
    symbols = _symbol_names(n_sym)
    axis = _axis(times, t_len)
    timestamps = _timestamps(axis)

    # 目标权重已由 allocate 决定；允许做空由权重本身决定，不能另从 config 猜。
    allow_short = bool(np.any(position < -_EPS))
    gross = float(np.abs(position).sum(axis=1).max())
    max_gross = max(float(max_leverage), gross, 1.0)

    config = BacktestConfig(
        initial_cash=_INITIAL_CASH,
        allow_short_selling=True,
        allow_leverage=max_leverage > 1.0 + _EPS,
        initial_margin=initial_margin,
        long_maintenance_margin=maintenance_margin,
        short_maintenance_margin=maintenance_margin,
        execution_mode=ExecutionMode.NEXT_BAR,
        execution_price=ExecutionPrice.CLOSE,
        next_bar_execution_price=ExecutionPrice.CLOSE,
        commission_type=CommissionType.PERCENTAGE,
        commission_rate=0.0,           # V5 口径在 fills 上逐格计算，不让库再计一次
        commission_minimum=0.0,
        slippage_type=SlippageType.PERCENTAGE,
        slippage_rate=0.0,
        partial_fills_allowed=True,
        skip_cash_validation=True,
        calendar="crypto",
    )
    strategy = _TargetWeightStrategy(
        position, symbols, allow_short=allow_short, max_gross_leverage=max_gross,
    )
    price_frame, missing_cells = _price_frame(price, timestamps, symbols)
    feed = DataFeed(prices_df=price_frame)
    result = Engine(
        feed, strategy, config,
        funding_df=_funding_frame(funding, timestamps, symbols),
    ).run()

    # ---- 唯一内部结果：ml4t 成交 + V5 逐格成本 ---------------------------
    fills_w = _fill_weights(result, axis, n_sym, symbols)
    turnover_cell = 0.5 * np.abs(fills_w)
    fee_rate, slip_rate = _rate_grids(tiers, cost_tiers, slippage, slippage_bp)
    fee_cell = turnover_cell * fee_rate
    slip_cell = turnover_cell * slip_rate

    prev_w = np.vstack([np.zeros((1, n_sym), dtype="float64"), position[:-1]])
    use_funding = (funding is not None and include_funding
                   and fee_model == "taker_with_funding")
    if use_funding:
        fund_cell = prev_w * np.asarray(funding, dtype="float64")
    else:
        fund_cell = np.zeros((t_len, n_sym), dtype="float64")

    equity = result.to_equity_dataframe()
    gross_bar = np.asarray(equity["return"].to_numpy(), dtype="float64")
    # 长度必须与面板对得上。**只允许「多」不允许「少」**：ml4t 会多出 t0 那一行
    # （无前序持仓），截断是正确语义；少了则说明成交链漏了 bar，尾部补 0
    # 会得到一条「数值看着正常、只是年月对不上」的净值曲线——最坏的一类错。
    if gross_bar.size < t_len:
        raise FlowError(
            E3003,
            f"ml4t 权益序列只有 {gross_bar.size} 行，少于面板 {t_len} 根 bar",
            path="backtest.outputs.gross_return",
            hint="成交链漏了 bar：尾部补 0 会让净值曲线静默错位，不是可接受的降级",
        )
    gross_bar = gross_bar[:t_len]
    net_return = (gross_bar - fee_cell.sum(axis=1) - slip_cell.sum(axis=1)
                  - fund_cell.sum(axis=1))
    net_return[0] = 0.0
    if not np.isfinite(net_return).all():
        raise FlowError(E3003, "backtest 净收益出现非有限值",
                        path="backtest.outputs.net_return")

    fee_total = float(fee_cell.sum())
    slip_total = float(slip_cell.sum())
    fund_total = float(fund_cell.sum())
    cost_breakdown: dict[str, Any] = {
        "fee": fee_total, "slippage": slip_total,
        "funding": fund_total, "total": fee_total + slip_total + fund_total,
    }

    bpy, bpy_source = bars_per_year(times, t_len)
    periods = net_return[1:]
    std = float(np.std(periods)) if periods.size else 0.0
    degenerate = periods.size < 2 or not math.isfinite(std) or std <= _EPS
    sharpe = 0.0 if degenerate else float(np.mean(periods)) / std * math.sqrt(bpy)
    if degenerate:
        cost_breakdown["sharpe_degenerate"] = True

    turnover_series = 0.5 * np.abs(position - prev_w).sum(axis=1)
    turnover_series = np.ascontiguousarray(turnover_series, dtype="float64")
    turnover_series.setflags(write=False)
    net_series = np.ascontiguousarray(net_return, dtype="float64")
    net_series.setflags(write=False)
    summary: dict[str, Any] = {
        "net_return_ref": array_digest(np.ascontiguousarray(net_return)),
        # 内存通道（与 evaluate.values 同性质）：身份仍是 net_return_ref。
        # 认证要逐折复算净表现，只拿 digest 算不出 Sharpe 与回撤。
        "net_return": net_series,
        "cost_breakdown": cost_breakdown,
        "turnover": turnover_series,
        "funding_total": fund_total,
        "sharpe": float(sharpe),
        "annualisation_used": "calendar_365",
        "bars_per_year": float(bpy),
        "bars_per_year_source": bpy_source,
        # 缺失格数（无价 → 不建价、不成交）。落盘是为了让「这只标的在区间里
        # 有 30% 的 bar 没交易」可见——否则它会表现为「净值偏低」而无从归因。
        "missing_price_cells": int(missing_cells),
    }
    if detail == "summary":
        return summary

    ledger = np.stack([
        np.broadcast_to(np.arange(t_len)[:, None], (t_len, n_sym)).astype("float64"),
        np.broadcast_to(np.arange(n_sym)[None, :], (t_len, n_sym)).astype("float64"),
        position, prev_w + fills_w, fills_w, price, fee_cell, slip_cell, fund_cell,
    ], axis=-1)
    equity_curve = np.cumprod(1.0 + net_return)
    with np.errstate(invalid="ignore", divide="ignore"):
        asset_ret = np.where((price[:-1] > 0) & np.isfinite(price[1:]),
                             price[1:] / np.where(price[:-1] > 0, price[:-1], 1.0) - 1.0,
                             0.0)
    asset_ret = np.vstack([np.zeros((1, n_sym), dtype="float64"), asset_ret])
    contrib = prev_w * asset_ret
    full = dict(summary)
    full.update({
        "ledger_ref": array_digest(np.ascontiguousarray(ledger)),
        "fills_ref": array_digest(np.ascontiguousarray(fills_w)),
        "equity_ref": array_digest(np.ascontiguousarray(equity_curve)),
        "long_short_split": {
            "long": float(np.where(prev_w > 0, contrib, 0.0).sum()),
            "short": float(np.where(prev_w < 0, contrib, 0.0).sum()),
        },
        "drawdown_episodes": _drawdown_episodes(equity_curve),
        "attribution": dict(cost_breakdown),
    })
    return full


__all__ = ["build"]
