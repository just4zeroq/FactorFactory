# 模块审查 06 · `capability/stream.py`（642 行）

> 审查日期：2026-11-18
> 依据：源码 642 行全文通读（分两段）；定义 `Pin`/`SupplyBatch`/`FieldStream`/`Scope` 四个供给侧值对象；被 `read`/`boundary`/`align`/`label` 全面依赖；设计 `s0/01-data-contracts.md` §3.3、`s0/04-capability.md` §3.1–3.3、`10-crypto-fit.md` §7.2（合约四维）、`11-field-operator-registry.md`；测试 `test_v5_boundary_align.py:146-294`（码表/Scope 不变量）、`test_v5_read.py`（版本身份）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Stream 定义供给侧四个不可变值对象，对应"数据是哪个版本 / 字段长什么样 / 哪些格能用"：

1. **`Pin`**：版本引用，不是数据。只回答"哪次采集、覆盖到哪、末端是否收盘"。`end_closed=false` 可存在于 Depot，但不得被别名指向。`checksum` 必须是可解析内容摘要（空串会关掉完整性校验）；`gate_a` 未过即拒（`E4005`，与门禁 B 同码）。
2. **`SupplyBatch`**：事实层↔能力层**唯一表通道**。执行器读 parquet、验指纹、转列后构造它；能力层因此不需要 pyarrow/pathlib（AC31），也没有"顺手打开另一个版本"的位置。`source_ref.digest == pin.checksum` 必须成立（`E4004`）——路径与版本指向同一内容。
3. **`FieldStream`**：`read` 产物，字段数组 + 逐字段供给元信息。`manifest` 的 `available_lag` 是注册表字段、不可在流里改写（改写=把前视藏进数据层）；`digest()` 含 manifest 与 symbol_classes（换 lag 版本在缓存层可见）。
4. **`Scope`**：`boundary` 产物，两张分开的掩码 + 分层 + 逐格理由码。核心不变量 `tradable ⊆ observable`——"不可观测却能下单"是 V4 最难查的错误（持仓表与成本表各算一套）。理由码一格一个、从 legend 派生可观测码（`OBSERVABLE_CODES`），新增码自动跟上。

**加密特化**：`OBSERVABLE_CODES = 全部码 − 不可观测码`，unclosed/low_liquidity/stable 可观测但不可交易（加密里未收盘价、稳定币、低流动性币都属此类）；`symbol_classes` 区分 spot/perp/stable/leveraged（结构性排除 stable 下单）。**合约四维身份 `(exchange, underlying, contract_type, settle_ccy)` 当前缺失**——`SYMBOL_CLASSES` 只有 4 值、无 `expiry`/dated，期限结构与跨合约横截面不可做（C4，全局 P0-5）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 用 `venv/Scripts/python.exe` 调用真实构造与方法**实测打印**。复现按 `tests/tests/test_v5_boundary_align.py:55-100`、`test_v5_read.py:48-86`；临时脚本在系统临时目录，未改仓库。

### [A] 码表与不变量（实测）

```
REASON_CODES = {valid:0, reserved:1, not_listed:2, delisted:3,
                missing_data:4, unclosed:5, low_liquidity:6, stable_or_leveraged:7}
OBSERVABLE_CODES = [0,5,6,7]   = 全部码 − _UNOBSERVABLE{1,2,3,4}
SYMBOL_CLASSES = ['leveraged','perp','spot','stable']   ← 4 值，无 expiry/dated/settle（C4 缺失）
码 5/6/7 的 (可观测, 不可交易) = [(5,True,True),(6,True,True),(7,True,True)]
```
→ 证实"可观测≠可交易"三码分离；新增理由码只要进 `REASON_CODES` 且不在 `_UNOBSERVABLE`，observable 掩码自动派生（`stream.py:59-65`）。

### [B] Pin 构造与校验（实测）

