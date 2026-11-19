"""v3 算子扩表（34 → 46）：补齐 qweave 0.6.2 仍暴露、v2 漏掉的原语。

与 v2 一样是**语义级对拍**：`ts_rank_raw` 是计数名次、`ts_count` 在 NaN 上的
口径、`quantile` 的两个参数位（window 是整数、q 是 [0,1] 实数）、比较算子返回
0/1 浮点而不是布尔——这些都写死在手算值上，不用 `assert finite` 搪塞。
"""
from __future__ import annotations

from importlib.metadata import version as pkg_version

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.factors import _check_ast
from capability.impls import install_default_impls
from capability.mining import normalise_ast
from capability.panel import Panel
from capability.registry import default_registry
from capability.runtime import reset_impls
from contracts.errors import E1003, E1004, E1015, E3003, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
KERNEL = pkg_version("qweave")

X = np.array([3.0, 1.0, 2.0, 5.0, 4.0, 6.0, 7.0])
Y = X[::-1].copy()


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _panel(a: np.ndarray, b: np.ndarray | None = None) -> Panel:
    right = Y if b is None else b
    n = a.size
    times = START + np.arange(n, dtype="int64") * DAY_NS
    return Panel(times=times, symbols=("A", "B"),
                 fields={"x": np.column_stack([a, right])})


def _code(fn) -> str:
    with pytest.raises(FlowError) as exc:
        fn()
    return exc.value.error.code


def _eval(ast, panel: Panel) -> np.ndarray:
    call = Call.create(
        capability="evaluate", station="Evidence", impl="qweave",
        impl_version=KERNEL,
        params={
            "operator_set": default_registry().operators_version,
            "nan_policy": "propagate",
            "node_cache": False,
        },
        inputs=(
            InputRef("payload", "sha256:" + "0" * 64, data=ast),
            InputRef("panel", panel.digest(), data=panel),
        ),
    )
    return run(call)["values"][:, 0]


def _f(name: str = "x") -> dict:
    return {"field": name}


def _c(value) -> dict:
    return {"const": value}


def _op(name: str, *args) -> dict:
    return {"op": name, "args": list(args)}


# ---------------------------------------------------------------- 双参数位
def test_quantile_is_window_percentile_with_two_params():
    """`quantile(x, 3, 0.5)`：窗口 [3,1,2] 的中位数是 2，不是均值也不是末值。"""
    out = _eval(_op("quantile", _f(), _c(3), _c(0.5)), _panel(X))
    assert np.isnan(out[:2]).all()
    expected = [np.median(X[t - 2:t + 1]) for t in range(2, X.size)]
    assert out[2:] == pytest.approx(expected)


def test_quantile_endpoints_and_fraction_domain():
    """q=0 / q=1 合法（min/max）；q>1、q 是字符串、窗口是浮点都要编译期拒。"""
    panel = _panel(X)
    assert _eval(_op("quantile", _f(), _c(3), _c(0.0)), panel)[2] == pytest.approx(1.0)
    assert _eval(_op("quantile", _f(), _c(3), _c(1.0)), panel)[2] == pytest.approx(3.0)
    assert _code(lambda: _eval(_op("quantile", _f(), _c(3), _c(1.5)), panel)) == E1004
    assert _code(lambda: _eval(_op("quantile", _f(), _c(3), _c("0.5")), panel)) == E1004
    assert _code(lambda: _eval(_op("quantile", _f(), _c(2.5), _c(0.5)), panel)) == E1004


def test_multi_param_ast_is_accepted_by_normaliser_and_builtin_check():
    reg = default_registry()
    ast = _op("quantile", _f("close"), _c(3), _c(0.5))
    assert normalise_ast(ast) == ast
    assert _check_ast(ast, reg, "test") == {"close"}
    bad = _op("quantile", _f("close"), _c(3), _c(2.0))
    with pytest.raises(ValueError):
        normalise_ast(bad)
    with pytest.raises(FlowError) as exc:
        _check_ast(bad, reg, "test")
    assert exc.value.error.code == E1015
    # 单参数窗口算子仍必须拒绝 2.5：放宽 fraction 不能顺手放宽 count。
    with pytest.raises(ValueError):
        normalise_ast(_op("mean", _f("close"), _c(2.5)))


# ---------------------------------------------------------------- 时序族补齐
def test_ts_rank_raw_is_count_rank_and_ts_rank_is_normalised():
    """无并列时 `ts_rank = (ts_rank_raw + 1) / window`，方向从窗口起点数。"""
    panel = _panel(X)
    raw = _eval(_op("ts_rank_raw", _f(), _c(3)), panel)
    pct = _eval(_op("ts_rank", _f(), _c(3)), panel)
    assert raw[2:] == pytest.approx([1.0, 2.0, 1.0, 2.0, 2.0])
    assert pct[2:] == pytest.approx((raw[2:] + 1.0) / 3.0)


