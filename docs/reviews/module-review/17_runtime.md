# 模块审查 17 · `capability/runtime.py`

> 审查日期：2026-11-18
> 依据：源码 447 行全文通读；依赖 `params.py`（第 2/3/5 步校验）、`spec.py`（`describe`）、`contracts/digest.py:418`（`call_digest` 真公式）、`contracts/types.py:126`（`CALL_PHASES`）、`contracts/errors.py`；设计 `s0/04-capability.md §1`（六步协议/子调用）、`s0/01-data-contracts.md §2.2/§3.4`；测试 `test_v5_capability_runtime.py`（30+ 用例）、`test_v5_package_isolation.py:129-144`（AST 守卫）、`test_v5_executor_real.py:35-91`（工位块进指纹）；历史审查 `review_u6_u7_20261118.md §2.10`、`v5_fix_execution_20261118.md`（E1 两套 Call）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响；"生产可用=能真挖因子"（plan §0.0/§0.1）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Runtime 是能力调用的**唯一入口 `run(call)`**，回答五件事：

1. **六步协议、顺序即契约**：①重算 `call_digest` 与自身一致（E2002）→②按 param_schema 校验 params（E1003/E1004/E1008）→③输入数量/顺序/kind 一致（E3001）→③.5 子调用结构合法（E2008/E1020/E2001）→④执行实现体（E3004）→⑤返回结构校验（E3003）→⑥返回，**不落盘**（`:3-20`）。
2. **`Call` 全仓唯一定义**：六通道快照 + 四个指纹；`__post_init__` 校验字段名对得上契约表、phase 落进 `CALL_PHASES`、seed 显式（`:59-128`）。曾经契约层另有一份 16 字段 `Call`（存三个子摘要字段、校验更强却无人构造），两份并存让"哪个才算数"只能靠猜——合并后 AST 守卫钉死唯一性（`:61-77`）。
3. **三个子摘要派生、不存储**：`params_digest`/`policy_view_digest`/`runtime_facts_digest` 是 `params`/`policy_view`/`runtime_facts` 的纯函数（`:130-151`）。存储只会多出一个能与本体分叉的副本（V5 反复吃亏的正是此类）；`call_digest` 存字段，因它是"这单调用"的身份且第 1 步要比对。
4. **第 3.5 步子调用护栏**：`policy_view` 只含调用链声明过的口径子块（父+子并集），声明外即 E2001；子调用必须在父 `composes` 声明内（E2008）、同工位（E2001）、共享同一 `policy_view`（E1020）、深度 ≤ 2 且无自递归（`:332-406`）。防止"证据层用便宜成本判可交易、组合层用昂贵成本回测"在同一工位内发生。
5. **实现解析与异常兜底**：`single/fixed` 单实现、`closed_multi` 白名单、`open` 允许新增但未实现不得跑；任何未预期异常转 E3004，不当成功（`:269-324,430-445`）。

**加密特化**：本模块是调用协议底座，不含加密数值语义；但 `call_digest` 含工位块全部内容（impl+params+time_axis 作为 boundary.params 一部分）+ `phase`+`seed`，是 R5 单因子可归因、R7 "口径与判据不可静默改写"的机器保证。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 用仓库 `venv/Scripts/python.exe` 调用 `capability.runtime` 各函数**实际打印**。
> 复现脚本：`C:/Users/lansa/AppData/Local/Temp/fm_probe_impls_runtime.py` 的 RUNTIME 段 + AST 扫描。

### 例 1：`Call.create` + 派生指纹（真实输出）

```
Call.create(capability='evaluate', station='Evidence', impl='qweave', impl_version='1',
            params={'op':'sum'}, inputs=(InputRef('payload','sha256:'+'a'*64),))
call_digest = sha256:e22ba819e05561d7ad68e1d95219ea9d64c76a521ff51dc69ddd0c5b58d32bd7
params_digest == H(params)?        True
policy_view_digest == H({})?       True
runtime_facts_digest (empty) = sha256:c0ad96853402ab103...
dataclass fields = ['capability','station','station_revision','impl','impl_version',
                    'phase','params','inputs','policy_view','runtime_facts',
                    'composed','seed','call_digest']   # 13 字段；三个子摘要不在其中
```

**关键**：`call_digest` 存字段；`params_digest`/`policy_view_digest`/`runtime_facts_digest` 是属性、不在 dataclass 字段里——实测字段列表无这三个名（符合 15_spec.md 同源设计，无"另一份值可漂移"）。

