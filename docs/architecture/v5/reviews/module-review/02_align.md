# 模块审查 02 · `v5/capability/ops/align.py`

> 审查日期：2026-11-18
> 依据：源码 315 行全文通读；依赖 `stream.py`（FieldStream/Scope/OBSERVABLE_CODES）、`panel.py`（Panel/array_digest）、`registry.py`（字段 available_lag）、`timeaxis.py`（唯一区间实现）、`stats.py`（因果算子）；设计 `s0/04-capability.md` §3.3（L282–347）、`10-crypto-fit.md` §7.1/§7；测试 `test_v5_boundary_align.py:298-411`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Align 是八工位第 3 步"面板对齐"，把 `read` 的 `FieldStream` 与 `boundary` 的 `Scope` 合成一块可信面板，回答四件事：

1. **按 Scope 消费运行窗口**：只用 `mine ∪ oos`（`axis_resolved`）裁行；`holdout` 的任何取值不得进面板。scope 与流的时间轴/标的必须来自同一次供给版本，不一致即拒。
2. **未收盘丢弃**：`drop_unclosed=true` 且供给末端 `end_closed=false` 时，丢弃的是**数据末端那一根**（不是运行窗口末端），并留可查证据 `{count, last_ts}`。
3. **因果探针 `causal_probe`**：`none` = 索引单调 + 保留区不进；`strict` = 以上 + 逐字段逐位比对（面板取值==流在保留行上的字节）+ 时间连续性（跨洞不得相邻拼行）。检查必须**能真失败**——一个恒 `passed=true` 的探针比没有更糟。
4. **字段滞后 `field_lags` 从注册表取**：声明滞后与注册表不符即前视，整步失败；`tz` 只声明构造契约（UTC），不做换算；质量门禁（缺失比例 / 有效观测数）超限即拒，逐个列出问题列。

**加密特化**：24/7 无日历，未收盘靠供给末端事实（`stream.end_closed`）而非日历；「连续相邻一根」是 `forward_return`/`trailing_mean` 的语义，所以维护/合约迁移造成的数据洞必须显式声明为 holdout，不得被拼成相邻行（P5）；字段 `available_lag` 是防前视的唯一门禁，加密事件流（funding/OI/basis）尤其依赖它。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下矩阵是用仓库 `venv/Scripts/python.exe` 调用 `v5.capability.run(call)` **实际打印**的结果，不是推演。
> 复现方式：按测试 `tests/v5/test_v5_boundary_align.py:55-121` 的 `_stream/_pin/_scope/_align_call` 构造；临时脚本放系统临时目录，未改仓库文件。

### 例子的场景（围绕加密货币）

30 根 1d bar（`2024-01-01…2024-01-30`，24/7 无日历）、6 个标的（S0…S5）、字段 `close`/`volume`、`available_lag=0`、`end_closed=True`。
时间轴：`mine=01-05..01-20`，`oos=01-21..01-25`，`holdout=reserved`。先跑 boundary 得 Scope，再喂 align。

### 输入：`Call`

```
capability   = "align"
station      = "Panel"
impl         = "point_in_time_align"
impl_version = "1"
params = {
  "drop_unclosed": True,
  "causal_probe":  "strict",
  "quality_gates": {},
}
runtime_facts = {"holdout_visibility": "reserved"}   # 揭示状态是执行期事实，不入 params
inputs = (
  InputRef("field_stream", <digest>, data=FieldStream(
      times=30×1d, symbols=("S0".."S5"),
      fields={"close":(30,6), "volume":(30,6)},
      manifest={"close":{...,available_lag:0}, "volume":{...,available_lag:0}},
      end_closed=True)),
  InputRef("scope", <digest>, data=Scope(...)),       # 由 boundary 产出、共享同一时间轴
)
```

要点：align **不读盘**——FieldStream 与 Scope 由执行器解析注入（`:39-67`），拿不到或类型不对即 `E3003`。

### 输出：`run(call)` 返回的 dict（9 个键，实测）

```
keys = ['panel_ref','shape','time_index_ref','field_manifest','causal_report',
        'quality_report','panel','holdout_visibility']   （并 core 字段）
```

**shape / panel**（实测）：
```
shape        : {'bars': 21, 'symbols': 6, 'fields': 2}
panel.shape  : (21, 6)      fields = ['close','volume']
panel.times[0] = 1704412800000000000  (2024-01-05)
panel.times[-1]= 1706140800000000000  (2024-01-25)
holdout_visibility = "reserved"
panel_ref    : sha256:bc66be7054920fab933dfd2e81507661ad14f2d9319624d495e0fe3f25cbfc75
time_index_ref: sha256:94f89b863c115eeecbc5e9eb747627efa0a4a7b4dd26163d7f67b8f1ea2a31a4
```
→ 运行窗口 01-05..01-25 精确裁成 21 根，保留区（01-01..01-04、01-26..01-30）一根不进。

