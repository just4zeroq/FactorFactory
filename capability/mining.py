"""挖掘算法扩展点：统一 Candidate、candidate_id、deap GP 生成器与 fitness 闭包。

这里实现 02-mining-interface.md 的四方法协议（describe/fit/generate/lineage）
的第一个内置算法 `gp_symbolic`。它**不**改其余七步：产出统一 CandidateSet，
由编排层统一评估、裁决、计数。

三条禁令（02 §2 / s0/04 §6）靠结构保证：
- 不评估结论：算法不 import verdict，fitness 闭包只返回 §2.1 允许的标量/向量，
  不含 p 值、成本后收益、判据。
- 不写账本：算法无账本句柄；试验登记在 fitness 闭包的去重 set 内，计数结果由
  build 返回、执行器代写 Budget。算法手里没有可写账本对象。
- 不自选口径：fitness 复用 `evaluate` 的**同一个 build + 同一白名单版本 +
  同一 nan_policy**，算法无法替换求值实现。

candidate_id 是内容身份（H(规范化 payload + required_fields)），不含 generator
与 seed——跨算法去重才能成立（02 §6）。规范化规则见 `_normalise_ast`。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from capability.registry import default_registry

# 并发自适应搜索互斥：两个 fit（needs_fitness）同时进入 → E2009。
# 模块级而非实例级：不同搜索实例共享同一进程随机源，必须一起守。
_FIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class AlgorithmSpec:
    name: str
    version: str
    deterministic: bool = True
    needs_seed: bool = True
    stateful: bool = False
    needs_fitness: bool = False
    fitness_objectives: tuple[str, ...] = ()
    param_schema: Mapping[str, Any] = field(default_factory=dict)
    # 02 §3 AlgorithmSpec 的编译期字段（缺省值不代表"没有"，而是显式声明）
    fit_window: str = "mine_only"
    output_kind: str = "expression"
    required_fields: tuple[str, ...] = ()
    operator_whitelist: tuple[str, ...] = ()
    max_candidates: int = 40
    budget_group: str = "inherit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version,
            "deterministic": self.deterministic, "needs_seed": self.needs_seed,
            "stateful": self.stateful, "needs_fitness": self.needs_fitness,
            "fitness_objectives": list(self.fitness_objectives),
            "param_schema": dict(self.param_schema),
            "fit_window": self.fit_window, "output_kind": self.output_kind,
            "required_fields": list(self.required_fields),
            "operator_whitelist": list(self.operator_whitelist),
            "max_candidates": self.max_candidates, "budget_group": self.budget_group,
        }


# ---- 候选身份与 AST 规范化 ---------------------------------------------
# 带参算子的末位参数是「计数」（窗口/滞后），只接受 int；其余算子的 const 是值，
# int/float 皆可。02 §6 硬约束 2：位置决定语义——mul(close,2.0) 合法，
# mean(close,2.0) 必须 E1003。normalise_ast 抛 ValueError，由调用方转 E1003。
_PARAM_OPS = frozenset({
    "delay", "ref", "sum", "mean", "std", "max", "min", "delta", "corr", "cov",
})
_MAX_AST_DEPTH = 64  # 02 §6 硬约束 3


def _canon_number(x: float) -> int | float:
    """值位置的数字归一：2 与 2.0 是同一个常数（02 §6）。"""
    if isinstance(x, bool):
        raise ValueError("bool 不是常数")
    f = float(x)
    if not np.isfinite(f):
        raise ValueError("常数必须有限")
    return int(f) if f.is_integer() else f


def normalise_ast(node: Any, *, _depth: int = 0) -> Any:
    """JSON AST → 唯一规范形式，供 candidate_id 哈希。

    只接受三种节点（field / const / op），键集合必须精确匹配；多一个注释键
    就会稀释内容身份，让两个语义相同的表达式被当成两次试验（C16）。同时执行
    02 §6 的三条硬约束：带参算子末位参数必须是 int、嵌套深度 ≤64、键集合闭合。
    """
    if _depth > _MAX_AST_DEPTH:
        raise ValueError(f"AST 深度超过 {_MAX_AST_DEPTH}")
    if not isinstance(node, Mapping):
        raise ValueError("AST 节点必须是映射")
    keys = set(node)
    if keys == {"field"}:
        name = node["field"]
        if not isinstance(name, str) or not name:
            raise ValueError("field 必须是非空字符串")
        return {"field": name}
    if keys == {"const"}:
        return {"const": _canon_number(node["const"])}
    if keys == {"op", "args"}:
        op = node["op"]
        args = node["args"]
        if not isinstance(op, str) or not op:
            raise ValueError("op 必须是非空字符串")
        if not isinstance(args, (list, tuple)):
            raise ValueError("args 必须是列表")
        # 参数位置：末位必须是以 int 表达的 const（bool 也不行）。规范化前校验
        # 原始节点，否则 _canon_number 会把 2.0 归一成 2，绕过计数约束。
        if op in _PARAM_OPS and args:
            last = args[-1]
            if not (isinstance(last, Mapping) and set(last) == {"const"}
                    and isinstance(last["const"], int)
                    and not isinstance(last["const"], bool)):
                raise ValueError(
                    f"带参算子 {op!r} 的末位参数必须是整数 const（{{'const': int}}）"
                )
        return {"op": op, "args": [normalise_ast(a, _depth=_depth + 1) for a in args]}
    raise ValueError(f"非法 AST 节点键: {sorted(keys)}")


def candidate_payload(ast: Any) -> dict[str, Any]:
    """候选 payload = 规范化的 expression。"""
    return {"expression": normalise_ast(ast)}


def candidate_id(payload: Mapping[str, Any], required_fields: Sequence[str]) -> str:
    """内容身份：H(payload + required_fields)，不含 generator/seed（02 §6）。"""
    body = {
        "payload": payload,
        "required_fields": sorted(str(f) for f in required_fields),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_candidate(ast: Any, *, source: str = "algorithm",
                   generator: str = "", parents: Sequence[str] = (),
                   seed: int | None = None, params_snapshot: Mapping[str, Any] | None = None,
                   required_fields: Sequence[str] | None = None,
                   available_from: str = "") -> dict[str, Any]:
    """构造统一 Candidate；字段严格按 02 §6，禁止携带任何指标/结论。

    `available_from` 是执行期 as-of（编译期固化的数据末端），由执行器经
    `market_view` 注入；能力层不自取墙钟。空串表示"本域内使用、待执行器固化"，
    不参与 `candidate_id`（内容身份只含 payload + required_fields）。
    """
    payload = candidate_payload(ast)
    fields = sorted(set(required_fields or _ast_fields(ast)))
    return {
        "candidate_id": candidate_id(payload, fields),
        "source": source,
        "generator": generator,
        "payload": payload,
        "required_fields": fields,
        "parents": list(parents),
        "operators": sorted(_ast_operators(ast)),
        "params_snapshot": dict(params_snapshot or {}),
        "seed": seed,
        "available_from": available_from,
    }


def _ast_fields(node: Any, out: set[str] | None = None) -> set[str]:
    out = out if out is not None else set()
    if isinstance(node, Mapping):
        if "field" in node and isinstance(node["field"], str):
            out.add(node["field"])
        for v in node.values():
            _ast_fields(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _ast_fields(v, out)
    return out


def _ast_operators(node: Any, out: set[str] | None = None) -> set[str]:
    out = out if out is not None else set()
    if isinstance(node, Mapping):
        if "op" in node and isinstance(node["op"], str):
            out.add(node["op"])
        for v in node.values():
            _ast_operators(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _ast_operators(v, out)
    return out


def dedup_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """跨来源按 candidate_id 去重；generator 谱系合并进 generators 列表。

    去重键只有 candidate_id（内容身份）。generator 是谱系元数据，不参与去重、
    不分池分账（02 §6 / §10）。
    """
    order: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    for c in candidates:
        cid = str(c["candidate_id"])
        if cid not in merged:
            row = dict(c)
            row["generators"] = [str(c.get("generator", ""))] if c.get("generator") else []
            order.append(cid)
            merged[cid] = row
        else:
            gen = str(c.get("generator", ""))
            if gen and gen not in merged[cid]["generators"]:
                merged[cid]["generators"].append(gen)
    return [merged[cid] for cid in order]


# ---- GP 生成（栈式基因编码 + deap 进化框架） ----------------------------
# 不用 deap 的 PrimitiveTree 携带 AST：1.4 版本的 Primitive 对象没有 func、
# Terminal.value 被改名，内部结构不可靠。改为「整数基因序列 + 自己确定性解码」：
# deap 只负责选择/交叉/变异的进化框架，AST 解码完全在我们的控制下，同一基因
# 序列恒得同一 AST（AC39 逐位复现）。
_TERMINAL_CONST = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class GPConfig:
    fields: tuple[str, ...]
    population: int = 20
    generations: int = 3
    max_depth: int = 4
    window_min: int = 2
    window_max: int = 10
    p_crossover: float = 0.7
    p_mutation: float = 0.3
    tournament: int = 3
    max_candidates: int = 40
    genome_len: int = 7


class _OpCodes:
    """Opcode 表：前半终端、后半算子。索引即基因整数值。"""

    def __init__(self, fields: Sequence[str], reg, cfg: GPConfig) -> None:
        self.terminals: list[dict[str, Any]] = [
            {"field": f} for f in fields
        ] + [{"const": c} for c in _TERMINAL_CONST]
        self.ops: list[tuple[str, int, int | None]] = []  # (name, arity, window)
        for name in ("rank", "scale", "abs", "log", "sign"):
            if name in reg.operators:
                self.ops.append((name, 1, None))
        for name in ("add", "sub", "mul", "div"):
            if name in reg.operators:
                self.ops.append((name, 2, None))
        # G6：带参算子从注册表的 `param_domain` 派生，不再手写 7 个名字。
        # `corr` / `cov` 在 operators.json 里声明了 impl_symbol 与窗口域，旧生成器
        # 却从不产出它们——声明与生成分叉，且没有任何检查点会报错（产物只是
        # "搜索空间比文档小"）。arity 用 `spec["arity"] - 1`：注册表的 arity
        # **含**末位窗口参数，decode 会再追加一个 const 窗口，直接传 registry
        # 的 arity 会让 corr 多出一个参数。
        _PARAM_ORDER = ("ref", "delta", "mean", "std", "max", "min", "sum",
                        "corr", "cov")
        for name in _PARAM_ORDER:
            spec = reg.operators.get(name)
            if not spec or not spec.get("param_domain"):
                continue
            domain = next(iter(spec["param_domain"].values()), {})
            lo = max(int(domain.get("min", cfg.window_min)), cfg.window_min)
            hi = min(int(domain.get("max", cfg.window_max)), cfg.window_max)
            arity = max(1, int(spec.get("arity", 2)) - 1)
            for w in sorted({lo, (lo + hi) // 2, hi}):
                if lo <= w <= hi:
                    self.ops.append((name, arity, w))
        self.n_terminal = len(self.terminals)

    @property
    def max_gene(self) -> int:
        return self.n_terminal + len(self.ops) - 1

    def decode(self, genes: Sequence[int], max_depth: int | None = None) -> Any:
        """栈式解码：终端压栈；算子按 arity 弹栈组合；末尾栈顶即根。

        深度超过 `max_depth`（配置值）或栈无法归约 → ValueError（该个体适应度
        记 -1、generate 阶段丢弃）。`max_depth` 必须真正参与判定：只用常量 16
        兜底会让配置的上限形同虚设，`max_depth=3` 与 `=8` 搜出同一片空间。
        """
        stack: list[tuple[Any, int]] = []  # (ast, depth)
        for g in genes:
            if g < self.n_terminal:
                stack.append((dict(self.terminals[g]), 1))
                continue
            name, arity, window = self.ops[g - self.n_terminal]
            if len(stack) < arity:
                raise ValueError("基因序列无法归约")
            args = [stack.pop() for _ in range(arity)]
            depth = 1 + max(d for _, d in args)
            if max_depth is not None and depth > max_depth:
                raise ValueError(f"解码深度 {depth} 超过 max_depth={max_depth}")
            if depth > _DECODE_MAX_DEPTH:
                raise ValueError("解码深度超限")
            children = [a for a, _ in args]
            if window is not None:
                children.append({"const": window})
            stack.append(({"op": name, "args": children}, depth))
        if len(stack) != 1:
            raise ValueError("基因序列残留多个栈项")
        return stack[0][0]


_DECODE_MAX_DEPTH = 16  # 解码硬上限（与配置 max_depth 无关的防御）


def _gene_bounds(codes: _OpCodes) -> tuple[int, int]:
    return 0, codes.max_gene


def _random_individual(rng: random.Random, codes: _OpCodes, length: int,
                       max_depth: int) -> list[int]:
    """递归后序生成**保证可归约、深度受控**的基因序列。

    后序（子节点在前、算子在后）天然满足栈式 decode 的归约约束；递归深度
    上限保证不会生成超深表达式。返回的序列长度 <= length（不足时用一元算子
    包裹根补齐到 length，仍保持可归约）。

    这是初始种群的生成器。交叉/变异后的个体可能不再可归约，由 decode 捕获
    并给适应度 -1，不会让整代无候选。
    """
    unary_genes = [codes.n_terminal + i for i, op in enumerate(codes.ops)
                   if op[1] == 1]
    binary_genes = [codes.n_terminal + i for i, op in enumerate(codes.ops)
                    if op[1] == 2]
    terminal_genes = list(range(codes.n_terminal))

    def build(depth: int) -> tuple[list[int], int]:
        """返回 (后序基因, 实际深度)，补齐时据此守住 max_depth。"""
        can_expand = depth < max_depth and (unary_genes or binary_genes)
        if not can_expand or rng.random() < 0.35:
            return [rng.choice(terminal_genes)], 1
        pool = (unary_genes + binary_genes) if (unary_genes and binary_genes) \
            else (unary_genes or binary_genes)
        op = rng.choice(pool)
        arity = codes.ops[op - codes.n_terminal][1]
        left, dl = build(depth + 1)
        genes = left
        depth_out = 1 + dl
        if arity == 2:
            right, dr = build(depth + 1)
            genes = genes + right
            depth_out = 1 + max(dl, dr)
        return genes + [op], depth_out

    genes, depth = build(1)
    # 补齐到 length：用一元算子反复包裹根，保持可归约；但每次包裹深度 +1，
    # 达到 max_depth 即停——旧实现无视深度一路补齐，是 max_depth 失效的入口。
    while len(genes) < length and unary_genes and depth < max_depth:
        genes = genes + [rng.choice(unary_genes)]
        depth += 1
    return genes


def generate_gp(
    *,
    cfg: GPConfig,
    fields: Sequence[str],
    seed: int,
    fitness: Callable[[Mapping[str, Any]], float] | None,
    genes_limit: int | None = None,
    initial_genes: Sequence[Sequence[int]] | None = None,
    available_from: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """跑一次 GP：fit(有 fitness) + generate(吐最终种群)。

    返回 (candidates, fitted_state)。`fitness` 由编排层注入，算法不自建求值器。
    无 fitness 时退化为纯随机种群（describe.deterministic 仍成立），不冒充 GP。

    `initial_genes` 让 `generate()` 门面复用 `fit()` 落盘的最终种群——否则
    generate 会另起随机种群，与 fit 分叉（02 §5 要求 fit/generate 连续）。

    复现（AC39）：deap 的选择/交叉/变异走全局 random 模块。进入时把全局随机
    源设置成局部种子、退出时无条件还原——同 seed 两次逐位一致，且不污染调用方
    的全局随机状态。
    """
    from deap import base, creator, tools

    reg = default_registry()
    codes = _OpCodes(fields, reg, cfg)
    if codes.max_gene < 1:
        raise ValueError("搜索空间为空：无可用终端或算子")

    if not hasattr(creator, "V5FitnessMax"):
        creator.create("V5FitnessMax", base.Fitness, weights=(1.0,))
    if not hasattr(creator, "V5Individual"):
        creator.create("V5Individual", list, fitness=creator.V5FitnessMax)

    def viable(ind: Sequence[int]) -> bool:
        """可归约且深度受控——交叉/变异后用它是为了修复而非丢弃个体。"""
        try:
            codes.decode(ind, cfg.max_depth)
            return True
        except ValueError:
            return False

    rng = random.Random(seed)
    saved_state = random.getstate()
    random.setstate(rng.getstate())
    try:
        toolbox = base.Toolbox()
        toolbox.register("individual", lambda: creator.V5Individual(
            _random_individual(rng, codes, cfg.genome_len, cfg.max_depth)))
        toolbox.register("population", tools.initRepeat, list, toolbox.individual)
        toolbox.register("select", tools.selTournament, tournsize=cfg.tournament)
        toolbox.register("mate", tools.cxTwoPoint)
        toolbox.register("mutate", tools.mutUniformInt,
                         low=0, up=codes.max_gene, indpb=cfg.p_mutation)
        toolbox.register("clone", lambda ind: type(ind)(ind))

        if initial_genes is not None:
            # generate 门面：严格复用 fit 落盘的基因，不重新初始化。
            pop = [creator.V5Individual(list(g)) for g in initial_genes]
        else:
            pop = toolbox.population(n=cfg.population)

        repaired = 0
        if fitness is not None:
            for _ in range(cfg.generations):
                fits = []
                for ind in pop:
                    try:
                        ast = codes.decode(ind, cfg.max_depth)
                        val = fitness(make_candidate(
                            ast, seed=seed, generator="gp_symbolic@1",
                            available_from=available_from))
                    except ValueError:
                        val = -1.0
                    fits.append(float(val))
                for ind, val in zip(pop, fits):
                    ind.fitness.values = (val,)
                selected = toolbox.select(pop, len(pop))
                offspring = [toolbox.clone(ind) for ind in selected]
                # 交叉：失败（不可归约/超深）时回退到两个父本之一，保留种群规模。
                # 旧实现直接丢弃子代，实测丢弃率约 70%——等于每代凭空少 70% 试验，
                # 还让最终种群远小于声明规模（C14 配额对不上）。
                for a, b in zip(offspring[::2], offspring[1::2]):
                    if rng.random() < cfg.p_crossover:
                        backup_a, backup_b = list(a), list(b)
                        toolbox.mate(a, b)
                        if not viable(a) or not viable(b):
                            a[:], b[:] = backup_a, backup_b
                            repaired += 1
                        else:
                            del a.fitness.values, b.fitness.values
                # 变异：同理，失败则回退到变异前基因。
                for ind in offspring:
                    if rng.random() < cfg.p_mutation:
                        backup = list(ind)
                        toolbox.mutate(ind)
                        if not viable(ind):
                            ind[:] = backup
                            repaired += 1
                        else:
                            del ind.fitness.values
                pop = offspring

        # generate：只吐最终种群，不评估、不筛选（02 §2）。无效个体已由上面的
        # 修复策略消除；这里仍保留兜底（无 fitness 的纯随机种群理论上也可归约）。
        cands: list[dict[str, Any]] = []
        dropped = 0
        final_genes: list[list[int]] = []
        for ind in pop:
            try:
                ast = codes.decode(ind, cfg.max_depth)
            except ValueError:
                dropped += 1
                continue
            final_genes.append(list(ind))
            cands.append(make_candidate(
                ast, generator="gp_symbolic@1", seed=seed,
                available_from=available_from,
                params_snapshot={"population": cfg.population,
                                 "generations": cfg.generations,
                                 "max_depth": cfg.max_depth},
            ))
        fitted_state = {
            "algorithm": "gp_symbolic", "version": "1", "seed": seed,
            "population": cfg.population, "generations": cfg.generations,
            "genome_len": cfg.genome_len, "max_depth": cfg.max_depth,
            "fields": list(fields), "dropped_decode": dropped,
            "repaired": repaired, "final_genes": final_genes,
        }
        if genes_limit is not None and len(cands) > genes_limit:
            cands = cands[:genes_limit]
            fitted_state["final_genes"] = final_genes[:genes_limit]
        return cands, fitted_state
    finally:
        random.setstate(saved_state)


def generate_random(
    *,
    cfg: GPConfig,
    fields: Sequence[str],
    seed: int,
    available_from: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯随机表达式搜索（`random_expression`）：无 fitness、无进化。

    与 GP 的关系：两者共用同一套 `_OpCodes` 解码与 `_random_individual` 生成器，
    所以「随机搜索」不是另一个空间，而是同一空间的均匀采样。这正是它作为
    基线算法的价值——GP 若显著优于随机采样，才说明进化真的起了作用；否则
    `gp_symbolic` 的名字是虚的。

    确定性：只用局部 `random.Random(seed)`，不碰全局 random 模块（GP 因为 deap
    必须改全局，这里不必承担那个代价）。同 seed 同参数 → 逐位相同的候选序列。

    去重：按 `candidate_id` 去重，最多尝试 `count * 20` 次生成——拒绝采样。
    生成空间（终端 + 算子）可能小到装不下 `count` 个不同表达式，此时如实
    返回少于 `count` 个，而不是靠重复项充数（重复项会让 n_trials 虚高）。
    """
    reg = default_registry()
    codes = _OpCodes(fields, reg, cfg)
    if codes.max_gene < 1:
        raise ValueError("搜索空间为空：无可用终端或算子")
    # 随机搜索的窗口/深度由 cfg 决定；population/generations 对它无意义，
    # 但仍进 params_snapshot，便于两算法在同一张表里横向比较。
    rng = random.Random(seed)
    target = int(cfg.max_candidates)
    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    attempts = 0
    limit = max(target * 20, target + 20)
    while len(cands) < target and attempts < limit:
        attempts += 1
        genes = _random_individual(rng, codes, cfg.genome_len, cfg.max_depth)
        try:
            ast = codes.decode(genes, cfg.max_depth)
        except ValueError:
            continue
        cand = make_candidate(
            ast, generator="random_expression@1", seed=seed,
            available_from=available_from,
            params_snapshot={"max_candidates": target, "max_depth": cfg.max_depth,
                             "genome_len": cfg.genome_len},
        )
        if cand["candidate_id"] in seen:
            continue
        seen.add(cand["candidate_id"])
        cands.append(cand)
    state = {
        "algorithm": "random_expression", "version": "1", "seed": seed,
        "fields": list(fields), "target": target, "attempts": attempts,
        "dropped_decode": 0, "repaired": 0,
    }
    return cands, state


def describe_random() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="random_expression", version="1", deterministic=True, needs_seed=True,
        stateful=False, needs_fitness=False,
        fitness_objectives=(),
        fit_window="mine_only",
        output_kind="expression",
        max_candidates=40,
        budget_group="inherit",
        param_schema={
            "max_candidates": {"type": "int", "min": 1},
            "max_depth": {"type": "int", "min": 1},
            "genome_len": {"type": "int", "min": 1},
        },
    )


def describe_gp() -> AlgorithmSpec:
    reg = default_registry()
    return AlgorithmSpec(
        name="gp_symbolic", version="1", deterministic=True, needs_seed=True,
        stateful=True, needs_fitness=True,
        fitness_objectives=("abs_rank_ic",),
        fit_window="mine_only",
        output_kind="expression",
        operator_whitelist=tuple(sorted(reg.operators)),
        max_candidates=40,
        budget_group="inherit",
        param_schema={
            "population": {"type": "int", "min": 2},
            "generations": {"type": "int", "min": 0},
            "max_depth": {"type": "int", "min": 1},
            "max_candidates": {"type": "int", "min": 1},
        },
    )
