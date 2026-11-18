# 13 · 从 V4 到 V5：迁移映射

> 本篇回答：现有 **24,896 行**生产代码，哪一部分进 V5 的哪一层，哪一部分删掉，按什么顺序搬，以及**什么才算搬对了**。
> 迁移的验收不是「新代码能跑」，是**同一份数据、同一份口径下，新旧两条路径逐位对得上**。

---

## 1. 四条原则

| 原则 | 含义 | 反面（V4 已经踩过） |
|---|---|---|
| **先契约后逻辑** | 新层先落地接口与拒绝路径，再往里填算法 | 先写实现，接口随实现变形，最后无法拒绝任何输入 |
| **一次只搬一层** | 一层搬完、对拍通过、再搬下一层 | 一次性重写全链，出错无法定位到层 |
| **旧路径保持可跑** | 新路径通过同一 AC 之前不删旧代码 | 删了旧实现，新实现还没对拍，只能靠「看起来对」 |
| **对拍是唯一验收** | 数值逐位或按明示容差一致 | 「结果差不多」——差不多的量级差异正是 V4 的病根 |

一条硬规矩：**迁移期不允许出现「两套都算一遍然后取其一」的分支**。要么旧路径是唯一实现，要么新路径是唯一实现，切换点由配置显式指定，用完即删。

---

## 2. V4 现状盘点（实测）

| 模块 | 文件 | 行数 | 主要内容 |
|---|---|---|---|
| `core/data_feed/` | 14 | 5,164 | ccxt 下载、分页、缺口填补、加密流、面板校验、bar 时钟、命名 |
| `core/evaluation/` | 11 | 4,476 | 表达式求值、显著性、漏斗、目标、组合评估、holdout 守卫、代码沙箱 |
| `core/commands/` | 12 | 4,138 | CLI 各命令实现（pipeline/doctor/refresh/experiment/factors/promote/holdout/…） |
| `core/miner/` | 18 | 3,586 | 五种范式（GP/LLM/RL/NN/DL）、表达式、算子运行时、编组、种子 |
| `core/experiment/` | 12 | 3,490 | 实验流水线、预算、成本、走查窗口、holdout 窗口、swEEP、universe |
| `core/fabric/` | 6 | 1,275 | L0–L8 可插拔契约层（旁路，未接入生产） |
| `core/backtest/` | 2 | 805 | ml4t-backtest 适配器 |
| `core/inspector/` | 5 | 685 | 单因子审计引擎、指标、报告、解析 |
| `core/storage/` | 2 | 442 | 因子存储（metadata + values + snapshots） |
| `core/execution/` | 3 | 162 | 编译器与执行器雏形 |
| `core/analysis/` | 2 | 134 | 因子分析 |
| `core/config/` | 3 | 126 | defaults / overrides |
| `core/utils/` | 1 | 79 | 工具 |
| `core/cli.py` + `startup_validation.py` | 2 | 334 | 参数表与启动校验 |
| **合计** | **93** | **24,896** | — |

另有：`api/`（FastAPI 4 文件）、`web/`（React SPA）、`scripts/`（运维脚本）、`tests/`（61 文件 / 771 例）、四个 `factor_db*` 目录。

---

## 3. 模块 → 层 映射与判定

判定四档：**保留**（直接进新层，改 import 即可）／**适配**（包一层契约，逻辑不动）／**重写**（语义已错或结构不对）／**删除**（不该存在）。

