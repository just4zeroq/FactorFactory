"""计划 schema 的契约测试（对应 18-plan-schema.md §8 的 AC59–AC62）。

这组测试的目的不是"验证校验器写得对"，而是**让文档不能漂移**：
文档里的 YAML 片段、规范样例、schema 三者必须同时自洽，否则失败。
本轮之前的三处漂移（funding_period / min_cross_section / 杠杆三名字）都是
"规则在一篇、示例在另一篇"造成的，没有任何检查同时读两处。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from v5.contracts.errors import FlowError
from v5.contracts.plan_schema import (
    assert_valid_plan,
    load_schema,
    validate_plan,
)

ROOT = Path(__file__).resolve().parents[2]
V5 = ROOT / "docs" / "architecture" / "v5"
EXAMPLE = V5 / "examples" / "plan_cs_2026q4.yaml"


def load_example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def codes(violations) -> set[str]:
    return {v.code for v in violations}


# --------------------------------------------------------------------------
# AC59 · 规范样例必须零违规
# --------------------------------------------------------------------------


def test_canonical_example_is_valid():
    assert validate_plan(load_example()) == []


def test_canonical_example_covers_all_required_root_keys():
    schema = load_schema()
    plan = load_example()
    required = {k for k, v in schema["root"].items() if v.get("required")}
    missing = required - set(plan)
    assert not missing, f"规范样例缺必填根键：{missing}"


def test_canonical_example_pins_removed_keys_absence():
    """已删的键不能在样例里复活。单独断言，免得改 schema 时顺手把它们加回来。"""
    text = EXAMPLE.read_text(encoding="utf-8")
    for gone in ("funding_period", "funding_interval_hours", "min_cross_section"):
        assert gone not in text, f"{gone} 已删除（见 18 §4.5），不得回到样例里"


# --------------------------------------------------------------------------
# AC60 · 已删键与常见写错必须被拒，且提示可操作
# --------------------------------------------------------------------------


def _with(path: str, value) -> dict:
    """在规范样例上按点路径塞一个值。"""
    plan = load_example()
    node = plan
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value
    return plan


@pytest.mark.parametrize(
    ("path", "hint_token"),
    [
        ("caliber.execution.funding_period", "数据属性"),
        ("caliber.execution.funding_interval_hours", "I34"),
        ("caliber.label.min_cross_section", "min_width"),
    ],
)
def test_removed_caliber_keys_are_rejected_with_hint(path, hint_token):
    violations = validate_plan(_with(path, 8))
    assert violations, f"{path} 应被拒绝"
    hit = [v for v in violations if v.path == path]
    assert hit, f"违规路径应精确指向 {path}，实际 {[v.path for v in violations]}"
    assert hit[0].code == "E1001"
    assert hint_token in (hit[0].hint or ""), f"提示里应说明该怎么写：{hit[0].hint}"


def test_unknown_root_key_gets_closest_candidate():
    violations = validate_plan(_with("target", {"impl": "forward_return_cs", "horizn": 5}))
    hit = [v for v in violations if v.code == "E1001"]
    assert hit and hit[0].path == "target.horizn"
    assert "horizon" in hit[0].hint


def test_missing_required_is_e1008_not_defaulted():
    plan = load_example()
    plan["caliber"]["label"].pop("min_width")
    violations = validate_plan(plan)
    hit = [v for v in violations if v.code == "E1008"]
    assert any(v.path == "caliber.label.min_width" for v in hit)


def test_params_is_an_open_map_at_this_layer():
    """`params` 内部不在本层校验：类型是 `population: "many"` 还是 `max_dept` 多余，
    由实现的 `param_schema` 判定（E1003 / E1004，见 18 §5、s0/04）。

    这条测试钉住的是**边界**而不是功能：本层若开始校验 params 内部，就会出现
    「schema 与实现各有一套参数规则」的第二真相。它只保证 params 是个对象。
    """
    plan = load_example()
    plan["search"]["algorithms"][0]["params"]["population"] = "many"
    assert validate_plan(plan) == []

    plan = load_example()
    plan["panel"]["params"] = ["not", "an", "object"]
    assert "E1004" in codes(validate_plan(plan))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("caliber.execution.annualisation", "calendar_252"),  # 加密只允许 365
        ("caliber.execution.entry", "current_close"),  # 同一根 bar 成交 = 前视
        ("caliber.label.transform", "cs_rank2"),
    ],
)
def test_illegal_enum_values_are_rejected(path, value):
    assert "E1004" in codes(validate_plan(_with(path, value)))


# --------------------------------------------------------------------------
# C20 · 口径不得写进工位参数
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("station", "key", "owner"),
    [
        ("target", "horizon", "caliber.label"),
        ("certify", "max_weight", "caliber.allocate"),
        ("boundary", "cost_tiers", "caliber.net"),
        ("evidence", "rebalance", "caliber.execution"),
        # portfolio 曾不在 `_collect_params` 的遍历范围里，于是这一站写口径
        # 字段时 E1020 完全看不见——口径合法地存在两份且不报错。
        ("portfolio", "cost_tiers", "caliber.net"),
    ],
)
def test_caliber_key_in_params_is_rejected(station, key, owner):
    plan = load_example()
    target = plan[station]
    target.setdefault("params", {})[key] = 1
    violations = validate_plan(plan)
    hit = [v for v in violations if v.code == "E1020"]
    assert hit, f"{station}.params.{key} 应触发 E1020"
    assert hit[0].path == f"{station}.params.{key}"
    assert owner in hit[0].hint


def test_assert_valid_plan_raises_with_first_violation():
    with pytest.raises(FlowError) as exc:
        assert_valid_plan(_with("caliber.label.transform", "nope"))
    assert exc.value.error.code == "E1004"
    assert exc.value.error.path == "caliber.label.transform"


# --------------------------------------------------------------------------
# X1–X7 · 跨字段规则（单键各自合法，组合起来不合法）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "mutate", "expect_code"),
    [
        ("X1 维持 >= 开仓", lambda p: p["caliber"]["net"].update(maintenance_margin=0.2), "E1004"),
        ("X2 成本档升序", lambda p: p["caliber"]["net"].update(cost_tiers=[60, 20, 150]), "E1004"),
        ("X3 权重可行性", lambda p: p["caliber"]["allocate"].update(max_weight=0.6, min_members=3), "E1004"),
        (
            "X4 重叠标签",
            lambda p: (
                p["caliber"]["label"].update(horizon=10, rebalance=1),
                p["caliber"]["label"].pop("overlap_policy"),
            ),
            "E1008",
        ),
        (
            "X5 中性化窗口",
            lambda p: p["caliber"]["label"].pop("neutralise_window"),
            "E1008",
        ),
        (
            "X6 组合窗口顺序",
            lambda p: p["portfolio"]["window"].update(fit="2024-07-01..2026-04-02"),
            "E1021",
        ),
        (
            "X7 时间轴重叠",
            lambda p: p["boundary"]["time_axis"].update(oos="2026-09-01..2026-09-13", holdout="2026-09-10..2026-10-01"),
            "E1008",
        ),
    ],
)
def test_cross_field_rules(rule, mutate, expect_code):
    plan = load_example()
    mutate(plan)
    violations = validate_plan(plan)
    assert expect_code in codes(violations), f"{rule} 未触发 {expect_code}：{violations}"


# --------------------------------------------------------------------------
# AC61 · 文档不手抄：文档里的每个 YAML 片段都必须能过 schema 的键检查
# --------------------------------------------------------------------------


def _iter_yaml_blocks():
    for path in sorted(V5.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"```ya?ml\n(.*?)```", text, re.S):
            yield path, m.group(1)


def _doc_blocks() -> list[tuple[Path, dict]]:
    out = []
    for path, body in _iter_yaml_blocks():
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            out.append((path, data))
    return out


def test_document_yaml_blocks_use_only_legal_caliber_keys():
    schema = load_schema()
    caliber_spec = schema["root"]["caliber"]["keys"]
    bad: list[str] = []
    checked = 0
    for path, data in _doc_blocks():
        cal = data.get("caliber")
        if not isinstance(cal, dict):
            continue
        for sub, subvalue in cal.items():
            if sub not in caliber_spec:
                bad.append(f"{path.name}: caliber.{sub} 不是合法子块")
                continue
            if not isinstance(subvalue, dict):
                continue
            legal = caliber_spec[sub].get("keys", {})
            for key in subvalue:
                checked += 1
                if key not in legal:
                    bad.append(f"{path.name}: caliber.{sub}.{key} 不在 schema 里")
    assert checked > 0, "没有扫到任何 caliber 片段，扫描逻辑失效了"
    assert not bad, "文档里的 YAML 与 schema 不一致：\n" + "\n".join(bad)


def test_document_yaml_blocks_do_not_put_caliber_into_params():
    names = {
        key
        for sub in load_schema()["root"]["caliber"]["keys"].values()
        for key in sub.get("keys", {})
    }

    def walk(node, where, hits):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "params" and isinstance(value, dict):
                    leaked = set(value) & names
                    if leaked:
                        hits.append(f"{where}.params 里出现口径键 {sorted(leaked)}")
                else:
                    walk(value, f"{where}.{key}", hits)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{where}[{i}]", hits)

    hits: list[str] = []
    for path, data in _doc_blocks():
        local: list[str] = []
        walk(data, path.stem, local)
        hits.extend(local)
    assert not hits, "\n".join(hits)


# --------------------------------------------------------------------------
# AC61 的另一半 · schema 的每个键都必须在 18 里出现（反向核对）
# --------------------------------------------------------------------------


def test_every_caliber_key_is_documented():
    doc = (V5 / "18-plan-schema.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for sub, spec in load_schema()["root"]["caliber"]["keys"].items():
        if sub not in doc:
            missing.append(f"子块 {sub}")
        for key in spec.get("keys", {}):
            if key not in doc:
                missing.append(f"caliber.{sub}.{key}")
    assert not missing, "这些键在 schema 里存在但 18 没写：" + ", ".join(missing)


def test_document_declares_removed_keys_and_reasons():
    """删掉的键必须在 18 §4.5 留下理由，否则会被下一个觉得不方便的人加回来。"""
    doc = (V5 / "18-plan-schema.md").read_text(encoding="utf-8")
    for gone in ("funding_period", "min_cross_section", "gross_exposure", "allow_leverage"):
        assert gone in doc, f"{gone} 已删除，18 §4.5 必须记录原因"

# --------------------------------------------------------------------------
# 基线与计划必须同构（s0/03 §3.1 约束 4）
# --------------------------------------------------------------------------


def _baseline_from_doc() -> dict:
    """从 s0/03 的示例里取出 baseline.json。文档示例也要被校验，否则它只是散文。"""
    text = (V5 / "s0" / "03-runtime.md").read_text(encoding="utf-8")
    for m in re.finditer(r"```json\n(.*?)```", text, re.S):
        if "baseline_version" in m.group(1):
            return json.loads(m.group(1))
    raise AssertionError("s0/03-runtime.md 里找不到 baseline.json 示例")


def _shape(node, path=""):
    """把文档结构归一成可比较的形状。

    `*.params` 折叠成 `<map>`：它的内部归各实现的 `param_schema`（见 18 §5），
    基线给默认实现留空 `{}` 与计划填满参数是合法差异。唯一例外是
    `verdict.params`——它的三个键由 schema 声明，必须逐项对上。
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key == "params" and path != "verdict":
                out[key] = "<map>"
            else:
                out[key] = _shape(value, child)
        return out
    if isinstance(node, list):
        return "<list>"
    return "<scalar>"


