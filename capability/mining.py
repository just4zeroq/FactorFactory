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

from capability.registry import default_registry, param_error, param_kind

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
#
# 名字表不手写：`param_domain` 就是「这个算子的末位是计数」的唯一声明处
# （registry.py 的自检也读同一字段）。手写一份必然在扩表时与 JSON 分叉，
# 而分叉的症状是「新算子的窗口能写成 2.5」，运行到求值才炸。
def _param_domains(op: str) -> list[tuple[str, Mapping[str, Any]]]:
    """按声明顺序返回这个算子的参数域；无参算子返回空表。

    参数位不从 AST 的形状猜：`quantile` 有两个参数位（window 是 count、q 是
    fraction），只看「末位是 int」会把它拒之门外，而只看「末位是数字」又会
    放过 `mean(x, 2.5)`。域表是唯一真相。
    """
    spec = default_registry().operators.get(op)
    if spec is None:
        return []
    return list((spec.get("param_domain") or {}).items())


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
        # 参数位置：每个 param_domain 对应 AST 末尾的一个 const 节点，顺序与
        # JSON 声明一致。count 位置必须原始就是 int（规范化前查，否则 2.0 会
        # 被 _canon_number 归一成 2，绕过计数约束）；fraction 位置允许实数。
        domains = _param_domains(op)
        for j, (pname, domain) in enumerate(domains):
            node = args[len(args) - len(domains) + j]
            if not (isinstance(node, Mapping) and set(node) == {"const"}):
                raise ValueError(
                    f"带参算子 {op!r} 的第 {j + 1} 个参数必须是 {{'const': 数}}"
                )
            err = param_error(op, pname, domain, node["const"])
            if err:
                raise ValueError(err)
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


# GP 的 opcode 表顺序决定「基因整数 → 算子」的映射。一旦重排，历史种子会解出
# 另一批表达式；扩空间时把旧算子钉在原位、新算子追加到末尾，才谈得上「同一
# 种子同一结果」。这不是又一张白名单：它只规定**顺序**，算子集合仍从注册表来。
_GP_ORDER_HINT = (
    "rank", "scale", "abs", "log", "sign",
    "add", "sub", "mul", "div",
    "ref", "delta", "mean", "std", "max", "min", "sum", "corr", "cov",
)
# 不进 GP 的算子分两类：
# 1) 随机组合会炸——`product` 是窗口连乘，落在价格上时 1e5 的 30 次方即溢出 inf；
#    `power` / `signed_power` 的右操作数一旦是表达式，量级不可控（close^close），
#    或直接把自己变成 NaN（负底数的分数次幂）。
# 2) 同义重复——`delay` 与 `ref` 在 operators.json 里指向同一个 impl_symbol，
#    两个名字进同一张 opcode 表只会让 GP 把预算花在 `candidate_id` 去重上。
# 3) 生成器形状不够——`quantile` 有两个参数位（当前 _OpCodes 只会追加一个
#    窗口 const），`where_` 是三元算子（当前只支持一元/二元/带一元窗口）。
#    它们在先验池与计划里可用，但不由搜索算法自动生成。
# 4) 等值算子——`eq`/`ne` 作用在连续字段上时几乎只命中浮点/量化精度造成的
#    孤立格子（campaign_r1 的 `eq(2, ts_mad(low,6))`：53010 格只有 1 个 1），
#    会伪装成高 IC 的单名 artifact。保留给手写事件因子，但不让搜索自动生成。
# 被排除的算子仍可写进先验池与计划，只是不由搜索算法自动生成。
_GP_EXCLUDE = frozenset({
    "product", "power", "signed_power", "delay", "quantile", "where_",
    "eq", "ne",
})


