"""能力实现 · search：候选生成（s0/04 §3.5 / 02-mining-interface.md）。

对外仍走单函数 `build(call)`（runtime 的 ImplFn 模型），内部实现四方法协议：
`describe` / `fit` / `generate` / `lineage` 在 `capability.mining`。

三条禁令在本模块的落点：
- **不判显著**：fitness 闭包只返回 |RankIC|（02 §2.1 允许的目标），不返回 p 值、
  成本后收益、判据；search 的 outputs 没有任何 decision/significant 字段。
- **不写计数**：每次 fitness 调用在闭包内去重登记（`seen`），build 把去重后的
  实测数返回给执行器代写 Budget；算法手里没有账本句柄。
- **不自选口径**：fitness 构造的 evaluate 子调用用 `default_registry()` 的白名单
  版本、`nan_policy=propagate`、`node_cache=False`，与 Evidence 同一实现；算法
  无法替换求值器。

candidate 身份、跨算法去重、random state 保护、并发检测都在 `mining.py`。
"""

from __future__ import annotations

from importlib.metadata import version as _pkg_version
from typing import Any, Callable, Mapping

import numpy as np

from capability.inference import ic_series
from capability.params import implementation_defaults
from capability.mining import (
    AlgorithmSpec, GPConfig, _FIT_LOCK, dedup_candidates,
    describe_gp, describe_random, describe_template_enum,
    generate_gp, generate_random, generate_template_enum, make_candidate,
)
from capability.panel import Panel
from capability.registry import default_registry
from contracts.errors import E1004, E2009, E3001, E3003, E3004, FlowError


def _algorithms() -> dict[str, AlgorithmSpec]:
    """已登记算法表。新增算法只在这里加一行（open 名字空间）。

    `gp_symbolic` 用适应度驱动的进化，`random_expression` 是同一表达式空间的
    均匀采样基线 —— GP 若赢不过它，进化的价值就不成立。
    """
    specs = (describe_gp(), describe_random(), describe_template_enum())
    return {spec.name: spec for spec in specs}


def describe() -> dict:
    """契约四方法之一：返回内建算法元信息（纯静态，不触碰数据）。"""
    return {name: spec.to_dict() for name, spec in _algorithms().items()}


