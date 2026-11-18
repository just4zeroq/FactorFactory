# 因子挖掘：业界/竞品算法全景与我们的差距（2026-11-18）

> 问题：业界或竞品的因子挖掘算法是怎么做的，我们还能做哪些。
> 结论先行：**我们的瓶颈不在"搜索器不够先进"，而在截面太窄、搜索空间太窄、目标函数太单一**。
> 换算法（上 RL/GFlowNet/LLM）在当前数据规模上大概率是负收益；先把前三条补掉，同样的 GP 会多找到东西。

---

## 1. 业界四代范式（按"谁在产生候选"划分）

| 代 | 范式 | 代表工作 | 候选来源 | 关键实测（论文口径，见 §5 不可比警告） |
|---|---|---|---|---|
| 1 | 人工模板 + 枚举 | Alpha101（Kakushadze 2016）、Alpha158（qlib）、WorldQuant BRAIN 表达式语言 | 人写模板 × 参数网格 | 覆盖率是唯一卖点；Alpha158 在 qlib 基准上长期是"打不过就说明模型有问题"的下限 |
| 2 | 遗传规划 / 符号回归 | gplearn、deap、AlphaEvolve（Cui 2021, AutoML-Zero 式）、国内券商金工 GP 系列 | 表达式树变异 + 交叉 + 适应度选择 | GP 优于随机基线，但在大种群下被证明次优（Petersen 2021）；code bloat、对微小改动敏感 |
| 3 | RL 序列生成 | **AlphaGen**（KDD 2023, PPO 逐步选算子/字段）、Alpha²（2024, 代码生成 + 量纲分析）、RiskMiner（2024, risk-seeking MCTS）、**AlphaQCM**（2025, IQN 分布式 RL） | 策略网络逐 token/逐步构造表达式 | AlphaGen：CSI300 IC 0.0725 vs GP 0.0183；AlphaQCM：CSI500 IC 0.0955、ICIR +27% |
| 4 | 池感知 / 多样性 / LLM 闭环 | **AlphaForge**（AAAI 2024，生成式网络 + 动态组合）、**AlphaSAGE**（2025, GFlowNet）、AlphaAgent（2025, LLM + 新颖性/复杂度正则）、Alpha-GPT、QuantAgent、RD-Agent、TreEvo / CogAlpha（LLM 思维树进化）、Alpha Jungle（LLM + MCTS） | 生成模型 / 采样器 / LLM 代理 | AlphaSAGE：CSI300 Sharpe 1.71 vs AlphaGen 0.84；AlphaAgent：CSI500 年化 11%、IR 1.49 |

三条趋势值得单独记住：

1. **目标从"单因子好"转向"对池子的增量好"**。AlphaGen 的 reward 是"新因子对已有因子池的协同贡献"，AlphaForge 直接把组合模型放进两阶段回路，AlphaSAGE 用 GFlowNet 按 reward 比例采样而不是 argmax（对抗 mode collapse → 高相关因子堆）。这正好命中我们 2026-11-18 自己撞出来的问题：三成员组合 OOS 16.4%，拆开 mom60 只有 0.71%、basis carry 18.8%——**等权组合掩盖了归因，也是"挖出一堆高相关因子"的典型症状**。
2. **多样性必须按行为度量，不按语法度量**。综述里点得很直接：两个语法不同的表达式可能张成同一个暴露；反之亦然。我们现在的去重是内容身份（表达式 sha256），只能防"同一个式子报两次"，防不了"十个式子同一个信号"。
3. **LLM 的位置是"假设生成器"，不是"搜索器"**。所有能复现的工作都把 LLM 放在 idea → 表达式这一步，且都配了正则项（AlphaAgent 的三约束：语义对齐、与既有因子的语法树相似度、复杂度上限）来对抗 alpha decay 与同质化。

---

## 2. 我们现在的真实家底（可核对的数字）

| 维度 | 现状 | 出处 |
|---|---|---|
| 截面 | **20 个永续合约** | 黄金面板 1277 bar × 20 标的 |
| 历史 | **1277 根 1d**（2023-01-01 ~ 2026-06-30） | `db/golden_panel.json` |
| 字段 | **9 个**：open/high/low/close/volume/vwap + funding_rate/open_interest/basis | `registry/fields.json` |
| 算子 | **19 个**：ref/delay/sum/mean/std/max/min/rank/scale/delta/corr/cov/abs/log/sign/add/sub/mul/div | `registry/operators.json` |
| 算法 | 2 个：`gp_symbolic`（deap）、`random_expression`（基线） | `capability/mining.py` |
| 适应度 | **单目标 `abs_rank_ic`**（挖掘窗口内） | `mining.py:570` |
| 去重 | 内容身份（表达式 sha256） | `mining.py:dedup_candidates` |
| 中性化 | label 层支持 `beta` / `log_dollar_volume`，**无分组中性化** | `registry/params.json` label.neutralise |
| 组合 | `combine` 仅等权（S4-lite），`orthogonalise=symmetric` / `corr_max<1` 一律 E3004 | `ops/combine.py` |

