"""① runtime 真实化测试（s0/01 §5 指纹 + s0/03 §5/§8）。

验证：四锚点指纹真公式、来源表逐叶子、门禁 B 四项、charter 四类加载，
以及 D4 判据——数据版本变化让 blueprint 变而 policy 不变。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from v5.contracts.digest import compute_digest
from v5.contracts.errors import (
    E1001, E1002, E1004, E1008, E4001, E4002, E4003, E4004, E4005, FlowError,
)
from v5.contracts.types import Digest, Pin
from v5.runtime.charter import (
    load_baseline, load_catalog, load_plan,
)
from v5.runtime.compiler import compile_plan
from v5.runtime.gate_b import check_gate_b

from pathlib import Path as _Path

PLAN = json.load(open(_Path(__file__).parents[1] / "fixtures" / "v5" / "plan_valid.json", encoding="utf-8"))


def _err(fn):
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


def _compile(**kw):
    defaults = dict(baseline=None, plan=PLAN, strict=True)
    defaults.update(kw)
    return compile_plan(**defaults)


_SYM_SETS = {"cs_top60_2026q3": ["BTC", "ETH"]}


def _alias_pin(dataset: str, version: str, acquired_at: str) -> dict:
    """完整 Pin 字典（S2 起别名只允许这种形态）。"""
    return {
        "dataset": dataset, "version": version,
        "checksum": "sha256:" + "b" * 64,
        "coverage": {"start": "2023-12-31T00:00:00+00:00",
                     "end": "2026-06-30T00:00:00+00:00"},
        "symbols_digest": compute_digest(_SYM_SETS["cs_top60_2026q3"]),
        "end_closed": True, "schema_version": "1",
        "acquired_at": acquired_at,
        "gate_a": {"passed": True, "reason_code": "ok"},
    }


def _aliases(version: str, acquired_at: str) -> dict:
    names = ("ohlcv", "funding", "open_interest", "basis")
    return {n: _alias_pin(n, version, acquired_at) for n in names}


# ---- 四指纹 -----------------------------------------------------------

def test_plan_digest_is_raw_plan_not_merged():
    """plan_digest 只覆盖用户写下的原文，且必须用契约层同一公式（不能两套）。"""
    from v5.contracts import digest as D

    c = _compile()
    assert c.fingerprints["plan_digest"] == D.plan_digest(PLAN)


def test_fingerprints_are_real_not_pending():
    c = _compile()
    for key in ("plan_digest", "blueprint_digest", "policy_digest",
                "caliber_digest", "domain_digest"):
        v = c.fingerprints[key]
        assert v.startswith("sha256:") and "pending" not in v
        assert len(v.split(":", 1)[1]) == 64


def test_fingerprints_deterministic_across_calls():
    assert _compile().fingerprints == _compile().fingerprints


def test_baseline_change_moves_blueprint_and_policy():
    # 基线文件的版本标识是 `baseline_version`；它是元字段，不得并入 plan
    # （否则成为 schema 非法顶层键 E1001），但必须进 policy/blueprint 指纹。
    c1 = compile_plan(baseline={"baseline_version": "v1"}, plan=PLAN)
    c2 = compile_plan(baseline={"baseline_version": "v2"}, plan=PLAN)
    assert c1.fingerprints["blueprint_digest"] != c2.fingerprints["blueprint_digest"]
    assert c1.fingerprints["policy_digest"] != c2.fingerprints["policy_digest"]
    assert "baseline_version" not in c1.plan


def test_d4_supply_pin_change_moves_blueprint_not_policy():
    """D4：数据版本变化 → blueprint 变、policy 不变（I59 的指纹基础）。"""
    p1 = dict(PLAN)
    p2 = json.loads(json.dumps(PLAN))
    p2["supply"] = {**p2["supply"], "datasets": ["ohlcv", "funding"]}
    a1 = compile_plan(baseline=None, plan=p1, aliases=_aliases("a" * 16, "2026-11-01T00:00:00+00:00"),
                      symbol_sets=_SYM_SETS,
                      supply_meta=_meta_for(("ohlcv", "funding", "open_interest", "basis"),
                                            version="a" * 16))
    a2 = compile_plan(baseline=None, plan=p2, aliases=_aliases("b" * 16, "2026-11-01T00:00:00+00:00"),
                      symbol_sets=_SYM_SETS,
                      supply_meta=_meta_for(("ohlcv", "funding", "open_interest", "basis"),
                                            version="b" * 16))
    assert a1.fingerprints["blueprint_digest"] != a2.fingerprints["blueprint_digest"]
    assert a1.fingerprints["policy_digest"] == a2.fingerprints["policy_digest"]


def test_operations_not_in_policy():
    # s0/01 §2.3：操作性参数进 plan/blueprint，但不进 policy
    a = _compile(plan={**PLAN, "operations": {"workers": 1}})
    b = _compile(plan={**PLAN, "operations": {"workers": 8}})
    assert a.fingerprints["policy_digest"] == b.fingerprints["policy_digest"]
    assert a.fingerprints["plan_digest"] != b.fingerprints["plan_digest"]


def test_caliber_change_moves_policy():
    p = json.loads(json.dumps(PLAN))
    p["caliber"]["label"]["horizon"] = 10
    assert _compile().fingerprints["policy_digest"] != _compile(plan=p).fingerprints["policy_digest"]


def test_impl_and_seed_do_not_enter_policy():
    """s0/01 §2.3：实现名与种子进 blueprint/parcel，但不进 policy。"""
    p_impl = json.loads(json.dumps(PLAN))
    p_impl["panel"]["impl"] = "another_panel"
    p_seed = json.loads(json.dumps(PLAN))
    p_seed["search"]["seed"] = p_seed["search"]["seed"] + 1

    base = _compile()
    for p in (p_impl, p_seed):
        c = _compile(plan=p)
        assert c.fingerprints["policy_digest"] == base.fingerprints["policy_digest"]
        assert c.fingerprints["blueprint_digest"] != base.fingerprints["blueprint_digest"]


def test_override_path_is_applied_not_created_as_literal_key():
    """`--set a.b=v` 必须写到 a.b，不能造一个字面量键 'a.b'。"""
    c = _compile(overrides={"caliber.label.horizon": 7},
                 allow_constitutive_override=True)
    assert c.plan["caliber"]["label"]["horizon"] == 7
    assert "caliber.label.horizon" not in c.plan
    assert c.origins["caliber.label.horizon"] == "override"
    assert c.origins["caliber.label.transform"] == "plan"


def test_override_is_revalidated():
    """覆盖层与计划原文同权：未知键与类型错误都必须被拦。"""
    assert _err(lambda: _compile(overrides={"nope": 1})) == E1001
    assert _err(lambda: _compile(overrides={"caliber": "oops"},
                                 allow_constitutive_override=True)) == E1004


def test_origins_cover_exactly_every_leaf():
    from v5.contracts.types import leaf_paths

    c = _compile()
    assert set(c.origins) == leaf_paths(c.plan)


def test_null_override_does_not_fall_back_to_baseline():
    """计划显式写 null 时不得静默回落到基线值（schema 应按类型拒绝）。"""
    p = json.loads(json.dumps(PLAN))
    p["caliber"]["label"]["horizon"] = None
    code = _err(lambda: compile_plan(baseline={"caliber": {"label": {"horizon": 5}}},
                                     plan=p))
    assert code == E1004
    for origin in _compile().origins.values():
        assert origin in {"plan", "override"} or origin.startswith("baseline@")


def test_supply_pin_acquired_at_not_in_blueprint():
    """重采一次数据（仅 acquired_at 变）不得改变蓝图指纹。"""
    aliases = _aliases("a" * 16, "2026-11-01T00:00:00+00:00")
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    a1 = compile_plan(baseline=None, plan=p, aliases=aliases, symbol_sets=_SYM_SETS,
                      supply_meta=_meta_for(("ohlcv",)))
    aliases["ohlcv"] = _alias_pin("ohlcv", "a" * 16, "2026-11-16T00:00:00+00:00")
    a2 = compile_plan(baseline=None, plan=p, aliases=aliases, symbol_sets=_SYM_SETS,
                      supply_meta=_meta_for(("ohlcv",)))
    assert a1.fingerprints["blueprint_digest"] == a2.fingerprints["blueprint_digest"]


def test_compiler_step9_runs_gate_b_read_only():
    """第 9 步必须真的跑门禁 B，且失败不留任何副作用。"""
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    aliases = _aliases("a" * 16, "2026-11-01T00:00:00+00:00")
    aliases["ohlcv"]["end_closed"] = False
    assert _err(lambda: compile_plan(baseline=None, plan=p, aliases=aliases,
                                     symbol_sets=_SYM_SETS,
                                     supply_meta=_meta_for(("ohlcv",)))) == E4003


def test_compiler_step9_rejects_stub_alias():
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    assert _err(lambda: compile_plan(baseline=None, plan=p,
                                     aliases={"ohlcv": "v1"})) == E4002


def test_compiler_step9_missing_alias():
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    assert _err(lambda: compile_plan(baseline=None, plan=p, aliases={})) == E4001


# ---- 来源表 -----------------------------------------------------------

def test_origins_mark_plan_vs_baseline():
    # 基于合法 plan，去掉一个叶子让基线补上——验证来源表逐叶子标注
    plan = json.loads(json.dumps(PLAN))
    del plan["caliber"]["label"]["horizon"]
    base = {"caliber": {"label": {"horizon": 5}}}
    c = compile_plan(baseline=base, plan=plan)
    assert c.origins["caliber.label.horizon"] == "baseline@v1"
    assert c.origins["caliber.label.transform"] == "plan"


def test_deep_merge_does_not_concatenate_lists():
    base = {"search": {"algorithms": [{"name": "gp_symbolic"}], "prior_pool": ["mom20"]}}
    plan = json.loads(json.dumps(PLAN))
    plan["search"]["algorithms"] = [{"name": "random_expression", "params": {"max_candidates": 10}}]
    c = compile_plan(baseline=base, plan=plan)
    assert [a["name"] for a in c.plan["search"]["algorithms"]] == ["random_expression"]
    # list 整体替换，不拼接：plan 有自己的 prior_pool 就用 plan 的，
    # baseline 的同名 list 不会混进来（mom20 只出现一次，不是拼接后的两份）
    assert c.plan["search"]["prior_pool"] == PLAN["search"]["prior_pool"]
    assert c.plan["search"]["prior_pool"].count("mom20") == 1


def test_explain_is_readable_and_has_leaf_paths():
    text = _compile().explain()
    assert "caliber.label.horizon" in text
    assert "plan" in text or "baseline@v1" in text


# ---- 门禁 B 四项 -------------------------------------------------------

def _pin(**over):
    data = dict(
        dataset="ohlcv", version="a" * 16,
        checksum=Digest.parse(compute_digest("x")),
        coverage={"start": datetime(2024, 1, 1, tzinfo=timezone.utc),
                  "end": datetime(2026, 9, 13, tzinfo=timezone.utc)},
        symbols_digest=Digest.parse(compute_digest(["BTC", "ETH"])),
        end_closed=True, schema_version="1",
        acquired_at=datetime(2026, 11, 15, tzinfo=timezone.utc),
        # 故意不放 symbols 键：Pin 契约没有标的清单，gate_b 不得依赖它。
        gate_a={"passed": True, "reason_code": "ok"},
    )
    data.update(over)
    return Pin(**data)


_PLAN_B = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
           "supply": {"symbols": ["BTC", "ETH"],
                      "frequency": "1d", "exchange": "binance"}}


def _meta(freq: str = "1d", exchange: str = "binance", market: str = "futures") -> dict:
    """供给事实清单的最小投影：门禁 B 读 timeframe / exchange / market 三项。

    market 是独立维度：同一个 exchange id 下 spot 与 futures 是两份数据，
    少了它「用现货挖的因子」会被标成「合约因子」而不报错。
    """
    return {"timeframe": freq, "exchange": exchange, "market": market}


def _meta_for(names, version: str = "a" * 16) -> dict:
    """按流生成 manifest 投影；dataset/version 必须与别名实际指向的一致。"""
    return {n: {"dataset": n, "version": version, **_meta()} for n in names}


def test_gate_b_ok():
    check_gate_b(_pin(), _PLAN_B, supply_meta=_meta())


def test_gate_b_named_symbol_set():
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
            "supply": {"symbols": "cs_test", "frequency": "1d", "exchange": "binance"}}
    check_gate_b(_pin(), plan, symbol_sets={"cs_test": ["ETH", "BTC", "BTC"]},
                 supply_meta=_meta())


def test_gate_b_coverage_gap():
    # 断言消息含「覆盖区间」：只断言 E4002 会被下一条标的集检查的
    # E4002 掩盖，等于没有守住覆盖检查（review P2-2）。
    plan = {"boundary": {"time_axis": {"mine": "2023-01-01..2026-06-30"}},
            "supply": {"symbols": ["BTC", "ETH"]}}
    with pytest.raises(FlowError) as exc:
        check_gate_b(_pin(), plan, supply_meta=_meta())
    assert exc.value.code == E4002 and "覆盖区间不足" in exc.value.error.message


def test_gate_b_unparsable_time_axis():
    plan = {"boundary": {"time_axis": {"mine": "reserved"}},
            "supply": {"symbols": ["BTC", "ETH"]}}
    assert _err(lambda: check_gate_b(_pin(), plan, supply_meta=_meta())) == E4002


def test_gate_b_symbol_mismatch():
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
            "supply": {"symbols": ["BTC", "DOGE"]}}
    assert _err(lambda: check_gate_b(_pin(), plan, supply_meta=_meta())) == E4002


def test_gate_b_unresolved_symbol_set_fails_closed():
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
            "supply": {"symbols": "cs_unknown"}}
    assert _err(lambda: check_gate_b(_pin(), plan, supply_meta=_meta())) == E4002


def test_gate_b_gate_a_not_passed():
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}}}
    pin = _pin(gate_a={"passed": False, "reason_code": "incomplete_coverage"})
    assert _err(lambda: check_gate_b(pin, plan, supply_meta=_meta())) == E4005


def test_gate_b_acquired_before_coverage_end():
    pin = _pin(acquired_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert _err(lambda: check_gate_b(pin, _PLAN_B, supply_meta=_meta())) == E4002


def test_gate_b_gate_a_passed_must_be_bool():
    # 1/"true" 都不得当作通过（严格 True）
    pin = _pin(gate_a={"passed": 1, "reason_code": "ok"})
    assert _err(lambda: check_gate_b(pin, _PLAN_B, supply_meta=_meta())) == E4005


def test_compiler_require_supply_rejects_empty_alias_table():
    """空别名表 + 空 datasets 不得绕过门禁 B（review P1-1）。"""
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = []
    assert _err(lambda: compile_plan(baseline=None, plan=p, aliases={},
                                     require_supply=True)) == E4001


def test_gate_b_end_not_closed():
    assert _err(lambda: check_gate_b(_pin(end_closed=False), _PLAN_B, supply_meta=_meta())) == E4003


# ---- 门禁 B：供给维度（frequency / exchange）---------------------------

def test_gate_b_frequency_mismatch():
    """计划声明 4h、事实是 1d → 必须失败，否则整条研究口径错而门禁全绿。"""
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
            "supply": {"symbols": ["BTC", "ETH"],
                       "frequency": "4h", "exchange": "binance"}}
    with pytest.raises(FlowError) as exc:
        check_gate_b(_pin(), plan, supply_meta=_meta(freq="1d"))
    assert exc.value.code == E4002 and "频率不符" in exc.value.error.message


def test_gate_b_exchange_mismatch():
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
            "supply": {"symbols": ["BTC", "ETH"],
                       "frequency": "1d", "exchange": "okx"}}
    with pytest.raises(FlowError) as exc:
        check_gate_b(_pin(), plan, supply_meta=_meta(exchange="binanceusdm"))
    assert exc.value.code == E4002 and "交易所不符" in exc.value.error.message


def test_gate_b_requires_manifest_meta():
    """查不到来源不是「来源没问题」：缺 manifest 必须拒绝。"""
    with pytest.raises(FlowError) as exc:
        check_gate_b(_pin(), _PLAN_B)
    assert exc.value.code == E4002 and "manifest" in exc.value.error.message


def test_gate_b_exchange_undeclared_in_manifest_fails_closed():
    plan = {"boundary": {"time_axis": {"mine": "2024-02-01..2026-06-30"}},
            "supply": {"symbols": ["BTC", "ETH"],
                       "frequency": "1d", "exchange": "binance"}}
    assert _err(lambda: check_gate_b(_pin(), plan, supply_meta=_meta(exchange=""))) == E4002


def test_gate_b_non_mapping_plan_is_e4002():
    assert _err(lambda: check_gate_b(_pin(), {"boundary": "x", "supply": {}})) == E4002


def test_compiler_rejects_pin_for_wrong_stream():
    """别名键必须等于 Pin.dataset：ohlcv 指向 funding 的版本是数据串流。"""
    aliases = _aliases("a" * 16, "2026-11-01T00:00:00+00:00")
    aliases["ohlcv"] = _alias_pin("funding", "a" * 16, "2026-11-01T00:00:00+00:00")
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    assert _err(lambda: compile_plan(
        baseline=None, plan=p, aliases=aliases, symbol_sets=_SYM_SETS,
        supply_meta=_meta_for(("ohlcv",)),
    )) == E4001


def test_compiler_rejects_alias_without_readable_manifest():
    """别名存在但 seed manifest 读不到 → E4001（别名指向不存在的版本）。"""
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    assert _err(lambda: compile_plan(
        baseline=None, plan=p, aliases=_aliases("a" * 16, "2026-11-01T00:00:00+00:00"),
        symbol_sets=_SYM_SETS, supply_meta={},
    )) == E4001


# ---- charter 四类加载 --------------------------------------------------

def test_charter_missing_file_is_e1008(tmp_path):
    assert _err(lambda: load_baseline(tmp_path / "none.json")) == E1008
    assert _err(lambda: load_plan(tmp_path / "none.json")) == E1008
    assert _err(lambda: load_catalog(tmp_path / "none.json")) == E1008


def test_charter_baseline_rejects_preset_inheritance(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"baseline_version": "v1", "inherits": "other"}), encoding="utf-8")
    assert _err(lambda: load_baseline(p)) == E1008
    p.write_text(json.dumps({"baseline_version": "v1"}), encoding="utf-8")
    assert load_baseline(p)["baseline_version"] == "v1"


def test_charter_loaders_share_one_json_reader(tmp_path):
    """三个加载器（plan / baseline / catalog）必须走同一个 JSON 读取器。

    方案 B 之后不再有第四个「别名表加载器」——别名表本身已不存在，
    供给身份由 `store.read_dataset` 现算。
    """
    cases = (
        ("plan", load_plan, {"k": "plan"}, "k"),
        # baseline 要校验 baseline_version（它是基线标识，不是普通配置块）
        ("baseline", load_baseline, {"baseline_version": "v1"}, "baseline_version"),
        ("catalog", load_catalog, {"k": "catalog"}, "k"),
    )
    for name, loader, payload, key in cases:
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        assert loader(p)[key] == payload[key]




def test_compiler_rejects_manifest_of_other_version():
    """manifest 必须是这一流这一版本的事实，否则门禁 B 比的是别人的口径。"""
    p = json.loads(json.dumps(PLAN))
    p["supply"]["datasets"] = ["ohlcv"]
    aliases = _aliases("a" * 16, "2026-11-01T00:00:00+00:00")
    meta = {"ohlcv": {"dataset": "ohlcv", "version": "b" * 16,
                      "timeframe": "1d", "exchange": "binance"}}
    assert _err(lambda: compile_plan(
        baseline=None, plan=p, aliases=aliases, symbol_sets=_SYM_SETS,
        supply_meta=meta,
    )) == E4004
