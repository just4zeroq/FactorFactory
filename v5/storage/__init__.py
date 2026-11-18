"""存储层（L1）：路径、对象、登记、索引、回收。

依赖方向：只有事实层与供给链可以 import 本包；能力层禁止（AC31）。
"""

from .layout import Layout, all_zone_literals

__all__ = ["Layout", "all_zone_literals"]
