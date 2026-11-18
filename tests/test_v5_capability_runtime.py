"""S3-M0：能力统一调用协议（s0/04 §1）与 param schema（§2）。"""

from __future__ import annotations

import json

import numpy as np
from pathlib import Path

import pytest

from capability import CAPABILITIES, describe, run
from capability.params import load_param_schemas, validate_params
from capability.runtime import (
    Call, InputRef, impls, register, reset_impls, resolve,
)
from contracts.errors import (
    E1002, E1003, E1004, E1008, E1020, E2001, E2002, E2003, E2006, E2008,
    E3001, E3003, E3004, FlowError,
)

ROOT = Path(__file__).resolve().parents[1]
PARAMS = ROOT / "capability" / "registry" / "params.json"

# 每个能力在测试里用的实现名（实现名进 digest，必须与 register 的一致）
_IMPL = {
    "evaluate": "qweave", "label": "cs", "observe": "funnel", "verdict": "sentinel",
    "certify": "walk_forward", "read": "pit", "allocate": "quantile",
    "search": "gp", "align": "pit_align", "boundary": "pit", "combine": "ridge",
    "backtest": "ml4t",
}
_PANEL = InputRef(kind="panel", digest="sha256:" + "b" * 64)
_PAYLOAD = InputRef(kind="payload", digest="sha256:" + "a" * 64)
_OBS = InputRef(kind="observation_set", digest="sha256:" + "8" * 64)
_UPSTREAM = InputRef(kind="all_upstream", digest="sha256:" + "9" * 64)
_EVAL_PARAMS = {"operator_set": "ops@1", "nan_policy": "propagate", "node_cache": False}
_OK_EVAL = {"values_ref": "v", "valid_mask_ref": "m", "node_stats": {},
            "operators_used": [], "values": [0.0], "valid_mask": [True]}


def _code(fn) -> str | None:
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


@pytest.fixture(autouse=True)
def _clean_impls():
    reset_impls()
    yield
    reset_impls()


def _call(capability: str, params: dict, *, impl: str | None = None, **kw) -> Call:
    base = dict(
        capability=capability, station="Evidence",
        impl=_IMPL[capability] if impl is None else impl, impl_version="1",
        params=params,
    )
    base.update(kw)
    return Call.create(**base)


def _eval_call(**kw) -> Call:
    return _call("evaluate", _EVAL_PARAMS, inputs=(_PAYLOAD, _PANEL), **kw)


def _register_eval(fn) -> None:
    register("evaluate", "qweave", "1", fn)


# ---- schema 数据本身 ---------------------------------------------------
def test_every_capability_has_param_schema():
    doc = load_param_schemas()
    assert set(doc) == set(CAPABILITIES)
    for name in CAPABILITIES:
        assert doc[name]["params"] is not None
        assert doc[name]["outputs"]


def test_schema_self_check_rejects_broken_entry(tmp_path):
    src = json.loads(PARAMS.read_text(encoding="utf-8"))
    src["capabilities"]["evaluate"]["params"]["nan_policy"]["values"] = []
    broken = tmp_path / "params.json"
    broken.write_text(json.dumps(src), encoding="utf-8")
    assert _code(lambda: load_param_schemas(broken)) == E2003

    src2 = json.loads(PARAMS.read_text(encoding="utf-8"))
    src2["capabilities"]["label"]["params"]["horizon"]["type"] = "whatever"
    broken.write_text(json.dumps(src2), encoding="utf-8")
    assert _code(lambda: load_param_schemas(broken)) == E2003


# ---- 第 2 步：参数 -----------------------------------------------------
def test_unknown_param_is_e1003_not_silently_ignored():
    """C16：n_trials 属于 runtime_facts；写进 params 就是错误，不是"配了没用"。"""
    call = _call("verdict", {"dsr_min": 0.95, "fdr_max": 0.1,
                             "cost_adj_min": 1.0, "family": "f", "n_trials": 10},
                 inputs=(_OBS,))
    assert _code(lambda: run(call)) == E1003

    call = _call("certify", {"folds": 5, "embargo": "5bar",
                             "oos_window": {"start": "2026-07-01T00:00:00Z",
                                            "end": "2026-08-01T00:00:00Z"},
                             "holdout": "revealed"},
                 inputs=(_UPSTREAM,))
    assert _code(lambda: run(call)) == E1003


