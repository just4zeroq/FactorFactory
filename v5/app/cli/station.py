"""flow station —— 单步执行某工位（核心命令，writes_facts）。

S5 语义（s0/05 §6、s0/02 §4）：

* `--probe`（默认，无 flag 时同义）：产物写 `steps/`，**不**推进正式状态、
  不写 `state.jsonl`、不触发事件、不碰已发布结论。四项不变是 AC7 的判据。
* `--commit`：产物写 `parcels/`，追加一条 `state.jsonl`；**仍不触发事件**。
  `append_only` 工位（Search / Verdict）拒绝提交（E3006）。
* 两者互斥；同时给就是 E1001，不静默选一个。

单步没有 `--plan`：构成必须来自 ticket 目录里的 `frozen/`（`run` 落盘），否则
「单步重放」就成了「拿别的参数重算一遍」。
"""

from __future__ import annotations

import json
from typing import Any

from v5.contracts.errors import E1001, E1004, E3001, E3002, FlowError
from v5.facilities import StructlogLogSink, build_facilities, install_log, reset_log
from v5.orchestration import Executor
from v5.orchestration.station import BY_NAME
from v5.runtime.store import TicketWriter, resolve_data_root
from v5.storage.layout import Layout
from v5.upstream import store
from v5.capability.params import implementation_defaults

from .common import build_supply_context, envelope_ok, load_json_dict
from .live import build_live_supply


def _supply_staleness(plan: dict, ctx: dict, root) -> dict[str, Any]:
    """对比冻结时的供给 checksum 与当前磁盘：变了就是 stale。

    为什么必须在单步前查：单步没有 `--plan`，它的合法性完全来自「和 run 时
    同一份输入」。数据被重新采集过（`flow fetch` 又跑了一次、或换了标的集），
    单步仍会算出一份看似正常的产物，而它与 ticket 里的结论不同源——这是
    「单步重放」最容易骗过自己的地方。

    老 ticket 没有 `supply_pins`（本轮之前生成的），显式标 `legacy_ticket`，
    不假装检查过。
    """
    frozen = ctx.get("supply_pins") or {}
    if not frozen:
        return {"checked": False, "reason": "legacy_ticket", "stale": False,
                "changed": []}
    supply = plan.get("supply") or {}
    exchange = str(supply.get("exchange") or "")
    market = str(supply.get("market") or "futures")
    tf = str(supply.get("frequency") or "1d")
    changed: list[dict[str, Any]] = []
    for dataset, pin in frozen.items():
        try:
            ds = store.read_dataset(
                root, dataset, exchange, market,
                timeframe=tf if dataset == store.BAR_DATASET else None,
            )
        except FlowError as exc:
            changed.append({"dataset": dataset, "reason": "unreadable",
                            "code": exc.error.code, "message": exc.error.message})
            continue
        if str(ds.checksum) != str((pin or {}).get("checksum")):
            changed.append({
                "dataset": dataset, "reason": "checksum_changed",
                "frozen": (pin or {}).get("checksum"), "current": ds.checksum,
            })
    return {"checked": True, "stale": bool(changed), "changed": changed}


def _frozen(writer: TicketWriter, ticket_id: str) -> tuple[dict, dict]:
    """读回冻结方案与执行上下文；缺一份就 E3001（不猜默认值）。"""
    plan_path = writer.ticket_root / "frozen" / "plan.json"
    ctx_path = writer.ticket_root / "frozen" / "context.json"
    if not plan_path.exists() or not ctx_path.exists():
        raise FlowError(
            E3001, f"ticket {ticket_id} 没有冻结方案",
            path=f"ticket.{ticket_id}.frozen",
            hint="先 flow run 生成 ticket；单步只认冻结方案，不接受现场补参数",
        )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or not isinstance(ctx, dict):
        raise FlowError(
            E3001, f"ticket {ticket_id} 的冻结方案不可解析",
            path=f"ticket.{ticket_id}.frozen",
        )
    return plan, ctx


