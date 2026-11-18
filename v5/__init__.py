"""FactorMiner V5 运行时（`v5/`）。

与 `core/`（V4）和 `flow/`（S0 契约层的前一版物理目录）并存的独立包。
按 `docs/architecture/v5/13-migration.md` 的 M0–M6 顺序逐层替换。

**依赖方向（结构约束，CI 会扫）**：
- `v5` 不得 import `core` / `api` / `flow`；三者也不得 import `v5`。
- 迁移期多套实现并存，任何跨包 import 都会让「旧路径保持可跑」这条回滚手段失效。

**为什么 v5/contracts 与 flow/contracts 内容相同**：契约语义（49 错误码、八个指纹、
七类结构、plan schema）已通过文档验收并冻结，`v5/contracts/` 是它在 V5 包内的
权威副本；`flow/contracts/` 保留作回滚参照，S0 十二条全绿后删除。两处不得各自演化。

S0 交付：契约层 + facilities + runtime 骨架 + 核心五条 CLI。
"""
