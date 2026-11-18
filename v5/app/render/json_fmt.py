"""JSON 渲染：信封逐字输出，不重排、不换算（09 §2.5）。"""

from __future__ import annotations

import json
from typing import Any, Mapping


def render_json(envelope: Mapping[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False, default=str)
