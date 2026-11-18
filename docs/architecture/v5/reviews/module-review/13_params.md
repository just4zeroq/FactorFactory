# 模块审查 13 · `v5/capability/params.py`

> 审查日期：2026-11-18
> 依据：源码 539 行全文通读；依赖 `registry/params.json`（唯一真相）、`contracts/errors.py`（E1003/E1004/E1008/E2003/E3001/E3003）；消费方 `ops/boundary.py`、`ops/label.py`、`ops/search.py`；设计 `s0/04-capability.md` §1–§2（L161-166）、`18-plan-schema.md:142`；测试 `test_v5_capability_runtime.py`、`test_v5_capability.py:224-242`、`test_v5_package_isolation.py:122-127`；历史审查 `review_u6_u7_20261118.md §2.6`、`v5_fix_execution_20261118.md`（G3/G5/G8/G10/G14）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响；"生产可用=能真挖因子"（plan §0.0/§0.1）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Params 是能力调用的**第 2/3/5 步契约执法者**，把 `registry/params.json` 这份契约数据变成运行时防线，回答四件事：

1. **schema 是契约数据、唯一真相**：`params.json` 是唯一定义处，代码里不得再写一份"必填哪些键"——两份规则必然漂移（`params.py:5-6`）。进程内返回**深拷贝**，杜绝 `schemas()["verdict"]["params"]["n_trials"]=...` 一条赋值废掉一条验收（C16）。
2. **多余字段即错、缺字段即错、运行期不补默认**：`additional_properties=false` 等价物——多余字段 `E1003`（"配了就错"不是"配了没用"）；类型/范围 `E1004`；缺必填 `E1008`。默认值**只在编译期展开进 `values` 并进 `policy_digest`**，运行期拿不到就拒（`params.py:10-11,318-319`）。
3. **两类默认值严格分离**：
   - **口径默认值**（constitutive）：是口径，编译期展开进 plan、进指纹；缺了 `E1008`，运行期绝不补。
   - **实现缺省值**（`params.json.defaults`）：是分位边界/GP 种群/求解器正则这类实现细节，**不进 plan、不进指纹**，只在调用方未显式给值时由实现读取；单一真相在 `defaults` 一处（`params.py:313-340`）。混成一类会让"调一下 GP 种群"悄悄改变 `policy_digest`（G3/G5/G8/G10/G14）。
4. **输入/输出结构也是契约**：输入按位置比对（kind 校验、`None` 占位可选输入、尾部可省）；输出按 params 选档（`backtest` 的 summary/full），缺字段/多字段/类型错/NaN 一律 `E3003`，不当成功。

**加密特化**：本模块是类型/结构层，不含加密语义；但它守护的"默认值不进指纹"是 C10（判据不得被悄悄改写）的底座——若 `dsr_min`/`n_trials` 能从 defaults 兜底，高波动下判据就会被静默改写。时间戳强制 UTC `Z`（`:52-66`）是跨所事件对齐不出现 8 小时漂移的基础。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下矩阵是用仓库 `venv/Scripts/python.exe` 调用 `v5.capability.params` 各函数**实际打印**的结果，不是推演。
> 复现脚本：`C:/Users/lansa/AppData/Local/Temp/fm_probe_params_registry.py`（系统临时目录， PARAMS 段）。

### 例 1：`implementation_defaults` —— 实现缺省值的单一真相

真实输入→输出：

```
implementation_defaults('search')   -> {'population': 20, 'generations': 3, 'max_depth': 4, 'max_candidates': 40, 'genome_len': 7}
implementation_defaults('boundary') -> {'tier_edges': [0.3333333333333333, 0.6666666666666666]}
implementation_defaults('backtest') -> {'initial_cash': 1000000.0, 'base_ts': '2020-01-01T00:00:00Z'}
implementation_defaults('cli')      -> {'master_seed': 42, 'family': 'family_v1'}
implementation_defaults('NOPE')     -> {}          # 未登记能力返回空字典，不报错（见 D2/params-1）
```

**关键分离证据**（同一探针实测）：

```
search params schema keys  = ['prior_pool', 'max_candidates']   additional=True
'population' in search params schema? False   but in defaults? True
```

`population=20` 只在 `defaults`、不在 `search.params` schema——因此它不进 `validate_params` 的 `out`、不进 `params_digest`、不进 plan、不进 `policy_digest`；改它不影响任何指纹（G5 成立）。

### 例 2：`duration_bars` —— 时长归一成 bar（真实输入输出 + 失败错误码）

| 输入 | 输出 |
|---|---|
| `duration_bars(5)` | `5` |
| `duration_bars('5bar')` | `5` |
| `duration_bars('5d', step_ns=86_400_000_000_000)`（1d 轴） | `5` |
| `duration_bars('5h', step_ns=86_400_000_000_000)`（1d 轴） | `E1004` "duration '5h' 在步长 86400000000000ns 的轴上不足一根 bar" |
| `duration_bars('5h')`（缺 step_ns） | `E1004` "duration '5h' 需要时间步长才能换算成 bar" |
| `duration_bars(0)` | `E1004` "duration 必须 >= 1 bar，得到 0" |
| `duration_bars(-1)` | `E1004` "duration 必须 >= 1 bar，得到 -1" |
| `duration_bars('5x')` | `E1004` "duration 形式非法: '5x'" |

