# 20 · 因子挖掘算法：原理、调研与实现方案

> 本篇回答三个问题：**V5 现在用哪些算法、为什么是它们、下一步怎么扩**。
> 术语与 `02-mining-interface.md` 保持一致：算法 = 四方法抽象（`describe/fit/generate/lineage`）的一个实现；产出统一 CandidateSet，之后整条链只认这份结构。

---

## 1. 搜索空间的定义（算法与空间必须分开）

所有算法共享同一个**表达式空间**，差异只在「怎么在这个空间里找」：

| 要素 | 定义 | 出处 |
|---|---|---|
| 字段 | 注册表 `fields.json`（因果安全字段：OHLCV + funding/OI/basis 事件字段） | `11-field-operator-registry.md` |
| 算子 | 注册表 `operators.json`（arity 封闭集；`bfill/lead/future` 等因果禁令词在加载期即拒） | 同上 |
| 语法 | AST dict（`op/field/const`），深度上限由算法参数约束；非法 AST 在 evaluate 处 fail-closed | `04-runtime.md` |
| 身份 | Candidate 内容身份 = `sha256:` 规范化表达式摘要；同表达式跨算法只算一次试验 | `02-mining-interface.md` §3 |
| 标签 | `caliber.label`（horizon/transform/entry）由 Target 工位统一产出，算法**不可**自选标签 | `06-evaluation.md` §3 |

两条铁律对任何算法都成立，写在这里是因为它们是算法层的**接口约束**而不是实现细节：

1. **`generate()` 不评估**。反馈只允许发生在 `fit()`（`fit_window=mine_only`，两张调用单从类型上切断越界）。
2. **计数跨算法共享**。一次执行一份 Budget：候选按内容身份去重，`n_trials` = 去重后实测数，无论候选来自几个算法（I21）。

---

## 2. 已实现的两个算法

### 2.1 `gp_symbolic` — 遗传规划符号回归（deap）

**原理**：把表达式编码为树（整数基因串），用进化搜索拟合「表达式暴露 → 标签收益」的秩相关：

```
init（ramped half-and-half）→ 逐代评估 fitness → 锦标赛选择
  → 交叉（子树交换）+ 变异（子树替换/点变异）→ 精英保留 → 下一代
```

- **适应度**：`fit()` 内经 `ctx.fitness` 评估——暴露与 `raw` 标签的截面 Spearman 秩相关（`ic_mean`）。选择压力只来自挖掘窗口，评估窗口在类型上不可达。
- **去重**：种群去重用**内容身份**，不用树结构相等——结构不同、语义相同的表达式不重复计数。
- **确定性**：deap 种群种子由执行器派生（`master_seed + scope`），同计划同数据两次跑候选集合逐位一致（AC39）。全局 `random` 禁用（E2007 契约层兜底）。
- **参数**：`population/generations/max_depth/max_candidates`。`max_candidates` 截断产出（配额内去重）；截断后 `n_trials` 按实际产出记账。

**已知局限**（写明，不掩盖）：
- GP 的 fitness 是全挖掘窗口的单一秩相关——对时变 regime 不敏感；这是有意的保守选择，规避「在窗口内学方向」类泄漏。
- 种群规模 × 代数与候选数的关系是非线性的：重复个体多时实测产出远小于配额。观测手段是 Search 产物 `generators[].produced`（15-operations §4-5）。

### 2.2 `random_expression` — 均匀随机基线

**原理**：同一表达式空间上的拒绝采样（生成 → 语法/深度校验 → 查重 → 接受），无 fitness、无进化、无状态（`stateful=False`，无 fit 调用单）。

它存在的理由不是「找因子」，而是**给 GP 一个对照系**：
- GP 的产出如果系统性优于同量随机采样，才说明进化压力真的在工作；
- 反之（两者量级相当），说明 GP 的选择压没有信息量——这是最廉价的算法有效性自检。
- 实操上两者共享同一 fitness/seen 计数，一次 Budget 回答两套产出。

