"""跨层设施 · cache（s0/02-crosscutting.md §7）。

以 call_digest 为键的产物复用。只有两类合法键（call / node），
不允许自定义键或手工拼字符串。station/probe 作用域只读。

命中必须重新校验文件存在性与 payload_digest；校验失败视为未命中（F5），
不抛错、不返回空产物。命中只跳过计算，不跳过计数（F6）。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from contracts.digest import assert_relative_path, blob_digest, compute_digest
from contracts.errors import E2001, FlowError

# F5：内存键表必须有界。键表无界 = 长跑进程内存线性增长（15-operations §3
# 「运行时长」类故障的同族）。淘汰只丢键表映射，不删文件——文件是内容寻址
# 对象，由 GC 与引用计数管理，缓存无权碰（登记库/对象「缓存不许淘汰」）。
DEFAULT_MAX_ENTRIES = 1024


@dataclass(frozen=True)
class CacheHit:
    payload_ref: str
    payload_digest: str
    produced_at: str


class Cache:
    def __init__(self, scope: str, root: Path, node_enabled: bool = False,
                 max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if scope not in {"upstream", "ticket", "station", "probe"}:
            raise FlowError(E2001, f"unknown cache scope: {scope}")
        if max_entries < 1:
            raise FlowError(E2001, f"cache max_entries 必须 >= 1: {max_entries}")
        self.scope = scope
        self.root = root
        self.node_enabled = node_enabled  # 默认关：当前规模落盘净亏 65×
        self.max_entries = int(max_entries)
        # OrderedDict 兼作 LRU：get 命中 move_to_end，put 满员淘汰队首。
        self._call: OrderedDict[str, CacheHit] = OrderedDict()
        self._node: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.evictions = 0  # 观测字段：淘汰计数供 doctor/预算核对，不进指纹

    @property
    def writable(self) -> bool:
        return self.scope in {"upstream", "ticket"}

    def _touch(self, call_digest: str) -> None:
        self._call.move_to_end(call_digest)

    def _evict_over_capacity(self) -> None:
        while len(self._call) > self.max_entries:
            self._call.popitem(last=False)
            self.evictions += 1
        while self.node_enabled and len(self._node) > self.max_entries:
            self._node.popitem(last=False)
            self.evictions += 1

    def get(self, call_digest: str) -> CacheHit | None:
        hit = self._call.get(call_digest)
        if hit is None:
            return None
        # F5：命中后重新校验「文件存在 + 内容指纹」，不能只看文件名。
        # 文件被删/被替换都视为未命中——返回被篡改的产物比 miss 昂贵得多。
        try:
            rel = assert_relative_path(hit.payload_ref)
        except FlowError:
            self._call.pop(call_digest, None)
            return None
        target = (self.root / rel).resolve()
        try:
            target.relative_to(Path(self.root).resolve())
        except ValueError:
            self._call.pop(call_digest, None)
            return None
        if not target.is_file():
            self._call.pop(call_digest, None)
            return None
        if not _digest_matches(target.read_bytes(), hit.payload_digest):
            self._call.pop(call_digest, None)
            return None
        self._touch(call_digest)  # LRU：命中即刷新新近度
        return hit

    def put(self, call_digest: str, payload_ref: str, payload_digest: str) -> None:
        if not self.writable:
            raise FlowError(E2001, f"cache scope={self.scope} is read-only")
        self._call[call_digest] = CacheHit(payload_ref, payload_digest, "")
        self._call.move_to_end(call_digest)
        self._evict_over_capacity()

    def invalidate(self, call_digest: str) -> None:
        # put/put_node 都查 writable，唯独 invalidate 漏了——station/probe
        # 虽不能写，却能删掉 ticket 缓存迫使重算（P1-9）。只读就必须真只读。
        if not self.writable:
            raise FlowError(E2001, f"cache scope={self.scope} is read-only")
        self._call.pop(call_digest, None)

    def get_node(self, node_digest: str) -> tuple[str, str] | None:
        if not self.node_enabled:
            return None
        hit = self._node.get(node_digest)
        if hit is not None:
            self._node.move_to_end(node_digest)
        return hit

    def put_node(self, node_digest: str, payload_ref: str, payload_digest: str) -> None:
        if not self.node_enabled:
            return
        if not self.writable:
            raise FlowError(E2001, f"cache scope={self.scope} is read-only")
        self._node[node_digest] = (payload_ref, payload_digest)
        self._evict_over_capacity()


def _digest_matches(content: bytes, expected: str) -> bool:
    """产物既可能是原始字节对象，也可能是 JSON 结构对象，两种指纹都要认。"""
    if blob_digest(content) == expected:
        return True
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return False
    try:
        return compute_digest(payload) == expected
    except FlowError:
        return False
