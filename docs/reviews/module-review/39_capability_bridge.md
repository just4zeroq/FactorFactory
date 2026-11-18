# 模块审查 39 · `orchestration/capability_bridge.py`

> 审查日期：2026-11-18
> 依据：源码 1481 行全文通读；依赖 `capability/`（Call/InputRef/run、spec.method_id、runtime.impls）、`storage/objects.py`（ObjectStore）、`contracts/digest.py`（compute_digest/array_digest）。设计 `s0/05-orchestration.md` §2.1、§3.1、`s0/01 §2.2`（六通道）、`s0/04 §1.3`（落盘/对象拆分）、`03-orchestration.md`、`06-evaluation.md`。测试 `test_v5_executor_capability.py`（468 行）、`test_v5_s5_single_step.py`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_bridge.py`（BR 段）与 `probe_bridge2.py`（M2 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

CapabilityBridge 是 **executor 唯一调用能力的适配层**（`:1-9`）：编排层有权（构造 Call 六通道、解析 InputRef.data、拆落盘视图/内存对象），能力层无权（不读盘、不写账本、不产结论）。三个不可省的动作：

1. **落盘投影**（`:13-16,1158-1176`）：能力返回的 `opaque`/`array` 字段（Panel/Scope/ndarray）只在内存传，不进 `Parcel.payload`（后者要 `json.dumps`+`compute_digest`，会拒这些类型）；落盘视图只留标量/ref/map/list。
2. **内存对象表**（`:17-19,264-274`）：data 通道对象按 kind 存对象表，下游按声明 kind 取用放 `InputRef.data`；data 不进指纹（`runtime.py:49 compare=False`），指纹链仍由 digest 驱动。
3. **fail-closed**（`:20-21,1050-1057`）：未实现能力（certify S7 等）由 run 抛 E3004；缺上游对象/类型不符 E3003——绝不返回空产物。

**工位→能力映射**（数据表，`:46-58`）：Boundary→boundary、Panel→align、Target→label、Search→search、Evidence→observe、Verdict→verdict、Portfolio→combine、Certify→certify。非法工位无映射 E1004。

**六通道 Call**（`:1108-1113`）：`impl@version`、params、inputs、policy_view（按 policy_reads 裁剪，`:1103-1106`）、runtime_facts、composed/seed/phase——全部进 call_digest（经 contracts.digest）。

**对象输入解析优先级**（`_resolve_input`，`:299-334`）：shared > 上游对象 > 派生（price 从 panel.close）> supply_fn 注入；外部 kind（pin/field_stream/supply/market_view）必须由 supply_fn 注入，否则 E3003。

**data 落 Ref / exposures**（`:377-421,1165-1176`）：数据通道对象写 ObjectStore（`parcel_data` 类）得内容寻址 `<field>_ref` 进 payload；ndarray→parquet、值对象→json 摘要、list/dict→json；>2D 拒（E3003，读侧无法还原形状）。**能力原生 `*_ref` 不被派生覆盖**（`:1161-1174`）：先收 `native_keys=set(returned)`，派生时只在 `ref_key not in native_keys` 才写——否则同一对象两个 digest，下游按 ref 取回内容与能力声明身份不符。

**三处 backtest 调用点 funding 透传**（用户必查点）：
- `:639-656`（S4 单因子成本链 `_run_single_factor_costs`，Evidence 逐候选）：`include_funding + InputRef("funding")`；
- `:782-795`（组合尾链 `_run_portfolio_tail`，Portfolio）：同上；
- `:966-980`（逐折认证 `_run_certify_folds`，Certify）：同上。
三处都经 `_funding_input`（`:505-529`，从 supply 注入事件网格、`_align_to_panel` 机械对齐、不重算不插值）取 funding，params 与 InputRef 双透传。

**`use_capabilities` 两路径**（`:24-27` 设计）：默认 False 走 dummy 桩（S0/S1/S2 与无供给场景可用）；True 经 bridge 调真实能力。两条路径共用调用链、指纹链、落盘逻辑（executor 层保证）。

**M2 单步跨进程重建**（`:1305-1410`）：`rebuild_upstream` 从冻结产物重建对象表——JSON 直取（observe/verdict）、重推导+payload_digest 逐字节对拍（boundary/align/label/combine，E3002）、Search 不重跑（append_only）暴露重算。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测。`probe_bridge.py` 用 `_stream()`（90 bar × 8 标的，2024-01-01 起）与 `_pin()`，`CapabilityBridge(supply_fn=...)` / `Executor(use_capabilities=True)`。

### 例 1：工位→能力映射 + 落盘投影（真实输出）

```
STATION_CAPABILITY = {'Boundary':'boundary','Panel':'align','Target':'label','Search':'search',
                      'Evidence':'observe','Verdict':'verdict','Portfolio':'combine','Certify':'certify'}

