# 模块审查 45 · `v5/contracts/digest.py`

> 审查日期：2026-11-18
> 依据：源码 497 行全文通读；依赖 `errors.py`（E2002/FlowError）；设计 `s0/01-data-contracts.md` §2.1–2.3（规范化八规则 + 八指纹公式 + 什么进指纹表）、`05-contracts.md`；测试 `tests/v5/test_v5_contracts.py` D1/D3–D5/D10（37 例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `C:/Users/lansa/AppData/Local/Temp/fm_probe_contracts.py` 的 E3/E3b 段。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`digest.py` 是全系统**指纹公式的唯一真相**。所有产物身份、缓存键、口径锚点都落到它算出的 `sha256:<64hex>` 上。它该做的三件事：

1. **规范化（canonicalization）**：把"同一逻辑值的多种写法"收敛到唯一字节串。八条规则（源码:9-22）：键按 UTF-8 字节序、`;`/`:` 后无空白、整数不带小数点且 `1`≡`1.0`、`-0.0`≡`0`、NaN/Inf 拒绝、Unicode NFC、时间统一 UTC RFC3339 微秒、路径相对 `/` 且禁 `..`、无序集合按元素 digest 升序、null≠缺失。
2. **唯一公式**：`H(x) = "sha256:" + hex(sha256(canonical_bytes(x)))`（:226-228）。指纹**只作用在规范化字节串**上，不作用在内存对象或原始字符串。
3. **八指纹 + 两个专用摘要**：在规范化之上，为不同语义域提供带 `kind` 包装的指纹函数，`kind` 保证"同一个 dict 当 plan 与当 caliber 不撞指纹"（实测 `plan_digest(raw) != compute_digest(raw)` True）。

**八指纹分组**（`s0/01 §2.2`，理由比公式重要）：

| 组 | 指纹 | kind | 变化意味着 |
|---|---|---|---|
| 意图与口径 | `plan_digest` | `plan` | 用户写的计划原文变了 |
| | `caliber_digest` | `caliber` | 四子块口径（execution/label/allocate/net）变了 |
| | `domain_digest` | `domain` | 组合域成员（含 available_from）变了 |
| | `policy_digest` | `policy` | 试验族策略变了（**不含数据版本**） |
| | `blueprint_digest` | `blueprint` | 展开后方案变了（**含数据版本**） |
| 物化 | `supply_digest` | `supply` | 用的事实版本变了（**排除 acquired_at**） |
| | `environment_digest` | `environment` | 环境变了（**不进任何产物指纹**，仅排查用） |
| | `call_digest` / `parcel_digest` | `call` / `parcel` | 一次调用 / 一个产物的身份 |

**硬性排除项**（"只要一个字段会随时间或机器变化，就不能进任何指纹"，源码:244-248）：
- 时间戳：`acquired_at`（supply 投影排除）、`started_at`/`seconds`（parcel_digest 签名只有 call/payload）、`compiled_at`（Blueprint 不进指纹）。
- 路径：`relpath` 归一后出现在 `Ref`（内容寻址，不随机器变化）；`assert_relative_path` 拒绝对路径。
- 耗时：任何 `seconds`/`duration` 都只在观测字段，不进哈希输入。
- 滚动计数：`runtime_facts_digest` 收的是"本次执行读到的快照"（改变判据→必须让缓存失效），不是账本里每次都不同的滚动数字。

**性能护栏**：`MAX_INLINE_SEQUENCE=8192`（:44）——大数组必须落 `Ref`，禁止把整个面板塞进指纹（性能事故最常见形态）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 全部 `venv/Scripts/python.exe` 实测输出。

### 例 1 · 十一个指纹函数的真实输出（含八指纹 + 两专用 + 基础）