def _algorithm_runs(params: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """把工位参数归一成 `[(算法名, 该算法参数)]`。

    两种写法都接受，且**行为一致**：

    - 多算法：`params.algorithms = [{name, params}, ...]`（计划里的列表原样透传）；
    - 单算法：扁平 `params.algorithm` + 同级键（老计划与直接调用的写法）。

    多算法不再「取第一个然后静默丢掉其余」—— 那会让计划里写了两个算法、
    实际只跑一个，而产物和运行日志都不留下任何痕迹（多算法支持的初衷是
    让 GP 与随机基线在同一 budget 内对照，静默丢弃使这个对照永远不成立）。
    """
    raw = params.get("algorithms")
    if isinstance(raw, (list, tuple)) and raw:
        runs: list[tuple[str, dict[str, Any]]] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                raise FlowError(
                    E1004, f"search.params.algorithms[{i}] 必须是映射",
                    path=f"search.params.algorithms[{i}]",
                )
            name = str(entry.get("name") or "")
            if not name:
                raise FlowError(
                    E1004, f"search.params.algorithms[{i}] 缺 name",
                    path=f"search.params.algorithms[{i}].name",
                )
            sub = dict(entry.get("params") or {})
            runs.append((name, sub))
        return runs
    return [(str(params.get("algorithm", "gp_symbolic")), dict(params))]


def fit(ctx, params, seed):
    """契约四方法之一：拟合阶段（stateful 算法才有意义）。

    `ctx` 必须提供 `fitness` 闭包与 `fields`；本实现的 GP 在 fit 内评估、
    产出可序列化 FittedState（含最终基因）。无 fitness 时退化为纯随机（不冒充
    GP 拟合）。needs_fitness 的 fit 同样受 `_FIT_LOCK` 守护（02 §5 / E2009）。

    无状态算法（`random_expression`）的 fit 是空操作，返回显式标记而不是
    假装拟合过 —— 否则 generate 会去找一份不存在的 `final_genes`。
    """
    # 门面只处理**单算法**：`fit` / `generate` 的参数形状是 `params.algorithm`
    # （扁平），而 `build` 用 `params.algorithms`（列表、多算法共享一次计数）。
    # 两者曾各认一套：把多算法计划喂给门面，它只会按 `algorithm`（缺省
    # gp_symbolic）跑一遍，其余算法静默消失——没有报错、没有日志，结果看起来
    # 像"多算法搜索"。这里显式拒绝，别让两种形状在同一个函数里假装兼容。
    if isinstance(params.get("algorithms"), list):
        raise FlowError(
            E1004,
            "search.fit/generate 是单算法门面，不接受 params.algorithms（多算法请走 build）",
            path="search.params.algorithms",
            hint="多算法的计数与去重由 build 的 _algorithm_runs 统一处理；门面只服务"
                 "单算法的四方法契约调用",
        )
    # 门面只处理**单算法**：`fit` / `generate` 的参数形状是 `params.algorithm`
    # （扁平），而 `build` 用 `params.algorithms`（列表、多算法共享一次计数）。
    # 两者曾各认一套：把多算法计划喂给门面，它只会按 `algorithm`（缺省
    # gp_symbolic）跑一遍，其余算法静默消失——没有报错、没有日志，结果看起来
    # 像"多算法搜索"。这里显式拒绝，别让两种形状在同一个函数里假装兼容。
    if isinstance(params.get("algorithms"), list):
        raise FlowError(
            E1004,
            "search.fit/generate 是单算法门面，不接受 params.algorithms（多算法请走 build）",
            path="search.params.algorithms",
            hint="多算法的计数与去重由 build 的 _algorithm_runs 统一处理；门面只服务"
                 "单算法的四方法契约调用",
        )
    from capability.mining import GPConfig, generate_gp
    algorithm = str(params.get("algorithm", "gp_symbolic"))
    if algorithm != "gp_symbolic":
        return {"algorithm": algorithm, "fitted": False, "reason": "stateless"}
    fields = tuple(ctx.get("fields", ())) if isinstance(ctx, Mapping) else ()
    impl = implementation_defaults("search")
    cfg = GPConfig(
        fields=fields or ("close",),
        population=int(params.get("population", impl["population"])),
        generations=int(params.get("generations", impl["generations"])),
        max_depth=int(params.get("max_depth", impl["max_depth"])),
        max_candidates=int(params.get("max_candidates", impl["max_candidates"])),
    )
    fitness = ctx.get("fitness") if isinstance(ctx, Mapping) else None
    # available_from 由上层经 ctx/view 注入（编译期固化的 as-of），不自取墙钟。
    as_of = str(ctx.get("available_from", "")) if isinstance(ctx, Mapping) else ""
    if fitness is not None and not _FIT_LOCK.acquire(blocking=False):
        raise FlowError(
            E2009, "检测到并发的自适应搜索（needs_fitness）",
            path="search.fit", hint="同一进程只允许一个带 fitness 的 fit 进行",
        )
    try:
        _, fitted = generate_gp(
            cfg=cfg, fields=cfg.fields, seed=int(seed), fitness=fitness,
            available_from=as_of,
        )
    finally:
        if fitness is not None:
            _FIT_LOCK.release()
    return fitted


def generate(view, params, seed, fitted_state):
    """契约四方法之一：生成最终种群，不评估、不筛选（02 §2）。

    直接复用 fit 落盘的 `final_genes`——旧实现忽略 fitted_state、另起随机种群，
    与 fit 分叉（review P1）。fitted_state 缺失时退化为一次性随机种群并显式
    标记，不冒充"接着 fit 的结果"。
    """
    # 门面只处理**单算法**：`fit` / `generate` 的参数形状是 `params.algorithm`
    # （扁平），而 `build` 用 `params.algorithms`（列表、多算法共享一次计数）。
    # 两者曾各认一套：把多算法计划喂给门面，它只会按 `algorithm`（缺省
    # gp_symbolic）跑一遍，其余算法静默消失——没有报错、没有日志，结果看起来
    # 像"多算法搜索"。这里显式拒绝，别让两种形状在同一个函数里假装兼容。
    if isinstance(params.get("algorithms"), list):
        raise FlowError(
            E1004,
            "search.fit/generate 是单算法门面，不接受 params.algorithms（多算法请走 build）",
            path="search.params.algorithms",
            hint="多算法的计数与去重由 build 的 _algorithm_runs 统一处理；门面只服务"
                 "单算法的四方法契约调用",
        )
    # 门面只处理**单算法**：`fit` / `generate` 的参数形状是 `params.algorithm`
    # （扁平），而 `build` 用 `params.algorithms`（列表、多算法共享一次计数）。
    # 两者曾各认一套：把多算法计划喂给门面，它只会按 `algorithm`（缺省
    # gp_symbolic）跑一遍，其余算法静默消失——没有报错、没有日志，结果看起来
    # 像"多算法搜索"。这里显式拒绝，别让两种形状在同一个函数里假装兼容。
    if isinstance(params.get("algorithms"), list):
        raise FlowError(
            E1004,
            "search.fit/generate 是单算法门面，不接受 params.algorithms（多算法请走 build）",
            path="search.params.algorithms",
            hint="多算法的计数与去重由 build 的 _algorithm_runs 统一处理；门面只服务"
                 "单算法的四方法契约调用",
        )
    from capability.mining import GPConfig, generate_gp
    impl = implementation_defaults("search")
    fields = tuple(view.get("fields", ())) if isinstance(view, Mapping) else ()
    as_of = str(view.get("available_from", "")) if isinstance(view, Mapping) else ""
    algorithm = str(params.get("algorithm", "gp_symbolic"))
    if algorithm != "gp_symbolic":
        cfg = GPConfig(
            fields=fields or ("close",),
            max_depth=int(params.get("max_depth", impl["max_depth"])),
            max_candidates=int(params.get("max_candidates", impl["max_candidates"])),
            genome_len=int(params.get("genome_len", impl["genome_len"])),
        )
        cands, state = generate_random(
            cfg=cfg, fields=cfg.fields, seed=int(seed), available_from=as_of,
        )
        state["resumed_from_fit"] = False
        return {"candidates": cands, "fitted_state": state}
    is_fitted = isinstance(fitted_state, Mapping) and bool(fitted_state.get("final_genes"))
    if is_fitted:
        fields = tuple(fitted_state.get("fields") or fields)
        cfg = GPConfig(
            fields=fields or ("close",),
            population=int(fitted_state.get("population", params.get("population", impl["population"]))),
            generations=0,  # 生成阶段不再评估、不进化
            max_depth=int(fitted_state.get("max_depth", params.get("max_depth", impl["max_depth"]))),
            max_candidates=int(params.get("max_candidates", impl["max_candidates"])),
        )
        cands, state = generate_gp(
            cfg=cfg, fields=cfg.fields, seed=int(seed), fitness=None,
            initial_genes=fitted_state["final_genes"], available_from=as_of,
        )
        state["resumed_from_fit"] = True
        return {"candidates": cands, "fitted_state": state}

    # 无 fitted_state：退化为一次性随机种群，不伪装成续接 fit。
    cfg = GPConfig(
        fields=fields or ("close",),
        population=int(params.get("population", impl["population"])),
        generations=0,
        max_depth=int(params.get("max_depth", impl["max_depth"])),
        max_candidates=int(params.get("max_candidates", impl["max_candidates"])),
    )
    cands, state = generate_gp(cfg=cfg, fields=cfg.fields, seed=int(seed),
                               fitness=None, available_from=as_of)
    state["resumed_from_fit"] = False
    return {"candidates": cands, "fitted_state": state}


def lineage(candidate: Mapping[str, Any]) -> dict:
    """契约四方法之一：血缘回溯，返回 parents/operators/generator。"""
    if not isinstance(candidate, Mapping):
        raise FlowError(E3003, "lineage 的 candidate 必须是映射", path="search.lineage")
    return {
        "parents": list(candidate.get("parents") or []),
        "operators": sorted(candidate.get("operators") or []),
        "generator": str(candidate.get("generator", "")),
        "generators": list(candidate.get("generators") or []),
    }


def _input(call, kind: str):
    for item in call.inputs:
        if item is not None and item.kind == kind:
            return item.data
    raise FlowError(E3001, f"search 缺输入 {kind!r}", path=f"search.inputs.{kind}")


def _panel(call) -> Panel:
    data = _input(call, "panel")
    if not isinstance(data, Panel):
        raise FlowError(E3003, "search 的 panel 输入必须是 Panel", path="search.inputs.panel")
    return data


def _label(call) -> Mapping[str, Any]:
    data = _input(call, "label")
    if not isinstance(data, Mapping) or not isinstance(data.get("score"), np.ndarray):
        raise FlowError(E3003, "search 的 label 输入必须带 score 数组", path="search.inputs.label")
    return data


def _make_fitness(call, panel: Panel, score: np.ndarray):
    """构造 fitness 闭包：调同一个 evaluate + 去重登记 + 返回 |RankIC|。

    eval 用 `runtime.run` 走与 Evidence 完全相同的六步校验（白名单、指纹、
    outputs），因此算法无法替换求值口径。子调用里没有测试段/保留区引用（02 §2.1）。
    """
    from capability.runtime import Call, InputRef, run

    registry = default_registry()
    # 实现版本必须与 impls.py 注册 evaluate 时用的完全一致。注册用的是
    # qweave 的**包版本**（内核语义），不是白名单摘要——传错会让 runtime.resolve
    # 抛 E1002，fitness 恒 -1，GP 静默退化为随机生成（review P0）。
    eval_version = _pkg_version("qweave")
    seen: set[str] = set()
    calls = {"total": 0, "ok": 0, "failed": 0}

    def fitness(candidate: Mapping[str, Any]) -> float:
        cid = candidate["candidate_id"]
        if cid not in seen:
            seen.add(cid)
        calls["total"] += 1
        ast = candidate["payload"]["expression"]
        try:
            eval_call = Call.create(
                capability="evaluate", station="Search", impl="qweave",
                impl_version=eval_version,
                params={
                    "operator_set": registry.operators_version,
                    "nan_policy": "propagate",
                    "node_cache": False,
                },
                inputs=(
                    InputRef("payload", "sha256:" + "0" * 64, data=ast),
                    InputRef("panel", panel.digest(), data=panel),
                ),
            )
            out = run(eval_call)
            values = out["values"]
            valid = out["valid_mask"]
        except FlowError:
            # 退化表达式（常数/全 NaN/非法算子）给最小适应度，让 GP 淘汰它；
            # 不把异常抛给算法，否则一次坏变异会中断整代（02 §2.1）。
            calls["failed"] += 1
            return -1.0
        calls["ok"] += 1
        mask = valid & np.isfinite(score)
        if not mask.any():
            return -1.0
        ic = ic_series(np.where(mask, values, np.nan), np.where(mask, score, np.nan))
        finite = ic[np.isfinite(ic)]
        if finite.size < 2:
            return -1.0
        # 目标只有 |RankIC|：不含 p 值、成本、判据（搜索空间污染防线）。
        return float(abs(np.mean(finite)))

    return fitness, seen, calls


# ---------------------------------------------------------------- 算法分派
# 分派键是**注册表里的算法名**，不是「stateful 与否」。旧实现按 `spec.stateful`
# 二选一，等于把「有 fitness」等同于「是 GP」：再加第三个算法时，它要么被当成
# GP 跑（参数全错），要么被当成随机跑（进化白做），而注册表里一切看起来正常。
# 现在每个算法各自一行：新增算法 = 加一个 runner + 注册表加一行，别处零改动。
_RUNNERS: dict[str, Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]] = {}


