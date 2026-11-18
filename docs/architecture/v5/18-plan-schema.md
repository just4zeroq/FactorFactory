# 18 · 计划 schema：键全集与校验分级

> 本篇是 `v5/contracts/plan_schema.json` 的**可读渲染**。那份 JSON 是唯一真相，本篇解释它为什么这样切。
> 在此之前，「计划的契约」实际上只是 README 里的一段 YAML 示例。示例不是契约：它没有类型、没有必填、没有「哪些键不存在」，因此**照抄示例的代码和照抄规则的代码会得到两个系统**。

---

## 1. 为什么必须把这件事单独成篇

S0 的验收里有五条（S0-1 ~ S0-5）全部是「配置写错了会怎样」：

| 验收 | 坏输入 | 期望 |
|---|---|---|
| S0-1 | `target.horizn` | `E1001` + 退出码 2 |
| S0-2 | `search.algorithms[0].name = gp_symbolc` | `E1002` + 退出码 2 |
| S0-3 | 给 `gp_symbolic` 传 `max_dept` | `E1003` + 退出码 2 |
| S0-4 | `population: "many"` | `E1004` + 退出码 2 |
| S0-5 | 命令行改 `target.horizon` | `E1005` + 退出码 2 |

这五条要能写出来，前提是**存在一份可以说「这个键不在这里」的键全集**。没有它，校验器只能校验「我认识的键」，拼错的键一路通过——这正是 V4 的形态：配置写错不报错，只是结果不一样。

### 1.1 已经真实发生的三次漂移

写这一篇之前，文档里已经有三个「示例与规则不一致」的实例，而且都不是笔误：

| 漂移 | 表现 | 为什么没被发现 |
|---|---|---|
| `funding_period: 8h` 留在示例与字段索引里 | I34 明令 `caliber` 不得含资金费周期，但 `06` 的示例和字段表都写着它 | **规则在 A 篇、示例在 B 篇**，没有任何检查同时读两篇 |
| `min_cross_section: 20` 只出现在基线 | 与 `label.min_width`、`allocate.min_members` 表达同一件事，全系统再无第二处提到它 | 它只在一个示例里出现过一次，没人需要读它第二遍 |
| `gross_exposure` / `allow_leverage` vs `max_leverage` | 杠杆上限有三个名字，示例用第三个、参数表写前两个 | 三处都是「看起来合理」的写法 |

结论：**示例必须被机器校验，否则它一定会漂移**。漂移的代价不是文档难看，是实现照着示例写、规则就失效了。

---

## 2. 三个文件，一份真相

| 文件 | 提供什么 | 谁维护 | 变了会怎样 |
|---|---|---|---|
| `v5/contracts/plan_schema.json` | **结构**：有哪些键、什么类型、哪些取值合法、哪些跨字段规则 | 能力层 | 改它是一次契约变更：所有配置文件、所有校验器、所有文档示例都要重跑 |
| `baseline.json`（`baseline@vN`） | **取值**：每个键的默认值、每个工位的默认实现 | 随实现发布，显式升版 | 升版必须显式发生，产生新的 `policy_digest`，旧产物不再命中缓存 |
| `plan.yaml` | **差异**：只写与基线不同的项 | 研究者 | 由 `plan_digest` 锁定；已声明的构成性参数改动即新试验族 |

三条关系：

1. **基线与计划同构**。两边都是同一份 schema 的实例，`caliber` 的结构、类型、枚举完全相同。结构不同的话，「这个值来自基线还是计划」就要读代码回答，`flow explain` 的逐项来源表（`s0/03` §5.2）无法机械化。
2. **计划可以省略任何非必填键，但不能省略必填键**。省略 = 用基线的值并在来源表里标 `baseline@vN`；缺必填 = `E1008`，不补默认。
3. **覆盖层只接受操作性参数**。构成性参数出现在 `--set` 里 → `E1005`（除非显式放行，且强制新试验族，`E1009`）。

---

## 3. 顶层键

十三个键，其中 `portfolio` 与 `certify` 可缺省（研究阶段不需要认证）。**「构成性」列决定它是否进 `policy_digest`**。

