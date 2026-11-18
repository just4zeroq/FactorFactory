"""跨层设施 · rng（s0/02-crosscutting.md §5）。

按名字派生确定性随机源。没有全局状态：同 master_seed + 同 scope_path
必得同 seed，改一个字符则不同。不含时间、进程 id、路径。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RngFactory:
    master_seed: int

    def derive_seed(self, scope_path: str) -> int:
        """sub_seed(seed, scope) = int(sha256(f'{seed}:{scope}')[:16], 16)"""
        payload = f"{self.master_seed}:{scope_path}".encode("utf-8")
        return int(hashlib.sha256(payload).hexdigest()[:16], 16)

    def scope(self, scope_path: str) -> random.Random:
        """返回一个独立 Random 实例，不与任何其他作用域共享状态。"""
        return random.Random(self.derive_seed(scope_path))
