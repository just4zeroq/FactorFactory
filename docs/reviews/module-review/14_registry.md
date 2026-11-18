# 模块审查 14 · `capability/registry.py`

> 审查日期：2026-11-18
> 依据：源码 235 行全文通读；真相源 `registry/{fields,operators,params}.json`（9 字段 / 19 算子 / 12 能力 schema）；依赖 `contracts/digest.py:compute_digest`、`contracts/errors.py`（E1002/E1015/E2003/E2006）；消费方 `mining._OpCodes:256-274`、`runtime/compiler.py` 步骤 5；设计 `11-field-operator-registry.md §4.1/§5`、`s0/04-capability.md §5.1`；测试 `test_v5_capability.py:41-153`、`test_v5_package_isolation.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响；"生产可用=能真挖因子"（plan §0.0/§0.1）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Registry 是字段/算子契约数据的**唯一加载器 + 自检器 + 指纹器**，回答四件事：

1. **JSON 是真相，代码只读**：字段白名单、算子表、参数 schema 只在 JSON 里定义；代码里不得再写字符串白名单常量——那会与 JSON 各说一套（`:2-4`）。加载即算内容哈希（字段 `fields_digest`、算子 `operators_version`）供编译期对账。
2. **加载即自检、整体失败（E1015）**：负位移、窗口/位移 ≤ 0、`center` 居中窗口、`bfill`/`lead`/`future` 因果禁令、缺 `impl_symbol`、常数 `degenerate_policy`、三份 JSON `schema_version` 漂移、字段/算子重名——任何一条不满足整体拒，不跳过单条（"跳过=悄悄删掉"，`:6-7,110`）。
3. **字段/算子白名单**：`field()`/`operator()` 未注册即 E1015；`available_lag≥0`（0 合法：close/funding 同 bar 收盘后可用，不构成前视，`:146-156`）。
4. **四档名字空间解析（编译期版）**：`resolve_impl` 实现 `open/closed_multi/single/fixed` 规则（`:211-235`），与运行期 `runtime.resolve` 同源——两处必须一致，否则"编译期一个名字、运行期另一个名字"成默认行为。

**加密特化**：注册表声明了 funding/OI/basis 等字段（`11.md §2.1`，`available_lag=0` 且各有可反驳理由），是 C8（列集开放）的前置；因果禁令（拒 `shift(-n)`/`lead`/`bfill`）是 C9（只 ffill 不 bfill）和白名单防前视（V4 头号 bug）的核心。`operators_version` 只含语义字段、不含注释，避免"改一句话让历史计划全部失效"（`:170-185`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下用仓库 `venv/Scripts/python.exe` 调用 `capability.registry` 与 `mining._OpCodes` **实际打印**。
> 复现脚本：`C:/Users/lansa/AppData/Local/Temp/fm_probe_params_registry.py` 的 REGISTRY 段 + 独立负位移自检命令。

### 例 1：`load_registry()` 正常加载 + 内容哈希

```
fields = 9    operators = 19
fields_digest       = sha256:437ae8d5c31fb66d2...
operators_version   = sha256:e18df4ffba76a7d79...
field('close')  = {'name':'close', 'unit':'quote', 'available_lag':0}
operator('corr')= {'name':'corr','arity':3,'param_domain':{'window':{'min':2,'max':512}},
                   'impl_symbol':'correlation','degenerate_policy':'nan'}
