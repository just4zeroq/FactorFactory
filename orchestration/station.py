"""八条 StationSpec 规格表（s0/05-orchestration.md §1.2）。

规格是数据表：加约束、改默认、改名字空间都只改数据，不改执行器。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.errors import E1003, FlowError


@dataclass(frozen=True)
class StationSpec:
    name: str
    revision: str
    inputs: tuple[str, ...]
    output: str
    default_impl: str
    namespaced: str  # open / closed_multi / single / fixed
    side_effect: str  # pure / repeatable / append_only
    standalone: str  # allowed / requires_frozen_plan / probe_only
    event: str | None = None  # Budget / Publish


STATION_SPECS: tuple[StationSpec, ...] = (
    StationSpec("Boundary", "r1", ("Pin[]", "reserve_decl"), "ScopeParcel",
                "boundary@1", "single", "repeatable", "requires_frozen_plan"),
    StationSpec("Panel", "r1", ("Boundary", "Pin[]", "Reveal"), "PanelParcel",
                "panel@1", "single", "repeatable", "requires_frozen_plan"),
    StationSpec("Target", "r1", ("Panel", "caliber.label", "caliber.execution"), "LabelParcel",
                "target@1", "single", "pure", "requires_frozen_plan"),
    StationSpec("Search", "r1", ("Label", "Panel"), "CandidateParcel",
                "search@1", "open", "append_only", "probe_only", event="Budget"),
    StationSpec("Evidence", "r1", ("Candidate", "Panel", "Label"), "EvidenceParcel",
                "evidence@1", "single", "pure", "allowed"),
    StationSpec("Verdict", "r1", ("Evidence",), "VerdictParcel",
                "sentinel@1", "fixed", "append_only", "probe_only"),
    StationSpec("Portfolio", "r1", ("Verdict", "Panel", "Boundary"), "PortfolioParcel",
                "portfolio@1", "closed_multi", "repeatable", "allowed"),
    StationSpec("Certify", "r1", ("all_upstream",), "CertificateParcel",
                "certify@1", "single", "repeatable", "allowed", event="Publish"),
)

BY_NAME = {s.name: s for s in STATION_SPECS}

# `inputs` 里用的是逻辑产物名（Label/Candidate…），产出的工位名却不同
# （Label←Target，Candidate←Search）。这张表是二者的唯一映射（数据表，不是逻辑）。
# 非工位引用（Pin[]/reserve_decl/Reveal/caliber.*）不在此表：它们是共享事实引用，
# 由编译期 gate_b 校验，不作为「上游 Parcel 依赖」。
_INPUT_TO_STATION = {
    "Boundary": "Boundary",
    "Panel": "Panel",
    "Label": "Target",
    "Candidate": "Search",
    "Evidence": "Evidence",
    "Verdict": "Verdict",
    "Portfolio": "Portfolio",
    "Certify": "Certify",
}

# 非工位的共享事实引用（编译期由 gate_b 校验，不是上游 Parcel 依赖）。
# 白名单之外的名字**必须在导入期报错**：拼错一个依赖名若被静默跳过，
# 下游就会在没有上游指纹的情况下继续跑——正是"静默降级"。
_NON_STATION_REFS = frozenset({"Pin[]", "reserve_decl", "Reveal"})
_CALIBER_PREFIX = "caliber."


def _validate_specs() -> None:
    for spec in STATION_SPECS:
        for ref in spec.inputs:
            if ref == "all_upstream" or ref in _INPUT_TO_STATION:
                continue
            if ref in _NON_STATION_REFS or ref.startswith(_CALIBER_PREFIX):
                continue
            raise FlowError(
                E1003,
                f"{spec.name}.inputs 含未定义依赖 {ref!r}",
                path=f"station.{spec.name}.inputs",
                hint="依赖只能是工位产物名、all_upstream、Pin[]/reserve_decl/Reveal 或 caliber.*",
            )
        if spec.name in input_producers(spec):
            raise FlowError(
                E1003, f"{spec.name}.inputs 出现自依赖",
                path=f"station.{spec.name}.inputs",
            )


def input_producers(spec: StationSpec) -> list[str]:
    """把 spec.inputs 解析为上游工位名（有序、去重）。

    ``all_upstream`` 展开为序列中位于本工位之前的所有工位。返回顺序即契约，
    执行器据此建立 lineage 与 input_digests，形成指纹链。
    非工位引用（Pin[]/reserve_decl/Reveal/caliber.*）不参与 Parcel 依赖。
    """
    out: list[str] = []

    def add(name: str) -> None:
        if name not in out:
            out.append(name)

    for ref in spec.inputs:
        if ref == "all_upstream":
            for prior in STATION_SPECS:
                if prior.name == spec.name:
                    break
                add(prior.name)
        elif ref in _INPUT_TO_STATION:
            add(_INPUT_TO_STATION[ref])
    return out


_validate_specs()

