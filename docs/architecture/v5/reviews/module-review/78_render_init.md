# 模块审查 78 · `v5/app/render/__init__.py`

> 审查日期：2026-11-18
> 依据：源码 31 行全文通读；依赖 `json_fmt.py`/`ndjson.py`/`table.py`（三格式实现，见模块 75-77）、`cli/__init__.py`（argparse `choices=FORMATS`）；设计 `09-app-storage.md` §2.5（L200-217）；测试 `test_v5_app_commands.py:61-66,80-87,124-127`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`render/__init__.py` 是渲染层的**唯一入口与门面**，回答两件事（docstring `:1-7`）：
1. **纯导出 + 分派**：对外只露 `render()` + `FORMATS`（`__all__`，`:30`）；`render(envelope, fmt)` 按 fmt 分派到三格式实现，延迟 import（`:20-27`）避免导入期循环依赖。
2. **三条硬约束的守门人**：①不做单位换算/美化；②警告与错误分开；③`ok` 与 `exit_code` 恒一致由信封构造保证，渲染层不得改。未知格式 → `ValueError`，由 `main` 转 E1004（`:17`）。

**纯导出并入说明**：本文件不含独立业务语义，是模块 75/76/77 的组装门面——按 plan §U11 "`__init__.py` 纯导出文件并入所属模块说明"的约定，其审查结论并入三格式渲染，本单独立档仅为完整性与分派/AOI 校验。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 用真实 dict 调 `render(env, fmt)` 三格式。

### 输入 / 输出：`render(envelope, fmt)`（真实打印）

```
FORMATS = ('table', 'json', 'ndjson')
render(env,'json')   == render_json(env)   → True
render(env,'ndjson') == render_ndjson(env) → True
render(env,'table')  == render_table(env)  → True
deterministic 三格式两次 → [True, True, True]
render(env,'yaml')   → ValueError 未知渲染格式: yaml
```

**格式选择的两道门（实测）**：
- argparse 层：每条命令 `--format choices=FORMATS`（`cli/__init__.py:67` 等），非法格式在解析期被挡（exit 2）；唯 reconcile 缺 `--format`（见模块 73 R-1）。
- 门面层：`render` 的 `if fmt not in FORMATS: raise ValueError`（`:18-19`）——直接调用（不经 argparse）时兜底。

### 失败输入 → 错误输出

| 输入 | 行为 | 说明 |
|---|---|---|
| `render(env,"yaml")` | ValueError（`:19`） | 实测；注释称"由 main 转 E1004"，见 D2 I-2 |
| 未知格式经 CLI `--format yaml` | argparse exit 2 `invalid choice` | 第一道门，不进 render |
| `fmt=None` | `None not in FORMATS` → ValueError | 不猜默认（fmt 默认 "json"，`:16`） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| I-1 | **reconcile 未接 `--format`**（非本文件问题，但属 FORMATS 一致性边界）：`cli/__init__.py:150` 漏加 `--format`，导致 `FORMATS` 声明的三格式在 reconcile 上不可用（实测 exit 2）。 | P1（归入模块 73 R-1） | `cli/__init__.py:150`；本文件 `FORMATS` | 不在此文件改；模块 73 已列 P1 修复。 |
| I-2 | **注释"未知格式 → ValueError（由 main 转 E1004）"与实际不符**：`main` 的兜底 `except Exception` 把 ValueError 转成 E3004/exit 1（`cli/__init__.py:212-216`），不是 E1004/exit 2。实际非法格式被 argparse choices 先挡（exit 2），render 的 ValueError 只在直接调用时触发。 | P2（注释失准） | `render/__init__.py:17`；`cli/__init__.py:67,212-216`；实测 | 改注释为"由 main 兜底转 E3004；CLI 路径由 argparse 先挡"，或统一错误码。 |
| I-3 | **`__all__` 只含 `render`/`FORMATS`**：三格式函数 `render_json/ndjson/table` 不在公共导出（但可直接 `from v5.app.render.json_fmt import ...`）。 | ✅ 设计如此 | `:30`；模块 75-77 直接 import 子模块测试 | 纯门面正确，不列问题。 |
| I-4 | **data_keys/文档对账**：门面无 data_keys；09 §2.5 三格式实测一致。 | ✅ 无差距 | 实测 | 无需改。 |

