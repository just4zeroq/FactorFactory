"""七类核心结构（S0 契约层）：`Digest` / `Ref` / `Pin` / `Evidence` / `Parcel` /
`Ticket` / `Blueprint`。

这七类是全系统唯一的类型字典。任何一层都不得在这里之外定义第二套"差不多"
的结构——两套结构并存时，"同一个概念在不同地方含义不同"这类缺陷无法被任何
检查点发现，只会在数字对不上时才暴露。

**`Call` 不在这七类里**：它带一条只读载荷通道（`InputRef.data`），装的是面板
数组这类不可序列化的东西，与下面第 2、3 条冲突。所以它的唯一定义在能力层
`v5/capability/runtime.py`，契约层只持久化它的结果（`Parcel.call_digest`）。
曾经这里也有一份纯数据版的 `Call`，生产代码一处都不构造——见 §3.4 的说明。

三条通用规则：

1. **值对象不可变**。修改一律产出新实例，旧实例不失效、不删除。
2. **不存活对象**。字段不得是模型实例、连接句柄、生成器、内存视图、文件句柄。
3. **可序列化即可传递**。不能无损写进 JSON 的字段不允许出现在跨层结构里。

唯一例外是 ``Ticket``：它有两个可变字段（``status`` / ``archived_at``），其余
字段一经创建不可变，改写即 ``E2005``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .digest import assert_relative_path
from .errors import (
    E1004,
    E1007,
    E2003,
    E2004,
    E2005,
    FlowError,
)

# --------------------------------------------------------------------------
# 校验辅助
# --------------------------------------------------------------------------


def _parse_dt(value: Any, field_path: str) -> datetime:
    """接受 datetime 或 RFC3339 字符串，一律归一为 UTC。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise FlowError(
                E1004,
                f"{field_path} 不是 RFC3339 时间: {value!r}",
                path=field_path,
            ) from exc
    else:
        raise FlowError(
            E1004,
            f"{field_path} 必须是 datetime 或 RFC3339 字符串",
            path=field_path,
        )
    if parsed.tzinfo is None:
        raise FlowError(
            E1004,
            f"{field_path} 必须带时区",
            hint="时间一律 UTC，显式 Z；naive datetime 会被误当本地时间",
            path=field_path,
        )
    return parsed.astimezone(timezone.utc)


def _require_keys(kind: str, data: Mapping[str, Any], required: Sequence[str]) -> None:
    missing = [k for k in required if k not in data]
    if missing:
        raise FlowError(
            E2003,
            f"{kind} 缺必填字段 {missing}",
            hint="必填字段不允许补默认值：补默认会让'用户没写'和'用户写了默认值'不可区分",
            context={"missing": missing},
        )


def _reject_unknown(kind: str, data: Mapping[str, Any], allowed: Sequence[str]) -> None:
    extra = [k for k in data if k not in allowed]
    if extra:
        raise FlowError(
            E1004,
            f"{kind} 含未定义字段 {extra}",
            hint=f"允许的字段为 {sorted(allowed)}",
            context={"unknown": extra},
        )


