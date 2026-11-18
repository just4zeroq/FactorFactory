# 模块审查 75 · `app/render/json_fmt.py`

> 审查日期：2026-11-18
> 依据：源码 11 行全文通读；依赖 `render/__init__.py`（FORMATS/render 分派）、`common.py`（envelope_ok/envelope_error）；设计 `09-app-storage.md` §2.5（L200-217）、ACCEPTANCE；测试 `test_v5_app_commands.py:89-126`、`test_v5_full_review_regressions.py:119-125`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py` / `fm_review_c.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`json_fmt` 是 JSON 渲染器，回答一件事（docstring `:1`）：**把信封逐字输出，不重排、不换算**（09 §2.5）。理想态四条：
1. **完整保真**：信封 `{ok,command,run_id,data,warnings,exit_code,errors?}` 全字段进 JSON；`data` 里的数值、嵌套结构原样保留——渲染层不做单位换算、不改字段名。
2. **机器契约等价**：`--format json` 与 `--format ndjson` 的首行必须逐字节一致（实测成立，见下），因为 ok/exit_code/run_id/warnings 是唯一机器契约。
3. **确定性**：同输入同输出（可复现、可对拍）；不做随机排序。
4. **非 JSON 值兜底**：`default=str` 让 numpy/datetime 等可序列化为字符串而非直接崩（`:10`）——这是容错，但属"可能掩盖类型"的边界（D2 J-2）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 用真实 dict 调 `render_json`，`fm_review_c.py` 用 `envelope_error` 造错误信封。

### 输入：`render_json(envelope)`（真实 dict）

```python
env = {"ok": True, "command": "factors", "run_id": None,
       "data": {"count": 2, "factors": [
           {"factor_id":"sha256:aaa","name":"gp:mom60","evidence_grade":"out_of_sample",
            "statistics":{"dsr":0.97,"hac_t":3.1}}, …]},
       "warnings": [], "exit_code": 0}
```

### 输出（真实打印）

```json
{"ok": true, "command": "factors", "run_id": null, "data": {"count": 2, "factors": [{"factor_id": "sha256:aaa", "name": "gp:mom60", "evidence_grade": "out_of_sample", "statistics": {"dsr": 0.97, "hac_t": 3.1}}, {"factor_id": "sha256:bbb", "name": "oi_change20", "evidence_grade": "in_sample", "statistics": {"dsr": 0.88, "hac_t": 2.0}}]}, "warnings": [], "exit_code": 0}
```

**分派与一致性（实测）**：
```
render(env,'json') == render_json(env)          → True
deterministic render(env,'json') 两次           → True
ndjson first line == json output                → True   # 机器契约等价
错误信封 json   → {"ok":false,…, "errors":[{"code":"E1008",…,"message":"缺 --run-id"}], "exit_code":2}
errors[0] 含 exit_code ?                        → False  # 错误对象不带 exit_code（契约）
```

### 失败/边界输入

| 输入 | 行为 | 说明 |
|---|---|---|
| 含 numpy/datetime 的 dict | `default=str` 转字符串输出 | 容错，不崩；但值被改成字符串（D2 J-2） |
| 非 Mapping（如 list） | `json.dumps` 仍能输出（list 合法） | 但 `render()` 签名要 Mapping；上层保证 |

> 注：未知格式 `render(env,"yaml")` → `ValueError`（由 `__init__.py` 抛，main 兜底），实测 `ValueError 未知渲染格式: yaml`——json_fmt 本身不涉及格式选择。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| J-1 | **无 `sort_keys`**：输出顺序 = 输入 dict 插入顺序。当前信封由 `envelope_ok/error` 固定构造（键序稳定），同语义信封字节一致（实测确定性 True）；但若上游用不同路径构造 dict，同语义可能不同字节，影响逐字节对拍（AC63）。 | P2（需确认意图） | `json_fmt.py:10`；`common.py:18-21,27-31` | 信封构造集中，风险低；若要绝对确定性可加 `sort_keys=True`，但会改变既有黄金锚字节，需评估。 |
| J-2 | **`default=str` 静默转类型**：numpy 数组/datetime/自定义对象被转字符串而非报错，可能掩盖"渲染层不该拿到非 JSON 值"的问题（09 §2.5"不做单位换算/美化"的精神）。 | P2（边界，容错 vs 诚实） | `json_fmt.py:10` | 信封构造已保证类型（envelope_ok/error 只放 dict/list/标量），正常路径不触发；保留容错。 |
| J-3 | **data_keys/文档对账**：json 是渲染格式无 data_keys；09 §2.5 三格式契约实测符合。 | ✅ 无差距 | 实测 | 无需改。 |

