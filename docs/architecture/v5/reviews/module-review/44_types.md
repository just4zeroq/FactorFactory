# 模块审查 44 · `v5/contracts/types.py`

> 审查日期：2026-11-18
> 依据：源码 835 行全文通读；依赖 `digest.py`（assert_relative_path / compute_digest）、`errors.py`（E1004/E1007/E2003/E2004/E2005）、下游唯一 `Call` 定义 `capability/runtime.py:60`；设计 `s0/01-data-contracts.md` §3（七类核心结构）、`05-contracts.md`；测试 `tests/v5/test_v5_contracts.py`（37 例）+ `test_v5_capability_runtime.py`（42 例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1，Anaconda）实跑临时脚本 `C:/Users/lansa/AppData/Local/Temp/fm_probe_contracts.py`，输出见下（非推演）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`types.py` 是全系统**唯一类型字典**。它该做的只有一件事：把跨层流转的概念各定义成一个**不可变、可序列化、构造期自校验**的值对象，让"同一个概念在不同层含义不同"这类缺陷在构造期就暴露，而不是等数字对不上时才发现。三条通用规则（源码 docstring:13-17）：

1. **值对象不可变**：改一律产新实例（唯一例外 `Ticket` 的两个可变字段）。
2. **不存活对象**：字段不得是模型实例、连接句柄、生成器、内存视图。
3. **可序列化即可传递**：不能无损写 JSON 的字段不进跨层结构。

七类核心结构及边界：

| 类型 | 职责 | 关键不变量 |
|---|---|---|
| `Digest` | 内容指纹值对象 | 只收 `sha256:<64 位小写 hex>`，拒裸 hex/大写/未知 algo |
| `Ref` | "在哪、是什么、哪个版本"的引用，**不携带内容** | 持有者解析时必须重验 digest；relpath 构造期归一 `\`→`/` 并拒绝对路径/`..` |
| `Pin` | 供给版本引用 | `digest_view()` 排除 `acquired_at`（会随时间变，不得进指纹）；`end_closed=false` 不可被别名指向 |
| `Evidence` | 只允许事实 | 字段白名单 + 结论词检测；结论只能由 Sentinel 写进 VerdictParcel |
| `Parcel` | 不可变落盘产物 | `failed` 必带 `error`、`skipped` 必带 `skip_reason`；`seconds>=0` |
| `Ticket` | 一次执行状态主体 | 只有 `status`/`archived_at` 可变，其余字段改写即 `E2005` |
| `Blueprint` | 冻结方案 | `origins` 必须覆盖 `values` 每个叶子，缺一个即 `E1007` |

**三条硬边界**：
- **`Call` 不属于这里**。它带只读载荷通道 `InputRef.data`（面板数组等不可序列化内容），唯一定义在能力层 `capability/runtime.py:60`；契约层只持久化其**结果**（`Parcel.call_digest`）。`CALL_PHASES` 作为契约枚举留在本模块供能力层 import（`types.py:478`）。
- **不得有第二份"差不多"的结构**（docstring:4-6）——两套并存时无检查点能发现。
- **校验强度统一**：必填缺失 `E2003`、未知字段 `E1004`、结论词 `E2004`、不可变改写 `E2005`、origins 缺口 `E1007`——错误码是排查第一信息。

**加密特化**：本模块是纯结构层，不含任何年化/日历/资金费逻辑，C1–C12 大多"不适用"；它承载的是"供给事实版本可追溯、时间一律 UTC、路径与耗时不进指纹"这类加密数据版本链的底座不变量（C7 缺口依赖 `Pin.end_closed`、C6 可用时间依赖 `domain.available_from` 的字段存在性）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下均为 `venv/Scripts/python.exe` 调用真实函数打印的结果。

### 例 1 · 七类结构构造 + 不变量（成功路径）

```
Digest.md5 拒绝              : E1004
Digest.bad hex 拒绝           : E1004
Ref 反斜杠归一                : parcels\panel\abc  ->  parcels/panel/abc
Pin.digest_view keys          : ['checksum','coverage','dataset','end_closed','gate_a',
                                 'schema_version','symbols_digest','version']
