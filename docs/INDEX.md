# V5 文档总索引

> 本目录是唯一的**文档中心**：架构、设计、契约、开发计划、模块功能核对、验收、研究分析全部在此。
> 最后整理：2026-11-18（把原先散落在 `user_workspace/reports/` 的 24 份 V5 报告 + 79 份模块审查全部并入）。
> **时效性约定**：设计正文与 `reviews/module-review/`+`reviews/summary/` 是**当前有效**；`reviews/stage/`、`reviews/unit/`、`research/` 是**历史留痕**，结论可能已被后续轮次覆盖，只作追溯。
>
> **布局变更（2026-11-18）**：代码从 `v5/` 扁平化到仓库根，八个包各自成为顶层目录；
> 测试由 `tests/v5/` 变为 `tests/`，夹具由 `tests/fixtures/v5/` 变为 `tests/fixtures/`，
> 本文档目录由 `docs/architecture/v5/` 变为 `docs/`。
> **历史文档里的 `v5/...` 路径保留原文**（它们记录的是当时的布局），读时按上表换算；
> 当前有效的设计正文与 `reviews/module-review/` 已按新布局更新。

---

## 0. 三分钟上手：你想知道什么，就看哪一篇

| 你想知道 | 看这里 |
|---|---|
| 这套系统到底怎么设计的 | `README.md` §1/§3 → `01`–`06` |
| 一行结论是怎么算出来的（口径） | `06-evaluation.md` |
| 现在做到哪一步、还缺什么 | `reviews/summary/v5_module_review_findings_detailed_20261118.md` |
| 每个模块负责什么、有没有问题 | `reviews/module-review/00_progress.md`（索引）→ 对应 `NN_*.md` |
| 配置单每个键什么意思 | `18-plan-schema.md`（唯一真相是 `contracts/plan_schema.json`） |
| 开发计划与阶段门槛 | `IMPLEMENTATION-PLAN.md`、`12-stages.md`、`plans/` |
| 凭什么信它（测试/验收） | `14-test-strategy.md`、`17-walkthrough.md`、`ACCEPTANCE.md`、`reviews/acceptance/` |
| 加密场景哪里不一样 | `10-crypto-fit.md`、`19-supply.md` |
| 挖矿算法怎么加 | `02-mining-interface.md`、`20-mining-algorithms.md` |

---

## 1. 架构与设计（主线，当前有效）

| 文件 | 内容 | 主线 |
|---|---|---|
| `README.md` | 一页速读、配置单、固定八步、四档名字空间、新增与升级分界 | 总览 |
| `01-capability.md` | 能力层：调用单四通道、子调用协议、能力清单、共享能力 | 能力 |
| `02-mining-interface.md` | 挖掘算法统一接口、候选身份、编译期校验、多算法共存 | 能力 |
| `03-orchestration.md` | 编排层：工位与料包、八步详规、状态机、事件、整体与单步 | 编排 |
| `04-runtime.md` | 运行时：静态章程、配置编译 C1–C22、指纹、四区存储、供给维护链 | 运行时 |
| `05-contracts.md` | 不变量、验收、组合域、裁决表、开放问题、术语表 | 全局 |
| `06-evaluation.md` | **横截面评估与组合构建口径**：caliber、截面变换、持仓构造、成本、重叠、三段窗口 | 全局 |

## 2. 组件、工程与加密适配（当前有效）

