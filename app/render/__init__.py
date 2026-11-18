"""应用层渲染（09-app-storage.md §2.5）。

三条硬约束：
- 不做单位换算/「美化」：数值原样输出，单位写在字段名里。
- 警告与错误分开：`warnings` 不影响退出码，`errors` 影响。
- `ok` 与 `exit_code` 恒一致，由信封构造保证，渲染层不得改。
"""

from __future__ import annotations

from typing import Any, Mapping

FORMATS = ("table", "json", "ndjson")


def render(envelope: Mapping[str, Any], fmt: str = "json") -> str:
    """把信封渲染为字符串。未知格式 → ValueError（由 main 转 E1004）。"""
    if fmt not in FORMATS:
        raise ValueError(f"未知渲染格式: {fmt}")
    if fmt == "json":
        from .json_fmt import render_json
        return render_json(envelope)
    if fmt == "ndjson":
        from .ndjson import render_ndjson
        return render_ndjson(envelope)
    from .table import render_table
    return render_table(envelope)


__all__ = ["render", "FORMATS"]
