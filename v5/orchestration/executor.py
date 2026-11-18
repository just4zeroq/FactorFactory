"""唯一执行器（s0/05-orchestration.md §2/§4）—— S1 真实化。

整体跑、单步跑、恢复共用这一个 execute()。S1 把 S0 的三处占位换成真实实现：

1. **调用单六通道**（s0/01 §2.2 / s0/04 §2）：`impl@version`、`params`、
   `inputs`、`policy_view`、`runtime_facts`、`composed` 全部参与
   `call_digest`（经 `contracts.digest.call_digest`，含 phase 与 seed）。
   任意通道变化 → call_digest 变化 → 下游整条指纹链变化。
2. **指纹链**：上游 `parcel_digest` 进入下游 `inputs` 与 `lineage`；
   `parcel_digest = H(call_digest, payload_digest)`（contracts 真公式）。
   手改 payload 不改 digest → E3002。
3. **真实依赖校验**：按 `spec.inputs` 解析的上游工位（不是 S0 的线性链），
   上游缺失 → E3001；上游 `stale` 或指纹失配 → E3002；产物为空 → E3003。

落盘顺序严格按 s0/05 §2.1 的第 11–13 步：`payload.json` → `log.jsonl` →
`parcel.json` → `trace.jsonl`。`parcel.json` 用原子写（`TicketWriter`），因此
不存在"存在但不完整"的半成品；失败时只写 trace、不写 `parcel.json`。

边界（S5 已接通，S5-M2 留给下一步）：`--probe`/`--commit` 分流、单步从磁盘读上游、
提交只推进状态、`append_only` 拒绝提交，均已实现；事件仍是内存 `EventLedger`
（落全局账本与断电恢复属 S5-M2），能力模式单步仍需按 Ref 重建对象（`E3003`
fail-closed，见 s0/05 §6.2）。

能力接入（S3-M6）：`use_capabilities=True` 时经 `CapabilityBridge` 调真实能力
（内存对象表喂 data 通道、落盘视图剥离对象、未实现能力 fail-closed E3004）；
缺省仍走 dummy 桩以保持 S0/S1/S2 验收与无供给数据的场景可用。两条路径共用
同一条调用链、指纹链与落盘逻辑，切换开关不改变规格。
"""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

import numpy as np

from v5.contracts import digest as dg
from v5.contracts.digest import blob_digest, compute_digest
from v5.contracts.errors import (
    E1001, E1004, E3001, E3002, E3003, E3004, E3006, FlowError,
)
from v5.contracts.types import Digest, Ref
from v5.facilities import Facilities
from v5.facilities.trace import TraceRecord
from v5.runtime.store import TicketWriter
from v5.storage.objects import ObjectStore
from v5.capability.params import implementation_defaults

from .capability_bridge import BridgeResult, CapabilityBridge
from .events import EventLedger
from .state import StateMachine
from .station import BY_NAME, STATION_SPECS, StationSpec, input_producers

# 工位实现：S1 仍是 dummy 桩，返回固定空产物结构（不含真实数字）。
# 签名保留 `(station, inputs)`；inputs 现在是解析后的 {上游工位: parcel_digest}。
DummyFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def _dummy(station: str, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"station": station, "kind": "dummy", "payload": {}}


def _split_impl(default_impl: str, station: str) -> tuple[str, str]:
    """`name@version` → (name, version)。缺 `@` 是规格表错误，不得静默降级。"""
    impl, sep, version = default_impl.partition("@")
    if not sep or not impl or not version:
        raise FlowError(
            E1004, f"{station} 的 default_impl 必须是 name@version: {default_impl!r}",
            path=f"station.{station}.default_impl",
        )
    return impl, version


_LOWER_TO_NAME = {spec.name.lower(): spec.name for spec in STATION_SPECS}


def _canon_station(key: str) -> str:
    """plan 用小写工位键（`boundary`），规格表用大写名（`Boundary`）。

    两套写法必须有唯一归一入口：直接按名字查会因大小写不同静默漏掉参数，
    而"漏掉参数"在指纹上表现为"这个方案和那个方案相同"。
    """
    name = _LOWER_TO_NAME.get(str(key).lower())
    if name is None:
        raise FlowError(
            E1004, f"未知工位名: {key!r}",
            hint=f"合法工位: {sorted(_LOWER_TO_NAME.values())}",
        )
    return name