| 键 | 必填 | 构成性 | 内容 | 相关拒绝 |
|---|---|---|---|---|
| `plan` | 是 | 否 | 计划名，只用于人读与日志 | `E1008` 缺 |
| `baseline` | 是 | 是（间接） | 基线版本名，必须已发布 | `E1001` 未发布 |
| `supply` | 是 | 是 | 交易所、数据集、标的集名、频率 | `E1014` 数据集无对应供给 |
| `caliber` | 是 | **是** | 四子块口径，见 §4 | `E1008` / `E1004` |
| `boundary` | 是 | 是 | 实现、参数、三段 `time_axis` | `E1008` 区间重叠 |
| `panel` | 是 | 是 | 实现与参数 | — |
| `target` | 是 | 是 | 实现名（跨度与变换不在此处） | `E1020` 写了 `horizon` |
| `search` | 是 | 是 | 算法、先验池、试验族、种子 | `E1002` / `E1003` / `E1010` |
| `evidence` | 是 | 是 | 实现与参数 | — |
| `verdict` | 是 | 是 | 唯一裁判与三个阈值 | `E1006` 严格模式未注入 |
| `portfolio` | 否 | 是 | 合成器、正交/相关约束（`orthogonalise` / `corr_max`，可缺省）与三段窗口 | `E1018` / `E1019` / `E1021` / `E1008` |
| `certify` | 否 | 是 | 认证实现与折数 | — |
| `operations` | 否 | **否** | 缓存、并发、日志级别 | 改它**不得**改变任何指纹 |

`operations` 是唯一非构成性块。它是「怎么跑完」，不是「在回答什么问题」——把它放进 `policy_digest`，会让「改日志级别导致全部产物失效」，那等于逼人不敢开 debug。

**`supply.symbols` 是名字，不是成员清单。** 成员在编译期由标的集解析出来并固化进 `Boundary` 产物。把 60 个 symbol 内联进计划有两个后果：一是每次调仓都要改配置文件，二是不存在「同一份计划、同一份成员」这个可比对的对象。

---

## 4. `caliber`：四个子块

**四个子块的划分依据是「谁读它」**，不是「它们都跟成本有关」：

| 子块 | 谁读 | 为什么必须在一起 |
|---|---|---|
| `execution` | `Target` 与 `Evidence` 同时读 | 标签用 `horizon` 算前瞻、成本用 `rebalance` 算换手。两者分开写，就会出现「标签按 5 bar、换手按 1 bar」 |
| `label` | `Target` 只读 | 预测目标的定义 |
| `allocate` | `Portfolio` 与单因子评估同时读 | 成员数不同是允许的差异，权重约束必须相同 |
| `net` | 判据与回测同时读 | 判据用的成本必须就是回测用的成本（C9 / I12） |

### 4.1 `execution`

| 键 | 类型 | 合法取值 | 基线默认 | 说明 |
|---|---|---|---|---|
| `entry` | enum | `next_open` / `next_close` | `next_close` | **不接受 `current_close`**：权重由收盘价算出、又按同一根 bar 收盘成交，是前视。这个组合在 V4 的 12/13 份配置里存在过 |
| `rebalance` | int ≥ 1 | — | 5 | 单位是 bar |
| `annualisation` | enum | 仅 `calendar_365` | `calendar_365` | 加密 24/7。`calendar_252` 在校验期即拒绝，不是「允许但警告」——警告会被忽略 |
| `funding` | enum | 仅 `per_contract_events` | `per_contract_events` | 只按事件求和（I34）。结算周期是数据属性：实测同一合约在 1h/4h/8h 之间变过 |

### 4.2 `label`