BR-2 boundary.invoke（无 ObjectStore）：
  memory_object_keys = ['observable_mask','reason_codes','scope','tiers','tradable_mask']
  disk_payload_keys  = ['axis_resolved','reason_code_legend']
  scope_in_payload   = False    observable_mask_in_payload = False
  payload_jsonable   = True
```
读法：5 个数据对象走内存通道，落盘视图只剩 2 个标量/map 字段，可 json.dumps、可 compute_digest。

### 例 2：data 落 Ref + 原生 `*_ref` 不被派生覆盖（真实输出）

```
BR-3（有 ObjectStore）：
  boundary_refs = ['observable_mask_ref','reason_codes_ref','scope_ref','tiers_ref','tradable_mask_ref']
  panel_native_ref_preserved = True     # p.payload["panel_ref"] == p.objects["panel"].digest()
  panel_refs    = {'panel_ref':'sha256:833791dd8187018f4...', 'time_index_ref':'sha256:1f5ca807bba7bcc4a...'}
  ref_files_exist = True                # facts/parcel_data/ 下真文件
```
读法：有 store 时数据通道落 `<field>_ref`（不再静默丢弃），且 align 原生返回的 `panel_ref` 原样保留，未被 object_store 对 panel 对象的派生写入覆盖——同一对象只有一个身份。

### 例 3：三处 backtest funding 透传（真实输出，spy capability_run）

```
state = done    backtest_call_count = 5
calls = [
  {capability:backtest, include_funding:True, has_funding_ref:True, funding_is_ndarray:True, station:Evidence},   ← :645 单因子成本
  {capability:backtest, include_funding:True, has_funding_ref:True, funding_is_ndarray:True, station:Evidence},
  {capability:backtest, include_funding:True, has_funding_ref:True, funding_is_ndarray:True, station:Portfolio},  ← :785 组合尾链
  {capability:backtest, include_funding:True, has_funding_ref:True, funding_is_ndarray:True, station:Certify},    ← :970 逐折认证
  {capability:backtest, include_funding:True, has_funding_ref:True, funding_is_ndarray:True, station:Certify},
]
all_include_funding_true = True
all_have_funding_ref      = True
all_funding_is_ndarray    = True
```
读法：注入 funding 事件网格后，5 次 backtest 调用（Evidence 2 次逐候选 + Portfolio 1 次 + Certify 2 折）**全部**带 `include_funding=True` 且 `funding` InputRef 指向真实 ndarray——三个调用点 funding 透传实测成立。未注入时 `include_funding=False` 且不放 InputRef（源码 `:645/:785/:970` 的条件表达式）。

### 例 4：`use_capabilities` 两路径（真实输出）

```
dummy:  state=done  Boundary payload keys=['kind','payload','station']
real:   state=done  Boundary payload keys=['axis_resolved','reason_code_legend']
dummy/real Certify.call_digest 长度均为 71 （sha256: + 64 hex）
```
读法：两路径都跑完八工位、都产出真指纹；dummy 产物是桩结构，real 是能力投影；切换开关不改指纹链结构（同一调用点）。

### 例 5：M2 单步跨进程重建 digest 对拍（真实输出，probe_bridge2）

```
M2-2 rebuild_upstream（只重建 7 个上游工位）→ 全过，无 E3002
  rebuilt_stations = [Boundary, Panel, Target, Search, Evidence, Verdict, Portfolio]
  每工位对象：Boundary=[observable_mask,reason_codes,scope,tiers,tradable_mask], Panel=[panel],
             Target=[label,raw,score,valid_mask,width_series], Search=[candidate_set], ...
