# 模块审查 15 · `capability/spec.py`

> 审查日期：2026-11-18
> 依据：源码 194 行全文通读；依赖 `contracts/errors.py`（E2003）；消费方 `capability/__init__.py`（导出 `describe`/`all_specs`/`CAPABILITIES`）、`runtime.py`（`register`/`resolve`/`_declared_reads` 全靠 `describe()`）；设计 `s0/04-capability.md §3/§4.2`（`:60,395`）、`s0/01-data-contracts.md`；测试 `test_v5_capability.py:156-186,213-222`、`test_v5_package_isolation.py:129-167`（AST 守卫）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响；"生产可用=能真挖因子"（plan §0.0/§0.1）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Spec 是 12 项能力的**公开元信息单一来源**，回答四件事：

1. **能力注册表**：`CAPABILITIES` 声明 12 能力各自的名字空间（open/closed_multi/single/fixed）；`SERVES` 是工位→能力映射的"能力→服务工位"投影（一个能力服务哪些工位，如 allocate 服务 Evidence/Portfolio/Certify 三处）；`_SPECS` 逐能力声明副作用/composes/policy_reads。
2. **`describe()` 是纯静态投影**：返回冻结的 `CapabilitySpec`，只读、无数据、无结论、无配置入口。结构禁令靠"签名里没有位置"保证——不评估、不判显著、不写计数、不自选口径、不自建子调用（`:1-5`）。
3. **`CapabilitySpec` 自校验**：namespace/side_effect/policy_reads/composes 非法即 E2003；`policy_reads` 必须是 caliber 四子块（execution/label/allocate/net），不能是"policy_view"这个组装后的映射名（`:134-150`）。
4. **方法阶段标签单一出处**：`METHOD_STAGES` + `method_id()` 是 `@S3/@S4/@S7` 标签的唯一构造入口，拼错即 E2003（`:29-34`）。标签是**方法版本标记**不是"当前系统阶段"——`observe` 的毛粗筛量保留 @S3，`allocate+backtest` 才是 S4 真成本链，组合域 @S7（`:15-20`）。

**加密特化**：本模块是元信息层，不含加密数值语义；但它守护的"方法标签单一出处"（G1/G2）是 R5 单因子可归因的基础——标签散落会让"组合域 @S4 还是 @S7"无法确定，进而使 `domain_digest` 口径漂移（`s0/04:395`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 用仓库 `venv/Scripts/python.exe` 调用 `capability.spec` 各函数**实际打印**。
> 复现脚本：`C:/Users/lansa/AppData/Local/Temp/fm_probe_params_registry.py` 的 SPEC 段。

### 例 1：`describe('observe')` 完整元信息（真实输出）

```
describe('observe').to_dict() = {
  "name":"observe", "namespace":"single", "serves":["Evidence"],
  "params_schema_ref":"capability/observe/params",
  "policy_reads":["execution","net"],
  "side_effect":"pure", "cacheable":true,
  "composes":["allocate","backtest"]
}
```

`cacheable` 由 `side_effect` 派生（`!= 'append_only'`），不是独立字段——`to_dict()` 里没有 `deterministic`/`stateful`（实测均为 False），这两个恒默认字段已删除（E10 复核）。

### 例 2：`cacheable` 派生（真实输出，验证 deterministic/stateful 已删）

```
describe('verdict').cacheable = False    # append_only
describe('search').cacheable  = False    # append_only
describe('observe').cacheable = True     # pure
describe('allocate').cacheable= True     # repeatable
'deterministic' in describe dict? False   'stateful'? False
```

`append_only` 能力每次调用都往账本追加事实，命中缓存会让"试了几次"少记一笔，故不可缓存；`pure`/`repeatable` 可缓存。确定性由 `seed`+`call_digest` 保证，不需独立字段。

### 例 3：`method_id()` 单一构造入口（真实输出 + 失败错误码）

| 输入 | 输出 |
|---|---|
| `method_id('combine_domain')` | `'combine_domain@S7'` |
| `method_id('allocate+backtest')` | `'allocate+backtest@S4'` |
| `method_id('simple_longshort')` | `'simple_longshort@S3'` |
| `method_id('BAD')` | `E2003` "方法 'BAD' 未登记阶段标签" |

### 例 4：`CapabilitySpec.__post_init__` 校验（真实错误码）

| 非法输入 | 输出 |
|---|---|
| namespace=`'bad'` | `E2003` "非法名字空间: bad" |
| policy_reads=`('policy_view',)` | `E2003` "非法口径子块: ['policy_view']" |
| composes=`('ghost',)` | `E2003` "未登记的子能力: ['ghost']" |