| V4 模块 | V5 层 | 判定 | 理由与证据 |
|---|---|---|---|
| `data_feed/data_downloader.py`（1370） | 供给链 `acquire` | **适配** | 分页与合并逻辑是资产（已修假成功、已修交易所串目录）；外面包 `acquire` 契约 |
| `data_feed/pagination.py`（139） | 供给链 | **保留** | 游标按最后一根返回 bar 推进，是对的（旧实现按 30 天墙钟窗口会丢 29 天） |
| `data_feed/bar_clock.py`（116） | 门禁 + L1 读取 | **保留** | 三处共用同一「是否收盘」定义，正是 V5 要的「唯一定义」形态 |
| `data_feed/naming.py`（80） | L1 路径唯一真相 | **保留** | `resolve_data_root` / `data_path` 已经是唯一定义 |
| `data_feed/crypto_streams.py`（601） | 供给链 + 字段注册表 | **适配** | 五个流名与列名进注册表（`11`）；`load_crypto_streams` 的**静默 skip** 必须改为 fail-closed（I35） |
| `data_feed/loader.py`（301→已重写） | 应用/读取门径 | **重写已完成** | 原带 Yahoo 分支与自算指标；现为 ccxt-only 门面（`10-crypto-fit.md` §3.1） |
| `data_feed/freshness.py` / `panel_validator.py` | 门禁 A 检查项 | **适配** | 检查逻辑保留，输出改成门禁的结构化结果 |
| `data_feed/gap_filler.py` | 供给链补洞 | **适配** | 只允许作为采集的一部分；`success_count += 1` 从不下载的假成功已列 P0 |
| `evaluation/funnel.py` | 能力 `observe` 粗筛 | **保留** | 分层漏斗是成本控制的关键；经济口径 h=5 / rebalance=5 / entry=next_close 已定 |
| `evaluation/evaluator.py` | 能力 `evaluate` + `observe` | **重写** | 每代读 `get_test_returns()` 把 OOS 塞进 fitness，是 A 级泄漏；必须由 `11` 的注册表 + `evaluate` 纯函数替代 |
| `evaluation/significance.py` | 能力 `verdict` | **适配** | HAC/BH/Holm 公式是标准的；零分布必须换成**行内跨资产置换**（V4 整行置换在带漂移面板上中位 `\|t\|=4.74 > 观测 3.46`，`p=0.95`） |
| `evaluation/code_sandbox.py` | 编译期 C16/C17 | **删除** | 运行期 AST 沙箱是错位的防线：它允许 `shift` 却不查符号，等于没防；校验必须前移到编译期 |
| `evaluation/targets.py` | 能力 `label` | **适配** | `close[t+h]/close[t]-1` 与因子同索引是对的（已核实）；变换与中性化归 `caliber` |
| `evaluation/portfolio_eval.py` | 能力 `allocate` / `combine` | **重写** | 需拆成「分位构造」与「约束构造」两条路径，并把组合参数移入 `caliber` |
| `evaluation/holdout_guard.py` | 编排层事件 | **适配** | `period_key` 修好后才可作为唯一揭示入口 |
| `evaluation/cache.py` | 跨层缓存设施 | **适配** | 三指纹缓存方向对；键要改成「表达式 + 面板 + 经济口径」内容寻址 |
| `evaluation/annualisation.py` | `caliber` + 回测配置 | **合并** | 唯一实现是 ml4t 的 `calendar="crypto"`；本模块只保留「口径常量」 |
| `backtest/backtest_engine.py`（760） | 能力 `backtest` | **适配** | ml4t 适配器是资产；补 `detail` 两档与 `calendar` 注入（已加 `calendar="crypto"`） |
| `miner/paradigms/*`（5 个） | 能力 `search` | **适配** | GP/LLM/RL/NN/DL 的搜索逻辑保留；全部改成 `describe/fit/generate/lineage` 四方法 |
| `miner/expressions.py` | 算法内部 | **降级** | 字符串语法是**算法**的输入格式，不是求值层的契约；求值只认 JSON AST |
| `miner/operator_runtime.py` | 能力 `evaluate` 适配层 | **重写** | 改为 JSON AST → qweave `Expr` 构造；不再自己实现 rolling |
| `miner/crypto_features.py` | 内置因子清单 | **保留** | 整批声明、整批计数 |
| `miner/seeding.py` | 跨层随机源 | **保留** | 实例级 RNG 已修；deap 还需要 `random.setstate` 包裹（`07` §7.2） |
| `miner/director.py`（含 OOS 私法） | 编排层 | **拆分** | 真实 OOS 只能由编排层发起一次；能力层不得持有该入口 |
| `experiment/pipeline.py` | 编排层八工位 | **重写** | 现在是单函数长流程；必须拆成八工位 + Parcel + 状态机（`03-orchestration.md`） |
| `experiment/budget.py` | 编排层 Budget 事件 | **适配** | 计数口径改为去重 `candidate_id`（I38），并把 fit 内评估计入 |
| `experiment/windows.py` / `holdout` 相关 | 编排层 + `certify` | **适配** | 滚动窗口锚点、末端未闭合判定需接 `bar_clock` |
| `experiment/walk_forward.py` | 能力 `certify` | **适配** | embargo 逻辑保留 |
| `experiment/costs.py` / `estimate.py` | `caliber.net` | **适配** | 成本分档与估算保留，参数移入 `caliber` |
| `experiment/manifest.py` | 跨层指纹设施 | **重写** | 现有 manifest 只有文件哈希；V5 要八个指纹与规范化 JSON（`s0/01` §2） |
| `experiment/registry.py` / `sweep.py` / `universe.py` | 意图层 | **适配** | sweep 的每 cell 独立族保留 |
| `commands/*`（12） | 应用层 | **适配→重写** | 命令语义保留，实现改为「翻译成配置 + 调用编排」；口径参数从命令面移除（I30） |
| `cli.py` | 应用层 | **重写** | 改为命令清单驱动（名/参数 schema/输出 schema/mutation 一处定义） |
| `fabric/`（1275） | 跨层契约 | **并入** | 契约层方向对（Artifact/RunContext/VerdictEngine），但它现在是旁路；并入 `contracts/` + 编排接入，不保留第二套 Stage 协议 |
| `inspector/`（685） | 应用层只读查询 | **保留** | 等价于 `factor_db show` / `inspect`；不得写账本 |
| `storage/factor_storage.py` | L1 三类存储 | **重写** | 现在只有 metadata/values 两个目录；V5 要对象/登记/索引三类且登记是唯一真相 |
| `execution/`（162） | 意图层编译 | **重写** | 与 `startup_validation.py` 合并成 C1–C22 |
| `analysis/`（134） | `observe` 诊断通道 | **合并** | 只读诊断，不进判据 |
| `api/` + `web/` | 应用层（Web 壳） | **暂缓** | `09-app-storage.md` §2.5：契约要能接住，但现在不实现 |
| `scripts/` | 供给链运维入口 | **迁入** | 变成 `refresh` / `supply` 的内部实现，不再单独对外 |