def _config(sub: Mapping[str, Any], defaults: Mapping[str, Any],
            fields: tuple[str, ...]) -> GPConfig:
    """算法参数 → GPConfig。

    查找顺序：算法显式值 → 算法 spec 的 defaults → `registry/params.json` 的
    `defaults.search`（E8/G5）。第三层曾在三处各抄一遍 20/3/4/40/7——改一处
    会让「同一个算法」在不同入口拿到不同种群，而产物还长得一样。
    """
    impl_defaults = implementation_defaults("search")

    def _get(key: str) -> int:
        value = sub.get(key, defaults.get(key, impl_defaults[key]))
        return int(value)

    cfg = GPConfig(
        fields=fields,
        population=_get("population"),
        generations=_get("generations"),
        max_depth=_get("max_depth"),
        max_candidates=_get("max_candidates"),
        genome_len=_get("genome_len"),
    )
    if cfg.population < 2 or cfg.generations < 0 or cfg.max_depth < 1:
        raise FlowError(
            E1004, "search 的 population>=2/generations>=0/max_depth>=1",
            path="search.params.algorithms",
        )
    return cfg


def _run_gp(*, sub, fields, seed, fitness, as_of, defaults):
    cfg = _config(sub, defaults, fields)
    return generate_gp(
        cfg=cfg, fields=fields, seed=seed, fitness=fitness,
        genes_limit=cfg.max_candidates, available_from=as_of,
    )


