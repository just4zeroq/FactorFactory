"""章程加载（s0/03-runtime.md §3）：基线、计划、别名、名字目录。

Charter 编译期只读，不属于任何执行。基线单文件、不做 preset 继承（Q3 已收敛）。
四类都是「文件不存在 → E1008」（缺必填/未解析占位），不静默返回空。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from v5.contracts.errors import E1008, FlowError


def load_json(path: str | Path, *, label: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FlowError(E1008, f"{label} 文件不存在: {p}", path=str(p))
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FlowError(
            E1008, f"{label} JSON 解析失败: {exc}", path=str(p),
            hint="章程文件必须是合法 JSON",
        ) from exc
    if not isinstance(data, dict):
        raise FlowError(E1008, f"{label} 顶层必须是对象，得到 {type(data).__name__}", path=str(p))
    return data


def load_baseline(path: str | Path) -> dict[str, Any]:
    data = load_json(path, label="baseline")
    if "baseline_version" not in data:
        raise FlowError(E1008, "baseline 缺 baseline_version", path="baseline_version")
    # Q3：基线单文件，不做 preset 继承。出现 inherits/extends/preset 即拒绝，
    # 避免「同一路径多处定义」的第二真相。
    for key in ("inherits", "extends", "preset"):
        if key in data:
            raise FlowError(
                E1008, f"基线不得声明 '{key}'（基线单文件，不做 preset 继承）", path=key,
            )
    return data


def load_plan(path: str | Path) -> dict[str, Any]:
    return load_json(path, label="plan")


def load_catalog(path: str | Path) -> dict[str, Any]:
    """名字目录 catalog/impls.json：station → name → 实现元信息。"""
    return load_json(path, label="catalog")
