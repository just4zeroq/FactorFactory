"""跨层设施 · digest（s0/02-crosscutting.md §6）。

规范化与指纹计算的设施封装。底层公式来自 contracts.digest（唯一真相），
这里只提供注入入口，让能力层不必直接 import 契约层、也便于将来整体升版。

纯函数：无状态、无 IO、无时钟、无随机。实现变更=全系统指纹变更，
属于最高级别变更，必须显式升版并接受历史产物失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.digest import canonical_bytes, compute_digest

DIGEST_IMPL_VERSION = "v1"


@dataclass(frozen=True)
class DigestService:
    """注入式指纹服务。版本固定，升版需显式改 DIGEST_IMPL_VERSION。"""

    version: str = DIGEST_IMPL_VERSION

    def canonical_bytes(self, obj: Any) -> bytes:
        return canonical_bytes(obj)

    def of(self, obj: Any) -> str:
        """返回 'sha256:<64hex>' 形式的指纹字符串。"""
        return compute_digest(obj)
