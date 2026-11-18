"""S3-M3 · search 候选生成：四方法、candidate 身份、去重、fitness 记账、复现。

覆盖 AC20/22/37–39：
- AC22 跨算法合并去重、一次计数（count=实测去重数，n_trials 含 fit 内评估）；
- AC37 fitness 不含 p 值/成本/判据（search outputs 无任何结论字段）；
- AC38 试验不少记（n_trials >= 去重候选，fitness 内评估的都记）；
- AC39 同 seed 逐位一致、random.getstate 进出不变。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.mining import (
    candidate_id, dedup_candidates, make_candidate, normalise_ast,
)
from capability.panel import Panel
from capability.runtime import reset_impls
from contracts.errors import E1004, E2009, E3003, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _code(fn):
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


def _panel(t=40, n=6, seed=0):
    rng = np.random.default_rng(seed)
    times = START + np.arange(t, dtype="int64") * DAY_NS
    close = 100 + np.cumsum(rng.normal(size=(t, n)), axis=0)
    return Panel(times=times, symbols=tuple(f"S{j}" for j in range(n)),
                 fields={"close": close, "volume": rng.normal(size=(t, n)) + 20})


def _call(panel, *, seed=11, population=10, generations=2, prior=None, **extra):
    t = panel.shape[0]
    rng = np.random.default_rng(42)
    score = rng.normal(size=panel.shape)
    label = {"score": score, "valid_mask": np.isfinite(score), "effective_n": int(score.size)}
    params = {
        "algorithm": "gp_symbolic", "population": population,
        "generations": generations, "max_depth": 3, "max_candidates": 12,
        "fields": ["close", "volume"], "family": "f1",
    }
    view = {"mine_slice": [0, t]}
    if prior is not None:
        # prior_pool 参数是 ref 列表（schema type=ref）；内联候选由执行器解析后
        # 放进 market_view.prior_candidates。
        params["prior_pool"] = ["factor_db:close_raw"]
        view["prior_candidates"] = prior
    params.update(extra)
    return Call.create(
        capability="search", station="Search", impl="gp_symbolic",
        impl_version="1", params=params, seed=seed,
        inputs=(
            InputRef("panel", panel.digest(), data=panel),
            InputRef("label", "sha256:" + "1" * 64, data=label),
            InputRef("market_view", "sha256:" + "3" * 64, data=view),
        ),
    )


# --------------------------------------------------------------- 身份与规范化
def test_ast_normalisation_canonicalises_number_form():
    a = normalise_ast({"op": "mul", "args": [{"field": "close"}, {"const": 2.0}]})
    b = normalise_ast({"op": "mul", "args": [{"field": "close"}, {"const": 2}]})
    assert a == b


def test_ast_rejects_extra_keys_and_bool_const():
    with pytest.raises(ValueError):
        normalise_ast({"op": "add", "args": [], "comment": "x"})
    with pytest.raises(ValueError):
        normalise_ast({"const": True})


def test_candidate_has_no_metrics_or_verdict_fields():
    c = make_candidate({"field": "close"})
    banned = {"ic", "rank_ic", "p_value", "t_stat", "dsr", "decision", "cost", "significant"}
    assert not (set(c) & banned), set(c) & banned


def test_cross_source_dedup_merges_generators():
    c1 = make_candidate({"field": "close"}, generator="gp@1")
    c2 = make_candidate({"field": "close"}, generator="random@1")
    merged = dedup_candidates([c1, c2])
    assert len(merged) == 1
    assert set(merged[0]["generators"]) == {"gp@1", "random@1"}


# --------------------------------------------------------------- 四方法
def test_four_methods_describe_and_lineage():
    from capability.ops import search as s
    spec = s.describe()["gp_symbolic"]
    assert spec["deterministic"] is True and spec["needs_seed"] is True
    assert spec["fitness_objectives"] == ["abs_rank_ic"]
    lin = s.lineage({"parents": ["p"], "operators": ["mul", "add"],
                     "generator": "gp@1"})
    assert lin == {"parents": ["p"], "operators": ["add", "mul"],
                   "generator": "gp@1", "generators": []}


def test_fit_and_generate_are_deterministic():
    from capability.ops import search as s
    ctx = {"fields": ["close"], "fitness": lambda c: 1.0}
    f1 = s.fit(ctx, {"population": 6, "generations": 1, "max_depth": 3}, 5)
    f2 = s.fit(ctx, {"population": 6, "generations": 1, "max_depth": 3}, 5)
    assert f1 == f2
    g1 = s.generate({"fields": ["close"]}, {"max_candidates": 6}, 5, f1)
    g2 = s.generate({"fields": ["close"]}, {"max_candidates": 6}, 5, f2)
    assert [c["candidate_id"] for c in g1["candidates"]] == \
           [c["candidate_id"] for c in g2["candidates"]]


# --------------------------------------------------------------- build 契约
def test_search_build_count_and_dedup():
    panel = _panel()
    out = run(_call(panel))
    assert out["count"] == len(out["candidates"])
    assert out["count"] >= 1
    ids = [c["candidate_id"] for c in out["candidates"]]
    assert len(set(ids)) == len(ids)  # 去重后无重复
    assert out["n_trials"] >= len(ids)  # 不少记（fit 内评估也计入）
    assert out["dedup_digest"].startswith("sha256:")


def test_search_reproducible_and_global_rng_untouched():
    panel = _panel()
    state = random.getstate()
    out1 = run(_call(panel, seed=17))
    out2 = run(_call(panel, seed=17))
    assert [c["candidate_id"] for c in out1["candidates"]] == \
           [c["candidate_id"] for c in out2["candidates"]]
    assert random.getstate() == state  # AC39
    out3 = run(_call(panel, seed=18))
    assert [c["candidate_id"] for c in out1["candidates"]] != \
           [c["candidate_id"] for c in out3["candidates"]]  # 换 seed 改变结果


def test_search_no_conclusion_fields():
    panel = _panel()
    out = run(_call(panel))
    # outputs schema 已由 validate_outputs 锁死；这里再确认无结论词。
    banned = {"decision", "p_value", "dsr", "significant", "verdict"}
    assert not (set(out) & banned)


def test_search_prior_pool_dedup_with_generated():
    panel = _panel()
    # 先验池放一个纯 field 候选；生成种群里若撞车应去重且 generator 合并。
    prior = [{"name": "close_raw", "expression": {"field": "close"},
              "required_fields": ["close"]}]
    out = run(_call(panel, prior=prior))
    pids = [c["candidate_id"] for c in out["candidates"]]
    assert len(set(pids)) == len(pids)


def test_search_unknown_algorithm_rejected():
    panel = _panel()
    assert _code(lambda: run(_call(panel, algorithm="not_registered"))) == E1004


def test_every_registered_algorithm_has_a_dispatcher():
    """注册表与分派表必须同集合。

    旧实现按 `spec.stateful` 二选一，注册表里多一个算法不会报错，只会被当成
    GP 或随机跑——产物看起来正常，参数却全错。这条守卫让「登记了但没接分派」
    在测试期就暴露。
    """
    from capability.ops import search as search_op
    assert set(search_op._algorithms()) == set(search_op._RUNNERS)


def test_dispatch_is_by_name_not_by_stateful_flag():
    """把 random_expression 的 runner 换掉时，GP 那条路径不受影响。"""
    from capability.ops import search as search_op

    calls = {"random": 0}
    original = search_op._RUNNERS["random_expression"]

    def spy(**kwargs):
        calls["random"] += 1
        return original(**kwargs)

    search_op._RUNNERS["random_expression"] = spy
    try:
        panel = _panel()
        call = _call(panel, algorithms=[
            {"name": "gp_symbolic", "params": {"max_candidates": 3}},
            {"name": "random_expression", "params": {"max_candidates": 3}},
        ])
        out = run(call)
    finally:
        search_op._RUNNERS["random_expression"] = original
    assert calls["random"] == 1
    assert out["count"] >= 1


def test_search_concurrent_fit_is_e2009():
    panel = _panel()
    from capability.mining import _FIT_LOCK
    assert _FIT_LOCK.acquire(blocking=False)
    try:
        assert _code(lambda: run(_call(panel))) == E2009
    finally:
        _FIT_LOCK.release()


def test_search_empty_panel_fields_uses_defaults():
    panel = _panel()
    out = run(_call(panel, fields=["close"]))
    assert out["count"] >= 1


# --------------------------------------------------------------- review 守卫
def test_fitness_evaluate_subcalls_actually_succeed():
    """P0：fitness 的 evaluate 子调用必须用 qweave 实现版本，否则全部 E1002、
    适应度恒 -1、GP 退化为随机。用打点包装 runtime.run 统计子调用结果。"""
    from importlib.metadata import version as pkg_version
    import capability.runtime as rt
    from capability.ops import search as s

    seen_versions, outcomes = [], []
    real_run = rt.run

    def spy(call):
        if call.capability == "evaluate":
            seen_versions.append(call.impl_version)
            try:
                result = real_run(call)
                outcomes.append("ok")
                return result
            except FlowError as exc:
                outcomes.append(exc.error.code)
                raise
        return real_run(call)

    # _make_fitness 内部 `from ... import run`，需替换模块属性
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(rt, "run", spy)
        panel = _panel(t=30, n=5)
        fitness, _, calls = s._make_fitness(
            None, panel, np.random.default_rng(1).normal(size=panel.shape))
        val = fitness(make_candidate({"field": "close"}))
        assert val > -1.0, "合法候选的适应度不应恒为 -1"
    finally:
        mp.undo()
    assert outcomes, "没有发生 evaluate 子调用"
    assert all(v == pkg_version("qweave") for v in seen_versions), seen_versions
    assert "ok" in outcomes, f"evaluate 子调用全部失败: {outcomes}"
    assert calls["ok"] > 0 and calls["failed"] == 0, calls


def test_param_position_rejects_float_const():
    """P1：带参算子的末位参数必须是整数 const；值位置 2.0 仍合法且归一同 id。"""
    # 参数位置：2.0 必须拒绝
    with pytest.raises(ValueError):
        normalise_ast({"op": "mean", "args": [{"field": "close"}, {"const": 2.0}]})
    with pytest.raises(ValueError):
        normalise_ast({"op": "mean", "args": [{"field": "close"}, {"field": "volume"}]})
    with pytest.raises(ValueError):
        normalise_ast({"op": "mean", "args": [{"field": "close"}, {"const": True}]})
    # 整数参数合法
    ok = normalise_ast({"op": "mean", "args": [{"field": "close"}, {"const": 2}]})
    assert ok["args"][-1] == {"const": 2}


def test_nested_depth_limit_enforced():
    """02 §6：嵌套深度上限 64，超过必须拒绝（而非让递归栈崩在 evaluate）。"""
    node = {"field": "close"}
    for _ in range(66):
        node = {"op": "abs", "args": [node]}
    with pytest.raises(ValueError):
        normalise_ast(node)


def test_max_depth_actually_constrains_candidates():
    """P1：max_depth 必须真正约束搜索空间，不能只是常量 16 兜底。"""
    from capability.mining import GPConfig, generate_gp, _OpCodes
    from capability.registry import default_registry

    def depth(ast):
        if "op" not in ast:
            return 1
        return 1 + max(depth(a) for a in ast["args"])

    cfg = GPConfig(fields=("close",), population=20, generations=2, max_depth=2)
    cands, state = generate_gp(cfg=cfg, fields=cfg.fields, seed=3,
                              fitness=lambda c: 0.5)
    assert cands, "不应因深度约束丢光候选"
    assert state["dropped_decode"] == 0, state
    assert max(depth(c["payload"]["expression"]) for c in cands) <= 2


def test_generate_reuses_fit_final_genes():
    """P1：generate 门面必须复用 fit 落盘的最终基因，不得另起随机种群。"""
    from capability.ops import search as s
    ctx = {"fields": ["close"], "fitness": lambda c: 0.5}
    params = {"population": 8, "generations": 2, "max_depth": 3}
    fitted = s.fit(ctx, params, seed=23)
    gen = s.generate({"fields": ["close"]}, params, seed=23, fitted_state=fitted)
    assert gen["fitted_state"]["resumed_from_fit"] is True
    assert gen["fitted_state"]["final_genes"] == fitted["final_genes"]
    # 复用基因后，generate 的候选身份应与 fit 落盘的基因逐条对应（不复现随机）
    assert len(gen["candidates"]) == len(fitted["final_genes"])
    # 复核 1：final_genes 与 candidates 严格一一对齐，基因序列不被丢弃
    assert gen["fitted_state"]["dropped_decode"] == 0, gen["fitted_state"]


def test_available_from_propagates_to_candidates():
    """复核 4：available_from 必须经 build/fit/generate 透传到每个 Candidate。"""
    from capability.ops import search as s
    panel = _panel(t=30, n=5)
    out = run(_call(panel))
    # _call 未注入 as-of：默认空串
    assert all(c["available_from"] == "" for c in out["candidates"])
    # 注入已固化的 as-of（编译期数据末端，非墙钟）
    call = _call(panel)
    mv = next(i for i in call.inputs if i.kind == "market_view")
    from dataclasses import replace
    mv2 = replace(mv, data={**mv.data, "available_from": "2024-02-01T00:00:00Z"})
    inputs = tuple(mv2 if i is mv else i for i in call.inputs)
    call2 = Call.create(
        capability="search", station="Search", impl="gp_symbolic", impl_version="1",
        params=call.params, seed=call.seed, inputs=inputs,
    )
    out2 = run(call2)
    assert all(c["available_from"] == "2024-02-01T00:00:00Z"
               for c in out2["candidates"])
    # available_from 不进内容身份：同表达式不同 as-of 的 candidate_id 应相同
    assert [c["candidate_id"] for c in out["candidates"]] == \
           [c["candidate_id"] for c in out2["candidates"]]


def test_describe_exposes_compile_time_fields():
    """P2：AlgorithmSpec 必须含 02 §3 的编译期字段，不是只有运行时字段。"""
    from capability.ops import search as s
    spec = s.describe()["gp_symbolic"]
    for key in ("fit_window", "output_kind", "required_fields",
                "operator_whitelist", "max_candidates", "budget_group"):
        assert key in spec, key
    assert spec["fit_window"] == "mine_only"
    assert spec["budget_group"] == "inherit"
    assert spec["operator_whitelist"]  # 非空白名单


def test_malformed_const_raises_value_error():
    """P2-e：畸形 const（bool/字符串）应被 AST 规范化拒绝（ValueError，
    evaluate 层据此转 E1003），而不是裸 TypeError 或静默接受。"""
    with pytest.raises(ValueError):
        normalise_ast({"const": True})
    with pytest.raises(ValueError):
        normalise_ast({"const": "x"})


def test_candidate_id_excludes_generator_and_seed():
    ast = {"field": "close"}
    c1 = make_candidate(ast, generator="gp@1", seed=1)
    c2 = make_candidate(ast, generator="random@2", seed=99)
    assert c1["candidate_id"] == c2["candidate_id"]
    # 谱系元数据不带进内容身份：两者的 payload + required_fields 必须相同
    assert c1["payload"] == c2["payload"]
    assert c1["required_fields"] == c2["required_fields"]



def test_param_ops_come_from_the_registry():
    """G6：带参算子的生成集合必须覆盖注册表里声明过的全部带参算子。

    `corr` / `cov` 在 operators.json 里有 impl_symbol 与窗口域，旧生成器却
    只手写 7 个名字，于是它们**永远搜不到**——声明与生成分叉，且没有任何
    检查点会报错。
    """
    from capability.mining import GPConfig, _OpCodes
    from capability.registry import default_registry

    reg = default_registry()
    codes = _OpCodes(("close", "volume"), reg, GPConfig(fields=("close",)))
    # `ref` / `delay` 是同一个 impl_symbol 的两个名字：不必生成两遍等价表达式，
    # 所以按 impl_symbol 比对而不是按名字。
    generated = {reg.operators[name]["impl_symbol"] for name, _, _ in codes.ops
                 if name in reg.operators}
    declared = {spec["impl_symbol"] for spec in reg.operators.values()
                if spec.get("param_domain")}
    assert declared <= generated, f"注册表声明但生成器永不产出: {declared - generated}"
    assert {"correlation", "covariance"} <= generated


def test_ternary_param_op_decodes_to_registry_arity():
    """注册表 arity **含**末位窗口；decode 又追加一个 const，不能重复计数。"""
    from capability.mining import GPConfig, _OpCodes
    from capability.registry import default_registry

    reg = default_registry()
    codes = _OpCodes(("close", "volume"), reg, GPConfig(fields=("close",)))
    idx = next(i for i, (name, _, _) in enumerate(codes.ops) if name == "corr")
    gene = codes.n_terminal + idx
    ast = codes.decode([0, 1, gene])
    assert ast["op"] == "corr"
    assert len(ast["args"]) == 3
    assert isinstance(ast["args"][-1], dict) and "const" in ast["args"][-1]

def test_facade_rejects_multi_algorithm_params():
    """`fit` / `generate` 是单算法门面，不得静默吞掉多算法计划。

    参数形状有两套：门面认 `params.algorithm`（扁平），`build` 认
    `params.algorithms`（列表，多算法共享一次计数）。曾各认一套——把多算法计划
    喂给门面，它只会按缺省的 gp_symbolic 跑一遍，其余算法**静默消失**：没有报错、
    没有日志，产物看起来像"多算法搜索"。现在显式 `E1004`。
    """
    from capability.ops import search as s
    from contracts.errors import E1004, FlowError

    multi = {"algorithms": [{"name": "gp_symbolic", "params": {}},
                            {"name": "random_expression", "params": {}}]}
    for fn, args in ((s.fit, ({"fields": ("close",)}, multi, 7)),
                     (s.generate, ({"fields": ("close",)}, multi, 7, None))):
        try:
            fn(*args)
        except FlowError as exc:
            assert exc.error.code == E1004, fn.__name__
        else:
            raise AssertionError(f"{fn.__name__} 未拒绝多算法参数")


def test_template_enum_algorithm():
    """template_enum：确定性经济锚点 × 窗口网格，注册/分派/字段门控一致。"""
    from capability.mining import GPConfig, generate_template_enum
    from capability.ops import search as s

    specs = s.describe()
    assert "template_enum" in specs
    assert specs["template_enum"]["needs_fitness"] is False
    assert specs["template_enum"]["deterministic"] is True
    assert set(s._algorithms()) == set(s._RUNNERS)

    fields = ("close", "funding_rate", "volume", "basis")
    cfg = GPConfig(fields=fields, max_candidates=100)
    cands, state = generate_template_enum(cfg=cfg, fields=fields, seed=7)
    assert len(cands) >= 20
    assert state["algorithm"] == "template_enum"
    names = {c["generator"] for c in cands}
    assert any("mom" in n for n in names)
    assert any("funding_carry" in n for n in names)
    # 无 funding 字段时不得产出 funding 模板
    c2, _ = generate_template_enum(
        cfg=GPConfig(fields=("close",), max_candidates=100),
        fields=("close",), seed=7,
    )
    assert all("funding" not in c["generator"] for c in c2)
    assert all("basis" not in c["generator"] for c in c2)
    # 确定性：同参数逐位一致（与 seed 无关）
    c3, _ = generate_template_enum(cfg=cfg, fields=fields, seed=999)
    assert [c["candidate_id"] for c in cands] == [c["candidate_id"] for c in c3]


def test_template_enum_algorithm():
    """template_enum：确定性经济锚点 × 窗口网格，注册/分派/字段门控一致。"""
    from capability.mining import GPConfig, generate_template_enum
    from capability.ops import search as s

    specs = s.describe()
    assert "template_enum" in specs
    assert specs["template_enum"]["needs_fitness"] is False
    assert specs["template_enum"]["deterministic"] is True
    assert set(s._algorithms()) == set(s._RUNNERS)

    fields = ("close", "funding_rate", "volume", "basis")
    cfg = GPConfig(fields=fields, max_candidates=100)
    cands, state = generate_template_enum(cfg=cfg, fields=fields, seed=7)
    assert len(cands) >= 20
    assert state["algorithm"] == "template_enum"
    names = {c["generator"] for c in cands}
    assert any("mom" in n for n in names)
    assert any("funding_carry" in n for n in names)
    # 无 funding 字段时不得产出 funding 模板
    c2, _ = generate_template_enum(
        cfg=GPConfig(fields=("close",), max_candidates=100),
        fields=("close",), seed=7,
    )
    assert all("funding" not in c["generator"] for c in c2)
    assert all("basis" not in c["generator"] for c in c2)
    # 确定性：同参数逐位一致（与 seed 无关）
    c3, _ = generate_template_enum(cfg=cfg, fields=fields, seed=999)
    assert [c["candidate_id"] for c in cands] == [c["candidate_id"] for c in c3]