### 例 2：工位块整体进 call_digest（真实输出，R5 核心证据）

基于 `boundary` 调用单（params 含 `time_axis`），逐项改动看指纹是否变化：

| 改动 | call_digest 是否变化 |
|---|---|
| `impl`: pit_dynamic_topk → other | **True** |
| `impl_version`: 1 → 2 | **True** |
| `params.k`: 3 → 4 | **True** |
| `params.time_axis.oos`: b → c | **True** |
| `station_revision`: r1 → r2 | **True** |

`impl`+`impl_version`+`params`（含 `time_axis`）+`station@revision` 任一变都换指纹——工位块整体进 `call_digest`（真公式 `dg.call_digest`，`digest.py:418`）。`params_digest=H(params)` 单独变也换指纹（实测）。

### 例 3：`__post_init__` / `InputRef` 校验失败路径（真实错误码）

| 非法输入 | 输出 |
|---|---|
| `Call.create(..., impl='')` | `E1004` "Call.impl 必须是非空字符串" |
| `Call.create(..., phase='fitted')` | `E1004` "Call.phase='fitted' 不在 ['none','fit','generate']" |
| `Call.create(..., seed=True)` | `E1004` "Call.seed 必须是 int 或显式 null" |
| `Call.create(..., inputs=('notdigestref',))` | `E1004` "Call.inputs[0] 必须是 InputRef 或 None，得到 str" |
| `InputRef('','sha256:...')` | `E2003` "InputRef.kind 不能为空" |
| `InputRef('panel','')` | `E2003` "InputRef.digest 不能为空" |
| 构造后 `call.params['op']='mean'` 再 `verify()` | `E2002` "call_digest 与内容不符（调用单被改写）" |

`None` 是可选输入占位符（`ABSENT_DIGEST=sha256:af4a424ce7558ea75...`），实测保留在 inputs 原位不被当脏数据拒；手写 `Call(..., call_digest='')` → `E2002`（`runtime.py:199-212`）。

### 例 4：`run()` 成功 / 失败（真实输出）

```
run(valid evaluate) -> keys = ['node_stats','operators_used','valid_mask','valid_mask_ref','values','values_ref']
run(impl 缺失路径)  -> E1008   # 空 impl/impl_version 在 run 内拒（:422-429）
```

### 例 5：AST 守卫防第二份 Call（D4-1 证据，真实扫描）

```
AST: class Call definitions in repo = ['capability/runtime.py']
```

用 AST 遍历八个顶层包的全部 .py 的顶层 `ClassDef`，名为 `Call` 的仅 `capability/runtime.py` 一处——`test_v5_package_isolation.py:129-144` 钉死：加回第二份会红。

### 例 6：子调用校验失败路径（`test_v5_capability_runtime.py` 实测）

| 场景 | 错误码 |
|---|---|
| observe 调用未声明的 evaluate 子能力 | `E2008` |
| 子调用 policy_view 与父不同 | `E1020` |
| 读未声明的口径子块（evaluate 读 net） | `E2001` |
| 子调用工位与父不同 | `E2001` |
| 子调用链深度 > 2 / 自递归 | `E2008` |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| RT-1 | **静态子能力读口径检查缺位**：`_declared_reads` 按"父+子声明的并集"校验，残留风险是子实现可能读兄弟能力声明、而自己没声明的口径子块——运行时无法消除（共享 policy_view 是契约本身），需 C5 式静态检查（能力体内读 `policy_view` 的键 ⊆ 自身 `policy_reads`）。 | P2（已知残留风险） | `:348-363` 注释明确说明；`20-mining-algorithms.md`/`s0/04 §1.3` | **已确认有意的残留风险**：注释写明"只能靠静态检查消除"，当前运行时做不到。属已知的架构权衡，非 bug；若要闭合需引入 AST 静态分析（另立任务），不单独改本模块。 |
| RT-2 | **`validate_params`/`validate_inputs` 每调用都重新读 schema**（`params.schemas()` 有缓存，`:307-310`），但 `schema_for` 走 `schemas()` 深拷贝——每次 run 都深拷贝整份 schema。高频调用下有性能开销（非正确性问题）。 | P2（性能观察） | `runtime.py:416-418` 调 `validate_params`；`params.py:350-354,300-310` `schema_for→schemas()→deepcopy` | **需确认意图**：深拷贝是防契约被改写的安全取舍；当前 run 非高频热点（每次工位执行一次），影响有限。记录为观察项，不作为改动。 |
| RT-3 | **`Call` 的 `params`/`policy_view`/`runtime_facts` 是 `Mapping` 类型，但 `create` 用 `dict(...)` 浅拷贝**（`:171-177`）。嵌套 dict（如 `time_axis`）仍是同一对象引用，构造后改嵌套值仍可被 `verify` 抓到（实测 in-place 改 params 顶层 key → E2002），但嵌套对象的相等性依赖 `compute_digest` 递归。 | P2（说明） | `:171-179`；实测顶层改写 E2002；`test_v5_capability_runtime.py:205-209` | **已确认符合设计**：`verify` 靠重算 digest 比对，嵌套改写同样导致失配（只要 compute_digest 递归纳入）；无需深拷贝三通道。记录确认。 |

