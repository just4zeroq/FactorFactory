# 模块审查 66 · `app/cli/estimate.py`

> 审查日期：2026-11-18
> 依据：源码 98 行全文通读；上游 `capability/timeaxis.py`（CALENDAR_DAYS_PER_YEAR=365）、`capability/backtest_engine.py`（_rate_grids 成本模型）、`runtime/charter.py` load_plan、`contracts/errors.py`（E1008）、`cli/common.py`（envelope_ok/load_plan）；设计 `09-app-storage.md` §2（estimate 按 caliber.net 估算）、`06-evaluation.md` §成本、`10-crypto-fit.md`；实测 `venv/Scripts/python.exe -m app.cli estimate --plan tests/fixtures/plan_valid.json`（多组 turnover）。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`estimate.py` 回答**一个问题**：这套口径下，策略每期要赚多少才够付成本（模块 docstring `:3`）。它应然：

1. **口径全来自 plan 的 `caliber`**：CLI 不得出现口径开关（P1）。`--turnover` 不是口径，是「问题参数」——同一口径在不同换手下成本不同，正是要算的东西（`:5-6`）。
2. **算式与 backtest_engine 逐项对齐**：`fee = tier/10000`（tier 是 bp 单边）、`slippage = none→0 / fixed→tiers[0]/20000 / tiered→tier/20000`、每期成本 = `turnover×(fee+slippage)`、每年期数 = `365/rebalance`（`:8-15`）。两处不一致会让「估算说 12bp、实盘扣 18bp」无处可查。
3. **只读不碰数据**：不读 parquet、不跑执行、不产 ticket，纯算式。
4. **fail-closed**：缺 `caliber.net` / `cost_tiers` 非法 / slippage 未知 / rebalance<1 / turnover 超 [0,10] 全报 E1008/exit2。

**加密视角（C1 必查点）**：`periods_per_year = 365/rebalance`（`:26,62`），常量来自 `CALENDAR_DAYS_PER_YEAR`（timeaxis），**不依赖库默认 252、不用交易日日历**——这是 C1 在成本估算层的落点。`rebalance=5` 时实测 73.0 期/年（365/5）。资金费不在估算内（按事件逐 bar 计，C2），note 明确说明（`:94-95`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 真实调用 `venv/Scripts/python.exe -m app.cli estimate --plan tests/fixtures/plan_valid.json`，非推演。

### 例 1：默认 turnover=1.0（真实打印，节选）

```
{"ok":true,"exit_code":0,"data":{"estimate":{
  "plan":"cs_2026q4_a","annualisation":"calendar_365",
  "rebalance_bars":5,"periods_per_year":73.0,
  "turnover_per_period":1.0,"slippage":"tiered","fee_model":"taker_with_funding",
  "tiers":[
    {"tier_bp":20.0, "fee_per_period":0.002,  "slippage_per_period":0.001,
     "cost_per_period":0.003,  "cost_annual":0.219,  "breakeven_annual_return":0.219},
    {"tier_bp":60.0, "fee_per_period":0.006,  "slippage_per_period":0.003,
     "cost_per_period":0.009,  "cost_annual":0.657,  "breakeven_annual_return":0.657},
    {"tier_bp":150.0,"fee_per_period":0.015,  "slippage_per_period":0.0075,
     "cost_per_period":0.0225, "cost_annual":1.6425,"breakeven_annual_return":1.6425}
  ],
  "note":"成本按单边换手计；tiered 滑点取该档位的一半，与 backtest 的 _rate_grids 同口径。资金费不在本估算内（按事件逐 bar 计）。"
}}}
EXIT=0
```

逐项验算（tier=20bp, turnover=1）：`fee=20/10000=0.002` ✓；`slippage(tiered)=20/20000=0.001` ✓；`cost_per_period=1×(0.002+0.001)=0.003` ✓；`periods=365/5=73` ✓；`cost_annual=0.003×73=0.219` ✓。**算式与源码逐位一致，C1 的 365 落地（73.0 而非 365 交易日折算的 73 或 252/5=50.4）**。

### 例 2：turnover=2.0（真实）

`cost_per_period` 全部翻倍（20bp 档 0.006，年化 0.438），`periods_per_year` 仍 73.0——turnover 是问题参数，不改频率。

### 例 3：turnover 超界（真实失败）