**`causal_report`**（实测）：
```
probe.checks:
  P1 time_index   passed=True offending=[]
  P2 field_lags   passed=True offending=[] fields_checked=2
  P3 holdout      passed=True offending=[] boundary={start:None, end:None}
  P4 byte_identity passed=True offending=[]      # strict 才跑
  P5 continuity   passed=True offending=[]        # strict 才跑
probe.probe  = {'mode':'strict','fields':['close','volume'],
                'method':'byte_identity_and_continuity','max_shift_used':0}
field_lags   = {'close':0, 'volume':0}
holdout_boundary = {'start':None,'end':None}       # reserved → 补集语义
violations   = []
```
注意 P2/P3 的 `passed` 恒为 True——不通过的路径在上面已 `E3003` 抛掉、走不到这里；它们的价值在**证据**（`fields_checked=2`、`boundary` 实际取值），不是布尔值（`:158-164,182-186`）。

**`quality_report`**（实测）：
```
{'bars':21,'symbols':6,'fields':2,
 'missing_ratio': {'close':0.0,'volume':0.0},
 'min_obs_per_symbol': {'S0':21,...,'S5':21},
 'min_obs_basis': 'close',
 'unclosed_dropped': {'count':0,'last_ts':None},
 'duplicate_index': 0,
 'tz': 'UTC', 'tz_source': 'int64_utc_index'}
```
`field_manifest`（实测）：`[{'name':'close','dtype':'float64','unit':'quote','available_lag':0,'supply_version':'v1'}, {'name':'volume',...,'unit':'base',...}]`。

### 未收盘丢弃（实测：`end_closed=False` 且轴覆盖到数据末端）

```
drop_unclosed=True : quality_report.unclosed_dropped = {'count':1, 'last_ts':1706572800000000000}
                     panel 末根 = 1706486400000000000 (01-29)，数据末根 = 1706572800000000000 (01-30)
drop_unclosed=False: unclosed_dropped = {'count':0,'last_ts':None}，panel 末根 = 01-30
```
→ 丢的是**供给末端**那根（01-30），且 `count=1` 时 `last_ts` 非空（门禁记录可反查，`:111-114,286`）。

### 失败输入 → 错误输出（fail-closed 路径，同样实测）

| 非法输入 | 输出（错误码 + 真实消息） |
|---|---|
| manifest 的 `available_lag=1`（注册表=0） | `E3003` "2 个字段的 available_lag 与注册表不符" |
| scope 来自另一标的集（5 vs 6） | `E3003` "scope 与 field_stream 的时间轴或标的不一致" |
| `runtime_facts.holdout_visibility="bogus"` | `E3003` "holdout_visibility='bogus' 不在 ['reserved','revealed']" |
| far-axis Scope（对齐后无时点） | `E3003` "对齐后没有任何时点" |
| `quality_gates.max_missing_ratio=0.01`（实测缺失 16.7%） | `E3003` "字段缺失比例超上限 1%: {'close': 0.1666...}" |
| `quality_gates.min_obs=100` | `E3003` "标的有效观测数低于 min_obs=100: {'S0':21,...}"（逐个列，非总数） |
| 数据中间跳过 3 天（01-10→01-14）、`strict` | `E3004` "面板时间轴不连续：mine 与 oos 之间存在被跳过的区间" |
| 同一数据、`causal_probe="none"` | **OK**，`panel.shape=(27,6)`，report 里**无 P5** → 洞检查仅在 strict |