对照业界：Alpha101/158 的算子在 40~60 个量级（含 `ts_rank`、`ts_argmax`、`signed_power`、`decay_linear`、`product`、`where`、`skew`、`kurt`、`ts_quantile`），字段在数十到数百；研究截面 N=300~800、历史 10 年+。

**我们的搜索空间大约是主流配置的 1/3（算子）× 1/20（字段）× 1/15（截面）**。在这个空间里，GP 已经能找到 basis carry 这种非常规组合（`sum(ref(basis,6),10)`），说明**搜索器本身没有明显短板**。

### 2.1 最刺眼的一条：算子缺口是"注册表没开"，不是"引擎没有"

实测内核能力面（`venv` 内 `qweave.PyExpr`）：

```
abs delay delta ema log neg rank scale sign slope resi rsquare product quantile
decay_linear wma ts_mean ts_std ts_sum ts_max ts_min ts_rank ts_rank_raw ts_argmax
ts_argmin ts_count ts_kurt ts_skew ts_mad ts_quantile*
+ 模块级：power / signed_power / where_ / group_rank / group_neutralize
+ 预置全集：worldquant_alpha101 / gtja_alpha191 / qlib_alpha158
```

我们注册了 **19 个**，其中**表达力关键的那批一个都没开**：

| 未注册算子 | 缺了它意味着什么 |
|---|---|
| `where_` | **写不出条件逻辑**。"高波动时反转、低波动时动量"这类交互假设完全不可表达——这是搜索空间里最大的一块空白 |
| `ts_rank` / `ts_quantile` | 没有时序分位，Alpha101 的高频用法整族缺失 |
| `ts_argmax` / `ts_argmin` / `ts_count` | 没有"位置/计数"类信息（事件发生了几次、最高点在哪天） |
| `decay_linear` / `ema` / `wma` | 只有等权窗口，没有衰减窗口；对信息半衰期（业界公认的因子衰减结构）零表达 |
| `product` / `skew` / `kurt` / `ts_mad` | 复利、尾部、偏度形态整族缺失 |
| `slope` / `resi` / `rsquare` | 没有趋势斜率与残差化——`resi` 本身就是最便宜的"剔除某信号影响"工具 |
| `signed_power` / `power` | 无法表达非线性强度（Alpha101 的 `signed_power(rank(x),2)` 是标准写法） |
| `group_rank` / `group_neutralize` | 没有分组中性化（股票里是行业，币圈对应板块/beta 分桶） |

**结论：把注册表从 19 扩到 ~40，是零新算法代码、纯配置 + 单测的工作，却把搜索空间扩大一倍以上。** 这比引入任何新搜索器的收益都高，且完全不触碰契约（`operators.json` 加载期自检已就位）。

`group_*` 需要先有"分组字段"（我们的 9 个字段里没有）；可行的最小方案是用**滚动 beta-to-BTC 分桶**或**成交额分桶**在编排侧派生一个 `group_id`，或直接在 label 的 `neutralise` 里加 `beta_bucket` 口径（改动更小）。

---

## 3. 定量诊断：为什么"换算法"救不了我们

单 bar 截面 Spearman 在零假设下的标准差 ≈ `1/√(N−1)`：

| 截面 N | 单 bar SE | T=1271 的 mean-IC SE | 95% 置信半宽 | 检测 IC=0.02 所需 bar 数（80% power） |
|---|---|---|---|---|
| **20（我们）** | 0.229 | **0.0064** | ±0.0126 | **≈1033** |
| 50 | 0.143 | 0.0040 | ±0.0079 | ≈403 |
| 100 | 0.101 | 0.0028 | ±0.0055 | ≈199 |
| 300 | 0.058 | 0.0016 | ±0.0032 | ≈65 |

再乘一个惩罚：**重叠标签**。h=5、rebalance=5 时 IC 序列强自相关，有效独立样本 ≈ `T/h`，上表门槛要再乘 ~h。也就是：

- 检测 IC=0.02 实际需要 **≈5000 根 bar（约 20 年）**；
- 检测 IC=0.05 需要 **≈800 根 bar** —— 这解释了我们为什么只捞得到 basis carry（IC 量级大、经济逻辑硬）、捞不到 mom60 这类弱信号（隔离 OOS 仅 0.71%）。

