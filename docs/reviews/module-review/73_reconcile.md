# 模块审查 73 · `app/cli/reconcile.py`

> 审查日期：2026-11-18
> 依据：源码 221 行全文通读；依赖 `digest.py`（blob_digest/compute_digest）、`layout.py`（db/runs/facts）、`capability.params.schema_for`、`orchestration.STATION_CAPABILITY`、`contract.py`（READ_ONLY / max_exit=3 / data_keys）；设计 `15-operations.md` §4-8/§5、`09-app-storage.md` §4（L294-305）；测试 `test_v5_s6_ops.py:190-225`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py` + `--help` 实测。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`reconcile` 是登记库 / 产物链 / 对象存储的**三方一致性核对**，回答一件事（docstring `:1-19`）：
1. **只读**：发现损坏不是失败，是**发现**；核对重心是"登记库是否还能完整回答发生过什么"——索引可重建、对象可重算，唯一不可重建的是 append-only jsonl 登记库（`:3-5`）。
2. **三类检查**：①登记（`db/*.jsonl` 每行可解析、digest 格式合法、一条不少）；②产物链（每 ticket 的 parcel.json → payload 存在且 `payload_digest` 逐字节对拍；lineage 上游存在）；③对象（`*_ref` 声明的派生对象在对象存储有同名文件 + schema sidecar，blob_digest 对拍）。
3. **不误报**：只有**派生数据通道**（opaque/array 且无原生同名 ref）的 `<field>_ref` 才当落盘对象核对；`panel_ref`/`score_ref` 这类原生 content-digest ref 设计上没文件，当对象核对是误报（`_derived_ref_fields:37-56`）。
4. **发现即 exit 3**：事实层不自洽是"契约违反"而非"执行失败"——E2005 / exit 3；violations 全量随 context 走，终端只摘要；继续跑只会放大不一致（`:198-211`）。

**加密特化**：本模块不碰加密语义——它守的是事实层自洽（账本/产物/对象），是 R7"口径与判据不可静默改写"的底座。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 构造手工 ticket 并写一份合法的 `parcel.json`（`payload_digest=compute_digest(payload)`），调用 `cmd_reconcile`；随后篡改 payload 触发核对。

### CLI help（真实退出码）——**暴露一个缺口**

```
$ venv/Scripts/python.exe -m app.cli reconcile --help
usage: flow reconcile [-h]
exit=0
$ venv/Scripts/python.exe -m app.cli reconcile --format json
usage: flow [-h] {run,... reconcile,...} ...
flow: error: unrecognized arguments: --format json
exit=2
```
> 所有其他命令都注册了 `--format`，唯独 reconcile 没有（`cli/__init__.py:150`）——见 D2 R-1、D5。

### 输入：`cmd_reconcile(args)`（无参数）

### 输出：`data`（干净，真实打印）

```json
{"data_root":"C:\\…\\fm_review_A_xxx","tickets":2,"parcels":1,
 "registers":{"trials":0,"events":1,"published":0,"members":0},
 "violations":[],"clean":true,"summary":{"violations":0,"kinds":[]}}
```

### 失败输入 → 错误输出（fail-closed，实测）

| 场景 | 错误码 | exit | 真实输出 |
|---|---|---|---|
| 篡改 Target/Search payload（改字段） | **E2005** | **3** | `reconcile 发现 1 处不一致: ['payload_digest_mismatch']`；context.total=1、tickets=2 |
| payload 文件缺失 | E2005 | 3 | violation `kind=payload_missing`（`_check_tickets:117-122`） |
| 登记行不可解析 | E2005 | 3 | violation `kind=register_unparseable`（`_check_registers:82-85`） |
| 对象 digest 不符 / sidecar 缺 / lineage 断 | E2005 | 3 | 对应 kind 进 violations（`:152-187`） |

> 实测篡改后 violations kinds=`['payload_digest_mismatch']`，与 `test_v5_s6_ops.py:207-225` 一致（那里篡改 Target payload 得 E2005 exit 3）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| R-1 | **`reconcile` 子解析器未注册 `--format`**：`cli/__init__.py:150` 只有 `sub.add_parser("reconcile", help=…)`，没有 `add_argument("--format")`。实测 `reconcile --format json` → argparse exit 2 `unrecognized arguments`。违背"三格式一致"与 09 §2.5（每条命令可指定渲染格式）。 | **P1（CLI 一致性 bug，可复现）** | `cli/__init__.py:150` vs `:114,118,132` 等；实测 exit 2 | 最小修复：`reconcile` 子解析器加 `--format choices=FORMATS default=json`；不改核对逻辑。 |
| R-2 | **坏 `parcel.json` 抛 E3004 而非 E2005**：`_check_tickets:114` `json.loads(parcel_json.read_text())` 无 try/except；单个不可解析的 parcel.json 会让未预期 `JSONDecodeError` 冒到 main 兜底 → E3004/exit 1，而不是"发现损坏"的 E2005/exit 3。与登记行坏时按 E2005 处理（`:79-85`）不一致。 | P1（失败路径错误码错层） | `reconcile.py:114` vs `:79-85`；对照 D1"发现损坏=发现" | 应把 parcel.json 解析失败也登记为 `kind=parcel_unparseable` 的 violation，走 E2005；当前实测篡改 payload 能正确报 E2005，但坏 parcel.json 本身漏网。 |
| R-3 | **lineage 解包假设**：`for name, digest in (record.get("lineage") or [])`（`:173`）——若 lineage 元素是 dict 或长度≠2，解包抛未预期异常 → E3004；契约应明确 lineage 是 `[[name,digest],…]`。 | P2（数据格式边界） | `reconcile.py:173` | 低优先；正常产物是 list of pairs，手工篡改才触发。 |
| R-4 | **对象 digest 抽全量 blob_digest**（`:151`）：大对象存储下逐字节读全量做哈希，性能陷阱；docstring 自述"抽全量"（`:14`）是刻意选择（宁慢不漏）。 | P2（性能，刻意） | `reconcile.py:14,151` | 不列问题，记录为已知取舍。 |
| R-5 | **E2005 码义借用**：E2005 定义"不可变字段被改写"（`errors.py:104`），用于"三方不一致"是文档 §5 的约定（docstring `:16-18`）。 | P2（语义，需确认意图） | `errors.py:104`；`15-operations.md` §5 | 文档已如此约定，保留；不改。 |
| R-6 | **data_keys 对账**：实测返回键 `tickets/parcels/registers/violations/clean/summary/data_root`，`contract.py:114` 声明前四个——声明 ⊆ 实际，无缺口（clean/summary 为附加）。 | ✅ 无差距 | `contract.py:114`；实测 | 无需改。 |

**多了什么**：无冗余。`_derived_ref_fields` 用注册表分类避免误报（`:37-56`）是必要设计——测试专门守 `panel_ref/score_ref` 不算 object_missing（`test_v5_s6_ops.py:202-204`）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| content-addressed storage + 逐字节 digest 对拍 | 采纳 | `blob_digest`/`compute_digest` 对拍（`:128-132,151`），与 executor `_load_committed` 同源规则 |
| event sourcing：登记库 append-only 不可重建 | 采纳 | 核对重心是登记库完整性（`:3-5`），坏行按 violation 不崩（`:79-85`） |
| 派生 ref vs 原生 ref 分类核对 | 采纳 | `_derived_ref_fields` 从 `schema_for` 的 outputs 推导（`:37-56`），避免把 array_digest 当漏文件 |
| 发现损坏就静默跳过/降级成功 | 拒绝 | 一律 E2005 exit3 fail-closed（`:198-211`） |
| 用第二套口径"修复"不一致 | 拒绝 | 修复路径按 §5：索引/产物重算、对象重建、登记库从备份恢复（hint `:210`）；本命令只读不改 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **三类检查齐全**：登记（`:68-96`）、产物链（`:99-169`）、对象（`:138-169`）+ lineage（`:170-187`），实测 clean 与篡改两路径。
2. **不误报原生 ref**：`_derived_ref_fields` 只选 `v in {opaque,array}` 且无原生同名 ref 的字段（`:50-55`）；测试守 panel_ref/score_ref 不算 object_missing。
3. **坏登记行不崩**：`json.loads` 包 try/except ValueError → `register_unparseable` violation（`:79-85`）。
4. **payload digest 逐字节对拍**：`compute_digest(json.loads(content)) != payload_digest` → `payload_digest_mismatch`（`:128-133`），实测篡改触发 E2005 exit3。
5. **发现即 exit 3**：violations 非空 → FlowError(E2005, context=violations[:100]+total+tickets+parcels+registers)（`:198-211`），实测 exit=3。
6. **只读**：无写调用（全文无 `write_text`/`unlink`），mutation=READ_ONLY。

**发现的问题**：R-1（`--format` 缺失，P1 可复现）、R-2（坏 parcel.json 漏网成 E3004，P1 失败路径）、R-3（lineage 解包，P2）、R-4/R-5（刻意/语义，记录）。无 P0 级 bug；核心核对逻辑正确，E2005 exit3 实测成立。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2（命令参数） | 每条命令可指定 `--format` | 补 reconcile 的 `--format`（R-1 修复后同步文档）。 |
| `15-operations.md` §5 | 三类核对 + E2005/exit3 | 明确"坏 parcel.json 也应按 E2005 发现、不是 E3004"（R-2），统一失败语义。 |
| `09-app-storage.md:238` | "删 duckdb 后 reconcile 恢复全部查询能力" | 实测核对只读不重建索引——文档措辞"恢复能力"指核对发现后人工重建，需澄清 reconcile 本身不写。 |
| `contract.py:114` | reconcile data_keys 四项 | 可补 `clean/summary`（可选，声明⊆实际已满足）。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增登记文件（如 `alerts.jsonl`） | ⚠️ 半配置化 | `_REGISTER_NAMES`（`:33`）加值即可；不需改核对逻辑。 |
| 新增对象类 fact_kind | ⚠️ 半配置化 | 对象核对按 `parcel_data` 路径写死（`:149`）；新 kind 需加路径分支。 |
| 新增能力/工位（影响 `_derived_ref_fields`） | ⚠️ 自动跟随 | 从 `schema_for` outputs 推导（`:37-56`），注册新能力后自动纳入分类；无需改 reconcile。 |
| 自动修复不一致 | ❌ 不支持 | 本命令只读；修复走 §5 人工路径（索引重建/对象重建/账本恢复），刻意为之。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块守事实层自洽，不直接参与加密事实判定。
- **R7 fail-closed（核心相关）**：✅ 强支撑——payload/对象/lineage/登记任一不自洽即 E2005 exit3，是"口径与判据不可静默改写"的底座（产物被篡改会被对拍发现）。

## R1–R7 影响

- **R1 真数据跑通**：中性（核对不产因子）。
- **R2 结论可信**：✅ 强支撑（证据链完整性可对拍）。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：中性。
- **R6 迭代复用**：✅ 登记库完整性是跨轮计数/门槛的前提。
- **R7 fail-closed**：✅ 强支撑（E2005 exit3 实测）。

---

## 结论

**本模块核对逻辑正确、E2005 exit 3 实测成立，可确认通过（附两个 P1 修复项）。** 三类检查 + 不误报原生 ref + 坏登记行不崩都符合 D1。**两个 P1**：R-1 缺 `--format`（CLI 一致性，可复现 exit 2）、R-2 坏 `parcel.json` 漏网成 E3004 而非 E2005（失败路径错误码错层）——两者都是小修，不触碰核对核心。建议最小修复后回归 `test_v5_s6_ops.py:190-225`。

**请确认本模块审查结果**：
- ✅ 确认通过（R-1/R-2 列入 P1 修复清单）→ 我进入第 74 个模块 `replay.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ R-2 失败路径错误码想当场裁决（E2005 vs E3004）→ 告诉我
