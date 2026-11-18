"""能力层（L3）：纯计算，无权。

五条结构禁令：不评估、不判显著、不写计数、不自选口径、不自建子调用。
靠签名与类型里没有位置来保证，不靠代码评审。AC31 用 AST 守卫机器检查。

对外三个入口：
- `load_registry()` 字段/算子注册表（编译期输入）；
- `describe()` 能力元信息（纯静态）；
- `run(call)` 唯一调用协议（六步校验，只返回不落盘）。
"""
from .registry import Registry, load_registry, resolve_impl, NAMESPACES
from .runtime import Call, InputRef, Impl, impls, register, resolve, run
from .spec import CapabilitySpec, CAPABILITIES, describe, all_specs

# 装配内置实现：能力层不靠"调用方记得注册"，否则 run() 会在生产路径上
# 随机变成 E3004。装配本身幂等。
from .impls import install_default_impls as _install

_install()

__all__ = [
    "Registry", "load_registry", "resolve_impl", "NAMESPACES", "CapabilitySpec",
    "CAPABILITIES", "describe", "all_specs",
    "Call", "InputRef", "Impl", "impls", "register", "resolve", "run",
]
