"""登记库：append-only jsonl，全局单调的唯一真相（09 §3.1）。

只追加，永不修改或删除。坏了不能"重算"——这是状态，不是派生物。
四类登记：trials / events / published / members（s0/03 §2 的 register 区）。

去重：事件有一次性键（Reveal / Budget / Publish）；同键重复追加必须拒绝，
否则"重放一次"就能刷计数或重复揭示。这是 AC4–AC8 的存储层保证。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from filelock import FileLock

from v5.contracts.errors import E3006, FlowError

from .layout import Layout

VALID_REGISTERS = ("trials", "events", "published", "members")


def _dedup_key(record: Mapping[str, Any]) -> str | None:
    """一次性事件的去重键。有 event_id 即视为一次性。"""
    if "event_id" in record:
        return str(record["event_id"])
    return None


class Ledger:
    """只追加登记。同一进程内维护 seen 集合；跨进程靠扫描现有 jsonl 重建。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self._seen: dict[str, set[str]] = {name: set() for name in VALID_REGISTERS}
        for name in VALID_REGISTERS:
            self._seen[name] = self._scan_keys(self.layout.register(name))

    @staticmethod
    def _scan_keys(path: Path) -> set[str]:
        if not path.exists():
            return set()
        keys: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半行由 reconcile 处理；这里不把它当真相
            key = _dedup_key(rec)
            if key is not None:
                keys.add(key)
        return keys

    def _guard(self, register: str) -> FileLock:
        # 同键去重必须跨进程原子：只有 seen + write 在同一把 OS 文件锁内，
        # 两个实例同时启动才不会都判"未见过"而各写一份。
        return FileLock(str(self.layout.register(register)) + ".guard", timeout=10)

    def append(self, register: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条登记；一次性键重复 → E3006，不写盘。"""
        if register not in VALID_REGISTERS:
            raise FlowError(E3006, f"未知登记账本: {register!r}", path="register")
        key = _dedup_key(record)
        path = self.layout.register(register)
        with self._guard(register):
            # 锁内重扫真相：实例构造后的外部写入也要看见。
            seen = self._scan_keys(path)
            # 重扫结果必须回并实例视图：只 add 本次新 key 时，`_seen` 会长期
            # 落后于磁盘，任何「我见过这条吗」的判断都会在两个实例之间漂移。
            self._seen[register] |= seen
            if key is not None and key in seen:
                raise FlowError(
                    E3006,
                    f"一次性事件重复: {key}",
                    hint="揭示/计数/发布只能发生一次；重放不得刷账本",
                    path=f"{register}.{key}",
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            # O_APPEND 保证多写者逐行不撕裂；filelock 保证去重判定原子。
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            if key is not None:
                self._seen[register].add(key)
        return dict(record)

    def records(self, register: str) -> list[dict[str, Any]]:
        """只读回放。"""
        if register not in VALID_REGISTERS:
            raise FlowError(E3006, f"未知登记账本: {register!r}", path="register")
        path = self.layout.register(register)
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # 与 _scan_keys 同策略：半行不是真相，index 重建不能因它崩。
                continue
        return out

    def get(self, register: str, event_id: str) -> dict[str, Any] | None:
        """按一次性键取回已有登记（跨进程幂等用）。

        存在的意义是让「重复触发」有两种正确结局：同一进程内直接返回旧值；
        另一个进程已经写过时，调用方读回那条记录——而不是把 E3006 当成
        异常处理掉，也不是重新写一份。
        """
        for rec in self.records(register):
            if str(rec.get("event_id")) == str(event_id):
                return rec
        return None

    def count(self, register: str) -> int:
        return len(self.records(register))
