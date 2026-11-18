# 模块审查 34 · `runtime/compiler.py`

> 审查日期：2026-11-18
> 依据：源码 475 行全文通读；依赖 `contracts/plan_schema.py`（validate_plan / validate_cross_field / caliber_field_names）、`contracts/digest.py`（五指纹真公式）、`runtime/gate_b.py`（门禁 B）、`contracts/types.py`（leaf_paths）。设计 `04-runtime.md` §C1–C22（L97–118）、`18-plan-schema.md`、`s0/03-runtime.md` §5/§6（L261–319）；测试 `test_v5_runtime_real.py`、`test_v5_full_review_regressions.py`、`test_v5_plan_schema.py`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_u4.py`（C 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

编译器是**配置 → 冻结方案的唯一纯函数**（`compile_plan`，`:107-341`）：不读盘、不写文件、不取时钟（`compiled_at` 由调用方注入，`:4-5`）。十步职责：

1. **深合并 + 来源表**（步骤 1–2，`:137-143`）：baseline 与 plan 按叶子深合并，list 整体替换（`:437-461`），每个叶子标 `baseline@vN` / `plan` / `override`（`_mark_origin`，`:469-475`）。**baseline 默认编译期展开标来源、不进 plan**——`baseline_version` 是元字段，`BASELINE_META_KEYS` 剔除（`:60-63,140`），否则成为 E1001 非法顶层键。
2. **结构层校验**（步骤 1–4）：复用 `plan_schema.validate_plan` + `validate_cross_field`（`:146-171`），C1/C4/C20/C21/X1–X7。
3. **overrides 叠加**（步骤 3，`:153-171`）：构成性参数须显式放行（E1005）；按路径写叶子（`_apply_override`，`:402-428`）；覆盖后重过结构校验，防 `--set` 绕过。
4. **C20/E1020**（步骤 4，`:173-187`）：工位 `params` 出现 `CALIBER_FIELDS` 任一字段即拒——口径只有 `caliber` 一份真相。**字段清单从 schema 派生**（`:50`，`caliber_field_names()`），不手抄（手写 24 个字面量曾漏同步）。
5. **实现名检查**（步骤 5，`:189-206`）：非空字符串（E1002）、占位实现 `dummy_/demo_/placeholder` 拒（E1011）；`target/evidence` 可无 impl。
6. **步骤 6/7 留接入口**（`:208-210`）：param_schema 逐项校验、composes 子调用图依赖能力注册表，此处**不放宽为忽略继续**。
7. **供给别名 + 门禁 B**（步骤 9，`:212-282`）：别名必须完整 Pin、`pin.dataset==stream`、manifest 与版本一致（E4004）、过 check_gate_b；`require_supply` 下空别名/空 datasets 拒（E4001）。Pin 用 `digest_view` 规范化投影进指纹（`:280-282`，Z 与 +00:00 等价）。
8. **caliber 四子块**（步骤 10，`:285-301`）：`execution/label/allocate/net` 缺一不可（E1008）；只取这四块进 `caliber_digest`。
9. **五指纹唯一公式**（`:284-326`）：`plan_digest=H(plan_raw)`、`domain_digest=H(members,fit/embargo/eval)`、`caliber_digest=H(四子块)`、`policy_digest=H(baseline_version,constitutive_subset,caliber,domain)`、`blueprint_digest=H(baseline_version,values,caliber,supply_pins,domain)`。**红线**：公式只在 `contracts/digest.py` 一处（本模块全部经 `dg.*` 调用），`policy` 不含 supply_pins（数据刷新是事实演进，不新建试验族），`operations/compiled_at` 不进任何指纹（`:20,57`）。
10. **来源表对齐**（`:328-339`）：origins 与 merged 叶子一一对应，缺失 E1007。

**C1–C22**（`s0/03-runtime.md:292-315`）全部 fail-closed；本模块直接落实 C1/C4/C5/C8/C11/C12/C20/C21，其余分散在 plan_schema（C1/C4/C20/C21/C22）与能力注册表（C2/C3/C13–C17），C19 因 `available_from` 语义暂缓。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下为 `venv/Scripts/python.exe` 调用 `compile_plan` / `check_gate_b` **实际打印**结果，输入为仓库 `tests/fixtures/plan_valid.json`。复现脚本 `probe_u4.py`。

### 例 1：五指纹 + 来源表（真实输出）

```
plan_digest       = sha256:0585b103835178283c3f413590016a4efc86e6b6c8c479bfd369f2827f683ad9
blueprint_digest  = sha256:9aa21aa43a9f0c4461ed79563356b444dab68bd4587d1ba8fefe20e60acdf320
policy_digest     = sha256:72aafcac93823980e3c9ef6cb4d0d697e5e1fd2fbbb4d04eebfa69294d2e5447
caliber_digest    = sha256:1e314dd15b659e3316d7a4483962a0d6e28fcd74fbf462d975adbf639a50eb6a
domain_digest     = sha256:8220751d2294b4bba51058ad94e0af24eafef6b3f470d51cad204930ab7a904e
plan_digest==H(plan_raw) : True
origins 叶子数 : 62      CALIBER_FIELDS 数 : 27      STATIONS : 8 个
```
`contracts/digest.py` 共有 12 个 `*_digest` 函数（blob/blueprint/caliber/call/compute/domain/environment/parcel/plan/policy/runtime_facts/supply）——编译器只用其中 5 个，**公式唯一**成立。

### 例 2：baseline 默认编译期展开标来源、不进 plan（真实输出）

输入：`baseline={"baseline_version":"v2","caliber":{"label":{"horizon":5}}}`，plan 删去 `caliber.label.horizon` 叶子。
```
{'baseline_version_in_plan': False,
 'origin_of_baseline_leaf': 'baseline@v2',
 'value_of_baseline_leaf': 5}