| 文件 | 内容 |
|---|---|
| `07-components.md` | 组件选型：算子复用 vs 口径自研分界、十二能力复用表、回测引擎边界、deap vs gplearn |
| `08-reuse-audit.md` | 逐层复用清点：每步采用/不采用什么、实测数字、依赖变更、结论修正 |
| `09-app-storage.md` | 应用层与存储层：七层依赖方向、CLI 命令面与 mutation 等级、三类存储、GC、规模墙 |
| `10-crypto-fit.md` | **加密适配**：组件三档判定、必须去掉的、已修年化缺陷、758 合约供给、资金费周期不是常数 |
| `11-field-operator-registry.md` | 字段与算子注册表：表达式可引用什么、因果声明、全局禁令、双指纹、退化输入不得常数兜底 |
| `19-supply.md` | 供给维护链（方案 B）：存储路径、append-only、市场维度、三层护栏、门禁 B |
| `20-mining-algorithms.md` | 挖矿算法调研与集成：可选算法、接入步骤、单因子六步链路 |
| `21-snapshot-replay-catalog.md` | **数据快照 / 回测复盘 / 因子管理**：sqlite 基础设施与服务层、证书身份、新快照重验闭环（P1–P5 已实施） |
| `22-vs-qlib.md` | **与 Qlib 的因子挖掘能力对比**：表达空间、搜索、统计判据、样本外、成本与复现逐项对照，含可借鉴路线与不该照搬的清单 |
| `23-operator-and-factor-expansion.md` | **v2 算子扩表（19→34）与加密原生因子（33→37）**：逐算子语义、进不进 GP、四个新因子的实测 IC/冗余度、被淘汰的 12 个候选及其数字（不要重测）、已知缺口 |
| `24-qweave-450-crypto-audit.md` | **qweave 内置 450 因子（Alpha158/Alpha101/GTJA191）的加密可用性审计**：三段漏斗 426→421→52→44、24 个缺字段因子的替代方案、Top 15 与库内因子的重叠度、已入库 9 个（37→46）的完整闸口与复算、被判重复/排除的清单、vwap 数据缺口 |
| `25-crypto-native-factor-families.md` | **按语义族补的 13 个加密原生因子（46→59）**：准入口径从「有 IC 才收」改为「加密语义 + 不同族」，事件时间/尾部形态/趋势斜率/资金费期限结构等空白族，替换口径与验证 |
| `26-qweave-operator-surface-audit.md` | **qweave 0.6.2 公开算子面全量审计（34→46）**：29 个 PyExpr 方法 + 比较 dunder + 模块级函数逐项对照、12 个补齐原语、wma 别名与 5 个无公开 API 的内部原语、分组算子的字段阻塞、双参数域 kind 与测试 |

## 3. 契约、交付与运行（当前有效）

| 文件 | 内容 |
|---|---|
| `18-plan-schema.md` | 计划 schema：键全集与取值、跨字段规则、错误分级；唯一真相 `contracts/plan_schema.json` |
| `12-stages.md` | 阶段划分 S0–S7 与 E0–E4 对应、每阶段交付与退出门槛、最小可信纵切 |
| `13-migration.md` | V4→V5 迁移：模块逐层映射与四档判定、M0–M6 顺序、对拍清单、回滚 |
| `14-test-strategy.md` | 测试与拒绝策略：五层测试、拒绝矩阵、已知答案、对拍容差、性能基线、CI 门禁 |
| `15-operations.md` | 运维手册：日常序列、结构化记录、告警阈值、故障处置、备份恢复、GC 实操 |
| `16-security.md` | 凭据、权限与审计：威胁模型、凭据只存引用、四区权限矩阵、不可逆事件、泄漏处置 |
| `17-walkthrough.md` | 端到端走查：完整计划、目录树、八步输出、三处必失败演示、一次真实 0 晋升 |

## 4. 详细设计 `s0/`（可实现化，当前有效）

同一套设计落到"可以照着写实现、照着写拒绝测试"的粒度，不是第七篇设计。

| 文件 | 内容 |
|---|---|
| `s0/README.md` | s0 导航与阅读顺序 |
| `s0/01-data-contracts.md` | 数据契约：字段、版本、as-of、缺口 |
| `s0/02-crosscutting.md` | 跨层设施：日志、追踪、随机源、指纹、缓存 |
| `s0/03-runtime.md` | 运行时：编译、指纹、门禁、四区存储 |
| `s0/04-capability.md` | 能力层：八工位规格、掩码、标签、裁判细则（796 行，最细） |
| `s0/05-orchestration.md` | 编排层：执行器、事件、状态机、单步 |
| `s0/06-upstream.md` | 流水线之外：采集、门禁、发布 |

## 5. 开发计划

| 文件 | 内容 | 时效 |
|---|---|---|
| `IMPLEMENTATION-PLAN.md` | 实现计划：8 模块总览矩阵、逐文件待办、S1–S7 任务表、依赖图、明确不做 | 当前（落地基准） |
| `plans/v5_module_review_plan_20261118.md` | 逐模块审查计划：12 单元 U1–U12、六维框架、C1–C12 检查表、R1–R7 判据 | 当前（已执行完） |
| `plans/v5_fix_plan_20261118.md` | 修复计划：A–H 八组 49 项，最小修复集 | 历史（已执行） |
| `plans/README.md` | 计划类说明 | — |

## 6. 模块功能核对（审查，当前有效）