Pin.digest_view has acquired_at: False
Evidence gate_pass 拒绝        : E2004
Evidence allows rank_ic/raw_score/turnover: True / True / True
Ticket.status 可变            : pending -> running
Ticket.plan_digest 不可变       : E2005
Parcel skipped 无 reason 拒绝  : E2003
Parcel failed 无 error 拒绝    : E2003
Blueprint origins 缺 search.pop: E1007  missing=['search.pop']
leaf_paths({'a':{'b':1,'c':{}},'d':2}): ['a.b','a.c','d']
PARCEL_ORIGINS: ('run','step','commit','resume','cache')
EVIDENCE_KINDS: ('observation','diagnostic','count')
```

**逐条解读**：
- `Pin.digest_view` 的 8 个键**不含 `acquired_at`**——这是"采集时间不得进任何指纹"的硬性排除（`types.py:315-336`），重采逐字节相同的数据不会让下游全失效。
- `Evidence` 用**分词后集合相交**检测结论词（`types.py:427-435`）：`gate_pass` 命中 `pass` → E2004；而 `rank_ic`/`raw_score`/`turnover` 是合法观察量名，放行——避免"按子串拒绝会误伤真正事实"（源码:395-397）。字段名层面另有 `EVIDENCE_FIELDS` 白名单兜底。
- `Ticket` 用 `__setattr__` + `TICKET_MUTABLE_FIELDS` 白名单（`types.py:645-656`）而不是"前 N 个字段不可变"，增删字段时不会数错——改 `plan_digest` 当场 E2005。
- `Blueprint.__post_init__` 用 `leaf_paths(values) - origins` 求差集（`types.py:742-752`），空映射也算叶子（"显式为空"与"没写"可区分，`leaf_paths:801-814`）。

### 例 2 · AST 守卫：`Call` 唯一性（本模块核心红线）

> 用 Python `ast` 扫描 `v5/contracts/*.py` 全部类的 `ClassDef`：

```
Call definitions under contracts/: NONE (符合红线)
Call definition in capability/runtime.py lines: [60]
CALL_PHASES in contracts = ('none', 'fit', 'generate')
```

`types.py:467-478` 有一段长注释记录历史：这里曾有一份 16 字段纯数据版 `Call`（带 `params_digest`/`policy_digest`/`runtime_facts_digest` 存储字段），**生产代码一处都不构造**，且校验比真实类型强（那版拦 phase 枚举、真实版当时不拦）→ 已删除。当前 AST 实测确认契约层零 `Call` 定义，唯一形态在 `capability/runtime.py:60`，`CALL_PHASES` 仍作为契约枚举留在 `types.py:126`。**红线成立，无第二份。**

### 失败路径 → 错误码（均实测）

| 非法输入 | 错误码 |
|---|---|
| `Digest(algo='md5', ...)` | E1004 |
| `Digest(hex='ABC')`（非 64 小写） | E1004 |
| `Ref(relpath='../escape')` / 绝对路径 | E2002（经 `assert_relative_path`） |
| `Evidence(name='gate_pass')` | E2004 |
| `Evidence(kind='verdict')` | E1004（枚举） |
| `Ticket(ticket_id='ticket-1')` | E1004（格式正则） |
| 改写 `Ticket.plan_digest` / `.pins` | E2005 |
| `Parcel(status='skipped', skip_reason=None)` | E2003 |
| `Blueprint(origins 漏叶子)` | E1007 |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| T-1 | **无死类型 / 无冗余类型**。七类结构全部被下游消费，`__all__`（:817-835）与导出一一对应；历史上的死 `Call` 已删。 | —（正面） | AST 扫描 contracts/ 无 Call；源码:467-478 注释 + 实测 | **已确认干净**。835 行的体量来自逐字段构造期校验（每类 20–90 行 to_dict/from_dict/__post_init__），不是冗余堆叠。 |
| T-2 | `Parcel.error: Any`（:503）无类型约束，只校验 `failed` 时非 None（:528-534）；`to_dict` 用 `_plain(self.error)`（:582）兜底。错误对象字典化全靠 `_plain` 的 duck-typing。 | P2 | `types.py:503,528-534,582` | **需确认意图**：错误对象带 retryable 但不带 exit_code（属 `errors.py` 契约），此处宽松接收是为容纳 `Error`/dict/异常。可复现性无碍（`_plain` 确定性），但类型契约偏弱。低优先。 |
| T-3 | `Ticket` 是七个值对象中唯一 `@dataclass(slots=True)` 非 frozen 的例外（:594），靠 `__setattr__` 拦截实现"准不可变"。用户审查点明确承认这是设计例外。 | —（设计确认） | `types.py:19-20,594,645-656` | **已确认符合设计**。白名单法比"前 N 字段不可变"稳健。 |
| T-4 | `CALL_PHASES` 枚举驻留契约层（:126）但唯一 `Call` 在能力层——枚举与结构分处两层。 | P2 | `types.py:126,478`；`runtime.py:60` | **需确认意图**：当初这样切是为"能力层 import 契约枚举、契约层不持有 Call"。若将来 phase 增加，需确认两层同步更新路径；当前 `E2002` 测试只钉住 phase 进 call_digest。属说明性缺口。 |

**多了什么**：无明显多余。`__all__` 导出 21 个名字全部有下游引用；`leaf_paths` 是 Blueprint 校验的必要辅助，非死代码。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| git object 模型 / content-addressed 值对象 | 采纳 | `Digest`+`Ref` 将"内容身份"与"存放位置"分离，重采同内容不改指纹（`Pin.digest_view` 排除 acquired_at，实测 True） |
| Typed error taxonomy（按"谁该修"分层） | 采纳 | 构造期即带码（E1004/E2003/E2004/E2005/E1007），与 `errors.py` 的 5 层退出码映射贯通 |
| 不可变 dataclass + slots | 采纳 | 值对象 frozen+slots，杜绝运行期改写；`Ticket` 白名单是唯一例外 |
| JSON Schema 生成的动态模型（pydantic） | 拒绝 | 本层要求"零运行时魔法、纯 dataclass + 显式 __post_init__"；schema 唯一真相放在 `plan_schema.json` 与能力层 `param_schema`，类型层不重复做 schema 校验 |
| "差不多就复制一个结构"到各层 | 拒绝（红线） | 源码 docstring:4-6 与历史死 `Call` 删除（:467-478）正是为此；AST 实测守卫通过 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **无第二份类型字典**：AST 扫描 `contracts/` 零 `Call`；唯一在 `runtime.py:60`（实测）。死类型已清理。
2. **值对象不可变**：`Digest/Ref/Pin/Evidence/Parcel/Blueprint` 全部 `frozen=True slots=True`；`Ticket` 改 `plan_digest` 即 E2005（实测）。
3. **路径归一 + 注入防护**：`Ref.__post_init__` 经 `assert_relative_path`（:216）把 `\`→`/`、拒 `..`/绝对路径（实测 E2002），指纹不留平台分隔符。
4. **时间不进指纹**：`Pin.digest_view()` 排除 `acquired_at`（:315-336），实测 8 键无该字段；配合 `digest.py` 的 supply_digest 投影双保险。
5. **Evidence 只报事实**：字段白名单 `EVIDENCE_FIELDS`（:393）+ 结论词分词检测（:398-435），`gate_pass` 拒、`rank_ic` 放行（实测），误伤防护到位。
6. **Parcel 语义完整**：`skipped`/`failed` 分别强制 `skip_reason`/`error`（:521-534），`seconds>=0`（:543-544），实测均 E2003。
7. **Blueprint origins 全覆盖**：`leaf_paths` 差集校验（:742-752），空映射算叶子，缺叶子 E1007（实测 missing=['search.pop']）。
8. **必填/未知字段分离**：`_require_keys`→E2003、`_reject_unknown`→E1004（:75-94），不补默认值（"用户没写"与"写了默认值"可区分）。

**发现的问题**：
- 见 D2 表 T-2（`error: Any` 类型弱）、T-4（CALL_PHASES 与 Call 分层的同步路径靠约定）。**无 P0/P1 级 bug**。测试覆盖充分（contracts 37 例 + capability_runtime 42 例，含不可变、结论词、origins、错误码全表）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/01-data-contracts.md` §3（七类结构） | 与实现逐类一致 | 无需更新 |
| `s0/01-data-contracts.md` §2.2 | "时间/路径/耗时不进指纹"已写明 | 无需更新；`Pin.digest_view` 是这条的实现落点 |
| `05-contracts.md` | 类型字典描述与 7 类一致 | 可补一句"AST 守卫确保 Call 只在 capability/runtime"，把口头红线写进文档（说明性，非契约变更） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增错误码 | ⚠️ 需两处同步 | `errors.py` 加常量 + `ALL_ERROR_CODES` + 文档 §4.2（见 46_errors.md）；本模块只消费不定义 |
| 新增一类跨层结构 | ❌ 需改代码 + 文档 | 必须进 `types.py` + `__init__.py` 导出 + `s0/01` §3，不是纯配置（这是刻意收紧） |
| 新增 `Evidence` 事实字段 | ⚠️ 半配置化 | 改 `EVIDENCE_FIELDS` 白名单（:393）即可，结论词检测自动生效 |
| 新增 `Parcel.origin`（如 `altered`） | ⚠️ 需多文件同步 | `PARCEL_ORIGINS`(:127) + `trace.py:35` 的 origin 集合两处（实测当前一致），需同步 |
| 加新工位字段且不破坏指纹 | ✅ 支持 | 只要不进各类 digest_view / 构造期投影，`frozen` 值对象加字段不影响既有指纹语义 |

---

## C1–C12 相关项判定

本模块为纯结构层，无业务计算，多数条目不适用：

- **C1 365 年化**：不适用（无年化逻辑）
- **C6 上市/下架**：✅ 结构性支撑——`domain` 成员要求 `available_from` 字段存在（在 digest.py 强制，`types.py` Blueprint.domain 承载）
- **C7 24/7 缺口**：✅ 结构性支撑——`Pin.end_closed` 是 bool 且在 digest_view 内（:330），上游边界判未收盘靠此事实
- **C9 as-of 对齐**：不适用（无填充逻辑）
- 其余 C2/C3/C4/C5/C8/C10/C11/C12：不适用（数据列、市场维度、判据、分层均不在结构层）

## R1–R7 影响

- **R1 真数据跑通**：中性（结构层，已被真跑反复使用）
- **R2 结论可信**：✅ 强支撑——不可变 + 构造期校验 + 时间不进指纹，是所有可信结论的底座
- **R3 因子零代码扩展**：中性偏正——`Evidence` 白名单允许新观察量名（`rank_ic` 类）零改代码进 Evidence
- **R7 fail-closed**：✅ 强支撑——必填缺失、未知字段、结论词、不可变改写、origins 缺口全部当场拒绝

---

## 结论

**本模块代码质量高，835 行全部是有信息的构造期校验与序列化逻辑，无冗余/死类型，无 P0/P1 级 bug，可确认通过。** 用户三个必查点逐条落地：①死类型——历史死 `Call` 已删、AST 实测 contracts/ 零 Call、唯一 Call 在 runtime.py:60，红线成立；②Call 唯一性——`CALL_PHASES` 作为契约枚举留本层，结构在能力层，分工清晰；③Parcel/Scope/Evidence 不变量——Parcel 状态-理由配对、Evidence 事实白名单 + 结论词分词检测均实测正确。`Scope` 不在本模块（在 `capability/stream.py`，其 `tradable⊆observable` 不变量见 01/06 号文档）。两条 P2 均为说明性/需确认意图，不影响生产可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 2 个模块 `digest.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
