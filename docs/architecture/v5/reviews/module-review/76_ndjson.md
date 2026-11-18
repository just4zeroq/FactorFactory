# 模块审查 76 · `v5/app/render/ndjson.py`

> 审查日期：2026-11-18
> 依据：源码 29 行全文通读；依赖 `render/__init__.py`（FORMATS/render 分派）、`common.py`（envelope_ok/envelope_error）、`json_fmt.py`（首行等价）；设计 `09-app-storage.md` §2.5（L200-217）；测试 `test_v5_app_commands.py:106-110`、`test_v5_full_review_regressions.py:119-125`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py` / `fm_review_c.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`ndjson` 是 NDJSON（换行分隔 JSON）渲染器，回答一件事（docstring `:1`）：**信封一行；data 里的列表每条单独一行（带 `_kind` 标记）**。四条理想态：
1. **首行是完整信封**：第一行永远是完整 envelope——`ok/exit_code/run_id/warnings` 是 CLI 唯一机器契约，丢掉后 `ndjson` 与 `json` 语义不等价（docstring `:10-14`）。
2. **明细行带 `_kind`**：`data` 里每个 list 元素单独成行，`{"_kind": key, **item}`，便于流式消费（每行自描述来源 key）。
3. **非 Mapping 元素兜底**：标量元素用 `{"_kind": key, "value": item}`（`:25-26`）。
4. **与 json 首行逐字节一致**：实测成立（见下）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 用真实 dict 调 `render_ndjson`，`fm_review_c.py` 测错误信封。

### 输入：`render_ndjson(envelope)`（真实 dict，同 75）

```python
env = {"ok":True,"command":"factors","run_id":None,
       "data":{"count":2,"factors":[{"factor_id":"sha256:aaa", ...},
                                    {"factor_id":"sha256:bbb", ...}]},
       "warnings":[],"exit_code":0}
```

### 输出（真实打印，4 行）

```json
{"ok": true, "command": "factors", "run_id": null, "data": {"count": 2, "factors": [...]}, "warnings": [], "exit_code": 0}
{"_kind": "factors", "factor_id": "sha256:aaa", "name": "gp:mom60", "evidence_grade": "out_of_sample", "statistics": {"dsr": 0.97, "hac_t": 3.1}}
{"_kind": "factors", "factor_id": "sha256:bbb", "name": "oi_change20", "evidence_grade": "in_sample", "statistics": {"dsr": 0.88, "hac_t": 2.0}}
```

**首行等价（实测）**：
```
render_ndjson(env).splitlines()[0] == render_json(env)  → True
render(env,'ndjson') == render_ndjson(env)              → True
deterministic 两次                                      → True
```

**错误信封（实测，`fm_review_c.py`）**：
```
--- ndjson ---
{"ok": false, "command": "promote", "run_id": null, "data": null, "warnings": [],
 "exit_code": 2, "errors": [{"code":"E1008",…,"message":"缺 --run-id"}]}
# data=null 无列表 → 只有信封行，无明细行；与 json 输出逐字一致
```

### 边界输入

| 输入 | 行为 | 说明 |
|---|---|---|
| `data` 含多个 list key | 每个 list 都展开明细行，各自 `_kind=key` | `:17-27` |
| list 元素非 Mapping（如字符串） | `{"_kind":key,"value":item}` | `:25-26` |
| `data=None`（错误信封） | 只输出信封行 | 实测 |
| `data` 顶层非 Mapping | 跳过展开，只信封行 | `:17` isinstance 守卫 |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| N-1 | **只展开 `data` 顶层的直接 list，不递归嵌套**：如 `data.groups[].items[]` 这类深层 list 不展开，仍以 JSON 子串留在信封行里。docstring 说"data 里的列表"，严格的"顶层"限定未写明。 | P2（设计取舍，需文档说明） | `ndjson.py:16-27` | 多数信封是扁平列表（factors/stations/evaluation），够用；深嵌套场景需明确"不递归"是设计。 |
| N-2 | **`_kind` 可被 item 字段覆盖**：`rec={"_kind":key}; rec.update(item)`（`:22-24`）——若某 item 自带 `_kind` 键，会覆盖机器标记，破坏"每行自描述来源"契约。 | P2（边界，需确认意图） | `ndjson.py:22-24` | 信封条目由命令层构造，正常无 `_kind` 字段；若担心可改 `rec.update` 后再强制 `rec["_kind"]=key`。 |
| N-3 | **首行等价依赖 json_fmt 同样 `default=str`/无 sort_keys**：若 json_fmt 加了 `sort_keys=True` 而 ndjson 用 `json.dumps` 默认（`:15`），首行等价会破。两处 `json.dumps` 参数需同步。 | P2（耦合，需守卫） | `ndjson.py:15`；`json_fmt.py:10` | 实测当前一致；建议加"首行==json输出"的守卫测试钉住（现测试只验 `_kind` 存在，未验首行等价——`test_full_review:123` 验了，覆盖到了）。 |