| 位置 | 内容 | 数量 |
|---|---|---|
| `reviews/module-review/00_progress.md` | **78 模块审查总进度与编号索引**（先读这一份） | 1 |
| `reviews/module-review/NN_*.md` | 逐模块六维审查：D1 应然 / D2 差距 / D3 业界 / D4 代码 / D5 文档 / D6 扩展 + 真实例子 + C1–C12 + R1–R7 | 78 |
| `reviews/summary/v5_module_review_findings_detailed_20261118.md` | **汇总总账**：4 项 P0、11 项 P1、5 处过期基线更正、78 模块一句话结论 | 1 |
| `reviews/summary/v5_module_review_findings_20261118.md` | 单元级审查总账（7 项 P0 初判；逐模块版见 detailed，**结论以 detailed 为准**） | 1 |
| `reviews/summary/v5_full_module_review_20261117.md` | 上一轮全模块审查（历史） | 1 |
| `reviews/unit/*.md` | 4 份单元级审查（U6/U7、U8-10、U3-5、U1-2/11-12）；**已被 module-review 逐模块版取代，历史留痕** | 4 |

## 7. 验收

| 文件 | 内容 | 时效 |
|---|---|---|
| `ACCEPTANCE.md` | 38 项验收清单与判退条件 | 当前 |
| `reviews/acceptance/v5_acceptance_check_20261117.md` | 逐项核对结果（S0–S5、S7 机制门槛通过；S6 部分未做） | 历史核对 |
| `reviews/acceptance/v5_ac63_known_answer_20261117.md` | AC63 黄金锚点已知答案对拍记录 | 历史核对 |

## 8. 研究与专题分析（历史，含仍有效的结论）

| 文件 | 内容 |
|---|---|
| `research/v5_e2e_analysis_20261117.md` | 端到端严格判据复跑分析（2 pass/59 reject/1 watch） |
| `research/v5_mining_industry_gap_20261118.md` | 挖矿算法业界差距调研 |
| `research/v5_supply_market_20261117.md` | 供给市场维度（spot/futures）改造记录 |
| `research/v5_s3_m6_executor_capability_20261117.md` | S3-M6 工位→能力桥接入记录 |
| `research/v5_s7_combine_and_single_commands_20261118.md` | S7 组合域与三条单命令接入记录 |

## 9. 阶段审查（历史留痕，只作追溯）

`reviews/stage/` 9 份：`v5_s1_code_review`、`v5_s2_code_review`、`v5_s3_m1_review`、`v5_s3_review_fix`、`v5_s4_review`、`v5_s5_review`、`v5_s7_review`、`v5_full_code_review`、`v5_fix_execution`。
这些是各阶段当时的代码审查与修复执行记录。**当前结论请看 `reviews/summary/` 与 `reviews/module-review/`**。

## 10. 样例

| 文件 | 内容 |
|---|---|
| `examples/plan_cs_2026q4.yaml` | 唯一规范计划样例，被测试严格校验；文档不再各自抄一份 |

---

## 11. 目录结构

```
docs/
├── INDEX.md                 ← 本文件（总索引）
├── README.md                一页速读与设计总览
├── 01…20-*.md               架构与设计正文（20 篇）
├── ACCEPTANCE.md            验收清单
├── IMPLEMENTATION-PLAN.md   实现计划
├── s0/                      详细设计（6 篇 + README）
├── examples/                规范样例
├── reviews/                 审查与核对
│   ├── module-review/       78 份逐模块 + 00_progress（79）
│   ├── summary/             汇总总账（3）
│   ├── unit/                单元级（4，历史）
│   ├── stage/               阶段审查（9，历史）
│   └── acceptance/          验收核对（2）
├── plans/                   计划类补充（2）
└── research/                研究与专题（5，历史）
```

## 12. 已知需要更正的文档陈述（以总账为准）

以下旧陈述已被 2026-11-18 逐模块审查实测推翻，**读文档时以 `reviews/summary/v5_module_review_findings_detailed_20261118.md` §四为准**：

| 旧陈述 | 实测 |
|---|---|
| "五加密流未进面板" | funding/OI/basis 三流已进面板且 GP 可引用；仅清算+跨所两流缺 |
| "GC/index 未做" | 均已实现（reconcile/一致性校验/引用计数回收/删库重建） |
| "内置 158 因子" | 真实 12 个 `BUILTIN_FACTORS`；158 是 qweave Alpha158 模板能力 |
| "log 过度自研" | 已 structlog 化 |
| "OI 25 行影响 estimate" | estimate 为纯算式模块，无关 |

> 这些是**文档滞后**，不是代码缺陷。更正文档时按上表逐条改，改动前先对照代码与测试。