### 例 5：工位→能力映射（真实输出）

```
SERVES['backtest'] = ('Evidence','Verdict','Portfolio','Certify')   # 服务 4/8 工位
SERVES['allocate'] = ('Evidence','Portfolio','Certify')             # 服务 3 处
all_specs() count = 12
```

### 例 6：重复定义 AST 实证（D4-1 的证据）

```
spec.py 顶层 METHOD_STAGES/method_id 定义 = ['method_id','method_id']  count=2
```

用 AST 扫描 spec.py 顶层节点，`method_id` 函数定义出现两次（`METHOD_STAGES` 赋值两次），第二次覆盖第一次（`:21-34` 与 `:42-55`）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| S-1 | **`METHOD_STAGES` 字典与 `method_id` 函数各定义两次**（`:21-34` 与 `:42-55`），第二次覆盖第一次。当前值完全相同、功能无害，但违反"单一真相"：未来只改一处会遗漏另一处。 | **P2（已确认问题）** | 实测 AST：顶层 `method_id` 定义 `count=2`；`review_u6_u7_20261118.md §2.8` 亦记为 P2 | **已确认问题，建议修**：合并为一处。当前不改代码（本轮只读），列入 P2 清单 P2-1。注意：AST 守卫 `test_method_stage_tags_have_one_source` 只检查"@S 字面量只在 spec.py"，抓不到 spec.py 内部重复，故测试未红。 |
| S-2 | **`SERVES` 是"能力→服务工位"投影，不是"工位→能力"映射**。真正的工位→能力映射 `STATION_CAPABILITY` 在 `capability_bridge.py:47`（本模块 `:73-90` 只从能力侧声明）。两份映射需保持同步，无机器守卫。 | P2 | `spec.py:73-90`；`capability_bridge.py:47,1045-1048`；`live.py:55-57` 注释"同源" | **需确认意图**：方向不同（一个是能力元信息、一个是编排路由），分属两层合理；但"同源"靠注释约定，建议加一致性测试钉住。并入 R-1 同类守卫，不单独修代码。 |
| S-3 | **`_SPECS` 与 `CAPABILITIES`/`SERVES` 三张表需 key 完全一致**，`describe()` 用 `_SPECS[name]` 直接索引（`:181`），若某能力漏了 `_SPECS` 条目会 `KeyError` 而非 E2003。 | P2（健壮性） | `:181` `row=_SPECS[name]`；三表 key 当前一致（实测 12 能力齐全） | **需确认意图**：当前数据自洽、启动即装入（`__init__.py` 调 `install_default_impls` 不触发 describe 全表），`all_specs()` 会遍历 CAPABILITIES 并 describe——漏配会在首次 all_specs 时 KeyError。可在 `describe()` 加一层 E2003 兜底，属健壮性增强，非当前 bug。 |

**多了什么**：无冗余。`CAPABILITIES`/`SERVES`/`_SPECS` 都是 describe 投影所需；`_POLICY_SUBBLOCKS`（`:119`）是校验白名单。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 单一规格表 + 纯静态 `describe()` 投影 | 采纳 | 能力元信息一处定义；`describe` 无数据/结论/配置入口（结构禁令靠签名保证） |
| 标签单一构造入口（`method_id`） | 采纳 | G1/G2：`@S3/@S4/@S7` 只在 spec.py，拼错 E2003；`test_v5_package_isolation.py:146-167` AST 守卫 |
| `cacheable` 由副作用派生 | 采纳 | 不存恒定字段（原 `cacheable`/`deterministic`/`stateful` 全默认导致 describe 印三个恒定真假值，E10）；实测已删 |
| 一套默认值套所有能力（全 pure、全空 composes） | **拒绝** | 会让编译期子调用图与口径组装缺依据（P1-12）；`_SPECS` 逐能力声明（`:98-117`） |
| 散落 `@S4`/`@S7` 字面量到实现里 | **拒绝** | 组合域曾写 @S4 而实现已 S7，无法确定哪个是口径；`method_id` 收口（`s0/04:395`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **12 能力元信息齐全**（`test_v5_capability.py:158-186`）：`all_specs()` 实测 12；`verdict=fixed`、`search=open`；allocate 服务三处、backtest 服务四处（实测 `SERVES`）。
2. **`CapabilitySpec.__post_init__` 校验**（`:134-150`）：namespace/side_effect/policy_reads/composes 非法各 E2003（实测三条）；`policy_reads` 只许 caliber 四子块。
3. **`cacheable` 派生**（`:160-174`）：由 `side_effect != 'append_only'` 派生，deterministic/stateful 已删除（实测 describe 字典无这两键）。
4. **`method_id` 唯一入口 + E2003**（`:29-34`）：实测未登记方法 E2003，三个已登记方法返回正确 `@S` 标签。
5. **`describe()` 纯静态**（`:177-190`）：未登记能力 E2003；`params_schema_ref` 指向 `capability/<name>/params`（与 params.py 的 schema 对齐）。
6. **AST 守卫**（`test_v5_package_isolation.py:146-167`）：`@S\d` 字面量只允许出现在 spec.py，防止标签散落。