```

`operators_version` 是**内容哈希**而非包版本号（改算子语义才变、改注释不变）；`fields_digest` 对整份 fields.json 做哈希。

### 例 2：字段/算子白名单失败路径（真实错误码）

| 非法输入 | 输出 |
|---|---|
| `r.field('market_cap')` | `E1015` "字段未注册: market_cap" |
| `r.operator('lead_forward')` | `E1015` "算子未注册: lead_forward" |
| 复制注册表、追加 `{name:'shift_neg', arity:2, param_domain:{lag:{min:-1,max:5}}, degenerate_policy:'nan'}` 后加载 | `E1015` "算子 'shift_neg' 的 lag.min=-1 < 1（窗口/位移必须为正）" |
| 追加无 arity 的负位移算子 | `E1015` "缺合法 arity"（先命中 arity 自检） |
| 改 fields.json 的 `schema_version='2'`（三份不一致） | `E1015` "注册表 schema_version 不一致: {'fields':'2','operators':'1','params':'1'}" |

### 例 3：`param_domain` 派生 arity（跨模块消费，实测）

`_OpCodes` 从注册表 `param_domain` 派生带参算子的窗口集合，`arity = 注册表 arity - 1`（末位窗口参数由 decode 追加 const）。实测（fields=close/volume，window 域裁剪到 2..10）：

```
ref/delta/mean/std/max/min/sum : (name, 1, window∈{2,6,10})   # 注册表 arity=2 → 1
corr/cov                       : (name, 2, window∈{2,6,10})   # 注册表 arity=3 → 2
```

算子 `corr` 的 `param_domain.window={min:2,max:512}` 被裁剪到配置窗口上下界（2/6/10 三点），`arity=3-1=2`——证明 arity 由注册表**派生**而非手写（G6，`mining.py:256-274`）。

### 例 4：`resolve_impl` 四档名字空间（真实错误码）

| 输入 | 输出 |
|---|---|
| `resolve_impl({}, 'search','new_algo','open')` | `'new_algo'`（open 允许目录外新名） |
| `resolve_impl({'allocate':{'q':{}}}, 'allocate','unknown','closed_multi')` | `E1002` "实现名 'unknown' 不在 allocate 白名单" |
| `resolve_impl({'verdict':{'sentinel':{}}}, 'verdict','other','fixed')` | `E2003` "fixed 名字空间不得出现 impl: verdict" |
| `resolve_impl({'verdict':{'a':{},'b':{}}}, 'verdict',None,'fixed')` | `E2006` "fixed 名字空间 verdict 条目数 2 != 1" |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| R-1 | **`resolve_impl`（编译期）与 `runtime.resolve`（运行期）是两份规则实现**：注释声明"两处必须一致"（`:239-241`），但无机器守卫强制同步。两者当前行为一致（已实测 open/closed/single/fixed 各档），但未来改一处可能漂移。 | P2 | `registry.py:211-235`；`runtime.py:269-324`；`runtime.py:239-241` 注释 | **需确认意图**：两份规则分属编译期（catalog）与运行期（impls 表），数据源不同，难以合并为一份函数；但可加一致性契约测试钉住。属"应有守卫缺失"，非当前 bug。并入 U7/U5 测试项，不单独修代码。 |
| R-2 | **`fields_digest` 对整份 fields.json 哈希（含 `_comment`/注释字段）**，与 `operators_version` 只取语义字段的口径不一致。改一句字段注释会让 `fields_digest` 变（算子侧则不会）。 | P2（口径一致性） | `:169` `compute_digest(fields_doc)`；对比 `:172-185` 算子只取语义字段 | **需确认意图**：`fields_digest` 用途是"编译期对账供给覆盖"，注释变化导致新试验族是保守行为（fail-closed 偏严），不破坏正确性。但两侧口径不对称，建议文档说明或统一为语义字段哈希。 |
| R-3 | **`_check_operator` 的字符串 token 扫描（`:105` `"shift(-"`/`"pct_change(-"`/`periods\": -`）是按字面匹配**，非 AST 级。注释或 impl_symbol 里出现这些子串会误报；变形写法（空格/换行）可能漏报。 | P2（防御深度） | `:105-106`；`11.md:160-162` 另有编译期 AST 因果扫描兜底 | **需确认意图**：本层是"加载即自检"的第一道，AST 级扫描在编译器（`11.md:162`）。有兜底，误报/漏报影响有限；但过滤范围（整个 `str(op)`）偏宽。记录为观察项。 |

**多了什么**：无明显冗余。`_BANNED_NAMES`/`NAMESPACES` 是必要的封闭集合；`_DEFAULT` 单例（`:189-201`）防"同一次运行读两份注册表导致 available_lag 因调用顺序而异"。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| content hashing（git object 模型）做注册表指纹 | 采纳 | `compute_digest(fields_doc)` / `operators_version`（`:169-185`），内容变则指纹变；拒绝包版本号当算子指纹（I27，`test_v5_capability.py:60-64`） |
| 加载即自检 + 整体失败（schema-first） | 采纳 | 三份 JSON 同 schema_version、字段/算子重名、因果禁令，任一违反 E1015 不跳过（`:115-165`） |
| 白名单 + 参数域双重校验 | 采纳 | `11.md:106` V4 头号 bug：只查算子名不查参数域，可构造 `shift(-1)` 完美前视因子；本层查 `param_domain.min/max≥1`（`:75-104`） |
| 代码内字符串白名单常量表 | **拒绝** | 与 JSON 各说一套（`:2-4`）；字段/算子真相只在 JSON |
| 把注释/`basis` 语义字段纳入 `operators_version` | **拒绝** | 改一句话就让所有历史计划 `operator_set` 失效——"注释当口径"（`:170-171`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **加载即自检 E1015，不返回部分结果**（`:44-50,109-165`）：负位移（实测带 arity 的 `lag.min=-1` → E1015）、零窗口、`center` 真值（`:62-63`，`center:1`/`"yes"` 也拒，P1-11）、bfill/lead/future 名（`:57-59`）、缺 impl_symbol（`:66-67`）、常数 degenerate_policy（`:68-74`，必须 `nan`/`reject_batch`）、`param_domain` min/max 双向校验（`:78-104`，只查 min 时 `max=-3` 会穿透，P1-11）、三份同 schema_version（`:115-124`，实测漂移 E1015）、重名（`:136-137,163-164`）。
2. **`available_lag≥0` 且 0 合法**（`:146-156`）：修复了历史"错误要求 ≥1，与文档矛盾、把可用时刻整体推后一根 bar、与 `label.entry=next_close` 叠加成双重滞后"的问题（注释 `:146-150`）。实测 `close.available_lag=0`。
3. **`operators_version` 只含语义字段**（`:172-185`）：`basis`/`_self_check_note` 注释不进摘要，按 name 排序保证确定性；实测与两轮加载一致（`test_v5_capability.py:60-64`）。
4. **`default_registry` 进程内单例**（`:192-201`）：防同一次执行读两份注册表；`reset_default_registry` 仅测试用。
5. **arity 派生（跨模块）**：实测 `mining._OpCodes` 用 `arity-1` + `param_domain` 窗口生成带参算子，注册表是唯一数据源（G6）。
6. **四档名字空间**：实测 open/closed_multi/fixed 各返回预期值或码；`single` 档（如 read）要求目录恰好一条（`:225-228`）。
7. **`E1015` vs `E1015` 用法**：`field`/`operator` 查找失败也是 E1015（`:33-41`），与加载自检同码——符合 `11.md §5` 的"算子白名单 E1015"。

