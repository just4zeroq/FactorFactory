# 06 · 横截面评估与组合构建口径

> 本篇回答一个此前悬空的问题：**一个候选「净成本后可交易」到底是怎么算出来的。**
> 在此之前，「成本、入场价、跨度、年化一致」只是四项参数的比对；本篇把它升级为一条**唯一的、单因子与组合共用**的构造链。

---

## 1. 为什么必须单独定义这一层

同一个因子，用不同构造方式评估，净收益可以差好几倍：

| 构造差异 | 影响 |
|---|---|
| 多空对冲 vs 纯多 | 去掉市场 beta 后收益与波动同时下降，净夏普可能升也可能降 |
| 等权 vs 按流动性加权 | 大池子里权重集中在头部标的，与「几百个标的一起排序」的统计假设不一致 |
| 分位组合 vs 固定前 K 名 | K 固定时有效截面变化会改变实际分位，同一因子在不同时期含义不同 |
| 每根 bar 换手 vs 每 h 根换手 | 换手差 h 倍，成本差 h 倍，结论方向可能翻转 |
| 重叠标签当独立观测 | 标准误被低估，显著性虚高 |

**这五件事此前一件都没有定义**。于是「同一口径」这句话在单因子这一层是空的：裁判判可交易时用的净收益，与组合层回测出的净收益，可能根本不是同一个量。

本篇的做法不是补一段说明，而是让**构造与成本只有一个来源**，任何工位都不能自带一份。

---

## 2. 一条链，五个环节

```
暴露 (T,N,K)  →  截面变换  →  合成分数 (T,N)  →  构造持仓  →  净收益
   候选/域        标签口径         组合域           allocate        net
```

每个环节只有一个权威参数来源，且**单因子与组合走同一条链**：

| 环节 | 单因子评估（第 5 步） | 组合评估（第 7 步） | 是否共用同一实现 |
|---|---|---|---|
| 暴露 | 一个候选的 `(T,N)` 暴露 | 若干成员的合成分数 `(T,N)` | 是（形态相同） |
| 截面变换 | `caliber.label` | `caliber.label` | 是 |
| 领域 | 单成员域 | 冻结的组合域 | 否（数据不同，非口径不同） |
| 构造 | `caliber.allocate` | `caliber.allocate` | **是，同一实现同一参数** |
| 净收益 | `caliber.net` | `caliber.net` | **是，同一实现同一参数** |

> **判据**：单因子评估与组合评估之间，允许的差异只有「成员数」与「成员身份」。**实现名、版本、参数、成本档位、入场价、年化基准逐项相同**，否则就是 I12 被违反。

---

## 3. 口径块：`caliber`

`caliber` 是冻结方案里的一个**顶层块**，不是任何工位的私有参数。四个子块：

```yaml
caliber:
  execution:                          # 标签与成本共用的执行口径
    entry: next_close
    rebalance: 5
    annualisation: calendar_365       # 加密只允许 365
    funding: per_contract_events      # 按事件求和；周期是数据属性，不是口径（I34）
  label:
    horizon: 5
    transform: cs_rank_zscore
    neutralise: [beta, log_dollar_volume]
    neutralise_window: 60
    overlap_policy: independent_sample   # 或 nw_adjusted
    min_width: 40
    width_policy: drop
  allocate:
    quantiles: 5
    long_short: true
    weighting: liquidity
    max_weight: 0.05
    net_exposure: 0.0
    turnover_max: 0.35
    min_members: 8
  net:
    cost_tiers: [20, 60, 150]
    slippage: tiered
    fee_model: taker_with_funding
    max_leverage: 1.0                 # 本研究的上限，不是场所能力
    initial_margin: 0.10              # 场所属性，只用于强平模拟
    maintenance_margin: 0.005
    liquidation_buffer: 0.01
```

三条结构规则：