**推论（重要）**：把截面从 20 扩到 80~100，统计功效提升 **5~10 倍**，相当于把现有 GP 的产出翻几倍；而把 GP 换成 AlphaGen 的 RL，在同一份 20×1277 数据上大概率学不出策略（RL 需要海量交互，且 reward 极稀疏——AlphaQCM 整篇论文就是在解决"多数 alpha 弱 → 零奖励"这件事）。

---

## 4. 差距矩阵与优先级

评级：★★★ = 收益/成本比高、且不破坏契约；★★ = 有价值但需先改文档；★ = 观望。

### 4.1 P0（先做，都是"补空间"而非"换算法"）

| # | 事项 | 为什么 | 落点 | 成本 |
|---|---|---|---|---|
| P0-1 | **算子扩表 19 → ~40**：`where_`、`ts_rank`、`ts_quantile`、`ts_argmax/argmin`、`ts_count`、`decay_linear`/`ema`/`wma`、`product`、`skew/kurt/ts_mad`、`slope/resi/rsquare`、`signed_power/power`、`group_rank/group_neutralize` | 见 §2.1：**引擎全都有，只是注册表没开**。缺 `where_` 等于没有条件逻辑，缺 `decay_linear` 等于不知道信息会衰减 | `registry/operators.json` + `qweave` 符号映射 + 加载期自检（E1015） | **低**（无新算法代码，纯注册 + 参数域 + 单测） |
| P0-2 | **模板枚举算法 `template_enum`**（Alpha158 子集 + 参数网格 + 配额截断） | 唯一能**保证覆盖率**的算法；也是 GP 的第二个对照系（现在只有随机基线一个）。qweave 已载入 Alpha158 全集，映射成本低 | 新增 `generate()`（无状态），`lineage` = 模板名+参数 | 低（半天级） |
| P0-3 | **适应度多目标化**：`rank_ic` + `icir` + 换手/成本惩罚，用**加权和 + Pareto 档案**（而非 NSGA-II 全量） | 单目标 abs_rank_ic 会系统性偏好"高波动、高换手"的表达式（波动大 → 相关性偶然高）；ICIR 是对此最便宜的修正 | `mining.py` GPConfig + `observe` 已算 `icir`，只需在 fit 里可取 | 低 |
| P0-4 | **池感知奖励（synergistic reward）**：fitness 从"单因子 IC"改为"加入当前已接受池后的**残差 IC 增量**" | 直接对应 AlphaGen 的核心创新，且正好治我们"组合收益全来自一个成员"的病；实现上只需在 fit 内维护一个 mine 窗口内的已接受池做截面正交化，**不越界**（不读 eval 窗口） | `mining.py` fit ctx + `inference.py` 正交化原语 | 中（需仔细定义"池"的接受规则与顺序无关性） |

> P0-4 的顺序无关性是个真问题：增量 IC 依赖池的构建顺序 → 同 seed 两次跑可能不一致 → 违反 AC39。解法是**按 fitness 降序 + 内容身份作 tie-break** 固定顺序，或干脆改成分批（每代重算池）——这一点必须先写进文档再动手。

### 4.2 P1（有价值，但要先扩数据或先改契约）

| # | 事项 | 前置条件 | 备注 |
|---|---|---|---|
| P1-1 | **行为级去重**：暴露相关性 > 阈值的候选只保留 fitness 最高的一个（聚类硬约束） | 需要定义"用哪个窗口算相关性"（只能是 mine 窗口） | ★★★ 这是性价比最高的"多样性"手段，比 GFlowNet 便宜两个数量级 |
| P1-2 | **MAP-Elites 质量-多样性档案**：按 (换手桶 × 波动桶 × 方向) 保留每格最优 | 无 | 比 GFlowNet 更简单、可复现、天然决定论；AlphaSAGE 的收益大部分可以用它近似 |
| P1-3 | **模拟退火 / 语法引导 GP（GE/CGP）** | 无 | 前者在 `random_expression` 加温度即成；后者省 evaluate 浪费（我们 evaluate 有缓存，收益打折） |
| P1-4 | **扩截面到 80~100 永续 + 多交易所** | 采集链路（方案 B 已就绪） | 统计功效 ×5~10，是所有算法改进的前提 |
| P1-5 | **LLM 假设生成器（离线）**：LLM 产出"经济假设 + 表达式"→ 人工/规则审核 → 写入 `prior_pool`（带 `hypothesis` 与 `prompt_hash` 进 lineage） | 需要 `prior_pool` 条目支持附加元字段 | 严格遵守：不进 Search、不参与 AC39 对拍、失败不静默入库 |
| P1-6 | **crypto 专属字段**：taker buy/sell ratio、long/short account ratio、liquidation、mark/index price、跨所基差 | 采集（ccxt 部分支持，历史受限） | 这是相对股票 A 股竞品的**差异化空间**——funding/basis 已被证明能出东西（basis carry OOS 18.8%），同族字段大概率还有 |

