# 22 · 因子挖掘能力对比：FactorFactory vs Qlib

> 口径：Qlib 以本地仓库 `D:/code/github/qlib`（commit `5435523`，2026-10-08）源码为准；
> FactorFactory 以本仓库当前状态为准（20 个命令 / 908 项测试 / 59 个内置因子 / 46 个算子）。
> 目的不是评优劣，而是回答三个问题：**两边各自在解决什么、边界在哪、该抄什么**。

---

## 0. 一句话结论

- **Qlib 是预测建模平台**：因子是特征，核心是把特征喂给 35 个模型里最合适的那个，用 IC / 回测证明**模型**好。
- **FactorFactory 是因子裁决流水线**：因子是可执行表达式，核心是用多重检验 + 成本 + 样本外证明**因子本身**站得住。
- 因此在「因子挖掘」这个交集上：**Qlib 的表达空间和工程效率强一个量级，FactorFactory 的判据纪律和可复现性强一个量级**。两者不是替代关系，是上下游关系。

一个可检验的判据：Qlib 源码里搜不到任何 `genetic / symbolic regression / factor mining` 模块，也没有 `FDR / DSR / embargo`；而 FactorFactory 的 4 个搜索算法和 5 道统计闸是主链路，不是插件。

---

## 1. 定位差异

| 问题 | Qlib | FactorFactory |
|---|---|---|
| 核心对象 | 预测模型（Forecast Model） | 因子表达式（Candidate） |
| 因子从哪来 | 人工写表达式 / Alpha158·360 模板 / 外挂 RD-Agent | 4 个搜索算法 + 先验池 + 模板枚举 |
| 怎么判断好用 | IC / RankIC / ICIR、回测年化 / IR，人看 | HAC t、置换 p、BH-FDR、DSR、成本闸、OOS 折 |
| 产物 | 模型 + 预测分 + 回测报告 | 候选 → 证据 → 裁决 → 证书 → 因子库 |
| 复现单位 | 配置 + MLflow run | snapshot + plan digest + 单步重建 |
| 市场 | A 股 / 美股（现货股票） | 币安 USDT-M 永续 |
| 频率 | 日频 + 1min | 日频（可扩） |
| 规模 | 800 只/日 × 13 年（基准） | 199 标的，其中约 38 个有 ≥1500 天历史 |

两边的「因子」甚至不是同一个东西：Qlib 的因子是**模型输入的一列特征**，FactorFactory 的因子是**可独立求值、可内容寻址、可认证的表达式**。这决定了下游所有差异。

---

## 2. 能力对照总表

图例：● 强 / ◐ 有但不完整 / ○ 基本没有。

| 维度 | Qlib | FactorFactory | 说明 |
|---|:--:|:--:|---|
| 因子表达空间 | ● | ◐ | Qlib 约 50 个算子、Alpha158 158 个特征；FF 34 个算子、9 个字段、59 个内置因子（含 v4） |
| 自动搜索算法 | ○ | ● | Qlib 无内建搜索；FF 有 GP / 随机 / 模板枚举 / 束搜索，且共享试验计数 |
| 统计判据 | ○ | ● | Qlib 只有 IC / IR；FF 有 HAC t、置换 p、BH-FDR、DSR、成本闸 |
| 多重检验校正 | ○ | ● | Qlib 无；FF 有 `n_trials` / `trial_family` / DSR benchmark |
| 样本外认证 | ◐ | ● | Qlib 手动切 test；FF 自动多折 + embargo + evidence_grade |
| 成本建模 | ◐ | ● | Qlib 固定开平仓费率；FF 分层成本 + 资金费事件 + 滑点 + 保证金/强平 |
| 组合构建 | ● | ◐ | Qlib TopkDropout / EnhancedIndexing / 优化器；FF 分位多空 + 岭回归 + 约束投影 |
| 模型能力 | ● | ○ | Qlib 35 个模型实现；FF 无 ML 模型层 |
| 数据工程 / 性能 | ● | ◐ | Qlib 二进制列存 + 双层缓存（14 因子 × 800 股 × 13 年 7.4s）；FF parquet 无表达式缓存 |
| 加密数据适配 | ○ | ● | Qlib 股票现货；FF 原生 funding / basis / OI、365 年化、退市强平 |
| 复现与审计 | ◐ | ● | Qlib MLflow 记录配置与产物；FF 快照 digest + 单步重建 + 复盘 |
| 生产 / 执行 | ◐ | ○ | Qlib 有在线预测滚动，官方明确不生成订单；FF 无交易网关 |

