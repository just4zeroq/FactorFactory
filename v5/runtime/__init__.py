"""V5 运行时 · 编译与存储骨架（s0/03-runtime.md）。

编译是纯函数：不读数据、不写文件、不取时钟（compiled_at 由调用方注入）。
十步中 1–4 已在 v5.contracts.plan_schema 落地，这里实现 5–10 的骨架
与 C1–C22 的 fail-closed 检查。第 9 步（供给别名）只读，失败不创建 Ticket。

对外暴露 compile_plan / CompiledPlan。
"""

from .compiler import CompiledPlan, compile_plan

__all__ = ["CompiledPlan", "compile_plan"]