1. **工位参数里不得出现 `caliber` 已定义的字段**。`target.params.horizon`、`portfolio.params.max_weight`、`net.params.entry` 这类写法一律编译失败（C20）。原因很简单：一旦允许工位自带一份，就会有第二份，然后两份会不一致。
2. **`caliber` 整体进口径锚点**。`caliber_digest` 是 `policy_digest` 的组成部分；改任何一项即新实验、新试验族、全部下游失效。
3. **`entry` / `rebalance` / `horizon` 是「外界参数，但不是旋钮」**：它们写在计划 YAML 的 `caliber` 块里（省略时取基线 `v1` 默认），`flow explain` 必须逐项指出来源；但它们**不得**写进工位 `params`（C20 / `E1020`），也**不得**进命令行 `--set`（C5 / `E1005`，除非显式放行并强制新试验族）。这三者中改动任何一个 = 新计划 + 新试验族 + 旧产物全部失效，不存在「只是试试」。
4. **实现名也在口径里**。换一个构造实现（例如从分位法换到优化法）不是「试试」，是新实验——它的数值含义完全不同。

`execution` 子块被 `label` 与 `net` **同时读取**，这正是此前 `entry` 两处各写一遍、`rebalance` 与再平衡周期易错位的根治点。`rebalance` 的执行语义（§5.6）：**再平衡周期**——`allocate` / `observe` / 回测链的目标权重只在 `t ≡ 0 (mod rebalance)` 重算，中间冻结；`label` 用它判重叠（`rebalance < horizon` → 必须 `overlap_policy`）。三处读同一个值，不允各自解释。

---

## 4. 截面变换（`label`）

| 参数 | 取值 | 约束 |
|---|---|---|
| `horizon` | 正整数根 bar | 必须 ≤ 评估窗口允许的最大前瞻；跨越保留区边界拒绝 |
| `transform` | `cs_rank` / `cs_zscore` / `cs_rank_zscore` | 逐时点截面，**不得使用全期统计** |
| `neutralise` | 空集 / `beta` / `log_dollar_volume` / 组合 | 逐时点回归取残差，回归只用该时点及之前窗口 |
| `neutralise_window` | 正整数 bar | `neutralise` 非空时必填；回归窗口为 `[t - neutralise_window, t]`，未声明即 `E1008` |
| `overlap_policy` | `independent_sample` / `nw_adjusted` | 见 §7 |
| `min_width` | 正整数 | 截面有效成员下限，低于则该时点标记不可用 |
| `width_policy` | `keep` / `drop` | `drop` 时产出逐时点有效掩码，由后续工位统一消费 |

三条硬约束：

- **变换只用当根 bar 的截面**。`cs_zscore` 的均值与标准差来自该时点截面，不是全期。这是「全期统计」类前视最常见的入口。
- **缺失不填充**。截面内按成员是否存在计算；不存在不是零，是缺席。缺席成员不参与排序，也不参与权重。
- **宽度不足不是零信号**。`min_width` 不满足时，该时点的截面统计不是同一个量，必须显式标记而不是照常输出。

三个 `transform` 取值必须给出**唯一**定义，否则「同一名字两种算法」会在对拍时才暴露（V4 的 `_ts_rank` 方向与 `rank(pct)` 相反，就是这类）：

| 取值 | 定义（全部逐时点截面，`x` 为该时点成员取值） | 边界 |
|---|---|---|
| `cs_rank` | `u = rank(x, pct=True, method="average")`；**升序**：`x` 越大 `u` 越大 | 并列取平均秩，**禁止 `method="first"`**——它会把常数向量排成字母序噪声 |
| `cs_zscore` | `(x - mean_cs(x)) / std_cs(x, ddof=1)` | `std_cs = 0` → 该时点标不可用（并入 `valid_bar_mask`），**不得除、不得补 0** |
| `cs_rank_zscore` | 先 `u = cs_rank(x)`，再 `(u - mean_cs(u)) / std_cs(u, ddof=1)` | 同一 `std=0` 规则；这是有界的 `cs_zscore`，对厚尾稳健 |