---

## 3. 逐段对比

### 3.1 表达空间：Qlib 宽，FactorFactory 窄而严

Qlib 的算子注册表 `OpsList` 有 49 个条目（加 `TResample` 与别名约 50 个），覆盖：

- 时序：`Ref / Mean / Std / Var / Skew / Kurt / Med / Mad / Slope / Rsquare / Resi / Quantile / Rank / Count / EMA / WMA / IdxMax / IdxMin`
- 截面：`Rank / Quantile / Mask`
- 算术与逻辑：`Add / Sub / Mul / Div / Power / Greater / Less / And / Or / Not / If / Abs / Sign / Log`
- 事件：`Feature / PFeature`（PIT 基本面）

Alpha158 = **9 个 KBAR + 4 个 PRICE + 29 个滚动算子 × 5 窗口 = 158**；Alpha360 = 6 字段 × 60 天 = 360。这两套是**预置特征集**，不是搜索空间——但它们是极强的先验，等价于「人工模板枚举」的成熟版本。

FactorFactory 的注册表是 **9 字段 / 46 算子**：v1 的 19 个（带参时序 + `rank/scale/abs/log/sign` + `add/sub/mul/div`）、v2 的 15 个 Alpha158 时序族与逐点幂、v3 的 12 个（`quantile`/`ts_count`/`ts_rank_raw`/`elem_max`/`elem_min`/6 个比较算子/`where_`）。`template_enum` 是 8 个经济模板 × 6 个窗口 = 48 个组合，`BUILTIN_FACTORS` 59 个。

**v2 → v3 扩表把这一节原来点名的三个天花板拆了**：过去 60 天线性回归的残差 → `resi(close,60)`；价格在 60 天窗口的分位数位置 → `ts_rank(close,60)`，真正的 q 分位数 → `quantile(close,60,0.75)`；距离前高的天数 → `ts_argmax(high,60)`；"只在上涨日统计" → `where_(gt(close, ref(close,1)), ...)`。仍未覆盖的是 `Med`（qweave 无中位数原语）、`GroupRank/GroupNeutralize`（需分组字段），以及 5 个 qweave 内部递归原语——见 `docs/26-qweave-operator-surface-audit.md`。

**剩下的差距不在词汇量，在词汇的**深度**：Qlib 的算子可以直接吃 `$feature` 做截面与分组，FF 的截面算子目前是 `rank`/`scale` 加上 v3 的条件选择，仍没有分组字段，写不了"板块内中性化"或"按成交额加权分位"。

### 3.2 搜索：FactorFactory 独有的一层

Qlib 的「alpha seeking」在工程上是**人工写表达式 + 模板集**，自动化部分放在独立仓库 RD-Agent（LLM 多智能体提假设 → 写表达式 → 跑 Qlib 验证），不在 Qlib 主链路里。Qlib 本身没有搜索算法、没有候选去重、没有试验预算概念。

FactorFactory 的 Search 工位是四方法抽象（`describe/fit/generate/lineage`），当前 6 个实现：

| 算法 | 类型 | 特点 |
|---|---|---|
| `gp_symbolic` | 进化 | deap 树搜索，fitness 只用挖掘窗口的秩相关 |
| `random_expression` | 基线 | 同空间均匀采样，用来检验 GP 的选择压是否真有信息 |
| `template_enum` | 枚举 | 经济模板 × 窗口网格，确定性、可解释 |
| `beam_search` | 贪心 | 逐层扩展 + 按适应度保留 beam |
| `semantic_mutation` | 邻域 | 从 59 个内置因子做窗口/字段/符号/rank/scale 小步变异，轮转覆盖全部锚点 |
| `factor_blend` | 配对 | 内置因子与 6 个正交经济锚点做截面秩差/秩比 |

