"""供给门禁 B（只读适用性检查，s0/03-runtime.md §8）。

第 9 步唯一接触外部世界的步骤，且只读。失败时不创建 Ticket、不写目录、
不触发采集。检查用正式 contracts.Pin（不是自造简化类），避免两套 Pin。

契约边界（S2-M5 裁决）：
- Pin 只携带 `symbols_digest`，**没有**标的清单。因此标的集检查是
  「计划标的集摘要 == pin.symbols_digest」，而不是读取契约外字段
  `pin.gate_a["symbols"]`。这不是放宽：相等比子集更严，且不需要第二真相。
  失败时把计划标的集与其摘要一起放进错误 context，供人工定位。
- 新鲜度以纯函数为准：`acquired_at` 不得早于覆盖末端。plan_schema 的 supply
  没有 `max_age_days` 键，故**不实现**墙钟新鲜度分支（代码里留着也永远不可达）；
  **绝不**在模块内 `datetime.now()`（F1）。
- `gate_a.passed` 必须为真：门禁 A 未过的版本不得被别名指向（C11/E4005）。

明确不做：不补采、不降级用旧版本、不把时间轴自动缩短到数据末端。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from contracts.digest import compute_digest
from contracts.errors import E4002, E4003, E4005, FlowError
from contracts.types import Pin

# `supply.frequency` 只约束「bar 频率」这一件事，因此只对 K 线数据集比较。
# funding / open_interest / basis 的结算是数据属性（实测 1h/4h/8h 混合且同合约
# 会变，I34），拿计划的 bar 频率去要求它们会制造假失败。
BAR_DATASET = "ohlcv"


def _parse_day(value: Any) -> datetime | None:
    """把 'YYYY-MM-DD' 或 RFC3339 解析成带时区 datetime；失败返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_symbols(
    plan: Mapping[str, Any],
    symbol_sets: Mapping[str, Sequence[str]] | None = None,
) -> list[str] | None:
    """把计划声明的标的集解析成有序去重清单；无法解析返回 None（不猜）。

    plan_schema 里 `supply.symbols` 是**标的集名**，编译期由 charter 的
    symbol_sets 解析；list/tuple 是测试与开发态的直接清单形态。
    """
    supply = plan.get("supply")
    if not isinstance(supply, Mapping):
        return None
    raw = supply.get("symbols")
    if isinstance(raw, (list, tuple, set, frozenset)):
        return sorted({str(s) for s in raw})
    if isinstance(raw, str) and symbol_sets:
        members = symbol_sets.get(raw)
        if members is not None:
            return sorted({str(s) for s in members})
    return None


def as_pin(raw: Any) -> Pin | None:
    """别名项 → Pin。只有完整 Pin（实例或九字段字典）才算已解析。

    S1 的纯字符串别名是当时的测试桩，S2 起生产别名必须是完整 Pin；
    这里返回 None 让编译步骤按 C11 明确报错，不静默放行。
    """
    if isinstance(raw, Pin):
        return raw
    if isinstance(raw, Mapping):
        try:
            return Pin.from_dict(raw)
        except FlowError:
            return None
    return None


