# 模块审查 21 · `v5/capability/ops/allocate.py`

> 审查日期：2026-11-18
> 依据：源码 577 行全文通读；依赖 `stats.py`（rebalance_freeze）、`params.py`（implementation_defaults for l2）、cvxpy（约束投影）；设计 `s0/04-capability.md` §3.9、`06-evaluation.md` §5/§5.6（L226）、`14-test-strategy.md`；测试 `test_v5_s4_allocate_backtest.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Allocate 是全系统**唯一**的持仓构造函数（06 §5）。三条硬边界：
1. **口径只有一个来源**：quantiles/long_short/weighting/max_weight/net_exposure/turnover_max/min_members 全部从 `policy_view.allocate` 读；`params` 里只允许构造选择 `top_k`。口径写进 params 会在编译期 E1003 挡下（`:5-8`）。
2. **权重 t 只依赖 t 及以前**：分位/排名用 score[t] 截面；`inv_vol` 用价格收益 `[0..t]` 的 Welford 滚动波动（绝不用未来 bar）；换手约束只看 w[t−1]。任何一行不读 t+1。
3. **宽度不足不"有多少算多少"**：可交易成员 < min_members 的时点整行置 0 并记 `dropped_bars`，不用少数标的拼出看起来便宜的组合。

另有**再平衡网格冻结**（06 §5.6）：目标权重只在 `t ≡ 0 (mod rebalance)` 重算，中间行冻结沿用（与 observe 同一网格）；冻结发生在换手约束之前（约束作用在真实持仓序列的逐 bar 增量上）。约束投影顺序：按净敞口拆多/空组 → weighting 分配 → max_weight capped water-filling → turnover_max 缩放增量；三个约束真正触发才记 `binding_constraints`。

**加密特化**：24/7 无日历，rebalance 是 bar 数（C12）；流动性由 tiers 决定可交易性（C11）；高波动下 max_weight×min_members>1 的不可满足约束必须 E1013（C10 不得放松）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` 调用 `run(call)` **实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_rest.py`（系统临时目录）。

### 场景
12 标的 × 8 bar；score 为随机 Finance 暴露；tradable 全 True、tiers 全 0；口径 `quantiles=4, long_short=True, weighting=equal, max_weight=0.4, net_exposure=0, turnover_max=1.0, min_members=2`。

### 输入
```
capability="allocate" impl="quantile"
policy_view={"allocate":{...}, "execution":{"rebalance":1|5}}
inputs=(score(T,N), tradable_mask(bool), tiers(int), [price 可选])
```

### 必查要点逐项实测输出

**分位路径 + rebalance 真冻结**——真实打印 `turnover_series`：
```
rebalance=1: [0.5 1. 1. 1. 0.75 1. 1. 1. 0.75 1. 1. 1.]
rebalance=5: [0.5 0. 0. 0. 0. 0.75 0. 0. 0. 0. 0.5 0.]   ← 非网格行换手为 0
rebalance=5 时 weights[1] == weights[0] ? True
```
即非网格行持仓完全沿用（B3 同网格冻结的另一半，与 observe.py:239 一致）。代码：`raw_weights = rebalance_freeze(raw_weights, dropped_row, rebalance)`（`:523`），冻结在换手约束之前（`:520-522` 注释）。

**宽度不足 dropped_bars**——真实输出：
```
dropped_bars = {'count': 0, 'by_reason': {'insufficient_members':0,'no_tradable':0,'width_below_min':0},
                'fallback': {'inv_vol_history_short':0}}