**多了什么**：无冗余。`MAX_COMPOSE_DEPTH=2`、`ABSENT_DIGEST`、`ImplFn` 类型别名都是必要常量；`run` 的 except 分层（FlowError 透传 / NotImplementedError→E3004 / 其他→E3004）是必要的异常边界。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 内容寻址调用单（`call_digest` 唯一身份） | 采纳 | 六通道 + phase + seed + 工位块全进指纹；任一变→下游整条指纹链变，缓存不串 |
| immutable value object + 派生子摘要 | 采纳 | 三个子摘要是纯函数属性、不存储；`Call` frozen + `__post_init__` 校验；无副本可漂移 |
| 唯一入口 + 六步校验流水线 | 采纳 | 顺序即契约；任何失败 fail-closed 返回明确错误码，不静默降级 |
| 第二套 `Call` 定义（契约层 16 字段版） | **拒绝（已删）** | 两份并存让"哪个算数"靠猜、校验强度不同→同输入走不同码；AST 守卫钉死唯一性（E1，实测仅一处） |
| `call_digest` 存三个子摘要字段 | **拒绝** | 存储=多一个能与本体分叉的副本；改为派生属性（15_spec.md 同源，实测字段列表无子摘要） |
| `impl=''` 静默回落唯一实现 | **拒绝** | digest 记 `impl@''`、实际跑另一实现；Open 档所有算法共享一条指纹——构造时即 E1004（`:422-429`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **`Call` 全仓唯一定义**（`:59-128`）：AST 实测全仓仅 `capability/runtime.py` 一处；`test_v5_package_isolation.py:129-144` 守卫。
2. **`__post_init__` 校验**（`:93-128`）：5 个非空串、phase 枚举（实测 `fitted`→E1004）、三通道 Mapping、seed 显式（实测 bool→E1004）、inputs 类型/None 占位、composed 类型——全部实测。
3. **三个子摘要派生不存储**（`:130-151`）：实测 `params_digest==H(params)`、`policy_view_digest==H({})`；dataclass 字段无子摘要。
4. **`compute_digest` 含全部通道**（`:181-197` + `digest.py:418`）：实测工位块（impl/version/params/time_axis/station_revision）各变均换指纹；`phase`/`seed` 进指纹（`test_v5_capability_runtime.py:168-190`、`test_v5_contracts.py:181-198`）。
5. **`verify` 抓就地改写**（`:199-212`）：实测改 `call.params['op']` 后 E2002；`run` 也返回 E2002。
6. **`InputRef` 校验**（`:44-56`）：kind/digest 非空（实测空值 E2003）；`data` 字段 `compare=False,repr=False` 不进指纹（只 kind+digest 进）。
7. **子调用结构校验**（`:332-406`）：声明/工位/共享 policy/深度/自递归全检（实测各类 E2008/E2001/E1020）。
8. **`run` 唯一入口 + 异常兜底**（`:410-447`）：六步顺序；FlowError 透传、NotImplementedError/其他异常→E3004；`impl=''`/`impl_version=''` 在 run 内 E1008（实测）；返回前 `validate_outputs` 按 params 选档。
9. **实现解析四档**（`:269-324`）：single/fixed 单实现、closed_multi 白名单、open 允许新增但未实现 E3004、版本不符 E1002（测试 `:425-465` 全覆盖）。
10. **`ABSENT_DIGEST` 区分 None 占位与少传一项**（`:38-41,190-192`）：实测 None 保留原位、独立指纹，避免"少传让后继前移"的静默错位。

