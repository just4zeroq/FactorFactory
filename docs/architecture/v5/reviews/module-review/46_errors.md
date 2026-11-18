# 模块审查 46 · `v5/contracts/errors.py`

> 审查日期：2026-11-18
> 依据：源码 273 行全文通读；依赖 `exit_codes.py`（ExitCode）；设计 `s0/01-data-contracts.md` §4（错误分层与码表）、`05-contracts.md`；测试 `tests/v5/test_v5_contracts.py` D10（37 例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `C:/Users/lansa/AppData/Local/Temp/fm_probe_contracts.py` 的 E1 段（码表唯一性/连续性脚本实测）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`errors.py` 是全系统**错误码与错误分层的唯一真相**。分层不按模块，而按"谁该修"划分——调用方只看退出码就知道该找谁。它该做的四件事：

1. **五层 taxonomy**：

| 类 | 前缀 | 退出码 | 含义 | 数量 |
|---|---|---|---|---|
| 用法与配置 | E1xxx | 2 | 用户改配置即可 | 22 |
| 契约违反 | E2xxx | 3 | 系统不变量被破坏，按 bug 处理 | 9 |
| 执行失败 | E3xxx | 1 | 输入合法但这步没成功 | 6 |
| 供给失败 | E4xxx | 4 | 事实层不可用/不适用 | 7 |
| 存储与 IO | E5xxx | 1 | 落盘、权限、并发 | 5 |

2. **码表唯一 + 连续**：49 个码，零重复、零缺号；未知码（如拼错的 `E1023`）在 `layer_for` 处 fail-closed 抛 `ValueError`。
3. **错误对象带 `retryable`、不带独立 `exit_code` 字段**：`layer`/`exit_code` 都是由 `code` 推导的只读 property（源码:21-22），不接受独立赋值——"存两份必然漂移，且漂移后无检查点能发现被归错类"。
4. **三条硬规则**（源码:15-19）：不得捕获后继续（可降级错误数为零）、不得换码（底层 E4002 上层不得包装成 E1001）、不得只记日志不返回。

**序列化契约**：`to_dict()` 只输出 `code/layer/message/retryable` 四个必有字段，可选 `path`/`hint`/`context` 缺失时**不补 null**（null 与缺失不等价，实测 `test_error_serialisation_omits_absent_optionals`）。

**加密意义**：错误分层服务于 R7 fail-closed——供给失败（E4xxx，如现货目录读 funding→E1004/E4002）、契约违反（E2xxx，如设施被绕过 E2007）都有明确归属，不静默降级。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑 `fm_probe_contracts.py` 的 E1 段。

### 例 1 · 49 错误码唯一性 + 连续性（用户必查点，脚本实测）

```
count = 49
unique = 49
dups = []
  E1xxx: 1001..1022 n=22 gaps=[]
  E2xxx: 2001..2009 n=9  gaps=[]
  E3xxx: 3001..3006 n=6  gaps=[]
  E4xxx: 4001..4007 n=7  gaps=[]
  E5xxx: 5001..5005 n=5  gaps=[]
ALL continuity gaps = {'E1': [], 'E2': [], 'E3': [], 'E4': [], 'E5': []}
per-prefix counts = {'E1': 22, 'E2': 9, 'E3': 6, 'E4': 7, 'E5': 5}
```

**判断**：49 个码 = 22+9+6+7+5，五段各自 1 起连续到最大，**零重复、零缺号**。

### 例 2 · 错误码 → 退出码映射（实测）

```
exit map sample:
  E1001 -> (usage, 2)
  E2002 -> (contract, 3)
  E3001 -> (execution, 1)
  E4002 -> (supply, 4)
  E5001 -> (storage, 1)     # 存储层归执行退出码 1
```

注意 E5xxx（storage）映射到 `ExitCode.EXECUTION=1` 而非独立码——存储失败按"执行没成功"处理，符合 `exit_codes.py` 只有 0/1/2/3/4 五个码的设计（见 47 号文档）。

### 例 3 · 错误对象结构 + 失败路径（实测）

```
Error fields (to_dict): ['code','layer','message','retryable']
Error has exit_code attr (property): True   retryable in dict: False
E1023 unknown rejected: 未定义的错误码 'E1023'；新增错误码必须同时加进
                        v5/contracts/errors.py 的 ALL_ERROR_CODES 与 s0/01 §4.2
```