binding_constraints = []
```
三个 by_reason 互斥且之和=count（`:568-572`，`14-test-strategy` 验收线）；不可交易 tiers<0 的成员被 `eligible` 排除（`:470`）。

**约束投影路径（两路径齐备）**——`mode="constraint"` 真实调用成功：
```
binding = ['max_weight','net_exposure']   weights_ref = sha256:12f89...
```
cvxpy `cp.Problem` 一次 canonicalize、逐 bar 只换 `cp.Parameter`（`:297-336`），solver=CLARABEL，失败即 E3003（`:337-340`）。约束不可同时满足 `max_weight*min_members>1` → E1013（`:289-291` / `:426-432`）。

**口径护栏（源码逐条核对）**：`policy_view` 缺 allocate 子块 E1008（`:40-44`）；口径字段类型/范围错 E1004（`:61-121`）；`inv_vol` 缺 price E3003（`:444-450`）；rebalance 非法 E1013（`:396-407`）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| A-1 | **约束路径 `mode`/`constraints` 未被 plan_schema 暴露**：代码注释明说"当前 plan_schema 尚未暴露这两个键，CLI 仍只走分位路径；先把实现与回归守卫落地，待契约补键即可启用"（`:275-277`）。约束投影实现已可用（本次直接调用成功），但真实计划走不到。 | P1（可达性） | `allocate.py:272-277,413-418`；`impls.py:70-78`（只注册 quantile） | **需确认意图**：本轮"生产可用=能真挖因子"，分位路径已够；约束路径是扩展储备。补 plan_schema 属 U1/U4 契约变更，非本模块单独可补。 |
| A-2 | **`inv_vol` 历史不足两根回退等权**：不报错但在 `dropped_bars.fallback.inv_vol_history_short` 留痕（`:505-511,573-574`）。回退行仍持仓，不算丢弃。 | P2（已确认设计） | `:505-511` | **已确认符合设计**：数据缺口不触发斩仓，与 observe 网格语义一致；留痕保证可归因。 |
| A-3 | 网格行成员不足时：`rebalance>1` 持仓不动（`:530-534`）；`rebalance=1` 才硬清仓（`:535-538`）。两种语义并存，靠 rebalance 值区分。 | P2（说明缺口） | `:529-538` | **已确认符合设计**（06 §5.6 L226 明写）；建议文档强调"rebalance=1 的清仓是旧硬语义"，防误读为新口径。 |
| A-4 | `l2_regularisation` 从 `params.json.defaults` 读（`:261-269`），非口径字段；G14 已归位。 | —（正面） | `:262-269` | 属已修项，无差距。 |

**多了什么**：`_waterfill`、`_inv_vol_history` 都是必要实现，无冗余。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| cvxpy + CLARABEL 约束投影 | **采纳（约束路径）** | `allocate.py:282,306-336`；一次 canonicalize、逐 bar 换 Parameter；`07-components.md:16` 要求复用成熟 QP 求解器 |
| capped water-filling（renorm→clip→renorm 迭代） | **采纳（分位路径）** | `:195-225`；max_weight 触顶记 binding |
| Welford 增量滚动波动（inv_vol） | 采纳 | `:228-258`；只用 `[0..t]`，PIT 不读未来 |
| PyPortfolioOpt | **拒绝** | 历史 scipy 1.18 崩溃（plan U9 D3）；本模块用 cvxpy 自管 |
| 自写撮合循环 | **拒绝** | 持仓构造 ≠ 成交模拟；撮合归 backtest_engine（ml4t） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **口径唯一来源**：`_caliber` 只读 policy_view.allocate（`:37-49`），缺块 E1008、非映射 E1004。
2. **两条路径齐备**：`mode=constraint` → `_build_constrained`（`:272-381`，实测成功）；否则 quantile（`:411-576`，实测成功）；非法 mode E1004（`:416-418`）。
3. **rebalance 真冻结**：`rebalance_freeze`（`:523`），实测 weights[1]==weights[0]、非网格换手 0（同网格与 observe 一致）。
4. **冻结在换手约束之前**（`:520-522`）：约束作用在真实持仓增量上，才是"每 rebalance 根换一次仓"的换手。
5. **PIT 不读未来**：`inv_vol` 用 Welford `[0..t]`（`:228-258`）；换手只看 prev（`:539-546`）。
6. **宽度不足整行置 0**：`min_members` 拒（`:472-476,323-327`）；dropped_bars 三类互斥之和=count（`:565-575`）。
7. **约束不可满足 E1013**：`max_weight*min_members>1`（`:426-432`）；净敞口/权重>1 直接拒。
8. **权重只读数组**：`setflags(write=False)`（`:554-557`），防下游改权重。
9. **成员顺序内容决定**：分位用 `np.argsort(kind="stable")`（`:478`），同分按列序，可复现。

**发现的问题**：A-1（约束路径未进 plan_schema，P1 可达性，需 U1/U4 补契约）；A-2/A-3 已确认设计。**无 P0 级代码 bug**。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `06-evaluation.md:226`（rebalance 同网格冻结） | 与实现一致，已明确"allocate 与 observe 同一网格" | 无需更新 |
| `s0/04-capability.md` §3.9（分位构造） | 与实现一致 | 无需更新 |
| `s0/04-capability.md` §3.9 / `14-test-strategy.md` | 约束路径已实现但未进 plan_schema（A-1） | 补一句"cvxpy 约束投影已实现，待 plan_schema 补 `mode`/`constraints` 键后可由 CLI 选择" |
| `06-evaluation.md:226` | rebalance=1 硬清仓 vs rebalance>1 持仓不动 | 明确"rebalance=1 的清仓是旧硬语义"，防误读 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 分位建仓（默认） | ✅ 已支持 | top_k 构造选择 + 4 种 weighting（equal/liquidity/inv_vol）+ 三约束 |
| 约束投影（cvxpy） | ⚠️ 实现已就绪但 CLI 不可达 | A-1：需 plan_schema 补 mode/constraints 键（U1/U4） |
| 新增 weighting（如 min_var） | ❌ 需改代码 | `_WEIGHTINGS`（`:32`）白名单 + `_raw`（`:491-501`）加分支 |
| 新增约束（如 tracking error） | ❌ 需改代码+契约 | cvxpy 约束在 `:304-309`，加约束需同步 plan_schema |
| 加密流动性 tiers 分层 | ✅ 已支持 | `eligible = tradable & tiers>=0 & isfinite(score)`（`:470`） |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不算年化）
- **C10 判据不可放松**：✅ `max_weight*min_members>1` E1013（`:426`），约束不可满足直接拒
- **C11 流动性分层**：✅ `tiers>=0` 过滤 + tradable 掩码决定可交易成员（`:470`）
- **C12 1 观测=1 天、rebalance 以 bar 计**：✅ `t % rebalance` 按 bar 网格（`:468`）
- **C6 上市/下架**：✅ 经上游 tradable_mask/tiers 传递；本模块不自行判 alive

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（分位路径真跑在用）
- **R2 结论可信**：✅ 持仓路径 PIT、约束触发可查（binding_constraints）、权重只读
- **R3 因子零代码扩展**：✅ 正向（新因子只需 score 输入）
- **R5 单因子可归因**：✅ 权重与绑定约束落盘，组合收益可拆到成员
- **R7 fail-closed**：✅ 强支撑——口径缺失/非法、约束不可满足、inv_vol 缺 price、宽度不足全拒或记账

---

## 结论

**本模块两条路径（分位/约束投影）齐备，rebalance 真冻结经真实调用确认（非网格行换手为 0、weights 沿用），无 P0 级代码 bug，可确认通过。** 唯一 P1 是 A-1：约束投影实现已就绪（实测调用成功）但 plan_schema 未暴露 `mode`/`constraints`，CLI 走不到——属契约扩展（U1/U4），非本模块单独可补；本轮"真挖因子"用分位路径已够。

**请确认本模块审查结果**：
- ✅ 确认通过（A-1 并入 U1/U4 契约扩展清单） → 我进入第 5 个模块 `combine.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想当场裁决 A-1（是否本轮补 plan_schema 键） → 告诉我
