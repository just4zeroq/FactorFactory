"""进程退出码（S0 契约层）。

五个码，语义互不重叠。关键在于 `3` 与 `1` 必须分开：前者说明"代码写错了"，
后者说明"输入或环境有问题"。合并成一个码，线上排查就会退化成猜。

**没有 0 以外的"成功但有警告"**。警告只能进结构化记录，不能改变退出码，
也不能让一次本应失败的执行返回 0（V4 的假成功就是这么来的）。
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """`flow` 命令的唯一退出码枚举。"""

    OK = 0
    """成功。"""

    EXECUTION = 1
    """执行失败或存储失败（E3xxx / E5xxx）。"""

    USAGE = 2
    """用法或编译错误（E1xxx）。用户改输入即可，不需要读代码。"""

    CONTRACT = 3
    """契约违反（E2xxx）。按 bug 处理，必须 fail-closed。"""

    SUPPLY = 4
    """供给失败（E4xxx）。事实层不可用或不适用。"""


__all__ = ["ExitCode"]