```
  plan_digest         = sha256:a03917a5ae0397d7bd5d4fe439f3bb02c5f25937f9f7255b15af54c59ef92ced
  caliber_digest      = sha256:78deb234cba875548e66858476f633abad1b02813c8c75dc77cb16d8f2f95639
  domain_digest       = sha256:7e5533b8c10a1383a42fc31d4d55a2951d6b939b1ce77238626f901c11faedc9
  supply_digest       = sha256:1b40b3f5d41896692ae2d16fb31f5488ffed0c39a61b1bf6d8b2090bdefce238
  environment_digest  = sha256:33863f49c2e675080c255e003eedac5fc5b2803b933c28bb90f0aa0c7306199f
  policy_digest       = sha256:3df3ac65957729402163c17d644e02e5f4aae9724f02df9458545a22b8117d29
  blueprint_digest    = sha256:000d7d246397b5085173e4ff769dfb294d09f7822f982e87b653ddd25e1e7516
  runtime_facts_digest= sha256:97d0894b036c2c68397d827fd6fb828ed7f58c69fdddd3fdced709a78cf468b8
  call_digest         = sha256:f5b5b9bfbd4316e07d534190c515e92f98fc5325a5290653fcdfbb0f70008ba2
  parcel_digest       = sha256:1cb0697ededab0b514380d82eade38e2aba237a87682949a1c6119dc4e92dd15
```

输入（节选，均为确定性构造）：`plan_digest({"plan":"x"})`；`call_digest(station="Panel",...,phase="generate",seed=7,...)`；`supply_digest(pins)` 中 pin 含 `acquired_at/path/note/updated_at` 等干扰字段。

### 例 2 · 时间/路径/耗时不进指纹（用户必查点，实测）

```
supply_digest 改 acquired_at/path/note/updated_at 后相同: True
  p1 = sha256:1b40b3f5...ce238
  p2 = sha256:1b40b3f5...ce238     # acquired_at 从 11-16 改到 12-31、path 换机器、加 note/updated_at
parcel_digest signature = ['call', 'payload']     # 无 started_at / seconds / 时间戳入口
kind 隔离 plan_digest(raw) != compute_digest(raw): True
key-order invariant: True
int/float normalized (1 vs 1.0): True
```

**解读**：同一份 pin，无论采集时间、磁盘路径、备注怎么变，`supply_digest` 逐字节一致——这是"重采逐字节相同的数据不该让全部下游产物失效"的实测证明（源码:305-315）。`parcel_digest` 的签名里根本没有时间/耗时参数。

### 例 3 · 规范化八规则 + 失败路径

```
NaN 输入          -> E2002  exit_code=3
内联序列 9000>8192 -> E2002  (MAX_INLINE_SEQUENCE=8192，hint 指向 Ref)
路径 '../escape'  -> E2002
naive datetime    -> E2002  ("必须带时区")
bytes 直接进指纹   -> E2002  (须落盘后用 Ref)
```

