"""flow holdout —— 揭示保留区（writes_ledger，必须 --yes）。

保留区（holdout）的全部意义是「只看一次」。所以这条命令的唯一职责是**记录
这次揭示**，而不是改数据：

- 区间来自计划 `boundary.time_axis.holdout`，不是命令行参数 —— 保留区是研究
  设计的一部分，能临时指定就失去意义；
- 揭示写入 `db/events.jsonl`（一次性键 `Reveal:holdout:<plan_digest>:<span>`）；
- 第二次执行不报错，但 `first_time=false`。这是给「我是不是已经看过了」一个
  可查的答案，而不是靠人记。

`holdout=reserved` 时拒绝：保留区还没划出来就谈不上揭示。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.contract import require_confirmation
from contracts.errors import E1004, E1008, FlowError
from orchestration.events import EventLedger
from runtime.compiler import compile_plan
from runtime.store import resolve_data_root
from storage.layout import Layout
from storage.ledger import Ledger

from .common import envelope_ok, load_plan, parse_overrides


def _span(text: str) -> tuple[str, str]:
    if not isinstance(text, str) or ".." not in text:
        raise FlowError(
            E1008, f"holdout 必须是 'YYYY-MM-DD..YYYY-MM-DD'，得到 {text!r}",
            path="boundary.time_axis.holdout",
        )
    left, right = (p.strip() for p in text.split("..", 1))
    for part in (left, right):
        try:
            date.fromisoformat(part)
        except ValueError as exc:
            raise FlowError(E1008, f"holdout 日期不合法: {part!r}",
                            path="boundary.time_axis.holdout") from exc
    if left > right:
        raise FlowError(E1008, f"holdout 起点晚于终点: {text!r}",
                        path="boundary.time_axis.holdout")
    return left, right


def cmd_holdout(args: Any) -> dict[str, Any]:
    require_confirmation("holdout", args)
    plan = load_plan(args)
    axis = ((plan.get("boundary") or {}).get("time_axis") or {})
    raw = axis.get("holdout")
    if raw is None or str(raw) == "reserved":
        raise FlowError(
            E1004, "计划的 holdout 是 reserved，没有可揭示的区间",
            path="boundary.time_axis.holdout",
            hint="先把保留区写成 'YYYY-MM-DD..YYYY-MM-DD' 再揭示",
        )
    start, end = _span(str(raw))

    compiled = compile_plan(
        baseline=None, plan=plan, overrides=parse_overrides(args.set) or None,
        aliases=None, require_supply=False, strict=not args.no_strict,
        allow_constitutive_override=args.allow_constitutive_override,
    )
    digest = str(compiled.fingerprints["plan_digest"])
    scope = f"holdout:{digest.split(':', 1)[-1][:16]}"
    span = f"{start}..{end}"

    layout = Layout(resolve_data_root())
    events = EventLedger(Ledger(layout))
    existed = events.get("Reveal", f"{scope}:{span}") is not None
    record = events.reveal(scope, span)

    return envelope_ok("holdout", scope, {
        "holdout": {
            "plan": str(plan.get("plan") or ""),
            "plan_digest": digest,
            "span": span,
            "start": start,
            "end": end,
            "first_time": not existed,
            "revealed_value": record.value,
            "ledger": str(layout.register("events")),
        },
    })