**多了什么**：无冗余。单行 `json.dumps` 是最简实现，符合"纯壳"定位。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| JSON Lines / 结构化输出 | 采纳 | 一行一信封，可被 `json.loads` 直接解析（测试 `test_json_render_roundtrip`） |
| `ensure_ascii=False` 保留中文 | 采纳 | 错误 message 含中文（如"未知 --min-grade"），原样输出便于排查 |
| `sort_keys=True` 绝对确定性 | ⚠️ 未采用 | 当前靠信封构造顺序保证；加它会改黄金锚字节，需权衡（J-1） |
| 渲染层做单位换算/字段改名/数值"美化" | 拒绝 | 09 §2.5 硬约束；本模块逐字输出 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **逐字保真**：`json.dumps(envelope, ensure_ascii=False, default=str)`（`:10`），实测字段/数值/嵌套全保留。
2. **机器契约等价**：ndjson 首行 == json 输出（实测 True）。
3. **确定性**：同输入两次输出一致（实测 True）；信封由 `envelope_ok/error` 固定构造（键序稳定）。
4. **错误对象不带 exit_code**：错误信封 `errors[0]` 无 exit_code（实测 False），进程码只从顶层 exit_code 读（`common.py:26-30`）。
5. **不改 ok/exit_code**：渲染层只读信封、不构造（`:10`），符合 `__init__.py:6` 约束。

**发现的问题**：J-1（无 sort_keys，P2 需确认）、J-2（default=str 容错边界，P2）。**无 P0/P1 级 bug**；测试 `test_json_render_roundtrip` + `test_ndjson_always_emits_envelope_line` 覆盖核心契约。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.5 | 渲染层不换算、警告/错误分开、ok/exit_code 由信封保证 | 实测符合，无需改。 |
| `09-app-storage.md` §2.5 | 三格式字段一致性 | 补一句"json 与 ndjson 首行逐字节一致"（实测成立），作为机器契约锚点。 |
| AC63 黄金锚 | 逐字节/±10% 两层容差 | 若决定加 `sort_keys=True`（J-1），需重算所有黄金锚字节——不轻易动。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增输出格式（csv/yaml） | ❌ 需改代码 | 改 `FORMATS` + `render` 分派（`__init__.py`）+ 每条命令 argparse choices——四处同步，非纯配置。 |
| 可配置缩进/排序 | ❌ 不支持 | `render_json` 无参数（`:9`）；若要 pretty-print 需改签名，但会破坏"一行一信封"机器契约。 |
| 中文/特殊字符 | ✅ 已支持 | `ensure_ascii=False`（`:10`），实测中文 message 原样输出。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为通用 JSON 渲染，不参与任何加密事实判定（纯格式层）。
- **R2 结论可信（间接）**：✅ 逐字保真 + 确定性，确保"看到的=算出来的"。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ 强支撑（渲染不改值、不美化）。
- **R3/R4/R5/R6**：中性（渲染层不决定因子/搜索/归因/复用）。
- **R7 fail-closed**：✅ 错误信封也完整渲染（errors 数组进 JSON），不静默吞错。

---

## 结论

**本模块是最简且正确的 JSON 渲染器，可确认通过。无 P0/P1 级 bug。** 逐字保真、机器契约等价（ndjson 首行==json）、确定性、错误对象不带 exit_code 四条全部实测成立。J-1（无 sort_keys）与 J-2（default=str）均为 P2 边界项，当前信封构造集中使风险很低；不建议为 J-1 轻易改字节序列化（会动 AC63 黄金锚）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 76 个模块 `ndjson.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ J-1 是否加 `sort_keys=True` 想当场裁决 → 告诉我