```
Pin(dataset='ohlcv', version='v1', checksum='sha256:7c62...',
    coverage=(1704067200000000000, 1706659200000000000), symbols_digest='s0', end_closed=False)
Pin.digest() = sha256:162784212c08810c476eb23608d12bb4d...
```
失败路径（实测）：
| 非法输入 | 输出 |
|---|---|
| `checksum="bad"` | `E4004` "pin.checksum 必须是 'sha256:<64 位小写十六进制>'，得到 'bad'" |
| `gate_a={"passed":False,"reason_code":"X"}` | `E4005` "pin.gate_a 未通过（'X'）" |
| `coverage=(START,START)` | `E4002` "pin.coverage 必须 start < end" |
| `end_closed="false"`（字符串） | `E4002` "pin.end_closed 必须是布尔" |

### [C] FieldStream 校验（实测）

```
FieldStream symbols=('S0','S1','S2') shape=(30,3)
symbol_classes = {'S0':'spot','S1':'perp','S2':'stable'}
manifest = {'close':{dataset:'ohlcv',version:'v1',unit:'quote',available_lag:0},
            'volume':{...,unit:'base',available_lag:0}}
missing_ranges = []   coverage = {start:1704067200000000000,end:1706572800000000000,bars:30,symbols:3}
FieldStream.digest() = sha256:df5b8fad76730ec3724bee5de3a863b9a...
```
失败路径（实测）：
| 非法输入 | 输出 |
|---|---|
| `available_lag=-1` | `E1014` "字段 'close' 的 available_lag 必须是非负整数，得到 -1" |
| 缺 `unit` | `E1014` "字段 'close' 缺 unit" |
| `symbol_classes` 含未知标的 'S9' | `E1014` "symbol_classes 含未在清单中的标的 'S9'" |
| 类型 'future'（不在 4 值内） | `E1014` "标的 'S0' 的类型 'future' 不在 ['leveraged','perp','spot','stable'] 内" |

### [D] Scope 不变量 `tradable ⊆ observable`（实测）

```
Scope shape=(30,3)  legend = 8 个码
```
失败路径（实测）：
| 非法构造 | 输出 |
|---|---|
| observable 全 False、tradable 全 True | `E3003` "tradable_mask 不是 observable_mask 的子集" |
| `reason_codes` 含未登记码 99 | `E3003` "reason_codes 含未登记的码 [99]" |
| reserved(码1) 格进 observable | `E3003` "有 1 个格被标为不可观测的理由码却进了 observable_mask" |
| `tiers[0,0]=256`（int64 传入） | `E3003` "tiers 档位 256 超出 int8 表示范围" |
→ 先验窄化（先查值域再 `astype int8`）避免 256 静默变 0 码/0 档让不变量失效（`:562-584`）。

### [E] SupplyBatch 版本身份（实测）

```
SupplyBatch rows=2 symbols=('S0',) digest=sha256:455ff958b0758a73acc...
source_ref.digest(sha256:other) ≠ pin.checksum → E4004 "source_ref.digest 与 pin.checksum 不一致"
```

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| S-1 | **合约四维身份缺失（C4 / 全局 P0-5）**：`SYMBOL_CLASSES` 只有 spot/perp/stable/leveraged（`:67`），无 `underlying/contract_type/expiry/settle`。结果：无法区分 `BTCUSDT` 与 `BTCUSDT_250926`，期限结构/跨合约横截面不可做。 | **P0（全局，非本模块单独可补）** | `stream.py:67,431-445,506-508`；`10-crypto-fit.md:198-209,288` | **已确认缺口**：需供给层 ccxt markets 元数据 + 四元组进 Pin/FieldStream/Scope + 全仓 symbol 键改造，是全局 P0-5。本模块是落点之一但不是唯一。 |
| S-2 | **`market∈{spot,futures}` 维度未显式化**：`dataset` 隐含市场（funding∈futures），但 `Pin`/`FieldStream` 无 `market` 字段、无第二道断言；spot 混入 funding 字段由供给层护栏拦，能力层没有。 | P2（责任划分） | `stream.py:126-211`（Pin 无 market）；对照 C3 | **需确认意图**：同 boundary B-1。若设计把 market 责任放供给层则合理；但文档 `10-crypto-fit` §7.2 的合约维度在能力层确实缺（并入 S-1）。 |
| S-3 | **`leveraged` 类的判定来源**：`symbol_classes` 是载荷携带的映射，能力层只校验值合法、不从合约类型自动推断；一个杠杆代币若供给层漏标，会被当普通 spot 下单。 | P2（依赖上游标注） | `stream.py:431-445,309-317` | **需确认**：能力层不读盘、信任供给标注是设计；但需要供给层保证标注完整（U3 职责），非 stream 单独可补。 |
| S-4 | **`FieldStream.digest` 含 manifest/symbol_classes、Scope.digest 含 axis/legend，但 `Pin.digest` 不含 `acquired_at`/`schema_version`/`gate_a`**：两个 acquired_at 不同的版本指纹相同。 | P2（指纹范围） | `stream.py:213-227` | **需确认意图**：acquired_at 是采集时间、不影响"读哪批数据"的内容身份；gate_a 已通过才进 Pin（未过直接拒）。范围合理，非 bug。 |