def _run_random(*, sub, fields, seed, fitness, as_of, defaults):
    _ = fitness  # 基线算法不读适应度：它必须与 GP 在同一空间上均匀采样
    cfg = _config(sub, defaults, fields)
    return generate_random(cfg=cfg, fields=fields, seed=seed, available_from=as_of)


def _run_template_enum(*, sub, fields, seed, fitness, as_of, defaults):
    _ = fitness  # 模板枚举是确定性结构先验，不读适应度
    cfg = _config(sub, defaults, fields)
    return generate_template_enum(
        cfg=cfg, fields=fields, seed=seed, available_from=as_of,
        windows=sub.get("windows"), templates=sub.get("templates"),
    )


_RUNNERS.update({
    "gp_symbolic": _run_gp,
    "random_expression": _run_random,
    "template_enum": _run_template_enum,
})


def build(call) -> dict:
    params = call.params
    runs = _algorithm_runs(params)
    algos = _algorithms()
    unknown = [name for name, _ in runs if name not in algos]
    if unknown:
        raise FlowError(
            E1004, f"未知算法 {unknown[0]!r}", path="search.params.algorithm",
            hint=f"已登记: {sorted(algos)}",
        )

    panel = _panel(call)
    label = _label(call)
    score = label["score"]

    fields = tuple(sorted(set(params.get("fields") or _panel_fields(panel))))
    if not fields:
        raise FlowError(E3003, "search 无法确定搜索字段集", path="search.params.fields")

    seed = int(call.seed or 0)
    # fitness/seen/calls 在所有算法之间**共享**：多算法一次计数（02 §2.2）。
    # 每个算法各自建一个闭包会让同一表达式被算两次、n_trials 虚高，
    # 而 DSR 的门槛正是 n_trials 的函数——那等于用记账错误去放宽判据。
    fitness, seen, calls = _make_fitness(call, panel, score)

    prior_cands, prior_skipped, as_of = _prior_candidates(call)

    gen_cands: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    defaults = {"max_candidates": int(params.get(
        "max_candidates", implementation_defaults("search")["max_candidates"]))}
    for name, sub in runs:
        spec = algos[name]
        runner = _RUNNERS.get(name)
        if runner is None:
            # 注册表有 spec、却没有 runner：fail-closed。放它过去只会得到
            # 「跑了一个什么都没产出的算法」这种看起来成功的假结果。
            raise FlowError(
                E3004, f"算法 {name!r} 已登记但没有分派实现",
                path="search.params.algorithms",
                hint=f"在 ops/search._RUNNERS 增一行；已接: {sorted(_RUNNERS)}",
            )
        if spec.needs_fitness:
            # 并发自适应检测：两个 needs_fitness 的算法同时进入 → E2009。
            # 同一算法内部的串行多代不受影响（锁粒度是「一次 fit」）。
            if not _FIT_LOCK.acquire(blocking=False):
                raise FlowError(
                    E2009, "检测到并发的自适应搜索（needs_fitness）",
                    path="search.fit", hint="同一进程只允许一个带 fitness 的 fit 进行",
                )
            try:
                cands, state = runner(
                    sub=sub, fields=fields, seed=seed, fitness=fitness,
                    as_of=as_of, defaults=defaults,
                )
            finally:
                _FIT_LOCK.release()
        else:
            cands, state = runner(
                sub=sub, fields=fields, seed=seed, fitness=fitness,
                as_of=as_of, defaults=defaults,
            )
        gen_cands.extend(cands)
        states.append(state)

    # fail-closed：声明 needs_fitness 却一次 evaluate 都没成功，说明求值口径
    # 接错了（如 impl_version 传白名单摘要触发 E1002）。此时所有适应度恒 -1、
    # GP 退化成随机，产物看起来"有候选"但搜索根本没发生——必须拒绝而不是
    # 返回一批假候选（review P0 的守卫）。
    if any(algos[name].needs_fitness for name, _ in runs) \
            and calls["total"] > 0 and calls["ok"] == 0:
        raise FlowError(
            E3003,
            f"search 的 {calls['total']} 次 evaluate 子调用全部失败（适应度恒 -1）",
            path="search.fitness",
            hint="检查 evaluate 子调用的 impl_version 是否与注册表一致",
        )

    all_cands = dedup_candidates(prior_cands + gen_cands)
    count = len(all_cands)
    if count < 1:
        raise FlowError(E3003, "search 没有产出任何候选", path="search.outputs.candidates")

    import hashlib
    import json
    dedup_text = json.dumps([c["candidate_id"] for c in all_cands],
                            separators=(",", ":"))
    dedup_digest = "sha256:" + hashlib.sha256(dedup_text.encode("utf-8")).hexdigest()

    # Budget 的真值：fitness 闭包里去重登记过的 candidate_id（含 fit 内评估、
    # 被淘汰的个体）与最终 CandidateSet 的并集去重。算法无法少记（不调 fitness
    # 就拿不到适应度），也不二次记账（同一 id 重复评估只记一次，02 §2.2）。
    trial_ids = set(seen) | {c["candidate_id"] for c in all_cands}
    n_trials = len(trial_ids)

    # 多算法的 fitted_state 保持逐算法可审计：单算法沿用扁平结构（generate
    # 门面要读 `final_genes`），多算法收进 `algorithms` 列表，不互相覆盖。
    fitted_state = dict(states[0]) if len(states) == 1 else {"algorithms": states}
    if prior_skipped:
        # 先验池的排除不是静默丢弃：名字与原因随产物带出，可事后核对
        # 「计划里写了 5 个、实际进池 2 个」这件事。
        fitted_state["prior_pool_skipped"] = prior_skipped
    if prior_cands:
        fitted_state["prior_pool_used"] = [c["generator"] for c in prior_cands]

    return {
        "candidates": all_cands,
        "family": str(params.get("family", implementation_defaults("cli")["family"])),
        "count": count,
        "dedup_digest": dedup_digest,
        "fitted_state_ref": "",  # S3 内存态；落盘引用由执行器按 fitted_state 生成
        "generators": sorted({g for c in all_cands for g in c.get("generators", [])}),
        "declared_at": "",  # 观测字段，进产物不进指纹
        # 求不出暴露的候选：搜索工位自己不调 evaluate（那会绕过 observe 的
        # Stage A 语义），这份清单由编排层在逐候选求值时回填；能力层只声明
        # 通道存在、默认空。缺了它，`count` 与 Evidence.per_candidate 的差额
        # 就只能是「看起来少了一些」，而不是「哪 8 个、为什么」。
        "unobservable": [],
        # 数据/记账通道（不进 schema，由执行器消费，不落盘为声明字段）
        "n_trials": n_trials,
        "fitted_state": fitted_state,
    }