**逐格解读**：`5h` 在 1d 轴上 ≈ 0.208 bar，若在 1d 面板上把它当 5 bar，窗口整体放大 24 倍且不会让任何测试变红——所以缺 `step_ns` 或不足一根 bar 都 fail-closed（`:95-130`）。这是加密多频率（1h/4h/1d）不串口径的关键守卫。

### 例 3：`validate_params` —— 多余 / 缺 / 错 全拒（真实错误码）

```
validate_params('verdict', {... 'n_trials':10})  -> E1003 "verdict.params 含 schema 之外的字段 ['n_trials']"
validate_params('verdict', {'dsr_min':0.95})     -> E1008 "verdict.params 缺必填字段 fdr_max"
validate_params('verdict', {'dsr_min':'0.95',...})-> E1004 "verdict.params.dsr_min 必须是实数，得到 str"
```

把 schema 深拷贝改坏后再校验：

```
d = schemas(); d['verdict']['params']['n_trials'] = {'type':'int'}
validate_params('verdict', {... 'n_trials':5})    -> E1003（仍拒，深拷贝生效）
```

`n_trials` 属于 `runtime_facts`，写进 params 必须 `E1003`（C16 验收点）；一行赋值无法废掉它。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| P-1 | **`implementation_defaults('未登记名')` 静默返回 `{}`**（`:336` `_DEFAULTS_CACHE.get(capability) or {}`），不抛错。调用方拼错能力名时拿到空缺省，不会报 E2003。 | P2 | 实测 `implementation_defaults('NOPE')` → `{}`；`:336` | **需确认意图**：缺省值是"未登记=无缺省"的合法语义（如多数能力无 defaults），改成报错会误伤。但若调用点要求"能力名必须已登记"，应由 `describe`/`schema_for` 侧把关（`schema_for` 对未登记能力抛 E2003，`:350-354`）。不单独修，记录为观察项。 |
| P-2 | **`additional:true`（仅 search）的透传字段不做 `_check_value`**（`:379-382` `out[name]=value`）。算法参数由 `search.describe()`/`build` 自行校验，本模块不校。 | P2（设计确认） | `:379-382`；`search params schema=['prior_pool','max_candidates'] additional=True`；`20-mining-algorithms.md §4` 第 3 步 | **已确认符合设计**：open 名字空间算法参数由算法层校验（`test_v5_capability_runtime.py:144-148` 已钉住）。但需确保 `search.build` 对每个算法参数都有校验，否则是一条无校验通道——属 U7 算法层责任，非本模块 bug。 |
| P-3 | **本模块不负责"默认值编译期展开进 policy_digest"本身**：`validate_params` 只做到"缺必填即 E1008"反向逼迫，真正的展开在编译器（U4）。 | P2（责任划分说明） | `:10-11,318-319,373-378`；`compiler.py` 步骤 5/6（`:189-210`） | **已确认符合设计**：职责分层正确（本模块拒绝运行期补默认，编译器负责展开）。文档应说清这条边界，避免误以为本模块会展开默认值。 |

**多了什么**：无冗余。`_OUTPUT_MARKERS`/`_SCALARS`/`_CONTAINERS` 都是自检所需的封闭集合；`ABSENT` 占位语义在 runtime 层（本模块 `:386-427` 只做 kind 校验）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| JSON Schema 契约优先 + `additionalProperties:false` | 采纳 | `params.json` 是唯一真相，多余键 E1003（`:362-368`）；C16 `n_trials` 写进 params 即拒 |
| typed error taxonomy（E1003/E1004/E1008 分层） | 采纳 | "多/错/缺"分三种码，上层按码分类不误判（`:27,358`） |
| 契约数据深拷贝返回 | 采纳 | `schemas()` deepcopy（`:300-310`），防一条赋值废掉验收 |
| 运行期"取不到就用默认"（常见配置库行为） | **拒绝** | 默认值编译期展开进指纹；运行期补默认会让"同一 plan 两种口径都合法"（`s0/04:162-166`） |
| 用 `datetime.fromisoformat` 接受任意时区偏移 | **拒绝** | 强制 UTC `Z`（`:52-66`），否则 `certify.oos_window` 边界可静默偏移 8 小时——跨所事件对齐基础 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **`implementation_defaults` 只读 `params.json.defaults`**，明确不进 plan/不进指纹（`:313-340` docstring + 实现）；单一真相一处定义。实测 `population` 只在 defaults 不在 params schema。
2. **`schemas()` 深拷贝**（`:300-310`）：实测改坏返回的 dict 不影响后续校验。
3. **多余键 E1003 / 类型范围 E1004 / 缺必填 E1008** 分层正确（`:357-383`），实测三条失败路径各返回预期码。
4. **NaN/Inf 拒绝**（`:69-77,153`）：`nan < b` 与 `nan > b` 恒 False，六个方向范围检查会穿过 NaN；显式 `_check_finite` 把它挡在 E1004，不让错误码从"范围违规"漂成"指纹非法 E2002"。实测 `float('nan')` 的 float 参数 → E1004（runtime 测试 `:367-371`）。
5. **时间戳强制 UTC `Z`**（`:52-66`），`+08:00`/空格/无秒均 E1004（`test_v5_capability_runtime.py:360-364`）。
6. **`duration_bars` 缺 step_ns / 不足一 bar fail-closed**（`:117-129`），实测两条 E1004。
7. **输入按位置比对 + `None` 占位**（`:386-427`）：中间可选输入（`backtest.funding`）不占位会让 tiers 前移静默错位——显式 `None` 更安全；实测不占位 E3001、必填给 None E3001（`test_v5_capability_runtime.py:391-404`）。
8. **输出按 params 选档**（`:446-463`）：`backtest.detail=full` 缺 `ledger_ref` 算失败（E3003），两档同源（`:446-451` 注释）。
9. **输出结构严格**：多字段 E1003 对称拒绝（`:484-491`），`opaque` 值对象要求 `digest()`（`:494-505`），`array` 拒标量（`:437-443,506-513`），float 拒 bool/NaN（`:518-538`）。

