"""先验因子池 + 多算法搜索：接通语义与 fail-closed 边界。

覆盖本轮补上的四个缺口（此前都是「计划里写了、实际没执行」）：

- `search.prior_pool` 是死键：live 展开后 `prior_candidates` 恒为空；
- `search.algorithms` 只取第一个，第二个算法静默丢弃；
- `plan.impl` 写错名字被静默替换成唯一注册实现；
- 内置因子清单不存在，`mom60` 这类已知答案无法进池做锚点。
"""

from __future__ import annotations

import numpy as np
import pytest

from v5.capability import Call, InputRef
from v5.capability import factors
from v5.capability.impls import install_default_impls
from v5.capability.mining import GPConfig, generate_random
from v5.capability.ops import search as search_op
from v5.capability.panel import Panel
from v5.capability.runtime import reset_impls
from v5.capability import run as cap_run
from v5.contracts.errors import E1004, E1015, FlowError

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


def _panel(t=40, n=6, seed=0, fields=None):
    rng = np.random.default_rng(seed)
    times = START + np.arange(t, dtype="int64") * DAY_NS
    close = 100 + np.cumsum(rng.normal(size=(t, n)), axis=0)
    cols = {
        "close": close,
        "volume": rng.normal(size=(t, n)) + 20,
        "high": close + 1.0,
        "low": close - 1.0,
        "vwap": close * 0.999,
    }
    if fields:
        cols = {k: v for k, v in cols.items() if k in fields}
    return Panel(times=times, symbols=tuple(f"S{j}" for j in range(n)), fields=cols)


def _call(panel, *, params, seed=11, prior_candidates=(), prior_skipped=()):
    score = np.random.default_rng(42).normal(size=panel.shape)
    label = {"score": score, "valid_mask": np.isfinite(score),
             "effective_n": int(score.size)}
    view = {
        "mine_slice": [0, panel.shape[0]],
        "available_from": "2024-02-10",
        "prior_candidates": list(prior_candidates),
        "prior_skipped": list(prior_skipped),
    }
    return Call.create(
        capability="search", station="Search", impl="gp_symbolic",
        impl_version="1", params=params, seed=seed,
        inputs=(
            InputRef("panel", panel.digest(), data=panel),
            InputRef("label", "sha256:" + "1" * 64, data=label),
            InputRef("market_view", "sha256:" + "3" * 64, data=view),
        ),
    )


# ------------------------------------------------------------ 内置因子池
def test_builtin_pool_self_checks():
    info = factors.validate_all()
    assert info["count"] == len(factors.BUILTIN_FACTORS)
    # ACCEPTANCE 的已知答案因子必须在池里，否则锚点不存在
    for name in ("mom60", "rev5", "rv20", "funding_carry"):
        assert name in info["names"]


def test_unknown_prior_name_is_config_error():
    assert _code(lambda: factors.factor_ast("no_such_factor")) == E1015


def test_prior_pool_drops_futures_only_factors_on_spot():
    """现货集引用 funding_rate：不是配置错误，但必须显式记录而非静默丢。"""
    resolved, skipped = factors.resolve_prior_pool(
        ["mom60", "funding_carry"], available_fields={"close", "volume", "high", "low"},
    )
    assert [c["name"] for c in resolved] == ["mom60"]
    assert skipped == [{"name": "funding_carry", "reason": "field_unavailable",
                        "detail": "funding_rate"}]


def test_prior_pool_dedups_same_name():
    resolved, skipped = factors.resolve_prior_pool(
        ["mom20", "mom20"], available_fields=set(factors.BUILTIN_FACTORS) | {"close"},
    )
    assert len(resolved) == 1 and not skipped


def test_resolved_prior_carries_expression_and_fields():
    resolved, _ = factors.resolve_prior_pool(
        ["mom60"], available_fields={"close"},
    )
    assert resolved[0]["name"] == "mom60"
    assert resolved[0]["required_fields"] == ["close"]
    assert resolved[0]["expression"]["op"] == "div"


# ------------------------------------------------------------ 随机搜索算法
def _random_cfg(fields=("close", "volume", "high", "low", "vwap"), n=12):
    return GPConfig(fields=fields, max_candidates=n, max_depth=3, genome_len=7)


