# 模块审查 19 · `capability/ops/observe.py`

> 审查日期：2026-11-18
> 依据：源码 860 行全文通读；依赖 `inference.py`（行内跨资产置换/HAC/DSR）、`label.py`（build_raw_by_h）、`spec.py`（method_id）；设计 `s0/04-capability.md` §3.6、`06-evaluation.md` §5.6/§9、`10-crypto-fit.md`；测试 `test_v5_observe.py`（38 用例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Observe 是证据层（Evidence）核心，回答："净成本前，这个候选在样本内有没有可信的统计证据？"只产观测、不产结论（无 pass/significant 词，E2004）。四件事：
1. **Stage A 廉价粗筛**：覆盖率/换手/|IC| 三项 prefilter；被筛掉的候选**不进零分布、不报"已检验"**（p_value 置 None → verdict 给 watch，不冒充 reject）。
2. **Stage B 零分布精算**：只对幸存者算置换零分布与 empirical_p；零分布必须保留截面结构（行内跨资产置换 `cross_section_permute`），不能用整行时间轴置换——后者保留资产漂移，实测中位 |t|=4.74 > 观测 3.46，把强因子判死（V4 头号 bug）。
3. **量纲与对齐**：多空序列 `ls_t = Σ_s w_t·raw_{t,s}`，第二操作数必须是**变换前**收益 `raw`；权重来自暴露自身的截面排序。标签 `entry=next_close` 的成交延迟已编在 `raw_t=p[t+1+h]/p[t+1]−1` 里，同索引取样，**不能再 +1**（重复延迟）。
4. **指标带 method_id + 同源统计**：DSR 的 sr/skew/kurtosis 必须来自同一条多空序列；HAC t 的自由度取 IC 时点数（T−1）而非面板格子数。

**加密特化**：一年 `365/h` 个独立持有期（h 根 bar 跨度收益、每 bar 采样、重叠窗口），年化乘 `√(365/h)` 而非 `√365`；24/7 无日历；判重默认关闭（corr=1.0，既有计划逐位不变），开启是新试验族。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` 调用 `capability.run(call)` **实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_observe.py`（系统临时目录）。

### 场景
8 标的 × 60 根 1d bar；暴露 `values=tanh(cumsum(噪声))`，标签 `score=tanh(噪声×0.5)`（进 IC）、`raw=噪声×0.01`（变换前收益，进多空）。刻意让 score ≠ raw 以暴露 B1。`null_model=within_bar_cross_asset`、`n_permutations=8`、`seed=7`。

### 输入（核心）
```
capability="observe" impl="two_stage_funnel"
inputs=(candidate_set{candidates:[c1],exposures[c1]={values,valid_mask,values_ref}},
        panel(8×60), label{score, raw, valid_mask, effective_n, horizon_bars})
policy_view={"execution":{"rebalance":1|5}}
```

### 必查要点逐项实测输出

**B1（`raw` 只进多空、score 只进 IC）**——输出 `gross_return=0.002867282758267519`；手工 `Σw·raw(同根)=0.002867282758267519`（**完全一致**）；若误用 `Σw·score(同根)=-0.034284676569734676`（量纲混拼）。结论：B1 修复成立，代码 `:258` `ls=Σ(weights*label_raw)`、`:687-688` 把 raw 传给 `_longshort`、`:679` 把 score 只传给 `ic_series`。

**B2（`ls_t=Σw_t·raw_t`、同根无多延迟）**——手工 `Σw·raw(t+1 延迟)=0.0034258230194532586` ≠ 输出值；同根值 = 输出值。代码 `:256-258` 注释明确"成交延迟已由标签 entry offset 表达，同索引 PIT，**不带额外 +1 移位**"。

**B3（`rebalance` 与 allocate 同网格冻结）**——`rebalance=1` 累计换手 `18.0`，`rebalance=5` 累计换手 `6.0`（5 倍持有显著降低换手）。代码 `_longshort:239-240` 用 `t % period` 冻结、allocate.py:468 用 `t % rebalance` 冻结，同语义；`observe.py:636-652` 从 `policy_view.execution.rebalance` 读（与 allocate 读同一口径）。