| 键 | 类型 | 合法取值 | 基线默认 | 说明 |
|---|---|---|---|---|
| `horizon` | int ≥ 1 | — | 5 | 前瞻 bar 数；跨保留区边界 → 拒绝 |
| `transform` | enum | `cs_rank` / `cs_zscore` / `cs_rank_zscore` | `cs_rank` | 三个取值的精确公式在 `06` §4，**升序、`method="average"`** |
| `neutralise` | list | 空 / `beta` / `log_dollar_volume` | `[]` | 逐时点回归取残差 |
| `neutralise_window` | int ≥ 2 | — | 无（条件键） | `neutralise` 非空时必填，窗口 `[t-w, t]` |
| `overlap_policy` | enum | `independent_sample` / `nw_adjusted` | `nw_adjusted` | `rebalance < horizon` 时必填（C22） |
| `min_width` | int ≥ 2 | — | 20 | **截面**有效成员下限；不满足即该时点标不可用 |
| `width_policy` | enum | `keep` / `drop` | `drop` | `drop` 产出逐时点有效掩码，由后续工位统一消费 |

`min_width` 与 `allocate.min_members` 不是同一件事，这是本节最容易搞错的地方：

- `min_width` 是**统计量是否成立**的问题。截面只有 3 个成员时，`cs_rank` 的秩均值没有意义。
- `min_members` 是**能不能建仓**的问题。截面有 40 个成员，但其中 35 个流动性不足，可交易成员只有 5 个，分 5 组每组 1 个。
- 前者决定「这个时点进不进样本」，后者决定「这个时点能不能下单」。合成一个阈值，就会在「统计有效但无法交易」的时点上给出一个看起来正常的权重。

### 4.3 `allocate`

| 键 | 类型 | 合法取值 | 基线默认 | 说明 |
|---|---|---|---|---|
| `quantiles` | int ≥ 2 | — | 5 | 分位组数 |
| `long_short` | bool | — | `true` | 多空或仅多 |
| `weighting` | enum | `equal` / `liquidity` / `inv_vol` | `equal` | 组内加权 |
| `max_weight` | float (0,1] | — | 0.10 | 单成员上限 |
| `net_exposure` | float [-1,1] | — | 0.0 | 净暴露目标 |
| `turnover_max` | float > 0 | — | 0.50 | 超限即拦截（fitness=0），不是惩罚项 |
| `min_members` | int ≥ 2 | — | 8 | 可交易成员下限 |

`turnover_max` 是**硬拦截**不是惩罚项：把它写成惩罚，搜索会学会「用一点换手换很多 IC」，而成本是真金白银。

### 4.4 `net`

| 键 | 类型 | 合法取值 | 基线默认 | 说明 |
|---|---|---|---|---|
| `cost_tiers` | list[int ≥ 0] | 严格升序 | `[40, 100, 250]` | bp，**单边**；三档全部要报，不能只报中档 |
| `slippage` | enum | `none` / `fixed` / `tiered` | `tiered` | — |
| `slippage_bp` | float ≥ 0 · **可选** | 单边 bp | 缺省（无） | 显式滑点。缺省时按 `slippage` 的派生规则：`fixed` = 最低档一半、`tiered` = 逐档一半 |
| `fee_model` | enum | `taker` / `maker` / `taker_with_funding` | `taker_with_funding` | 资金费按事件，不按周期 |
| `max_leverage` | float ≥ 1 | — | 1.0 | **本研究的硬上限**，不是场所能力 |
| `initial_margin` | float (0,1] | — | 0.10 | 场所属性，只用于强平模拟 |
| `maintenance_margin` | float > 0 | — | 0.005 | 必须 < `initial_margin`（X1） |
| `liquidation_buffer` | float ≥ 0 | — | 0.01 | 距强平价的缓冲比例 |
| `initial_cash` | float > 0 · **可选** | — | 1e6（实现缺省） | 回测初始权益，只用于把成交额折成权重；与 `BacktestConfig` 同源。属**实现缺省**而非口径默认：显式写入会进 `caliber_digest`，不写则不进（G3） |

### 4.5 已删除的四个键