**多了什么**：无冗余。`OBSERVABLE_CODES` 从 legend 派生（不抄字面量）是刻意的 G9 防漂移设计；`SupplyBatch` 的 `_symbols/_rows` 派生字段避免每次重扫（`:251-253,306-307`），合理。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| ccxt `markets.contractType/expiry/settle` 元数据 | **应借鉴（当前缺）** | 合约四维身份是加密特有；现在只有 4 值 symbol_classes，无 dated 合约（S-1/P0-5） |
| 内容寻址（checksum ↔ source_ref.digest、四个 digest） | 采纳 | 路径与版本必须同一内容（`E4004`，`:321-327`）；manifest/lag/symbol_classes/axis/legend 都进指纹，换语义即换指纹（`:485-509,621-642`） |
| 不可变值对象 + 先验窄化（值域先查、再 astype） | 采纳 | 256→0 码静默失效是最阴险的一类；先验让不变量在类型层成立（`:562-584`） |
| 两张掩码合成一张 | **拒绝** | 未收盘/低流动性/稳定币"可观测不可交易"是加密常态，合成一张二选一都错（`:9-16,516-518`） |
| OBSERVABLE_CODES 抄字面量 | **拒绝（已改派生）** | 新增理由码时抄字面量会漏改、observable 掩码不跟上；从 legend 派生改一处即可（`:59-65`，G9） |
| 能力层自己读 parquet / 发现"最新"版本 | **拒绝（AC31）** | 打开文件留事实层；SupplyBatch 是唯一表通道、无"顺手换版本"的位置（`:18-20,230-242`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **`tradable ⊆ observable` 不变量**：`Scope.__post_init__` 强制（`:586-592`），实测 tradable⊄observable 即 E3003；另两层校验"不可观测码不进 observable、不可交易码不进 tradable"（`:593-606`）。
2. **理由码先验窄化**：先查未登记码、再窄化 int8（`:555-570`），tiers 同理查 `>127`（`:572-584`）——避免 256 静默变合法码/档。
3. **OBSERVABLE_CODES 派生**：`frozenset(全部) − _UNOBSERVABLE`（`:63-65`），实测 = [0,5,6,7]。
4. **Pin 完整性**：dataset/version 非空、checksum 形态（`E4004`）、coverage 整数且 start<end、end_closed 布尔、gate_a 未过 `E4005`（`:140-180`）——实测五类拒。
5. **契约层 Pin ↔ 能力层 Pin 唯一转换点**：`from_contract` 一处（`:182-211`），避免"同一版本"在不同入口换成不同覆盖（差时区/单位不报错）。
6. **SupplyBatch 身份**：各列等长、非空、必备列、symbol≠time 列、`source_ref.kind=supply`、`digest==checksum`（`:255-327`）——实测身份不符 E4004。
7. **FieldStream manifest 只认合法**：每字段必须有 manifest、lag 非负整数（拒 bool）、unit 非空（`:410-429`）——实测三类 E1014；`available_lag` 不从载荷改写（`:280-282` 注释）。
8. **`missing_ranges` 派生成而非构造入参**：`FieldStream.missing_ranges` property 现算（`:475-483`），避免"改了数组忘了改报告"。
9. **四个 digest 都含结果相关语义**：Pin/SupplyBatch/FieldStream/Scope 的 digest 分别含各自会改变结果的字段（manifest/lag/symbol_classes/axis/legend 均进），实测。
10. **`interior_missing_runs` 只报首末有效之间的全字段缺失**（`:70-112`），结构性空段不算缺口。