def test_ts_count_excludes_nan_but_keeps_full_window_requirement():
    """缺失值不计入，整窗有NaN才算；全有限窗窗口内有一个缺失 → count=2。"""
    a = np.array([3.0, np.nan, 2.0, 5.0, 4.0, np.nan, 6.0, 7.0])
    b = np.array([1.0, 2.0, 3.0, 4.0, np.nan, 6.0, 7.0, 8.0])
    out = _eval(_op("ts_count", _f(), _c(3)), _panel(a, b))
    assert np.isnan(out[:2]).all()
    assert out[2:] == pytest.approx([2.0, 2.0, 3.0, 2.0, 2.0, 2.0])


# ---------------------------------------------------------------- 逐点/条件族
def test_elem_max_min_are_pointwise_not_rolling():
    panel = _panel(X)
    assert _eval(_op("elem_max", _f(), _c(2.5)), panel) == pytest.approx(
        np.maximum(X, 2.5))
    assert _eval(_op("elem_min", _f(), _c(2.5)), panel) == pytest.approx(
        np.minimum(X, 2.5))


def test_comparisons_return_float_flags():
    panel = _panel(X)
    thr = 3.0
    cases = {
        "gt": X > thr, "lt": X < thr, "ge": X >= thr,
        "le": X <= thr, "eq": X == thr, "ne": X != thr,
    }
    for name, expected in cases.items():
        out = _eval(_op(name, _f(), _c(thr)), panel)
        assert out.dtype.kind == "f"
        assert out == pytest.approx(expected.astype(float))


def test_where_selects_branches_from_comparison():
    panel = _panel(X)
    ast = _op("where_", _op("gt", _f(), _c(3.0)), _f(), _op("neg", _f()))
    assert _eval(ast, panel) == pytest.approx(np.where(X > 3.0, X, -X))


def test_ternary_arity_is_enforced():
    assert _code(lambda: _eval(_op("where_", _f(), _f()), _panel(X))) == E1003
    assert _code(lambda: _eval(_op("quantile", _f(), _c(3)), _panel(X))) == E1003


def test_near_constant_exposure_is_rejected():
    """100 格里只有 1 个 1 的等值因子不是信号，是单点 artifact。

    campaign_r1 曾放行 `eq(2, ts_mad(low,6))`：53010 个有效格只有 1 个触发，
    却因为极差非零 + HAC t=4.1 通过全部统计闸，certify 报 member_count=1。
    这类暴露必须在 evaluate 入口 fail-closed。
    """
    a = np.ones(300, dtype=float)
    a[150] = 2.0
    panel = Panel(times=START + np.arange(300, dtype="int64") * DAY_NS,
                  symbols=("A",), fields={"x": a.reshape(-1, 1)})
    assert _code(lambda: _eval(_op("eq", _f(), _c(2.0)), panel)) == E3003


def test_high_tie_share_exposure_is_rejected():
    """r2 的 `ts_min(ts_rank_raw(open,6),6)` 有 93% 的 0，OOS 只持 1 个名字。"""
    a = np.zeros(300, dtype=float)
    a[::3] = (np.arange(100) % 5) + 1.0  # 1/3 非零，但任意 3 根窗口都含 0
    panel = Panel(times=START + np.arange(300, dtype="int64") * DAY_NS,
                  symbols=("A",), fields={"x": a.reshape(-1, 1)})
    mean_out = _eval(_op("mean", _f(), _c(3)), panel)
    assert np.isfinite(mean_out[2:]).all()  # 对照组：普通滚动均值不该被挡
    assert _code(lambda: _eval(_op("min", _f(), _c(3)), panel)) == E3003


# ---------------------------------------------------------------- 注册表一致性
def test_v3_operators_are_declared_with_expected_shapes():
    reg = default_registry()
    windowed = ("ts_count", "ts_rank_raw")
    for name in windowed:
        spec = reg.operators[name]
        assert list(spec["param_domain"]) == ["window"]
        assert spec["since"] == "v3"
        assert spec["impl_symbol"] == name
    q = reg.operators["quantile"]
    assert list(q["param_domain"]) == ["window", "q"]
    assert q["param_domain"]["q"]["kind"] == "fraction"
    assert q["arity"] == 3
    for name in ("elem_max", "elem_min", "gt", "lt", "ge", "le", "eq", "ne"):
        assert reg.operators[name]["param_domain"] == {}
        assert reg.operators[name]["since"] == "v3"
    assert reg.operators["where_"]["arity"] == 3
    assert reg.operators["elem_max"]["impl_symbol"] == "max"