**逐条解读**：
- `Error.to_dict()` 输出**只有四个键**（code/layer/message/retryable），不含 `exit_code` 字段——exit_code 是推导 property（:210-212），不进序列化。这正是"带 retryable 不带 exit_code"的落点：错误对象不存退出码，只存 retryable；退出码由 code 经 layer 推导。
- `retryable` 默认 False（:196），构造后可带；`FlowError(..., retryable=False)` 透传（:242-250）。
- 未知码 `E1023` 在 `Error.__post_init__` 经 `layer_for` 当场炸（:199），不留到序列化后。

### 失败路径 → 错误码（实测/测试钉住）

| 非法输入 | 结果 |
|---|---|
| `layer_for('E1023')` | `ValueError`（未知码 fail-closed） |
| `Error(code='X0001', message='x')` | `ValueError` |
| `compute_digest({'x': nan})` | `FlowError(E2002)`，exit_code=3 |
| `Ref.from_dict` 缺 `id` | `FlowError(E2003)` |
| `FlowError('E3001', ...)` | code=E3001，exit_code=EXECUTION(1) |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| ER-1 | **码表 49 个全部有定义与用途，无"占位码"、无缺号**（脚本实测零 gaps）。 | —（正面） | E1 段实测 + `ALL_ERROR_CODES`(:144-152) | **已确认干净**。用户必查点"49 错误码零重复缺号"完全成立。 |
| ER-2 | `Error` 有 `exit_code` **property**（:210-212）但 `to_dict()` 不输出（:214-228）。"不带 exit_code"指不存独立字段、不进序列化；推导属性仍可被 `FlowError.exit_code`（:261-263）读取。 | P2（术语澄清） | `errors.py:181-228,257-263`；实测 to_dict 无 exit_code 键 | **已确认符合设计**。这是"推导而非存储"的刻意选择（docstring:21-22）。文档若写"错误对象不带 exit_code"须注明"指不存独立字段/不进序列化，推导属性仍有"以免误解。 |
| ER-3 | E1xxx 的注释语义较密（如 E1009 同时表示"构成性参数改动未声明族"与"数据版本变化后重跑搜索"，E1018 合并三种域错误）。 | P2（可诊断性） | `errors.py:81,90` | **需确认意图**：一码多义时 message/hint 必须说清是哪种情形。当前靠调用方写消息，码本身不细分。低优先，不影响分层。 |
| ER-4 | `layer_for` 用 `ValueError` 而非 `FlowError(E2xxx)` 报未知码（:163-167）。 | P2（一致性） | `errors.py:160-168` | **需确认意图/已接受**：未知码是"码表本身被改坏"，用裸 `ValueError` 避免再依赖被污染的码表；测试也钉住 `ValueError`。合理。 |

**多了什么**：无冗余。`ErrorLayer`/`_LAYER_BY_PREFIX`/`_EXIT_BY_LAYER` 三个映射把"前缀→层→退出码"拆开，避免 `layer` 字段与 `code` 漂移——是刻意设计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Typed error taxonomy + 按修复责任分层 | 采纳 | E1(用户)/E2(bug)/E3(执行)/E4(供给)/E5(IO) 五层，调用方只看退出码即知归属；49 码零缺号 |
| 错误码 → 退出码单点映射 | 采纳 | `_LAYER_BY_PREFIX` + `_EXIT_BY_LAYER`（:49-67），layer/exit 由 code 推导不另存 |
| fail-closed 未知码 | 采纳 | `layer_for` 对非码表值抛 `ValueError`（:160-168），拼错码不会静默通过 |
| 开放式前缀匹配（E1 开头就放行） | 拒绝 | docstring:153-157 明确拒绝——拼错的 E1023 会悄悄通过，对应的手工 raise 永远无人读到 |
| 错误对象里冗余存 layer/exit_code 字段 | 拒绝 | 存两份必漂移且无检查点；改为 property 推导（docstring:21-22） |
| 捕获后降级/换码 | 拒绝（硬规则） | 可降级错误数为零（:17）；换码会破坏"分类是排查第一信息"（:18） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **49 码唯一 + 连续**：脚本实测 `count=unique=49`、`dups=[]`、五段 gaps 全空（用户必查点，实测）。
2. **五层 → 退出码映射正确**：E1→2、E2→3、E3→1、E4→4、E5→1（实测）；测试 `test_every_error_code_has_layer_and_exit` 逐码钉住。
3. **错误对象带 retryable 不带 exit_code 字段**：`to_dict` 四键、exit_code 为 property、retryable 在序列化内（实测）。
4. **未知码 fail-closed**：`E1023`/`X0001` 构造期 ValueError（实测）。
5. **可选字段不补 null**：`path/hint/context` 缺失不输出（:222-228，测试钉住）。
6. **context 不可变**：`__post_init__` 用 `MappingProxyType(dict(...))` 包住（:202-204），跨层后不得就地改。
7. **layer 由前缀唯一决定**：`layer_for` 用 `_LAYER_BY_PREFIX[code[:2]]`（:168），无独立 layer 字段可漂移。
8. **退出码语义分离**：契约违反 E2xxx→3，执行失败 E3xxx→1，供给 E4xxx→4——`3` 与 `1` 严格区分（见 47 号文档）。