**发现的问题**：
- 见 D2 表 P-1（`implementation_defaults` 未登记名返回 `{}`）、P-2（additional 透传不校验，属算法层责任）。**无 P0/P1 级代码 bug**。测试覆盖充分：schema 自检、三类参数错误、深拷贝、type system 严格性、输入占位、输出选档均有钉住（`test_v5_capability_runtime.py` 共 20+ 用例；`test_v5_capability.py:224-242` 冻结 defaults 值；`test_v5_package_isolation.py:122-127` 钉住 `_BLOCK_MIN=4` 不进 defaults）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:161-166` | 明确 "default 只在编译期展开；运行期不存在取不到就用默认" | 与实现一致，无需更新 |
| `s0/04-capability.md:60` | `cacheable` 列在 describe 元信息 | 本模块不涉及（spec.py 职责），无需更新 |
| `18-plan-schema.md:142` | `initial_cash` 标"实现缺省"，显式写才进 `caliber_digest`（G3） | 与 `implementation_defaults` 分离语义一致，无需更新 |
| `11-field-operator-registry.md` / `20-mining-algorithms.md §4` | 算法参数由 describe 校验（additional 透传） | 补一句"search 的 additional 字段不在 params.py 校验，须在算法层校验"，消解 P-2 的责任模糊（说明性，非契约变更） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新能力加参数 | ✅ 纯配置 | 在 `params.json.capabilities.<cap>.params` 登记 schema；`load_param_schemas` 加载即自检 E2003 |
| 新增实现缺省值（如新旋钮） | ✅ 纯配置 | 加进 `params.json.defaults.<cap>`，实现用 `implementation_defaults` 读；不进指纹 |
| 算法私有参数（search） | ⚠️ 半配置化 | `additional:true` 透传，须在算法 `describe`/`build` 侧补校验（P-2） |
| 新输出结构档位（如 detail=diagnostic） | ✅ 配置 + 实现 | `outputs_by.cases` 加一档并在实现产出对应字段；缺档 E3003 |
| 新输入 kind | ✅ 配置 | `params.json.inputs` 声明 kind，按位置校验自动生效 |

---

## C1–C12 相关项判定

- **C1 365 年化 / C12 embargo 以观测为单位**：不适用（本模块不年化、不算 embargo）；但 `duration_bars` 在 1d/1h/4h 轴上归一时长（实测），是多频率口径正确的底座 ✅
- **C10 判据不得被加密波动放松**：✅ 强支撑——`n_trials`/判据参数写进 params 即 E1003，defaults 不进指纹；杜绝"调一下默认值就改判据"
- **C9 as-of 对齐 / C7 缺口**：间接支撑——时间戳强制 UTC `Z`（`:52-66`），避免跨所事件 8 小时漂移；本模块不判缺口
- **C8 列集开放**：✅ `read.fields` 是自由键映射（`:221-230`），键是数据集名不预设，支持 funding/OI 等新列

## R1–R7 影响

- **R1 真数据跑通**：中性（结构校验层，已在多次真跑中使用）
- **R2 结论可信**：✅ 强支撑——默认值不进指纹、NaN 显式拒绝、判据参数不可塞进 params
- **R3 因子零代码扩展**：✅ 正向——新能力参数/缺省值/输出档均配置化；`read.fields` 自由键映射支撑开放列集
- **R7 fail-closed**：✅ 强支撑——多/缺/错/NaN/时区/时长全拒，错误码分层明确；"生产可用"底座成立

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 三处 P2 均为"需确认意图/责任划分说明"：P-1 未登记名返回 `{}` 是缺省语义（由 `schema_for` 把能力名合法性），P-2 additional 透传属算法层校验责任，P-3 默认值编译期展开在编译器（本模块反向逼迫）。`implementation_defaults`/`duration_bars`/缺省值单一真相/G3·G5·G8 全部实测成立，是"生产可用=能真挖因子"的 fail-closed 底座。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 2 个模块 `registry.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
