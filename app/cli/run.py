"""flow run —— 编译 + 跑八步（核心命令，writes_facts）。"""

from __future__ import annotations

import json
from typing import Any

from facilities import StructlogLogSink, build_facilities, install_log, reset_log
from orchestration import Executor
from orchestration.events import EventLedger
from runtime.compiler import STATIONS, compile_plan
from runtime.store import TicketWriter, resolve_data_root
from storage.layout import Layout
from storage.ledger import Ledger

from .common import (
    build_supply_context, envelope_ok, load_json_dict, load_plan, new_ticket_id,
    parse_overrides, resolve_snapshot_for_plan,
)
from .live import build_live_supply, expand_station_inputs

from contracts.errors import E1004
from capability.params import implementation_defaults


def freeze_context(writer: TicketWriter, ctx: dict[str, Any]) -> None:
    """把「这次执行的构成」写进 `frozen/`（s0/05 §6：单步只认冻结方案）。

    单步命令 `flow station` 没有 `--plan`，它必须能从 ticket 目录里还原出同一份
    params / impl / 口径，否则「单步重放」就是拿别的参数重算一遍。写入用内容寻址
    的 `write_bytes`：同 ticket 重复 run 内容相同即幂等，内容不同直接拒绝——
    冻结的意思就是不能再改。
    """
    payload = json.dumps(ctx["plan"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    writer.write_bytes("frozen/plan.json", payload)
    context = {k: v for k, v in ctx.items() if k != "plan"}
    writer.write_bytes(
        "frozen/context.json",
        json.dumps(context, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8"),
    )


def cmd_run(args: Any) -> dict[str, Any]:
    """编译 + 执行。S7：编译含真实供给身份与门禁 B；`--live` 才调真实能力。"""
    plan = load_plan(args)
    overrides = parse_overrides(args.set)
    layout = Layout(resolve_data_root())
    strict = not args.no_strict

    # 先做一次「无供给」的编译：结构层 / 跨字段 / 实现名的错误必须先暴露。
    # 否则计划里把 `horizon` 拼成 `horizn`，用户收到的却是「数据文件不存在」——
    # 诊断方向完全反了，而且会把人骗去先采一遍数据。纯函数，多跑一次的成本可忽略。
    compile_plan(
        baseline=None, plan=plan, overrides=overrides or None,
        aliases=None, require_supply=False, strict=strict,
        allow_constitutive_override=args.allow_constitutive_override,
    )

    # 供给身份现算：没有别名表可读，路径即约定、内容即版本（19-supply.md）。
    symbol_sets = load_json_dict(layout.symbol_sets, code=E1004, label="标的集表")
    # 快照先于供给读取解析：一旦声明了 snapshot 且当前文件已变，必须在任何
    # 工位执行前 fail-closed，否则会把新数据算进旧实验（E2005）。
    resolved_snapshot_id, snapshot_manifest = resolve_snapshot_for_plan(
        plan, resolve_data_root(), auto_create=True,
    )
    aliases, symbol_sets, supply_meta, supply_datasets = build_supply_context(
        plan, symbol_sets, resolve_data_root(), manifest=snapshot_manifest,
    )

    compiled = compile_plan(
        baseline=None, plan=plan, overrides=overrides or None,
        aliases=aliases, symbol_sets=symbol_sets, supply_meta=supply_meta,
        require_supply=True,
        strict=strict,
        allow_constitutive_override=args.allow_constitutive_override,
    )
    # ticket_id 必须是 `<UTC>-<plan_digest 前8>`（Ticket 契约），不能拿 plan 名。
    ticket_id = new_ticket_id(compiled.fingerprints["plan_digest"])
    # 组合根构造 sink 并注入（F1：install 只在应用处；其余各处 get_log() 只读解析）。
    # 路径由 Layout 唯一给出，Logger 自身无 path 属性。
    sink = StructlogLogSink(layout.log_dir(ticket_id) / "process.jsonl")
    # 执行产物必须落盘：writer 不传就只在内存里，进程退出即消失，而信封仍报
    # ok/parcels=N——"报告成功却什么都没留下"是契约违背，不是 S1 边界。
    writer = TicketWriter(layout, ticket_id)
    # `master_seed` 必须来自计划（`search.seed`）：硬编码常量会让「改 seed 只改
    # 指纹、不改随机流」，用户写进 plan 的 seed 形同虚设。缺省仍给 42。
    search_block = compiled.plan.get("search") or {}
    master_seed = int(search_block.get("seed") or implementation_defaults("cli")["master_seed"])
    fx = build_facilities(scope="ticket", ticket_id=ticket_id,
                          master_seed=master_seed, log_sink=sink)
    token = install_log(fx.log)
    try:
        fx.log.info("run.start", "run 开始")

        # 工位块整体进调用单：impl 走实现通道，块内其余字段（params/time_axis/
        # algorithms…）整体走 params 通道。漏传会让"不同方案 → 同一 call_digest"。
        blocks = {k: v for k, v in compiled.plan.items()
                  if k in STATIONS and isinstance(v, dict)}
        station_impls = {k: v["impl"] for k, v in blocks.items() if "impl" in v}
        station_params = {k: {kk: vv for kk, vv in v.items() if kk != "impl"}
                          for k, v in blocks.items()}

        live = bool(getattr(args, "live", False))
        kwargs: dict[str, Any] = dict(
            writer=writer,
            policy_digest=compiled.fingerprints.get("policy_digest"),
            runtime_facts={"blueprint_digest": compiled.fingerprints.get("blueprint_digest")},
            station_params=station_params,
            station_impls=station_impls,
            # 事件落全局账本（db/events.jsonl）：Reveal/Budget/Publish 的「一次性」
            # 必须跨进程成立。只在内存里记，进程重启后同一 ticket 可以再揭示一次、
            # 再计数一次，AC8 就是空的（23 之前这里恒传 None）。
            events=EventLedger(Ledger(layout)),
            # 随机源作用域＝内容指纹（不含时间），否则同一份计划每次运行都换
            # 一套候选，「可复现」只是口号（见 executor.Executor._call_seed）。
            seed_scope=str(compiled.fingerprints["plan_digest"]),
        )
        if live:
            # 计划是嵌套的（params/time_axis + caliber），能力读扁平 params +
            # policy_view。展开只在这里做一次；供给从已发布 seed 注入。
            live_params, policy_view = expand_station_inputs(compiled.plan)
            # combine 是 closed_multi：计划里的 combiner 名字必须进实现通道，
            # 不能混进 params（那会让「换合成器」只改参数指纹、不改 impl 指纹）。
            impls = dict(kwargs["station_impls"])
            portfolio = compiled.plan.get("portfolio") or {}
            if portfolio.get("combiner"):
                impls["Portfolio"] = str(portfolio["combiner"])
            kwargs.update(
                station_impls=impls,
                station_params=live_params,
                policy_view=policy_view,
                use_capabilities=True,
                supply_fn=build_live_supply(
                    datasets=supply_datasets, plan=compiled.plan,
                ),
            )
            fx.log.info("run.live", "真实能力与供给注入已启用")

        # 冻结方案先落盘再执行：单步命令只认磁盘上的这一份，不认调用方的记忆。
        # `supply_pins` 是 S5-M3 stale 判定的唯一依据：单步前重算 checksum 与这里
        # 比对，数据被重新采集过就拒绝续跑——否则单步会在「另一份数据」上重算，
        # 产物看似成功，实际与 ticket 的结论不再同源。
        freeze_context(writer, {
            "ticket_id": ticket_id,
            "plan": compiled.plan,
            "fingerprints": compiled.fingerprints,
            "resolved_snapshot_id": resolved_snapshot_id,
            "snapshot_manifest_digest": (snapshot_manifest or {}).get("manifest_digest"),
            "live": live,
            "master_seed": master_seed,
            "seed_scope": kwargs["seed_scope"],
            "station_params": kwargs["station_params"],
            "station_impls": kwargs["station_impls"],
            "policy_view": kwargs.get("policy_view", {}),
            "policy_digest": kwargs.get("policy_digest"),
            "supply_pins": {
                str(name): {
                    "checksum": ds.checksum,
                    "version": ds.version,
                    "rows": int(ds.rows),
                    "symbols": len(ds.symbols),
                }
                for name, ds in (supply_datasets or {}).items()
            },
        })
        result = Executor(fx, **kwargs).execute(ticket_id)
        fx.log.info("run.done", "run 完成", parcels=len(result.parcels))
    finally:
        reset_log(token)
        fx.log.close()

    return envelope_ok("run", ticket_id, {        "station_specs": list(STATIONS),
        "fingerprints": compiled.fingerprints,
        "origins_count": len(compiled.origins),
        "parcels": len(result.parcels),
        "traces": len(result.traces),
        "state": result.state,
        # 零晋升是结论：`skip_reason` 非空时 state=skipped，退出码仍为 0。
        "skip_reason": result.skip_reason,
        "skipped_at": result.skipped_at,
        "stage": "S7-live-capability" if live else "S2-real-supply",
        "live": live,
        "resolved_snapshot_id": resolved_snapshot_id,
        # 读进来的数据集（含未接入成本模型的辅助数据集）；「已采集」不等于「已使用」。
        "supply_datasets": sorted(supply_datasets),
    })