方向必须与 IC 的符号约定一致：`IC = corr(score, forward_return)`，因此**分数越大必须预测收益越高**。任何让「高分组收益更低」的实现都是在把符号藏进代码，`Verdict` 看到的 t 值方向会与组合层的持仓方向相反，两边都不报错。

`neutralise` 同理要写清右端项与窗口：`beta` 用 `[t - neutralise_window, t]` 的标的收益对等权市场收益回归，`log_dollar_volume` 用当根 bar 的 `log(close * volume)`；窗口长度是**口径字段**（`caliber.label.neutralise_window`，正整数 bar），`neutralise` 非空而未声明 → `E1008`。残差只用该时点截面，**不得全期拟一次系数**。

### 4.1 谁决定丢弃宽度不足的时点

**`Target` 工位，且只按预注册规则。** 它产出 `valid_bar_mask`，由 `width_policy` 与 `min_width` 唯一决定；`Evidence`、`Portfolio`、`Certify` 一律按该掩码过滤，**不得自行判定**。

| 做法 | 后果 |
|---|---|
| Target 产出掩码，下游统一消费 | 全链用同一批时点，丢弃数量可审计 |
| 各工位自己判 `N(t) < 阈值就跳过` | 同一份证据在不同工位基于不同时点集合；丢弃数量无人知道 |
| 不标记、照常算 | 用 20 个成员的截面统计冒充 150 个成员的统计 |

`valid_bar_mask` 的丢弃计数必须作为观测写入料包：**丢弃了多少、丢在哪一段**，否则「研究窗口被静默缩短」无法被发现。

**硬上限 20%（I61 / A27）**：`width_policy=drop` 丢弃的时点占比超过 20% 即 `E3003`（退出码 1），不得以「丢一点继续跑」降级为成功；`quality_report` 必须记录丢弃数与所在区间。低于上限也要记录，因为同一份报告要能回答「研究窗口有没有被悄悄改短」。

---

## 5. 构造持仓（`allocate`）

这是全系统**唯一的持仓构造函数**，被三个地方调用：单因子评估、组合评估、认证复算。

```
allocate(score, tradable_mask, tiers, caliber) -> Position
```

| 输入 | 说明 |
|---|---|
| `score` | `(T,N)` 逐时点分数：单因子时是一个候选的暴露，组合时是合成分数 |
| `tradable_mask` | 逐时可交易名单；权重只能落在其中 |
| `tiers` | 逐时点流动性分层，用于成本档与可选的权重上限 |
| `caliber.allocate` | 构造参数，只读 |

| 输出 | 说明 |
|---|---|
| `weights` | `(T,N)` 稀疏权重 |
| `turnover_series` | 逐时点双边换手，**成本的唯一输入** |
| `effective_members` | 逐时点实际持仓数 |
| `binding_constraints` | 哪些约束真正起作用（上限 / 净敞口 / 换手 / 最小成员） |
| `dropped_bars` | 因不可行或宽度不足而未持仓的时点数及原因码 |

### 5.1 分位法的两种含义必须选一种

| 形式 | 定义 | 适用 |
|---|---|---|
| `quantiles: q` | 逐时点按分数分 q 组，取最高组做多、最低组做空 | 成员数随时间变化时，分位含义稳定 |
| `top_k: k` | 固定取前 k 名 | 成员数稳定时更直观；成员数变化时实际分位漂移 |

**同一份方案里只能出现一种**，且是构成性参数。同时出现两种写法、或让 k 随 N(t) 浮动，都属于运行期自由度。

`min_members` 必须显式：当某时点的可交易成员少于该下限时，`allocate` **不构造持仓**（记 `dropped_bars`），不得「有多少算多少」——用 3 个标的算出来的组合权重没有统计意义，且它在成本上看起来还很便宜。