**发现的问题**：
- **S-1（P2，已确认）**：`METHOD_STAGES`/`method_id` 各定义两次（`:21-34` 与 `:42-55`），实测 AST `count=2`。当前值相同功能无害，但违反单一真相——列入 P2 清单（P2-1），建议合并为一处。**本轮只读未改**。
- 见 D2 表 S-2（工位映射方向分层，无守卫）、S-3（`_SPECS` 漏配 KeyError 健壮性）。**无 P0/P1 级代码 bug**。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md §3`（`:181,375,440,460`） | 12 能力名字空间、服务工位 | 与 `CAPABILITIES`/`SERVES` 一致，无需更新 |
| `s0/04-capability.md:60` | `cacheable` 在 describe 元信息 | 已与派生实现一致；可补一句"deterministic/stateful 已删除，确定性由 seed+call_digest 保证"（说明性） |
| `s0/04-capability.md:395` | `combine_domain@S7` 是指纹分界点 | 与 `METHOD_STAGES` 一致；合并重复定义后无契约变化，无需更新文档 |
| `s0/04-capability.md §5.1` / `11.md §5` | 四档名字空间与服务工位 | 补注"工位→能力映射 `STATION_CAPABILITY` 在编排层，能力→工位在 spec.SERVES，两处同源靠测试"（S-2，说明性） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增方法阶段标签（如 S8） | ⚠️ 半配置化 | 在 `METHOD_STAGES` 登记（须先合并 S-1 重复定义）；改它进 payload/domain 指纹属新试验族（`s0/04:20`） |
| 新增能力 | ❌ 需改代码 + 文档 | `CAPABILITIES`/`SERVES`/`_SPECS` 三表 + `capability_bridge.STATION_CAPABILITY`（S-2）；工位固定不变 |
| 改能力副作用/composes/policy_reads | ⚠️ 需改代码 | `_SPECS` 逐能力声明；影响编译期子调用图与口径组装（P1-12），改后须过契约测试 |
| `describe()` 加字段 | ⚠️ 需改代码 + 指纹评估 | `to_dict()` 加键影响元信息展示；若进指纹需走契约变更流程 |

---

## C1–C12 相关项判定

- **C1 365 年化 / C2 资金费按事件 / C3 市场维度 / C4 合约四元组 / C7 缺口 / C8 列集 / C9 as-of / C10 判据 / C11 流动性 / C12 embargo**：均不适用或直接无关（本模块是能力元信息/标签层，不含加密数值语义）
- **R5 归因底座**：✅ `method_id` 单一出处 + 逐能力元信息是单因子可归因的方法论基础（标签散落会让 domain_digest 口径漂移）

## R1–R7 影响

- **R1 真数据跑通**：中性（元信息层，随 import 装入）
- **R2 结论可信**：中性偏正——`policy_reads` 白名单是"能力只读声明过的口径子块"的底座（runtime `_check_policy_scope` 消费）
- **R5 单因子可归因**：✅ 正向——方法标签单一出处（`method_id`）、逐能力元信息支撑归因
- **R7 fail-closed**：✅ 正向——非法 namespace/policy_reads/composes/方法名全 E2003；`cacheable` 派生避免"恒默认字段冒充逐能力判断"

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过；有一处已确认 P2 建议修。** S-1：`METHOD_STAGES`/`method_id` 各定义两次（`:21-34` 与 `:42-55`，AST 实测 `count=2`），当前值相同功能无害，但违反单一真相——列入 P2 清单（P2-1），建议合并为一处，本轮只读未改。其余 S-2（工位映射方向分层）、S-3（`_SPECS` 漏配健壮性）均为需确认意图/健壮性增强。`CapabilitySpec`、`cacheable` 派生（deterministic/stateful 已删）、工位→能力映射、`method_id` 全部实测成立，是 R5 归因与 R7 fail-closed 的元信息底座。

**请确认本模块审查结果**：
- ✅ 确认通过（S-1 列入 P2 待修清单）→ 我进入第 4 个模块 `impls.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想把 S-1 当场裁决为 P1 立即修 → 告诉我