---

## 4. 迁移步骤 M0–M6

与 `12-stages.md` 对齐，一步一验收。

| 步 | 内容 | 对应阶段 | 完成判据 |
|---|---|---|---|
| **M0** | 建 `v5/` 骨架：`contracts/`（含 `plan_schema.json` 与校验器）+ `facilities/` + 核心五条 CLI + 12 条拒绝测试 | S0 | s0 十二条全绿（每条断言码 + 退出码）；旧代码零改动 |
| **M1** | L1 三类存储落地：对象（parquet 内容寻址）/ 登记（append-only）/ 索引（duckdb 可重建） | S0→S2 | 同一份 parquet 写两次得到同一 digest；删索引可重建 |
| **M2** | `data_feed` 拆成供给链：`acquire` / `gate_a` / `publish` / `lease` | S2 | 从空目录 refresh 出可被编译引用的一版；断网不产生半份版本 |
| **M3** | 能力壳：`evaluate`（qweave）/ `backtest`（ml4t）/ `observe` / `verdict` | S3、S4 | 暴露与回测对拍通过（§5） |
| **M4** | 编排：`pipeline.py` 拆八工位 + Parcel + 状态机 + Budget 事件 | S1、S5 | 十次重放 digest 逐位一致；`--probe` 四项全不变 |
| **M5** | 应用层：命令面从 5 条扩到 16 条 + 三个渲染器 | S1、S6 | 每条命令 `--json` 与退出码一致；命令面无口径参数；`experiment` 别名已移除 |
| **M6** | 删旧路径与死目录 | S7 后 | 下文 §6 清单全部清空，`git grep` 零命中 |