三条接口约束是 Qlib 没有的：`generate()` 不允许评估（反馈只能在 `fit()`）、`fit` 的调用单里根本没有评估区间引用（`fit_window=mine_only` 在类型上成立）、**跨算法共享一份试验预算**且按内容身份去重。最后一条直接决定了下游 FDR / DSR 的 `n_trials` 是否可信。

### 3.3 评估与判据：这是最本质的差异

Qlib 的评估产出（`SigAnaRecord` / `PortAnaRecord`）：IC、RankIC、ICIR、年化收益、信息比率、最大回撤、换手。没有显著性检验，没有多重检验校正，没有「扣完成本后是否还显著」的闸门。判断一个因子能不能用，靠人看 IC 和回测曲线。

FactorFactory 的 `observe` 产出：rank IC、ICIR、**HAC t（Newey-West）**、**截面置换 empirical p**、`cost_adj_return`、换手、分组收益、单调性、IC 衰减。`verdict` 再把它们送进五道闸：

| 闸 | 作用 | Qlib 对应物 |
|---|---|---|
| `dsr_fail` | Deflated Sharpe，基准随 `n_trials` 上升 | 无 |
| `fdr_fail` | BH-FDR 控制族错误率 | 无 |
| `cost_fail` | 净成本后收益必须为正 | 回测含成本，但不当判据 |
| `null_fail` | 置换零分布检验 | 无 |
| `insufficient_n` | 有效样本不足，不给结论 | 无 |

裁决只有 `pass / reject / watch` 三值，`insufficient_n` 和 `redundant` 是软理由——**证据不足不算否决**。这套语义 Qlib 没有对应层。

用一句话说：Qlib 回答「这个信号有没有预测力」，FactorFactory 回答「在试了 N 个候选、扣掉成本、放到没见过的数据上之后，这个信号还剩下多少」。

### 3.4 样本外：手动切分 vs 自动认证

Qlib 的 `DatasetH` 只做 `train / valid / test` 三段切分，没有 embargo、没有多折、没有证据等级。日频标签若跨 h 天，train 末端与 test 起点的标签窗口会重叠，源码里没有处理这个的机制。

FactorFactory 的 `certify` 把 OOS 区间切成 `folds` 段，段间留 `embargo` 根 bar 隔离，逐折复算净收益 / 夏普 / 回撤 / 换手，产出 `evidence_grade=out_of_sample`；`promote` 默认要求 `--min-grade out_of_sample`。证书身份是 `(snapshot_id, factor_id)`——同一个因子换一份数据要重新认证。

这不是「更严格所以更好」，而是**目标不同**：Qlib 要的是模型在 test 段的表现，FF 要的是因子结论的可发布性。

### 3.5 成本与组合：两个市场的不同答案

Qlib 的成本模型是股票现货的：开仓费率、平仓费率、单笔最低 5 元、涨跌停不成交（`limit_threshold=0.095`）。组合是 `TopkDropout(topk=50, n_drop=5)` 或带跟踪误差约束的增强指数。

FactorFactory 的成本模型是永续合约的：`cost = turnover × (分档手续费 + 滑点)`，资金费按**真实结算事件**累计（周期是数据属性，不写死 8h），另有保证金与强平模拟。组合是分位多空 + `equal/liquidity/inv_vol` 三种权重 + 权重上限 / 净敞口 / 换手上限 / 最小成员数，权重估计用 `fixed_ridge`，且强制 fit / embargo / eval 三段窗口。

两边都「有成本、有组合」，但不可直接比强弱：Qlib 的成本项少，是因为 A 股现货的成本结构简单；FF 的成本项多，是因为永续合约的资金费会吃掉 carry 类因子的大部分收益——不建模就必然高估。

### 3.6 复现与数据治理

Qlib 的复现靠「配置 + MLflow」：`qrun` 读 YAML，Recorder 记录参数、指标、artifact。数据侧是 `.bin` 列存 + 日历对齐 + ExpressionCache / DatasetCache 两级缓存，PIT 只覆盖季频 / 年频基本面（`P($$roewa_q)`）。