**B4（bridge 传 `horizon_bars`、年化 √(365/h)）**——`h=1`：`gross_sharpe=4.775449, gross_return=0.00286728`；`h=5`：`gross_sharpe=2.135646, gross_return=0.00057346`。实测比值 `g5/g1=0.447214` 恰为 `1/√5`、`r5/r1=0.2` 恰为 `1/5`。bridge `capability_bridge.py:1259` 真实传 `horizon_bars`；注释 `:1250-1258` 记录历史事故（桥曾漏传，observe 默认 1，√h 虚高且不报错）。年化实现 `observe.py:265` `annualised = sr_per_period * sqrt(365/h)`。

**缺 raw E3003**——真实调用删掉 `label.raw` → 返回 `E3003`（`observe.py:626-633` fail-closed，"静默退回 score 会把量纲 bug 重新藏起来"）；缺 `horizon_bars` 同样 `E3003`（`:658-666`）。

其余真实输出：`skew`/`kurtosis` 来自同一条 ls（`:691`）；Stage A 未过候选 `hac_t/p_value/sr=None` + `stage_a_pass=False`；判重默认关闭时 `n_effective=n_candidates`。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| O-1 | **B1/B2/B3/B4 四项历史事故全部确认已修并实测通过**：raw 隔离、同根无延迟、同网格冻结、bridge 传 horizon_bars + √(365/h) + 缺 raw E3003。 | —（正面结论） | 本次真实调用（见上）；源码 `:258,239,265,626,658`；bridge `:1259` | **已确认闭合**，非差距。 |
| O-2 | **S3 成本是占位（est_cost=0）**：`cost_adj_return = gross_return - turnover*0`（`:814-815`），净成本归 S4 同源 allocate+backtest。这是诚实的阶段边界，但 Stage A 的成本闸在 S3 名义存在、实际按毛收益放行。 | P1（阶段边界，需 S4 兜底确认） | `observe.py:810-824`；`06-evaluation.md` §5.3 | **需确认意图**：本模块不该自算净成本（两套成交逻辑=两套口径）。真正的净成本闸在 certify 逐折回测（已核 bridge `_run_certify_folds`），S3 毛口径仅作筛选，不进最终结论——可接受。 |
| O-3 | **`time_shift` 零分布保留在枚举里但标 deprecated + biased 警告**：缺省是 `within_bar_cross_asset`；若计划显式选 time_shift，产物会带 `null_biased=true`。 | P2（需护栏确认） | `observe.py:63-72,853-854` | **已确认符合设计**：诊断/对拍用途保留，warn 而非硬拒，靠产物标记防误用。 |
| O-4 | 判重阈值 `corr` 默认 1.0（关闭）；开启需在计划显式写 `dedup`，属新试验族。阈值本身不做跨加密池自适应。 | P2 | `observe.py:75-77,362-393` | **已确认符合设计**：默认逐位不变 old plans；自适应阈值会引入隐性口径变更。 |

**多了什么**：无明显冗余。`_BLOCK_MIN=4`、`_DEDUP_OFF=1.0` 等常量都有明确语义。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 行内跨资产置换 `cross_section_permute` | **采纳（缺省）** | `observe.py:335-337`；保留截面结构，避免整行置换保留资产漂移的 V4 头号 bug |
| 整行时间轴置换 `time_shift` | **拒绝为判据（保留诊断）** | `:338-341`，method_id 标 `whole_row_time_permutation@deprecated`，产物 `null_biased=true` |
| 移动块自助（IC 序列中心化后重采样） | **采纳（备选）** | `:342-357`；先中心化（原假设 IC 均值为 0），否则强因子 empirical_p 逼近 0.5（review P1-2） |
| Newey-West HAC t + p（df=T−1） | 采纳 | `:681,719`；自由度取 IC 时点数而非格子数（review P1-3） |
| 逐日 groupby / 固定 `periods=(1,5)` 股票 IC | **拒绝** | 加密重叠窗口 + √(365/h) 年化（C1/C12），`s0/04` 与 `06-evaluation.md` 明确 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **raw/score 分离**：ls 用 raw（`:258,687-688`），IC 用 score（`:679`），注释 `:195-204` 讲清量纲与对齐。
2. **同根无 +1**：`:256-259`；实测 B2 一致。
3. **再平衡同网格**：`t % period` 冻结、网格行算不出则持仓不动（`:239-249`），与 allocate.py:468 同语义；实测 B3 换手 18→6。
4. **√(365/h) 年化**：`:265`；实测 B4 比值精确。
5. **缺 raw / 缺 horizon_bars 均 E3003**：`:626-666`；实测两路径。
6. **DSR 输入同源**：sr/skew/kurt 来自同一条 ls（`:691` 与 `:768` 同源），自由度用 n_periods（`:769`）。
7. **p_value 0.0 不被 `or 1.0` 吞**：在 verdict 侧（verdict.py:250-264）处理；observe 侧 stage_a 未过才置 None（`:744-745`）。
8. **stage_a 不进 BH 族**：verdict.py:258-265 只读 stage_a_pass 的候选；observe 给未过的 p=None（`:745`）。
9. **判重只在 stage_a 幸存者间算**（`:841-843`），`n_unobserved` 与"关掉判重"分开落盘（`:850`）。
10. **`gross_return` 必须是基座**：`:749-754` 注释记录"首跑踩坑：编排层 `metrics.get('gross_return')` 拿到 None 静默跳过替换，成本闸永远停在 est_cost=0"——已修为必需统计量（`:776-779`）。