**M3 与 M4 之间不许调换**：编排在有真实能力的条件下才可能验证「指纹是否覆盖了影响数值的输入」；先做编排只会验证一个空壳。

---

## 5. 对拍清单（迁移的唯一验收）

| 对拍项 | 旧实现 | 新实现 | 通过标准 |
|---|---|---|---|
| **暴露值** | `evaluation.evaluator` | `evaluate`（qweave） | 同一表达式、同一面板 → 逐位相等（`NaN` 位置也必须相同） |
| **回测指标** | `backtest_engine` 现路径 | `backtest@v5` | 56 项指标逐项相等；不相等必须能解释为口径修正并记录 |
| **年化** | 不传 calendar | `calendar="crypto"` | 比值 = √(365/252) = 1.2035（实测 0.2517 / 0.2091） |
| **BH 校正** | statsmodels（dev 依赖） | scipy `false_discovery_control` | 逐位相同（实测差 1.11e-16） |
| **资金费** | 按每 bar 日费率 | 事件求和 | 1h/4h 两种网格、总额相同 → 聚合后金额相等 |
| **置换零分布** | 整行时间轴置换 | 行内跨资产置换 | 纯噪声面板上 `empirical_p` 近均匀；带漂移面板不得出现中位 `\|t\|` 高于观测 |
| **候选计数** | 只记最终种群 | 去重 `candidate_id`（含 fit 内） | 新计数 ≥ 旧计数；GP 300×10 场景显著大于 300 |
| **CLI 退出码** | `main()` 丢弃返回值 → 恒 0 | 单一退出码 | 每条命令 `--json` 的 `ok` 与 `exit_code` 一致 |

**对拍记录要落盘**：每次对拍产出一份 `reports/migration_parity_<date>.json`，含表达式、数据版本、两边的值、差异与判定。没有记录的对拍等于没做。

---

## 6. 删除清单（不可逆，需逐项确认）

| 项 | 现状 | 处置 |
|---|---|---|
| `evaluation/code_sandbox.py` | 运行期 AST 沙箱 | **删除**，能力移交编译期 C16/C17 |
| `data_feed/loader.py` 的股票分支 | **已删**（本轮） | 保留 ccxt-only 门面；守卫测试 `tests/test_data_source_ccxt_only.py` |
| `data_feed/loader.py` 自算指标 | **已删**（本轮） | `ma_5` / `momentum_20` 归 qweave 算子 |
| `factor_db/metadata_pre_reaudit/` | 空目录 | 删除（旧 84 因子基线已作废） |
| `factor_db_oos/`（12 文件） / `factor_db_p0/`（12） / `factor_db_p0_oos/`（6） | **已归档**：`user_workspace/archive/legacy_factor_dbs_20261116.tar.gz`（49 条目 / 1.9 MB，`tar -tzf` 核对过） | 目录本身留到 **M1**（对象存储布局落地）再删；归档已在，删除随时可做 |
| `factor_db/{metadata,values}` | **空**（0 文件） | 迁移到对象存储布局后删旧目录 |
| `fabric/` 的 `Stage` 协议 | 旁路契约层 | 并入 `contracts/` 后删除，不保留第二套协议 |
| `scripts/run_mine.py` | 已 DEPRECATED 转发 shim | 调度改指 `pipeline` 后删除 |
| `api/` + `web/` | Web 壳 | **不删**，冻结；契约要能接住（`09` §2.5） |

