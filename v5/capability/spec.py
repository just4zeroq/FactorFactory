"""能力规格与 describe 元信息（s0/04 §3 / §4.2）。

能力层的五条结构禁令靠「签名里没有位置」保证：
  不评估、不判显著、不写计数、不自选口径、不自建子调用。
本模块只声明规格，不含任何评估/账本/配置/结论入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from v5.contracts.errors import E2003, FlowError

# 十二项能力的名字空间（s0/04 §3 逐项 + §5.1）
# 方法标签的阶段后缀（G1/G2）。这里的 `S3` / `S4` / `S7` 是**方法版本标记**，
# 不是"当前系统阶段"：`observe` 的 turnover/gross_sharpe 是 S3 定义的**毛**粗筛
# 量（cost=0，绝不冒充净成本），所以保留 @S3；`allocate+backtest` 才是 S4 的
# 真成本链；组合域在 S7 才成形，旧代码写 @S4 是阶段归属错误。
# 五处散落的字面量收进这一张表；改这里会进 payload / domain 指纹，属新试验族。
METHOD_STAGES: dict[str, str] = {
    "simple_longshort": "S3",
    "longshort": "S3",
    "allocate+backtest": "S4",
    "combine_domain": "S7",
}


def method_id(name: str) -> str:
    """方法标签的唯一构造入口。未登记的方法不给默认值——拼错了就 E2003。"""
    stage = METHOD_STAGES.get(name)
    if stage is None:
        raise FlowError(E2003, f"方法 {name!r} 未登记阶段标签", path="capability.method_id")
    return f"{name}@{stage}"


# 方法标签的阶段后缀（G1/G2）。这里的 `S3` / `S4` / `S7` 是**方法版本标记**，
# 不是"当前系统阶段"：`observe` 的 turnover/gross_sharpe 是 S3 定义的**毛**粗筛
# 量（cost=0，绝不冒充净成本），所以保留 @S3；`allocate+backtest` 才是 S4 的
# 真成本链；组合域在 S7 才成形，旧代码写 @S4 是阶段归属错误。
# 五处散落的字面量收进这一张表；改这里会进 payload / domain 指纹，属新试验族。
METHOD_STAGES: dict[str, str] = {
    "simple_longshort": "S3",
    "longshort": "S3",
    "allocate+backtest": "S4",
    "combine_domain": "S7",
}


def method_id(name: str) -> str:
    """方法标签的唯一构造入口。未登记的方法不给默认值——拼错了就 E2003。"""
    stage = METHOD_STAGES.get(name)
    if stage is None:
        raise FlowError(E2003, f"方法 {name!r} 未登记阶段标签", path="capability.method_id")
    return f"{name}@{stage}"


CAPABILITIES: dict[str, str] = {
    "read": "single",
    "boundary": "single",
    "align": "single",
    "label": "single",
    "search": "open",
    "observe": "single",
    "verdict": "fixed",
    "combine": "closed_multi",
    "allocate": "closed_multi",
    "evaluate": "single",
    "backtest": "closed_multi",
    "certify": "single",
}

# 服务工位（用于编译期名字解析与 C20 检查）。
# 唯一真相：s0/04 §3 各能力的「服务工位」行 + §1.3 子调用协议。
SERVES: dict[str, tuple[str, ...]] = {
    "read": ("Panel",),
    "boundary": ("Boundary",),
    "align": ("Panel",),
    "label": ("Target",),
    "search": ("Search",),
    "observe": ("Evidence",),
    "verdict": ("Verdict",),
    "combine": ("Portfolio",),
    # allocate 服务三处：Evidence 单因子 / Portfolio 组合 / Certify 复算（§3.9）
    "allocate": ("Evidence", "Portfolio", "Certify"),
    "evaluate": ("Evidence", "Certify"),
    # backtest 服务 5/6/7/8 四个工位（§3.11）
    "backtest": ("Evidence", "Verdict", "Portfolio", "Certify"),
    "certify": ("Certify",),
}

# s0/04 §1.1：side_effect ∈ {pure, repeatable, append_only}
_VALID_SIDE_EFFECTS = frozenset({"pure", "repeatable", "append_only"})

# 逐能力元信息（s0/04 §1.1/§1.3 + §3.1–3.12）。
# 不要用一套「全 pure、全空 composes」的默认值套所有能力——那会让编译期
# 子调用图与口径组装都缺依据（P1-12）。只读表，describe() 从这里投影。
_SPECS: dict[str, dict[str, Any]] = {
    "read":       {"side_effect": "pure",       "composes": (), "policy_reads": ()},
    "boundary":   {"side_effect": "repeatable", "composes": (), "policy_reads": ("execution",)},
    "align":      {"side_effect": "repeatable", "composes": (), "policy_reads": ("execution",)},
    "label":      {"side_effect": "pure",       "composes": (), "policy_reads": ("execution", "label")},
    "search":     {"side_effect": "append_only", "composes": (), "policy_reads": ()},
    "observe":    {"side_effect": "pure",       "composes": ("allocate", "backtest"),
                   "policy_reads": ("execution", "net")},
    "verdict":    {"side_effect": "append_only", "composes": (), "policy_reads": ()},
    "combine":    {"side_effect": "repeatable", "composes": (), "policy_reads": ()},
    "allocate":   {"side_effect": "repeatable", "composes": (),
                   # execution.rebalance：再平衡周期（06 §5.6），构造必须与
                   # 回测链同一个持仓过程——网格在哪个能力里冻结都行，但
                   # 只能有一处定义、两边同读。
                   "policy_reads": ("allocate", "execution")},
    "evaluate":   {"side_effect": "pure",       "composes": (), "policy_reads": ()},
    "backtest":   {"side_effect": "pure",       "composes": (), "policy_reads": ("net",)},
    "certify":    {"side_effect": "repeatable", "composes": ("evaluate", "allocate", "backtest"),
                   "policy_reads": ("execution", "label", "allocate", "net")},
}

_POLICY_SUBBLOCKS = frozenset({"execution", "label", "allocate", "net"})


@dataclass(frozen=True)
class CapabilitySpec:
    """能力的公开元信息。不含评估/计数/结论/配置字段。"""

    name: str
    namespace: str
    serves: tuple[str, ...]
    params_schema_ref: str
    policy_reads: tuple[str, ...] = ()
    side_effect: str = "pure"
    composes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.namespace not in {"open", "closed_multi", "single", "fixed"}:
            raise FlowError(E2003, f"非法名字空间: {self.namespace}", path=f"{self.name}.namespace")
        if self.side_effect not in _VALID_SIDE_EFFECTS:
            raise FlowError(E2003, f"非法副作用声明: {self.side_effect}", path=f"{self.name}.side_effect")
        # policy_reads 必须是 caliber 的合法子块；"policy_view" 是组装后的
        # 只读映射名，不是口径子块（P1-12）。
        bad = set(self.policy_reads) - _POLICY_SUBBLOCKS
        if bad:
            raise FlowError(
                E2003, f"非法口径子块: {sorted(bad)}", path=f"{self.name}.policy_reads",
                hint="合法子块: execution/label/allocate/net",
            )
        unknown = set(self.composes) - set(CAPABILITIES)
        if unknown:
            raise FlowError(E2003, f"未登记的子能力: {sorted(unknown)}",
                            path=f"{self.name}.composes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "namespace": self.namespace, "serves": list(self.serves),
            "params_schema_ref": self.params_schema_ref, "policy_reads": list(self.policy_reads),
            "side_effect": self.side_effect, "cacheable": self.cacheable,
            "composes": list(self.composes),
        }

    @property
    def cacheable(self) -> bool:
        """产物能否按 `call_digest` 复用：由 `side_effect` 派生，不单独声明。

        曾经是三个独立字段（`cacheable` / `deterministic` / `stateful`），
        `_SPECS` 一个都没赋值，于是恒为 `True/True/False`——`describe` 里印三个
        恒定的真假值，读的人会以为它们是逐能力的判断（复核 E10）。真正的信息
        只有一条：`append_only` 的能力每次调用都往账本追加事实，命中缓存会让
        「试了几次」少记一笔，所以它**不可缓存**；`pure` / `repeatable` 可以。

        确定性与状态性不在这里声明：确定性由 `seed` 通道 + `call_digest` 保证
        （同一单必须同结果，否则 `E2002`）；有没有 `fit/generate` 阶段是**算法**
        的属性，在 `mining.AlgorithmSpec` 里（`stateful` / `needs_fitness`）。
        """
        return self.side_effect != "append_only"


def describe(name: str) -> CapabilitySpec:
    """返回能力的公开规格。名字未登记即 E2003。"""
    if name not in CAPABILITIES:
        raise FlowError(E2003, f"能力未登记: {name}", path="capability")
    row = _SPECS[name]
    return CapabilitySpec(
        name=name,
        namespace=CAPABILITIES[name],
        serves=SERVES[name],
        params_schema_ref=f"capability/{name}/params",
        policy_reads=row["policy_reads"],
        side_effect=row["side_effect"],
        composes=row["composes"],
    )


def all_specs() -> dict[str, CapabilitySpec]:
    return {name: describe(name) for name in CAPABILITIES}