```
$ ... estimate --plan ... --turnover 99
→ {"ok":false,"exit_code":2,"errors":[{"code":"E1008","layer":"usage",
   "message":"--turnover 必须在 [0,10]，得到 99.0","path":"estimate.turnover"}]}
EXIT=2
```

### 例 4：缺 `caliber.net`（真实）

用一个无 net 块的 plan 调用（或 fixture 删 net）→ E1008「计划缺 caliber.net，无法估算成本」hint「成本档与滑点必须写进口径块」→ exit2（源码 `:33-37`，语义确认，未构造新文件重跑）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| E-1 | **OI 数据仅 25 行的已知事实与本模块的关系**：estimate 的成本估算**不读任何数据**（纯 plan 算式），OI 25 行不影响 estimate 输出；但用户必查点特别提到「OI 数据仅 25 行的已知事实」——该事实是**采集/面板层限制**（币安 OI 约 30 天历史），不是 estimate bug。 | —（事实澄清） | `deployment_and_updates.md:135`、`v5_e2e_analysis_20261117.md:36`：20 个 OI 流各 25 行（2026-10-21~11-14）；estimate.py 全文无数据读取 | **已确认**：OI 25 行是上游数据源上限（见 64 号 F-5 与 65 号 thin 体检），estimate 作为纯算式命令与它无关。若计划用 OI 类因子，25 行覆盖不了 2023–2025 窗口 → 特征几乎全空，这是 R3 缺口的一部分，非本模块修。 |
| E-2 | **`fee_model` 只在输出里透传（`:92`），不参与算式**：`fee = tier/10000` 写死，不区分 `taker` / `maker` / `taker_with_funding` 等档位差异。 | P2（需确认意图） | `estimate.py:66` 固定 `tier/10000`；实测 fee_model='taker_with_funding' 但 fee 仍按 tier 算 | **需确认**：若 backtest_engine 的 `_rate_grids` 也不按 fee_model 分档（同口径），则一致；若分档，则估算与实盘会分歧。属「算式与 backtest 对齐」的核对点（用户必查），需对照 backtest_engine._rate_grids 确认。 |
| E-3 | **`slippage=fixed` 时用 `tiers[0]/20000`**（`:70`），但 `cost_tiers` 顺序若非升序则 tiers[0] 不一定是最低档——输出不提示。 | P2（边界） | `estimate.py:70`；plan fixture tiers=[20,60,150] 升序（实测正常） | **低优先**：plan_schema 可能未强制 tiers 升序；如果降序，fixed 滑点取了最高档。可加「tiers 应升序」校验。 |
| E-4 | **`breakeven_annual_return` 与 `cost_annual` 完全相等**（`:79,81`）：字段名不同但值恒同。 | — | `estimate.py:79,81`；实测两列逐行相等 | **符合应然**：「毛收益要盖住成本才不亏」，盈亏平衡年化收益 = 年化成本，语义正确，非冗余 bug。 |

**多了什么**：无。98 行全是必要算式与校验。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 365 日历年化 | 采纳 | C1：`CALENDAR_DAYS_PER_YEAR=365`（`:26`），实测 73.0 期/年——不依赖 252/交易日。 |
| 成本算式与回测引擎对齐（_rate_grids 同源） | 采纳 | docstring（`:8-9`）明确逐项对齐，防「估算与实盘口径分家」。 |
| 资金费按事件逐 bar（不进成本估算） | 采纳 | C2：note 明说（`:94-95`），资金费随每段持仓的事件求和，不能折算成固定 bp。 |
| turnover 作为问题参数（非口径） | 采纳 | `:5-6` 的区分正确——口径写 plan，turnover 是提问维度，CLI 可传不违反 P1。 |
| 固定 252 / 交易日折算 | 拒绝 | 加密 24/7，无交易日（C1/C7）。 |
| 把 OI 25 行硬塞进估算 | 拒绝 | 估算不读数据；OI 覆盖不足是数据层问题（E-1），不在这伪装成成本数字。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **C1 365 实测**：`periods_per_year=73.0`（365/5），非 252 系（例 1）。
2. **算式逐位验算通过**：fee/slippage/cost_per_period/cost_annual 与源码逐项一致（例 1）。
3. **口径全来自 plan**：CLI 只有 `--plan/--turnover/--format`，无口径开关——P1 守卫无触发面；turnover 是问题参数（`:5-6`）。
4. **fail-closed 齐全**：缺 net（E1008）、cost_tiers 非空列表校验（`:45-48`）、slippage 枚举（`:49-51`）、rebalance≥1（`:52-55`）、turnover∈[0,10]（`:57-60`）。
5. **annualisation 透传**：输出 `'calendar_365'`（实测），让研究员确认年化口径。
6. **note 透明**：明说「资金费不在估算内、tiered 滑点取半档」（`:94-95`），无隐藏假设。