def _gp_operator_order(reg) -> tuple[list[str], list[str], list[str]]:
    """按注册表派生 GP 的三张算子表：(一元, 二元, 带参)。

    单点维护：生成器与 beam 都读它，扩表后两边自动一致——旧实现各写一份
    tuple，`corr`/`cov` 曾经只存在于生成器一侧，beam 永远搜不到它们。
    """
    def _order(pred) -> list[str]:
        def ok(name: str) -> bool:
            return name not in _GP_EXCLUDE and pred(reg.operators[name])

        head = [n for n in _GP_ORDER_HINT if n in reg.operators and ok(n)]
        tail = sorted(n for n in reg.operators if n not in head and ok(n))
        return head + tail

    unary = _order(lambda s: int(s.get("arity", 0)) == 1 and not s.get("param_domain"))
    binary = _order(lambda s: int(s.get("arity", 0)) == 2 and not s.get("param_domain"))
    windowed = _order(lambda s: bool(s.get("param_domain")))
    return unary, binary, windowed


class _OpCodes:
    """Opcode 表：前半终端、后半算子。索引即基因整数值。"""

    def __init__(self, fields: Sequence[str], reg, cfg: GPConfig) -> None:
        self.terminals: list[dict[str, Any]] = [
            {"field": f} for f in fields
        ] + [{"const": c} for c in _TERMINAL_CONST]
        self.ops: list[tuple[str, int, int | None]] = []  # (name, arity, window)
        unary, binary, windowed = _gp_operator_order(reg)
        for name in unary:
            self.ops.append((name, 1, None))
        for name in binary:
            self.ops.append((name, 2, None))
        # 带参算子：窗口域从注册表读，不手写。arity 用 `spec["arity"] - 1`——
        # 注册表的 arity **含**末位窗口参数，decode 会再追加一个 const 窗口，
        # 直接传注册表的 arity 会让 corr 多出一个参数。
        for name in windowed:
            spec = reg.operators[name]
            domain_spec = spec.get("param_domain") or {}
            if len(domain_spec) != 1:
                # 多参数位算子（quantile）不能只追加一个 const 就交差：那会
                # 生成 arity 不符的 AST，烧一次求值后必然被拒。
                continue
            domain = next(iter(domain_spec.values()), {})
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


def _op(name: str, *args: Any) -> dict[str, Any]:
    """构造 AST 算子节点（模板枚举算法用）。"""
    return {"op": name, "args": list(args)}


def _fld(name: str) -> dict[str, Any]:
    return {"field": name}


def _cn(value: int | float) -> dict[str, Any]:
    return {"const": value}


def _template_ret(name: str, window: int) -> dict[str, Any]:
    d = _op("ref", _fld(name), _cn(window))
    return _op("div", _op("sub", _fld(name), d), d)


def _build_templates(fields: frozenset[str], windows: Sequence[int]):
    """(模板名, 窗口) → AST。只产出字段在 `fields` 内的模板。

    每个模板都是经济学上有动机的锚点（动量/反转/波动/量能/carry/基差/持仓），
    在窗口网格上枚举。这与 random_expression 的均匀采样互补：模板命中率远高于
    随机，先验命中后再由 GP 在同一空间做交叉变异。
    """
    out: list[tuple[str, int, dict[str, Any]]] = []
    for w in windows:
        if "close" in fields:
            out.append(("mom", w, _template_ret("close", w)))
            out.append(("rev", w, _op("mul", _cn(-1), _template_ret("close", w))))
        if "close" in fields and "vwap" in fields:
            out.append(("vwap_gap", w, _op("div",
                       _op("sub", _fld("close"), _fld("vwap")), _fld("vwap"))))
        if "high" in fields and "low" in fields and "close" in fields:
            out.append(("hl_range", w, _op("div",
                       _op("sub", _fld("high"), _fld("low")),
                       _op("mean", _fld("close"), _cn(w)))))
        if "volume" in fields:
            out.append(("vol_ratio", w, _op("div", _fld("volume"),
                       _op("mean", _fld("volume"), _cn(w)))))
        if "funding_rate" in fields:
            out.append(("funding_carry", w, _op("mul", _cn(-1),
                       _op("mean", _fld("funding_rate"), _cn(w)))))
        if "basis" in fields:
            out.append(("basis_carry", w, _op("mean", _fld("basis"), _cn(w))))
        if "open_interest" in fields:
            out.append(("oi_change", w, _template_ret("open_interest", w)))
        # 交互：负 funding × 量能比（仅在两字段都在时用 w 作为量能窗口）
        if "funding_rate" in fields and "volume" in fields:
            out.append(("funding_vol", w, _op(
                "mul",
                _op("mul", _cn(-1), _op("mean", _fld("funding_rate"), _cn(w))),
                _op("div", _fld("volume"), _op("mean", _fld("volume"), _cn(20))),
            )))
    return out