```
读法：基线值 5 被展开进方案，来源标 `baseline@v2`，而 `baseline_version` 这个元字段没有污染 plan（否则 E1001）。

### 例 3：caliber 四子块 → 指纹传播（真实输出）

只改 `caliber.label.horizon: 5→99`：
```
plan_digest_same=False   caliber_digest_diff=True
policy_digest_diff=True  blueprint_digest_diff=True   domain_digest_same=True
```
读法：改 caliber 必动 caliber/policy/blueprint（口径变了全链失效），domain 只由组合域/窗口决定故不变；`plan_digest` 因 plan 原文变了而变（H(plan_raw) 的应有行为）。

### 失败输入 → 错误输出（全部实测）

| 非法输入 | 输出（错误码\|消息） |
|---|---|
| `target.params.horizon=5`（C20 口径字段进工位） | `E1020\|工位参数里出现口径字段 'horizon'` |
| `plan.caliber="oops"` | `E1004\|应为对象，实际是 str` |
| `plan.baseline={...}`（非字符串） | `E1004\|plan.baseline 必须是版本字符串（如 v2）` |
| `overrides={"caliber.label.horizon":7}` 未放行 | `E1005\|构成性参数 'caliber' 出现在覆盖层且未放行` |
| `overrides={"nope":1}` | `E1001\|未知键 'nope'` |
| `overrides={"caliber.label.horizon.x":1}` | `E1001\|覆盖路径 ... 的中间节点 'horizon' 不是映射` |
| `horizon=null` + 基线有 5（不回落） | `E1004\|应为整数，实际是 None` |
| `boundary.impl="dummy_foo"` | `E1011\|默认实现指向占位 'dummy_foo'` |
| `supply.market="spot"` 但 datasets 含 funding/OI/basis | `E1004\|现货市场不支持数据集 ['basis','funding','open_interest']` |
| `require_supply=True` + 空别名 | `E4001\|计划未声明 supply.datasets，无法定位任何供给` |

**关键边界实测**：`search.algorithms=[]` 时编译器返回 **OK（不拒绝）**——"live≥1 算法"的 E1004 不在 compiler，在 `app/cli/live.py:135`（见 D2 B-1）；`supply.market="swap"`（枚举外）被 schema 拒 `E1004\|取值 'swap' 不在允许集内`。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| C-1 | **"E1004 live≥1 算法"不在编译器**：`search.algorithms=[]` 编译通过（实测 OK），该校验在 `app/cli/live.py:135`。`flow explain --plan` 只编译，会对零算法计划显示全绿，直到 `--live` 才 E1004。 | P2 | 实测 `compiler_empty_algorithms: OK`；`live.py:135` | **已确认事实**：编译器职责是编译期 schema/口径校验，live 是应用层执行前置。属"确认意图"——是否把该校验前移到编译期需裁决；若前移须只加一处避免双真相。 |
| C-2 | **C2/C3/C13–C17（实现名在目录、参数 schema、算法四方法/白名单/拟合阶段等）不在编译器**：步骤 6/7 仅留接入口（`:208-210`），实际由 `capability_bridge._resolve_impl`（E1002）与 `runtime.run` 的注册校验兜底。 | P2 | `compiler.py:208-210`；`capability_bridge.py:336-375` | **已确认设计如此**：分工是"编译期做结构/口径，运行期注册表做实现"。但 `s0/03 §6` 写"步骤 6/7 由 registry.validate_calls 填充"，当前 compiler 未接——文档与实现有落差（见 D5）。 |
| C-3 | **C19（拟合窗口起点早于域内最晚可用时间）E1019 未实现**：plan 提及因 `available_from` 语义暂缓；`compiler.py` 无 E1019 代码，`_domain_members` 的 `available_from` 对 list 形态为 None（`:344-365`）。 | P2（确认暂缓） | `compiler.py:344-365`；`s0/03-runtime.md:312` | **已确认符合当前设计**（plan §U4 D2 明列此条）。待符号集解析出真实 available_from 后补。 |
| C-4 | **C22 重叠声明错误码是 E1008，不是文档规定的 E1022**：`plan_schema.py:318-324` 用 E1008，`s0/03:315` 写 E1022。 | P2（码一致性） | `plan_schema.py:318-324`；`s0/03-runtime.md:315` | **已确认不一致**：语义已闭环（fail-closed），但码与文档不符，调用方按 E1022 分支会漏。属 plan_schema 单元，本模块记录不单独修。 |
| C-5 | **"八指纹"口径**：`compiler.py` 文档串列五指纹（`plan/domain/caliber/policy/blueprint`），全系统共 12 个 `*_digest`；U1 契约单元称"八指纹"。 | P2（术语） | 实测指纹表；`compiler.py:10-15` | **需确认意图**：是"编译期五锚点 + 执行期 call/parcel 等"合称八，还是术语遗留。公式唯一（都在 contracts/digest）无争议。 |

**多了什么**：无冗余。`BASELINE_META_KEYS` 含 `inherits/extends/preset` 是防御性排除（charter 已拒，双保险）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Schema-first 编译期校验（JSON Schema + 跨字段） | 采纳 | C1/C4/C20/C21 在 plan_schema 一次做全；`--set` 覆盖层重校（`:166-171`）防绕过 |
| 不可变构建产物 + 逐叶子来源表（git object 模型） | 采纳 | 62 叶子全标来源，`explain` 可逐条回答"这个值从哪来" |
| 内容寻址指纹（`*_digest`，kind 包装） | 采纳 | 五指纹唯一公式在 contracts/digest；`policy` 不含 supply_pins 使数据刷新不新建试验族 |
| 运行期解析"最新"数据 / 插件热加载 | **拒绝** | 编译期冻结，`compiled_at` 由注入而非 `datetime.now()`；无动态版本解析 |
| 手抄口径字段清单 | **拒绝（已修）** | `CALIBER_FIELDS` 从 schema 派生（`:50`），防 schema 加键后 C20 失效 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **纯函数**：`plan_raw = deepcopy(dict(plan))` 最先执行（`:125`），实测 `--set` 不改调用方 plan（`test_full_review_regressions:55-68`；本轮 C-12 复测 True）。
2. **list 整体替换**：`_deep_merge_with_origins` 的 walk 对非 Mapping 整体替换（`:460-461`），搜索算法数组不被悄悄拼成两份（`:228-237` 测试）。
3. **null 是值不是"未提供"**：`:458-461` 注释明确，实测 `horizon=null` → E1004 而非回落基线 5（C-8）。
4. **E1020 从 schema 派生**：`:50`，实测 27 个口径字段，工位自带即拒（C-4）。
5. **impl 占位拒**：`:200-206`，实测 `dummy_foo` → E1011。
6. **别名串流防护**：`pin.dataset != stream` → E4001（`:249-257`），manifest 版本不符 → E4004（`:270-278`），实测覆盖到位。
7. **Pin 规范化进指纹**：用 `digest_view` 而非原始 JSON（`:280-282`），Z/+00:00 等价（门禁 B G-10 实测 `equal=True`）。
8. **caliber 四子块**：`:288-301`，缺块 E1008，只取四块进 `caliber_digest`。
9. **来源表对齐**：`leaf_paths` 差集 E1007（`:330-339`），62 叶子全覆盖。
10. **overrides 重校验**：`:166-171`，实测未知键 E1001、类型错 E1004。

**发现的问题**：C-1~C-5 均为 P2/需确认意图，**无 P0/P1 级代码 bug**。测试覆盖充分（四指纹、来源表、门禁 B 11 项、C20/C21、别名防护，见 `test_v5_runtime_real.py` 与 `test_v5_full_review_regressions.py`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/03-runtime.md:261` | 写"步骤 6/7 由 registry.validate_calls 填充" | compiler 当前未接 registry，实现名校验实际在 bridge；补一句"步骤 6/7 由编排层 bridge + runtime 完成"或标注为待接（说明性） |
| `04-runtime.md:97-118`（C1–C22） | 列全 22 条 | 补注"C2/C3/C13–C17 落在能力注册表，C19 暂缓，C22 当前报 E1008"，避免读者以为 compiler 全包 |
| `s0/03-runtime.md:315`（C22 E1022） | 规定 E1022 | 实现报 E1008；统一其一（属 plan_schema 单元，记录不擅改） |
| `compiler.py:10-21`（指纹文档串） | 列五指纹 | 与 U1"八指纹"术语对齐说明（五锚点 vs 全系统八/十二） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 加新口径字段 | ⚠️ 半配置化 | 改 `plan_schema.json` 的 caliber 块 → `CALIBER_FIELDS` 自动派生（`:50`），C20 自动覆盖，无需改 compiler 代码 |
| 新工位 | ❌ 需改多处 | `STATIONS`/`CONSTITUTIVE_TOPS`/`FINGERPRINT_TOPS` 三处字面量（`:43-68`）+ schema + 规格表；非纯配置 |
| 新数据集（清算/链上）进入供给门禁 | ⚠️ 需改多处 | `plan_schema` 的 datasets 枚举 + gate_b 的 BAR_DATASET 判定；compiler 经别名表通用传递，无需单改 |
| C19（available_from 前置校验） | ❌ 暂不支持 | plan 明确暂缓；待符号集解析出真实 available_from 后补（C-3） |
| live≥1 算法前移到编译期 | ❌ 当前不支持 | 校验在 live.py；需裁决是否前移（C-1） |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（编译期不算年化）；caliber.net 只做口径字段存在性校验
- **C3 spot/futures 维度**：✅ 间接支撑——`supply.market` schema 枚举 spot/futures（实测 `swap`→E1004），cross-field 禁 spot 带 funding/OI/basis（实测 E1004），门禁 B 第三道比对（G-6）
- **C4 合约四元组**：❌ 无（全局 P0-5，非编译期落点；`supply.symbols` 是标的集名，不含 expiry/settle）
- **C8 列集开放**：⚠️ plan_schema 的 datasets 枚举仅 ohlcv/funding/OI/basis 四种，新数据流需改枚举（D6）
- **C10 判据不被放松**：✅ 判据字段在 caliber，工位不可自带（E1020），构成性参数 override 须放行（E1005）

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（编译冻结方案是八工位执行前置）
- **R2 结论可信**：✅ 强支撑（五指纹唯一公式 + caliber 单源 + E1020，防"判据/回测两套口径"）
- **R7 fail-closed**：✅ 强支撑（C1/C4/C5/C8/C11/C12/C20/C21 全实测拒绝，无静默默认）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 十步编译、baseline 编译期展开标来源、caliber 四子块 + E1020（27 字段从 schema 派生）、门禁 B 接线、五指纹唯一公式（全在 contracts/digest 一处）、来源表叶子全覆盖——全部以真实调用坐实。五处 P2 均为职责边界或术语/码一致性：C-1 live≥1 算法在 live.py 非 compiler（需裁决是否前移）、C-2 步骤 6/7 由 bridge 承接（文档落差）、C-3 C19 暂缓（已确认）、C-4 C22 报 E1008 非 E1022（属 plan_schema）、C-5 八指纹术语（公式唯一无争议）。**生产可用（能真挖因子）= 是**：`--live` 全链编译门禁已支撑真实运行。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 35 个模块 `gate_b.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我（如 C-1 是否前移 live 校验）