### 5.2 权重方式

| `weighting` | 定义 | 注意 |
|---|---|---|
| `equal` | 组内等权 | 大池子里权重被头部少数标的稀释 |
| `liquidity` | 按 trailing 成交额加权 | 权重与流动性同向，容量更好；但会把因子暴露混入流动性暴露 |
| `inv_vol` | 按 trailing **资产收益**波动倒数加权 | 降低高波动标的支配；需要 `price` 输入与足够长的 trailing 窗口，缺价格即 `E3003` |

三种都是封闭白名单，能选不能加（`plan_schema.json` 的 `caliber.allocate.weighting` 为准）。`weighting` 与 `neutralise` 的交互必须显式：若已在标签层做过流动性中性化，又用流动性加权，等于**中性化被抵消两次**，这一点在报告里必须能看出来（`binding_constraints` 与构造参数并列呈现）。

**编译期告警、不拒绝（A28）**：这种组合可能是有意设计，所以不判编译失败；但告警必须进入报告。静默抵消一旦发生，报告读者无从知道标签层的中性化已经失效。

### 5.3 成本只认换手

```
cost(t) = turnover(t) × (commission(tier) + slippage(tier))
```

三条后果：

- **不得按「持仓名义额」计费**。不换手的仓位不产生交易成本；按名义额计费会系统性高估成本、把可交易信号判死。
- **不得按固定 bar 数摊成本**。`rebalance` 与 `horizon` 不等时，换手节奏不同，成本也不同。
- **滑点必须能从口径里读出来**。`caliber.net.slippage` 的枚举只决定**派生方式**（`fixed` = 最低档手续费的一半、`tiered` = 逐档一半），那是一个假设，不是数据事实——真实滑点取决于下单方式与盘口深度，与 taker 费率没有因果关系。要写具体数值就用 `caliber.net.slippage_bp`（单边 bp，可选）；它一旦给出就覆盖派生规则（`slippage: none` 仍为 0）。缺省的派生值仍进 `caliber_digest`，但**说不出口径里没有的那部分**。
- **成本档位按 `tiers` 逐格取**，不是全期一个常数。分层成本的意义在于承认「小标的更贵」，而不是给全池一个平均折扣。

### 5.4 年化：基准是 365 天，频率是另一个量

```
sharpe = mean(net_return) / std(net_return) × sqrt(bars_per_year)
bars_per_year = 365 天 ÷ 中位 bar 步长          # 1d → 365，1h → 8760，4h → 2190
```

两个量必须分开记，不能混成一个「年化」：

- `annualisation` 只用 `calendar_365`——加密 24/7 无休市，用 252 会把 365 天的波动低估约 17%。这一项是**口径**，写进 `caliber.execution`。
- `bars_per_year` 由**数据频率**决定，不是口径，不能写进 `caliber`。同一份 `calendar_365` 口径下，1h 与 1d 供给的每年 bar 数差 24 倍。

**把频率写死成 365 是错的口径**：小时线策略的年报 Sharpe 会被低估 sqrt(24) ≈ 4.9 倍。`backtest` 因此接受 `time` 输入（UTC 纳秒，与 `Panel.times` 同源）并用中位步长推导 `bars_per_year`，返回值里用 `bars_per_year_source` 标明来源为 `time_axis_median` 还是 `assumed_daily`。非等距时间轴取中位步长：缺口是数据缺陷，不该改写标称频率。

### 5.5 换手：累计量与每期量不是一回事

`0.5 · Σ_t |w_t − w_{t-1}|` 是**整段**累计换手（1270 根 bar 上会到 7~8），而 `prefilter.max_turnover`、`caliber.allocate.turnover_max` 都是**每期**上限。拿累计值比每期上限，任何真实候选都会被判超限——闸门形同恒假。

