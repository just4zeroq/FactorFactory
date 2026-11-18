"""引用计数回收：facts 区里没人引用的对象（S6，`09 §3.4`）。

真相是**引用**，不是目录。判定规则只有一条：对象的完整 digest 是否出现在
任何一处真相文本里——

- `runs/**` 的 parcel / payload / frozen / trace；
- `db/*.jsonl` 四类登记；
- `catalog/factors.jsonl`；
- `parcels/**` 的 depot 侧引用。

出现即保留，未出现即回收候选。**不做可达性分析**：内容寻址对象之间没有指针，
parcel 里存的是最终 digest 而不是对象图，所以「一层扫描」就是全部可达性。

两个安全边界：

1. `dry_run=True` 是默认。回收不可撤销，调用方必须先看清单再确认；
2. 只扫 `facts/` 六个类别目录，且只删 `.parquet` / `.json` —— 不递归删除数据根
   的其它区域。删之前再核一次「仍在引用集里」就不删，避免扫描后被新 run 引用。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .layout import FACT_KINDS, Layout
from .objects import SCHEMA_VERSION  # noqa: F401  (保持与对象布局同版本)

_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
# 类别白名单与 index / fact_kind 同源（`layout.FACT_KINDS`）：三处各写一份
# 时，index 曾漏掉 `parcel_data`，GC 与索引对「谁算活对象」给出两个答案。
_KINDS = FACT_KINDS
_TEXT_SUFFIXES = (".json", ".jsonl")
_OBJECT_SUFFIXES = (".parquet", ".json")


def _iter_text_files(layout: Layout) -> Iterable[Path]:
    zones = (layout.runs, layout.parcels, layout.db, layout.catalog, layout.baselines)
    for zone in zones:
        if not zone.exists():
            continue
        for path in zone.rglob("*"):
            if path.is_file() and path.suffix in _TEXT_SUFFIXES:
                yield path


def referenced_digests(layout: Layout) -> set[str]:
    """所有真相文本里出现过的完整 digest（64 位 hex，不带前缀）。"""
    seen: set[str] = set()
    for path in _iter_text_files(layout):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        seen.update(_DIGEST.findall(text))
    return seen


def scan_objects(layout: Layout) -> list[dict[str, Any]]:
    """facts 下的全部对象：按文件名反推 digest（文件名就是 digest）。"""
    out: list[dict[str, Any]] = []
    for kind in _KINDS:
        base = layout.fact_kind(kind)
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if not path.is_file() or path.suffix not in _OBJECT_SUFFIXES:
                continue
            stem = path.stem
            if len(stem) != 64 or any(c not in "0123456789abcdef" for c in stem):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            out.append({"kind": kind, "digest": f"sha256:{stem}",
                        "path": str(path), "bytes": size})
    return out


def plan_gc(layout: Layout) -> dict[str, Any]:
    """回收计划（只读）。`unreferenced` 按字节降序，先看大的。"""
    refs = referenced_digests(layout)
    objects = scan_objects(layout)
    dead = [o for o in objects if o["digest"].split(":", 1)[1] not in refs]
    dead.sort(key=lambda o: o["bytes"], reverse=True)
    return {
        "objects": len(objects),
        "bytes_total": sum(o["bytes"] for o in objects),
        "referenced": len(objects) - len(dead),
        "unreferenced": len(dead),
        "reclaimable_bytes": sum(o["bytes"] for o in dead),
        "candidates": dead,
    }


def apply_gc(layout: Layout, *, limit: int | None = None) -> dict[str, Any]:
    """真正删除。每删一个之前重核引用集（扫描后可能有新 run 引用了它）。

    返回实际删除清单与失败项；单个文件删除失败不中断整批（剩下的下次还能收）。
    """
    plan = plan_gc(layout)
    refs = referenced_digests(layout)
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in plan["candidates"]:
        if limit is not None and len(removed) >= limit:
            break
        if item["digest"].split(":", 1)[1] in refs:
            skipped.append({"path": item["path"], "reason": "referenced_now"})
            continue
        path = Path(item["path"])
        try:
            path.unlink()
            removed.append(item)
        except OSError as exc:
            failed.append({"path": item["path"], "error": str(exc)})
    return {
        "removed": removed,
        "skipped": skipped,
        "failed": failed,
        "bytes_freed": sum(o["bytes"] for o in removed),
        "remaining": plan["unreferenced"] - len(removed),
    }


__all__ = ["plan_gc", "apply_gc", "referenced_digests", "scan_objects"]