def _enum(kind: str, name: str, value: Any, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise FlowError(
            E1004,
            f"{kind}.{name} = {value!r} 不在 {sorted(allowed)} 内",
            path=f"{kind}.{name}",
            context={"allowed": list(allowed)},
        )
    return str(value)


def _plain(value: Any) -> Any:
    """把值对象转成可 JSON 序列化的普通结构。"""
    hook = getattr(value, "to_dict", None)
    if hook is not None and not isinstance(value, Mapping):
        return hook()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


_HEX64 = re.compile(r"^[0-9a-f]{64}$")

DIGEST_ALGOS = ("sha256",)
REF_KINDS = ("parcel", "supply", "derived", "register", "fitted")
CALL_PHASES = ("none", "fit", "generate")
PARCEL_ORIGINS = ("run", "step", "commit", "resume", "cache")
PARCEL_STATUSES = ("done", "failed", "skipped")
TICKET_STATUSES = ("pending", "running", "done", "failed", "stale")
EVIDENCE_KINDS = ("observation", "diagnostic", "count")
TICKET_MUTABLE_FIELDS = ("status", "archived_at")


# --------------------------------------------------------------------------
# 3.1 Digest
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Digest:
    """内容指纹。序列化形态为 ``"sha256:<hex>"``，不接受裸 hex 或大写。"""

    algo: str
    hex: str

    def __post_init__(self) -> None:
        if self.algo not in DIGEST_ALGOS:
            raise FlowError(
                E1004,
                f"未知摘要算法 {self.algo!r}",
                hint=f"当前唯一支持 {list(DIGEST_ALGOS)}",
                path="Digest.algo",
            )
        if not isinstance(self.hex, str) or not _HEX64.match(self.hex):
            raise FlowError(
                E1004,
                "Digest.hex 必须是 64 位小写十六进制",
                hint="不接受大写或裸 hex；用 Digest.parse('sha256:...') 构造",
                path="Digest.hex",
            )

    @classmethod
    def parse(cls, text: str) -> "Digest":
        if not isinstance(text, str) or ":" not in text:
            raise FlowError(
                E1004,
                f"指纹必须带算法前缀: {text!r}",
                hint="形如 sha256:<64 位小写十六进制>",
            )
        algo, _, hex_part = text.partition(":")
        return cls(algo=algo, hex=hex_part)

    def __str__(self) -> str:
        return f"{self.algo}:{self.hex}"

    def __flow_canonical__(self) -> str:
        return str(self)

    def to_dict(self) -> str:
        return str(self)


# --------------------------------------------------------------------------
# 3.2 Ref
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ref:
    """内容引用：只描述"在哪、是什么、哪个版本"，**不携带内容**。

    任何持有 `Ref` 的层在解析时必须重新校验 `digest`，不信任路径本身。
    """

    kind: str
    id: str
    digest: Digest
    relpath: str
    schema_version: str

    def __post_init__(self) -> None:
        _enum("Ref", "kind", self.kind, REF_KINDS)
        if not isinstance(self.id, str) or not self.id:
            raise FlowError(E1004, "Ref.id 必须是非空字符串", path="Ref.id")
        if not isinstance(self.digest, Digest):
            raise FlowError(
                E1004,
                f"Ref.digest 必须是 Digest，得到 {type(self.digest).__name__}",
                hint="用 Digest.parse('sha256:...')，不要传裸字符串",
                path="Ref.digest",
            )
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise FlowError(E1004, "Ref.schema_version 必须是非空字符串", path="Ref.schema_version")
        # 路径在构造期归一（`\` → `/`）并校验，让非法路径当场失败，
        # 也让指纹里不会留下平台相关的分隔符。
        object.__setattr__(self, "relpath", assert_relative_path(self.relpath))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Ref":
        _require_keys("Ref", data, ("kind", "id", "digest", "relpath", "schema_version"))
        _reject_unknown("Ref", data, ("kind", "id", "digest", "relpath", "schema_version"))
        return cls(
            kind=data["kind"],
            id=data["id"],
            digest=Digest.parse(data["digest"]) if isinstance(data["digest"], str) else data["digest"],
            relpath=data["relpath"],
            schema_version=data["schema_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "digest": str(self.digest),
            "relpath": self.relpath,
            "schema_version": self.schema_version,
        }


# --------------------------------------------------------------------------
# 3.3 Pin
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pin:
    """供给版本引用。`end_closed=false` 的版本可以存在于 Depot，但不得被别名指向。"""

    dataset: str
    version: str
    checksum: Digest
    coverage: Mapping[str, Any]
    symbols_digest: Digest
    end_closed: bool
    schema_version: str
    acquired_at: datetime
    gate_a: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, str) or not self.dataset:
            raise FlowError(E1004, "Pin.dataset 必须是非空字符串", path="Pin.dataset")
        if not isinstance(self.version, str) or not re.match(r"^[0-9a-f]{16}$", self.version):
            raise FlowError(
                E1004,
                "Pin.version 必须是 16 位小写十六进制",
                hint="版本号是内容摘要短标识，不是自增序号或时间戳",
                path="Pin.version",
            )
        for name in ("checksum", "symbols_digest"):
            if not isinstance(getattr(self, name), Digest):
                raise FlowError(E1004, f"Pin.{name} 必须是 Digest", path=f"Pin.{name}")
        if not isinstance(self.end_closed, bool):
            raise FlowError(E1004, "Pin.end_closed 必须是 bool", path="Pin.end_closed")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            # 宽松接受 int 会让 manifest 里的 1 在指纹投影里静默变成 "1"，
            # 两种写法被当成同一个版本（review P2-7）。
            raise FlowError(
                E1004, "Pin.schema_version 必须是非空字符串",
                path="Pin.schema_version",
            )
        for name in ("coverage", "gate_a"):
            if not isinstance(getattr(self, name), Mapping):
                raise FlowError(E1004, f"Pin.{name} 必须是映射", path=f"Pin.{name}")
        _require_keys("Pin.coverage", self.coverage, ("start", "end"))
        _require_keys("Pin.gate_a", self.gate_a, ("passed", "reason_code"))
        start, end = self.coverage["start"], self.coverage["end"]
        if not (isinstance(start, datetime) and isinstance(end, datetime)):
            raise FlowError(
                E1004,
                "Pin.coverage.start/end 必须是带时区的 datetime",
                hint="覆盖区间是闭区间，且 start < end",
                path="Pin.coverage",
            )
        if start >= end:
            raise FlowError(
                E1004,
                f"Pin.coverage 区间非法: start={start} >= end={end}",
                path="Pin.coverage",
            )
        if not isinstance(self.acquired_at, datetime):
            raise FlowError(
                E1004,
                "Pin.acquired_at 必须是 datetime",
                hint="从 JSON 读入时用 Pin.from_dict，它会解析 RFC3339 字符串",
                path="Pin.acquired_at",
            )
        if self.acquired_at.tzinfo is None:
            raise FlowError(
                E1004,
                "Pin.acquired_at 必须带时区",
                hint="采集时间一律 UTC；naive datetime 会被误当本地时间",
                path="Pin.acquired_at",
            )

    def digest_view(self) -> Mapping[str, Any]:
        """参与 `supply_digest` 的投影。**排除 `acquired_at`**。

        `acquired_at` 是采集完成时间，属于"会随时间变化、因此不得进任何指纹"
        的硬性排除项。重采一次逐字节相同的数据不该让全部下游产物失效。
        """
        return {
            "dataset": self.dataset,
            "version": self.version,
            "checksum": str(self.checksum),
            "coverage": {
                "start": self.coverage["start"],
                "end": self.coverage["end"],
            },
            "symbols_digest": str(self.symbols_digest),
            "end_closed": self.end_closed,
            "schema_version": self.schema_version,
            "gate_a": {
                "passed": self.gate_a["passed"],
                "reason_code": self.gate_a["reason_code"],
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Pin":
        keys = (
            "dataset",
            "version",
            "checksum",
            "coverage",
            "symbols_digest",
            "end_closed",
            "schema_version",
            "acquired_at",
            "gate_a",
        )
        _require_keys("Pin", data, keys)
        _reject_unknown("Pin", data, keys)
        cov = data["coverage"]
        if not isinstance(cov, Mapping) or set(cov) != {"start", "end"}:
            raise FlowError(E1004, "Pin.coverage 必须含 start/end", path="Pin.coverage")
        parsed_coverage = {
            "start": _parse_dt(cov["start"], "Pin.coverage.start"),
            "end": _parse_dt(cov["end"], "Pin.coverage.end"),
        }
        return cls(
            dataset=data["dataset"],
            version=data["version"],
            checksum=Digest.parse(data["checksum"]),
            coverage=parsed_coverage,
            symbols_digest=Digest.parse(data["symbols_digest"]),
            end_closed=data["end_closed"],
            schema_version=data["schema_version"],
            acquired_at=_parse_dt(data["acquired_at"], "Pin.acquired_at"),
            gate_a=data["gate_a"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "checksum": str(self.checksum),
            "coverage": {
                "start": _plain(self.coverage["start"]),
                "end": _plain(self.coverage["end"]),
            },
            "symbols_digest": str(self.symbols_digest),
            "end_closed": self.end_closed,
            "schema_version": self.schema_version,
            "acquired_at": _plain(self.acquired_at),
            "gate_a": dict(self.gate_a),
        }


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

EVIDENCE_FIELDS = ("kind", "name", "value", "unit", "source", "method", "uncertainty")

#: `Evidence.name` 里出现的纯结论词。**不含** `score` / `rank` / `weight`：
#: 那三个同时是合法观察量名（`rank_ic`、`raw_score`），按子串拒绝会误伤真正的
#: 事实。字段名层面由 `EVIDENCE_FIELDS` 白名单兜底，那里已全部禁止。
_FORBIDDEN_EVIDENCE_TOKENS = (
    "pass",
    "verdict",
    "significant",
    "approve",
    "reject",
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """只允许事实。结论只能由 Sentinel 写入 `VerdictParcel`。

    字段白名单在构造期校验（`E2004`）。这一条比"约定不要写结论"可靠：
    约定拦不住 forward return 那种名字叫 `ret` 的结论字段，白名单能拦。
    """

    kind: str
    name: str
    value: Any
    unit: str | None
    source: str
    method: str
    uncertainty: Any = None

    def __post_init__(self) -> None:
        _enum("Evidence", "kind", self.kind, EVIDENCE_KINDS)
        if not isinstance(self.name, str) or not self.name:
            raise FlowError(E2004, "Evidence.name 必须是非空字符串", path="Evidence.name")
        tokens = {t for t in re.split(r"[^a-z0-9]+", self.name.lower()) if t}
        hit = sorted(tokens & set(_FORBIDDEN_EVIDENCE_TOKENS))
        if hit:
            raise FlowError(
                E2004,
                f"Evidence.name = {self.name!r} 含结论性词 {hit}",
                hint="Evidence 只允许事实（数值、计数、诊断）；结论写入 VerdictParcel",
                path=f"Evidence.{self.name}",
            )
        for name in ("source", "method"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise FlowError(E2004, f"Evidence.{name} 必须是非空字符串", path=f"Evidence.{name}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        extra = [k for k in data if k not in EVIDENCE_FIELDS]
        if extra:
            raise FlowError(
                E2004,
                f"Evidence 含禁用字段 {extra}",
                hint=f"允许的事实字段只有 {list(EVIDENCE_FIELDS)}；结论写入 VerdictParcel",
                context={"forbidden": extra},
            )
        _require_keys("Evidence", data, ("kind", "name", "value", "source", "method"))
        return cls(
            kind=data["kind"],
            name=data["name"],
            value=data["value"],
            unit=data.get("unit"),
            source=data["source"],
            method=data["method"],
            uncertainty=data.get("uncertainty"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: _plain(getattr(self, k)) for k in EVIDENCE_FIELDS}


# --------------------------------------------------------------------------
# 3.4 Call —— 定义在能力层（`v5/capability/runtime.py`）
# --------------------------------------------------------------------------
#
# 这里曾有一份 16 字段的 `Call`（带 `params_digest` / `policy_digest` /
# `runtime_facts_digest` 三个存储字段、`inputs: list[Ref]`），生产代码一处都不
# 构造它——它是一张与真实类型对不上、也没有任何地方消费的规格表，而且校验强度
# 比真实类型强（那一份拦 phase 枚举，这一份不拦），于是"哪个才算数"只能靠猜。
#
# 合并后的唯一形态在 `capability/runtime.py`：`inputs` 是 `InputRef`（多了
# `data` 只读载荷通道），三个子摘要降为派生属性，`call_digest` 存字段。契约层
# 只持久化它的**结果**——`Parcel.call_digest` 与 `Parcel` 的 impl/station 字段。
# `CALL_PHASES` 仍留在本模块：它是契约枚举，能力层 import 它做校验。

# --------------------------------------------------------------------------
# 3.5 Parcel
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Parcel:
    """不可变落盘产物。`status=failed` 必须有 `error`，`skipped` 必须有 `skip_reason`。"""

    ticket_id: str
    station: str
    station_revision: str
    impl: str
    impl_version: str
    call_digest: Digest
    payload_ref: Ref
    payload_digest: Digest
    digest: Digest
    lineage: Sequence[tuple[str, Digest]]
    evidence: Sequence[Evidence]
    origin: str
    status: str
    skip_reason: str | None
    error: Any
    started_at: datetime
    seconds: float
    log_ref: Ref

    def __post_init__(self) -> None:
        for name in ("ticket_id", "station", "station_revision", "impl", "impl_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise FlowError(E1004, f"Parcel.{name} 必须是非空字符串", path=f"Parcel.{name}")
        _enum("Parcel", "origin", self.origin, PARCEL_ORIGINS)
        _enum("Parcel", "status", self.status, PARCEL_STATUSES)
        for name in ("call_digest", "payload_digest", "digest"):
            if not isinstance(getattr(self, name), Digest):
                raise FlowError(E1004, f"Parcel.{name} 必须是 Digest", path=f"Parcel.{name}")
        for name in ("payload_ref", "log_ref"):
            if not isinstance(getattr(self, name), Ref):
                raise FlowError(E1004, f"Parcel.{name} 必须是 Ref", path=f"Parcel.{name}")
        if self.status == "skipped" and not self.skip_reason:
            raise FlowError(
                E2003,
                "status=skipped 时必须给出 skip_reason",
                hint="跳过必须有可读理由，否则无法区分'不需要算'和'忘了算'",
                path="Parcel.skip_reason",
            )
        if self.status == "failed" and self.error is None:
            raise FlowError(
                E2003,
                "status=failed 时必须给出 error",
                hint="失败必须携带错误码与信息，不能只留一个状态",
                path="Parcel.error",
            )
        for idx, item in enumerate(self.evidence):
            if not isinstance(item, Evidence):
                raise FlowError(E1004, f"Parcel.evidence[{idx}] 必须是 Evidence", path="Parcel.evidence")
        for idx, entry in enumerate(self.lineage):
            if not (isinstance(entry, tuple) and len(entry) == 2):
                raise FlowError(E1004, f"Parcel.lineage[{idx}] 必须是 (station, Digest)", path="Parcel.lineage")
            if not isinstance(entry[1], Digest):
                raise FlowError(E1004, f"Parcel.lineage[{idx}][1] 必须是 Digest", path="Parcel.lineage")
        if self.seconds < 0:
            raise FlowError(E1004, "Parcel.seconds 不得为负", path="Parcel.seconds")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Parcel":
        keys = ('ticket_id', 'station', 'station_revision', 'impl', 'impl_version', 'call_digest', 'payload_ref', 'payload_digest', 'digest', 'lineage', 'evidence', 'origin', 'status', 'skip_reason', 'error', 'started_at', 'seconds', 'log_ref')
        _require_keys("Parcel", data, keys)
        _reject_unknown("Parcel", data, keys)
        def _dg(v: Any) -> Digest:
            return Digest.parse(v) if isinstance(v, str) else v
        return cls(
            ticket_id=data["ticket_id"], station=data["station"],
            station_revision=data["station_revision"], impl=data["impl"],
            impl_version=data["impl_version"], call_digest=_dg(data["call_digest"]),
            payload_ref=Ref.from_dict(data["payload_ref"]),
            payload_digest=_dg(data["payload_digest"]), digest=_dg(data["digest"]),
            lineage=[(name, _dg(d)) for name, d in data["lineage"]],
            evidence=[Evidence.from_dict(e) for e in data["evidence"]],
            origin=data["origin"], status=data["status"], skip_reason=data["skip_reason"],
            error=data["error"], started_at=_parse_dt(data["started_at"], "Parcel.started_at"),
            seconds=data["seconds"], log_ref=Ref.from_dict(data["log_ref"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "station": self.station,
            "station_revision": self.station_revision,
            "impl": self.impl,
            "impl_version": self.impl_version,
            "call_digest": str(self.call_digest),
            "payload_ref": self.payload_ref.to_dict(),
            "payload_digest": str(self.payload_digest),
            "digest": str(self.digest),
            "lineage": [[name, str(d)] for name, d in self.lineage],
            "evidence": [e.to_dict() for e in self.evidence],
            "origin": self.origin,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "error": _plain(self.error) if self.error is not None else None,
            "started_at": _plain(self.started_at),
            "seconds": self.seconds,
            "log_ref": self.log_ref.to_dict(),
        }


# --------------------------------------------------------------------------
# 3.6 Ticket
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Ticket:
    """一次执行的状态主体。

    **可变字段只有 `status` 与 `archived_at`**。用"白名单两个可变字段"表述，
    比"前 N 个不可变"可靠——后者在增删字段时必然数错。
    """

    ticket_id: str
    plan_digest: Digest
    blueprint_digest: Digest
    policy_digest: Digest
    caliber_digest: Digest
    baseline_version: str
    family: str
    family_declared_at: datetime
    master_seed: int
    pins: Sequence[Pin]
    domain_digest: Digest
    status: str
    created_at: datetime
    environment_ref: Ref
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        if not re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$", self.ticket_id):
            raise FlowError(
                E1004,
                f"ticket_id 格式非法: {self.ticket_id!r}",
                hint="<UTC 紧凑时间>-<plan_digest 前 8 位>，例如 20261116T053000Z-1a2b3c4d",
                path="Ticket.ticket_id",
            )
        for name in ("plan_digest", "blueprint_digest", "policy_digest", "caliber_digest", "domain_digest"):
            if not isinstance(getattr(self, name), Digest):
                raise FlowError(E1004, f"Ticket.{name} 必须是 Digest", path=f"Ticket.{name}")
        _enum("Ticket", "status", self.status, TICKET_STATUSES)
        if not isinstance(self.master_seed, int) or isinstance(self.master_seed, bool):
            raise FlowError(E1004, "Ticket.master_seed 必须是 int", path="Ticket.master_seed")
        for idx, pin in enumerate(self.pins):
            if not isinstance(pin, Pin):
                raise FlowError(E1004, f"Ticket.pins[{idx}] 必须是 Pin", path="Ticket.pins")
        if not isinstance(self.environment_ref, Ref):
            raise FlowError(E1004, "Ticket.environment_ref 必须是 Ref", path="Ticket.environment_ref")
        if self.family_declared_at.tzinfo is None or self.created_at.tzinfo is None:
            raise FlowError(
                E1004,
                "Ticket 时间字段必须带时区",
                hint="预注册时间与创建时间一律 UTC",
                path="Ticket.created_at",
            )

    def __setattr__(self, name: str, value: Any) -> None:
        # dataclass 的 __init__ 也走这里；首次赋值（属性尚不存在）一律放行。
        already_set = name in type(self).__slots__ and hasattr(self, name)
        if already_set and name not in TICKET_MUTABLE_FIELDS:
            raise FlowError(
                E2005,
                f"Ticket.{name} 不可变，运行期不得改写",
                hint=f"可变字段只有 {list(TICKET_MUTABLE_FIELDS)}；其余字段改了就不是同一次执行",
                path=f"Ticket.{name}",
                retryable=False,
            )
        object.__setattr__(self, name, value)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Ticket":
        keys = ('ticket_id', 'plan_digest', 'blueprint_digest', 'policy_digest', 'caliber_digest', 'baseline_version', 'family', 'family_declared_at', 'master_seed', 'pins', 'domain_digest', 'status', 'created_at', 'environment_ref', 'archived_at')
        _require_keys("Ticket", data, keys)
        _reject_unknown("Ticket", data, keys)
        def _dg(v: Any) -> Digest:
            return Digest.parse(v) if isinstance(v, str) else v
        return cls(
            ticket_id=data["ticket_id"], plan_digest=_dg(data["plan_digest"]),
            blueprint_digest=_dg(data["blueprint_digest"]), policy_digest=_dg(data["policy_digest"]),
            caliber_digest=_dg(data["caliber_digest"]), baseline_version=data["baseline_version"],
            family=data["family"],
            family_declared_at=_parse_dt(data["family_declared_at"], "Ticket.family_declared_at"),
            master_seed=data["master_seed"], pins=[Pin.from_dict(p) for p in data["pins"]],
            domain_digest=_dg(data["domain_digest"]), status=data["status"],
            created_at=_parse_dt(data["created_at"], "Ticket.created_at"),
            environment_ref=Ref.from_dict(data["environment_ref"]),
            archived_at=_parse_dt(data["archived_at"], "Ticket.archived_at") if data["archived_at"] is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "plan_digest": str(self.plan_digest),
            "blueprint_digest": str(self.blueprint_digest),
            "policy_digest": str(self.policy_digest),
            "caliber_digest": str(self.caliber_digest),
            "baseline_version": self.baseline_version,
            "family": self.family,
            "family_declared_at": _plain(self.family_declared_at),
            "master_seed": self.master_seed,
            "pins": [p.to_dict() for p in self.pins],
            "domain_digest": str(self.domain_digest),
            "status": self.status,
            "created_at": _plain(self.created_at),
            "environment_ref": self.environment_ref.to_dict(),
            "archived_at": _plain(self.archived_at) if self.archived_at is not None else None,
        }


# --------------------------------------------------------------------------
# 3.7 Blueprint
# --------------------------------------------------------------------------

BLUEPRINT_KEYS = (
    "values",
    "origins",
    "resolved",
    "caliber",
    "supply_pins",
    "domain",
    "family_declaration",
    "station_order",
    "subcall_graph",
    "digests",
    "check_results",
    "compiled_at",
)


@dataclass(frozen=True, slots=True)
class Blueprint:
    """冻结方案。`origins` 必须覆盖 `values` 的每一个叶子，缺一个即 `E1007`。"""

    values: Mapping[str, Any]
    origins: Mapping[str, str]
    resolved: Mapping[str, Any]
    caliber: Mapping[str, Any]
    supply_pins: Sequence[Pin]
    domain: Mapping[str, Any]
    family_declaration: Mapping[str, Any]
    station_order: Sequence[tuple[str, str]]
    subcall_graph: Mapping[str, Sequence[str]]
    digests: Mapping[str, Any]
    check_results: Sequence[Mapping[str, Any]]
    compiled_at: datetime

    def __post_init__(self) -> None:
        _require_keys("Blueprint.family_declaration", self.family_declaration, ("family", "declared_at"))
        _require_keys(
            "Blueprint.digests",
            self.digests,
            ("plan", "blueprint", "policy", "domain", "supply", "caliber"),
        )
        leaves = leaf_paths(self.values)
        missing = sorted(leaves - set(self.origins))
        if missing:
            # 缺叶子的后果是 `flow explain` 出现"这个值从哪来"的盲区，
            # 所以这不是警告而是硬错误。
            raise FlowError(
                E1007,
                f"origins 未覆盖 {len(missing)} 个叶子",
                hint="每个叶子都必须标出来源 baseline@vN / plan / override",
                context={"missing": missing[:20], "missing_count": len(missing)},
            )
        for name, revision in self.station_order:
            if not isinstance(name, str) or not isinstance(revision, str):
                raise FlowError(E1004, "station_order 必须是 (工位名, revision) 对", path="Blueprint.station_order")
        if not isinstance(self.compiled_at, datetime) or self.compiled_at.tzinfo is None:
            raise FlowError(
                E1004,
                "Blueprint.compiled_at 必须是带时区的 datetime",
                hint="编译时间仅记录，不进指纹；但仍须带时区以免日志排序错乱",
                path="Blueprint.compiled_at",
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Blueprint":
        keys = ('values', 'origins', 'resolved', 'caliber', 'supply_pins', 'domain', 'family_declaration', 'station_order', 'subcall_graph', 'digests', 'check_results', 'compiled_at')
        _require_keys("Blueprint", data, keys)
        _reject_unknown("Blueprint", data, keys)
        return cls(
            values=dict(data["values"]), origins=dict(data["origins"]),
            resolved=dict(data["resolved"]), caliber=dict(data["caliber"]),
            supply_pins=[Pin.from_dict(p) for p in data["supply_pins"]],
            domain=dict(data["domain"]), family_declaration=dict(data["family_declaration"]),
            station_order=[tuple(pair) for pair in data["station_order"]],
            subcall_graph={k: list(v) for k, v in data["subcall_graph"].items()},
            digests={k: Digest.parse(v) if isinstance(v, str) else v for k, v in data["digests"].items()},
            check_results=list(data["check_results"]),
            compiled_at=_parse_dt(data["compiled_at"], "Blueprint.compiled_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": _plain(self.values),
            "origins": dict(self.origins),
            "resolved": _plain(self.resolved),
            "caliber": _plain(self.caliber),
            "supply_pins": [p.to_dict() for p in self.supply_pins],
            "domain": _plain(self.domain),
            "family_declaration": {
                "family": self.family_declaration["family"],
                "declared_at": _plain(self.family_declaration["declared_at"]),
            },
            "station_order": [[n, r] for n, r in self.station_order],
            "subcall_graph": {k: list(v) for k, v in self.subcall_graph.items()},
            "digests": {k: str(v) for k, v in self.digests.items()},
            "check_results": [_plain(c) for c in self.check_results],
            "compiled_at": _plain(self.compiled_at),
        }


def leaf_paths(tree: Mapping[str, Any], prefix: str = "") -> set[str]:
    """列出配置树的全部叶子路径（`a.b.c`）。

    叶子定义：非映射值，或空映射。空映射算叶子，因为"显式写成空"与"没写"
    必须可区分——否则删掉一个空块就无法被 `origins` 覆盖率检查发现。
    """
    out: set[str] = set()
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            out |= leaf_paths(value, path)
        else:
            out.add(path)
    return out


__all__ = [
    "Blueprint",
    "CALL_PHASES",
    "DIGEST_ALGOS",
    "Digest",
    "EVIDENCE_FIELDS",
    "EVIDENCE_KINDS",
    "Evidence",
    "PARCEL_ORIGINS",
    "PARCEL_STATUSES",
    "Parcel",
    "Pin",
    "REF_KINDS",
    "Ref",
    "TICKET_MUTABLE_FIELDS",
    "TICKET_STATUSES",
    "Ticket",
    "leaf_paths",
]