**多了什么**：无冗余。延迟 import（`:20-27`）是正确的依赖管理——避免导入 `render` 时强制加载三格式实现。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 门面（Facade）+ 延迟导入分派 | 采纳 | 单一入口 `render()`，`__all__` 只露两个符号，延迟 import 避免环 |
| 集中格式注册表（`FORMATS`）驱动 argparse choices | 采纳 | `cli/__init__.py:67` 用 `choices=FORMATS`，一处定义多处用（reconcile 漏接除外） |
| 渲染层按 fmt 做不同口径/换算 | 拒绝 | 三实现都只读信封、不改值（模块 75-77 实测） |
| 导入期静态加载所有格式 | 拒绝 | 延迟 import 更轻、避免循环依赖 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **纯导出**：`__all__=["render","FORMATS"]`（`:30`），无业务逻辑。
2. **分派正确**：三格式 `render(env,fmt)==对应函数`（实测 True），确定性（实测 [True,True,True]）。
3. **未知格式 fail-closed**：`render(env,"yaml")` → ValueError（实测），不静默返回默认格式。
4. **延迟 import**：`if fmt=="json": from .json_fmt import…`（`:20-27`），无导入期副作用。
5. **默认 fmt=json**（`:16`）：与 argparse 默认一致（`cli/__init__.py:67`），无分叉。
6. **三条硬约束声明**（docstring `:3-6`）：与三实现行为一致（模块 75-77 实测）。

**发现的问题**：I-1（reconcile 漏接，P1 归模块 73）、I-2（注释错误码失准，P2）。无 P0 级 bug；门面本身干净。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `render/__init__.py:17` | 注释"由 main 转 E1004" | 改为"CLI 路径由 argparse choices 先挡（exit 2）；直接调用经 main 兜底转 E3004"（I-2）。 |
| `09-app-storage.md` §2.5 | 三格式渲染 | 补"`FORMATS` 是唯一格式清单，每条命令 `--format` 应 choices=FORMATS"——reconcile 漏接（I-1）由此暴露。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增格式（csv/yaml） | ❌ 需改四处 | `FORMATS` + `render` 分派 + 各命令 argparse choices + contract；非纯配置（与模块 75-77 D6 一致）。 |
| 渲染层新增横切逻辑（如脱敏） | ❌ 不建议在此加 | 会违反"渲染不改值"；脱敏应在信封构造前做（如 log 的 `tolist` 守卫，test_full_review:128）。 |
| 格式别名（如 `ndjson`→`jsonl`） | ❌ 不支持 | 无 ALIASES 机制；加值需在 `render` 和 argparse 两处映射。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为渲染门面，不参与加密事实判定。
- **R2 结论可信（间接）**：✅ 门面不改值、fail-closed 未知格式。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ 三格式分派一致、确定性、不改信封。
- **R3/R4/R5/R6**：中性。
- **R7 fail-closed**：✅ 未知格式拒，不静默降级到默认渲染。

---

## 结论

**本模块是干净的纯导出门面，可确认通过。无 P0/P1 级 bug（在门户自身）。** FORMATS 单一清单 + 延迟 import 分派 + 三格式一致性 + 未知格式 fail-closed 全部实测成立；按 plan 约定并入模块 75-77 的三格式说明。**I-1（reconcile 漏接 `--format`）是跨文件一致性问题，已归入模块 73 的 P1 修复清单**；I-2 为本文件注释错误码失准（P2），改注释即可。

**请确认本模块审查结果**：
- ✅ 确认通过 → 全部 11 个模块审查完成，我汇总结论与 P0/P1 清单
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ I-2 注释错误码想当场裁决（改注释 vs 统一 E1004）→ 告诉我