### 2.3 先验池 + 因子库闭环（不新造表达式，只复用身份）

第三个「来源」不是算法，是**先验注入**：

- `prior_pool` 支持三类条目：内置因子名（`mom60` 等 12 个，AST 在编排侧解析）、`db:*`（拉全库 active）、`db:<name>`（精确引用）。
- 因子库（`catalog/factors.jsonl`）append-only、按 `factor_id`（= candidate_id 内容身份）折叠状态、**不进任何指纹**——加因子不得使历史 plan 失效。
- 通路意义：本轮通过裁决的因子，下一轮可作为先验直接进入搜索池，与 GP/随机产出同台受审。**先验候选与新生成候选在同一套 Evidence/Verdict 判据下裁决**——「库里的因子」不豁免。

---

## 3. 业界方法调研与取舍

按「能否纳入四方法接口、能否满足因果/计数约束」评估，结论分三档。

### 3.1 已纳入或同族（可扩）

| 方法 | 原理 | 相对 GP 的优劣 | V5 适配成本 |
|---|---|---|---|
| **模板枚举**（Alpha101/Alpha158 族） | 人工算子模板 × 参数网格穷举 | 无进化噪声、全覆盖、可解释；组合爆炸需配额截断 | 低：无状态算法，模板展开即 `generate`；qweave 已载入 Alpha158 全集 |
| **随机搜索 / 模拟退火** | 表达式空间随机游走，温度控制接受劣解 | 实现极简；退火比纯随机多「局部细化」 | 低：`random_expression` 加温度参数即成（需 stateful+fit） |
| **CMA-ES / 贝叶斯优化** | 连续参数空间进化策略；对表达式结构搜索需先编码 | 样本效率高于 GP；但离散树结构适配复杂 | 中：需先定义树↔向量编码，编码本身引入结构偏置 |
| **语法引导遗传编程（GE/CGP）** | 基因型→表现型解耦（文法约束）| 保证语法合法，省去拒绝采样 | 中：deap 支持；收益主要是省 evaluate 浪费 |

### 3.2 明确不采用（含理由，防「看起来更先进」的复辟冲动）

| 方法 | 拒绝理由 |
|---|---|
| **gplearn** | 表达力弱（算子集固定、无注册表约束）、无法对接我们的因果字段层与内容身份；V5 选型审计已定 deap（`08-reuse-audit.md`） |
| **神经网络端到端（Transformer/GNN 出因子权重）** | 产出不是表达式——整条 Evidence/Verdict/审计链都建立在「候选是可执行、可指纹、可逐位复算的表达式」上；NN 权重无法内容寻址去重，计数与血缘全断。若要 NN 特征，路径是「NN 离线蒸馏成表达式先验进 prior_pool」，不是进 Search |
| **强化学习挖因子** | **延后，不是否决**。障碍按权重：① `AC39` 要求同 seed 两次 `generate` 逐位一致，而 GPU 上的非确定性归约会让它失效（要满足只能 CPU + 单线程 + deterministic，训练成本再乘数倍）；② 样本效率——AlphaQCM 整篇论文就在治「多数候选弱→零奖励」，而我们的截面比它的实验设定小两个量级；③ torch/GPU 进链路会改变 S5 单步重建与镜像成本；④ 没有证据表明它在同一表达式空间里优于现有 GP（GP 已自主找出 basis carry）。**注意：RL 的 reward 可以只在 `mine_only` 窗口内计算，不构成 eval 泄漏——上一版把「边看边学」当作否决理由是错的**。解锁条件：截面 ≥100 且 GP/模板枚举的 pass 率饱和；届时 `stateful=True`、policy 权重落 `fitted_state`、`generate` 用固定噪声保证决定论 |
| **LLM 生成表达式** | 可作为**先验来源**（LLM 提出假设 → 表达式 → prior_pool），不可作为 Search 内算法：生成不可复现（同 prompt 不同输出），AC39 决定论对拍不成立；且 LLM 失败静默 mock 入库是 V4 已确认缺陷（15-operations §4-5 红线） |

