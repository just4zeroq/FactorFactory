"""flow estimate —— 成本估算（只读，不碰数据）。

回答一个问题：**这套口径下，策略每期要赚多少才够付成本。**

口径全部来自计划的 `caliber`（P1：CLI 不得出现口径开关）。`--turnover` 不是
口径，它是**问题参数**：同一个口径在不同换手下成本不同，这正是要算的东西。

算式与 `backtest_engine._rate_grids` 逐项对齐，两处不一致会让「估算说 12bp、
实盘扣 18bp」这种分歧无处可查：

- `fee = tier / 10000`（tier 是 bp，单边）
- `slippage = none → 0` / `fixed → tier[0] / 20000` / `tiered → tier / 20000`
- 每期成本 = `turnover × (fee + slippage)`，turnover 按「单边换手」计
- 每年期数 = `365 / rebalance`（`annualisation=calendar_365`；加密 7×24）
"""

from __future__ import annotations

from typing import Any

from v5.contracts.errors import E1008, FlowError
from v5.capability.timeaxis import CALENDAR_DAYS_PER_YEAR

from .common import envelope_ok, load_plan

_ANNUAL_DAYS = float(CALENDAR_DAYS_PER_YEAR)


def _net_block(plan: dict[str, Any]) -> dict[str, Any]:
    caliber = plan.get("caliber") or {}
    net = caliber.get("net") or {}
    if not net:
        raise FlowError(
            E1008, "计划缺 caliber.net，无法估算成本", path="caliber.net",
            hint="成本档与滑点必须写进口径块",
        )
    return net


def cmd_estimate(args: Any) -> dict[str, Any]:
    plan = load_plan(args)
    net = _net_block(plan)
    execution = ((plan.get("caliber") or {}).get("execution") or {})

    tiers = net.get("cost_tiers")
    if not isinstance(tiers, list) or not tiers:
        raise FlowError(E1008, "caliber.net.cost_tiers 必须是非空列表",
                        path="caliber.net.cost_tiers")
    slip_mode = str(net.get("slippage") or "none")
    if slip_mode not in ("none", "fixed", "tiered"):
        raise FlowError(E1008, f"未知 slippage {slip_mode!r}", path="caliber.net.slippage")
    rebalance = int(execution.get("rebalance") or 0)
    if rebalance < 1:
        raise FlowError(E1008, "caliber.execution.rebalance 必须是正整数",
                        path="caliber.execution.rebalance")

    turnover = float(getattr(args, "turnover", 1.0))
    if not (0.0 <= turnover <= 10.0):
        raise FlowError(E1008, f"--turnover 必须在 [0,10]，得到 {turnover}",
                        path="estimate.turnover")

    periods = _ANNUAL_DAYS / float(rebalance)
    rows = []
    for i, tier in enumerate(tiers):
        bp = float(tier)
        fee = bp / 10000.0
        if slip_mode == "none":
            slip = 0.0
        elif slip_mode == "fixed":
            slip = float(tiers[0]) / 20000.0
        else:
            slip = bp / 20000.0
        per_period = turnover * (fee + slip)
        rows.append({
            "tier_bp": bp,
            "fee_per_period": round(turnover * fee, 8),
            "slippage_per_period": round(turnover * slip, 8),
            "cost_per_period": round(per_period, 8),
            "cost_annual": round(per_period * periods, 6),
            # 毛收益要覆盖这一行成本才不亏；用年化表达便于与 SR 对照
            "breakeven_annual_return": round(per_period * periods, 6),
        })

    return envelope_ok("estimate", None, {
        "estimate": {
            "plan": str(plan.get("plan") or ""),
            "annualisation": str(execution.get("annualisation") or ""),
            "rebalance_bars": rebalance,
            "periods_per_year": round(periods, 3),
            "turnover_per_period": turnover,
            "slippage": slip_mode,
            "fee_model": str(net.get("fee_model") or ""),
            "tiers": rows,
            "note": ("成本按单边换手计；tiered 滑点取该档位的一半，与 backtest "
                     "的 _rate_grids 同口径。资金费不在本估算内（按事件逐 bar 计）。"),
        },
    })
