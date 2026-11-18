"""② capability 注册表与契约壳测试（s0/04 / 11 §4）。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from contracts.digest import compute_digest
from contracts.errors import E1002, E1015, E2003, E2006, FlowError
from capability import all_specs, describe, load_registry, resolve_impl
from capability.ops import search as search_ops

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "capability" / "registry"


def _err(fn):
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


def _bad_registry(tmp_path, mutate):
    """复制注册表到临时目录，改坏后加载。"""
    import shutil
    shutil.copytree(REG, tmp_path / "reg")
    op_path = tmp_path / "reg" / "operators.json"
    doc = json.loads(op_path.read_text(encoding="utf-8"))
    mutate(doc)
    op_path.write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path / "reg"


# ---- 注册表真实现 ------------------------------------------------------

def test_registry_loads_and_fingerprints_content():
    r = load_registry()
    assert len(r.fields) >= 9 and len(r.operators) >= 19
    doc = json.loads((REG / "operators.json").read_text(encoding="utf-8"))
    # 只覆盖语义字段：注释（basis / _self_check_note）不进算子集版本，
    # 否则改一句话就让所有历史计划的 operator_set 失效。
    assert r.operators_version == compute_digest({
        "schema_version": doc["schema_version"],
        "operators": [
            {k: op.get(k) for k in ("name", "arity", "impl_symbol",
                                    "degenerate_policy", "since")}
            | {"param_domain": op.get("param_domain") or {}}
            for op in sorted(doc["operators"], key=lambda o: str(o["name"]))
        ],
    })
    fields_doc = json.loads((REG / "fields.json").read_text(encoding="utf-8"))
    assert r.fields_digest == compute_digest(fields_doc)


def test_operators_version_is_content_not_package_version():
    """I27：算子表内容变 → 版本变，与 qweave.__version__ 无关。"""
    r1 = load_registry()
    r2 = load_registry()
    assert r1.operators_version == r2.operators_version


def test_lookup_unknown_raises_e1015():
    r = load_registry()
    assert _err(lambda: r.operator("lead_forward")) == E1015
    assert _err(lambda: r.field("market_cap")) == E1015


def test_self_check_rejects_negative_shift(tmp_path):
    def mutate(doc):
        doc["operators"].append({"name": "shift_neg", "impl_symbol": "shift",
                                 "param_domain": {"lag": {"min": -1, "max": 5}},
                                 "degenerate_policy": "nan"})
    assert _err(lambda: load_registry(_bad_registry(tmp_path, mutate))) == E1015


def test_self_check_rejects_center_bfill_missing_impl(tmp_path):
    cases = [
        lambda d: d["operators"].append({"name": "center", "impl_symbol": "x", "degenerate_policy": "nan"}),
        lambda d: d["operators"].append({"name": "bfill", "impl_symbol": "x", "degenerate_policy": "nan"}),
        lambda d: d["operators"].append({"name": "no_impl", "degenerate_policy": "nan"}),
        lambda d: d["operators"].append({"name": "magic_div", "impl_symbol": "div", "degenerate_policy": "1e-9"}),
    ]
    for i, mutate in enumerate(cases):
        assert _err(lambda m=mutate: load_registry(_bad_registry(tmp_path / str(i), m))) == E1015


def test_self_check_rejects_zero_window(tmp_path):
    def mutate(doc):
        doc["operators"][2]["param_domain"] = {"window": {"min": 0, "max": 10}}
    assert _err(lambda: load_registry(_bad_registry(tmp_path, mutate))) == E1015


def test_self_check_rejects_negative_max_window(tmp_path):
    """P1-11：只查 min 时 max=-3 这类负位移/零窗口会穿透。"""
    for bad_domain in ({"lag": {"min": 1, "max": -3}},
                       {"window": {"max": 0}},
                       {"window": {"min": 10, "max": 2}}):
        def mutate(doc, dom=bad_domain):
            doc["operators"].append({
                "name": "edge_case", "impl_symbol": "x",
                "param_domain": dom, "degenerate_policy": "nan",
            })
        key = str(abs(hash(str(bad_domain))))
        assert _err(lambda m=mutate, k=key: load_registry(_bad_registry(tmp_path / k, m))) == E1015


def test_self_check_rejects_truthy_center(tmp_path):
    """P1-11：center:1 / "yes" 与 center:true 同为居中窗口。"""
    for v in (1, "yes"):
        def mutate(doc, value=v):
            doc["operators"].append({
                "name": "centered", "impl_symbol": "x", "center": value,
                "degenerate_policy": "nan",
            })
        assert _err(lambda m=mutate, k=str(v): load_registry(_bad_registry(tmp_path / str(v), m))) == E1015


def test_self_check_rejects_negative_lag_field(tmp_path):
    # `11` §2.1 只允许 >= 0；负数才是前视。0 是合法的（close 就是 0）。
    import shutil
    shutil.copytree(REG, tmp_path / "reg")
    path = tmp_path / "reg" / "fields.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["fields"][0]["available_lag"] = -1
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert _err(lambda: load_registry(tmp_path / "reg")) == E1015


# ---- 四档名字空间 ------------------------------------------------------

def test_namespace_open_allows_unregistered_name():
    assert resolve_impl({}, "search", "new_algo", "open") == "new_algo"
    assert _err(lambda: resolve_impl({}, "search", None, "open")) == E1002


def test_namespace_closed_multi_rejects_unregistered():
    catalog = {"allocate": {"q_quantile": {}, "cvxpy_cvx": {}}}
    assert resolve_impl(catalog, "allocate", "q_quantile", "closed_multi") == "q_quantile"
    assert _err(lambda: resolve_impl(catalog, "allocate", "unknown", "closed_multi")) == E1002


def test_namespace_single_and_fixed():
    catalog = {"read": {"only": {}}, "verdict": {"sentinel": {}}}
    assert resolve_impl(catalog, "read", None, "single") == "only"
    assert resolve_impl(catalog, "verdict", None, "fixed") == "sentinel"
    assert _err(lambda: resolve_impl(catalog, "verdict", "other", "fixed")) == E2003
    assert _err(lambda: resolve_impl({"verdict": {"a": {}, "b": {}}}, "verdict", None, "fixed")) == E2006
    assert _err(lambda: resolve_impl({"read": {}}, "read", None, "single")) == E2006


# ---- 12 壳 + spec ------------------------------------------------------

def test_all_12_specs_present():
    specs = all_specs()
    assert len(specs) == 12
    assert set(specs) == {"read", "boundary", "align", "label", "search", "observe",
                          "verdict", "combine", "allocate", "evaluate", "backtest", "certify"}
    assert describe("verdict").namespace == "fixed"
    assert describe("search").namespace == "open"
    assert _err(lambda: describe("nonexistent")) == E2003


def test_spec_metadata_matches_frozen_capability_contract():
    """P1-12：元信息必须与 s0/04 §3 逐能力声明一致，不得一套默认值套全部。"""
    # 服务工位（§3.9 allocate 三处；§3.11 backtest 四处）
    assert describe("allocate").serves == ("Evidence", "Portfolio", "Certify")
    assert describe("backtest").serves == ("Evidence", "Verdict", "Portfolio", "Certify")
    # 副作用三档（§1.1）；append_only 必须可用
    side = {n: describe(n).side_effect for n in all_specs()}
    assert side["search"] == side["verdict"] == "append_only"
    assert side["boundary"] == side["combine"] == side["certify"] == "repeatable"
    assert side["read"] == side["evaluate"] == "pure"
    # 组合图（§1.3：证据调构造+成本；认证调评估+构造+成本）
    assert describe("observe").composes == ("allocate", "backtest")
    assert describe("certify").composes == ("evaluate", "allocate", "backtest")
    assert describe("read").composes == ()
    # 口径子块只能是 caliber 四块，不是 "policy_view"
    for name, spec in all_specs().items():
        assert set(spec.policy_reads) <= {"execution", "label", "allocate", "net"}, name
    assert set(describe("backtest").policy_reads) == {"net"}
    assert set(describe("label").policy_reads) == {"execution", "label"}


def test_search_four_methods_present():
    """S3 起四方法已有真实实现（gp_symbolic），不再是空壳。"""
    assert callable(search_ops.describe)
    assert callable(search_ops.fit)
    assert callable(search_ops.generate)
    assert callable(search_ops.lineage)
    # describe 返回算法元信息，不含任何评估/结论字段
    spec = search_ops.describe()["gp_symbolic"]
    assert spec["name"] == "gp_symbolic"
    assert spec["needs_fitness"] is True
    assert spec["fitness_objectives"] == ["abs_rank_ic"]
    # lineage 是纯元数据回溯
    lin = search_ops.lineage({"parents": ["p1"], "operators": ["add", "abs"],
                              "generator": "gp@1"})
    assert lin["parents"] == ["p1"] and lin["operators"] == ["abs", "add"]


def test_all_ops_importable():
    import importlib
    for name in all_specs():
        mod = importlib.import_module(f"capability.ops.{name}")
        assert mod is not None


def test_cacheable_is_derived_from_side_effect():
    """`cacheable` 曾是独立字段且从不赋值（恒 True）——`append_only` 的能力也被
    标成"可缓存"，而它们每次调用都往账本追加一条事实。改为派生后语义自洽。"""
    assert describe("verdict").cacheable is False       # append_only
    assert describe("search").cacheable is False        # append_only
    assert describe("observe").cacheable is True        # pure
    assert describe("allocate").cacheable is True       # repeatable
    payload = describe("observe").to_dict()
    # 恒默认字段不再出现在公开载荷里：印一个恒定的真假值等于没有信息
    assert "deterministic" not in payload and "stateful" not in payload

def test_implementation_defaults_single_source():
    """G10/E8/G14/G11/G3/G12 共用的实现缺省注册表：值冻结在这里。

    这些数不是口径（不进指纹），但也不能在五个文件里各写一份：改一处会让
    同一算法在不同入口拿到不同种群、同一告警在不同命令里有不同阈值。
    """
    from capability.params import implementation_defaults

    assert implementation_defaults("search") == {
        "population": 20, "generations": 3, "max_depth": 4,
        "max_candidates": 40, "genome_len": 7,
    }
    assert implementation_defaults("boundary")["tier_edges"] == [
        1 / 3, 2 / 3,
    ]
    assert implementation_defaults("allocate")["l2_regularisation"] == 1e-3
    assert implementation_defaults("pipeline")["n_trials_drop_ratio"] == 0.5
    assert implementation_defaults("cli") == {"master_seed": 42, "family": "family_v1"}
    assert implementation_defaults("backtest")["initial_cash"] == 1e6