| 键 | 删除理由 | 加回来会怎样 |
|---|---|---|
| `funding_period` | 结算周期是数据属性，同一合约会变。写死 `8h` 会把 1h 合约的资金费算成 1/8 | 与 I34 冲突；`caliber` 里出现同名键即 `E1001` |
| `min_cross_section` | 与 `min_width` 同义。两个阈值并存，实际生效哪个要靠读实现 | 见 §4.2：统计有效性归 `label`、可交易性归 `allocate` |
| `gross_exposure` | 它是每期权重的**结果**，不是口径。写进口径等于「先定 gross 再构造」，把构造自由度反过来当成约束 | 构造器会为了满足 gross 而扭曲权重 |
| `allow_leverage` | 与 `max_leverage > 1` 表达同一件事。两个开关必然出现「理论上关了、实际开着」 | 两个开关的四种组合里只有两种有意义 |

### 4.6 保证金：为什么先做单值

分档保证金表（按持仓名义额分档）**不纳入 v1**。理由不是「以后再说」，而是当前拿不到可核对的档位数据：20 资产面板里没有交易所档位表，用估值填进去比用保守单值更危险——它会给出一个精确的、错误的强平价。

扩展点留在 `net` 子块。加键属于新试验族，因为它改变强平模拟。

---

## 5. 工位块的通用形状

七个工位块（`boundary` / `panel` / `target` / `evidence` / `portfolio` / `certify` / `search` 里的每个算法）形状相同：

```yaml
<station>:
  impl: <名字>        # 必须已在能力目录注册（E1002）
  params: {...}       # 该实现自己的参数，由它的 param_schema 校验（E1003 / E1004）
```

`params` 是本 schema 里的**开放集**：本篇不列它的键，因为每个实现的参数表归 `11` 的注册表。本层只保证两件事：

1. 它是个对象（不是列表、不是标量）；
2. 它里面**不含** `caliber` 已定义的任何字段名。

第二件事就是 C20，也是本 schema 里唯一一条「跨块」的禁用规则：

| 禁止出现在 `*.params` 里的键 | 应写在哪 |
|---|---|
| `horizon` / `transform` / `neutralise` / `min_width` / `width_policy` | `caliber.label` |
| `rebalance` / `entry` / `annualisation` / `funding` | `caliber.execution` |
| `max_weight` / `quantiles` / `min_members` / `net_exposure` / `turnover_max` | `caliber.allocate` |
| `cost_tiers` / `slippage` / `fee_model` / `max_leverage` | `caliber.net` |

违反 → `E1020`，错误信息必须指出**应写在哪一块**（「不许写在这里」没有用，人要的是下一步动作）。

---

## 6. 错误分级：七种「写错了」，七个码

区分依据是**错误的层次**，不是「严重程度」。层次决定谁该修——注意七个码的退出码全是 `2`：它们都属于「用户输入不合法」，差别只在「改哪一处」。`E1020` 单独列出来，是因为它的修法不是改值，而是**把键搬个位置**。

| 现象 | 码 | 退出码 | 谁来修 |
|---|---|---|---|
| 顶层键或子键拼错 / 不存在 | `E1001` | 2 | 写配置的人（附最近似合法路径） |
| 实现名未注册 | `E1002` | 2 | 写配置的人，或需要先加实现 |
| 实现参数里有多余键 | `E1003` | 2 | 写配置的人（多余键不得静默忽略） |
| 类型 / 范围 / 枚举不符 | `E1004` | 2 | 写配置的人 |
| 命令行改构成性参数 | `E1005` | 2 | 想绕过编译期的人 |
| 必填缺失 / 未解析占位 | `E1008` | 2 | 写配置的人 |
| 口径字段写进工位 `params` | `E1020` | 2 | 写配置的人（提示应写在哪一块） |

两条容易混的边界，必须写死：

- **`E1001` 与 `E1003` 的分界**：`E1001` 是**结构**不认识这个键（`target.horizn`、`caliber.execution.funding_period`）；`E1003` 是**结构认识、但这个实现不认这个参数**（给 `gp_symbolic` 传 `max_dept`）。前者靠 schema 判，后者靠实现的 `param_schema` 判。
- **`E1004` 与 `E1008` 的分界**：给了值但值不合法（`population: "many"`）是 `E1004`；没给且没有默认（`verdict.params.dsr_min: null`）是 `E1008`。**没有默认值就是没有默认值，不许在实现里写一个「宽松兜底」**。

---

## 7. 跨字段规则