### 3.3 观望（等真实证据再说）

| 方法 | 观望条件 |
|---|---|
| **多目标进化（NSGA-II：收益 vs 换手）** | 等 Verdict 数据积累出「收益-换手前沿」的真实形状；现在单目标 + 成本闸已够，多目标会把预算摊薄到前沿两端 |
| **迁移学习/跨市场预训练** | 等双市场（spot/futures）数据量足以支撑；当前 20 标的 × 1277 bar 对任何预训练都是玩具规模 |
| **神经架构搜索式宏搜索** | 表达式深度 ≤3 的空间里穷举+GP 已近全覆盖，宏搜索的收益空间不存在 |

---

## 4. 扩展方案：加一个新算法要动什么

设计目标：**新算法只动一处（算法注册），其余七步零改动**。清单：

1. **实现四方法**（`v5/capability/mining.py` 同族模块）：
   - `describe()` → `AlgorithmSpec(name, version, stateful, needs_seed, needs_fitness)`；
   - `generate(view, params, seed, fitted_state)` → CandidateSet（唯一出口）；
   - stateful 算法加 `fit(ctx, params, seed)`，产出必须可序列化、可重建（FittedState 落盘）；
   - `lineage(candidate)` → 父代/算子记录（GP 已有；模板枚举是「模板名+参数」）。
2. **注册**：加入算法实现目录（`impls` 注册表），编译期 `_resolve_impl` 按声明名校验（E1002，不静默替换）。
3. **参数 schema**：`registry/params.json` 的 `search.params.algorithms` 下登记参数定义（加载即自检 E1015）。
4. **零改动**：编排层共享 fitness/seen 计数、Candidate 内容身份去重、Evidence/Verdict 判据、Certify 认证——全部按接口自动生效。
5. **契约测试**（新算法验收四条）：
   - 同 seed 两次 `generate` 逐位一致（AC39）；
   - 产出的每个候选能被 `evaluate` 求值（`unobservable` 通道为空或全部实名有因）；
   - `n_trials` = 去重后候选数（跨算法共享，不虚高）；
   - stateful 算法的 fit 产物可落盘重建（S5-M2 重建对拍逐位一致）。

---

## 5. 决策记录

| 裁决 | 日期 | 理由 |
|---|---|---|
| GP 引擎选 deap，不用 gplearn | S3 选型 | 表达力/注册表对接/内容身份三处 gplearn 全不合格 |
| `random_expression` 作为常驻基线 | 2026-11-17 | GP 有效性的最廉价对照；共享计数零额外成本 |
| 先验池 + 因子库闭环 | 2026-11-17 | 「上一轮结论当下一轮假设」是研究的正常节拍；库里因子不豁免判据 |
| LLM/NN 不进 Search | 2026-11-17 | 不可复现/不可内容寻址；只允许以先验形式进入 |
| 不采用节点级磁盘缓存 | S6 | 实测净亏 65×（`cache.py` node_enabled 默认关，F5 只管键表有界） |
| RL 由「否决」改为「延后 + 解锁条件」 | 2026-11-18 | 原否决理由（「边看边学」与 `fit_window=mine_only` 冲突）不成立：reward 只在挖掘窗口内算即可。真实障碍是 AC39 决定论、样本效率、依赖成本、边际收益未证——全部是条件而非原则 |

---

## 6. 完整链路检验（**每个"好因子"的强制流程**，2026-11-18 起）

> 用户裁决：**后面挖掘发现的每一个候选因子，都必须走完本节全部六步，才算"发现"**。
> 动机：全量跑的组合认证会掩盖成员归因——首轮 E2E 组合 OOS 16.4%/0.789，拆开看 basis carry 单独 18.8%/0.919、mom60 单独仅 0.71%/0.177，组合收益几乎全部来自 basis carry。不做单因子隔离，就会把「组合还行」误读成「每个成员都行」。

### 6.1 六步清单

