"""NDJSON 渲染：信封一行；data 里的列表每条单独一行（带 _kind 标记）。"""

from __future__ import annotations

import json
from typing import Any, Mapping


def render_ndjson(envelope: Mapping[str, Any]) -> str:
    """第一行永远是完整信封；data 里的列表随后逐项展开为明细行。

    列表行不能取代信封：ok / exit_code / run_id / warnings 是 CLI 的唯一
    机器契约，丢掉后 `run --format ndjson` 与 `--format json` 语义不等价。
    """
    lines = [json.dumps(envelope, ensure_ascii=False, default=str)]
    data = envelope.get("data")
    if isinstance(data, Mapping):
        for key, items in data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                rec = {"_kind": key}
                if isinstance(item, Mapping):
                    rec.update(item)
                else:
                    rec["value"] = item
                lines.append(json.dumps(rec, ensure_ascii=False, default=str))
    return "\n".join(lines)