**发现的问题**：
- 见 D2 表 R-1（编译期/运行期两份 resolve 规则无机器守卫）、R-2（fields_digest vs operators_version 口径不对称）、R-3（字符串 token 扫描防御深度）。**无 P0/P1 级代码 bug**。测试覆盖充分（`test_v5_capability.py:41-153` 覆盖加载、哈希、白名单、负位移/零窗口/负 max/center 真值/版本漂移、四档名字空间）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `11-field-operator-registry.md §4.1/§5`（`:129,147,160-162`） | 加载即自检 E1015、字段/算子白名单、AST 因果扫描 | 与实现一致，无需更新 |
| `11-field-operator-registry.md:34,78-84` | `available_lag` 只允许 ≥0，0 合法且逐字段理由 | 与 `:146-156` 一致，无需更新 |
| `11-field-operator-registry.md:95-97` | `arity`/`param_domain` 定义 | 补一句"带参算子的搜索 arity 由 `param_domain` + arity-1 派生（mining._OpCodes），注册表是唯一数据源"，消解 R-2 跨模块理解成本（说明性） |
| `s0/04-capability.md §5.1`（`:181,375,440,460`） | 四档名字空间 | `resolve_impl`（编译期）与 `runtime.resolve`（运行期）两份规则，文档应标注"两处同源、一致性靠契约测试"（R-1，说明性） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增字段（如清算/链上/跨所价差） | ✅ 配置 | 在 `fields.json` 加条目（name/unit/datasets/available_lag≥0）；加载自检和哈希自动生效。R3 的前置，但字段声明≠真正进面板（属 U6 面板层） |
| 新增算子 | ⚠️ 配置 + 代码 | `operators.json` 加条目 + `impl_symbol` 映射到真实实现 + 参考实现对拍（`11.md §5`）；`param_domain` 派生搜索 arity（无需手改 mining） |
| 新增名字空间 | ❌ 需改代码 | `NAMESPACES` 封闭集合 + `resolve_impl` 与 `runtime.resolve` 两处规则都要改（R-1） |
| 改算子语义/窗口域 | ✅ 指纹自动变 | `operators_version` 是内容哈希，改语义即新试验族；注释不变指纹（已实现） |
| 加字段到注册表但供给未覆盖 | ✅ fail-closed | `fields_digest` 供编译期对账供给覆盖，缺字段会暴露（R-2 偏严但安全） |

---

## C1–C12 相关项判定

- **C8 列集开放**：✅ 正向——字段注册表已声明 funding/OI/basis 等（9 字段），加字段纯配置；但 R3 全局缺口仍在（清算/跨所/合约维度未声明），本模块是前置非落点
- **C9 只 ffill 不 bfill / 前视防线**：✅ 强支撑——因果禁令（拒 bfill/lead/future/负位移）+ `param_domain.min/max≥1` + `available_lag≥0`；实测负位移/零窗口/负 max 全 E1015
- **C4 合约四元组 / C5 计价货币**：❌ 字段表无 `expiry`/`contract_type`/`settle_ccy` 维度（全局 P0-5，本模块非单独落点）
- **C1 365 年化 / C12 embargo**：不适用（本模块不年化/不算 embargo）

## R1–R7 影响

- **R1 真数据跑通**：中性（契约加载器，每次真跑都加载）
- **R2 结论可信**：✅ 强支撑——因果禁令与参数域双重校验挡住前视因子进搜索空间
- **R3 因子零代码扩展**：⚠️ 部分正向——加字段/算子配置化，但合约四维、清算/跨所未声明（全局缺口）；arity 派生使新带参算子零代码进搜索空间（G6）
- **R7 fail-closed**：✅ 强支撑——加载即自检、白名单、内容哈希、版本一致性全拒；"生产可用"的契约地基成立

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 三处 P2 为"需确认意图/守卫缺失"：R-1 编译期/运行期两份 `resolve` 规则无机器守卫（当前一致，建议加一致性测试）、R-2 `fields_digest` vs `operators_version` 口径不对称（偏严但安全）、R-3 字符串 token 扫描防御深度（有编译器 AST 兜底）。加载即自检 E1015、三份 JSON 同版本、`METHOD_STAGES`/算法名分派（本模块经 arity 派生体现）、字段/算子白名单全部实测成立，是因子挖掘的契约地基。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 3 个模块 `spec.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