def cmd_station(args: Any) -> dict[str, Any]:
    # P2：--probe 与 --commit 互斥，不得静默选一个
    if args.probe and args.commit:
        raise FlowError(E1001, "--probe 与 --commit 互斥",
                        hint="观察与提交必须分开，见 s0/05 §6")
    name = str(args.name)
    if name not in BY_NAME:
        raise FlowError(E1001, f"未知工位 {name!r}", path="station.name",
                        hint=f"可用工位: {sorted(BY_NAME)}")
    # 无 flag = 观察。默认落在 probe 上，是因为「没说要提交」不等于「允许提交」。
    mode = "commit" if args.commit else "step"

    layout = Layout(resolve_data_root())
    writer = TicketWriter(layout, str(args.on))
    plan, ctx = _frozen(writer, str(args.on))

    # S5-M3 stale 闸：数据变了就不许在这张 ticket 上续跑。放在最前面——等
    # Boundary 跑完再发现数据不同源，已经白算了一个工位。
    stale = _supply_staleness(plan, ctx, layout.root)
    if stale["stale"]:
        detail = "; ".join(
            f"{c['dataset']}({c['reason']})" for c in stale["changed"][:3])
        raise FlowError(
            E3002,
            f"ticket {args.on} 的供给已失效: {detail}",
            path=f"ticket.{args.on}.supply",
            hint="数据被重新采集过；用当前供给重新 flow run，不要在这张 ticket 上续跑",
        )

    sink = StructlogLogSink(layout.log_dir(str(args.on)) / "process.jsonl")
    # 与 `run` 用**同一个** master_seed 与 seed 作用域，否则「单步重放」拿到的
    # sub_seed 与正式运行不同——产物还能算，但指纹对不上，等于用另一套随机流
    # 重算了一遍。两者都从冻结产物里读，不在单步现场猜默认值。
    frozen_fp = ctx.get("fingerprints") or {}
    master_seed = int((plan.get("search") or {}).get("seed")
                      or implementation_defaults("cli")["master_seed"])
    fx = build_facilities(scope="ticket", ticket_id=str(args.on),
                          master_seed=master_seed, log_sink=sink)
    token = install_log(fx.log)
    try:
        fx.log.info("station.start", f"单步 {name} mode={mode}")
        kwargs: dict[str, Any] = dict(
            writer=writer,
            policy_digest=ctx.get("policy_digest"),
            runtime_facts={"blueprint_digest": (ctx.get("fingerprints") or {})
                           .get("blueprint_digest")},
            station_params=ctx.get("station_params") or {},
            station_impls=ctx.get("station_impls") or {},
            policy_view=ctx.get("policy_view") or {},
            seed_scope=str(frozen_fp.get("plan_digest") or args.on),
        )
        if ctx.get("live"):
            symbol_sets = load_json_dict(layout.symbol_sets, code=E1004, label="标的集表")
            _pins, _sets, _meta, supply_datasets = build_supply_context(
                plan, symbol_sets, resolve_data_root(),
            )
            kwargs.update(
                use_capabilities=True,
                supply_fn=build_live_supply(datasets=supply_datasets, plan=plan),
            )
        result = Executor(fx, **kwargs).execute_station(str(args.on), name, mode=mode)
        fx.log.info("station.done", f"单步 {name} 完成")
    finally:
        reset_log(token)
        fx.log.close()

    if result.skip_reason is not None:
        # 结果性跳过（零合格候选等）：没有产物可给，但仍是一条成功执行的结论。
        return envelope_ok("station", str(args.on), {
            "station": name,
            "mode": mode,
            "state": "skipped",
            "skip_reason": result.skip_reason,
            "trace": result.traces[-1],
            "live": bool(ctx.get("live")),
            "stale_check": stale,
            "stage": "S5-single-step",
        })

    parcel = result.parcels[name]
    return envelope_ok("station", str(args.on), {
        "station": name,
        "mode": mode,
        "scope": parcel.scope_dir(),
        "payload_digest": parcel.payload_digest,
        "parcel_digest": parcel.parcel_digest,
        "call_digest": parcel.call_digest,
        "trace": result.traces[-1],
        "state": result.state,
        "idempotent": result.idempotent,
        "state_recovered": result.state_recovered,
        "live": bool(ctx.get("live")),
        "stale_check": stale,
        "stage": "S5-single-step",
    })