@dataclass
class Parcel:
    """执行期产物（S0 兼容接口 + S1 指纹链与落盘上下文）。

    `payload` 保留为可写 dict 是为了 O3「手改 payload → E3002」的拒绝演示；
    `parcel_digest` 由真公式 `H(call_digest, payload_digest)` 产生，任何字段
    被就地改写后 `verify()` 都会失败。

    `payload.json` / `log.jsonl` 的**内容由本对象确定性生成**（同一 fingerprint
    重放得到逐字节相同的文件），`Ref.digest` 与实际写入的字节一一对应。
    """

    station: str
    payload: dict[str, Any]
    parcel_digest: str
    call_digest: str
    payload_digest: str
    lineage: list[tuple[str, str]] = field(default_factory=list)
    station_revision: str = "r1"
    impl: str = "boundary@1"
    origin: str = "run"
    # 落盘上下文：不进 parcel_digest（digest 只由 call+payload 决定）
    ticket_id: str = ""
    seq: int = 0
    started_at: datetime | None = None
    seconds: float = 0.0

    def verify(self) -> None:
        """校验 payload 未被就地改写，且指纹链自洽；手改即 E3002。"""
        re_payload = compute_digest(self.payload)
        if re_payload != self.payload_digest:
            raise FlowError(E3002, f"{self.station} 产物指纹失配（payload 被改写）",
                            path=f"parcel.{self.station}.payload")
        if dg.parcel_digest(self.call_digest, self.payload_digest) != self.parcel_digest:
            raise FlowError(E3002, f"{self.station} 产物指纹失配（parcel 摘要不自洽）",
                            path=f"parcel.{self.station}.digest")

    # ---- 落盘内容（确定性，重放逐字节一致） ---------------------------
    def scope_dir(self) -> str:
        """写入范围：`steps/` 仅单步观察，`parcels/` 是正式产物（s0/02 §4）。

        范围只由 `origin` 决定：`step` 是隔离观察，其余（`run` / `commit` /
        `resume` / `cache`）进正式目录。这样「probe 的产物去哪了」不需要调用方
        记参数，产物自身的 origin 就是答案。
        """
        return "steps" if self.origin == "step" else "parcels"

    def base_relpath(self) -> str:
        """工位产物目录：`<scope>/<seq>-<Station>-<payload hex 前16>`。"""
        short = self.payload_digest.split(":", 1)[-1][:16]
        return f"{self.scope_dir()}/{self.seq:02d}-{self.station}-{short}"

    def payload_bytes(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def log_bytes(self) -> bytes:
        """工位日志（s0/02 §2）：一条完成记录，文件名由 call_digest 反推。"""
        at = self.started_at.isoformat() if self.started_at is not None else ""
        record = {
            "at": at,
            "level": "info",
            "event": "station.done",
            "message": f"{self.station} 完成",
            "ticket_id": self.ticket_id,
            "station": self.station,
            "seq": self.seq,
            "call_digest": self.call_digest,
            "payload_digest": self.payload_digest,
        }
        return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    def to_contract(self) -> Any:
        """转成 contracts.Parcel 值对象（落盘真相）。延迟导入避免循环。"""
        from v5.contracts.types import Parcel as ContractParcel

        impl, version = _split_impl(self.impl, self.station)
        base = self.base_relpath()
        return ContractParcel(
            ticket_id=self.ticket_id,
            station=self.station,
            station_revision=self.station_revision,
            impl=impl,
            impl_version=version,
            call_digest=Digest.parse(self.call_digest),
            payload_ref=Ref(kind="parcel", id=f"{self.ticket_id}:{self.station}",
                            digest=Digest.parse(self.payload_digest),
                            relpath=f"{base}/payload.json",
                            schema_version="1"),
            payload_digest=Digest.parse(self.payload_digest),
            digest=Digest.parse(self.parcel_digest),
            lineage=[(name, Digest.parse(d)) for name, d in self.lineage],
            evidence=[],
            origin=self.origin,
            status="done",
            skip_reason=None,
            error=None,
            started_at=self.started_at,
            seconds=self.seconds,
            log_ref=Ref(kind="derived", id=f"{self.ticket_id}:{self.station}:log",
                        digest=Digest.parse(blob_digest(self.log_bytes())),
                        relpath=f"{base}/log.jsonl",
                        schema_version="1"),
        )


@dataclass
class ExecuteResult:
    ticket_id: str
    parcels: dict[str, Parcel]
    traces: list[dict[str, Any]]
    state: str
    events: list[dict[str, Any]] = field(default_factory=list)
    # 单步提交时，若同一 payload 已在正式目录里，这次提交不产生新产物（幂等）。
    idempotent: bool = False
    # S5-M3 断电恢复：幂等提交时发现 parcel 已在而 state.jsonl 缺这条
    # （上次 commit 在两段之间断电），补上状态推进并置 True。
    state_recovered: bool = False
    # 「结果性跳过」：非空表示整条链在 `skipped_at` 停下，原因取自 s0/05 §4.3。
    # 这是结论（例如零晋升），不是失败——调用方据此报 exit 0 而不是 E3xxx。
    skip_reason: str | None = None
    skipped_at: str | None = None
    # 本工位重算后的内存对象表（station → kind → 对象）。单命令（backtest /
    # certify）要读「重算出来的净值/证书」，而它们不进 payload，只活在这里。
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)