def test_type_and_range_violations_are_e1004():
    assert _code(lambda: run(_call("verdict", {
        "dsr_min": "0.95", "fdr_max": 0.1, "cost_adj_min": 1.0, "family": "f"},
        inputs=(_OBS,)))) == E1004
    assert _code(lambda: run(_call("verdict", {
        "dsr_min": 1.5, "fdr_max": 0.1, "cost_adj_min": 1.0, "family": "f"},
        inputs=(_OBS,)))) == E1004
    # bool 不是 int：`folds: true` 不得当成 1
    assert _code(lambda: run(_call("certify", {
        "folds": True, "embargo": 5,
        "oos_window": {"start": "2026-07-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"}},
        inputs=(_UPSTREAM,)))) == E1004
    # duration 必须是正整数 bar 或 '5bar'
    assert _code(lambda: run(_call("label", {
        "horizon": 0, "rebalance": 5, "entry": "next_close",
        "transform": "cs_rank", "neutralise": []}, inputs=(_PANEL,)))) == E1004


def test_missing_required_param_is_e1008():
    assert _code(lambda: run(_call("verdict", {"dsr_min": 0.95},
                                   inputs=(_OBS,)))) == E1008


def test_map_param_rejects_unknown_key():
    assert _code(lambda: run(_call("read", {
        "datasets": ["ohlcv"], "fields": {"ohlcv": ["close"]}, "frequency": "1d",
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z",
                   "extra": 1},
        "symbols": "cs_top60"},
        inputs=(InputRef(kind="pin", digest="sha256:" + "7" * 64),)))) == E1003


def test_search_allows_algorithm_specific_params():
    """search 是 open 名字空间：算法参数由算法 param_schema 决定，不在此拒绝。"""
    out = validate_params("search", {"max_candidates": 10, "population": 200,
                                     "generations": 30})
    assert out["population"] == 200


