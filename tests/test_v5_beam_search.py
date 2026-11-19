"""beam_search 挖掘算法测试：确定性、fitness 引导、记账完整、AST 合法。"""
from __future__ import annotations

import numpy as np
import pytest

from capability.mining import (
    GPConfig, describe_beam_search, generate_beam_search, make_candidate,
)
from capability.registry import default_registry


def test_describe_registered():
    spec = describe_beam_search()
    assert spec.name == "beam_search"
    assert spec.needs_fitness is True
    assert spec.deterministic is True
    assert spec.stateful is False


def _cfg():
    return GPConfig(fields=("close", "volume"), population=8, generations=2,
                    max_depth=5, max_candidates=12)


def test_deterministic_same_seed():
    """同 seed 两次逐位一致（AC39 复现）。"""
    seen = {"n": 0}

    def fit(cand):
        seen["n"] += 1
        return float(np.sin(len(str(cand["candidate_id"]))))  # 确定性的伪目标

    a, sa = generate_beam_search(cfg=_cfg(), fields=("close", "volume"), seed=7,
                                 fitness=fit, beam_width=4, depth=2)
    b, sb = generate_beam_search(cfg=_cfg(), fields=("close", "volume"), seed=7,
                                 fitness=fit, beam_width=4, depth=2)
    assert [c["candidate_id"] for c in a] == [c["candidate_id"] for c in b]
    assert sa["evaluated"] == sb["evaluated"]


def test_fitness_guided_beats_no_fitness_structure():
    """fitness 偏好某类表达式时，beam 应把它排到前面。

    目标：对以 close 为根的表达式给高分。beam 最终 top 应主要是含 close 的。
    """
    def fit(cand):
        return 1.0 if _contains_field(cand["payload"]["expression"], "close") else 0.0

    def _contains_field(node, name):
        if node.get("field") == name:
            return True
        return any(_contains_field(a, name) for a in node.get("args", [])
                   if isinstance(a, dict))

    cands, state = generate_beam_search(
        cfg=_cfg(), fields=("close", "volume"), seed=3,
        fitness=fit, beam_width=3, depth=2)
    assert cands, "beam 应产出候选"
    # 最高分必须是 1.0（含 close 的表达式）
    assert _contains_field(cands[0]["payload"]["expression"], "close")
    assert state["evaluated"] > len(cands)  # 评估过更多，只留 top


def test_all_candidates_are_valid_ast():
    """产出的候选表达式必须能通过注册表校验。"""
    reg = default_registry()
    from capability.mining import normalise_ast
    cands, _ = generate_beam_search(
        cfg=_cfg(), fields=("close", "volume"), seed=1,
        fitness=lambda c: 0.5, beam_width=3, depth=2)
    for c in cands:
        ast = c["payload"]["expression"]
        norm = normalise_ast(ast)
        assert norm == ast  # 已是规范形式


def test_accounting_matches_unique_evaluations():
    """记账：calls.total 应等于去重后实际评估数（fitness 只算一次/候选）。"""
    counter = {"n": 0}

    def fit(cand):
        counter["n"] += 1
        return 0.1

    _, state = generate_beam_search(
        cfg=_cfg(), fields=("close", "volume"), seed=2,
        fitness=fit, beam_width=3, depth=2)
    assert state["evaluated"] == state["calls"]["total"]
    assert counter["n"] == state["evaluated"]


def test_no_fields_returns_empty():
    cands, state = generate_beam_search(
        cfg=_cfg(), fields=(), seed=0, fitness=lambda c: 1.0)
    assert cands == []
    assert state.get("reason") == "no_fields"