FactorFactory 的复现靠「指纹 + 快照 + 单步重建」：`policy_digest` / `caliber_digest` / `parcel_digest` 逐层锁定，快照清单记录每个文件的 `blob_digest` 与 `columns_digest`，`replay` 可以在新快照上重跑旧计划并对比。数据侧还有 Gate A（覆盖 / 缺口 / 未闭合 bar）与 Gate B（标的集 digest / 覆盖区间）两道 fail-closed 门禁。

Qlib 的复现回答「这次跑的是哪份配置、哪个模型」，FF 的复现回答「这次跑的是哪份数据、哪个表达式、哪套口径」。后者更适合因子研究，前者更适合模型迭代。

---

## 4. 同一个因子，两边的走法

以 60 日动量为例。

**Qlib**

```text
表达式：$close / Ref($close, 60) - 1
用法：  作为 Alpha158 之外的一列特征，进 LightGBM
评估：  SigAnaRecord 出 IC / RankIC / ICIR；PortAnaRecord 出年化 / IR / 回撤
结论：  人看 IC 是否稳定、回测是否跑赢基准
```

**FactorFactory**

```text
AST：   div(sub(close, ref(close, 60)), ref(close, 60))   # 内置 mom60
链路：  boundary → panel → target → search → evidence → verdict → portfolio → certify
评估：  observe 出 rank IC / HAC t / 置换 p / cost_adj_return
裁决：  verdict 过 DSR + FDR + 成本 + 零分布四道闸
认证：  certify 切 3 折 + embargo，出 out_of_sample 证书
入库：  promote 写 catalog，证书键 (snapshot, factor)
```

| 步骤 | Qlib | FactorFactory |
|---|---|---|
| 定义 | 表达式字符串 | 规范化 AST，内容寻址 `sha256` |
| 计算 | 表达式引擎 + 缓存 | Panel / FieldStream，逐候选求值 |
| 评估 | 作为特征进模型，出 IC | 独立评估，出 IC + 显著性 + 成本 |
| 判断 | 人看 | 五道闸自动裁决 |
| 样本外 | 手动 test 段 | 自动多折 + embargo |
| 沉淀 | 模型 artifact | 因子库 + 证书 |

同一个 `mom60`，在 Qlib 里只是 158 列之外的一列；在 FF 里是一条完整的证据链。这就是两种范式的具体差别。

---

## 5. Qlib 值得借鉴的 5 件事（按 ROI 排序）

### P0 · 扩算子与模板（直接抬高搜索空间天花板）

补 `Slope / Rsquare / Resi / Quantile / IdxMax / IdxMin / Count / EMA / WMA / If / Mask` 等算子，并把 `template_enum` 扩到 Alpha158 风格的 KBAR 特征 + 29 个滚动算子 × 5 窗口。

- 成本：注册表 + 实现 + 契约测试；不动证据链。
- 风险：低。表达式空间变大只影响搜索，不影响判据。
- 验收：注册表自检通过、GP 能表达回归残差、模板枚举产出从 48 升到 150+。

### P1 · 表达式缓存（工程效率）

Qlib 的实测数字很有说服力：14 因子 × 800 股 × 13 年，无缓存 147s，开双层缓存 7.4s。FF 目前每次 run 重算所有候选，同表达式跨计划不复用。

- 成本：缓存键 `(snapshot manifest digest, 规范化 AST digest, 字段集)`，落盘与失效策略。
- 风险：中。缓存只能加速，不能改变结果；必须把 dtype / 版本写进键，否则会引入「同表达式不同值」的隐蔽错误。
- 验收：同计划两次跑逐位一致（AC39 仍成立），缓存命中率与耗时下降可观测。

### P1 · 模型化组合（补非线性）

FF 现在的组合是 `fixed_ridge` 线性加权，抓不到因子间的非线性交互——这正是 Qlib 用 GBDT 的强项。可行的做法是把 GBDT 作为 `combine` 的一种方法（`scheme=gbdt`），输入是成员因子暴露，输出是合成分数。

