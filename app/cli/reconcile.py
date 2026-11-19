"""flow reconcile —— 登记库 / 产物链 / 对象存储三方一致性核对（15-operations §4-8/§5）。

只读。发现损坏不是失败，是**发现**：索引可重建、对象可重算，唯一不可
重建的是登记库（append-only jsonl）——所以核对的重心是「登记库是否还
能完整回答发生过什么」。

三类检查（对应 §5 三类存储）：

1. **登记**：`db/*.jsonl` 每行可解析、digest 字段格式合法；一条都不少
   （丢一条 = 丢一次试验计数 = 门槛失真）。
2. **产物链**：每个 ticket 的 parcel.json → payload 文件存在且
   payload_digest 逐字节对拍；lineage 声明的上游 parcel 存在。
3. **对象**：parcel payload 里声明的每个 `*_ref` 在对象存储里有同名文件
   且带 schema sidecar；对象文件自身做 blob_digest 对拍（抽全量）。

发现即退出码 3（契约违反）：损坏状态不是「执行失败」，是「事实层不再
自洽」，继续跑只会把不一致放大。修复路径按 §5：索引/产物重算、对象从
上游重建、登记库从备份恢复。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contracts.digest import blob_digest
from runtime.store import resolve_data_root
from storage.layout import Layout

from .common import envelope_ok

_REGISTER_NAMES = ("trials", "events", "published", "members")
_MEMORY_MARKERS = frozenset({"opaque", "array"})


def _derived_ref_fields(capability: str) -> frozenset[str]:
    """该能力的哪些 `<field>_ref` 是**落盘对象引用**（其余是原生 content-digest）。

    与 capability_bridge.invoke 的落盘规则同源：只有「内存通道
    （opaque/array）且能力没有原生返回同名 ref」的字段才会经 ObjectStore
    落成对象。panel_ref / score_ref / weights_ref 这类原生 ref 是
    array_digest/对象身份，设计上就没有文件——把它们当对象核对是误报。
    """
    # 空能力名 = 这个 station 本来就没有能力映射（如纯记账站），正常跳过。
    # 能力名存在但注册表读不出来 = 校验器自身故障，此时若按空处理会让对象
    # 引用检查被静默跳过、最后还报 clean:True —— 这正是 reconcile 要抓的
    # 「事实层不自洽」。必须抛错，不能 fail-open。
    if not capability:
        return frozenset()
    from capability.params import schema_for
    from contracts.errors import E3003, FlowError
    try:
        outputs = schema_for(capability).get("outputs") or {}
    except Exception as exc:
        raise FlowError(
            E3003, f"能力 {capability!r} 的 schema 不可读，无法核对对象引用",
            path="reconcile.registry",
            context={"error": f"{type(exc).__name__}: {exc}"},
            hint="注册表损坏时核对结论无意义；修复后重跑，不得按空跳过",
        ) from exc
    native_refs = {k for k, v in outputs.items()
                   if v == "str" and str(k).endswith("_ref")}
    derived = {
        f"{k}_ref" for k, v in outputs.items()
        if v in _MEMORY_MARKERS and f"{k}_ref" not in native_refs
    }
    return frozenset(derived)


def _digest_of_text(text: str) -> str:
    """任何登记行里像 digest 的字段都按 `sha256:<64hex>` 校验格式。"""
    return text if (
        isinstance(text, str) and text.startswith("sha256:")
        and len(text) == 71
        and all(c in "0123456789abcdef" for c in text[7:])
    ) else ""


def _check_registers(db_dir: Path, violations: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in _REGISTER_NAMES:
        path = db_dir / f"{name}.jsonl"
        if not path.exists():
            counts[name] = 0
            continue
        rows = 0
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                violations.append({
                    "kind": "register_unparseable", "register": name,
                    "line": no,
                })
                continue
            rows += 1
            for key, value in record.items():
                if isinstance(value, str) and value.startswith("sha256:") \
                        and not _digest_of_text(value):
                    violations.append({
                        "kind": "register_bad_digest", "register": name,
                        "line": no, "field": key,
                    })
        counts[name] = rows
    return counts


def _check_tickets(runs_dir: Path, facts_dir: Path,
                   violations: list[dict[str, Any]]) -> tuple[int, int]:
    """产物链 + 对象引用核对。返回 (ticket 数, parcel 数)。"""
    tickets = 0
    parcels = 0
    if not runs_dir.exists():
        return 0, 0
    for ticket_dir in sorted(runs_dir.iterdir()):
        if not (ticket_dir / "ticket.json").exists() \
                and not list(ticket_dir.glob("parcels/*")):
            continue
        tickets += 1
        digest_by_station: dict[str, str] = {}
        for parcel_json in sorted(ticket_dir.glob("parcels/*/parcel.json")):
            parcels += 1
            record = json.loads(parcel_json.read_text(encoding="utf-8"))
            station = str(record.get("station", parcel_json.parent.name))
            payload_path = ticket_dir / str(record["payload_ref"]["relpath"])
            if not payload_path.exists():
                violations.append({
                    "kind": "payload_missing", "ticket": ticket_dir.name,
                    "station": station, "path": str(payload_path),
                })
                continue
            content = payload_path.read_bytes()
            payload_digest = record.get("payload_digest")
            if payload_digest:
                # payload 是 JSON 结构对象，按规范化 digest 对拍（与
                # executor._load_committed 同一条规则）。
                from contracts.digest import compute_digest
                if compute_digest(json.loads(content)) != payload_digest:
                    violations.append({
                        "kind": "payload_digest_mismatch",
                        "ticket": ticket_dir.name, "station": station,
                    })
            digest_by_station[station] = str(record.get("digest", ""))
            # 对象引用：只有**派生数据通道**的 *_ref 才必须落在对象存储
            # （注册表分类，见 _derived_ref_fields）；原生 content-digest
            # ref（array_digest/对象身份）设计上没有文件，不按对象核对。
            from orchestration.capability_bridge import STATION_CAPABILITY
            derived_refs = _derived_ref_fields(STATION_CAPABILITY.get(station, ""))
            payload = json.loads(content)
            for key, value in payload.items():
                if not (key.endswith("_ref") and isinstance(value, str)
                        and value.startswith("sha256:")):
                    continue
                if key not in derived_refs:
                    continue
                short = value.split(":", 1)[1]
                for ext in (".parquet", ".json"):
                    obj = facts_dir / "parcel_data" / f"{short}{ext}"
                    if obj.exists():
                        if blob_digest(obj.read_bytes()) != value:
                            violations.append({
                                "kind": "object_digest_mismatch",
                                "ticket": ticket_dir.name, "station": station,
                                "field": key,
                            })
                        sidecar = Path(str(obj) + ".schema.json")
                        if ext == ".parquet" and not sidecar.exists():
                            violations.append({
                                "kind": "sidecar_missing",
                                "ticket": ticket_dir.name, "station": station,
                                "field": key,
                            })
                        break
                else:
                    violations.append({
                        "kind": "object_missing", "ticket": ticket_dir.name,
                        "station": station, "field": key,
                    })
        # lineage：声明依赖的上游 parcel 必须存在
        for parcel_json in sorted(ticket_dir.glob("parcels/*/parcel.json")):
            record = json.loads(parcel_json.read_text(encoding="utf-8"))
            for name, digest in (record.get("lineage") or []):
                if name not in digest_by_station:
                    violations.append({
                        "kind": "lineage_missing",
                        "ticket": ticket_dir.name,
                        "station": str(record.get("station")),
                        "upstream": str(name),
                    })
                elif digest and digest_by_station[name] != digest:
                    violations.append({
                        "kind": "lineage_digest_mismatch",
                        "ticket": ticket_dir.name,
                        "station": str(record.get("station")),
                        "upstream": str(name),
                    })
    return tickets, parcels


def cmd_reconcile(args: Any) -> dict[str, Any]:
    root = Path(resolve_data_root())
    layout = Layout(root)
    violations: list[dict[str, Any]] = []
    registers = _check_registers(layout.db, violations)
    tickets, parcels = _check_tickets(
        layout.runs, layout.facts, violations)
    if violations:
        # 事实层不再自洽 = 系统不变量被破坏（E2005 契约违反，exit 3）。
        # violations 全量随 context 走，终端只摘要；继续跑只会放大不一致。
        from contracts.errors import E2005, FlowError
        raise FlowError(
            E2005, f"reconcile 发现 {len(violations)} 处不一致: "
                   f"{sorted({v['kind'] for v in violations})}",
            path="reconcile.violations",
            context={"violations": violations[:100],
                     "total": len(violations),
                     "tickets": tickets, "parcels": parcels,
                     "registers": registers},
            hint="索引/产物可重算、对象从上游重建、登记库从备份恢复（15-operations §5）",
        )
    return envelope_ok("reconcile", None, {
        "data_root": str(root),
        "tickets": tickets,
        "parcels": parcels,
        "registers": registers,
        "violations": [],
        "clean": True,
        "summary": {"violations": 0, "kinds": []},
    })
