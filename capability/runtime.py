"""能力统一调用协议 `run(call) -> Result`（s0/04 §1）。

六步，顺序即契约：

| 步骤 | 动作 | 失败 |
|---|---|---|
| 1 | 重算 `call_digest` 与自身一致 | `E2002` |
| 2 | 按 `param_schema` 校验 `params` | `E1003` / `E1004` / `E1008` |
| 3 | `inputs` 数量、顺序、`kind` 与声明一致 | `E3001` |
| 3.5 | 子调用只能是已声明的、深度 ≤ 2、共享同一 `policy_view` | `E2008` / `E1020` |
| 4 | 执行实现体 | `E3004` |
| 5 | 按 `outputs_schema` 校验返回值 | `E3003` |
| 6 | 返回，**不落盘** | — |

第 3.5 步是本模块存在的核心理由之一：`policy_view` 只含本能力**声明过**的
口径子块。声明外的一律 `E2001`——不是"读了也没用"，而是"读到了就已经越权"。
子调用必须共享父 `policy_view`，否则"证据层用便宜成本判可交易、组合层用
昂贵成本回测"会发生在同一个工位内部，比跨工位更难发现。

本模块**不**提供任何写盘、账本、判据入口：能力只返回，落盘与计数归执行器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from contracts import digest as dg
from contracts.digest import compute_digest
from contracts.errors import (
    E1002, E1004, E1008, E1020, E2001, E2002, E2003, E2006, E2008, E3004, FlowError,
)
from contracts.types import CALL_PHASES

from .params import validate_inputs, validate_outputs, validate_params
from .spec import describe

MAX_COMPOSE_DEPTH = 2
# 可选输入的位置占位符的指纹：`None` 也是一个可被观察到的输入状态，
# 不能与"少传一项"混为一谈（后者会让后继输入整体前移）。
ABSENT_DIGEST = compute_digest({"kind": "absent_input"})


@dataclass(frozen=True)
class InputRef:
    """一条已解析的输入：`kind` 进契约校验，`digest` 进指纹，`data` 只读。"""

    kind: str
    digest: str
    data: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.kind:
            raise FlowError(E2003, "InputRef.kind 不能为空", path="call.inputs")
        if not self.digest:
            raise FlowError(E2003, "InputRef.digest 不能为空", path="call.inputs")


@dataclass(frozen=True)
class Call:
    """一次调用的**唯一**定义：六通道快照 + 四个指纹。

    **全仓只有这一处 `class Call`**。曾经契约层还另有一份 16 字段的 `Call`
    （带 `params_digest` / `policy_digest` / `runtime_facts_digest` 三个存储字段），
    但生产代码一处都不构造它——它只是一张没人对得上的规格表，且与真实类型的
    字段名不同（`call_digest` vs `digest`）、校验强度不同（那一份会校验 phase
    枚举与非空串，这一份什么都不校验）。两份定义并存的代价不是"冗余"，而是
    **看代码的人无法知道哪个是真的**。

    合并的取舍：三个子摘要不存字段、改为**派生属性**——它们是 `params` /
    `policy_view` / `runtime_facts` 的纯函数，存储只会多出一个能与本体分叉的
    副本（V5 反复吃亏的正是这一类）。`call_digest` 存字段，因为它是"这单调用"
    的身份，且第 1 步要拿它与重算值比对。

    校验（`__post_init__`）从被删的那一份搬过来：字段名对得上契约表，`phase`
    落不进 `CALL_PHASES` 即 `E1004`。这条以前只在没人用的类型里生效，等于没有。
    """

    capability: str
    station: str
    station_revision: str
    impl: str
    impl_version: str
    phase: str
    params: Mapping[str, Any]
    inputs: tuple[InputRef, ...]
    policy_view: Mapping[str, Any]
    runtime_facts: Mapping[str, Any]
    composed: tuple["Call", ...] = ()
    seed: int | None = None
    call_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("capability", "station", "station_revision", "impl", "impl_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise FlowError(E1004, f"Call.{name} 必须是非空字符串",
                                path=f"Call.{name}")
        if self.phase not in CALL_PHASES:
            raise FlowError(
                E1004,
                f"Call.phase={self.phase!r} 不在 {list(CALL_PHASES)}",
                path="Call.phase",
                hint="有状态实现才给 fit/generate；无状态必须显式 none",
            )
        for name in ("params", "policy_view", "runtime_facts"):
            if not isinstance(getattr(self, name), Mapping):
                raise FlowError(E1004, f"Call.{name} 必须是映射", path=f"Call.{name}")
        if not isinstance(self.seed, (int, type(None))) or isinstance(self.seed, bool):
            raise FlowError(
                E1004, "Call.seed 必须是 int 或显式 null", path="Call.seed",
                hint="不需求种子的实现填 null，不要省略这个字段",
            )
        for idx, item in enumerate(self.inputs):
            # `None` 是**可选输入的位置占位符**（见 `ABSENT_DIGEST`）：它表达
            # "这一项声明了但本次没给"，与"少传一项让后继整体前移"是两件事，
            # 所以占位符必须留在原位，不能当脏数据拒掉。
            if item is not None and not isinstance(item, InputRef):
                raise FlowError(
                    E1004,
                    f"Call.inputs[{idx}] 必须是 InputRef 或 None（可选输入占位），"
                    f"得到 {type(item).__name__}",
                    path="Call.inputs",
                )
        for idx, sub in enumerate(self.composed):
            if not isinstance(sub, Call):
                raise FlowError(E1004, f"Call.composed[{idx}] 必须是 Call",
                                path="Call.composed")

    # ---- 子摘要：派生，不存储 ------------------------------------------
    @property
    def params_digest(self) -> str:
        """`H(params)`：`call_digest` 的一个入参，也是"参数被改了"的最小证据。"""
        return compute_digest(dict(self.params))

    @property
    def policy_view_digest(self) -> str:
        """`H(policy_view)`：**口径视图**的摘要，**不是** `policy_digest` 本体。

        真正的 `policy_digest` 由编排层持有（含 `caliber_digest` 与
        `constitutive_subset`，能力 `Call` 里没有这两个通道）。所以这里如实叫
        `policy_view_digest`：起名 `policy_digest` 会让"这一单看到的口径取值"
        与"口径身份"同名不同义——同一个坑已经踩过一次（证书清单里的
        `policy_view_digest` 就是因为这个改的）。
        """
        return compute_digest(dict(self.policy_view))

    @property
    def runtime_facts_digest(self) -> str:
        """`H(runtime_facts)`：计数快照 / 揭示状态 / 审核状态。"""
        return dg.runtime_facts_digest(dict(self.runtime_facts))

    # ---- 构造：指纹只在这里算 -----------------------------------------
    @classmethod
    def create(
        cls,
        *,
        capability: str,
        station: str,
        impl: str,
        impl_version: str,
        params: Mapping[str, Any] | None = None,
        inputs: Sequence[InputRef] = (),
        policy_view: Mapping[str, Any] | None = None,
        runtime_facts: Mapping[str, Any] | None = None,
        composed: Sequence["Call"] = (),
        phase: str = "none",
        seed: int | None = None,
        station_revision: str = "r1",
    ) -> "Call":
        body = dict(
            capability=capability, station=station,
            station_revision=station_revision, impl=impl, impl_version=impl_version,
            phase=phase, params=dict(params or {}), inputs=tuple(inputs),
            policy_view=dict(policy_view or {}), runtime_facts=dict(runtime_facts or {}),
            composed=tuple(composed), seed=seed,
        )
        draft = cls(**body, call_digest="")
        return cls(**body, call_digest=draft.compute_digest())

    def compute_digest(self) -> str:
        return dg.call_digest(
            station=self.station,
            station_revision=self.station_revision,
            capability=self.capability,
            impl=self.impl,
            impl_version=self.impl_version,
            phase=self.phase,
            params_digest=self.params_digest,
            input_digests=[i.digest if i is not None else ABSENT_DIGEST
                           for i in self.inputs],
            input_kinds=[i.kind if i is not None else "absent" for i in self.inputs],
            policy=self.policy_view_digest,
            runtime_facts=self.runtime_facts_digest,
            composed_digests=[c.call_digest for c in self.composed],
            seed=self.seed,
        )

    def verify(self) -> None:
        """第 1 步。`params` / `policy_view` 被就地改写同样会失配。"""
        if not self.call_digest:
            raise FlowError(
                E2002, f"{self.capability} 调用单没有 call_digest",
                path=f"call.{self.capability}",
                hint="用 Call.create() 构造；手写 dataclass 会漏掉指纹",
            )
        if self.compute_digest() != self.call_digest:
            raise FlowError(
                E2002,
                f"{self.capability} 的 call_digest 与内容不符（调用单被改写）",
                path=f"call.{self.capability}",
            )


# ---- 实现注册表 --------------------------------------------------------
ImplFn = Callable[[Call], Mapping[str, Any]]


@dataclass(frozen=True)
class Impl:
    capability: str
    name: str
    version: str
    fn: ImplFn

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise FlowError(E2003, f"{self.capability} 的实现名/版本不能为空",
                            path=f"{self.capability}.impl")


_IMPLS: dict[str, dict[str, Impl]] = {}


def register(capability: str, name: str, version: str, fn: ImplFn) -> None:
    """登记一个能力实现。

    名字空间在这里**执法**（s0/04 §5）：`single` / `fixed` 只允许一个条目，
    出现第二个即 `E2006`（第二套执行路径）。`registry.resolve_impl()` 是同一
    规则的编译期版本；两处必须一致，否则"编译期一个名字、运行期另一个名字"
    会成为默认行为。
    """
    spec = describe(capability)  # 未登记的能力名直接 E2003
    if not name or not version:
        raise FlowError(E2003, f"{capability} 的实现名/版本不能为空",
                        path=f"{capability}.impl")
    bucket = _IMPLS.setdefault(capability, {})
    if bucket and spec.namespace in {"single", "fixed"}:
        raise FlowError(
            E2006,
            f"{capability} 是 {spec.namespace} 名字空间，已有实现 {sorted(bucket)}，"
            f"不得再登记 {name!r}",
            path=f"{capability}.impl",
            hint="single/fixed 只能有一个实现；第二套执行路径会让裁判可替换",
        )
    if name in bucket:
        raise FlowError(
            E2003, f"{capability} 已有同名实现 {name}", path=f"{capability}.impl",
            hint="同名不同版本同样是第二套执行路径",
        )
    bucket[name] = Impl(capability=capability, name=name, version=version, fn=fn)


def impls() -> dict[str, dict[str, Impl]]:
    """只读快照，供编译期/测试枚举。"""
    return {k: dict(v) for k, v in _IMPLS.items()}


def resolve(capability: str, name: str | None = None, version: str | None = None) -> Impl:
    """按名字空间解析实现（规则与 `registry.resolve_impl` 一致）。

    - `single` / `fixed`：`name` 不得给出（给了即 `E1002`），目录必须恰好一条，
      多条 → `E2006`；
    - `closed_multi`：必须给 `name` 且命中目录，否则 `E1002`；
    - `open`：必须给 `name`（缺失 `E1002`）；目录里没有该名字 → `E3004`
      （"允许新增条目"不等于"未实现也能跑"）。
    """
    spec = describe(capability)
    bucket = _IMPLS.get(capability) or {}
    if not bucket:
        raise FlowError(
            E3004, f"能力 {capability} 尚无真实实现",
            path=capability,
            hint="按阶段填实现；不得回落到桩成功",
        )
    if spec.namespace in {"single", "fixed"}:
        if len(bucket) != 1:
            raise FlowError(
                E2006, f"{capability} 目录条目数 {len(bucket)} != 1",
                path=f"{capability}.impl",
                hint="single/fixed 只允许一个实现",
            )
        chosen = next(iter(bucket.values()))
        if name and name != chosen.name:
            raise FlowError(
                E1002, f"{capability} 是 {spec.namespace} 名字空间，不得指定 {name!r}",
                path=f"{capability}.impl",
                hint=f"唯一实现是 {chosen.name}；要换实现就是改契约",
            )
    else:
        if not name:
            raise FlowError(
                E1002, f"{capability} 缺 impl 名", path=f"{capability}.impl",
                hint=f"已登记: {sorted(bucket)}",
            )
        if name not in bucket:
            if spec.namespace == "open":
                raise FlowError(
                    E3004, f"能力 {capability} 无实现 {name!r}", path=f"{capability}.impl",
                    hint="open 档允许新增目录条目，但未实现的名字不得执行",
                )
            raise FlowError(
                E1002, f"实现名 {name!r} 不在 {capability} 白名单",
                path=f"{capability}.impl", hint=f"已登记: {sorted(bucket)}",
            )
        chosen = bucket[name]
    if version is not None and chosen.version != version:
        raise FlowError(
            E1002,
            f"{capability} 实现 {chosen.name} 的版本是 {chosen.version}，不是 {version}",
            path=f"{capability}.impl_version",
            hint="实现版本进 call_digest，不能只写名字",
        )
    return chosen


def reset_impls() -> None:
    """仅供测试：清空实现表。"""
    _IMPLS.clear()


# ---- 第 3.5 步：子调用结构 ---------------------------------------------
def _depth(call: Call, seen: int = 1) -> int:
    if not call.composed:
        return seen
    if seen >= MAX_COMPOSE_DEPTH + 1:
        return seen
    depths = []
    for child in call.composed:
        if child.capability == call.capability:
            raise FlowError(
                E2008, f"{call.capability} 的子调用出现自递归", path=f"composed.{child.capability}",
            )
        depths.append(_depth(child, seen + 1))
    return max(depths, default=seen)


def _declared_reads(call: Call) -> set[str]:
    """整条调用链声明过的口径子块（父 + 全部子）。

    只按"父声明"校验会让合法的组合无法成立：`observe` 声明 `execution/net`、
    `allocate` 声明 `allocate`，而 §1.3 强制父子共享同一 `policy_view`。
    因此这里判的是"这个子块有没有人声明过"——没人声明的子块出现在
    `policy_view` 里，就是被偷偷塞进来的。

    残留风险：子实现仍可能去读兄弟能力声明、而自己没声明的子块。这一条
    无法在本层用运行时检查消除（共享 `policy_view` 是契约本身），只能靠
    C5 式静态检查（能力体内读 `policy_view` 的键必须 ⊆ 自身 `policy_reads`）。
    """
    declared = set(describe(call.capability).policy_reads)
    for child in call.composed:
        declared |= _declared_reads(child)
    return declared


def _check_policy_scope(call: Call) -> None:
    extra = sorted(set(call.policy_view) - _declared_reads(call))
    if extra:
        raise FlowError(
            E2001,
            f"{call.capability} 的调用链读了未声明的口径子块 {extra}",
            path=f"{call.capability}.policy_view",
            hint="想读就必须先在 policy_reads 里声明；子块集合是全链并集",
        )


def _check_composition(call: Call) -> None:
    _check_policy_scope(call)
    spec = describe(call.capability)
    for child in call.composed:
        if child.capability not in spec.composes:
            raise FlowError(
                E2008,
                f"{call.capability} 调用了未在 composes 中声明的能力 {child.capability}",
                path=f"{call.capability}.composed.{child.capability}",
            )
        if child.station != call.station:
            raise FlowError(
                E2001,
                f"子调用 {child.capability} 的工位 {child.station} 与父 {call.station} 不同",
                path=f"{call.capability}.composed.{child.capability}",
                hint="一个工位内的全部子调用必须属于同一工位",
            )
        if compute_digest(dict(child.policy_view)) != compute_digest(dict(call.policy_view)):
            raise FlowError(
                E1020,
                f"{call.capability} 与子调用 {child.capability} 读的口径不同",
                path=f"{call.capability}.composed.{child.capability}.policy_view",
                hint="父子共享同一 policy_view；自带口径即新实验",
            )
        child.verify()
    if _depth(call) > MAX_COMPOSE_DEPTH:
        raise FlowError(
            E2008, f"{call.capability} 的子调用链深度超过 {MAX_COMPOSE_DEPTH}",
            path=f"{call.capability}.composed",
        )


# ---- 唯一入口 ----------------------------------------------------------
def run(call: Call) -> dict[str, Any]:
    """执行一次能力调用。只返回，不落盘、不写账本、不产结论。"""
    # 1. 指纹
    call.verify()
    # 2. 参数（只校验，不改写：改写会让 digest 与实际执行内容不一致，
    #    而 digest 已在第 1 步按同一份 params 重算过）
    validate_params(call.capability, call.params)
    # 3. 输入
    validate_inputs(call.capability, call.inputs)
    # 3.5 子调用结构
    _check_composition(call)
    # 4. 执行
    if not call.impl or not call.impl_version:
        # `impl=""` 曾静默回落到唯一实现：digest 记的是 `impl@''`，实际跑的是
        # 另一个实现。Open 档尤其致命——所有算法版本会共享同一条指纹。
        raise FlowError(
            E1008, f"{call.capability} 的调用单缺 impl 或 impl_version",
            path=f"{call.capability}.impl",
            hint="实现名与版本都是指纹的一部分；不得在运行期临时解析",
        )
    impl = resolve(call.capability, call.impl, call.impl_version)
    try:
        payload = impl.fn(call)
    except FlowError:
        raise
    except NotImplementedError as exc:
        raise FlowError(
            E3004, f"{call.capability} 实现未完成: {exc}",
            path=call.capability,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 能力体内任何异常都不得当成成功
        raise FlowError(
            E3004,
            f"{call.capability} 实现抛出未预期异常: {type(exc).__name__}: {exc}",
            path=call.capability,
        ) from exc
    # 5. 返回结构（按 params 选档：backtest 的 summary/full 字段不同）
    return validate_outputs(call.capability, payload, call.params)
