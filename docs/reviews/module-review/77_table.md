# 模块审查 77 · `app/render/table.py`

> 审查日期：2026-11-18
> 依据：源码 45 行全文通读；依赖 `render/__init__.py`（FORMATS/render 分派）、`common.py`（envelope_ok/envelope_error）；设计 `09-app-storage.md` §2.5（"渲染层不得做单位换算"L1/§2.5）；测试 `test_v5_app_commands.py:112-115`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py` / `fm_review_c.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`table` 是给人看的渲染器，回答一件事（docstring `:1-4`）：**不改变任何值**，只把信封排成可读文本。四条理想态：
1. **头部三件套**：`command/status/exit_code`，run_id 非空时插在 command 之后（`:19-24`）；`status = OK if ok else ERROR`（`:19`），与信封 ok 一致。
2. **两种 data 形态**：扁平映射逐行 `key: value`；`list[dict]` 排成表格（表头取首行键，逐行对齐）；复杂嵌套（dict/list 值）回退 JSON 子串（`:12-15,27-37`）。
3. **错误/警告分开**：errors 进 `error: CODE: message` 行，warnings 进 `warn: …` 行（`:38-43`）——呼应 `__init__.py:5-6` 的硬约束。
4. **不换算、不改值**：`_flat` 对 dict/list 用 `json.dumps`、对 None 用空串、其余 `str(value)`（`:12-15`）——数值原样显示。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 用真实 dict 调 `render_table`，`fm_review_c.py` 用错误信封。

### 输入：`render_table(envelope)`（真实 dict，同 75）

### 输出（真实打印）

```
command: factors
status: OK
exit_code: 0
count: 2

[factors] (2 rows)
  factor_id | name | evidence_grade | statistics
  sha256:aaa | gp:mom60 | out_of_sample | {"dsr": 0.97, "hac_t": 3.1}
  sha256:bbb | oi_change20 | in_sample | {"dsr": 0.88, "hac_t": 2.0}
```

要点：
- 扁平键 `count: 2` 直接一行；`factors` 是 `list[dict]` → 表格，`statistics` 是嵌套 dict → **回退 JSON 子串**（`{"dsr": 0.97, "hac_t": 3.1}`），值未改。
- 表头取首行 4 个键（`:32`），两行按序对齐。
- `render(env,'table') == render_table(env)` → True；确定性两次 → True（实测）。

**错误信封（真实打印，`fm_review_c.py`）**：
```
command: promote
status: ERROR
exit_code: 2
error: E1008: 缺 --run-id
```
> `status: ERROR` 由 `ok=False` 推出（`:19`）；`errors[0]` 渲染为 `error: CODE: message`；顶层 exit_code=2 显示。

### 边界输入

| 输入 | 行为 | 说明 |
|---|---|---|
| `run_id` 非空 | 插在 `command` 行之后 | `:23-24` `head.insert(1, …)` |
| `list[dict]` 为空 `[]` | 输出 `[key] (0 rows)` + 无表头/行 | `:30-35` `if value:` 守卫 |
| list 元素非全 dict | 当作扁平值 → `_flat` JSON 子串 | `:29` `all(isinstance(x,Mapping))` |
| 嵌套 dict/list 值 | `_flat` → `json.dumps` 子串 | `:13-14`，不改值 |
| `None` 值 | 空串 | `:15` |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| T-1 | **列取首行键**：`cols=list(value[0])`（`:32`），后续行若有首行没有的键 → 该列被静默丢弃（数据不可见）；首行缺的列在后续行出现也不可见。 | P2（需确认意图） | `table.py:32-35` | 信封条目由命令层 `_row` 固定构造（同构），正常不触发；若担心可改为"取所有行的键并集"。 |
| T-2 | **数值 `str(value)` 原样显示**：浮点如 `-0.021193092621664054` 全精度显示，可读性差——但"不改值"要求下这是正确取舍（09 §2.5）。 | P2（可读性，刻意） | `table.py:15` | 不列问题；格式化（如四舍五入）会违反"渲染不改值"。 |
| T-3 | **无列宽对齐**：`join(" | ")` 不 pad 列宽，长值错位（视觉）。 | P2（美观） | `table.py:33-35` | 给人看的渲染，不影响机器契约；可用 tabulate 但会引入依赖（plan D3 拒绝 Web，未禁 tabulate，但当前无必要）。 |
| T-4 | **data_keys/文档对账**：表格是渲染格式无 data_keys；实测 status/exit_code/errors 行为符合 09 §2.5。 | ✅ 无差距 | 实测 | 无需改。 |