class Executor:
    def __init__(
        self,
        facilities: Facilities,
        *,
        dummy_fn: DummyFn | None = None,
        events: EventLedger | None = None,
        writer: TicketWriter | None = None,
        policy_digest: str | None = None,
        policy_view: dict[str, Any] | None = None,
        runtime_facts: dict[str, Any] | None = None,
        station_params: dict[str, dict[str, Any]] | None = None,
        station_impls: dict[str, str] | None = None,
        stale: tuple[str, ...] | list[str] | set[str] = (),
        seed_scope: str | None = None,
        use_capabilities: bool = False,
        supply_fn: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        capability_fn: Callable[..., BridgeResult] | None = None,
        persist: bool = True,
        cache_stations: tuple[str, ...] | list[str] | frozenset[str] = (),
    ) -> None:
        """
        `policy_digest`/`policy_view`/`runtime_facts`/`station_params` 组成调用单的
        四个非 station 通道。缺省时用确定性的空值（S0/S1 dummy 可复现），
        但**照样进 call_digest**——填入真实值时指纹链自然随之改变。
        `writer` 非空时把 payload/log/parcel/trace 落盘；`stale` 列出上游被判失效的工位。

        `station_params` / `station_impls` 的键可用小写（与 plan 的工位键一致），
        内部归一到 `StationSpec.name`。**工位块必须整体进 params 通道**：`time_axis`
        改变数据窗口却不改变 `station.params`，漏掉它会让两个不同方案得到同一个
        call_digest，缓存直接串（s0/01 §2.2「改变结果的东西必须进指纹」）。
        """
        self.fx = facilities
        # F1：缓存 opt-in，且必须按工位白名单开启。payload 可缓存，但能力调用
        # 同时产出内存对象（掩码/候选集/面板），命中时无法从 payload 复原；
        # 全局开启会让下游拿到空对象表而不是报错。
        self.cache_stations = frozenset(cache_stations)
        self.dummy = dummy_fn or _dummy
        self.events = events or EventLedger()
        self.writer = writer
        self.policy_digest = policy_digest
        self.policy_view = dict(policy_view or {})
        self.runtime_facts = dict(runtime_facts or {})
        self.station_params = {_canon_station(k): dict(v)
                               for k, v in (station_params or {}).items()}
        self.station_impls = {_canon_station(k): str(v)
                              for k, v in (station_impls or {}).items()}
        self.stale = frozenset(stale)
        # 只读重算（`flow evaluate/backtest/certify`）：产物只在内存里，不写
        # steps/、不写 trace。契约把这三条标成 read_only，若为实现方便落一份
        # probe 产物，read_only 就是假的。
        self.persist = bool(persist)
        # 随机源的**作用域**：默认退回 ticket_id 保持既有行为，但 ticket_id 含
        # 秒级时间戳（`new_ticket_id`），拿它派生 sub_seed 会让「同计划同数据"
        # 每次都得到不同 seed —— 实测同一份 plan.json 连跑三次拿到 31/24/22 个
        # 候选，AC39「同 seed 两次逐位一致」在 CLI 层根本不成立。组合根必须传
        # 内容作用域（plan_digest），它不含时间/路径/耗时（s0/02 §5）。
        self.seed_scope = seed_scope
        # 本次进程内提交过的工位（`--commit` 的单步记录）。它是**观测缓冲区**，
        # 不是真相：真相在 `state.jsonl`，这里只让调用方拿到刚发生的事。
        self.state_log: list[dict[str, Any]] = []
        # S3-M6 能力接入：默认关闭以保 S0/S1/S2 与无供给场景可用；显式开启后
        # 经 bridge 调真实能力。capability_fn 可整体替换（测试/未来多实现分派）。
        self.use_capabilities = bool(use_capabilities)
        if use_capabilities:
            # 落盘接线（s0/05 §3.1）：能力数据通道写成内容寻址对象，引用进
            # Parcel.payload。ObjectStore 只用 writer 的 layout（同一数据根），
            # 不另开根——两个根会让 ref 指向另一个仓库。
            store = ObjectStore(writer.layout) if writer is not None else None
            self.bridge = CapabilityBridge(
                supply_fn=supply_fn, object_store=store, policy_digest=policy_digest,
            )
        else:
            self.bridge = None
        self.capability_fn = capability_fn

    # ---- 能力调用（S3-M6） -------------------------------------------
    def _call_seed(self, ticket_id: str, spec: StationSpec, seq: int) -> int:
        """工位级 sub_seed：作用域必须是**内容**，不是 ticket 的墙钟前缀。

        `seed_scope` 缺省时退回 ticket_id（单步/测试仍可用），但这会让同计划
        两次运行的随机流不同；组合根给了 plan 指纹就完全可复现。
        """
        scope = self.seed_scope or ticket_id
        return self.fx.rng.derive_seed(f"{scope}:{spec.name}:{seq}")

    def _cache_get(self, call_digest: str, station: str):
        """F1：查缓存。`Cache.get` 已重验文件与内容指纹，这里再验一次反序列化结果。"""
        if station not in self.cache_stations or self.writer is None:
            return None
        hit = self.fx.cache.get(call_digest)
        if hit is None:
            return None
        try:
            target = (Path(self.fx.cache.root) / hit.payload_ref).resolve()
            payload = json.loads(target.read_bytes().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            self.fx.cache.invalidate(call_digest)
            return None
        if not isinstance(payload, dict) or self.fx.digest.of(payload) != hit.payload_digest:
            self.fx.cache.invalidate(call_digest)
            return None
        return payload

    def _cache_put(self, call_digest: str, parcel) -> None:
        """落盘后登记缓存键；产物在 cache.root 之外时静默跳过。"""
        if self.writer is None or parcel.station not in self.cache_stations:
            return
        if not self.fx.cache.writable:
            return
        try:
            target = self.writer.ticket_root / parcel.base_relpath() / "payload.json"
            rel = target.resolve().relative_to(Path(self.fx.cache.root).resolve())
        except ValueError:
            return
        self.fx.cache.put(call_digest, rel.as_posix(), parcel.payload_digest)

    def _from_cache_or_invoke(self, ticket_id, seq, spec, lineage, objects, call_digest):
        """缓存命中跳过**计算**，不跳过落盘、计数与 trace（F6）。"""
        from v5.orchestration.capability_bridge import BridgeResult

        cached = self._cache_get(call_digest, spec.name)
        # s0/02 §7：命中必须由执行器统一把 trace 的 origin 标成 `cache`，
        # 不能让产物看起来像刚算过——否则「跑了几次」这件事无法审计。
        self._cache_hit_last = cached is not None
        if cached is not None:
            return BridgeResult(payload=cached, objects={}, capability="cache")
        return self._invoke_station(
            ticket_id=ticket_id, seq=seq, spec=spec,
            lineage=lineage, objects=objects,
        )

    def _invoke_station(
        self,
        *,
        ticket_id: str,
        seq: int,
        spec: StationSpec,
        lineage: list[tuple[str, str]],
        objects: dict[str, dict[str, Any]],
    ) -> BridgeResult:
        """返回能力调用的完整结果（落盘视图 + 内存对象 + 跳过信号）。

        `skip_reason` 非空表示「结果性跳过」（s0/05 §4.2）：没有产物可落，工位
        记 `skipped`，下游一律 `upstream_skipped`——零晋升是结论，不是故障。
        """
        if not self.use_capabilities:
            return BridgeResult(payload=self.dummy(spec.name, {name: d for name, d in lineage}))
        impl_name, impl_version = self._impl_for(spec)
        invoke = self.capability_fn or self.bridge.invoke
        # 上游对象表：按工位名取每个工位产出的 {kind: object}。
        upstream = {name: dict(objects.get(name, {})) for name, _ in lineage}
        # S4 诊断链要用到非直接上游的对象：Portfolio 需要 Search 的候选暴露与
        # Verdict 的裁决集合；Evidence 的净成本链需要 Boundary 的 tradable_mask/
        # tiers（Evidence 在规格里只依赖 Candidate/Panel/Label）。用 prior_objects
        # 显式传入全部已产出工位对象，不让桥绕过指纹链自己读盘；调用单的
        # input_digests 仍只取 spec 声明的上游——Boundary 的变化必经 Panel 的
        # parcel_digest 传递下来，指纹覆盖不因省掉这条边而失效。
        shared: dict[str, Any] = {}
        if spec.name in ("Portfolio", "Evidence"):
            shared["prior_objects"] = {
                name: dict(value) for name, value in objects.items() if name != spec.name
            }
        result: BridgeResult = invoke(
            station=spec.name,
            capability=None,
            impl=impl_name,
            impl_version=impl_version,
            params=self.station_params.get(spec.name, {}),
            policy_view=self.policy_view,
            runtime_facts=self.runtime_facts,
            phase="none",
            seed=self._call_seed(ticket_id, spec, seq),
            station_revision=spec.revision,
            upstream=upstream,
            shared=shared,
        )
        return result

    # ---- 实现名（plan 声明优先，规格表只是默认） -----------------------
    def _impl_for(self, spec: StationSpec) -> tuple[str, str]:
        """返回 (impl_name, version)。plan 声明了 `impl` 就用它，版本沿用规格表。

        能力模式下继续解析到**算法实现名**：工位语义名（boundary@1）与能力
        注册名（pit_dynamic_topk@1）不同源，若工位指纹记前者、能力 Call 记
        后者，换实现时工位指纹不变而实际执行变了，会静默串缓存（review P2）。
        两条路径统一到能力注册名，指纹才有单一真相。
        """
        default_name, default_version = _split_impl(spec.default_impl, spec.name)
        declared = self.station_impls.get(spec.name)
        if not declared:
            name, version = default_name, default_version
        else:
            parsed_name, sep, parsed_version = declared.partition("@")
            if not parsed_name:
                raise FlowError(
                    E1004, f"{spec.name}.impl 不能为空",
                    path=f"station.{spec.name}.impl",
                )
            name, version = parsed_name, (parsed_version if sep and parsed_version
                                          else default_version)
        if self.use_capabilities and self.bridge is not None:
            from .capability_bridge import STATION_CAPABILITY
            cap = STATION_CAPABILITY.get(spec.name)
            if cap:
                # declared=True 才做「声明名必须真存在」的校验：规格表默认名
                # （`boundary@1`）本来就与能力注册名不同源，不能按声明处理。
                return self.bridge._resolve_impl(cap, name, version,
                                                 declared=bool(declared))
        return name, version

    # ---- 调用单六通道 -------------------------------------------------
    def _call_digest(
        self,
        ticket_id: str,
        seq: int,
        spec: StationSpec,
        input_digests: list[str],
        composed_digests: list[str] | None = None,
        phase: str = "none",
    ) -> str:
        """call_digest 唯一入口：六通道 + phase + seed，全部进指纹。"""
        impl, version = self._impl_for(spec)
        params = self.station_params.get(spec.name, {})
        params_digest = compute_digest(params)
        policy = self.policy_digest or compute_digest(self.policy_view)
        facts = dg.runtime_facts_digest(self.runtime_facts)
        seed = self._call_seed(ticket_id, spec, seq)
        return dg.call_digest(
            station=spec.name,
            station_revision=spec.revision,
            impl=impl,
            impl_version=version,
            phase=phase,
            params_digest=params_digest,
            input_digests=input_digests,
            policy=policy,
            runtime_facts=facts,
            composed_digests=composed_digests or [],
            seed=seed,
        )

    # ---- 依赖解析 -----------------------------------------------------
    def _resolve_inputs(
        self, spec: StationSpec, parcels: dict[str, Parcel],
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """按 spec.inputs 解析上游产物。

        返回 ``(lineage, input_digests)``：lineage 是 (工位, parcel_digest) 有序对，
        input_digests 是对应的 digest 列表（下游调用单的 inputs 通道）。
        上游缺失 → E3001；上游 stale → E3002。依赖判定用实际解析结果，不信任 lineage。
        """
        lineage: list[tuple[str, str]] = []
        for name in input_producers(spec):
            prior = parcels.get(name)
            if prior is None:
                raise FlowError(
                    E3001,
                    f"{spec.name} 缺上游产物: {name}",
                    path=f"station.{spec.name}",
                    hint="先跑上游工位；单步模式需先落盘上游产物",
                )
            if name in self.stale:
                raise FlowError(
                    E3002, f"{spec.name} 上游 {name} 已 stale，拒绝续跑",
                    path=f"station.{spec.name}.inputs.{name}",
                    hint="上游实现/口径变化后须重跑，不得在失效产物上继续",
                )
            prior.verify()
            lineage.append((name, prior.parcel_digest))
        return lineage, [d for _, d in lineage]

    # ---- 一次性事件：Reveal 前置 ---------------------------------------
    def _prepare_reveal(self, ticket_id: str, mode: str) -> None:
        """保留区可见性只由执行期事实决定，且 Reveal 只在整链触发一次。

        `holdout_visibility` 写在 `runtime_facts` 而不是 `params`：写进参数就等于
        允许把「未揭示」声明成「已揭示」（s0/04 §3.12）。单步没有整体执行，
        因此只能读已记录的结果——缺记录即 `E3006`，绝不自行揭示。
        """
        visibility = str(self.runtime_facts.get("holdout_visibility", "reserved"))
        if visibility not in {"reserved", "revealed"}:
            raise FlowError(
                E1004,
                f"runtime_facts.holdout_visibility 必须是 reserved/revealed，得到 {visibility!r}",
                path="runtime_facts.holdout_visibility",
            )
        self.runtime_facts["holdout_visibility"] = visibility
        if visibility == "reserved":
            return
        window = str(self.runtime_facts.get("holdout_window") or "")
        if not window:
            raise FlowError(
                E1004, "holdout_visibility=revealed 但缺 holdout_window",
                path="runtime_facts.holdout_window",
                hint="保留区必须有名有姓：没有窗口就无从记录「揭示了一次」",
            )
        if mode == "run":
            self.events.reveal(ticket_id, window)
        else:
            self.events.require_reveal(ticket_id, window)

    # ---- 主流程 -------------------------------------------------------
    def execute(self, ticket_id: str, *, mode: str = "run") -> ExecuteResult:
        """顺序跑八个工位（s0/05 §2）。

        `mode` 只改两件事（第 14/15 步）：**写入范围**与**是否触发一次性事件**。
        `run` 是唯一会触发事件的模式；`step` 写隔离目录 `steps/`、不动正式状态；
        `commit` 写正式目录并推进状态，但**同样不触发事件**——否则「单步提交」
        就是一条绕开整体执行的计数/发布后门（I7）。
        """
        if mode not in {"run", "step", "commit"}:
            raise FlowError(
                E1004, f"execute mode 必须是 run/step/commit: {mode!r}",
                path="execute.mode",
            )

        sm = StateMachine()
        sm.transition("running")
        parcels: dict[str, Parcel] = {}
        traces: list[dict[str, Any]] = []
        # Reveal 必须在 Boundary 之前（s0/05 §5）：保留区是否可见决定面板装不装
        # 那段数据。整链由 `run` 触发一次；单步只读记录，不自行揭示。
        self._prepare_reveal(ticket_id, mode)
        # 内存对象通道：工位能力返回的数据字段（Panel/Scope/ndarray）只在此流动，
        # 不进 Parcel.payload、不落盘、不进指纹（s0/04 §1.3）。
        objects: dict[str, dict[str, Any]] = {}
        # 结果性跳过的传播标记：一旦某个工位被判 skipped(reason)，其后所有工位
        # 都是 upstream_skipped（s0/05 §4.2 的 pending→skipped 转移）。
        skipped_reason: str | None = None
        skipped_at: str | None = None

        for seq, spec in enumerate(STATION_SPECS, start=1):
            if skipped_reason is not None:
                # 上游已结果性跳过：本工位与其下游都是 `upstream_skipped`，
                # 不再执行业务逻辑，也不再往下游传「半个对象」。
                trace = self._trace(seq, spec, "", None, "skipped", mode,
                                    inputs=[], skip_reason="upstream_skipped",
                                    seconds=0.0).to_dict()
                traces.append(trace)
                self._write_trace(trace)
                continue
            lineage, input_digests = self._resolve_inputs(spec, parcels)
            call_digest = self._call_digest(ticket_id, seq, spec, input_digests)
            started_at = self.fx.clock.wall()
            t0 = self.fx.clock.monotonic()

            try:
                invoked = self._from_cache_or_invoke(
                    ticket_id, seq, spec, lineage, objects, call_digest,
                )
                if invoked.skip_reason is not None:
                    skipped_reason = invoked.skip_reason
                    skipped_at = spec.name
                    trace = self._trace(
                        seq, spec, call_digest, None, "skipped", mode,
                        inputs=lineage, skip_reason=skipped_reason,
                        seconds=self.fx.clock.monotonic() - t0,
                    ).to_dict()
                    traces.append(trace)
                    self._write_trace(trace)
                    continue
                payload, produced = invoked.payload, invoked.objects
                parcel = self._parcel_for(
                    spec=spec, payload=payload, call_digest=call_digest,
                    lineage=lineage, origin=mode, ticket_id=ticket_id,
                    seq=seq, started_at=started_at, t0=t0,
                )
                # 顺序：payload → log → parcel.json（第 11–12 步）。
                # 落盘失败（E5001/E5004）同样进下面的 except → 写 failed trace。
                self._persist(parcel)
                self._cache_put(call_digest, parcel)
            except Exception as exc:
                # s0/05 §2.1：第 8 步之后任何失败都要留下 status=failed 的 trace，
                # 且不得留下半成品 parcel.json（写盘是原子 replace，且只有全部
                # 内容就绪才写它）。
                # 能力实现可能在契约闸门之外抛 NotImplementedError/ValueError
                # 等未预期异常（P1-10）：统一映射成 E3004 再走同一条 failed-trace
                # 路径，否则这类失败会完全没有 trace，事后无法还原。
                if isinstance(exc, FlowError):
                    err = exc
                else:
                    err = FlowError(
                        E3004,
                        f"{spec.name} 未预期异常: {type(exc).__name__}: {exc}",
                        path=f"station.{spec.name}",
                    )
                seconds = self.fx.clock.monotonic() - t0
                trace = self._trace(seq, spec, call_digest, None, "failed", mode,
                                    inputs=lineage, error_code=err.error.code,
                                    seconds=seconds).to_dict()
                traces.append(trace)
                self._write_trace(trace)
                if err is exc:
                    raise
                raise err from exc

            parcels[spec.name] = parcel
            objects[spec.name] = produced

            # 能力模式：把 Search 实测的试验计数注入执行期事实，供 Verdict 的
            # FDR/DSR/BH 使用。n_trials 是执行器（唯一有权计数者）的事实，不是
            # verdict.params 的旋钮（E1003），也不是 search 自己写账本。
            # dummy 路径不动：Search payload 无该键，runtime_facts 保持构造时的
            # 值，S0/S1/S2 的 call_digest 逐字节不变。
            if self.use_capabilities and spec.name == "Search" and isinstance(
                    parcel.payload, Mapping):
                val = parcel.payload.get("n_trials")
                if isinstance(val, (int, np.integer)) and not isinstance(val, bool):
                    self.runtime_facts["n_trials"] = int(val)

            trace = self._trace(seq, spec, call_digest, parcel.parcel_digest,
                                "done", mode, inputs=lineage,
                                seconds=parcel.seconds).to_dict()
            traces.append(trace)
            self._write_trace(trace)

            # 一次性事件：Budget 在 Search 落盘后；Publish 在 Certify 后（S5 落全局账本）
            if mode == "run":
                if spec.event == "Budget":
                    # Budget 的真值是 CandidateSet 去重后的实测候选数（含 fit 内
                    # 登记、按 candidate_id 去重）——由真实 search 能力放进产物
                    # payload 的 n_trials 通道。dummy 桩没有该键，保持 0（S1/S3-M6
                    # 之前的 determinism）。不得在这里另算：计数只有能力层一份真相。
                    n_trials = 0
                    search_payload = parcels["Search"].payload
                    if isinstance(search_payload, Mapping):
                        val = search_payload.get("n_trials")
                        if isinstance(val, (int, np.integer)) and not isinstance(val, bool):
                            n_trials = int(val)
                    self.events.budget(ticket_id, self._family(), n_trials)
                elif spec.event == "Publish":
                    # 发布只接受认证段的结论（AC27）。等级字段是 Certify 的
                    # 唯一产出等级标记；其余工位一律 in_sample，拿它发布就是把
                    # 「样本内诊断」说成「可交易证据」。
                    # 只在能力模式下校验：dummy 桩没有证据等级字段，S0/S1 的
                    # 事件语义不因真实链的门槛而改变。
                    grade = "out_of_sample"
                    if self.use_capabilities:
                        grade = ""
                        cert = parcels.get("Certify")
                        if cert is not None and isinstance(cert.payload, Mapping):
                            grade = str(cert.payload.get("evidence_grade", ""))
                    if grade != "out_of_sample":
                        raise FlowError(
                            E3006,
                            f"Certify 的 evidence_grade={grade!r}，不得发布",
                            path="parcel.Certify.evidence_grade",
                            hint="只有认证段（out_of_sample）的结论可发布",
                        )
                    self.events.publish(ticket_id, parcels["Certify"].parcel_digest)

        if skipped_reason is not None:
            # 整体执行**成功结束**，只是结论是「零晋升」：状态机记 skipped 并带
            # 枚举 reason（s0/05 §4.2）。退出码仍是 0——把它当 E3xxx 就是把
            # 结果误判成故障（AC52）。
            sm.transition("skipped", reason=skipped_reason)
        else:
            sm.transition("done")
        return ExecuteResult(
            ticket_id=ticket_id, parcels=parcels, traces=traces, state=sm.state,
            events=[{"kind": e.kind, "value": e.value} for e in _all_events(self.events)],
            skip_reason=skipped_reason, skipped_at=skipped_at,
        )

    # ---- 单步执行（S5） ------------------------------------------------
    def execute_station(
        self, ticket_id: str, station: str, *, mode: str = "step",
    ) -> ExecuteResult:
        """只跑一个工位：上游全部从**磁盘**取，产物按 `mode` 分流（s0/05 §6）。

        与 `execute()` 的区别只有输入来源与写入范围，调用单构造、指纹链、落盘
        顺序完全相同——单步不是另一条路径，是同一函数的窄化调用。

        * `step`（`--probe`）：产物写 `steps/`，**不动**正式状态、不写账本、
          不触发事件。四项不变是 AC7 的判据。
        * `commit`：产物写 `parcels/`，追加一条 `state.jsonl` 推进正式状态，
          **仍不触发事件**（I7）。`append_only` 工位直接拒绝——提交式重放要么
          重复计数、要么改写已发布结论。
        """
        if mode not in {"step", "commit"}:
            raise FlowError(
                E1004, f"单步 mode 只能是 step/commit: {mode!r}", path="execute.mode",
            )
        spec = BY_NAME.get(station)
        if spec is None:
            raise FlowError(
                E1001, f"未知工位 {station!r}", path="station.name",
                hint=f"可用工位: {sorted(BY_NAME)}",
            )
        if spec.standalone == "probe_only" and mode == "commit":
            # append_only = 全局账本写入且不可撤销（s0/05 §6）。允许 commit 就是
            # 允许把「重放一次」变成「再记一笔」。
            raise FlowError(
                E3006, f"{station} 是 append_only 工位，禁止 --commit",
                path=f"station.{station}.mode",
                hint="只允许只读重算（--probe）；提交会重复计数或改写已发布结论",
            )
        # 单步不得自行揭示保留区：缺 Reveal 记录即 E3006（s0/05 §6.1 第 5 条）。
        # 放在落盘检查之前：这是「能不能开始」的前置，不是「写到哪」的问题。
        self._prepare_reveal(ticket_id, mode)
        if self.writer is None:
            raise FlowError(
                E3001, f"{station} 单步执行需要已落盘的上游产物",
                path=f"station.{station}.inputs",
                hint="先整体 run 一次，让上游 Parcel 落盘",
            )

        seq = next(i for i, s in enumerate(STATION_SPECS, start=1) if s.name == spec.name)
        parcels = {name: self._load_committed(ticket_id, name)
                   for name in input_producers(spec)}
        lineage, _ = self._resolve_inputs(spec, parcels)
        call_digest = self._call_digest(ticket_id, seq, spec, [d for _, d in lineage])
        started_at = self.fx.clock.wall()
        t0 = self.fx.clock.monotonic()

        objects: dict[str, dict[str, Any]] = {}
        if self.use_capabilities and self.bridge is not None:
            # S5-M2：单步进程没有 run 的内存对象表。按顺序载入全部上游正式
            # 产物并重建对象（重推导 + payload_digest 对拍，见 bridge 的
            # rebuild_upstream），Portfolio/Evidence 的 prior_objects 也靠它。
            upstream_parcels: dict[str, Any] = {}
            for s in STATION_SPECS[: seq - 1]:
                upstream_parcels[s.name] = self._load_committed(ticket_id, s.name)
            impls: dict[str, str] = {}
            for s in STATION_SPECS[: seq - 1]:
                impl_name, impl_version = self._impl_for(s)
                impls[s.name] = f"{impl_name}@{impl_version}"
            objects = self.bridge.rebuild_upstream(
                upstream_parcels, impls=impls,
                policy_view=self.policy_view,
                runtime_facts=self.runtime_facts,
                station_params=self.station_params,
            )
            # n_trials 注入（与 run 模式同一条规则，executor.execute 的 Search
            # 分支）：Verdict 的 FDR/DSR 需要执行器计数，单步进程必须从冻结
            # 的 Search 产物里取同一个数，否则指纹与结论都不同源。
            search_payload = getattr(upstream_parcels.get("Search"), "payload", None)
            if isinstance(search_payload, Mapping):
                val = search_payload.get("n_trials")
                if isinstance(val, (int, np.integer)) and not isinstance(val, bool):
                    self.runtime_facts["n_trials"] = int(val)
        try:
            invoked = self._from_cache_or_invoke(
                ticket_id, seq, spec, lineage, objects, call_digest,
            )
            if invoked.skip_reason is not None:
                # 单步的「结果性跳过」同样是结论：不落 parcel、不推进状态，
                # 用 trace 的 skip_reason 把它记下来（不是失败）。
                trace = self._trace(
                    seq, spec, call_digest, None, "skipped", mode,
                    inputs=lineage, skip_reason=invoked.skip_reason,
                    seconds=self.fx.clock.monotonic() - t0,
                ).to_dict()
                self._write_trace(trace)
                return ExecuteResult(
                    ticket_id=ticket_id, parcels={}, traces=[trace], state="skipped",
                    skip_reason=invoked.skip_reason, skipped_at=spec.name,
                )
            payload, produced = invoked.payload, invoked.objects
            # 本工位重算出来的对象也要进表：单命令（backtest/certify）读的是
            # 「这次重算得到什么」，而它只在 invoked.objects 里，不在 payload。
            objects[spec.name] = dict(produced or {})
            parcel = self._parcel_for(
                spec=spec, payload=payload, call_digest=call_digest,
                lineage=lineage, origin=mode, ticket_id=ticket_id,
                seq=seq, started_at=started_at, t0=t0,
            )
            # 同一 payload 已在正式目录里 → 这次提交没有产生新产物。
            # 目录名由 payload_digest 决定，所以「同名」就等于「同内容」；此时
            # 再写一次只会撞上不可变写入的拒绝（log 里有时间戳，字节必然不同）。
            # 幂等短路比「允许覆盖」安全：覆盖会打开「改写历史产物」的口子。
            idempotent = (mode == "commit" and self.writer is not None and
                          (self.writer.layout.run_dir(ticket_id)
                           / parcel.base_relpath() / "parcel.json").exists())
            state_recovered = False
            if idempotent:
                # S5-M3 断电恢复：事实 = 产物落盘 + 状态推进两段。上一次
                # commit 若在两段之间断电，parcel 已在而 state.jsonl 缺这条。
                # 重放 commit 补上状态——append-only、同 digest 各一条，
                # 不是重复计数，也不是改写（产物内容从未变过）。
                state_path = (self.writer.layout.run_dir(ticket_id)
                              / "state.jsonl")
                present = False
                if state_path.exists():
                    for line in state_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("parcel_digest") == parcel.parcel_digest:
                            present = True
                            break
                if not present:
                    self.writer.append_jsonl("state.jsonl", {
                        "at": started_at.isoformat() if started_at else "",
                        "station": spec.name,
                        "parcel_digest": parcel.parcel_digest,
                        "payload_digest": parcel.payload_digest,
                        "origin": mode,
                    })
                    self.state_log.append({"station": spec.name,
                                           "parcel_digest": parcel.parcel_digest})
                    state_recovered = True
            else:
                self._persist(parcel)
                self._cache_put(call_digest, parcel)
        except Exception as exc:
            err = exc if isinstance(exc, FlowError) else FlowError(
                E3004, f"{spec.name} 未预期异常: {type(exc).__name__}: {exc}",
                path=f"station.{spec.name}",
            )
            trace = self._trace(
                seq, spec, call_digest, None, "failed", mode, inputs=lineage,
                error_code=err.error.code, seconds=self.fx.clock.monotonic() - t0,
            ).to_dict()
            self._write_trace(trace)
            if err is exc:
                raise
            raise err from exc

        trace = self._trace(seq, spec, call_digest, parcel.parcel_digest, "done",
                            mode, inputs=lineage, seconds=parcel.seconds).to_dict()
        self._write_trace(trace)
        if mode == "commit" and not idempotent:
            # 正式状态只追加、不覆盖：一份 digest 一个事实，改写历史就是改写结论。
            self.writer.append_jsonl("state.jsonl", {
                "at": started_at.isoformat() if started_at else "",
                "station": spec.name,
                "parcel_digest": parcel.parcel_digest,
                "payload_digest": parcel.payload_digest,
                "origin": mode,
            })
            self.state_log.append({"station": spec.name,
                                   "parcel_digest": parcel.parcel_digest})
        return ExecuteResult(
            ticket_id=ticket_id, parcels={spec.name: parcel}, traces=[trace],
            state="running" if mode == "step" else "committed",
            events=[], idempotent=idempotent, state_recovered=state_recovered,
            objects=objects,
        )

    def _load_committed(self, ticket_id: str, station: str) -> Parcel:
        """从磁盘恢复一个**正式**产物并校验指纹（第 3 步：不信 parcel.json）。

        只读 `parcels/`，不读 `steps/`：probe 的产物是观察，不能当作下游输入，
        否则一次观察就会悄悄改变正式链路的输入。
        """
        if self.writer is None:  # pragma: no cover - 由调用方保证
            raise FlowError(E3001, "单步执行需要落盘上下文", path="executor.writer")
        return load_committed_parcel(self.writer.layout, ticket_id, station)


    # ---- 落盘 / 辅助 --------------------------------------------------
    def _family(self) -> str:
        fam = self.policy_view.get("family")
        return str(fam) if fam else str(implementation_defaults("cli")["family"])

    def _parcel_for(self, *, spec: StationSpec, payload, call_digest, lineage,
                    origin, ticket_id, seq, started_at, t0) -> Parcel:
        """构造并自检一个工位产物。`execute` 与 `execute_station` 共用这一份。

        两处曾各抄一遍：12 个字段的构造、「产物为空即 `E3003`」的判据、以及
        `verify()`。抄两份的代价不在行数——判据只改一处时，另一条路径会继续
        接受空产物、继续跳过 `payload_digest` 的自检，而两条路径的产物长得
        一模一样，谁都不会报错。
        """
        if payload is None or not isinstance(payload, dict) or not payload:
            raise FlowError(
                E3003, f"{spec.name} 产物为空（静默降级）",
                path=f"parcel.{spec.name}",
            )
        payload_digest = self.fx.digest.of(payload)
        impl_name, impl_version = self._impl_for(spec)
        parcel = Parcel(
            station=spec.name,
            payload=payload,
            parcel_digest=dg.parcel_digest(call_digest, payload_digest),
            call_digest=call_digest,
            payload_digest=payload_digest,
            lineage=lineage,
            station_revision=spec.revision,
            impl=f"{impl_name}@{impl_version}",
            origin=origin,
            ticket_id=ticket_id,
            seq=seq,
            started_at=started_at,
            seconds=self.fx.clock.monotonic() - t0,
        )
        parcel.verify()
        return parcel

    def _persist(self, parcel: Parcel) -> None:
        """落 payload/log/parcel.json。writer 为空或只读重算时跳过。"""
        if not self.persist or self.writer is None:
            return
        base = parcel.base_relpath()
        self.writer.write_bytes(f"{base}/payload.json", parcel.payload_bytes())
        self.writer.write_bytes(f"{base}/log.jsonl", parcel.log_bytes())
        record = parcel.to_contract()
        self.writer.write_bytes(
            f"{base}/parcel.json",
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True,
                       default=str).encode("utf-8"),
        )

    def _write_trace(self, trace: dict[str, Any]) -> None:
        """trace 是只追加的执行事实；失败也必须有一条（s0/02 §3）。"""
        if self.persist and self.writer is not None:
            self.writer.append_jsonl("trace.jsonl", trace)

    def _trace(self, seq, spec: StationSpec, call_digest, parcel_digest,
               status, origin, *, inputs, error_code=None,
               seconds: float = 0.0, skip_reason: str | None = None) -> TraceRecord:
        impl, version = self._impl_for(spec)
        if getattr(self, "_cache_hit_last", False):
            origin = "cache"
        record = TraceRecord(
            seq=seq, station=spec.name, station_revision=spec.revision,
            impl=impl, impl_version=version,
            params_digest=self.fx.digest.of(self.station_params.get(spec.name, {})),
            inputs=[(name, d) for name, d in inputs],
            call_digest=call_digest, parcel_digest=parcel_digest,
            status=status, origin=origin, seconds=seconds, error_code=error_code,
            skip_reason=skip_reason,
        )
        try:
            # 单步只跑一个工位，seq 不从 1 起——TraceLog 要求连续，这时跳过
            # 登记；主循环每条都进，落盘仍走原来那条路。
            self.fx.trace.append(record)
        except ValueError:
            pass
        return record


def _check_upstream(station: str, *, prior: str, parcels: dict[str, Parcel]) -> None:
    """S0 兼容的线性依赖检查纯函数（O2 直接测这个分支）。

    真实依赖图走 `Executor._resolve_inputs`（按 spec.inputs）；本函数保留给
    S0 验收用例与单工位调用方，二者共用同一 E3001 语义。
    """
    if prior not in parcels:
        raise FlowError(
            E3001,
            f"{station} 缺上游产物: {prior}",
            path=f"station.{station}",
            hint="先跑上游工位",
        )


def _all_events(ledger: EventLedger) -> list[Any]:
    return list(ledger._records.values())


def load_committed_parcel(layout, ticket_id: str, station: str) -> Parcel:
    """从 ticket 的 `parcels/` 读一个正式产物并逐项校验（模块级，供 CLI 复用）。

    与 `Executor._load_committed` 同源：CLI 的单命令（evaluate/backtest/certify）
    没有执行器实例，但必须用**同一套**信任规则读产物——复制一份解析逻辑就等着
    两份规则分叉。只读 `parcels/`，不读 `steps/`（probe 产物是观察，不是输入）。
    """
    from v5.contracts.types import Parcel as ContractParcel

    root = layout.run_dir(ticket_id)
    found = sorted(root.glob(f"parcels/*-{station}-*/parcel.json"))
    if not found:
        raise FlowError(
            E3001, f"{station} 没有已落盘的正式产物",
            path=f"station.{station}",
            hint="先整体 run；probe 产物在 steps/ 下，不作为下游输入",
        )
    record_path = found[-1]
    base = record_path.parent
    data = json.loads(record_path.read_text(encoding="utf-8"))
    payload_path = root / str(data["payload_ref"]["relpath"])
    if not payload_path.exists():
        raise FlowError(
            E3002, f"{station} 的 payload 缺失（重建失败）",
            path=f"station.{station}.payload",
            hint="产物引用必须可解析；缺文件不得降级为空产物",
        )
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload_digest = compute_digest(payload)
    if payload_digest != str(data["payload_digest"]):
        raise FlowError(
            E3002, f"{station} 的 payload 指纹与声明不符（内容被改写）",
            path=f"station.{station}.payload",
        )
    contract = ContractParcel.from_dict(data)
    seq = int(base.name.split("-", 1)[0])
    parcel = Parcel(
        station=station, payload=payload,
        parcel_digest=str(contract.digest), call_digest=str(contract.call_digest),
        payload_digest=payload_digest,
        lineage=[(name, str(d)) for name, d in contract.lineage],
        station_revision=contract.station_revision,
        impl=f"{contract.impl}@{contract.impl_version}",
        origin="run", ticket_id=ticket_id, seq=seq,
        started_at=contract.started_at, seconds=float(contract.seconds),
    )
    parcel.verify()
    return parcel