**多了什么**：无冗余。`isinstance(items, list)` 守卫跳过非列表值（如 `count:2`），正确。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| JSON Lines（每行一记录） | 采纳 | 明细行可流式解析、逐行 grep；信封行保机器契约 |
| 首行完整信封 + 明细自描述 `_kind` | 采纳 | 与 json 首行逐字节一致（实测），机器契约不丢 |
| 只输出明细、丢信封以"省一行" | 拒绝 | docstring `:11-13` 明拒——丢 ok/exit_code 则 ndjson 与 json 不等价 |
| 递归展开所有嵌套结构 | 拒绝（当前顶层限定） | 保持实现简单；深嵌套以 JSON 子串留在信封行（N-1，文档需写明） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **首行完整信封**：`lines=[json.dumps(envelope,…)]`（`:15`），实测首行==json 输出（True）。
2. **明细带 `_kind`**：`{"_kind":key}` + update item（`:22-24`），实测两行 factors 带 `_kind=factors`。
3. **非 Mapping 兜底**：`rec["value"]=item`（`:25-26`）。
4. **错误信封只信封行**：`data=None` 无展开（实测），与 json 一致。
5. **确定性**：同输入两次一致（实测）。
6. **不改 ok/exit_code**：只读信封，不构造（`:15`）。

**发现的问题**：N-1（不递归，P2 文档说明）、N-2（`_kind` 可被覆盖，P2 边界）、N-3（与 json_fmt 参数耦合，P2 守卫）。**无 P0/P1 级 bug**；测试覆盖首行等价（`test_full_review:123`）+ `_kind` 展开（`test_app_commands:106-110`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.5 | 三格式渲染 | 补"ndjson 首行=完整信封且与 json 逐字节一致"作为机器契约锚点。 |
| `09-app-storage.md` §2.5 | — | 说明"仅展开 data 顶层 list，不递归；标量元素用 value 键"（N-1/N-2）。 |
| `contract.py:102/114` 等 | 各命令支持 `--format ndjson` | reconcile 缺 `--format`（见模块 73 R-1），补后 ndjson 才在全部命令可用。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 递归展开嵌套 list | ❌ 不支持 | 需改 `render_ndjson` 加递归 + 路径式 `_kind`（如 `groups.items`）；改变输出形态，谨慎。 |
| 明细行加序号/父引用 | ❌ 不支持 | 现只有 `_kind`；加字段需改 `:22-26`。 |
| 按格式定制字段投影 | ❌ 不支持 | 渲染层不做字段选择（职责在 CLI `_row` 投影）；符合纯壳定位。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为通用 NDJSON 渲染，不参与加密事实判定。
- **R2 结论可信（间接）**：✅ 首行保真 + 明细自描述，确保流式消费不丢机器契约。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ 首行==json、明细带 `_kind`，语义等价。
- **R3/R4/R5/R6**：中性。
- **R7 fail-closed**：✅ 错误信封完整渲染（含 errors 数组），不吞错。

---

## 结论

**本模块正确、可确认通过。无 P0/P1 级 bug。** 首行完整信封、与 json 逐字节一致、明细 `_kind` 标记、错误信封只信封行四条全部实测成立。N-1（不递归）、N-2（`_kind` 可覆盖）、N-3（与 json_fmt 参数耦合）均为 P2 边界/文档项，正常命令信封不触发；建议补文档说明 + 保留首行等价守卫测试。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 77 个模块 `table.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ N-1 是否递归展开想当场裁决 → 告诉我