`observe` 因此同时给两个字段：`turnover`（累计，供报告与成本归因对账）与 `turnover_per_period = turnover / n_periods`（判据）。Stage A 与 `turnover_max` 只认后者。字段名不带单位是历史包袱，**读的人必须知道 `turnover` 不是每期量**。

### 5.6 成本闸的时间单位与对齐（2026-11-17 闭合）

`cost_adj_return` 的定义是「候选自身多空组合的**每 bar 等效毛收益** − **每 bar 净成本**」。三个历史缺口本轮全部闭合，实现与文档自此一致：

1. **量纲**：`gross_return` 必须是**收益**单位。`observe` 的多空序列 `ls_t = Σ_s w_{t,s} · raw_{t,s}` 用**中性化后、变换前**的标签收益 `raw`（`label` 新增同形数据通道），不用 `cs_rank` / `cs_zscore` 变换后的 `score`——后者单位是秩/标准差，拿它减收益单位的成本是量纲混拼。变换后的 `score` 只进 IC（rank IC 语义不变）。
2. **对齐**：`ls_t = Σ w_t · label_t`（**不带**额外的 +1 移位）。`entry=next_close` 的成交延迟已编在标签里（`label_t = p[t+1+h]/p[t+1] − 1`）：权重行 `t` 在 bar `t+1` 成交（回测链 `NEXT_BAR`），恰好吃满 `label_t` 的窗口。此前的 `w_t · label_{t+1}` 把延迟算了两次。
3. **rebalance 生效**：`caliber.execution.rebalance` 是**再平衡周期**——目标权重只在 `t ≡ 0 (mod rebalance)` 的时点重算，中间行冻结沿用。网格行算不出（成员不足）= 今天不调仓，**持仓不动**（数据缺口不触发斩仓；`dropped_bars` 仍如实记账）。仅 `rebalance=1`（逐 bar）保持「丢弃行清仓」的旧硬语义。`allocate` 与 `observe` 同一网格冻结，保证 S3 估计与 S4 回测是同一个持仓过程；回测链里权重不变 → `NEXT_BAR` 只在网格点成交，`rebalance=5` 才真的是 5 根 bar 持有。

年化沿用已修口径：`gross_sharpe = sr(ls) · sqrt(365 / h)`、`gross_return = mean(ls) / h`（每 bar 等效）。判据两侧（S3 估计与 S4 回测）单位、对齐、持仓周期逐项同构——AC63 的独立复算按本节口径复刻。

### 5.7 零晋升

`passed=0` 不是失败，也不是需要调门槛的信号：`Portfolio` 记 `skipped(no_candidate_passed)`、`Certify` 记 `upstream_skipped`、退出码 0，报告给出「最强候选 vs 门槛」的量级差距。这条口径与 AC52 一致——把零晋升写成 `E3xxx` 会让「判据有效」永远表现为「系统坏了」。

---

## 6. 多币种横截面的五件特有事项

这五件事在单标的时序研究里不存在，在横截面多币种里全都必须显式处理。

| 事项 | 规则 | 不处理的后果 |
|---|---|---|
| **资金费** | 按真实结算周期（通常 8h）累计，方向随持仓符号；逐 bar 归集时按 bar 内覆盖的结算次数计 | 按每 bar 收一次日费率，等于把费率放大或缩小数倍，多头策略的成本被系统性错算 |
| **下架 / 退市** | 最后一个可交易 bar 之后强制平仓，按该 bar 结算价成交并计一次成本；此后该标的从掩码中永久移除 | 用「今天仍挂牌」的名单回测历史，等于假装当年买了后来归零的合约还能持有 |
| **上市初期** | 前 `warmup` 根 bar 内不参与排序与持仓；`warmup` 由最长回看窗口决定，编译期可算 | 因子在只有 3 根 bar 历史的标的上取值为噪声，却被当成有效观测 |
| **合约类型** | 稳定币对、杠杆代币、交割合约、指数合约逐类排除，理由码逐格记录 | 稳定币对进池会把截面排序压成常数；交割合约的换月价格跳空被当成真实收益 |
| **时间基准** | UTC 日界、24/7 无休市、年化基准按日历日而非交易日 | 用 252 年化会把 365 天的波动低估约 17% |