**发现的问题**：O-2（S3 成本占位，需靠 S4/certify 兜底）、O-3/O-4（已确认设计）。**无 P0/P1 级代码 bug**；四项历史事故（B1–B4）经真实调用全部确认闭合。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `06-evaluation.md:228`（年化/每 bar 收益口径） | 与实现一致：`gross_sharpe=sr(ls)·√(365/h)`、`gross_return=mean(ls)/h` | 无需更新 |
| `06-evaluation.md:226`（rebalance 同网格冻结） | 与 `:239-249` 一致 | 无需更新 |
| `s0/04-capability.md:407`（group_stats 不冻结网格） | 与 `_group_stats:291-293` 一致 | 无需更新 |
| `s0/04-capability.md` §3.6（S3 成本占位） | 已声明净成本归 S4 | 补一句"S3 gross_return 是成本闸基座、属必需统计量"，固化 O-2 的修复 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增加密流因子（funding/OI/basis） | ✅ 零代码 | 暴露由 evaluate 产出，observe 只读；新字段进 Panel 即可 |
| 新增零分布模型（如 stationary bootstrap） | ❌ 需改代码 | `_METHODS['null']` 与 `_null_tstats`（`:328-359`）加分支；属口径变更 |
| 判重阈值按加密池宽度自适应 | ❌ 需改代码 | `corr` 是显式参数（`:370`），自适应会引入隐性口径变更 |
| 新增指标（如 PBO/CSC） | ⚠️ 半配置 | 需在 `_METHODS` 登记 method_id + `metrics_req` 请求 + build 里算；不新增工位 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ `√(365/h)`（`:265`），`CALENDAR_DAYS_PER_YEAR` 统一从 timeaxis 读（`:43,74`）✅
- **C2 资金费按事件**：不适用（observe 只估换手，不含资金费；资金费在 S4 backtest）✅
- **C7 24/7 无日历**：✅ 不靠日历，h 是 bar 数
- **C9 只 ffill 不 bfill**：不适用（observe 不填充）
- **C11 流动性分层**：⚠️ observe 的 `_longshort` 用 `valid & label_valid` 掩码，可交易性由上游 tradable/tiers 经 label 掩码传递；observe 自身不读 tiers（设计如此）
- **C12 1 观测=1 天、embargo 以观测计**：✅ h 是 bar 数，年化按 `365/h`

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（真跑中 observe 已产出证据）
- **R2 结论可信**：✅ 强支撑——四项历史事故闭合 + 零分布保留截面结构 + HAC df 正确
- **R3 因子零代码扩展**：✅ 中性偏正（只读暴露，新字段直接可用）
- **R5 单因子可归因**：✅ group_returns/ic_decay 等诊断把形状摊开，单因子可解释
- **R7 fail-closed**：✅ 强支撑——缺 raw/horizon_bars/组数/衰减跨度全拒

---

## 结论

**本模块核心修复（B1 raw 隔离、B2 同根无延迟、B3 同网格冻结、B4 bridge 传 horizon_bars + √(365/h)、缺 raw E3003）经真实调用逐项确认闭合，无 P0/P1 级代码 bug，可确认通过。** 唯一需留意的是 O-2：S3 `est_cost=0` 是阶段边界，真正的净成本闸靠 S4/certify 逐折回测兜底（已核 bridge 逐折传 funding 并回填 net_return），不是本模块缺陷。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 3 个模块 `verdict.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