| 步 | 动作 | 通过标准 |
|---|---|---|
| 1 | **全量跑**：`flow run --plan <plan> --live`，八工位全绿 | `state=done`、8 parcel、严判据（`dsr_min=0.95 / fdr_max=0.10`，文档口径） |
| 2 | **判据核对**：读 Verdict payload 逐闸核对 | `pass` 行 DSR≥dsr_min、p_adj≤fdr_max、emp_p 合理、cost>cost_adj_min；`statistics.dsr` 是 verdict 重算概率值 |
| 3 | **单因子隔离复跑**：prior_pool 只留该因子（内建名或 `db:<name>`），算法带 `random_expression{max_candidates:1,max_depth:1}` 陪跑 | `pass`、Portfolio `member_count=1` 且 weight 1.0 |
| 4 | **OOS 认证**：读 Certify payload | `evidence_grade=out_of_sample`、`fold_count≥3`、逐折 `positive_folds`、net_return/net_sharpe/max_dd/turnover 全部落盘 |
| 5 | **入库**：`flow promote --run-id <id> --candidate <cid> --min-grade out_of_sample --yes` | `promoted` 或 `already_in_catalog`；**只 promote 指定 pass 候选**（默认会带 watch，必须 `--candidate` 收窄） |
| 6 | **回灌复用**：下一轮计划 `prior_pool: ["db:<name>"]` 能命中 | resolve_prior_pool 的 `prior_candidates` 含该因子、`prior_skipped` 无它 |

### 6.2 已知边界（写清，避免误读）

- **live 模式必须有 ≥1 算法**（E1004 fail-closed）：单因子复跑用 1 个最浅 `random_expression` 候选陪跑；陪跑候选在严判据下几乎必拒，若意外 pass 则换 seed 重跑。
- **隔离复跑的 n_trials 很小**（去重后通常 2），DSR 门槛随之降低——这是诚实设计（只试 2 个候选门槛就低）。**多重检验纪律以全量跑（n_trials=87 等）为准**，隔离跑只验链路机械与归因，不作为显著性证据。
- **`tested:false` 的 watch 候选会进组合拿满权重**（s0/04 L575 / combine pass/watch 准入，现契约如此）——隔离复跑时若陪跑候选变 watch，Portfolio 不再单成员，须换 seed 重跑。
- 跨 run 的 emp_p 在 ±1/(n+1) 粒度浮动（零分布按 ticket 种子派生）；同 ticket 内单步重建逐字节一致（S5-M2 已验）。

### 6.3 首次应用记录（2026-11-18）

| 因子 | 表达式 | 全量跑裁决 | 隔离 OOS（3 折 / 359 bar） | 入库 |
|---|---|---|---|---|
| mom60 | `(close−ref(close,60))/ref(close,60)` | pass（DSR 0.9853@n_trials=87） | net_return **0.71%**、sharpe 0.177、positive 1/3 | already_in_catalog |
| gp basis carry | `sum(ref(basis,6),10)`（gp_symbolic 自主发现） | pass（DSR 0.9932@n_trials=87） | net_return **18.80%**、sharpe 0.919、positive 2/3 | promoted（db:gp_symbolic:064db084） |

**归因结论**：三成员组合的 OOS 表现主要由 basis carry 驱动；mom60 在 OOS 窗口（2025-07~2026-06）单独贡献接近零。下一步组合层应做正交化/相关性约束（S7 完整 combine 范畴），避免等权低估集中度。

run 指纹：隔离复跑 `20261117T223442Z-03143f7e`（mom60）/ `20261117T223503Z-8b028903`（basis carry），全量严判据 `20261117T165601Z-04b85fd7`。

### 6.4 决策记录（补充）

| 裁决 | 日期 | 理由 |
|---|---|---|
| 每个候选因子必须走 §6 六步 | 2026-11-18（用户裁决） | 组合认证掩盖成员归因；单因子隔离 + 入库 + 回灌才构成「发现」的完整证据链 |
