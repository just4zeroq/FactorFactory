"""flow explain —— 逐叶子展示编译来源与四锚点指纹（只读）。

`--set` / `--no-strict` / `--allow-constitutive-override` 必须与 `run` 同语义：
同一个方案，`run` 与 `explain` 编译出的指纹必须逐位相同。声明了参数却不生效
会让「explain 告诉我这个值从哪来」变成假话。
"""

from __future__ import annotations

from typing import Any

from runtime.compiler import compile_plan
from runtime.store import resolve_data_root
from storage.layout import Layout

from .common import (
    build_supply_context, envelope_ok, load_json_dict, load_plan, parse_overrides,
    resolve_snapshot_for_plan,
)

from contracts.errors import E1004


def cmd_explain(args: Any) -> dict[str, Any]:
    plan = load_plan(args)
    overrides = parse_overrides(args.set)
    layout = Layout(resolve_data_root())
    strict = not args.no_strict

    # 与 run 同序：先让结构层错误自己说话，再去找数据。顺序反了会把
    # 「键拼错」报成「数据不存在」，而这两件事的处置完全不同。
    compile_plan(
        baseline=None, plan=plan, overrides=overrides or None,
        aliases=None, require_supply=False, strict=strict,
        allow_constitutive_override=args.allow_constitutive_override,
    )

    symbol_sets = load_json_dict(layout.symbol_sets, code=E1004, label="标的集表")
    # 与 run 同快照视图：explain 报的指纹必须就是 run 会冻结的指纹。只读命令
    # 不建快照；库里一份都没有时退回 legacy 视图（optional=True）。
    _sid, snapshot_manifest = resolve_snapshot_for_plan(
        plan, resolve_data_root(), optional=True,
    )
    aliases, symbol_sets, supply_meta, _datasets = build_supply_context(
        plan, symbol_sets, resolve_data_root(), manifest=snapshot_manifest,
    )
    compiled = compile_plan(
        baseline=None, plan=plan, overrides=overrides or None,
        aliases=aliases, symbol_sets=symbol_sets, supply_meta=supply_meta,
        require_supply=True,
        strict=strict,
        allow_constitutive_override=args.allow_constitutive_override,
    )
    return envelope_ok("explain", None, {
        "origins": compiled.origins,
        "fingerprints": compiled.fingerprints,
    })