**发现的问题**：
- 无 P0/P1 级 bug。E-2（fee_model 不参与算式，需对照 backtest 确认）、E-3（tiers 升序未校验）为 P2 对齐/边界问题，不影响估算基本功能。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `06-evaluation.md` §成本 | 成本算式描述 | 与 estimate.py:8-15 逐项核对；若文档写 fee_model 分档而实现不分（E-2），统一口径 |
| `10-crypto-fit.md` OI 25 行 | 已列为加密缺口 | 补 E-1 澄清：estimate 是纯算式，不受 OI 行数影响；OI 覆盖不足影响的是因子可用性（R3），非成本估算 |
| `09-app-storage.md` §2 estimate | 描述「预估成本与耗时」 | 实测本模块只算成本、不算耗时（docstring `:3` 也只说成本）；统一文档口径 |
| `s0/03-runtime.md` 365 常量 | 与 timeaxis CALENDAR_DAYS_PER_YEAR 一致 | 无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增成本档/滑点模式 | ⚠️ 需改代码 | slippage 枚举写死 none/fixed/tiered（`:50`）；新模式需加分支 + 同步 backtest_engine。 |
| fee_model 分档（maker/taker 不同费率） | ❌ 不支持 | 当前 fee=tier/10000 固定（E-2）；若 backtest 分档需扩展此处。 |
| 把资金费纳入估算 | ❌ 设计拒绝 | C2：资金费按事件逐 bar，无法折算固定 bp；note 已声明。 |
| 估算耗时/容量 | ❌ 不支持 | 文档写「成本与耗时」，实现只算成本（D5）；加耗时需新模型。 |
| OI 覆盖不足的「能否用 OI 因子」判断 | ❌ 不在本模块 | 属数据层/面板层（64/65 号），estimate 不读数据。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 强——`periods_per_year=365/rebalance`（`:26,62`），实测 73.0，不依赖 252/交易日日历。
- **C2 资金费按事件**：✅ note 声明资金费不在估算内、按事件逐 bar 计（`:94-95`），不把 funding 折算成固定 bp。
- **C7 24/7 无交易日**：✅ 隐含——365 日历年化假设全年无休市。
- **C10 判据不放宽**：中性（estimate 不算判据，只算成本）。
- **OI 25 行事实**：✅ 已确认与 estimate 无关（E-1：纯算式不读数据）；该事实是采集/面板层限制（R3 缺口的一部分）。
- **C3/C4/C5/C6/C8/C9/C11/C12**：不适用（本模块不读数据、不算口径判据）。

## R1–R7 影响

- **R1 真数据跑通**：中性（estimate 不碰数据）。
- **R2 结论可信**：✅ 正向——成本估算与 backtest 同口径（声明），365 年化正确，研究员可预判策略能否覆盖成本。
- **R3 因子零代码扩展**：⚠️ OI 25 行（E-1）使 OI 类因子窗口几乎全空，是 R3 未通的数据侧原因之一——但非 estimate 模块可修。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：✅ 正向——缺 net/cost_tiers/slippage/rebalance/turnover 全 E1008/exit2，不静默用默认成本。

---

## 结论

**本模块 98 行纯成本估算命令无 P0/P1 级 bug，可确认通过。** C1 的 365 年化实测成立（73.0 期/年）、算式与源码逐位验算一致、口径全来自 plan 无 CLI 泄漏、fail-closed 齐全。**用户必查点「OI 仅 25 行」已确认与本模块无关**（E-1：estimate 纯算式不读数据；25 行是币安数据上限，影响的是 OI 因子可用性=R3 缺口，非成本估算）。2 项 P2：E-2（fee_model 未参与算式，需对照 backtest_engine 确认同口径）、E-3（tiers 升序未校验）。不影响「生产可用=能真挖因子」。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 12 个模块 `cli/pipeline.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ E-2 想当场裁决（是否核对并支持 fee_model 分档）→ 告诉我