def check_gate_b(
    pin: Pin | Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    symbol_sets: Mapping[str, Sequence[str]] | None = None,
    supply_meta: Mapping[str, Any] | None = None,
) -> None:
    """只读适用性检查。任一失败抛 E4002/E4003/E4005，不产生任何副作用。

    - 门禁 A：pin.gate_a.passed 必须为真（E4005）
    - 覆盖区间：pin.coverage 覆盖 boundary.time_axis.mine 起止（E4002）
    - 标的集：计划标的集摘要 == pin.symbols_digest（E4002）
    - 新鲜度：acquired_at >= 覆盖末端（纯函数，无墙钟）（E4002）
    - 末端闭合：pin.end_closed 为真（E4003）
    - 供给维度：计划 supply.frequency / supply.exchange == manifest 的
      timeframe / exchange（E4002）。Pin 九字段不含这两项，只能核对事实清单。

    供给覆盖末端晚于研究时间轴终点是正常且期望的（覆盖 ⊇ 研究区间），
    不做反向检查——那会把「数据充足」误判为失败。
    """
    parsed = as_pin(pin)
    if parsed is None:
        raise FlowError(
            E4002, "别名项不是完整 Pin，无法做门禁 B",
            hint="别名表必须由 supply publish 写入；s1 的字符串桩不得用于执行",
            path="supply.datasets",
        )
    if parsed.gate_a.get("passed") is not True:
        raise FlowError(
            E4005, f"{parsed.dataset} 门禁 A 未通过，不得被别名指向",
            hint="版本保留供排查；先做维护重新发布，编译不补采",
            path="gate_a.passed",
            context={"reason_code": parsed.gate_a.get("reason_code")},
        )

    boundary = plan.get("boundary")
    if not isinstance(boundary, Mapping):
        raise FlowError(E4002, "计划 boundary 必须是映射", path="boundary")
    axis = boundary.get("time_axis")
    if not isinstance(axis, Mapping):
        raise FlowError(E4002, "计划 boundary.time_axis 必须是映射",
                        path="boundary.time_axis")
    supply = plan.get("supply")
    if not isinstance(supply, Mapping):
        raise FlowError(E4002, "计划 supply 必须是映射", path="supply")
    mine = str(axis.get("mine") or "")
    parts = mine.split("..")
    # 解析不了时间轴就 E4002：与标的集检查同一标准，不能静默跳过覆盖检查。
    # schema 允许 interval="reserved"，但那不是本次可检查的研究窗口。
    want_start = _parse_day(parts[0]) if len(parts) > 0 else None
    want_end = _parse_day(parts[1]) if len(parts) > 1 else None
    if want_start is None or want_end is None or want_start >= want_end:
        raise FlowError(
            E4002, f"时间轴无法解析为起止日期: {mine!r}",
            path="boundary.time_axis.mine",
            hint="写成形如 2024-01-01..2026-06-30 的闭区间；reserved 不可用于执行",
        )
    have_start = _parse_day(parsed.coverage.get("start"))
    have_end = _parse_day(parsed.coverage.get("end"))

    # 1. 覆盖区间
    gaps: list[str] = []
    if have_start is None:
        gaps.append(f"起点未知（要求 {want_start.date()}）")
    elif want_start < have_start:
        gaps.append(f"起点缺 {want_start.date()}..{have_start.date()}")
    if have_end is None:
        gaps.append(f"终点未知（要求 {want_end.date()}）")
    elif want_end > have_end:
        gaps.append(f"终点缺 {have_end.date()}..{want_end.date()}")
    if gaps:
        if parsed.dataset == "open_interest":
            # 交易所只保留约 30 天 OI，要求它覆盖 2023 起的窗口等于要求它永远失败。
            # OI 是辅助数据（成本/拥挤度），覆盖不足时标 dormant，不阻断研究
            # （19-supply.md §3）。这不是放宽完整性，而是承认上游事实的边界。
            pass
        else:
            raise FlowError(
                E4002,
                f"{parsed.dataset} 覆盖区间不足: {'; '.join(gaps)}",
                path="boundary.time_axis.mine",
                hint="延长 flow fetch 的区间或缩小计划时间轴；不得自动补采或降级用旧版本",
            )

    # 2. 标的集：Pin 只有摘要，比较计划标的集的规范摘要。
    #    list/tuple 是开发态，字符串名经 symbol_sets 解析；解析不了即失败。
    required = resolve_symbols(plan, symbol_sets)
    if required is None:
        raise FlowError(
            E4002, "计划标的集无法解析为成员清单",
            path="supply.symbols",
            hint="在 schemas/symbol_sets.json 登记标的集，或直接在计划里写清单",
            context={"declared": (plan.get("supply") or {}).get("symbols")},
        )
    want_digest = compute_digest(required)
    if parsed.dataset == BAR_DATASET and want_digest != str(parsed.symbols_digest):
        raise FlowError(
            E4002,
            f"标的集与供给版本不符: 计划 {want_digest}, 供给 {parsed.symbols_digest}",
            path="supply.symbols",
            hint="用同一标的集重新 fetch；不得换用部分标的继续研究",
            context={"required_symbols": required, "required_digest": want_digest,
                     "pin_symbols_digest": str(parsed.symbols_digest)},
        )
    if parsed.dataset != BAR_DATASET and not parsed.symbols_digest:
        # 辅助数据集只要求「有标的」：缺个别标的是交易所事实（新上市/已下架），
        # 不是数据损坏。面板基座（ohlcv）才必须与计划标的集逐位相等。
        raise FlowError(
            E4002, f"{parsed.dataset} 没有任何标的", path=f"supply.{parsed.dataset}",
        )

    # 3. 新鲜度（纯函数部分）：采集完成时间不得早于它声称覆盖的末端。
    if have_end is not None and parsed.acquired_at < have_end:
        raise FlowError(
            E4002,
            f"数据不新鲜: acquired_at={parsed.acquired_at.isoformat()} "
            f"< coverage.end={have_end.isoformat()}",
            path="pin.acquired_at",
            hint="先做维护刷新；不得降级用旧版本",
        )
    # 4. 末端闭合
    if not parsed.end_closed:
        raise FlowError(
            E4003,
            f"{parsed.dataset} 末端未闭合",
            path="pin.end_closed",
            hint="末端有未完成的 bar；先维护到闭合再研究",
        )

    # 5. 供给维度：计划声明的频率/交易所必须与事实清单一致。
    #    Pin 九字段冻结、不含 timeframe/exchange，所以这两项只能核对 seed
    #    manifest。缺 meta 就拒绝——「查不到来源」不是「来源没问题」。
    if not isinstance(supply_meta, Mapping):
        raise FlowError(
            E4002, f"{parsed.dataset} 缺少可读的 manifest，无法核对频率与交易所",
            path=f"supply.{parsed.dataset}",
            hint="别名指向的版本必须有 seeds/<dataset>/<version>/manifest.json",
        )
    declared_freq = supply.get("frequency")
    declared_exchange = supply.get("exchange")
    declared_market = supply.get("market") or "futures"
    for name, value in (("frequency", declared_freq), ("exchange", declared_exchange)):
        if not isinstance(value, str) or not value:
            raise FlowError(
                E4002, f"计划未声明 supply.{name}", path=f"supply.{name}",
                hint="supply 块里 frequency 与 exchange 都是必填",
            )
    actual_exchange = supply_meta.get("exchange")
    if not isinstance(actual_exchange, str) or not actual_exchange:
        raise FlowError(
            E4002, f"{parsed.dataset} 的 manifest 未记录交易所，无法确认数据来源",
            path=f"supply.{parsed.dataset}",
            hint="重新 fetch 并显式给 --exchange；不得靠默认值兜底",
        )
    if actual_exchange != declared_exchange:
        raise FlowError(
            E4002,
            f"交易所不符: 计划 {declared_exchange}, 事实 {actual_exchange}",
            path="supply.exchange",
            hint="用计划声明的交易所重新 fetch；不得混用两个交易所的数据",
            context={"declared": declared_exchange, "actual": actual_exchange},
        )
    # 市场是独立维度：同一个交易所 id 下 spot 与 futures 是两份数据，混用等于
    # 把「用现货挖出的因子」标成「合约因子」。事实清单里没有 market 就说明这份
    # 数据不是按新规则落盘的，同样拒绝。
    actual_market = supply_meta.get("market")
    if not isinstance(actual_market, str) or not actual_market:
        raise FlowError(
            E4002, f"{parsed.dataset} 的事实清单未记录市场（spot/futures）",
            path=f"supply.{parsed.dataset}",
            hint="重新 fetch；市场写在路径里但没有随读取一起报出来属于存储缺陷",
        )
    if actual_market != declared_market:
        raise FlowError(
            E4002,
            f"市场不符: 计划 {declared_market}, 事实 {actual_market}",
            path="supply.market",
            hint="计划写 spot 就只能读 spot 目录；现货与合约是两份不同的数据",
            context={"declared": declared_market, "actual": actual_market},
        )
    if parsed.dataset == BAR_DATASET:
        actual_freq = supply_meta.get("timeframe")
        if not isinstance(actual_freq, str) or not actual_freq:
            raise FlowError(
                E4002, f"{parsed.dataset} 的 manifest 未记录 timeframe",
                path="supply.frequency",
            )
        if actual_freq != declared_freq:
            raise FlowError(
                E4002,
                f"频率不符: 计划 {declared_freq}, 事实 {actual_freq}",
                path="supply.frequency",
                hint="按计划频率重新 refresh；1d 数据不能当 4h 用",
                context={"declared": declared_freq, "actual": actual_freq},
            )