`warmup` 与 `min_width` 都必须在编译期算出具体值并写入方案：它们决定「最早可评估时点」，会直接改变研究窗口的长度。

---

## 7. 重叠标签不能当独立观测

`horizon: 5` 意味着相邻 5 个时点的标签共享同一段未来价格。把它们当独立观测会低估标准误，显著性被系统性高估。

| `rebalance` 与 `horizon` 的关系 | 处理 |
|---|---|
| `rebalance ≥ horizon` | 标签基本不重叠，可直接当作独立观测 |
| `rebalance < horizon` | **必须**声明 `overlap_policy`，否则编译失败（C22） |

两种合法处理：

| 策略 | 做法 | 代价 |
|---|---|---|
| `independent_sample` | 按 `horizon` 间隔抽样评估，只用不重叠的时点 | 有效样本数下降为约 1/h |
| `nw_adjusted` | 保留全部时点，标准误用重叠稳健估计（截断阶数 ≥ horizon） | 需要显式声明阶数，且它本身是构成性参数 |

**禁止**：不做任何处理而直接用逐 bar 显著性。这是横截面研究里最常见、也最容易被忽略的一处虚假显著。

单因子评估与认证必须使用同一个 `overlap_policy`：否则组合层用重叠稳健标准误、认证层用朴素标准误，两边结论必然打架。

---

## 8. 组合的三段窗口

组合层此前只约束了拟合起点，没定义拟合终点与评估起点，这在字面上允许「全期拟权重、同段评估」——组合层自制样本内。

三段窗口，缺一不可：

| 窗口 | 用途 | 约束 |
|---|---|---|
| **fit** | 估合成分数权重 | 起点 ≥ 域内最晚 `available_from`；终点含在方案里 |
| **embargo** | 隔离标签重叠 | 长度 ≥ `horizon`，放在 fit 与 eval 之间 |
| **eval** | 计算组合净表现 | 起点 ≥ fit 终点 + embargo；终点 ≤ 搜索区间末端 |

三条编译期校验（C21）：

1. `fit.start < max(available_from)` → 拒绝：域里有成员在拟合起点时还不存在。
2. `fit.end + embargo > eval.start` → 拒绝：评估段看到了拟合段的重叠标签。
3. `eval.end` 超出搜索区间 → 拒绝：越界的样本外证据属于认证段，不属于组合层。

### 8.1 组合净表现是样本内诊断，不是结论

这一点必须写死，否则会重演「用探索段的表现发布可交易结论」：

| 产物 | 区间 | 性质 |
|---|---|---|
| Portfolio 的净表现 | 搜索区间内的 eval 段 | **构造诊断**：用来判断口径是否可行、约束是否过紧、换手是否过高 |
| 认证段的净表现 | 未参与搜索的新数据 | **结论**：唯一可发布的可交易性证据 |

因此 `PortfolioParcel` 里必须带一个显式字段标注其证据等级，且发布流程只接受认证段的结论。把组合层样本内表现直接当可交易结论，属于结论等级混用。

---

## 9. 诊断量与判据的分工

横截面研究有一堆看起来很像结论的数字。按用途分成两类，混用就会变成「挑一个好看的指标发布」。