| 非法输入 | 错误码 | 设计意图 |
|---|---|---|
| `compute_digest({'x': nan/inf})` | E2002 | NaN/Inf 不可比较、不可复现 |
| 内联序列 9000 元素 | E2002 | 大数组须走 `Ref`（性能护栏） |
| `assert_relative_path('../escape')` / 绝对路径 | E2002 | 路径换机器即失配 |
| naive datetime | E2002 | 静默当本地时间会偏移数小时 |
| 裸 bytes | E2002 | 二进制须落盘后引用 |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| G-1 | **"八指纹"与源码函数数量不完全对齐**：文档 §2.2 说八个指纹，但源码导出 10 个摘要函数（plan/caliber/domain/supply/environment/policy/blueprint/call/parcel/runtime_facts + compute/blob）。其中 `call`/`parcel`/`runtime_facts` 是工位/执行期摘要，`environment` 明确"不进任何产物指纹"。 | P2（术语） | `s0/01 §2.2`；`digest.py:480-497` __all__ | **需确认意图/术语澄清**：核心"八指纹"应指意图口径(5)+物化(3)那套；call/parcel/runtime_facts/environment 是补充摘要。文档与代码函数数不一致，建议文档明确列出每个函数归属，避免"到底几个"的争议。不影响正确性。 |
| G-2 | `call_digest` 含 `phase`/`capability`/`input_kinds` 三项是源码后补（:436-451），文档 §2.2 公式表未列全。 | P2（文档） | `digest.py:434-468`；对照 `s0/01 §2.2` | **已确认符合设计，文档滞后**：理由充分（fit/generate 两条路径、两能力同名实现、input kind 是 E3001 判据），源码注释也标注"公式没列但必须加"。属文档更新项（见 D5），非代码 bug。 |
| G-3 | `blob_digest` 走原始字节哈希，与 `compute_digest`（拒 bytes）是两条路径（:231-239）。调用方若混用会得到不同摘要。 | P2（接口语义） | `digest.py:231-239,226-228` | **已确认符合设计**：用途不同（内容寻址对象 vs 可规范化结构），docstring 已讲清。靠命名和类型检查区分（blob 只接受 bytes），低风险。 |

**多了什么**：无冗余。`MAX_INLINE_SEQUENCE`、`_pin_digest_view` 白名单、`_encode_set` 的 digest 排序都是必要的护栏，非过度设计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| git object 模型（内容寻址 + kind 域分隔） | 采纳 | `kind` 包装让同类内容在不同语义域指纹不同（实测 True）；`blob_digest` 对文件字节哈希，对应 git blob |
| RFC 8785 JSON Canonicalization Scheme (JCS) | 对标采纳（自实现等价） | 键 UTF-8 字节序、数字归一、NFC、无空白——与 JCS 同思路；加密场景无特殊库依赖，自实现 497 行可控 |
| Python `json.dumps(sort_keys=True)` | 拒绝直接用 | 默认按 Unicode 码位序而非 UTF-8 字节序（:150），`1`≠`1.0`，NaN 静默输出——不满足确定性 |
| 把整个 DataFrame 塞进哈希 | 拒绝（硬护栏） | `MAX_INLINE_SEQUENCE=8192` 拦截（实测 E2002），大数组走 `Ref`——加密面板动辄百万格，内联是性能事故 |
| 用 `repr(dict)` / pickle 做指纹 | 拒绝 | 键顺序、地址、版本相关，跨进程漂移；规范化字节串才是唯一稳定输入 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **唯一公式**：`compute_digest` 是唯一 `sha256(canonical_bytes(x))` 实现（:226-228），八个指纹都经它，无第二份公式（AST 层可由调用方 `DigestService.of` 复用，见 54 号文档）。
2. **十指纹全部带 `kind` 包装**（:253,266,296,314,352,375,399,415,455,477），域隔离实测成立。
3. **规范化八规则逐条落地**：bool 先于 int（:90）、float 归一 float64 子类（:104-120，修复 np.float64 repr 不一致）、`-0.0`→`0`（:114）、整数值浮点归一（:116-118）、NFC（:131）、UTC+微秒（:135-142）、UTF-8 字节序（:150）、集合按元素 digest 排序（:170-181）。
4. **时间/路径/耗时排除**：supply 投影排除 acquired_at（:305-315 + `_PIN_VIEW_KEYS` 白名单 :317-334）；parcel 只含 call/payload（:471-477）；environment 明确不进产物（:337-358）；实测全通过。
5. **深度/类型护栏**：嵌套 >64 拒绝（:186-187，防自引用）、二进制拒绝（:193-197）、未知类型拒绝（:215-218）。
6. **np.float64 往返一致性**（:104-107）：`float(value)` 归一，避免"内存算的 payload_digest"与"reload 后重算的"不一致（S5-M2 实测曾中招）。
7. **性能护栏**：`MAX_INLINE_SEQUENCE=8192`（实测 9000 元素 E2002，hint 指向 Ref）。