M2-3 篡改磁盘 Boundary payload.json → E3002|Boundary 的 payload 指纹与声明不符（内容被改写）
```
读法：重推导结果与冻结产物的 payload_digest 逐字节对拍全过；磁盘篡改被 `load_committed_parcel` 重算 digest 拒绝（E3002），不是信 parcel.json 就放行。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| B-1 | **Search 产物不重跑但暴露重算依赖 evaluate 决定论**：`_rebuild_search_objects`（`:1397-1410`）用 `_build_exposures` 重算暴露，若算子版本变化导致 evaluate 结果不同，重算暴露与冻结 payload 的候选身份可能错位，而对拍只比 payload_digest（候选取自 payload，暴露不进 payload）。 | P2 | `capability_bridge.py:1397-1410,1378-1393` | **需确认意图**：设计假设 evaluate 对同一表达式决定论（注释 `:1403-1404`）；若算子升级，重算暴露变但 payload 对拍仍过 → 单步结论与 run 不同源。属版本一致性前提，当前算子固定。 |
| B-2 | **三处 S4 尾链是"机械接续"但种子来自调用方**：`:631/:794/:979` 的 `seed=seed` 是工位级 seed，逐候选/逐折复用同一 seed；backtest 若用 seed 做随机（当前不用）会跨候选相关。 | P2（低） | `capability_bridge.py:617-655,761-795,946-980` | **已确认当前安全**：allocate/backtest 是确定性的（分位权重、成本口径），不消费 seed。若将来引入随机撮合需按候选/折派生子种子。 |
| B-3 | **Portfolio/Evidence 的 prior_objects 走 shared 旁路**：executor 把全部已产工位对象塞 `shared["prior_objects"]`（`:380-384`），桥按 kind 机械搬运（`:208-231`）；这些非直接上游对象的变化不进本工位 input_digest（注释 `:375-379` 论证 Boundary 变化必经 Panel 传递）。 | P2（指纹覆盖） | `capability_bridge.py:374-384,208-231`；`executor.py:380-384` | **已确认符合设计**：input_digests 只取 spec 声明上游；Boundary→Panel→...的指纹链已覆盖传递。属"信任链传递"而非"逐边直连"，需接受这个信任模型。 |
| B-4 | **`_scoped_view` 收口三处裁剪规则**（`:1444-1454`）：曾有三份局部 `scoped()` 函数，现收口一处。但 `invoke` 内又有一份等价裁剪（`:1105-1106` 的 `declared/set(...)`）未走 `_scoped_view`。 | P2（轻微重复） | `capability_bridge.py:1103-1106` vs `:1444-1454` | **需确认**：两处逻辑相同（都按 policy_reads 裁剪），但未来规则若改（如加默认子块），两处会分叉。建议 invoke 也调 `_scoped_view`（本轮不改代码，记录）。 |

**多了什么**：无功能冗余；`_scoped_view` 收口（B-4 提及的第二处除外）与 `native_keys` 防覆盖都是必要防线。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 编排层构造六通道 Call、能力只 run | 采纳 | "改变结果的东西必须进指纹"——工位块/params/policy/facts/seed 全进 call_digest |
| 落盘视图与内存对象分离 | 采纳 | ndarray/Panel 不可 json.dumps，且 data 不进指纹（runtime compare=False） |
| 内容寻址 Ref（`parcel_data` 类） | 采纳 | 数据落盘得 digest，下游按 Ref 重建；>2D fail-closed（读侧无法还原形状） |
| 原生 `*_ref` 优先、派生只填空缺 | 采纳 | 同一对象一个 digest（实测 panel_ref 保留），防能力身份与落盘身份分家 |
| 能力自建子调用 / 自己读配置 | **拒绝** | 能力不得自建子调用（s0/04 §1.3）；allocate/backtest 由桥在同一 caliber 下机械接续 |
| 未实现能力回落到桩成功 | **拒绝** | E3004 fail-closed（`:1050-1057`），"S4/S7 未做"不伪装成"数据没准备好" |
| 缓存默认全开工位 | **拒绝（executor 层）** | 命中时内存对象表为空会让下游拿到空对象；opt-in 白名单（见 40 号 CA-1） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **八工位映射表**：BR-1 实测 8 项，无能力 E1004（`:1045-1049`）。
2. **落盘投影**：BR-2 实测 data 对象不进 payload，payload 可 json.dumps/compute_digest（`:1158-1176`）。
3. **data 落 Ref**：BR-3 实测 5 个 `*_ref` 都是 sha256 且真文件存在（`:377-421`）。
4. **原生 ref 不被覆盖**：BR-3 实测 `panel_ref == panel.digest()`（`:1161-1174`，`native_keys` 守卫）。
5. **三处 funding 透传**：BR-4 实测 5 次调用全覆盖（`:639-656,782-795,966-980`）。
6. **两路径共享链**：BR-5 实测 dummy/real 都跑完八工位、指纹长度一致。
7. **未实现能力 E3004**：`invoke` 先查 installed_impls 再解析输入（`:1050-1057`），防 E3003 误导。
8. **实现名解析**：单实现忽略错误声明 E1002、多实现按 plan 声明（`:336-375`）；`declared=True` 才校验"声明名真存在"。
9. **可选项落 None**：`:1082-1093`，可选输入（allocate.price）取不到落 None，必填照抛——能力按口径自己判断。
10. **policy_reads 裁剪**：`:1103-1106`，能力只读声明过的口径子块，越权 E2001 由 runtime 兜。
11. **M2 重建对拍**：probe_bridge2 M2-2 全过、M2-3 篡改 E3002（`:1305-1395`）。
12. **exposures 子调用留痕**：`_build_exposures`（`:423-503`）逐候选 evaluate，失败写 `unobservable` 清单，Search count 与 Evidence per_candidate 差额可指名（V5 首跑 65→57 事故回归）。
13. **派生 label 通道带 raw/horizon_bars**：`:1241-1261`，防 observe 默认 horizon=1 导致 √h 虚高（B4/AC63）。

**发现的问题**：B-1~B-4 均为 P2/需确认意图或轻微重复，**无 P0/P1 级代码 bug**。测试覆盖落盘/内存拆分、Ref、原生 ref 保留、实现名解析、未实现能力拒绝、两路径、零晋升跳过、时间轴透传、Budget 真值、unobservable 留痕（`test_v5_executor_capability.py` 468 行）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §3.1（落盘接线） | 描述 data 落 Ref | 与实现一致，无需更新 |
| `s0/04 §1.3`（对象/落盘拆分） | 描述内存对象与落盘视图 | 一致，无需更新 |
| `06-evaluation.md`（单因子净成本） | 描述同 caliber 成本链 | 一致；可补"三处 backtest 均透传 funding，受 caliber.net.fee_model 最终把关"（B-2 关联说明） |
| `03-orchestration.md`（六通道） | 描述六通道 | 一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新工位通过六通道接入 | ⚠️ 需改多处 | STATION_CAPABILITY + executor.STATIONS + station.py 规格表 + schema；映射是数据表但非纯配置 |
| 新能力（如新合成器）接入 | ⚠️ 半配置化 | 在 capability 注册表加实现 + 必要时改 `_resolve_impl`；open/closed_multi 按 plan impl 名匹配（`:364-375`） |
| 资金费/新数据流进 backtest | ✅ 已支持 | supply_fn 注入 funding → `_funding_input` 对齐 → 三处透传（实测）；新流需加 kind 与 schema |
| 高维数据（>2D）落盘 | ❌ 明确拒绝 | `_write_ref` 只支持 1D/2D，>2D E3003（`:400-406`）；需先降维 |
| S7 全能力实现（certify 主能力） | ⚠️ 当前 certify 主能力已接，但"未实现能力"守卫 ln1050 仍对任何空 bucket 生效 | 当前 installed_impls 已有 certify；空 bucket 能力仍 E3004（fail-closed 保留） |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 间接支撑——`_panel_times` 把真实 bar 时间喂 backtest（`:192-205`），`bars_per_year` 由 bt_out 带回（`:1200,1215`），避免退回日频 √24 错误；`CALENDAR_DAYS_PER_YEAR` 来自 timeaxis 365（`:43`）
- **C2 资金费按事件**：✅ 强支撑——`_funding_input` 读 supply 已求和的事件网格（`:505-529`），三处 backtest 透传（实测），不按周期常数乘
- **C3 spot 无资金费**：⚠️ 桥只搬运 funding 数据，`include_funding` 最终由 `caliber.net.fee_model` 把关（`:610-612` 注释）；spot 计划不该有 funding 注入，能力层无第二道断言（全局 C3 责任在 supply/gate_b）
- **C4 合约四元组**：❌ 无（全局 P0-5）；对象表按 kind 不按合约身份分组
- **C5 BTCUSDT≠BTCUSD**：❌ 无（计价货币维度未处理）
- **C6 上市/下架**：✅ Boundary 的 tradable/tiers 经 `prior_objects` 进成本链与认证（`:564-571,817-825`），对齐到面板子集
- **C7 24/7 缺口**：✅ `_align_to_panel` 用 searchsorted 判子集（`:98-134`），非子集 E3003；不 ffill/bfill
- **C8 列集开放**：⚠️ 价格取 `panel.field("close")` 写死（`:320-323,561-563`）；新字段（funding/OI/basis）经 supply 注入，但"价格"概念固定为 close
- **C9 as-of 对齐**：✅ 桥不做填充，只机械对齐（`:98-134`）；缺时间轴 E3003（`:192-205`）
- **C11 流动性分层**：✅ tradable_mask/tiers 全程进 allocate（`:623-629`）与 backtest（`:652,791,952`）
- **C12 观测/embargo**：✅ 逐折切片 `fold_score=score[b0:b1]`（`:938-944`），embargo 由 certify 折边界决定，桥不折算

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（`--live` 全链真实能力串通，BR-5 实测 done）
- **R2 结论可信**：✅ 强支撑（同 caliber 成本链、funding 透传、原生 ref 单身份、净成本替换零成本占位）
- **R3 因子零代码扩展**：✅ 正向（六通道 + supply_fn 注入让资金费/OI/basis 不改桥代码即可进链；合约身份仍全局缺口 C4）
- **R5 单因子可归因**：✅ 强支撑（逐候选暴露 + unobservable 留痕，差额可指名）
- **R7 fail-closed**：✅ 强支撑（未实现 E3004、缺对象 E3003、>2D 拒、篡改 E3002、非子集 E3003）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 全部必查点以真实调用坐实：工位→能力映射（BR-1）、六通道 Call、对象表与落盘投影（BR-2）、data 落 Ref/exposures（BR-3）、**能力原生 `panel_ref` 不被派生覆盖**（BR-3 `panel_native_ref_preserved=True`）、**三处 backtest funding 透传**（BR-4 五次调用全 `include_funding=True` + funding ndarray）、`use_capabilities` 两路径共享链（BR-5）、M2 重建对拍全过与篡改 E3002（probe_bridge2）。四项 P2 均为前提/边界或轻微重复：B-1 Search 暴露重算依赖 evaluate 决定论（算子版本固定下成立）、B-2 尾链 seed 复用（当前能力确定性）、B-3 prior_objects 旁路（指纹链传递已覆盖）、B-4 invoke 内裁剪未走 `_scoped_view`（逻辑等价，建议收口）。**生产可用（能真挖因子）= 是**：本模块是真实能力接入的核心枢纽，全链可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 40 个模块 `executor.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 B-4（invoke 裁剪是否统一走 _scoped_view）→ 告诉我
