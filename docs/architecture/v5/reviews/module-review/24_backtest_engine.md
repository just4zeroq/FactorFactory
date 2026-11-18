# 模块审查 24 · `v5/capability/backtest_engine.py`

> 审查日期：2026-11-18
> 依据：源码 438 行全文通读；依赖 `ml4t.backtest`（Engine/DataFeed/Strategy/TargetWeightExecutor，`calendar=crypto`、`NEXT_BAR`）、`ops/backtest.py`（口径函数复用）；**ml4t 源码 `venv/Lib/site-packages/ml4t/backtest/broker.py:747` 与 `engine.py:175-184,642` 逐行核对**。设计 `07-components.md` §3（L69,82,92）、`s0/04-capability.md` §3.11（L605–617）、`05-contracts.md` I33/I34；测试 `test_v5_s4_backtest_ml4t.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
本模块是全系统**唯一生产回测实现**（注册名 `ledger@1`，`impls.py:71-75`）：ml4t 负责"能不能成交、何时成交"，V5 负责"这笔成交按什么口径计费"。四条边界：
1. **撮合来自 ml4t**：策略只把 allocate 算好的 `(T,N)` 目标权重喂给 `TargetWeightExecutor`，`ExecutionMode.NEXT_BAR` 在下一根 bar 成交（`:60-95`）。V5 不重写撮合循环。
2. **成本口径仍只有 V5 的 `caliber.net`**：ml4t 的 commission/slippage 置零（`:317,320`），逐笔成交取回后按 tiers 算 V5 的 `turnover×(fee+slippage)`。
3. **两档同源**：先构造唯一内部结果（净收益、逐格成本、权益、fills），再由 detail 投影 summary/full，full 不是第二次回测（`:335-434`）。
4. **fail-closed**：整列无价 / 整根 bar 无价 → E3003（`:162-190`）；成交时间不在窗口 → E3003（`:115-138`）；成交序列少于面板 bar → E3003（`:355-361`，只允许"多"截不多）。

**资金费的应然**：ml4t 引擎在 `funding_df` 传入时**已经把资金费扣进现金/权益**（broker.py:747 `cash_delta = -quantity*mark_price*multiplier*event.rate`，engine.py:642 调 `_apply_funding`）。因此 V5 若再按 `prev_w×funding` 扣一次，就是**双重计费**——这是本模块最严重的已确认缺陷（见下）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` 调用 `v5.capability.run(call)` **实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_funding.py` 与 `fm_verify_funding2.py`（系统临时目录），PYTHONPATH 指向仓库根。

### P0-1：资金费双重计费——源码核查 + 真实量化

**源码证据链（ml4t 内部已扣）**：
- `backtest_engine.py:332`：`Engine(..., funding_df=_funding_frame(funding, timestamps, symbols)).run()` —— 把 funding 交给 ml4t。
- `engine.py:175-184`：funding_df 非空 → `index_funding_events(...)` 建事件索引；`:642`：`self.funding_payments.extend(self.broker._apply_funding(funding_events))`。
- `broker.py:732-773` `_apply_funding`：`cash_delta = -quantity * mark_price * multiplier * event.rate`（`:747`），并 `self.cash = new_cash`（`:771`）——资金费**真实进入权益**，`equity["return"]`（engine.py:351）已含资金费。
- `backtest_engine.py:346`：`fund_cell = prev_w * np.asarray(funding)`；`:363-364`：`net_return = gross_bar - fee - slip - fund_cell.sum()` —— **V5 又扣一次**。gross_bar 已含 ml4t 资金费，再减 fund_cell = 双重计费。

**真实量化（全多仓、正费率 1e-4、gross=0.9，6 根 bar）**：

| 口径 | funding_total(V5 记账) | 全程净收益 Σ(net_return) | 含义 |
|---|---|---|---|
| A：funding_df 传入 **且** include_funding=True（当前生产） | `0.00036` | `-0.00408047` | ml4t 扣一次 + V5 再扣一次 |
| B：funding_df 传入、include_funding=False（仅 ml4t 扣） | `0.0` | `-0.00372047` | **正确口径**（单次） |
| C0：不传 funding（都不扣） | `0.0` | `-0.00345000` | 基线 |

逐 bar 量化：
```
ml4t 内部扣费（B − C0 全程） = -0.00027047
V5 再扣一次（A − B 全程）     = -0.00036000   （恰等于 A.funding_total）
A 相对基线 C0 多扣            = -0.00063047   ≈ 2.33 × 正确资金费
```
**结论：当前生产在有资金费时，净收益被资金费扣了约 2.33 倍**（ml4t 0.00027 + V5 0.00036）。混合多空仓场景同样确认：A 与 B 的 net_return 逐 bar 差恰为 V5 的 `fund_cell`（`fm_verify_funding.py` 输出 A−B 全程 5e-5 = A.funding_total）。P0-1 成立且已量化。

**其余必查要点实测**：
- **杠杆 >1**：engine **不报 E1004**——`max_gross = max(max_leverage, gross, 1.0)`（`:304`）、`allow_leverage = max_leverage>1`（`:309`），由 ml4t 按 initial_margin 模拟强平。这与 ops/backtest 的 E1004 是**有意分工**（ops 无强平模拟才拒，见 23 号文档），属"需确认意图"而非 bug。
- **annualisation_used 恒 calendar_365**：`:400` 固定字符串；bars_per_year 由 `bars_per_year(times,t_len)` 中位步长推导（`:378`），实测 ops 口径 1h→8760。
- **bar 频率按中位步长**：继承 `ops/backtest.bars_per_year`，非等距取中位。
- **missing_price_cells**：`:405` 落盘；整列无价 E3003、整根 bar 无价 E3003（实测于 `test_v5_s4_backtest_ml4t.py:249-272`）。
- **gross_bar 只截不多**：`:355-362` 成交序列少于 t_len 即 E3003，多于 t_len 只截前 t_len（t0 行）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| E-1 | **资金费双重计费（P0-1）**：ml4t `funding_df` 已在引擎内扣资金费（broker.py:747 + engine.py:642），V5 又在 :346 按 `prev_w×funding` 扣一次并写入 net_return。真实量化：全多仓正费率下净收益被扣约 2.33 倍（0.00063 vs 正确 0.00027）。 | **P0（已确认，必修）** | `backtest_engine.py:332,346,363-364`；`broker.py:747`；`engine.py:175-184,642`；本次真实量化表 | **已确认事实**。修法二选一：①**不传 funding_df 给 ml4t**（删 :332 的 funding_df），只保留 V5 的 :346 逐格扣费（V5 口径唯一、可审计）；②**传 funding_df 但删 V5 的 fund_cell 扣减**（:346,:363，funding_total 从 `result.funding_payments` 取）。推荐①：与"成本口径只有 V5"的设计一致（docstring :8-11）。这是用户点名的 P0-1，本轮只审查不改。 |
| E-2 | **杠杆 >1 无 E1004（与 ops 口径不同）**：engine 允许杠杆并由 ml4t 强平模拟；ops/backtest 拒 E1004。 | P1（需确认意图） | `backtest_engine.py:302-309,285` vs `ops/backtest.py:364-372` | **已确认符合分工**：ops 无强平才拒，engine 有强平才放行（23 号文档已述）。文档需明写"生产回测允许杠杆，靠 initial_margin/maintenance_margin 强平"，避免读者误以为 E1004 在所有回测路径生效。 |
| E-3 | **OHLC 是合成的**：open/high/low=close、volume=常数（`:152-201`），因生产只用 `ExecutionPrice.CLOSE`；不进落盘字节。 | P2（已确认设计） | `:156-160`；`test_v5_s4_backtest_ml4t.py:302-318`（换名逐位一致） | 扩展 intrabar 时必须扩 inputs 契约传真实 OHLCV，不能偷合成（注释已明说）。 |
| E-4 | funding_df 的频率对齐依赖上游 `load_funding_series` 把 8h 事件聚合到 bar 网格；本模块不校结算周期（C2）。 | P2（责任边界） | `07-components.md:82,93`；`backtest_engine.py:204-213` | 属供给层职责（I34）；本模块只按传入 rate 逐 bar 建事件，符合设计。 |

**多了什么**：无冗余。`_index_of` 最近邻匹配（`:115-138`）、synthetic symbol 集中入口（`:141-149`）都是必要工程处理。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| ml4t `Engine` + `TargetWeightExecutor` + `NEXT_BAR` + `calendar="crypto"` | **采纳（生产唯一）** | `:306-333`；撮合/保证金/强平/资金费复用成熟库（`07-components.md:16`） |
| V5 `caliber.net` 在 fills 上逐格算手续费/滑点 | **采纳** | `:338-340`，ml4t commission/slippage 置零（`:317,320`），避免库再计一次 |
| 资金费：ml4t 内部 vs V5 逐格 | **当前双重（E-1，P0）** | 只能二选一；推荐只保留 V5 逐格口径（与"成本口径唯一"设计一致） |
| 自写撮合循环 / 向量化捷径作生产 | 拒绝 | `07-components.md:16,21`；ops/backtest 仅参照 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **ml4t 撮合**：NEXT_BAR + CLOSE + crypto calendar（`:313-315,323`），实测 config 断言（`test_v5_s4_backtest_ml4t.py:91-109`）。
2. **成本不在库里重复计**：commission_rate=0、slippage_rate=0（`:317,320`）。
3. **两档同源**：summary/full 共享字段实测逐项相等（`:391-434`；测试 `:112-126`）。
4. **gross_bar 只截不多**：`:355-362`。
5. **缺价 fail-closed + 计数**：整列/整根无价 E3003（`:162-190`），missing_price_cells 落盘（`:405`）。
6. **成交时间对齐**：`_index_of` 最近邻 + 半步长容差（`:115-138`），不靠整除（缺口后不错位）。
7. **synthetic symbol 不进落盘字节**：`_symbol_names`（`:141-149`）+ fills_ref 是数值摘要（测试 `:302-318`）。
8. **年化口径**：annualisation_used 恒 calendar_365（`:400`）、bars_per_year 中位步长（`:378`）。
9. **只读数组**：net_series/turnover_series setflags(write=False)（`:386-390`）。

**发现的问题**：**E-1（P0，资金费双重计费，已量化）**、E-2（P1 杠杆口径差异，需确认意图）、E-3/E-4（P2 已确认设计）。这是全轮最严重的已确认生产缺陷。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `07-components.md:69,82,92`（资金费 ml4t 接线） | 规划"Engine(funding_df=...)；`load_funding_series` 聚合" | **与 E-1 冲突**：当前既传 funding_df 又 V5 再扣。修 E-1 后须明确"资金费只在 V5 逐格扣 / 或只在 ml4t 扣"，并同步此节 |
| `s0/04-capability.md:615`（年化与 bar 频率分开） | 与实现一致 | 无需更新 |
| `s0/04-capability.md:605`（detail 参数） | 与实现一致 | 无需更新 |
| `05-contracts.md:43-44`（I33/I34） | 年化 365、资金费按事件 | 补"资金费不得在 ml4t 与 V5 两处同时扣"为 I34 的推论 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 生产回测（撮合/保证金/强平/资金费） | ✅ 已支持（但 E-1 需先修） | ml4t engine 在位，真挖因子在用 |
| 杠杆/保证金 | ✅ 已支持 | ml4t 按 initial/maintenance_margin 模拟强平（E-2） |
| intrabar 撮合 | ❌ 不支持 | OHLC 合成（E-3），需扩 inputs 契约传真实 OHLCV |
| 非日频年化 | ✅ 已支持 | bars_per_year 中位步长（1h→8760，继承 ops 口径） |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ annualisation_used 恒 calendar_365 + bars_per_year 中位步长（实测）✅
- **C2 资金费按事件**：❌ **双重计费（E-1，P0）**——ml4t 内部已按事件扣（broker.py:747），V5 又按 bar 扣一次，违反"按事件求和、只扣一次"的应然
- **C3 spot 无资金费**：✅ fee_model 显式枚举，funding_df 仅在有时传入
- **C7 24/7 无日历**：✅ `calendar="crypto"`
- **C10 判据不可放松**：⚠️ 杠杆 >1 放行但靠强平模拟（E-2，需确认意图），非放松判据

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（生产回测真跑在用）
- **R2 结论可信**：❌ **强负向——E-1 双重计费直接扭曲净收益（实测约 2.33× 资金费），成本闸与认证净表现全部偏低，是当前最严重的结论可信缺陷**
- **R5 单因子可归因**：✅ cost_breakdown 三项分开（但 funding 项=E-1 多扣的部分，需先修）
- **R7 fail-closed**：✅ 强支撑——缺价/成交时间/序列长度全拒

---

## 结论

**本模块存在已确认 P0-1：资金费双重计费。** ml4t `broker.py:747` 在传入 funding_df 后已把资金费扣进权益，$ backtest_engine.py:346,363-364$ 又按 `prev_w×funding` 扣一次；真实量化显示全多仓正费率下净收益被扣约 **2.33 倍**（0.00063 vs 正确 0.00027）。修复建议：**不传 funding_df 给 ml4t（删 :332 的 funding_df），只保留 V5 逐格扣费**，与"成本口径只有 V5"的设计一致。其余项（杠杆由 ml4t 强平、OHLC 合成、年化口径）均为已确认设计或说明缺口。**这是全轮最需优先处理的缺陷**，属用户点名必查、本轮只审查不改。

**请确认本模块审查结果**：
- ✅ 确认 P0-1 成立，纳入必修清单 → 我进入最后一个模块 `certify.py`
- ✏️ 需要补充/修正某维度（如 E-1 修法偏好） → 告诉我
- ⚠️ 想当场裁决 P0-1 修法（删 funding_df vs 删 fund_cell） → 告诉我