**发现的问题**：
- 见 D2 表 G-1（八指纹术语与函数数对齐）、G-2（call_digest 三项后补未进文档公式表，文档更新项）、G-3（blob/compute 双路径靠命名区分）。**无 P0/P1 级代码 bug**。测试覆盖充分（D1 规范化、D3 call/parcel、D4 数据版本、D5 NaN、D10 全表）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/01-data-contracts.md` §2.2 八指纹公式表 | 未列 `call_digest` 的 phase/capability/input_kinds 三项；未列 runtime_facts | 补注："call_digest 另含 phase/capability/input_kinds，理由见源码:434-451；runtime_facts_digest 为执行期快照摘要" |
| `s0/01-data-contracts.md` §2.2 | 称"八个指纹"，源码导出 10 个摘要函数 | 明确八指纹=意图口径5+物化3；call/parcel/runtime_facts/environment 为补充摘要，environment 不进产物 |
| `05-contracts.md` | 与实现一致 | 无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一个语义指纹（如 `lineage_digest`） | ⚠️ 需改代码 + 文档 | 加函数 + `kind` 包装 + `__init__.py` 导出 + 文档 §2.2；公式仍复用 compute_digest（唯一公式不破） |
| 换摘要算法（如 sha3） | ❌ 不支持零代码 | `DIGEST_ALGOS=("sha256",)` 写死在 types.py:124，Digest 构造期拒 md5（实测 E1004）；升算法=全系统指纹失效，属最高级变更 |
| 新字段不进指纹 | ✅ 支持 | 指纹函数显式列键（:245-248 注释），给对象加字段不会自动进指纹——除非改 digest_view 投影 |
| 更大数组进指纹 | ⚠️ 需改护栏 | `MAX_INLINE_SEQUENCE=8192` 是常量；调大前应评估性能，正解仍是走 Ref |
| 指纹实现整体升版 | ⚠️ 需显式升版 | `DigestService.version`（54 号文档）与 `DIGEST_IMPL_VERSION="v1"` 是升版锚点 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（无年化）
- **C7 24/7 缺口**：✅ 间接支撑——时间规范化统一 UTC+微秒（:135-142），加密 24/7 无日历时区歧义
- **C10 判据不被放松**：✅ 强支撑——D2 的"敏感"性质（:114-115，改一字节必变指纹）保证 dsr_min/fdr_max 等口径字段不能静默改写
- 其余 C2/C3/C4/C5/C6/C8/C9/C11/C12：不适用（指纹层不含业务语义）

## R1–R7 影响

- **R1 真数据跑通**：中性（指纹层稳定，真跑反复使用）
- **R2 结论可信**：✅ 强支撑——确定性 + 敏感 + 域隔离，是"同一内容同一指纹、口径不可静默改写"的密码学底座
- **R6 迭代复用**：✅ supply_digest 排除 acquired_at，重采相同数据不使历史产物失效，利好多轮复用
- **R7 fail-closed**：✅ 强支撑——NaN/Inf/大数组/坏路径/naive 时间全 E2002 拒绝，不静默产出错指纹

---

## 结论

**本模块是全系统指纹公式的唯一真相，497 行逻辑紧密、无冗余，无 P0/P1 级 bug，可确认通过。** 用户必查点落地：①八指纹带 kind 包装唯一公式——10 个摘要函数全部经唯一 `compute_digest`、`kind` 域隔离实测成立（plan≠compute）；②时间/路径/耗时不进指纹——supply 排除 acquired_at、parcel 签名只有 call/payload、environment 不进产物，实测改采集时间/路径/备注后 supply_digest 逐字节一致。两条 P2 均为文档术语/说明性（G-1 八指纹与函数数对齐、G-2 call_digest 三项后补未进公式表），建议同步 `s0/01 §2.2`，不影响生产可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 3 个模块 `errors.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