**发现的问题**：
- 无 P0/P1 级本模块代码 bug。S-1（合约四维）是全局 P0 缺口、本模块是落点之一；S-2/S-3/S-4 为 P2（责任划分/上游依赖/指纹范围）。测试覆盖充分（码表/Scope 不变量 `test_v5_boundary_align.py:146-294`、版本身份 `test_v5_read.py`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/01-data-contracts.md` §3.3 / `s0/04-capability.md` §3.1-3.3 | 四值对象、两掩码、理由码、分层 | 与实现一致，无需更新 |
| `10-crypto-fit.md` §7.2 | 合约四维身份列为缺口 | 待全局 P0-5 落地后，回头更新 `SYMBOL_CLASSES`（`:67`）码表与四元组；本模块是落点之一 |
| `s0/04-capability.md` 理由码表 | 8 码、可观测/可交易两列 | 与 `REASON_CODES`/派生一致；新增码时同步 legend（G9） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增理由码（如 `expired`） | ⚠️ 半配置化 | 在 `REASON_CODES` 加码 + 归类 `_UNOBSERVABLE/_NOT_TRADABLE`（`:46-65`）；observable 掩码自动派生，但 boundary 需新增判定、文档同步 |
| 新增 symbol_class（如 `dated`） | ⚠️ 半配置化 | `SYMBOL_CLASSES` 加值 + 供给层标注（`:67,439-444`）；但**仅加分类≠有四维身份**（S-1，无 expiry） |
| **合约四维身份（期限结构分组）** | ❌ 不支持 | 全仓零实现（S-1/P0-5）；需供给层元数据 + symbol 键全仓改造 |
| **market=spot/futures 护栏** | ❌ 不支持零代码 | Pin/FieldStream 无 market 字段（S-2）；当前仅供给层有护栏 |
| 新增分层策略 / 字段元信息 | ❌ 需改代码 | 分层量在 boundary 写死；manifest 结构固定（dataset/version/unit/lag） |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（值对象）
- **C3 spot/futures 维度**：⚠️ 用 dataset 隐含市场、无 market 字段断言（S-2，责任在供给层）
- **C4 合约四元组**：❌ **无**（S-1，全局 P0-5；本模块是落点之一）
- **C5 计价货币**：不适用
- **C6 上市/下架**：✅ not_listed/delisted 由理由码 2/3 表达（`REASON_CODES`，`:49-50`）
- **C7 24/7 缺口**：✅ unclosed 理由码 5 靠供给事实 `end_closed`，不靠日历（`:52`）
- **C9 as-of lag**：✅ `available_lag` 只从注册表取、进 manifest、进 digest（`:280-282,496-505`），是防前视源头
- **C11 流动性分层**：✅ low_liquidity 码 6 + tiers 数组，可观测不可交易（`:53,63-65`）

## R1–R7 影响

- **R1 真数据跑通**：✅ 支撑（四值对象是全链基础，真跑反复验证）
- **R2 结论可信**：✅ 强支撑——两掩码不变量、理由码先验窄化、lag 进指纹，实测全闭合
- **R3 因子零代码扩展**：❌ **负向**——合约四维（C4/S-1）缺失锁死期限结构/跨合约因子，是全局 P0；symbol_classes 4 值不够
- **R5 单因子可归因**：✅ tiers/掩码结构正确，分层回测口径可靠
- **R7 fail-closed**：✅ 强支撑——checksum/版本身份/manifest/Scope 不变量全实测拒

---

## 结论

**本模块是供给侧不变量的核心守卫、无 P0/P1 自身 bug，可确认通过。** `tradable⊆observable` 不变量、理由码先验窄化（256 不静默变 0）、`OBSERVABLE_CODES` 从 legend 派生、manifest lag 只从注册表取且进指纹——全部实测正确。关键缺口是**合约四维身份（C4/S-1，全局 P0-5）**：`SYMBOL_CLASSES` 仅 4 值、无 expiry/dated，锁死期限结构与跨合约因子，本模块是落点之一但需供给层 ccxt 元数据 + 全仓 symbol 键改造协同，非单独可补。S-2/S-3/S-4 为 P2。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 7 个模块 `timeaxis.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
