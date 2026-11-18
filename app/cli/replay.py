"""flow evaluate / backtest / certify —— 在**冻结 ticket** 上重算单点结论。

三条命令过去一直"声明了但没有入口"，拒绝理由写的是「需要 S5-M2 的对象重建」。
那件事已经做完了（`CapabilityBridge.rebuild_upstream`，八工位单步与 run 逐字节
一致），所以这里把它们接上，并遵守三条边界：

1. **只认冻结 ticket**：没有 `--plan`，构成全部来自 `frozen/plan.json`。与 `station`
   同一取舍——否则"重算"就成了"换参数再算一遍"。
2. **不产新结论、不改已发布事实**：三者都在 `steps/` 语义下重算（probe），
   返回的是"这张 ticket 上这件事重算出来是什么"。`certify` 仍要求 `--yes`，
   因为它的契约等级是 writes_facts + writes_ledger，不能因为实现是重算就降级。
3. **重算走同一条能力链**：evaluate 用 `evaluate` 能力、backtest 用 Portfolio 工位
   （内部 combine → allocate → backtest）、certify 用 Certify 工位。命令层不自己
   算指标——自己算一份就等于多了一套口径。

三条命令的输入都是 ticket id；`evaluate` 另可 `--candidate` 收窄到单个候选。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from capability import Call, InputRef, run as capability_run
from capability.inference import ic_series
from contracts.digest import compute_digest
from contracts.errors import E3001, E3003, FlowError
from facilities import StructlogLogSink, build_facilities, install_log, reset_log
from orchestration import Executor
from orchestration.capability_bridge import CapabilityBridge
from orchestration.executor import load_committed_parcel
from orchestration.station import BY_NAME
from runtime.store import TicketWriter, resolve_data_root
from storage.layout import Layout

from app.contract import require_confirmation
from capability.params import implementation_defaults

from .common import (
    build_supply_context, envelope_ok, load_json_dict, load_snapshot_for_ticket,
)
from .live import build_live_supply
from .station import _frozen

# 三个命令各自重算到哪一步。evaluate 只需要面板与标签；backtest/certify 走完整链。
_EVALUATE_UPTO = ("Boundary", "Panel", "Target")


class _Replay:
    """一次 ticket 重算的公共装配（facilities + executor + 冻结上下文）。"""

    def __init__(self, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        self.layout = Layout(resolve_data_root())
        self.writer = TicketWriter(self.layout, ticket_id)
        self.plan, self.ctx = _frozen(self.writer, ticket_id)
        # 关键：重建必须用 run 当时绑定的快照，而不是「当前目录里有什么」。
        # 补采历史后目录变了，若重新扫描就会算出另一份 Boundary，E3002 正是
        # 这条链上被抓出来的。清单里的字节被改写则 fail-closed，不静默重算。
        self.snapshot_id, self.snapshot_manifest = load_snapshot_for_ticket(
            self.ctx, self.layout.root,
        )
        self.master_seed = int((self.plan.get("search") or {}).get("seed")
                               or implementation_defaults("cli")["master_seed"])
        sink = StructlogLogSink(self.layout.log_dir(ticket_id) / "process.jsonl")
        self.fx = build_facilities(scope="ticket", ticket_id=ticket_id,
                                   master_seed=self.master_seed, log_sink=sink)
        from orchestration.events import EventLedger
        from storage.ledger import Ledger

        self.kwargs: dict[str, Any] = dict(
            writer=self.writer,
            policy_digest=self.ctx.get("policy_digest"),
            runtime_facts={"blueprint_digest": (self.ctx.get("fingerprints") or {})
                           .get("blueprint_digest")},
            station_params=self.ctx.get("station_params") or {},
            station_impls=self.ctx.get("station_impls") or {},
            policy_view=self.ctx.get("policy_view") or {},
            seed_scope=str((self.ctx.get("fingerprints") or {}).get("plan_digest")
                           or ticket_id),
            events=EventLedger(Ledger(self.layout)),
            # read_only：只在内存里重算，不落 steps/、不写 trace
            persist=False,
        )
        if self.ctx.get("live"):
            symbol_sets = load_json_dict(self.layout.symbol_sets, code=E3001,
                                         label="标的集表")
            _pins, _sets, _meta, supply_datasets = build_supply_context(
                self.plan, symbol_sets, resolve_data_root(),
                manifest=self.snapshot_manifest,
            )
            self.kwargs.update(
                use_capabilities=True,
                supply_fn=build_live_supply(datasets=supply_datasets, plan=self.plan),
            )
        self.executor = Executor(self.fx, **self.kwargs)

    def parcels(self, stations: tuple[str, ...]) -> dict[str, Any]:
        return {s: load_committed_parcel(self.layout, self.ticket_id, s) for s in stations}

    def impls(self, stations: tuple[str, ...]) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in stations:
            name, ver = self.executor._impl_for(BY_NAME[s])  # noqa: SLF001 - 同一组合根
            out[s] = f"{name}@{ver}"
        return out

    def close(self) -> None:
        self.fx.log.close()


def _evaluate_candidates(ctx: "_Replay", candidate: str | None) -> list[dict[str, Any]]:
    """用 `evaluate` 能力逐个候选重算暴露，再算 IC/ICIR（不产裁决）。"""
    bridge = ctx.executor.bridge
    if bridge is None:
        raise FlowError(E3003, "ticket 不是 live 运行，无法重算表达式",
                        path="ticket.live")
    parcels = ctx.parcels(_EVALUATE_UPTO)
    # 传**执行器归一后**的 station_params：冻结上下文里是小写工位键，而
    # rebuild_upstream 按 `StationSpec.name`（Boundary/Panel/...）取值，直接传
    # 原始 dict 会让每个工位都拿到空参数，报成「boundary.params 缺 k」。
    objects = bridge.rebuild_upstream(
        parcels, impls=ctx.impls(_EVALUATE_UPTO),
        policy_view=ctx.executor.policy_view,
        runtime_facts=ctx.executor.runtime_facts,
        station_params=ctx.executor.station_params,
    )
    panel = (objects.get("Panel") or {}).get("panel")
    label = (objects.get("Target") or {}).get("label")
    if panel is None or not isinstance(label, Mapping):
        raise FlowError(E3001, "无法从 ticket 重建 panel/label",
                        path=f"ticket.{ctx.ticket_id}.objects")
    score = np.asarray(label.get("score"), dtype="float64")
    search_payload = ctx.parcels(("Search",))["Search"].payload
    cands = list(search_payload.get("candidates") or [])
    if candidate:
        cands = [c for c in cands if str(c.get("candidate_id")) == candidate]
        if not cands:
            raise FlowError(E3003, f"ticket 里没有候选 {candidate!r}",
                            path="evaluate.candidate")
    from capability.registry import default_registry
    from importlib.metadata import version as _pkg_version

    registry = default_registry()
    version = _pkg_version("qweave")
    rows: list[dict[str, Any]] = []
    for cand in cands:
        ast = (cand.get("payload") or {}).get("expression")
        if not isinstance(ast, Mapping):
            continue
        call = Call.create(
            capability="evaluate", station="Search", impl="qweave",
            impl_version=version,
            params={"operator_set": registry.operators_version,
                    "nan_policy": "propagate", "node_cache": False},
            inputs=(InputRef("payload", "sha256:" + "0" * 64, data=dict(ast)),
                    InputRef("panel", panel.digest(), data=panel)),
        )
        try:
            out = capability_run(call)
        except FlowError as exc:
            rows.append({"candidate_id": cand.get("candidate_id"),
                         "status": "unobservable", "code": exc.error.code,
                         "message": exc.error.message})
            continue
        values = np.asarray(out["values"], dtype="float64")
        valid = np.asarray(out["valid_mask"], dtype=bool)
        mask = valid & np.isfinite(score)
        ic = ic_series(np.where(mask, values, np.nan), np.where(mask, score, np.nan))
        finite = ic[np.isfinite(ic)]
        mean = float(finite.mean()) if finite.size else 0.0
        std = float(finite.std(ddof=1)) if finite.size > 1 else 0.0
        from capability.panel import array_digest as _array_digest

        rows.append({
            "candidate_id": cand.get("candidate_id"),
            "status": "observed",
            # 09 §4 的输出契约：expression_digest / series_ref / stats{}
            "expression_digest": compute_digest({"kind": "expression", "ast": dict(ast)}),
            "series_ref": _array_digest(np.ascontiguousarray(
                np.where(valid, values, np.nan))),
            "stats": {
                "ic_mean": mean,
                "rank_ic": mean,
                "icir": (mean / std) if std > 1e-12 else 0.0,
                "n_periods": int(finite.size),
                "coverage": float(mask.sum()) / float(mask.size),
            },
        })
    return rows


def cmd_evaluate(args: Any) -> dict[str, Any]:
    ctx = _Replay(str(args.on))
    token = install_log(ctx.fx.log)
    try:
        rows = _evaluate_candidates(ctx, getattr(args, "candidate", None))
    finally:
        reset_log(token)
        ctx.close()
    return envelope_ok("evaluate", str(args.on), {
        "ticket": str(args.on),
        "count": len(rows),
        "evaluation": rows,
        "stage": "S7-single-command",
    })


def cmd_backtest(args: Any) -> dict[str, Any]:
    ctx = _Replay(str(args.on))
    token = install_log(ctx.fx.log)
    try:
        result = ctx.executor.execute_station(str(args.on), "Portfolio", mode="step")
    finally:
        reset_log(token)
        ctx.close()
    if result.skip_reason is not None:
        return envelope_ok("backtest", str(args.on), {
            "ticket": str(args.on), "state": "skipped",
            "skip_reason": result.skip_reason, "summary": {},
        })
    summary = dict((result.objects.get("Portfolio") or {}).get("tail_summary") or {})
    if not summary:
        raise FlowError(
            E3003, "Portfolio 重算没有产出净值摘要",
            path="backtest.summary",
            hint="combine 的尾链（allocate+backtest）没跑或没写 objects['tail_summary']",
        )
    return envelope_ok("backtest", str(args.on), {
        "ticket": str(args.on),
        "state": "done",
        "summary": summary,
        "evidence_grade": result.parcels["Portfolio"].payload.get("evidence_grade"),
        "stage": "S7-single-command",
    })


def cmd_certify(args: Any) -> dict[str, Any]:
    require_confirmation("certify", args)
    ctx = _Replay(str(args.on))
    token = install_log(ctx.fx.log)
    try:
        result = ctx.executor.execute_station(str(args.on), "Certify", mode="step")
    finally:
        reset_log(token)
        ctx.close()
    payload = result.parcels["Certify"].payload
    return envelope_ok("certify", str(args.on), {
        "ticket": str(args.on),
        "state": "done",
        "certificate": {
            "metrics": payload.get("metrics") or {},
            "folds_detail": payload.get("folds_detail") or [],
            "evidence_grade": payload.get("evidence_grade"),
            "verdict_refs": payload.get("verdict_refs") or [],
        },
        "stage": "S7-single-command",
    })


__all__ = ["cmd_backtest", "cmd_certify", "cmd_evaluate"]