| 量 | 类别 | 用途 |
|---|---|---|
| RankIC / ICIR | 诊断 | 排序能力是否稳定；不能单独作为可交易证据 |
| IC 衰减曲线 | 诊断 | 同一个因子在不同持有期上的 IC；`h` 的取舍靠它定。`observe` 落成 `ic_decay`（单位 bar），多跨度标签由 `label` 的同一份实现产出 |
| 分层单调性 | 诊断 | 分数与收益是否同向；用于发现构造错误。`observe` 落成 `group_returns` + `monotonicity`（`groups` 给出组数，无默认值） |
| 换手率 | 诊断 | 成本可行性的先行指标 |
| 有效持仓数 | 诊断 | 分散化是否真实存在 |
| 容量 | 诊断 | 规模可行性；不参与显著性或可交易判定 |
| **净成本后夏普 / 净收益** | **判据输入** | 与试验计数、多重检验校正一起进入唯一裁判 |
| **净成本后是否覆盖成本** | **判据** | 可交易性的直接判据 |

> **判据只有一个来源**：`caliber`。诊断量可以随便算、随便看，但改变判据必须走新实验。

这条分工还有一个副作用值得写下来：**RankIC 最高的方案，净收益经常不是最高的**。排序能力强不等于扣完成本后能赚，尤其在高换手构造下。把 RankIC 当结论，就等于用一个不扣成本的指标替代判据。

### 9.1 两处「去相关」不是同一件事

| | Evidence 判重（`dedup`） | Portfolio 去相关（`corr_max`） |
|---|---|---|
| 问的问题 | 这两个候选是不是**同一个信号**？ | 这两个成员**能不能同时持仓**？ |
| 数据段 | 全样本（`stage_a_pass` 的候选之间） | `portfolio.window.fit` 段 |
| 依据 | score 相关性 | `fit` 段 IC 加权的 `fit` 段相关性 |
| 后果 | 相关者降为 `watch` + `redundant`，不计入独立发现 | 相关者从组合剔除，记入 `dropped_members` |
| 缺省 | `corr = 1.0`（关闭） | `corr_max = 1.0`（关闭） |

两者独立生效，不互相替代：把 Portfolio 的 `corr_max` 调紧**不能**替代 Evidence 判重——100 个 `mom60` 变体会先完整走完 Evidence → Verdict、全部进 BH 族，直到组合阶段才被剪，而那时的 `n_trials` 已经把它们全算成独立试验了。反之亦然：Evidence 判重不解决"两个独立但同向的因子叠加后敞口翻倍"。

`redundancy.n_effective` 是诊断量，**不参与判据**（用它会降低 `expected_max_sr`，等于放宽门槛）。它与 `n_trials` 的比值才是这批候选雷同程度的度量。

---

## 10. 参数总表（构成性，一处定义）

| 子块 | 参数 | 影响 |
|---|---|---|
| `execution` | `entry`（`next_open`/`next_close`） / `rebalance`（int≥1） / `annualisation`（仅 `calendar_365`） / `funding`（仅 `per_contract_events`） | 标签与成本共用的执行假设 |
| `label` | `horizon` / `transform` / `neutralise` / `neutralise_window` / `overlap_policy` / `min_width` / `width_policy` | 预测目标与截面处理 |
| `allocate` | `quantiles` / `long_short` / `weighting`（`equal`/`liquidity`/`inv_vol`） / `max_weight` / `net_exposure` / `turnover_max` / `min_members` | 持仓构造 |
| `net` | `cost_tiers`（bp 单边，严格升序） / `slippage`（`none`/`fixed`/`tiered`） / `fee_model`（`taker`/`maker`/`taker_with_funding`） | 成本 |
| `net`（杠杆与保证金） | `max_leverage`（≥1，本研究上限） / `initial_margin` / `maintenance_margin` / `liquidation_buffer` | 杠杆与强平；被 `backtest` 引擎读取 |

**键全集、类型、取值范围以 `contracts/plan_schema.json` 为唯一真相，本篇只解释为什么这样切。** 上一版这里出现过三个已经删掉的键，记在一起免得再加回来：