**多了什么**：无冗余。`_flat` 的 None→空串、dict/list→JSON 子串是必要的兜底，避免 `str(dict)` 的 Python 风格污染。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 人类可读表格（列头 + 行） | 采纳 | 研究员看 `flow factors --format table` 直接核对因子清单 |
| 嵌套值回退 JSON 子串 | 采纳 | 简单、不改值、无依赖；复杂嵌套本就不该塞进表格 |
| tabulate / rich 等表格库 | ⚠️ 未采用 | 会引入依赖且可能改值（对齐/截断）；当前手写 45 行够用，替换成本对比见 plan U2（自研日志先例） |
| 渲染层做单位换算/四舍五入"美化" | 拒绝 | 09 §2.5 硬约束；实测全精度原样显示 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **头部一致**：`status=OK if ok else ERROR`（`:19`），实测错误信封 `status: ERROR`；run_id 插入位置正确（`:23-24`）。
2. **两种形态**：扁平 `count: 2`、表格 `[factors] (2 rows)` + 表头 + 行（实测）。
3. **嵌套不改值**：`statistics` dict → JSON 子串原样（实测 `{"dsr": 0.97, "hac_t": 3.1}`）。
4. **错误/警告分开**：`error: E1008: 缺 --run-id`（实测）；warnings 走 `warn:`（`:41-43`）。
5. **不改 ok/exit_code**：只读信封、不构造（`:19-22`），数值原样（`:15`）。

**发现的问题**：T-1（首行列，P2 同构场景不触发）、T-2（全精度，刻意）、T-3（无对齐，美观）。**无 P0/P1 级 bug**；测试 `test_table_render_is_readable`（`:112-115`）守 status/count 行为。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.5 | "渲染层不得做单位换算" | 实测符合（全精度原样、嵌套 JSON 子串），无需改。 |
| `09-app-storage.md` §2.5 | 警告/错误分开、ok/exit_code 恒一致 | 补一句表格中 `status=OK/ERROR` 由 ok 推出、`error:`/`warn:` 分行，作为可读契约说明。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 列宽对齐 / 排序 | ❌ 不支持 | 需改 `:32-35` 或引入 tabulate；属美观，不改值前提下可做但非必要。 |
| 深层嵌套展开成子表 | ❌ 不支持 | 现回退 JSON 子串（`:13-14`）；改会破坏"简单渲染"定位。 |
| 新增输出格式 | ❌ 不支持 | 见模块 75/78：需 FORMATS + render 分派 + argparse choices 四处同步。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为通用表格渲染，不参与加密事实判定。
- **R2 结论可信（间接）**：✅ 不改值、错误分开，研究员看到的表格与信封一致。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ 表格渲染不改数值、不吞错误。
- **R3/R4/R5/R6**：中性。
- **R7 fail-closed**：✅ 错误信封也渲染 `status: ERROR` + `error:` 行，不静默。

---

## 结论

**本模块是正确、简洁的表格渲染器，可确认通过。无 P0/P1 级 bug。** 头部三件套、两种 data 形态、嵌套回退 JSON 子串、错误/警告分开、不改值五条全部实测成立。T-1（首行列）、T-2（全精度）、T-3（无对齐）均为 P2 边界/美观项，正常同构信封不触发；不建议为美观引入会改值的格式化。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 78 个模块 `render/__init__.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ T-1 是否取"所有行键并集"想当场裁决 → 告诉我