def generate_template_enum(
    *, cfg: GPConfig, fields: Sequence[str], seed: int,
    available_from: str = "",
    windows: Sequence[int] | None = None,
    templates: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """模板枚举（`template_enum`）：确定性枚举经济锚点 × 窗口网格。

    无 fitness、无随机：每个 (模板, 窗口) 生成一个候选，按 candidate_id 去重。
    `windows` 默认 [5,10,20,40,60,120]；`templates` 可筛选模板名前缀。
    `max_candidates` 截断（按窗口顺序，稳定）。
    """
    reg = default_registry()
    _ = reg  # 模板固定使用已注册算子/字段，normalise_ast 会再校验一次
    have = frozenset(str(f) for f in fields)
    wins = [int(w) for w in (windows or [5, 10, 20, 40, 60, 120])]
    wins = [w for w in wins if 1 <= w <= 512]
    tpl_filter = {str(t) for t in templates} if templates else None
    pool = _build_templates(have, wins)
    if tpl_filter:
        pool = [(n, w, ast) for n, w, ast in pool if n in tpl_filter]
    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, win, ast in pool:
        cand = make_candidate(
            ast, generator=f"template_enum:{name}@1", seed=seed,
            available_from=available_from,
            params_snapshot={"template": name, "window": win, "windows": wins},
        )
        if cand["candidate_id"] in seen:
            continue
        seen.add(cand["candidate_id"])
        cands.append(cand)
    target = int(cfg.max_candidates)
    if target and len(cands) > target:
        cands = cands[:target]
    state = {
        "algorithm": "template_enum", "version": "1", "seed": seed,
        "fields": list(fields), "windows": wins,
        "templates": sorted({n for n, _, _ in pool}),
        "target": target, "emitted": len(cands),
    }
    return cands, state


def generate_beam_search(
    *,
    cfg: GPConfig,
    fields: Sequence[str],
    seed: int,
    fitness: Callable[[Mapping[str, Any]], float] | None,
    available_from: str = "",
    beam_width: int = 8,
    depth: int = 3,
    windows: Sequence[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """集束搜索符号回归（`beam_search`）：沿 fitness 梯度确定性构造表达式。

    与 GP 的随机进化正交：GP 靠选择/交叉/变异在基因空间随机游走，本算法从
    终端出发，每轮对 beam 中每个表达式枚举**所有单步扩展**（一元算子、与字段
    的二元组合、带参算子×窗口），逐个求 fitness，去重后保留 top-`beam_width`
    个，迭代 `depth` 轮。确定性（同 seed 同结果），无随机。

    返回所有被 fitness 评估过的候选（去重），不只是最终 beam——让 Evidence/DSR
    看到真实的试验数（算法无法少记账）。无 fitness 时退化为「宽度优先枚举
    前 depth 层」，不冒充有指导的搜索。
    """
    reg = default_registry()
    have = tuple(sorted(f for f in fields if f in reg.fields))
    if not have:
        return [], {"algorithm": "beam_search", "reason": "no_fields"}
    beam_width = max(2, int(beam_width))
    depth = max(1, int(depth))
    wins = [int(w) for w in (windows or [5, 10, 20]) if 1 <= int(w) <= 512] or [5, 10, 20]

    # 单步扩展：一元无参算子（对整式）+ 带参一元算子×窗口 + 与字段的二元组合。
    # 三张表与 GP 共用 `_gp_operator_order`，扩算子后两边同时变宽；带参算子只取
    # registry arity==2 的那些（`corr`/`cov` 需要两个表达式，单步扩展给不出第二个，
    # 强行产出只会得到 arity 不符的 AST，白烧一次求值）。
    unary, binary, windowed = _gp_operator_order(reg)
    param_unary = tuple(n for n in windowed
                        if int(reg.operators[n].get("arity", 2)) == 2)

    def has_field(node: dict) -> bool:
        """AST 是否含至少一个 field。纯常量表达式无截面区分度，不扩展。"""
        if "field" in node:
            return True
        return any(has_field(a) for a in node.get("args", [])
                   if isinstance(a, dict))

    def expand(ast: dict) -> list[dict]:
        if not has_field(ast):
            return []  # 纯常量扩展只会得到更多常量，浪费预算
        out: list[dict] = []
        for op in unary:
            out.append({"op": op, "args": [ast]})
        for op in param_unary:
            for w in wins:
                out.append({"op": op, "args": [ast, {"const": w}]})
        for f in have:
            for op in binary:
                out.append({"op": op, "args": [ast, {"field": f}]})
        return out

    seen_ast: dict[str, dict] = {}
    scored: dict[str, float] = {}
    calls = {"total": 0, "ok": 0, "failed": 0}

    def score(ast: dict) -> float:
        try:
            norm = normalise_ast(ast)
            key = json.dumps(norm, sort_keys=True, separators=(",", ":"))
        except ValueError:
            return -1.0
        if key in seen_ast:
            return scored.get(key, -1.0)
        seen_ast[key] = norm
        if fitness is None:
            scored[key] = 0.0
            return 0.0
        val = fitness(make_candidate(
            norm, seed=seed, generator="beam_search@1", available_from=available_from))
        calls["total"] += 1
        if val > -1.0:
            calls["ok"] += 1
        else:
            calls["failed"] += 1
        scored[key] = float(val)
        return scored[key]

    # 初始 beam：每个字段本身 + 一个常量种子。
    beam = [{"field": f} for f in have] + [{"const": 1.0}]
    for a in beam:
        score(a)
    for _ in range(depth):
        neighbours: list[dict] = []
        for a in beam:
            neighbours.extend(expand(a))
        for a in neighbours:
            score(a)
        # 按 fitness 降序保留 top-B；同分按内容键定序（确定性）。
        ranked = sorted(seen_ast.keys(),
                        key=lambda k: (-scored.get(k, -1.0), k))
        beam = [seen_ast[k] for k in ranked[:beam_width]]

    ranked = sorted(seen_ast.keys(), key=lambda k: (-scored.get(k, -1.0), k))
    cands = [make_candidate(
        seen_ast[k], generator="beam_search@1", seed=seed,
        available_from=available_from,
        params_snapshot={"beam_width": beam_width, "depth": depth, "windows": wins},
    ) for k in ranked[:cfg.max_candidates]]
    fitted_state = {
        "algorithm": "beam_search", "version": "1", "seed": seed,
        "beam_width": beam_width, "depth": depth, "windows": wins,
        "fields": list(have), "evaluated": len(seen_ast), "calls": calls,
    }
    return cands, fitted_state


def describe_template_enum() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="template_enum", version="1", deterministic=True, needs_seed=True,
        stateful=False, needs_fitness=False,
        fitness_objectives=(),
        fit_window="mine_only",
        output_kind="expression",
        max_candidates=40,
        budget_group="inherit",
        param_schema={
            "max_candidates": {"type": "int", "min": 1},
            "windows": {"type": "list", "item": {"type": "int", "min": 1, "max": 512}},
            "templates": {"type": "list", "item": {"type": "str"}},
        },
    )


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


def describe_beam_search() -> AlgorithmSpec:
    reg = default_registry()
    return AlgorithmSpec(
        name="beam_search", version="1", deterministic=True, needs_seed=True,
        stateful=False, needs_fitness=True,
        fitness_objectives=("search_fitness",),
        fit_window="mine_only",
        output_kind="expression",
        operator_whitelist=tuple(sorted(reg.operators)),
        max_candidates=40,
        budget_group="inherit",
        param_schema={
            "beam_width": {"type": "int", "min": 2},
            "depth": {"type": "int", "min": 1},
            "windows": {"type": "list", "item": {"type": "int", "min": 1, "max": 512}},
            "max_candidates": {"type": "int", "min": 1},
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


# ---- 语义变异：从内置因子 Pool 做局部、可解释的邻域搜索 -----------------
# 与 random_expression 的均匀采样互补：不从空表达式随机长起，而是拿已经在这条
# 市场上有经济含义的锚点做小步变异。窗口翻倍/减半、同族字段互换、取负、截面
# 标准化都是「仍可解释」的变换；不做跨族乱拼，避免生成只有 p 值没有故事的式子。
_FIELD_SWAPS = (
    ("close", "vwap"), ("high", "low"),
    ("volume", "open_interest"), ("funding_rate", "basis"),
)
_MUTATIONS = ("window", "field", "neg", "rank", "scale", "smooth")
# 时序平滑窗口：rolling R² / 效率比这类"路径依赖"因子逐 bar 重算会抖得厉害，
# 平滑后信号方向不变、换手显著下降。trend_r2_20 在 h=10 下 ICIR/DSR 都够，
# 唯一卡在 turnover=0.131 → cost_adj=0.00027 没过 0.0004 闸，平滑是针对这个
# 短板的定向变异，不是放宽判据。
_SMOOTH_WINDOWS = (3, 5)


def _mutate_windows(node: Any, reg, scale: float) -> Any:
    if not isinstance(node, Mapping):
        return node
    if "op" not in node or "args" not in node:
        return node
    args = [_mutate_windows(a, reg, scale) for a in node["args"]]
    domains = list((reg.operators.get(node["op"], {}).get("param_domain") or {}).items())
    if domains:
        start = len(args) - len(domains)
        for j, (pname, domain) in enumerate(domains):
            if param_kind(domain) != "count" or start + j >= len(args):
                continue
            slot = args[start + j]
            value = slot.get("const") if isinstance(slot, Mapping) else None
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            lo = int(domain.get("min", 1))
            hi = int(domain.get("max", 512))
            new = int(round(value * scale))
            if lo <= new <= hi and new != value:
                args[start + j] = {"const": new}
    return {"op": node["op"], "args": args}


def _swap_fields(node: Any, pairs: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(node, Mapping):
        if set(node) == {"field"} and node["field"] in dict(pairs):
            # 保持方向确定：a→b 与 b→a 都由同一对定义，不依赖 dict 顺序。
            for a, b in pairs:
                if node["field"] == a:
                    return {"field": b}
                if node["field"] == b:
                    return {"field": a}
            return dict(node)
        if "op" in node and "args" in node:
            return {"op": node["op"],
                    "args": [_swap_fields(a, pairs) for a in node["args"]]}
    return node


def _wrap(node: dict, op: str) -> dict:
    return {"op": op, "args": [node]}


def generate_semantic_mutation(
    *, fields: Sequence[str], seed: int, max_candidates: int = 40,
    mutations: Sequence[str] | None = None, available_from: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """内置因子 × 语义变异，确定性枚举。

    窗口变异同时试 ×2 与 ÷2（只在域内、且原值变化时保留）；字段互换只作用在
    面板确实存在的字段对上。`normalise_ast` 对每个产物再过一遍白名单/参数域，
    非法产物直接丢弃，不靠「生成时应该没错」的假设。
    """
    from capability.factors import BUILTIN_FACTORS  # 延迟导入，避免与 factors 循环

    reg = default_registry()
    have = frozenset(str(f) for f in fields)
    kinds = [str(m) for m in (mutations or _MUTATIONS)]
    unknown = sorted(set(kinds) - set(_MUTATIONS))
    if unknown:
        raise ValueError(f"未知语义变异 {unknown}；可用 {list(_MUTATIONS)}")
    pairs = tuple((a, b) for a, b in _FIELD_SWAPS if a in have and b in have)

    # 先为**全部**内置因子生成变体，再按「第 k 种变异」轮转取用。
    # 早期实现按因子字典序边生成边截断：max_candidates=60 时只摸到前十几个
    # 因子，后面四十多个从未进入搜索——产物里写着 59 个先验，实际只搜了一个
    # 字母序前缀，而日志里看不出这件事。轮转保证预算再小也能覆盖每个锚点。
    variants_by_base: list[list[tuple[str, dict]]] = []
    for base in sorted(BUILTIN_FACTORS):
        ast = BUILTIN_FACTORS[base]
        variants: list[tuple[str, dict]] = []
        if "window" in kinds:
            for scale in (2.0, 0.5):
                variants.append((f"window{scale:g}", _mutate_windows(ast, reg, scale)))
        if "field" in kinds and pairs:
            variants.append(("field", _swap_fields(ast, pairs)))
        for op in ("neg", "rank", "scale"):
            if op in kinds:
                variants.append((op, _wrap(ast, op)))
        if "smooth" in kinds:
            for w in _SMOOTH_WINDOWS:
                variants.append((f"smooth{w}", _op("mean", ast, {"const": w})))
        variants_by_base.append([
            (kind, variant) for kind, variant in variants if variant != ast
        ])

    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    attempted = 0
    dropped = 0
    max_per_round = max((len(v) for v in variants_by_base), default=0)
    for slot in range(max_per_round):
        for base, variants in zip(sorted(BUILTIN_FACTORS), variants_by_base):
            if slot >= len(variants):
                continue
            kind, variant = variants[slot]
            attempted += 1
            try:
                cand = make_candidate(
                    variant, generator=f"semantic_mutation:{base}:{kind}@1",
                    seed=seed, available_from=available_from,
                    params_snapshot={"base": base, "mutation": kind},
                    required_fields=sorted(_ast_fields(variant)),
                )
            except ValueError:
                dropped += 1
                continue
            if cand["candidate_id"] in seen:
                continue
            seen.add(cand["candidate_id"])
            cands.append(cand)
            if len(cands) >= int(max_candidates):
                break
        if len(cands) >= int(max_candidates):
            break
    state = {
        "algorithm": "semantic_mutation", "version": "1", "seed": seed,
        "fields": sorted(have), "mutations": kinds,
        "max_candidates": int(max_candidates),
        "attempted": attempted, "dropped": dropped, "emitted": len(cands),
    }
    return cands, state


# ---- 因子配对：跨经济族的内置因子做截面秩组合 ----------------------------
# 语义变异是「一个因子的小步邻域」，GP/random 是「从零长出新式子」。中间缺一档：
# 两个**已知有经济含义**的因子如何组合。这里固定 6 个正交锚点（资金费 carry、
# 低波、动量、反转、已实现波动、量比），把 59 个内置因子逐一与它们做截面秩差 /
# 秩比。秩先把量纲去掉，配对才有可比性；`div` 的分母加 0.1 是为了避免 rank=0
# 时除零产生 inf（evaluate 会把它转 NaN，白白吃掉 coverage）。
_BLEND_ANCHORS = (
    "funding_carry14", "low_vol20", "mom20", "rev1", "rv20", "vol_ratio",
)
_BLEND_OPS = ("rank_spread", "rank_ratio")


def _blend_ast(base: dict, anchor: dict, op: str) -> dict:
    rb = _op("rank", _normalise_copy(base))
    ra = _op("rank", _normalise_copy(anchor))
    if op == "rank_spread":
        return _op("sub", rb, ra)
    if op == "rank_ratio":
        return _op("div", rb, _op("add", ra, {"const": 0.1}))
    raise ValueError(f"未知配对算子 {op!r}")


def _normalise_copy(node: dict) -> dict:
    """normalise_ast 返回新结构，绝不把内置因子表本身改掉。"""
    return normalise_ast(node)


def generate_factor_blend(
    *, fields: Sequence[str], seed: int, max_candidates: int = 40,
    available_from: str = "", anchors: Sequence[str] | None = None,
    blend_ops: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """跨族内置因子的截面秩配对，确定性枚举。

    `anchors` 缺省用 `_BLEND_ANCHORS`；`blend_ops` 缺省 `rank_spread` /
    `rank_ratio`。按 (算子, 锚点, 基准因子) 轮转，预算再小也能覆盖多个基准。
    面板缺字段的配对直接跳过，不靠下游 evaluate 报错来发现。
    """
    from capability.factors import BUILTIN_FACTORS

    have = frozenset(str(f) for f in fields)
    names = sorted(BUILTIN_FACTORS)
    anchor_names = [str(a) for a in (anchors or _BLEND_ANCHORS) if a in BUILTIN_FACTORS]
    ops = [str(o) for o in (blend_ops or _BLEND_OPS)]
    unknown = sorted(set(ops) - set(_BLEND_OPS))
    if unknown:
        raise ValueError(f"未知配对算子 {unknown}；可用 {list(_BLEND_OPS)}")

    variants: list[tuple[int, int, int, str, str, dict]] = []
    for oi, op in enumerate(ops):
        for ai, anchor in enumerate(anchor_names):
            for bi, base in enumerate(names):
                if base == anchor:
                    continue
                required = _ast_fields(BUILTIN_FACTORS[base]) | _ast_fields(
                    BUILTIN_FACTORS[anchor])
                if not required <= have:
                    continue
                ast = _blend_ast(BUILTIN_FACTORS[base], BUILTIN_FACTORS[anchor], op)
                variants.append((oi, ai, bi, base, anchor, ast))
    # 先按基准因子、再按配对算子、最后按锚点轮转：锚点变化最快。
    # 旧实现把 anchor 放在第一排序位：max_candidates=20 时全落在第一个锚点
    # low_vol20 上，6 个经济锚点里 5 个从未进入搜索，注释却写着"覆盖多种
    # 经济锚点"——产物里看不出这件事（generator 名字带 anchor，但日志没汇总）。
    variants.sort(key=lambda v: (v[2], v[0], v[1]))

    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _oi, _ai, _bi, base, anchor, ast in variants:
        try:
            cand = make_candidate(
                ast, generator=f"factor_blend:{base}:{anchor}:{_oi}@1", seed=seed,
                available_from=available_from,
                params_snapshot={"base": base, "anchor": anchor,
                                 "blend_op": ops[_oi]},
                required_fields=sorted(_ast_fields(ast)),
            )
        except ValueError:
            continue
        if cand["candidate_id"] in seen:
            continue
        seen.add(cand["candidate_id"])
        cands.append(cand)
        if len(cands) >= int(max_candidates):
            break
    state = {
        "algorithm": "factor_blend", "version": "1", "seed": seed,
        "fields": sorted(have), "anchors": anchor_names, "blend_ops": ops,
        "max_candidates": int(max_candidates),
        "possible_pairs": len(variants), "emitted": len(cands),
    }
    return cands, state


def describe_factor_blend() -> AlgorithmSpec:
    reg = default_registry()
    return AlgorithmSpec(
        name="factor_blend", version="1", deterministic=True,
        needs_seed=True, stateful=False, needs_fitness=False,
        fit_window="mine_only", output_kind="expression",
        operator_whitelist=tuple(sorted(reg.operators)),
        max_candidates=40, budget_group="inherit",
        param_schema={
            "max_candidates": {"type": "int", "min": 1},
            "anchors": {"type": "list", "item": {"type": "str"}},
            "blend_ops": {"type": "list", "item": {"type": "str"}},
        },
    )


def describe_semantic_mutation() -> AlgorithmSpec:
    reg = default_registry()
    return AlgorithmSpec(
        name="semantic_mutation", version="1", deterministic=True,
        needs_seed=True, stateful=False, needs_fitness=False,
        fit_window="mine_only", output_kind="expression",
        operator_whitelist=tuple(sorted(reg.operators)),
        max_candidates=40, budget_group="inherit",
        param_schema={
            "max_candidates": {"type": "int", "min": 1},
            "mutations": {"type": "list", "item": {"type": "str"}},
        },
    )
