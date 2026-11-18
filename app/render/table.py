"""表格渲染：给人看，不改变任何值（09 §2.5「渲染层不得做单位换算」）。

仅支持扁平映射与「dict 列表」两种形态；复杂嵌套回退到 JSON 子串。
"""

from __future__ import annotations

import json
from typing import Any, Mapping


def _flat(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "" if value is None else str(value)


def render_table(envelope: Mapping[str, Any]) -> str:
    status = "OK" if envelope.get("ok") else "ERROR"
    head = [f"command: {envelope.get('command')}",
            f"status: {status}",
            f"exit_code: {envelope.get('exit_code')}"]
    if envelope.get("run_id"):
        head.insert(1, f"run_id: {envelope['run_id']}")
    data = envelope.get("data")
    lines = list(head)
    if isinstance(data, Mapping):
        for key, value in data.items():
            if isinstance(value, list) and all(isinstance(x, Mapping) for x in value):
                lines.append(f"\n[{key}] ({len(value)} rows)")
                if value:
                    cols = list(value[0])
                    lines.append("  " + " | ".join(cols))
                    for row in value:
                        lines.append("  " + " | ".join(_flat(row.get(c)) for c in cols))
            else:
                lines.append(f"{key}: {_flat(value)}")
    errors = envelope.get("errors") or []
    for err in errors:
        lines.append(f"error: {err.get('code')}: {err.get('message')}")
    warnings = envelope.get("warnings") or []
    for w in warnings:
        lines.append(f"warn: {_flat(w)}")
    return "\n".join(lines)