**删除顺序**：先删「有替代且对拍通过」的，再删「无替代但确认不需要」的。`factor_db*` 目录属于后者：**2026-11-16 已归档，删除动作推迟到 M1**——归档件在 `user_workspace/archive/`，晚删只省一步，早删不会让任何当前工作变简单，而 M1 落对象存储时正好一次性清干净。

---

## 7. 迁移期的三个陷阱

1. **双写**：新旧两套都跑、取其一。→ 迁移期只允许一套是权威实现，切换点写在配置里，切完删除另一套。
2. **顺手优化**：搬的时候「顺便改好」。→ 迁移与优化分开提交。否则对拍不一致时，无法区分是搬家搬错了还是优化改错了。
3. **用新测试证明旧代码错**：新测试是针对新契约写的，它红不代表旧路径有 bug，只代表契约不同。→ 旧路径的判定只认对拍，不认新测试。

---

## 8. 回滚策略

| 时点 | 回滚方式 | 代价 |
|---|---|---|
| M0–M1 | 删 `v5/` 目录 | 无（旧代码未动） |
| M2（供给链切换） | 配置指回旧数据根 | 需重跑一次 refresh |
| M3（能力切换） | 配置把 `impl` 指回旧适配器 | 旧适配器必须仍在仓库里——这是「旧路径保持可跑」的意义 |
| M4（编排切换） | **不可回滚到单函数流程** | 一旦 Parcel 落盘成为唯一产物形态，旧 pipeline 已读不懂新产物。所以 M4 必须在 M3 对拍全绿后一次切换 |
| M5（CLI 切换） | 保留旧命令名做别名 | 命令面语义变化需在 CHANGELOG 标注 |
| M6（删旧） | 只能从 git 历史恢复 | 因此删除必须单独成 commit，且 message 写清替代品 |

---

## 9. 不变量与验收

| 编号 | 内容 | 检查点 |
|---|---|---|
| **I44** | 迁移期只有一套权威实现；不允许「两套都算再取其一」 | 配置里同一能力不得有两个活跃 `impl`；`git grep` 不得出现运行时分支选择新旧实现 |
| **I45** | 旧路径在新路径通过同一 AC 之前不得删除 | M6 之前删除清单中的项必须仍可被 import（或已在同一 commit 中提供等价替代） |
| **I46** | 每次切换都留下对拍记录 | `reports/migration_parity_*.json` 存在且字段完整；缺失即视为未验收 |
| **AC44** | 暴露对拍 | 同一表达式在手写 pandas 参考实现（仅测试用，不 import `v5`）与新 `evaluate` 上相对误差 ≤ `1e-12`（含 NaN 位置）。**2026-11-17 裁决**：原「V4 `evaluator` 逐位对拍」取消——不依赖 V4 任何内容，参考实现改为 pandas 手写 |
| **AC45** | 回测对拍 | 同一权重序列 → 手写小样本（3 资产 × 20 bar，逐笔可核）逐项相等，或差异有书面口径解释 |
| **AC46** | 删除可追溯 | 每个被删文件在 `MIGRATION.md` 有一行「原路径 → 替代品 → 删除 commit」 |

---

## 10. 反模式

1. **「重写比迁移快」**：重写会丢掉 V4 已经付过学费的修复（假成功、交易所串目录、负 shift）。这些修复没有测试覆盖的部分，重写后必然复发。
2. **按文件搬而不是按层搬**：同一层的代码分散在四个目录时，先合目录再搬层，否则契约会被目录结构带偏。
3. **迁移期新增功能**：新功能与迁移混在一起，回滚粒度就变成「全有或全无」。
4. **把 `fabric` 当 V5**：它是旁路契约层，1275 行里没有一条接进生产链路；把它当成品会得到「有契约、没执行」的假完成。