def _diff(a, b, prefix=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in a:
                out.append(f"{path} 只在基线里有")
            elif key not in b:
                out.append(f"{path} 只在计划里有")
            else:
                out.extend(_diff(a[key], b[key], path))
    elif a != b:
        out.append(f"{prefix}: {a!r} vs {b!r}")
    return out


def _conditional_paths(node=None, path="") -> set[str]:
    """schema 里带 `required_if` 的键：条件不成立时允许缺省（s0/03 §3.1 约束 5）。"""
    node = load_schema()["root"] if node is None else node
    out: set[str] = set()
    for key, spec in (node.items() if isinstance(node, dict) else []):
        if not isinstance(spec, dict):
            continue
        child = f"{path}.{key}" if path else key
        if "required_if" in spec:
            out.add(child)
        if spec.get("type") == "block":
            out |= _conditional_paths(spec.get("keys", {}), child)
    return out


def test_baseline_is_isomorphic_to_plan():
    """基线与计划必须是同一份 schema 的两个实例。

    不同构的后果不是"文档难看"：来源表（s0/03 §5.2）要回答"这个值来自哪一层"，
    两套结构就只能靠人读代码翻译，`flow explain` 也就答不出逐项来源。
    """
    plan_shape = _shape(load_example())
    base_shape = _shape(_baseline_from_doc())
    for extra in ("plan", "baseline"):
        plan_shape.pop(extra, None)
    for extra in ("baseline_version", "released_at"):
        base_shape.pop(extra, None)
    allowed = _conditional_paths()
    diffs = [d for d in _diff(plan_shape, base_shape) if d.split(" 只在")[0] not in allowed]
    assert diffs == []


def test_baseline_never_gives_a_lenient_default_to_gates():
    """判据阈值必须为 null：给一个"合理默认"等于没做决定。"""
    base = _baseline_from_doc()
    params = base["verdict"]["params"]
    assert set(params) == {"dsr_min", "fdr_max", "cost_adj_min"}
    assert all(v is None for v in params.values())

# --------------------------------------------------------------------------
# 走查文档的摘录必须与规范样例一致（否则又变成两个来源）
# --------------------------------------------------------------------------


def _walkthrough_plan() -> dict:
    text = (V5 / "17-walkthrough.md").read_text(encoding="utf-8")
    for m in re.finditer(r"```ya?ml\n(.*?)```", text, re.S):
        data = yaml.safe_load(m.group(1))
        if isinstance(data, dict) and "caliber" in data:
            return data
    raise AssertionError("17-walkthrough.md 里找不到计划摘录")


def _subset_diff(small, big, prefix=""):
    """small 里的每个键值都必须在 big 里相等；big 多出来的键不算错（摘录可以更短）。"""
    out = []
    if isinstance(small, dict):
        if not isinstance(big, dict):
            return [f"{prefix}: 类型不同"]
        for key, value in small.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in big:
                out.append(f"{path} 在规范样例里不存在")
            else:
                out.extend(_subset_diff(value, big[key], path))
    elif small != big:
        out.append(f"{prefix}: 摘录 {small!r} != 样例 {big!r}")
    return out


def test_walkthrough_excerpt_matches_canonical_example():
    """17 的摘录必须能对上 `examples/plan_cs_2026q4.yaml`。

    摘录是给人读的，但它同时是"这次走查用了什么口径"的唯一记录。两处不一致时，
    走查结论就对应不到任何可执行文件——这正是本轮 `funding_period` 那类漂移的形态。
    """
    example = load_example()
    assert _subset_diff(_walkthrough_plan(), example) == []


# --------------------------------------------------------------------------
# supply.market · 门禁 B 的第三道护栏（曾不在 schema 白名单里）
# --------------------------------------------------------------------------






# --------------------------------------------------------------------------
# supply.market · 门禁 B 的第三道护栏（曾不在 schema 白名单里）
# --------------------------------------------------------------------------
@pytest.mark.parametrize("market", ["spot", "futures"])
def test_supply_market_is_a_legal_key(market):
    """`market` 被 gate_b / common / station 消费，却曾不是 schema 合法键。

    后果：任何写了 `supply.market` 的计划在编译期就被 E1001 拒，门禁 B 的
    「计划声明 == 事实清单」那一栏永远走不到——护栏存在，但入口先被堵死。
    """
    plan = load_example()
    plan["supply"]["market"] = market
    assert [v for v in validate_plan(plan) if v.code == "E1001"] == []


def test_supply_market_rejects_unknown_value():
    plan = load_example()
    plan["supply"]["market"] = "options"
    assert "E1004" in codes(validate_plan(plan))


def test_spot_market_rejects_contract_only_datasets():
    """现货只有 ohlcv：funding / OI / basis 的定义域是合约。

    允许它们出现在 spot 下，只会让采集器逐个标的失败再把失败记成 skipped，
    命令照样退出 0 —— 标准的假成功。
    """
    plan = load_example()
    plan["supply"]["market"] = "spot"
    plan["supply"]["datasets"] = ["ohlcv", "funding"]
    assert "E1004" in codes(validate_plan(plan))




def test_caliber_fields_derive_from_schema():
    """E1020 的字段清单必须从 schema 派生，不能手抄。

    手抄一份的后果：schema 加了口径键、清单没跟，那个键就能同时出现在
    `caliber` 与某个工位 `params` 里——口径有两份，且不报错。
    """
    from v5.contracts.plan_schema import caliber_field_names, load_schema
    from v5.runtime.compiler import CALIBER_FIELDS

    expected = set()
    for sub in load_schema()["root"]["caliber"]["keys"].values():
        expected.update(sub.get("keys", {}))
    assert set(caliber_field_names()) == expected
    assert set(CALIBER_FIELDS) == expected
    # 新加的口径键必须自动进入清单（而不是靠人去补字面量）
    assert "slippage_bp" in expected