def test_read_fields_is_a_free_key_map():
    """read.fields 的键是数据集名，不预设；值仍按 list[str] 递归校验。"""
    out = validate_params("read", {
        "datasets": ["ohlcv", "funding"],
        "fields": {"ohlcv": ["close", "volume"], "funding": ["funding_rate"]},
        "frequency": "1d",
        "window": {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        "symbols": "cs_top60",
    })
    assert out["fields"]["funding"] == ["funding_rate"]

    bad_value = dict(out)
    bad_value["fields"] = {"ohlcv": "close"}  # 值不是 list → E1004
    assert _code(lambda: validate_params("read", bad_value)) == E1004


# ---- 第 1 步：指纹 -----------------------------------------------------
def test_digest_covers_every_channel():
    base = dict(capability="evaluate", station="Evidence", impl="qweave",
                impl_version="1", params=_EVAL_PARAMS)
    a = Call.create(**base)

    variants = [
        {**base, "params": {**_EVAL_PARAMS, "node_cache": True}},
        {**base, "seed": 1},
        {**base, "phase": "fit"},
        {**base, "impl_version": "2"},
        {**base, "policy_view": {"execution": {"entry": "next_open"}}},
        {**base, "runtime_facts": {"n_trials": 3}},
        {**base, "inputs": (_PANEL,)},
    ]
    for variant in variants:
        assert Call.create(**variant).call_digest != a.call_digest


def test_capability_is_part_of_digest():
    """`evaluate@qweave@1` 与 `align@qweave@1` 不得共享缓存键。"""
    common = dict(station="Panel", impl="qweave", impl_version="1", params={})
    assert Call.create(capability="evaluate", **common).call_digest != \
        Call.create(capability="align", **common).call_digest


def test_input_kind_is_part_of_digest():
    """kind 是 E3001 的唯一判据；不进指纹就等于输入角色可被偷换。"""
    a = Call.create(capability="evaluate", station="Evidence", impl="qweave",
                    impl_version="1", params=_EVAL_PARAMS,
                    inputs=(_PAYLOAD, InputRef("panel", "sha256:" + "b" * 64)))
    b = Call.create(capability="evaluate", station="Evidence", impl="qweave",
                    impl_version="1", params=_EVAL_PARAMS,
                    inputs=(InputRef("panel", "sha256:" + "a" * 64),
                            InputRef("panel", "sha256:" + "b" * 64)))
    assert a.call_digest != b.call_digest


def test_verify_catches_in_place_mutation():
    call = _eval_call()
    call.params["node_cache"] = True  # 冻结的是 dataclass，不是里面的 dict
    assert _code(call.verify) == E2002
    assert _code(lambda: run(call)) == E2002


def test_handmade_call_without_digest_is_rejected():
    call = Call(capability="evaluate", station="Evidence", station_revision="r1",
                impl="qweave", impl_version="1", phase="none",
                params=dict(_EVAL_PARAMS), inputs=(), policy_view={},
                runtime_facts={}, call_digest="")
    assert _code(lambda: run(call)) == E2002


# ---- 第 3 步：输入 -----------------------------------------------------
def test_input_count_and_kind_are_checked():
    _register_eval(lambda c: _OK_EVAL)
    assert run(_eval_call())["values_ref"] == "v"

    bad_kind = _call("evaluate", _EVAL_PARAMS, inputs=(_PANEL, _PANEL))
    assert _code(lambda: run(bad_kind)) == E3001

    too_few = _call("evaluate", _EVAL_PARAMS, inputs=(_PAYLOAD,))
    assert _code(lambda: run(too_few)) == E3001


# ---- 第 4/5 步：执行与返回 ---------------------------------------------
def test_unimplemented_capability_is_e3004_not_success():
    call = _call("label", {"horizon": 5, "rebalance": 5, "entry": "next_close",
                           "transform": "cs_rank", "neutralise": []},
                 inputs=(_PANEL,))
    assert _code(lambda: run(call)) == E3004


def test_outputs_schema_rejects_missing_field():
    _register_eval(lambda c: {"values_ref": "v"})
    assert _code(lambda: run(_eval_call())) == E3003


def test_unexpected_exception_is_e3004_with_cause():
    def boom(_call):
        raise ZeroDivisionError("x")

    _register_eval(boom)
    assert _code(lambda: run(_eval_call())) == E3004


def test_flow_error_passes_through_unchanged():
    def deny(_call):
        raise FlowError(E3003, "截面宽度不足")

    _register_eval(deny)
    assert _code(lambda: run(_eval_call())) == E3003


# ---- 第 3.5 步：子调用 -------------------------------------------------
def _allocate_child(policy: dict) -> Call:
    return Call.create(
        capability="allocate", station="Evidence", impl="quantile", impl_version="1",
        params={}, policy_view=policy,
        inputs=(InputRef(kind="score", digest="sha256:" + "c" * 64),
                InputRef(kind="tradable_mask", digest="sha256:" + "d" * 64),
                InputRef(kind="tiers", digest="sha256:" + "e" * 64)),
    )


def _observe_call(children, policy_view=None) -> Call:
    register("observe", "funnel", "1",
             lambda c: {"per_candidate": [], "method_ids": {}, "redundancy": {}})
    return Call.create(
        capability="observe", station="Evidence", impl="funnel", impl_version="1",
        params={"prefilter": {}, "null_model": "within_bar_cross_asset",
                "metrics": ["ic"], "cost_attribution": True, "n_permutations": 10},
        policy_view=policy_view if policy_view is not None
        else {"net": {"fee_model": "taker"}},
        composed=children,
        inputs=(InputRef(kind="candidate_set", digest="sha256:" + "1" * 64),
                InputRef(kind="panel", digest="sha256:" + "2" * 64),
                InputRef(kind="label", digest="sha256:" + "3" * 64)),
    )


def test_undeclared_child_capability_is_e2008():
    child = Call.create(
        capability="evaluate", station="Evidence", impl="qweave", impl_version="1",
        params=dict(_EVAL_PARAMS), policy_view={"net": {"fee_model": "taker"}},
        inputs=(_PAYLOAD, _PANEL))
    assert _code(lambda: run(_observe_call((child,)))) == E2008


def test_child_with_different_policy_is_e1020():
    child = _allocate_child({"net": {"fee_model": "maker"}})
    assert _code(lambda: run(_observe_call((child,)))) == E1020


def test_undeclared_policy_subblock_is_e2001():
    """evaluate 的 policy_reads 为空：读任何口径子块都已经是越权。"""
    _register_eval(lambda c: _OK_EVAL)
    call = _call("evaluate", _EVAL_PARAMS, inputs=(_PAYLOAD, _PANEL),
                 policy_view={"net": {"fee_model": "taker"}})
    assert _code(lambda: run(call)) == E2001


def test_declared_child_passes():
    child = _allocate_child({"net": {"fee_model": "taker"}})
    assert run(_observe_call((child,)))["per_candidate"] == []


# ---- 第 5 步的严格性 ---------------------------------------------------
def test_outputs_reject_extra_keys():
    _register_eval(lambda c: {**_OK_EVAL, "rank": 1})
    assert _code(lambda: run(_eval_call())) == E3003


def test_outputs_reject_nan_and_inf():
    """NaN/inf 必须在第 5 步被拒，而不是靠指纹 E2002 兜底。"""
    for bad in (float("nan"), float("inf")):
        payload = {"net_return_ref": "n", "net_return": np.zeros(2),
                   "cost_breakdown": {}, "turnover": [],
                   "funding_total": bad, "sharpe": 0.1,
                   "annualisation_used": "calendar_365",
                   "bars_per_year": 365.0,
                   "bars_per_year_source": "assumed_daily",
                   "missing_price_cells": 0}
        reset_impls()
        register("backtest", "ml4t", "1", lambda c, p=payload: p)
        call = _call("backtest", {"detail": "summary"},
                     inputs=(InputRef("position", "p"), InputRef("price", "q"),
                             None, InputRef("tiers", "t")))
        assert _code(lambda: run(call)) == E3003


def test_backtest_full_detail_requires_full_fields():
    """两档同源：`detail=full` 缺 ledger_ref 不算成功。"""
    summary = {"net_return_ref": "n", "net_return": np.zeros(2),
               "cost_breakdown": {}, "turnover": [],
               "funding_total": 0.0, "sharpe": 1.0,
               "annualisation_used": "calendar_365",
               "bars_per_year": 365.0,
               "bars_per_year_source": "assumed_daily",
               "missing_price_cells": 0}
    register("backtest", "ml4t", "1", lambda c: summary)
    full = _call("backtest", {"detail": "full"},
                 inputs=(InputRef("position", "p"), InputRef("price", "q"),
                         None, InputRef("tiers", "t")))
    assert _code(lambda: run(full)) == E3003

    ok = _call("backtest", {"detail": "summary"},
               inputs=(InputRef("position", "p"), InputRef("price", "q"),
                       None, InputRef("tiers", "t")))
    assert run(ok)["sharpe"] == 1.0


# ---- 类型系统的严格性 --------------------------------------------------
def test_timestamp_must_be_utc_z():
    for bad in ("2026-01-01T00:00:00+08:00", "2026-01-01 00:00:00Z", "2026-01-01"):
        assert _code(lambda b=bad: validate_params("certify", {
            "folds": 5, "embargo": 5,
            "oos_window": {"start": b, "end": "2026-08-01T00:00:00Z"}})) == E1004


def test_nan_float_param_is_e1004_not_e2002():
    assert _code(lambda: validate_params("observe", {
        "prefilter": {"min_coverage": float("nan")},
        "null_model": "time_shift", "metrics": ["ic"],
        "cost_attribution": True, "n_permutations": 1})) == E1004


def test_label_entry_close_is_legal():
    """04 §3.4：entry 是 {next_open, next_close, close}，少一个就有口径无法表达。"""
    out = validate_params("label", {"horizon": 5, "rebalance": 5, "entry": "close",
                                    "transform": "cs_rank", "neutralise": []})
    assert out["entry"] == "close"


def test_schema_cache_is_not_mutable_contract():
    """C16 可被一行赋值废除：`schemas()[...] = ...` 必须不影响校验。"""
    from capability.params import schemas
    doc = schemas()
    doc["verdict"]["params"]["n_trials"] = {"type": "int", "required": False}
    assert _code(lambda: validate_params("verdict", {
        "dsr_min": 0.95, "fdr_max": 0.1, "cost_adj_min": 1.0, "family": "f",
        "n_trials": 5})) == E1003


def test_optional_middle_input_needs_none_placeholder():
    """backtest 的 funding 在中间：省略它会让 tiers 前移，必须用 None 占位。"""
    from capability.params import validate_inputs
    ok = validate_inputs("backtest", (InputRef("position", "p"),
                                      InputRef("price", "q"), None,
                                      InputRef("tiers", "t")))
    assert ok[2] is None
    # 不占位就是错位绑定
    assert _code(lambda: validate_inputs("backtest", (
        InputRef("position", "p"), InputRef("price", "q"),
        InputRef("tiers", "t")))) == E3001
    # 必填输入不得为 None
    assert _code(lambda: validate_inputs("backtest", (
        None, InputRef("price", "q"), None, InputRef("tiers", "t")))) == E3001


# ---- 子调用 / 口径 -----------------------------------------------------
def test_policy_union_allows_child_declared_subblock():
    """observe 声明 execution/net，allocate 声明 allocate：三者都合法。"""
    child = _allocate_child({"execution": {"entry": "next_close"},
                             "net": {"fee_model": "taker"},
                             "allocate": {"quantiles": 5}})
    call = _observe_call((child,), policy_view={
        "execution": {"entry": "next_close"}, "net": {"fee_model": "taker"},
        "allocate": {"quantiles": 5}})
    assert run(call)["per_candidate"] == []


def test_policy_subblock_nobody_declares_is_e2001():
    child = _allocate_child({"label": {"horizon": 5}})
    call = _observe_call((child,), policy_view={"label": {"horizon": 5}})
    assert _code(lambda: run(call)) == E2001


# ---- 实现注册表 --------------------------------------------------------
def test_single_namespace_rejects_second_impl():
    """C6：`single` / `fixed` 出现第二个实现即 E2006（第二套执行路径）。"""
    register("label", "cs", "1", lambda c: {})
    assert resolve("label").name == "cs"
    assert _code(lambda: register("label", "alt", "1", lambda c: {})) == E2006
    assert _code(lambda: register("label", "cs", "2", lambda c: {})) == E2006
    # single 档不得指定另一个名字
    assert _code(lambda: resolve("label", "alt")) == E1002


def test_fixed_namespace_also_single_entry():
    register("verdict", "sentinel", "1", lambda c: {})
    assert _code(lambda: register("verdict", "other", "1", lambda c: {})) == E2006


def test_open_namespace_allows_multiple_but_requires_name():
    register("search", "gp", "1", lambda c: {})
    register("search", "random", "1", lambda c: {})
    assert _code(lambda: resolve("search")) == E1002
    assert resolve("search", "random").version == "1"
    # open 档：目录里没有的名字是「未实现」，不是「名字非法」
    assert _code(lambda: resolve("search", "never")) == E3004
    assert _code(lambda: resolve("search", "gp", "2")) == E1002


def test_closed_multi_requires_registered_name():
    register("allocate", "quantile", "1", lambda c: {})
    register("allocate", "cvx", "2", lambda c: {})
    assert _code(lambda: resolve("allocate")) == E1002
    assert resolve("allocate", "cvx").version == "2"
    assert _code(lambda: resolve("allocate", "unknown")) == E1002


def test_missing_impl_is_e3004():
    assert _code(lambda: resolve("label")) == E3004


def test_duplicate_same_name_in_open_namespace_rejected():
    register("search", "gp", "1", lambda c: {})
    assert _code(lambda: register("search", "gp", "1", lambda c: {})) == E2003


def test_empty_impl_is_rejected_not_fallen_back_to_single():
    """`impl=""` 曾静默回落到唯一实现：digest 记 impl@''，实际跑另一个实现。

    统一 `Call` 之后这条在**构造时**就断了（`E1004`），比原来"跑到 `run` 才
    `E1008`"更早。`resolve()` 里的空 impl 分支保留作后备：`Call` 之外的调用方
    （第三方注册表脚本）仍可能绕过 `create`。
    """
    _register_eval(lambda c: _OK_EVAL)
    assert _code(lambda: _eval_call(impl="", impl_version="")) == E1004


def test_describe_still_pure():
    assert describe("observe").composes == ("allocate", "backtest")
    assert isinstance(impls(), dict)


def test_phase_must_be_a_known_value():
    """`phase` 枚举校验曾只活在没人用的那份 `Call` 里——等于没有。"""
    assert _code(lambda: Call.create(
        capability="evaluate", station="Evidence", impl="qweave",
        impl_version="1", params=_EVAL_PARAMS, phase="fitted",
    )) == E1004


def test_sub_digests_are_derived_not_stored():
    """三个子摘要是派生属性：存字段就多一个能与本体分叉的副本。

    V5 反复吃亏的正是这一类——`policy_digest` 曾被能力层算成 `H(policy_view)`
    与编排层的真值同名不同义。派生属性没有"另一份值"可以漂移。
    """
    import dataclasses

    from contracts.digest import compute_digest

    call = _eval_call()
    stored = {f.name for f in dataclasses.fields(call)}
    assert {"params_digest", "policy_digest", "policy_view_digest", "runtime_facts_digest"} & stored == set()
    assert call.params_digest == compute_digest(dict(call.params))
    assert call.policy_view_digest == compute_digest(dict(call.policy_view))
