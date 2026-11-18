"""应用服务层：机制（storage/runtime）与命令面（app/cli）之间的业务规则。

快照、复盘、因子管理各自一个 service，共用 storage.sqlite_store 的机制，
不直接拼 DDL、不直接开连接。
"""

from __future__ import annotations

__all__: list[str] = []