def test_random_expression_is_deterministic_per_seed():
    a, sa = generate_random(cfg=_random_cfg(), fields=_random_cfg().fields, seed=7)
    b, sb = generate_random(cfg=_random_cfg(), fields=_random_cfg().fields, seed=7)
    assert [c["candidate_id"] for c in a] == [c["candidate_id"] for c in b]
    assert sa["attempts"] == sb["attempts"]


def test_random_expression_candidates_are_unique():
    cands, state = generate_random(cfg=_random_cfg(), fields=_random_cfg().fields, seed=3)
    ids = [c["candidate_id"] for c in cands]
    assert len(ids) == len(set(ids))
    assert all(c["generator"] == "random_expression@1" for c in cands)
    assert state["target"] == 12


def test_random_expression_does_not_touch_global_random():
    import random as _random
    _random.seed(1234)
    before = _random.getstate()
    generate_random(cfg=_random_cfg(), fields=_random_cfg().fields, seed=9)
    assert _random.getstate() == before


# ------------------------------------------------------------ 多算法
def test_algorithm_runs_accepts_list_and_flat_forms():
    listed = search_op._algorithm_runs(
        {"algorithms": [{"name": "gp_symbolic", "params": {"population": 4}},
                        {"name": "random_expression", "params": {}}]})
    assert [n for n, _ in listed] == ["gp_symbolic", "random_expression"]
    assert listed[0][1]["population"] == 4

    flat = search_op._algorithm_runs({"algorithm": "gp_symbolic", "population": 8})
    assert flat == [("gp_symbolic", {"algorithm": "gp_symbolic", "population": 8})]


def test_unknown_algorithm_in_list_is_named():
    panel = _panel()
    call = _call(panel, params={
        "algorithms": [{"name": "gp_symbolic", "params": {"population": 4,
                                                          "generations": 0,
                                                          "max_depth": 2,
                                                          "max_candidates": 3}},
                       {"name": "does_not_exist", "params": {}}],
        "fields": ["close", "volume"], "family": "f1",
    })
    try:
        cap_run(call)
    except FlowError as exc:
        assert exc.error.code == E1004
        assert "does_not_exist" in str(exc.error.message)
    else:  # pragma: no cover
        pytest.fail("未知算法必须 E1004")


def test_two_algorithms_both_run_and_share_trial_count():
    panel = _panel()
    call = _call(panel, params={
        "algorithms": [
            {"name": "gp_symbolic", "params": {"population": 6, "generations": 1,
                                               "max_depth": 2, "max_candidates": 6}},
            {"name": "random_expression", "params": {"max_candidates": 6,
                                                     "max_depth": 2}},
        ],
        "fields": ["close", "volume"], "family": "f1",
    })
    out = cap_run(call)
    gens = out["generators"]
    assert "gp_symbolic@1" in gens and "random_expression@1" in gens
    # 两个算法都真的产出了候选（不是只跑第一个）
    assert any("random_expression@1" in c["generators"] for c in out["candidates"])
    assert out["count"] == len(out["candidates"])
    # 一次计数：n_trials 不少于最终候选数，且不因两算法重复而虚高到 2 倍
    assert out["count"] <= out["n_trials"] <= out["count"] * 2


def test_multi_algorithm_result_is_reproducible():
    panel = _panel()
    params = {
        "algorithms": [
            {"name": "gp_symbolic", "params": {"population": 6, "generations": 1,
                                               "max_depth": 2, "max_candidates": 5}},
            {"name": "random_expression", "params": {"max_candidates": 5,
                                                     "max_depth": 2}},
        ],
        "fields": ["close", "volume"], "family": "f1",
    }
    a = cap_run(_call(panel, params=params, seed=5))
    b = cap_run(_call(panel, params=params, seed=5))
    assert a["dedup_digest"] == b["dedup_digest"]


# ------------------------------------------------------------ 先验候选进池
def test_prior_candidates_enter_candidate_set_and_fitted_state():
    panel = _panel()
    resolved, _ = factors.resolve_prior_pool(
        ["mom20", "mom60"], available_fields={"close"},
    )
    out = cap_run(_call(panel, prior_candidates=resolved, params={
        "algorithm": "gp_symbolic", "population": 4, "generations": 0,
        "max_depth": 2, "max_candidates": 2,
        "fields": ["close", "volume"], "family": "f1",
    }))
    gens = {g for c in out["candidates"] for g in c["generators"]}
    assert {"mom20", "mom60"} <= gens
    assert out["fitted_state"]["prior_pool_used"] == ["mom20", "mom60"]


