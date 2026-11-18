"""flow inspect —— 查一次执行落盘了什么（只读）。

S1 的占位实现恒返回空记录，于是「run 报 ok、产物在哪」只能靠翻目录。现在按
`runs/<ticket>/parcels/*/payload.json` 逐工位读数，并给出每个工位的摘要：

- `station`：工位名；
- `keys`：payload 顶层键（用来判断这一站到底算没算）；
- `count`：候选 / 逐候选结果的数量，是「有没有内容」最快的判据；
- `evidence_grade` / `state` / `skip_reason`：零晋升时 Verdict 为空，必须能
  看出来这是结论而不是故障。

`--station` 只返回某一站，`--full` 带完整 payload（默认只给摘要，避免一次
inspect 把整份候选集倒进终端）。
"""

from __future__ import annotations

from typing import Any

from contracts.errors import E1008, FlowError
from runtime.store import resolve_data_root
from storage.catalog import read_ticket
from storage.layout import Layout

from .common import envelope_ok

_COUNT_KEYS = ("candidates", "per_candidate", "folds", "parcels", "traces")


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for key in _COUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, (list, dict)):
            counts[key] = len(value)
    out: dict[str, Any] = {"keys": sorted(payload.keys()), "count": counts}
    for key in ("family", "dedup_digest", "evidence_grade", "n_trials_used",
                "state", "skip_reason", "net_return", "n_trials"):
        if key in payload:
            out[key] = payload[key]
    return out


def cmd_inspect(args: Any) -> dict[str, Any]:
    layout = Layout(resolve_data_root())
    info = read_ticket(layout, args.run_id)
    wanted = getattr(args, "station", None)
    if wanted and wanted not in info["payloads"]:
        raise FlowError(
            E1008,
            f"本次执行没有 {wanted!r} 的产物（已落盘: {info['stations'] or '无'}）",
            path="inspect.station",
        )

    names = [wanted] if wanted else list(info["stations"])
    full = bool(getattr(args, "full", False))
    records = []
    for name in names:
        payload = info["payloads"][name]
        row: dict[str, Any] = {"station": name}
        if full:
            row["payload"] = payload
        else:
            row.update(_summary(payload))
        records.append(row)

    return envelope_ok("inspect", args.run_id, {
        "run_id": args.run_id,
        "records": records,
        "stations": list(info["stations"]),
        "root": info["root"],
        "plan": (info["frozen"].get("plan") or {}).get("plan"),
    })