> 另：远轴在 boundary 阶段就被 `E4002` 拒（"区间 … 在数据上没有落到任何时点"），到不了 align；此处为构造 align 自身空面板路径，按测试 `test_align_rejects_empty_panel` 直接造 far-axis Scope 实测。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| A-1 | **P4 逐位比对只在保留行做**：文档要求"面板与流在保留行上字节一致"，但 reserved 语义下保留区本就不进面板——P4 实际比对的是"面板 == 流[keep]"，即所有保留下来的行（`:192-204`）。这是恒等变换的自证，能抓住"面板被改写/错行/补值"，但抓不住"流入面板前已被上游 shift(-1) 污染"——若 FieldStream 本身含前视值，P4 照样通过。文档 `s0/04:300` 承认旧"打乱未来值"探针被替换，但新 P4 的边界应写清"只保证面板忠实于流，不保证流本身无前视"。 | P2（说明缺口） | `align.py:190-204`；`s0/04-capability.md:300,321` | **需确认意图**：前视的源头防在 `read.field_manifest`（lag 从注册表）+ boundary，align 只证"不篡改"。建议文档补一句，非代码 bug。 |
| A-2 | **`unclosed_dropped` 无显式一致性断言**：文档要求"`count>0` 而 `last_ts` 为空即 E3003"。实现里 count 与 last_ts 总在同一次分支里成对赋值（`:111-114`），不可能出现 count>0/last_ts=None，故无需运行期检查——但这是靠构造保证，文档把它写成"门禁检查"容易让读者以为有运行时断言。 | P2（说明） | `align.py:110-114`；`s0/04-capability.md:346` | **已确认正确**（构造保证不变量），仅说明口径。 |
| A-3 | **`quality_gates` 是必填参数**（schema `required:true`），但实现 `dict(params.get("quality_gates") or {})` 容错缺省（`:257`）。经 `run()` 调用会被 `validate_params` 以 E1008 挡住；直调 `build()` 则静默按空门禁跑。两条入口行为不一致。 | P2（待核实入口） | `align.py:257`；`registry/params.json` align.quality_gates.required=true | **需确认**：与 boundary B-3 同类（编译期校验 vs 运行期容错）。并入 U4 编译期校验，不在本模块单独修。 |
| A-4 | **未收盘只丢末端 1 根**：`keep[-1]=False`（`:113`）。若供给末端连续多根未收盘（加密 24/7 撮合中最后几根都算未定），只丢一根会把其余半成品 bar 留在面板。当前 `pin.end_closed` 是单个布尔，无法表达"末端 k 根未收盘"。 | P2（数据模型限制） | `align.py:111-114`；`stream.py:135`（end_closed: bool） | **已确认符合当前契约**（end_closed 是布尔、只标末端一根）。若未来供给能给"未收盘根数"，需扩 Pin——属上游数据模型，非 align 单独可补。 |

**多了什么**：无冗余。`causal_report.checks` 的 P1/P4 与 `Panel` 构造校验有重叠（都在证索引单调），但文档明确定位 P1 是"给读者可核对的结论"、非检查点（`s0/04:318`），保留合理。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| qweave / polars 的 join-asof 对齐到 bar 网格 | **拒绝（本层）** | align 只裁行、不改值；事件流的 as-of 对齐属 `read`/供给层职责。本层逐位比对（P4）正是为了证明"没做隐蔽对齐"（`:190-204`） |
| 向量化字节比对 `np.array_equal(..., equal_nan=True)` | 采纳 | 逐字段严格比对，NaN 按位等；能抓错行/补值，且可复现（`:193-195`） |
| "打乱未来值看统计是否变化"的前视探针 | **拒绝（已替换）** | 在纯数组上恒通过（测的是因果算子自身），会以 `passed=true` 假通过；换成 P4 逐位一致 + P5 连续性，两者都能真失败（文件头注释 L20-24；`s0/04:300`） |
| 交易日历判"未收盘" | **拒绝** | 24/7 无休市；未收盘靠 `stream.end_closed` 供给事实（`:111`），不查日历（C7） |
| 时间轴洞按"相邻一根"线性拼接 | **拒绝（strict 下 fail-closed）** | `forward_return`/`trailing_mean` 按相邻解释，跨洞会把 oos 价格喂给 mine 标签；P5 用 `gaps<=step*1.5` 抓洞并 `E3004`（`:208-220`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **Scope 消费与同源校验**：`scope.times/stream.times`、`scope.symbols/stream.symbols` 必须一致，否则 `E3003`（`:91-97`）——掩码与面板不错位。
2. **未收盘丢末端一根 + 门禁记录**：`drop_unclosed && end_closed is False && keep[-1]` 才丢（`:111-114`）；末根不在运行窗口则不丢（测试 `test_unclosed_bar_outside_the_run_window_is_not_marked`），语义精确。
3. **字段滞后只认注册表**：逐字段比对 `manifest.available_lag` 与 `registry.fields`，不符即 `E3003`（`:140-157`），错误码是"面板不可信"而非"参数错"，契合文档。实测 lag=1 → E3003。
4. **保留区不进面板**：`holdout` 为区间时，面板含其内部时点即 `E3003`（`:166-177`）；`reserved` 时边界记 None、面板=运行窗口（`:178-186`）。
5. **causal_probe 真能失败**：P1 单调（`:134-138`）、P4 逐位（`:189-204`）、P5 连续（`:206-220`）；实测数据洞在 strict 下 E3004、none 下静默通过并缺 P5——检查强度确实由参数控制。
6. **strict 不降级**：`violations` 非空即整步 `E3004`，不降级为警告（`:222-228`）。
7. **质量门禁逐列报告**：`max_missing_ratio`（`:257-267`）、`min_obs`（`:268-277`）逐个列出超限字段/标的，实测符合。
8. **无 ffill/bfill**：align 不填充任何缺失，NaN 原样保留（`:121-125` 仅裁行）。
9. **`tz` 写清来源**：`tz='UTC'`、`tz_source='int64_utc_index'`（`:288-291`），明确是构造契约而非校验结果，不伪造门禁。
10. **`min_obs_basis` 不硬绑列**：优先 `close`、无则字典序第一字段（`:248-255`），适配开放列集。