单键校验管不了这些，必须单独列，否则它们会以「实现里的 if」形式散落：

| # | 规则 | 码 | 为什么不能靠单键 |
|---|---|---|---|
| X1 | `maintenance_margin < initial_margin` | `E1004` | 两个键各自合法，组合起来是「维持保证金高于开仓保证金」，强平永远不触发 |
| X2 | `cost_tiers` 严格升序、无重复 | `E1004` | 三档成本是「同一个策略在三种费率下」，乱序会让「最严档」被当成「最松档」 |
| X3 | `max_weight * min_members <= 1` | `E1004` | 单成员上限 0.6 配 3 个成员：约束无解，求解器要么报不可行要么悄悄放松一个 |
| X4 | `rebalance < horizon` 时 `overlap_policy` 必填 | `E1008` | 重叠标签当独立观测会把 t 值放大约 `sqrt(horizon/rebalance)` 倍 |
| X5 | `neutralise` 非空时 `neutralise_window` 必填 | `E1008` | 窗口没定义，「逐时点回归」四个字没有确定含义 |
| X6 | `portfolio.window.fit.end + embargo <= eval.start` | `E1021` | 独立看三个字段都合法，连起来是「用评估区数据拟合组合权重」 |
| X7 | `mine` 早于 `oos`，`oos` 与 `holdout` 不重叠 | `E1008` | 区间是字符串，语义要靠比较 |

---

## 8. 不变量与验收

> 编号与陈述以 `05-contracts.md` §1（不变量）与 §4（验收）为准；本表只补本篇的检查点，不重复定义。

| 编号 | 内容 | 检查点 |
|---|---|---|
| I58 | 计划的结构只有一份真相；文档示例必须能被它校验 | 删除 schema 里的任一键 → 校验规范样例失败；把 `funding_period` 写回任一篇文档的 YAML → 文档扫描测试失败 |
| **AC59** | 规范样例通过严格校验 | `examples/plan_cs_2026q4.yaml` 对 `plan_schema.json` 零错误，且每个必填键都在 |
| **AC60** | 已删键确实被拒 | 把 `caliber.execution.funding_period`、`caliber.label.min_cross_section` 分别注入样例 → `E1001`，提示含「数据属性」/「用 min_width」 |
| **AC61** | 文档不手抄 | 扫描 v5 全部 Markdown 的 YAML 块：出现的每个 `caliber.*` 键都必须存在于 schema；`*.params` 里出现口径键 → 失败 |
| **AC62** | 跨字段规则有反例 | X1–X7 各一条构造用例，断言码与退出码 |

---

## 9. 反模式

1. **在 `params` 里加一个「方便」的 `horizon`**：多一个入口，就多一份口径，然后判据与回测用不同的跨度。C20 会拒，但更该做的是不设计这个入口。
2. **给必填项配一个「合理默认」**：`verdict.params.dsr_min = 0.95` 看起来很合理，实际是把「没做决定」伪装成「做了决定」。基线里必须是 `null`。
3. **允许未知键并打警告**：警告不会有人看。拼错的键被忽略，等于该配置项不存在，而人会以为它生效了。
4. **在文档里再抄一份键表**：必然漂移，本篇 §1.1 就是三次实证。文档只描述结构与理由，键表由 schema 渲染。
5. **用枚举外的字符串表达「关闭」**：`slippage: none` 是枚举值，`slippage: false` 是类型错误。前者可枚举、后者会引出「`0` 算不算 false」。

---

## 10. 改这个 schema 的流程

1. 改 `v5/contracts/plan_schema.json`；
2. 跑 `tests/test_plan_schema.py`，它会同时校验规范样例、扫描全部文档 YAML、核对 `05` 的编号；
3. 若新增键：判断是否构成性，构成性则注意这会让所有历史 `policy_digest` 失效——**加键等于新试验族**，不是「加个默认值」；
4. 若删键：在 §4.5 记一行「为什么删」，否则它会被下一个觉得不方便的人加回来；
5. 更新本篇的表格（测试会强制这一步，因为文档扫描与 schema 是双向核对的）。