### 4.3 P2 / 延后："不做"是排期与成本裁决，不是"它们不对"

**先纠正本文档上一版的一处错误**：上一版写"RL 的策略评估天然边看边学，与 `fit_window=mine_only` 结构性冲突"——**这句是错的**。RL 完全可以把 reward 只在挖掘窗口内计算（reward = mine 窗口的 IC 或对池的增量贡献），推理链与 GP 的 fitness 同构，**不构成 eval 窗口泄漏**。真正的障碍在下面四条，且都不涉及"违反因果纪律"。

| 事项 | 真实裁决 | 真实理由（按权重） | 解锁条件（写死，避免拍脑袋复辟或永久封杀） |
|---|---|---|---|
| RL 序列生成（AlphaGen 族） | **延后（P2）**，不是禁止 | ① **AC39 逐位一致**：torch 在 GPU 上的非确定性算子（atomicAdd/reduction 顺序）会让"同 seed 两次跑候选集逐位一致"失效；要满足只能 CPU + 单线程 + `deterministic=True`，训练成本再乘数倍 ② **样本效率**：AlphaQCM 整篇论文就是在治"多数 alpha 弱→零奖励"，我们的 20×1277 比它的实验设定小两个量级 ③ **生产依赖**：torch/GPU 进链路会改变镜像体积、启动时间与 S5 单步重建成本 ④ **边际收益未证**：我们的 GP 在同一空间里已自主找出 basis carry，没有证据表明 RL 会更好 | 截面 ≥100 且 GP/模板枚举的 pass 率已饱和（produced 高但 pass≈0）；届时按四方法实现：`stateful=True`，`fit` 产 policy 权重落 `fitted_state`，`generate` 用 greedy/固定噪声采样以保证 AC39 |
| 神经网络端到端（AlphaNet / Transformer 直接出权重） | **不做（位置问题，不是技术否决）** | 产出不是表达式 → 无法内容寻址、无法逐位复算、Evidence/Verdict/血缘链全断。**但 NN 本身我们可以用**，只是不能放在 Search：正当位置是 **S7 `combine` 的组合模型**（学权重/非线性合成）与**离线蒸馏**（NN 特征 → 表达式 → `prior_pool`） | 无（架构性约束，除非放弃"候选是可审计表达式"这个前提） |
| GFlowNet | 观望 | 收益是多样性；而多样性可用 P1-1（行为去重）+ P1-2（MAP-Elites 档案）以 1% 成本拿到，且天然决定论 | 若 P1-1/P1-2 上线后仍观察到"语法多样但行为同质"持续存在 |
| NSGA-II 全量多目标 | 观望 | 预算摊薄到前沿两端；P0-3 的加权和 + Pareto 档案已能回答"收益-换手"的取舍 | Verdict 数据积累出真实前沿形状后 |
| 跨市场迁移学习 | 观望 | 玩具规模（20 标的 × 1277 bar，预训练无意义） | P1-4 扩截面 + 多年历史到位 |

**一句话**：这五项里没有一项是"业界证明无效"，全部是"**在我们的数据规模与可复现性契约下，投入产出比暂不成立**"。写成带解锁条件的延后，比写成"明确不做"更诚实，也更可执行。

### 4.4 业界到底用什么：论文、生产、我们的架构是三件事

用户问得对——**业界当然不只 GP**。但把"论文热度"直接当成"生产现状"会误判。三层要分开看：