**发现的问题**：
- 无 P0/P1 级代码 bug。A-1~A-4 均为 P2（说明/跨入口一致性/数据模型限制），其中 A-3 并入 U4 编译期校验、A-4 属上游 Pin 数据模型。测试覆盖充分（align 相关 12 个用例，`:298-411`，含未收盘、lag、保留区、质量门禁、空面板、C7 洞在测试中由 boundary 阶段拒绝）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:282-347` | 与实现逐条一致（裁行、未收盘、P1–P5、质量门禁、四条约束） | 无需更新 |
| `s0/04-capability.md:321`（P4 逐位一致） | 文档表述"面板与流在保留行上字节一致"，但 reserved 下保留区不进面板，P4 实为"面板忠实于流[keep]" | 补一句"P4 保证面板不篡改流，不保证流本身无前视（源头由 read 的 lag 门禁负责）"（A-1，说明性） |
| `s0/04-capability.md:346`（unclosed 门禁） | 写成"count>0 而 last_ts 空即 E3003" | 注明由构造成对赋值保证、无独立运行期断言（A-2，说明性） |
| `10-crypto-fit.md` §7.1 | 事件流 as-of 对齐属 read/供给层 | align 层无需改；待 C8/C9 落地后确认 P4 仍逐位成立 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增字段进面板（funding/OI/basis） | ✅ 支持 | 字段集来自 `stream.fields`，不硬绑 OHLCV；只要 read 产出该字段且注册表 lag 一致，align 自动纳入 P4/P5 与质量报告（`:124,192,245`） |
| 提高因果检查强度（加 P6） | ⚠️ 需改代码 | checks 是硬编码列表（`:134-220`），加检查点要改 align；但 enum 只有 none/strict 两档，加档需改 schema + 代码 |
| **事件流 as-of ffill-only 对齐** | ❌ 不支持（本层） | 属 read/供给层职责；align 不改值、只裁行。C8/C9 的前置在 read，不是 align |
| **表达"末端 k 根未收盘"** | ❌ 不支持 | `end_closed` 是单布尔、只丢一根（A-4）；需扩 Pin 数据模型 |
| 质量门禁加规则（如 turnover） | ❌ 需改代码 | 门禁写死两项（`:257-277`），无注册点 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不算年化）；`step` 用 `median(diff(times))`（`:208`）按观测推，无 252 依赖 ✅
- **C5 计价货币**：不适用（不读价格语义）
- **C7 24/7 缺口**：✅ 强相关——未收盘靠 `end_closed` 事实（`:111`）；数据洞靠 P5 连续性 fail-closed（`:208-220`），不靠日历
- **C8 开放列集**：✅ align 字段集来自流，不硬绑（`:124,192`）
- **C9 as-of ffill-only**：⚠️ 本层不改值、不填充；前视源头防在 lag 门禁（`:140-157`），真正 as-of 对齐在 read 层（见 03_read）
- **C11 流动性分层**：不适用（沿用 boundary 的 Scope，不重算）

## R1–R7 影响

- **R1 真数据跑通**：✅ 支撑（6 次真跑全绿，含未收盘/洞/门禁路径）
- **R2 结论可信**：✅ 强支撑——前视三道门禁（lag/保留区/逐位+连续性）全部实测能真失败
- **R3 因子零代码扩展**：✅ 正向——字段集开放，新流能自动进面板（受 read 层 as-of 对齐能力制约）
- **R5 单因子可归因**：中性（只产面板，不涉归因）
- **R7 fail-closed**：✅ 强支撑——lag 不符/保留区泄漏/洞/空面板/质量超限全拒，strict 不降级

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 四处 P2 均为"说明缺口"或"跨入口/数据模型限制"：A-1 P4 边界（面板忠实于流≠流本身无前视，源头在 read）、A-2 未收盘门禁由构造保证、A-3 参数缺省两入口不一致（并入 U4）、A-4 只丢末端一根（受 Pin 布尔数据模型限制，上游议题）。因果探针已按历史事件改为能真失败的三项，符合"生产可用=研究员能真挖因子"。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 3 个模块 `read.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