**发现的问题**：
- 见 D2 表 ER-2（exit_code 是 property 不进序列化，术语需澄清）、ER-3（一码多义靠 message 区分）、ER-4（未知码用 ValueError 而非 FlowError，已接受）。**无 P0/P1 级 bug**。测试覆盖充分（D10 全表 + 各层错误码在 30+ 模块测试中被触发验证）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/01-data-contracts.md` §4.2 码表 | 与 `errors.py` 49 码一致 | 无需更新；新增码须同步此表 + ALL_ERROR_CODES（源码:166 提示已写明） |
| `05-contracts.md` | 描述五层 taxonomy | 补一句"exit_code 为推导 property，不进 Error 序列化四字段；retryable 进序列化"——澄清 ER-2 |
| `s0/01-data-contracts.md` §4 | "错误对象带 retryable 不带 exit_code" | 注明"指不存独立 exit_code 字段/不进 to_dict"以免与 `FlowError.exit_code` 属性混淆 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增错误码（如 E1023） | ⚠️ 需多处同步 | 加常量 + `ALL_ERROR_CODES`(:144) + 文档 §4.2；`layer_for` 对未登记的码拒（实测 E1023 抛 ValueError）——这是防"拼错码静默通过"的护栏 |
| 新增错误层（如 E6xxx 安全） | ❌ 需改代码 | 加 `ErrorLayer` 枚举值 + `_LAYER_BY_PREFIX` + `_EXIT_BY_LAYER` + `ExitCode`，非纯配置 |
| 改某前缀的退出码 | ⚠️ 需改映射 + 文档 | `_EXIT_BY_LAYER`（:59-67）一处改；但改 E2→1 会破坏"3=契约违反≠1=执行失败"红线，需全仓复核 |
| 新字段进错误对象 | ⚠️ 需序列化同步 | 加 dataclass 字段 + `to_dict` + 测试；可选字段遵循"缺失不补 null" |

---

## C1–C12 相关项判定

本模块为错误分类层，不含业务计算：

- **C3 spot/futures 维度**：✅ 支撑——现货目录读 funding 等错误由 E1004/E4002 表达（plan_schema.py:356-368 实测触发 E1004）
- **C7 24/7 缺口**：✅ 支撑——E4003"时间末端未闭合"、E4007"时间索引重复/非单调"是加密缺口检测的错误码落点
- **C10 判据不被放松**：✅ 支撑——E1019（组合拟合窗口早于可用时间）、E1009（数据版本变化重跑搜索）等防止静默放宽判据
- 其余条目：不适用（分类层不直接计算）

## R1–R7 影响

- **R1 真数据跑通**：中性（错误码是旁路，但真跑中 E4xxx 供给失败会正确中断）
- **R2 结论可信**：✅ 强支撑——E2004（Evidence 出结论）、E2006（第二套裁判入口）等契约违反码守住可信红线
- **R7 fail-closed**：✅ 强支撑——未知码拒绝、可降级错误为零、不得换码/不得只记日志，49 码全覆盖

---

## 结论

**本模块是全系统错误分类的唯一真相，273 行逻辑干净，无 P0/P1 级 bug，可确认通过。** 用户必查点逐条实测成立：①49 错误码零重复缺号——脚本实测 count=unique=49、dups=[]、五段各 1 起连续、gaps 全空；②错误对象带 retryable 不带 exit_code——`to_dict()` 实测仅 `code/layer/message/retryable` 四键，exit_code 是推导 property 不进序列化，retryable 在序列化内。ER-2/ER-3/ER-4 三条 P2 均为术语澄清或"需确认意图"，不影响生产可用。新增码必须同步 `ALL_ERROR_CODES`+文档 §4.2（源码已提示）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 4 个模块 `exit_codes.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