- 边界：模型只做**组合**，不做因子；因子身份仍是表达式，证据链不变。
- 风险：高。必须复用 fit / embargo / eval 三段窗口，且组合层结论仍标 `in_sample`，只有认证段能发布。
- 验收：同一批成员，GBDT 组合的 OOS 净夏普优于 ridge，且 `n_trials` 不因模型调参而虚高。

### P2 · 分析报告层

Qlib 的 Analyser 把 IC 时间序列、月度 IC、分组累计收益、换手、回撤画成一张报告。FF 的数据都有（`ic_decay` / `group_returns` / `monotonicity`），但没有可视化产出。

- 成本：一个 HTML 报告生成器，读 run 产物。
- 风险：低。
- 验收：一次 run 结束能直接打开报告，不必手工拼图。

### P2 · 滚动重训 / 市场自适应

Qlib 有 Rolling Retraining 与 DDG-DA。FF 目前组合权重在一段 fit 窗口内估一次。可做「滚动 fit」——但只作用于组合权重，不改变因子身份。

- 风险：中高。滚动窗口本身是自由度，必须进 `caliber_digest`，否则就是新的过拟合入口。
- 验收：滚动组合的 OOS 表现提升，且窗口参数进指纹。

---

## 6. 不该照搬的 4 件事

| 不抄 | 理由 |
|---|---|
| **35 个模型的模型动物园** | 不是当前瓶颈。FF 的瓶颈是候选质量与统计功效，不是模型容量；把因子变成模型会破坏内容身份与裁决链 |
| **LLM 自动挖因子（RD-Agent）** | 同 prompt 不同输出，违反 AC39 的同 seed 逐位一致；只能作为先验来源（LLM 提假设 → 表达式 → `prior_pool`），不能进 Search |
| **在线 Serving / 订单执行** | FF 没有交易网关，Qlib 官方也明确「只更新预测、不生成订单」；这是另一个工程域 |
| **PIT 基本面** | 加密没有财报；FF 的 PIT 对象是上市 / 退市 / 缺口 / 未闭合 bar，不是公告日 |

---

## 7. 结论

如果目标是**「给定一批特征，找最好的预测模型」**，Qlib 是更成熟的底座：模型多、数据快、生态大、基准可对比。

如果目标是**「在一个表达式空间里，找出在多重检验和成本约束下仍然成立的因子」**，FactorFactory 的链路更完整：Qlib 缺的那一层（搜索、试验计数、FDR/DSR、样本外认证、因子证书）恰好是 FF 的主链路。

最务实的组合不是二选一，而是：**用 Qlib 的算子与模板扩充 FF 的表达空间，用 FF 的判据链约束 Qlib 式的高产搜索**。前者解决「想不到」，后者解决「信不过」。

---

## 8. 事实与口径

| 事实 | 来源 |
|---|---|
| Qlib commit `5435523`（2026-10-08） | 本地 `git log` |
| Qlib `OpsList` 49 个算子条目 | `qlib/data/ops.py` L1566 |
| Alpha158 = 9 KBAR + 4 PRICE + 29 滚动 × 5 窗口 | `qlib/contrib/data/loader.py`；滚动块计数 29 |
| Qlib 35 个模型实现、26 个 benchmark 目录 | `qlib/contrib/model/*.py`；`examples/benchmarks` |
| Qlib 无搜索 / FDR / DSR / embargo | 全仓 grep 为空 |
| Qlib PIT 仅基本面（`P($$roewa_q)`） | `qlib/data/data.py` `LocalPITProvider` |
| FF 9 字段 / 46 算子 / 59 内置因子 / 4 算法 / 20 命令 / 908 测试（本次扩表后） | 本仓库代码与 `pytest --collect-only` |
| FF 因子库 11 active / 2 retired | `~/factorfactory/catalog/factors.jsonl` |

**不可比声明**：两者市场、频率、标的数、数据源都不同，本文只比「因子挖掘的方法与工程」，不做收益或性能的横向 benchmark。Qlib 的 7.4s 是官方 README 的检索性能数字，不是本机实测。