def _prior_candidates(call) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    """读编排层已解析好的先验候选（`market_view.prior_candidates`）。

    `prior_pool` 在计划里只是名字列表（schema `type=ref`：只接受引用标识）。
    名字 → 表达式的解析发生在 `app/cli/live.py`（经 `capability.factors`），
    本函数只消费结果：能力层不读盘、不内联表达式，否则「口径只有一个来源」
    在搜索工位就破了。

    返回 `(候选, 被排除条目, as-of)`；`as_of` 是编译期固化的数据末端，
    由 `market_view.available_from` 注入，能力层不自取墙钟。
    """
    prior: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    mv = _input(call, "market_view")
    as_of = ""
    if not isinstance(mv, Mapping):
        return prior, skipped, as_of
    as_of = str(mv.get("available_from", ""))
    for ref in mv.get("prior_candidates") or []:
        if not isinstance(ref, Mapping) or ref.get("expression") is None:
            raise FlowError(
                E3003, "prior_candidates 条目缺 expression",
                path="search.inputs.market_view.prior_candidates",
            )
        prior.append(make_candidate(
            ref["expression"], source="prior",
            generator=str(ref.get("name", "prior")),
            required_fields=ref.get("required_fields"),
            available_from=as_of,
        ))
    for item in mv.get("prior_skipped") or []:
        if isinstance(item, Mapping) and item.get("name"):
            skipped.append({
                "name": str(item.get("name")),
                "reason": str(item.get("reason", "unknown")),
                "detail": str(item.get("detail", "")),
            })
    return prior, skipped, as_of


def _panel_fields(panel: Panel) -> set[str]:
    return set(panel.fields)