def test_semantic_mutation_is_deterministic_and_domain_safe():
    from capability.mining import generate_semantic_mutation

    fields = ("close", "high", "low", "volume", "vwap",
              "funding_rate", "basis", "open_interest")
    a, sa = generate_semantic_mutation(fields=fields, seed=7, max_candidates=12)
    b, _ = generate_semantic_mutation(fields=fields, seed=7, max_candidates=12)
    assert [c["candidate_id"] for c in a] == [c["candidate_id"] for c in b]
    assert sa["emitted"] == len(a) == 12
    reg = default_registry()
    for c in a:
        assert set(c["operators"]) <= set(reg.operators)
        assert normalise_ast(c["payload"]["expression"]) == c["payload"]["expression"]


def test_factor_blend_is_deterministic_and_domain_safe():
    from capability.mining import generate_factor_blend, normalise_ast

    fields = ("close", "high", "low", "volume", "vwap",
              "funding_rate", "basis", "open_interest")
    a, sa = generate_factor_blend(fields=fields, seed=11, max_candidates=12)
    b, _ = generate_factor_blend(fields=fields, seed=11, max_candidates=12)
    assert [c["candidate_id"] for c in a] == [c["candidate_id"] for c in b]
    assert sa["emitted"] == len(a) == 12
    assert sa["possible_pairs"] > 0
    reg = default_registry()
    ops_seen = set()
    for c in a:
        assert set(c["operators"]) <= set(reg.operators)
        assert normalise_ast(c["payload"]["expression"]) == c["payload"]["expression"]
        ops_seen.add(c["params_snapshot"]["blend_op"])
    assert len(ops_seen) > 1  # 预算再小也要跨多种配对算子


def test_semantic_mutation_budget_covers_every_builtin_anchor():
    """小预算也必须覆盖全部内置锚点，不能只搜字典序前缀。"""
    from capability.factors import BUILTIN_FACTORS
    from capability.mining import generate_semantic_mutation

    fields = ("close", "high", "low", "volume", "vwap",
              "funding_rate", "basis", "open_interest")
    cands, state = generate_semantic_mutation(
        fields=fields, seed=7, max_candidates=len(BUILTIN_FACTORS))
    bases = {c["params_snapshot"]["base"] for c in cands}
    assert bases == set(BUILTIN_FACTORS)
    assert state["emitted"] == len(BUILTIN_FACTORS)


def test_factor_blend_budget_covers_every_anchor():
    """小预算必须覆盖全部 6 个经济锚点，不能全落在 low_vol20。"""
    from capability.mining import generate_factor_blend, _BLEND_ANCHORS

    fields = ("close", "high", "low", "volume", "vwap",
              "funding_rate", "basis", "open_interest")
    cands, state = generate_factor_blend(fields=fields, seed=11, max_candidates=24)
    anchors = {c["params_snapshot"]["anchor"] for c in cands}
    assert anchors == set(_BLEND_ANCHORS)
    assert set(state["anchors"]) == set(_BLEND_ANCHORS)


def test_semantic_mutation_smooth_wraps_rolling_mean():
    """smooth 变异在 AST 外层套 mean(ast, w)，w 只能取域内窗口。"""
    from capability.mining import generate_semantic_mutation

    fields = ("close", "high", "low", "volume", "vwap",
              "funding_rate", "basis", "open_interest")
    cands, _ = generate_semantic_mutation(
        fields=fields, seed=7, max_candidates=200, mutations=["smooth"])
    assert cands
    seen_w = set()
    for c in cands:
        expr = c["payload"]["expression"]
        assert expr["op"] == "mean" and expr["args"][0]["op"] in default_registry().operators
        w = expr["args"][1]["const"]
        assert 1 <= w <= 512
        seen_w.add(w)
    assert len(seen_w) >= 2


def test_registry_rejects_unknown_param_kind(tmp_path):
    """`unknown` 不是参数类型：悄悄按 count 处理会让 q=0.5 在装载期穿透。"""
    import json
    from pathlib import Path

    import capability
    from capability.registry import load_registry

    registry_dir = Path(capability.__file__).parent / "registry"
    doc = json.loads((registry_dir / "operators.json").read_text(encoding="utf-8"))
    bad = next(op for op in doc["operators"] if op["name"] == "quantile")
    bad["param_domain"]["q"]["kind"] = "unknown"
    (tmp_path / "operators.json").write_text(json.dumps(doc), encoding="utf-8")
    for fname in ("fields.json", "params.json"):
        (tmp_path / fname).write_text(
            (registry_dir / fname).read_text(encoding="utf-8"), encoding="utf-8")
    assert _code(lambda: load_registry(tmp_path)) == E1015
