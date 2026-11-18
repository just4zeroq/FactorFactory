"""样例计划的两种形态必须等价。

`run` 只解析 JSON（`charter.load_plan` → `json.loads`），YAML 那份是给人读的
规范文本（带注释、能被文档引用）。两份一旦漂移，README 里的命令就会拿旧计划
去跑，而文档还声称它们等价——这类「文档说的和跑的不是一回事」正是本项目
最想消灭的东西，所以用一条测试钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML_PLAN = ROOT / "docs" / "architecture" / "v5" / "examples" / "plan_cs_2026q4.yaml"
JSON_PLAN = ROOT / "examples" / "plan_cs_2026q4.json"


def test_yaml_and_json_example_are_equivalent() -> None:
    assert YAML_PLAN.exists(), f"规范样例缺失：{YAML_PLAN}"
    assert JSON_PLAN.exists(), f"可运行样例缺失：{JSON_PLAN}"
    from_yaml = yaml.safe_load(YAML_PLAN.read_text(encoding="utf-8"))
    from_json = json.loads(JSON_PLAN.read_text(encoding="utf-8"))
    assert from_yaml == from_json, (
        "docs 里的 YAML 样例与 examples 里的 JSON 已漂移；"
        "改一份就要重新生成另一份"
    )


def test_demo_plan_points_at_shipped_symbol_set() -> None:
    """demo 计划只写标的集名；这个名字必须在随仓库提交的标的集表里存在。

    否则用户按 README 走到的第一步就是 `E1004 标的集不存在`。
    """
    plan = json.loads(
        (ROOT / "examples" / "plan_demo_crypto20.json").read_text(encoding="utf-8")
    )
    name = plan["supply"]["symbols"]
    table = json.loads(
        (ROOT / "examples" / "symbol_sets.json").read_text(encoding="utf-8")
    )
    members = (table.get("sets") or table).get(name)
    if isinstance(members, dict):
        members = members.get("symbols")
    assert isinstance(members, list) and members, f"标的集 {name} 不在 examples/symbol_sets.json"