| 层 | 主流做法 | 证据 |
|---|---|---|
| **论文/前沿（2023-2026）** | RL（AlphaGen/Alpha²/RiskMiner/AlphaQCM）、LLM 代理（Alpha-GPT/QuantAgent/AlphaAgent/RD-Agent/CogAlpha/TreEvo）、MCTS（Alpha Jungle）、GFlowNet（AlphaSAGE）、生成式网络（AlphaForge）。RL 与 LLM 已是两大主线，GP 反而被当成 baseline | 见 §1 与 §7；华泰金工 2026-01 综述直接把"LLM 驱动的因子语义进化"列为下一代核心范式 |
| **工业生产（真正在跑钱的）** | ① **表达式模板 + 大规模并行枚举筛选**（WorldQuant BRAIN 的本质就是一套表达式语言 + 海量模拟打分）② 机器学习做**预测与组合**（GBDT/NN 吃几百个表达式因子，学非线性权重），不是用 NN 替代因子表达式 ③ 风险模型/正交化（Barra 式）与容量约束 ④ GP 作为"自动补候选"的常规工具 | 这一点和 §1 的"论文"层有系统性偏差：生产要的是可解释、可归因、可风控、能过合规，黑箱权重过不了这三关 |
| **我们的架构（V5）** | 明确站在**生产层**：候选必须是可内容寻址的表达式，才能有八指纹/逐位复算/DSR-FDR/成本闸/OOS 认证。同时用 **两个扩展点**（挖掘算法 + 内置因子清单）给研究层留门 | `02-mining-interface.md` 四方法抽象；`20-mining-algorithms.md` §3.2 |

所以"只做 GP"这个印象，准确说法是：**我们把 GP 当作第一版的生产级搜索器，把 RL/LLM 当作可插拔的候选生成器——LLM 已经以 `prior_pool` 的形式进来了（P1-5），RL 留在扩展点上等数据规模。**

顺带一个反直觉的结论：**论文里那些 IC 提升，多数来自"搜索空间更宽"而不是"搜索器更聪明"**。Alpha² 的增益有一半来自量纲分析（剪掉无经济意义的表达式，等价于缩小空间）；AlphaAgent 的增益来自三重正则（等价于约束空间）；AlphaSAGE 的增益来自多样性（等价于改变采样分布）。**这三件事在我们的架构里都能用远比"引入一个新搜索器"更便宜的方式实现**（P0-1 扩表、P1-1 行为去重、P0-4 池感知）。


---

## 5. 必须写清的不可比性

论文里的 IC 0.07~0.10 是在 CSI300/500（N=300~800、10 年+、日频、无资金费成本）上测的；我们是 N=20、1277 bar、含 taker 费 + 资金费。**任何跨数据集比较都没有意义**。业界独立审计也反复指出论文数字与可复现结果存在显著差距（前视、样本内调参、成本缺失是三大来源）——这恰好是我们相比竞品的真实优势所在（八指纹、ASOF、append-only、DSR/FDR/成本闸、OOS 认证）。

所以正确姿态不是"追上他们的 IC"，而是"**在同样的诚实度下，把搜索空间和统计功效做到能持续出货**"。

---

## 6. 建议执行顺序（每步都走 §6 六步验收）

1. **P0-1 算子扩表**（~40 算子）→ 重跑同一计划，对比 `generators[].produced` 与 pass 数变化。
2. **P0-2 `template_enum`** → 建立"覆盖率基线"；GP vs 模板 vs 随机三方对照。
3. **P0-3 适应度加 ICIR**（先只加诊断，不改选择压力）→ 看 pass 集是否变化。
4. **P1-1 行为级去重**（mine 窗口相关性）→ 直接检验"高相关因子堆"是否消失。
5. **P0-4 池感知奖励**（需先改文档：池的接受规则、顺序无关性）→ 检验组合归因是否从"一个成员扛全部"变成"多成员分摊"。
6. **P1-4 扩截面** → 数据工程，与前五项正交，可并行。

前 4 步都不改契约、不动编排层，只是"注册表 + 算法实现"；第 5 步要改 `02-mining-interface.md` 的 fit 语义（必须先改文档）。

---

## 7. 参考

- Yu et al., *Generating Synergistic Formulaic Alpha Collections via Reinforcement Learning* (AlphaGen), KDD 2023.
- Xu et al., *Alpha²*, 2024（代码生成 + 量纲分析）。
- Zhu & Zhu, *AlphaQCM: Alpha Discovery with Distributional Reinforcement Learning*, 2025.
- Shi et al., *AlphaForge: A Framework to Mine and Dynamically Combine Formulaic Alpha Factors*, AAAI 2024 / arXiv:2406.18394.
- Chen et al., *AlphaSAGE*（GFlowNet 多样性采样），2025。
- Tang et al., *AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration to Counteract Alpha Decay*, arXiv:2502.16789.
- Ren et al., *TreEvo*；Liu et al., *CogAlpha*；Shi et al., *Alpha Jungle*（LLM + MCTS）——见华泰金工《AI 量化的当下与未来》2026-01 综述。
- *A survey on LLM-based alpha mining*, FITEE 2025, doi:10.1631/FITEE.2500386.
- Kakushadze, *101 Formulaic Alphas*, 2016；qlib Alpha158。
