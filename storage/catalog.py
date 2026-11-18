"""因子库：跨 run 累积的因子资产（catalog 区）。

**为什么需要它**：没有因子库时，一次挖掘的产物只活在
`runs/<ticket>/parcels/04-Search*/payload.json` 里——下一轮从零开始，上一轮
通过裁决的因子既不能被引用、也不能当先验池，去重只在单次执行内成立。
「挖出过什么」这件事在数据根里没有唯一答案。

三条设计约束：

1. **identity 是内容**：`factor_id = candidate_id = H(规范化 payload + 字段集)`。
   同一表达式无论由 GP、随机搜索还是手写先验池产生，进库只有一个条目；
   改名、换来源、换 run 都不新开一行。
2. **append-only**：`factors.jsonl` 只追加。状态变更（retire）追加一条新记录
   并让最后一条生效，不改历史行——与 ledger 同一套真相语义。
3. **不进指纹**：因子库是**资产目录**，不是实验构成。往库里加一个因子不得
   改变任何历史计划的 `plan_digest` / `blueprint_digest`，否则历史结论全部失效。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts.errors import E1008, E3006, FlowError

from .layout import Layout

CATALOG_VERSION = "1"
FACTORS_FILE = "factors.jsonl"
_STATUSES = ("active", "retired")


def _path(layout: Layout) -> Path:
    return layout.catalog / FACTORS_FILE


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # 半行不是真相；与 ledger.records 同策略（不因坏行整体崩）。
            continue
    return out


class Catalog:
    """因子库读写。只追加，按 `factor_id` 折叠出当前状态。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    @property
    def path(self) -> Path:
        return _path(self.layout)

    # ---- 读 -----------------------------------------------------------
    def entries(self) -> list[dict[str, Any]]:
        """按 factor_id 折叠：后写的 status 覆盖先写的，其余字段以首次入库为准。"""
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for rec in _read_lines(self.path):
            fid = str(rec.get("factor_id") or "")
            if not fid:
                continue
            if fid not in merged:
                merged[fid] = dict(rec)
                order.append(fid)
            else:
                # 只有状态与审计字段允许更新；表达式/身份不可变
                for key in ("status", "updated_at", "note", "evidence_grade"):
                    if key in rec:
                        merged[fid][key] = rec[key]
        return [merged[fid] for fid in order]

    def get(self, factor_id: str) -> dict[str, Any] | None:
        for row in self.entries():
            if row.get("factor_id") == factor_id:
                return row
        return None

    def list(self, *, status: str | None = None,
             limit: int | None = None) -> list[dict[str, Any]]:
        rows = self.entries()
        if status:
            if status not in _STATUSES:
                raise FlowError(
                    E1008, f"未知 status {status!r}", path="factors.status",
                    hint=f"可用: {list(_STATUSES)}",
                )
            rows = [r for r in rows if r.get("status", "active") == status]
        if limit is not None and limit >= 0:
            rows = rows[:limit]
        return rows

    def count(self) -> int:
        return len(self.entries())

    def expressions(self, *, status: str = "active") -> list[dict[str, Any]]:
        """可直接注入 `prior_candidates` 的形态（名字 + 表达式 + 字段集）。

        `factor_id` 一并带出：`prior_pool` 允许用 `db:<factor_id>` 精确引用，
        而名字可能重名（两个不同表达式都叫 `gp_symbolic:abcd1234` 的概率不为零）。
        """
        out = []
        for row in self.list(status=status):
            if not isinstance(row.get("expression"), Mapping):
                continue
            out.append({
                "factor_id": str(row.get("factor_id") or ""),
                "name": str(row.get("name") or row["factor_id"]),
                "expression": dict(row["expression"]),
                "required_fields": list(row.get("required_fields") or []),
            })
        return out

    # ---- 写 -----------------------------------------------------------
    def add(self, entry: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """追加一个因子条目。返回 `(条目, 是否新入库)`。

        同一 `factor_id` 已在库（且 status 相同）→ 不写盘，返回已有条目。
        这让 promote 可以重复执行：重跑一次不是错误，也不会把库撑大。
        """
        fid = str(entry.get("factor_id") or "")
        if not fid:
            raise FlowError(E1008, "因子条目缺 factor_id", path="factors.factor_id")
        if not isinstance(entry.get("expression"), Mapping):
            raise FlowError(
                E1008, f"因子 {fid} 缺 expression", path="factors.expression",
                hint="表达式是内容身份的唯一来源，不能只存名字",
            )
        existing = self.get(fid)
        if existing is not None and existing.get("status", "active") == entry.get(
                "status", "active"):
            return existing, False

        record = {
            "catalog_version": CATALOG_VERSION,
            "factor_id": fid,
            "name": str(entry.get("name") or fid),
            "expression": dict(entry["expression"]),
            "required_fields": sorted({str(f) for f in entry.get("required_fields") or []}),
            "operators": sorted({str(o) for o in entry.get("operators") or []}),
            "generators": list(entry.get("generators") or []),
            "origin": dict(entry.get("origin") or {}),
            "evidence_grade": entry.get("evidence_grade"),
            "statistics": dict(entry.get("statistics") or {}),
            "metrics": dict(entry.get("metrics") or {}),
            "status": str(entry.get("status", "active")),
            "promoted_at": str(entry.get("promoted_at") or ""),
        }
        if record["status"] not in _STATUSES:
            raise FlowError(E1008, f"未知 status {record['status']!r}",
                            path="factors.status")

        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        # O_APPEND：多写者逐行不撕裂。去重判定在同一把文件锁内做，避免两个
        # promote 同时启动各写一行（库里出现两条同 id 的 active）。
        from filelock import FileLock
        with FileLock(str(path) + ".guard", timeout=10):
            fresh = {str(r.get("factor_id")) for r in _read_lines(path)}
            if fid in fresh and self.get(fid) is not None:
                return self.get(fid) or record, False
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        return record, True

    def retire(self, factor_id: str, *, at: str, note: str = "") -> dict[str, Any]:
        """下架一个因子：追加一条 status=retired 的记录，不改历史行。"""
        existing = self.get(factor_id)
        if existing is None:
            raise FlowError(
                E3006, f"因子不在库中: {factor_id}", path="factors.factor_id",
            )
        if existing.get("status") == "retired":
            return existing
        record = dict(existing)
        record.update({"status": "retired", "updated_at": at, "note": note})
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return record


# ---- run 产物读取（promote / inspect 共用） -------------------------------
# 工位名清单，**必须与 `orchestration.station.STATION_SPECS` 一致**。
# 这里只做目录名匹配，不需要序号（目录名前缀自带），所以存元组而不是字典。
# 为什么不直接 import：storage 是被 orchestration 依赖的底层，反向依赖会让
# 「谁依赖谁」失去方向。两份真相由测试守卫（`test_station_names_match_specs`）。
_STATION_NAMES = (
    "Boundary", "Panel", "Target", "Search",
    "Evidence", "Verdict", "Portfolio", "Certify",
)


def station_of(dirname: str) -> str | None:
    """`01-Boundary-cf66963` → `Boundary`。不靠位置猜，靠已知站名精确匹配。"""
    for name in _STATION_NAMES:
        if dirname.endswith(name) or f"-{name}-" in dirname:
            return name
    return None


def read_ticket(layout: Layout, ticket_id: str) -> dict[str, Any]:
    """读一次执行的全部工位产物，返回 `{station: payload}` 与元信息。

    只认 `runs/<ticket>/parcels/<seq>-<Station>-<digest>/payload.json`。
    同一工位出现多个目录（append_only 工位重复执行）时保留**第一个**：
    Verdict/Search 的 append 语义要求按首次结论，不能被后续覆盖。
    """
    root = layout.run_dir(ticket_id)
    if not root.exists():
        raise FlowError(
            E1008, f"执行记录不存在: {ticket_id}", path="inspect.run_id",
            hint="检查 runs/ 下的目录名（含 Z 后缀的 UTC 时间戳 + plan_digest 前 8 位）",
        )
    parcels_dir = layout.run_parcels_dir(ticket_id)
    payloads: dict[str, dict[str, Any]] = {}
    frozen: dict[str, Any] = {}
    if parcels_dir.is_dir():
        for child in sorted(parcels_dir.iterdir()):
            if not child.is_dir():
                continue
            station = station_of(child.name)
            if station is None or station in payloads:
                continue
            target = child / "payload.json"
            if not target.exists():
                continue
            try:
                payloads[station] = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    frozen_dir = root / "frozen"  # ticket 内部结构，不属于九区
    if frozen_dir.is_dir():
        for name in ("plan.json", "context.json"):
            f = frozen_dir / name
            if f.exists():
                try:
                    frozen[name.split(".")[0]] = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
    return {"ticket_id": ticket_id, "root": str(root),
            "stations": sorted(payloads), "payloads": payloads, "frozen": frozen}


__all__ = ["Catalog", "read_ticket", "station_of", "CATALOG_VERSION"]