| 已删的键 | 为什么删 |
|---|---|
| `funding_period` | 结算周期是**数据属性**：实测同一合约在 1h/4h/8h 之间变过，写死 `8h` 会把 1h 合约的成本算成 1/8。资金费只按事件求和（I34） |
| `min_cross_section` | 与 `min_width` 表达同一件事。两个阈值并存的结果是「哪个生效」要靠读实现。截面宽度归 `label.min_width`，可交易成员数归 `allocate.min_members`，两者语义不同、不可互替 |
| `gross_exposure` / `allow_leverage` | `gross_exposure` 是每期权重的**结果**不是口径，写进口径就变成「先定 gross 再构造」；`allow_leverage` 与 `max_leverage>1` 表达同一件事，两个开关必然不一致。只留 `max_leverage` |

**分层保证金表（按持仓名义额分档）暂不纳入。** v1 用单值 `initial_margin` / `maintenance_margin`：分档表需要真实交易所档位数据，而当前 20 资产面板里没有一张可核对的档位表；拿估值填进去，比用一个保守单值更危险。扩展点留在 `net` 子块，加键属于新试验族。

全部属于构成性：改任何一项即新试验族、旧产物失效、旧计数不复用。

**三段窗口不在 `caliber` 里，在组合域里。** 因为它们是「这一份域用哪段数据」的属性，不同域可以有不同窗口；而 `caliber` 是「整个研究用哪套口径」，所有域共用。两者都进 `policy_digest`，但归属不同：窗口随域冻结，口径随方案冻结。

`caliber_digest` 参与 `policy_digest`，因此它同时是三个地方的检查点：编译期（C9 / C20）、单步执行（口径锚点必须一致）、认证（必须复用同一判据）。

---

## 11. 验收

> 编号与陈述以 `05-contracts.md` §1（不变量）与 §4（验收）为准；本表只补本篇的检查点，不重复定义。

| # | 验收项 | 通过标准 |
|---|---|---|
| AC24 | 构造口径单一 | 在任一工位参数里写 `horizon` / `max_weight` / `entry` / `cost_tiers` / `max_leverage` → 编译失败并指出应写在 `caliber`；单因子评估与组合评估读到的 `allocate` / `backtest` 实现名、版本、参数逐项相同 |
| AC25 | 窗口合法性 | 把 fit 起点调到早于某成员可用时间 → 编译失败；让 eval 与 fit 重叠（去掉 embargo）→ 编译失败；把 eval 末端推到搜索区间之外 → 编译失败 |
| AC26 | 重叠声明 | `rebalance < horizon` 且未声明 `overlap_policy` → 编译失败；声明后报告中出现所用策略与截断阶数 |
| AC27 | 证据等级分离 | `PortfolioParcel` 带样本内标记；尝试用组合层样本内表现发布可交易结论 → 拒绝发布；认证段复算使用同一 `caliber_digest` |

---

## 12. 反模式

| 反模式 | 为什么危险 |
|---|---|
| 单因子用分位多空评估、组合用优化权重部署 | 判据与实现不是同一个策略，可交易结论无效 |
| 各工位自带成本参数并「保持一致」 | 两份参数迟早不一致，且不一致时无人发现 |
| 按持仓名义额计交易成本 | 不换手的仓位被重复计费，可交易信号被系统性判死 |
| 用全期截面统计做标准化 | 训练期用到未来分布，是最隐蔽的前视 |
| 宽度不足时「有多少算多少」 | 3 个标的的截面统计被当成 150 个标的的统计 |
| 重叠标签当独立观测 | 标准误低估，显著性虚高，0 晋升可能变成假晋升 |
| 用组合层样本内表现发布结论 | 探索段的拟合优度被当成可交易性 |
| 拿 RankIC 或分层单调性当结论 | 不扣成本的指标替代了判据 |
| 资金费按每 bar 收一次日费率 | 结算周期与持仓周期不符，成本量级错误 |
| 用「今天仍挂牌」的名单回测历史 | 幸存者偏差，且无法从产物里看出 |