def test_prior_skipped_is_reported_not_silently_dropped():
    panel = _panel()
    out = cap_run(_call(
        panel,
        prior_skipped=[{"name": "funding_carry", "reason": "field_unavailable",
                        "detail": "funding_rate"}],
        params={
            "algorithm": "gp_symbolic", "population": 4, "generations": 0,
            "max_depth": 2, "max_candidates": 2,
            "fields": ["close", "volume"], "family": "f1",
        },
    ))
    assert out["fitted_state"]["prior_pool_skipped"][0]["name"] == "funding_carry"


def test_prior_candidate_without_expression_is_e3003():
    panel = _panel()
    call = _call(panel, prior_candidates=[{"name": "broken"}], params={
        "algorithm": "gp_symbolic", "population": 4, "generations": 0,
        "max_depth": 2, "max_candidates": 2,
        "fields": ["close", "volume"], "family": "f1",
    })
    try:
        cap_run(call)
    except FlowError as exc:
        assert exc.error.code == "E3003"
    else:  # pragma: no cover
        pytest.fail("缺 expression 必须 E3003")


# ------------------------------------------------------------ 因子库当先验池














# ------------------------------------------------------------ 因子库当先验池
def _catalog(tmp_path):
    from v5.storage.catalog import Catalog
    from v5.storage.layout import Layout
    return Catalog(Layout(tmp_path))


def _add(cat, cid, name, expr):
    cat.add({"factor_id": cid, "name": name, "expression": expr,
             "required_fields": ["close"], "operators": ["div"],
             "generators": ["gp_symbolic@1"], "origin": {"ticket_id": "t"},
             "evidence_grade": "out_of_sample", "statistics": {}, "metrics": {},
             "status": "active", "promoted_at": "2026-01-01T00:00:00Z"})


def test_db_star_pulls_active_catalog_factors(tmp_path):
    cat = _catalog(tmp_path)
    _add(cat, "sha256:" + "1" * 64, "f1", {"op": "div", "args": [
        {"field": "close"}, {"const": 2}]})
    _add(cat, "sha256:" + "2" * 64, "f2", {"op": "abs", "args": [{"field": "close"}]})
    resolved, skipped = factors.resolve_prior_pool(
        ["db:*"], available_fields={"close"}, catalog=cat)
    assert [r["name"] for r in resolved] == ["f1", "f2"]
    assert not skipped


def test_db_name_selects_one_factor(tmp_path):
    cat = _catalog(tmp_path)
    _add(cat, "sha256:" + "1" * 64, "f1", {"op": "abs", "args": [{"field": "close"}]})
    _add(cat, "sha256:" + "2" * 64, "f2", {"op": "log", "args": [{"field": "close"}]})
    resolved, _ = factors.resolve_prior_pool(
        ["db:f2"], available_fields={"close"}, catalog=cat)
    assert [r["name"] for r in resolved] == ["f2"]


def test_db_without_catalog_is_config_error():
    assert _code(lambda: factors.resolve_prior_pool(
        ["db:*"], available_fields={"close"}, catalog=None)) == E1015


def test_db_unknown_name_is_config_error(tmp_path):
    cat = _catalog(tmp_path)
    _add(cat, "sha256:" + "1" * 64, "f1", {"op": "abs", "args": [{"field": "close"}]})
    assert _code(lambda: factors.resolve_prior_pool(
        ["db:nope"], available_fields={"close"}, catalog=cat)) == E1015


def test_db_factor_missing_field_is_skipped(tmp_path):
    """库里含 funding_rate 的因子，在只有 close 的供给上被显式排除。"""
    cat = _catalog(tmp_path)
    _add(cat, "sha256:" + "3" * 64, "fundingish",
         {"op": "abs", "args": [{"field": "funding_rate"}]})
    resolved, skipped = factors.resolve_prior_pool(
        ["db:*"], available_fields={"close"}, catalog=cat)
    assert resolved == []
    assert skipped[0]["reason"] == "field_unavailable"
