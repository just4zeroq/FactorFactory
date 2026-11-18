"""内置实现装配（能力层唯一的注册入口）。

S1 的 12 个契约壳还在 `ops/*.py` 里；真实实现按阶段逐个登记到这里。
登记即执法：`single` / `fixed` 名字空间重复登记会 `E2006`（`runtime.register`）。

**实现名沿用计划里的名字**（`label` → `forward_return_cs`、`evaluate` → `qweave`、
`observe` → `two_stage_funnel`、`verdict` → `sentinel`），这样
`plan.target.impl` / `plan.evidence.impl` 与能力实现名是同一个词，
不需要一张"计划名 → 实现名"的映射表——那张表迟早会与本体不一致。
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

from .backtest_engine import build as _backtest_ledger_build
from .ops import align as _align
from .ops import allocate as _allocate
from .ops import boundary as _boundary
from .ops import certify as _certify
from .ops import combine as _combine
from .ops import evaluate as _evaluate
from .ops import label as _label
from .ops import observe as _observe
from .ops import read as _read
from .ops import search as _search
from .ops import verdict as _verdict
from .runtime import impls, register


def _has(capability: str) -> bool:
    return bool((impls().get(capability) or {}))


def install_default_impls() -> None:
    """幂等装配：已登记的能力不重复登记（重复会触发 E2006/E2003）。"""
    if not _has("read"):
        # 实现名说明语义而不说明对象：「只读已发布版本」是它唯一的读法，
        # 没有「最新」「补齐」变体（s0/04 §3.1）。
        register("read", "published_only", "1", _read.build)
    if not _has("boundary"):
        register("boundary", "pit_dynamic_topk", "1", _boundary.build)
    if not _has("align"):
        register("align", "point_in_time_align", "1", _align.build)
    if not _has("label"):
        register("label", "forward_return_cs", "1", _label.build)
    if not _has("evaluate"):
        # 实现版本取**内核版本**而不是 V5 自己的序号：qweave 的窗口/NaN 语义
        # 是口径的一部分（requirements.txt 精确锁定），换内核就是换求值语义，
        # 若版本号不跟着变，缓存会拿旧语义的产物去服务新语义。
        register("evaluate", "qweave", _pkg_version("qweave"), _evaluate.build)
    if not _has("search"):
        # open 名字空间：按算法名登记。当前内置 gp_symbolic；新算法在此加一行。
        # build 内部按 params.algorithm 分派（本阶段单实现，多算法走同一 build）。
        register("search", "gp_symbolic", "1", _search.build)
    if not _has("observe"):
        # single 名字空间；实现名沿用计划的 `two_stage_funnel`。
        register("observe", "two_stage_funnel", "1", _observe.build)
    if not _has("verdict"):
        # fixed 名字空间：唯一实现、不可配名。名字沿用计划里的 `sentinel`
        # （17-walkthrough / ACCEPTANCE 样例），不得在 plan 里出现 verdict.impl。
        register("verdict", "sentinel", "1", _verdict.build)
    if not _has("combine"):
        # closed_multi：实现名 = 计划的 `portfolio.combiner`。三个合成器共用同一
        # build（按 call.impl 分派），因为它们只差「权重怎么拟合」；各自注册是为
        # 了让 impl 名进 call_digest——换合成器必须换指纹，不能只换一个 params 键。
        for _name in ("equal_weight", "score_weighted", "fixed_ridge"):
            register("combine", _name, "1", _combine.build)
    if not _has("allocate"):
        # closed_multi：一个实现覆盖两条内部路径（分位构造 / cvxpy 约束投影），
        # 走哪条由 `caliber.allocate.mode|constraints` 决定，不由工位决定。
        # 当前 plan_schema 未暴露 mode，故 CLI 只走分位；约束路径已在实现内
        # 与单测中，待契约补键即启用（不得另注册一个「近似」实现）。
        register("allocate", "quantile", "1", _allocate.build)
    if not _has("backtest"):
        # closed_multi：生产唯一实现是 ml4t 目标权重引擎 + V5 成本口径。
        # ops/backtest.py 的向量化版本只作数值参照与单测夹具，不在此注册。
        register("backtest", "ledger", "1", _backtest_ledger_build)
    if not _has("certify"):
        # single：walk-forward 的折与 embargo 口径归 certify。实现只切折 +
        # 装配可复现清单，逐折净值由编排层驱动 allocate/backtest 回填
        # （能力不自建子调用，s0/04 §1.3）。
        register("certify", "walk_forward", "1", _certify.build)


__all__ = ["install_default_impls"]