**发现的问题**：
- 见 D2 表 RT-1（静态子能力读口径检查缺位——已知架构权衡，注释明确）、RT-2（schema 深拷贝性能观察）、RT-3（浅拷贝+digest 递归，符合设计）。**无 P0/P1 级代码 bug**。测试覆盖充分：`test_v5_capability_runtime.py` 30+ 用例覆盖六步全路径、指纹全通道、子调用、实现注册表；`test_v5_executor_real.py:35-91` 钉住工位块（impl/params/time_axis）进 call_digest；`test_v5_package_isolation.py:129-144` AST 守卫。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md §1`（六步协议/子调用） | 与实现一致 | 无需更新 |
| `s0/01-data-contracts.md §2.2/§3.4` | call_digest 公式 + CALL_PHASES | 真公式在 `digest.py:418`（含 phase/capability/input_kinds，比文档 §2.2 列的多）；文档应补注"phase/capability/input_kinds 也必须进指纹"（`digest.py:428-448` 已说明理由），消解文档与公式的表观差异（说明性） |
| `s0/04-capability.md §1.3` | 父子共享 policy_view | 补注 RT-1"子实现读兄弟声明口径的残留风险只能靠静态检查消除"，明确当前运行时边界（说明性） |
| `20-mining-algorithms.md §4`（add algorithm） | 新算法只动一处 | 与 `open` 名字空间 + `_RUNNERS` 分派一致；无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新能力接入 `run` | ⚠️ 需改代码 + 文档 | 需 `spec.CAPABILITIES`/`SERVES`/`_SPECS` + `impls` + `params.json` + `bridge.STATION_CAPABILITY` 同步；工位固定不变 |
| 新子调用组合（composes） | ⚠️ 需改代码 | `_SPECS` 加 composes 声明 + `policy_reads`；运行时自动按声明校验（无需改 runtime） |
| 新算法（open 档） | ✅ 基本支持 | `open` 名字空间允许未登记名；但"未实现不得跑"靠 impls 表/ `_RUNNERS`——须在 `search` 层登记（见 16_impls.md I-1） |
| 新增口径子块（caliber） | ❌ 当前封闭 | `policy_reads` 只许 execution/label/allocate/net 四块；加第五块需改 `_POLICY_SUBBLOCKS` + 校验（契约变更） |
| 提高子调用深度上限 | ⚠️ 需改代码 | `MAX_COMPOSE_DEPTH=2` 常量，改它属契约变更（影响可组装结构，须评估指纹） |

---

## C1–C12 相关项判定

- **C1 365 年化 / C2 资金费按事件 / C3 市场维度 / C4 合约四元组 / C5 计价货币 / C6 上市下架 / C7 缺口 / C8 列集 / C9 as-of / C11 流动性 / C12 embargo**：均不直接适用（本模块是调用协议层，不含加密数值语义）
- **C10 判据不得被加密波动放松**：✅ 底座支撑——`policy_view_digest`/`params_digest` 进 call_digest，判据通道任一变即换指纹；`verify` 抓就地改写（实测 E2002），防运行期偷改口径/判据

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向——唯一入口六步校验，生产路径不静默降级
- **R2 结论可信**：✅ 强支撑——子调用共享 policy_view、深度/工位/声明全检；判据通道进指纹且防改写
- **R5 单因子可归因**：✅ 强支撑——工位块（impl+params+time_axis）整体进 call_digest（实测各变均换指纹），单因子可精确归因
- **R7 fail-closed**：✅ 强支撑——六步全拒路径 + AST 守卫唯一 Call + 派生指纹无副本；收敛到"生产可用=能真挖因子"的协议底座

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过，5 个模块中协议地基最稳。** `Call` 唯一实现（AST 实测仅一处）+ `__post_init__` 全校验 + 三个派生指纹（不存字段）+ `call_digest` 字段 + AST 守卫 + 工位块（impl+params+time_axis）整体进指纹 + `InputRef`，全部实测成立。三处 P2 均为已确认的架构权衡/观察：RT-1 静态读口径检查缺位（注释明确、需另立静态分析任务）、RT-2 schema 深拷贝性能观察、RT-3 浅拷贝+digest 递归（符合设计）。是 R5 单因子可归因与 R7 fail-closed 的机器保证。

**请确认本模块审查结果**：
- ✅ 确认通过 → 5 份审查文档全部完成，我汇报总结（路径 + 结论 + P0/P1 清单）
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想把 RT-1（静态读口径检查）升为独立任务 → 告诉我
