"""flow stations —— 八工位规格自省（只读，O1 用）。"""

from __future__ import annotations

from typing import Any

from v5.orchestration import STATION_SPECS
from v5.runtime.compiler import STATIONS

from .common import envelope_ok


def cmd_stations(args: Any) -> dict[str, Any]:
    return envelope_ok("stations", None, {
        "stations": list(STATIONS),
        "count": len(STATION_SPECS),
    })
