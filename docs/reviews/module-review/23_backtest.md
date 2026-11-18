# 模块审查 23 · `capability/ops/backtest.py`

> 审查日期：2026-11-18
> 依据：源码 493 行全文通读；依赖 `timeaxis.py`（CALENDAR_DAYS_PER_YEAR）；**注意本文件已不是生产实现**——`impls.py:71-75` 只注册 `backtest_engine.py` 的 `ledger@1`，本文件保留作"纯数值参照与单测夹具"（`backtest_engine.py:15-16`）。设计 `s0/04-capability.md` §3.11（L605–617）、`06-evaluation.md` §5.3/§5.4、`07-components.md` §3；测试 `test_v5_s4_allocate_backtest.py`、`test_v5_s4_backtest_ml4t.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
本文件定义"成本引擎"的**口径参照**：一条链、一处真相——权重 → 成交模拟 → Ledger（唯一真相）→ summary/full 两档同源投影（两档不做两次计算）。核心口径：
1. **成本只认换手**：`cost=turnover×(fee+slippage)`，逐时点逐档位；资金费按每 bar 费率逐 bar 求和，没有 funding_period（结算周期是数据属性，I34）。
2. **收益兑现滞后一根（NEXT_BAR）**：`net_return[t]` 用 `w[t−1]` 吃 `price[t]/price[t−1]−1`，t=0 恒 0。
3. **年化**：基准日历 365 天/年（calendar_365，加密 24/7），但每年 bar 数由数据频率决定（1d=365、1h=8760）——`bars_per_year` 从 time 的中位步长推导；缺 time 按 365 假设并标 `assumed_daily`。
4. **杠杆护栏**：向量化版不做保证金/强平模拟，总敞口 >1 直接 `E1004`，要求杠杆换 ml4t 生产实现。

**当前定位**：生产唯一实现是 `backtest_engine.py`（ml4t 撮合 + V5 成本口径），本文件是数值参照/夹具。审查它主要看**口径定义是否仍正确、是否被生产实现继承**。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` **直接调用** `capability.ops.backtest.build(call)`（本文件未注册，故不走 run）**实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_rest.py`（系统临时目录）。

### 场景
3 标的 × 6 bar；全多仓 `position=[0,0.3,0.3,0.3,0.3,0.3]`（gross=0.9）、price 常数 100/50/20、tiers=0/1/2；`net` 口径 `cost_tiers=[20,60,150], slippage=none, fee_model=taker`。

### 输入
```
capability="backtest" impl="vectorized"（直调 build）
params={"detail":"summary","include_funding":False}
inputs=(position(T,N), price(T,N), funding=None, tiers(T,N), [time(T,)])
policy_view={"net":{成本口径}}
```

### 必查要点逐项实测输出

**杠杆 >1 → E1004**——把 position[1] 改成 `[0.6,0.6,0.6]`（gross=1.8）→ 真实返回 `E1004`。代码 `:364-372`："向量化实现不支持总敞口 >1（无保证金/强平模拟），要用杠杆换 ml4t 实现"。

**annualisation_used 恒 calendar_365**——真实输出 `annualisation_used='calendar_365'`, `bars_per_year=365.0`（`:453`）。字段固定为 `calendar_365`，与 `bars_per_year` 分开记（C1）。

**bar 频率按中位步长**——传 1h time axis（每根 3.6e12 ns）→ 真实输出 `bars_per_year=8760.0, bars_per_year_source='time_axis_median'`。代码 `bars_per_year`（`:181-206`）用 `_NS_PER_YEAR/median(diff)`；非等距取中位（采集缺口不拉低年化基准）。

**missing_price_cells**——无缺价时输出 0（`:458-459` `~(isfinite(price)&price>0)` 的计数）；生产实现 `backtest_engine.py:405` 同名字段。

**NEXT_BAR + 成本同源**——`prev_w`（`:375`）= 上一根持仓，`gross_bar=(prev_w*ret).sum`（`:402`），`net_return=gross−fee−slip−fund`（`:403`），t=0 置 0（`:404`）；summary 六字段与 full 同名对象（`:442-492`），非两次计算。

**资金费（向量化口径）**——`use_funding = funding 存在 & include_funding & fee_model=='taker_with_funding'`（`:389-399`）；`fund_cell=prev_w*funding`（单次扣，不经 ml4t，故本文件**不存在双重计费**）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| BT-1 | **本文件已不是生产实现**：`impls.py:71-75` 只注册 `backtest_engine.py` 的 `ledger@1`；本文件 docstring 自称"纯数值参照与单测夹具"（`backtest_engine.py:15-16`）。所有真实挖因子走 engine，不走本文件。 | —（定位说明） | `impls.py:71-75`；`backtest_engine.py:15-16` | **已确认**：审查重点是"口径定义是否被 engine 继承"，而非本文件性能。生产资金费双重计费（P0-1）在 `24_backtest_engine.md`，本文件无此问题。 |
| BT-2 | 向量化版不做保证金/强平，杠杆>1 E1004；杠杆需求转 engine（ml4t 按 initial_margin 模拟强平）。 | —（正向护栏） | `backtest.py:360-372`；实测 E1004 | **已确认符合设计**：两套实现分工明确，E1004 是正确 fail-closed。 |
| BT-3 | 缺价处理：向量化把缺价收益当 0（`_returns:241-252`），只记 `missing_price_cells`；生产 engine 则不建价/不成交（`backtest_engine.py:_price_frame`）。口径细节不同。 | P2（说明） | `backtest.py:241-252,458-459` | **已确认**：本文件是参照口径，生产以 engine 为准；`missing_price_cells` 同名字段保留可归因性。 |

**多了什么**：无冗余。两档同源、成本归因、drawdown_episodes 都是必要聚合。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| ml4t 目标权重引擎（生产） | **采纳（生产唯一实现在 engine）** | `backtest_engine.py:1-17`；撮合/保证金/强平/资金费复用成熟库 |
| 向量化参照实现（本文件） | **保留作夹具/对拍** | `impls.py:71-75` 不注册为生产；`07-components.md` 要求"禁止另写向量化捷径"用于生产 |
| 自写撮合循环 | 拒绝 | `07-components.md:16,21` 明确为造轮子反例 |
| 日历年化 / periods_per_year=365 库默认 | 拒绝 | C1：annualisation_used 恒 calendar_365、bars_per_year 由中位步长推导 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **杠杆 E1004**：`:364-372` 实测。
2. **annualisation_used 恒 calendar_365**：`:453` 实测。
3. **bars_per_year 中位步长**：`:181-206` 实测 1h→8760、非等距取中位。
4. **summary/full 同源**：`:442-492`，两档共享同名对象，非两次计算（I12）。
5. **NEXT_BAR t=0 置 0**：`:375,402-404`。
6. **成本逐格**：`turnover_cell=0.5|Δw|`（`:383`）、fee/slip 按 tiers 档位（`:380,306-308`）、`cost_breakdown` 三项分开（`:432-437`）。
7. **资金费单次扣**：`:389-399`（本文件不经 ml4t，无双重计费）。
8. **缺价可见**：`missing_price_cells` 计数（`:458-459`）。
9. **时间轴 int64 纳秒**：`_times:148-178` 不转 float64（纳秒量级超 2^53），严格递增校验。

**发现的问题**：BT-1/BT-2/BT-3 均是定位/说明，非 bug。**本文件无 P0/P1 级代码 bug**；生产实现的 P0-1 双重计费不在本文件（见 24 号文档）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:605-617`（backtest 契约） | 与向量化口径一致（成本同源/年化分离/护栏） | 补一句"生产实现为 `ledger@1`（ml4t），ops/backtest 是参照夹具"以免读者误认本文件为生产 |
| `06-evaluation.md:200-228`（年化/再平衡） | 与 bars_per_year 中位步长口径一致 | 无需更新 |
| `07-components.md` §3 | 明确"禁止另写向量化捷径" | 与 BT-1 一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 保证 summary/full 两档一致 | ✅ 已支持 | 同一内部结果投影（`:442-492`） |
| 杠杆/保证金/强平 | ❌ 本文件 E1004 | 必须换 engine（ml4t） |
| intrabar 撮合（open/high/low） | ❌ 不支持 | 本文件只有 close 收益；engine 也只合成 OHLC（见 24 号文档） |
| 非日频年化 | ✅ 已支持 | bars_per_year 中位步长（实测 1h→8760） |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ annualisation_used 恒 calendar_365 + bars_per_year 由中位步长推导（实测）✅
- **C2 资金费按事件**：✅ 按 bar 费率求和、无 funding_period（`:13-14`），但**生产实现的双重计费问题见 24 号文档**
- **C3 spot 无资金费**：✅ `fee_model` 显式枚举，不按 exchange 推（`:42`）
- **C7 24/7 无日历**：✅ 年化按日历日 365，不靠交易日历

## R1–R7 影响

- **R1 真数据跑通**：中性（本文件非生产路径）
- **R2 结论可信**：✅ 口径定义正确（成本同源、年化分离、NEXT_BAR）；生产可信度取决于 engine
- **R7 fail-closed**：✅ 杠杆>1 E1004、时间轴非法 E3003、净收益非有限 E3003

---

## 结论

**本模块（向量化参照实现）口径定义正确，杠杆 E1004、annualisation_used=calendar_365、bar 频率中位步长、missing_price_cells 均经真实调用确认，无 P0/P1 级代码 bug。** 需注意它已非生产实现（生产是 `24_backtest_engine.md`），生产侧的资金费双重计费（P0-1）不在本文件。本模块可确认通过。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 7 个模块 `backtest_engine.py`（重点核 P0-1 资金费双重计费）
- ✏️ 需要补充/修正某维度 → 告诉我
