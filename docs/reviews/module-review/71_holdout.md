# 模块审查 71 · `app/cli/holdout.py`

> 审查日期：2026-11-18
> 依据：源码 89 行全文通读；依赖 `contract.py`（WRITES_LEDGER / must --yes）、`compiler.py`（compile_plan → plan_digest）、`events.py`（EventLedger.reveal）、`boundary.py`（reserved 语义，见模块 01）、`errors.py`；设计 `09-app-storage.md` §2.4/L90/L193、`s0/05-orchestration.md`；测试 `test_v5_catalog_ops.py:348-385`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`holdout` 是"保留区只看一次"的**披露记录器**，回答四件事：
1. **只记录事实，不改数据**：保留区的全部意义是"只看一次"；本命令职责是记录这次揭示写 `events.jsonl`，不动 parquet（docstring `:3-8`）。
2. **区间来自计划，不来自命令行**：`holdout` 是研究设计的一部分，能临时指定就失去意义（`:6,53-54`）；从 `boundary.time_axis.holdout` 读，格式 `YYYY-MM-DD..YYYY-MM-DD`（`:31-47`）。
3. **一次性语义可查**：写一次性键 `Reveal:holdout:<plan_digest>:<span>`；第二次执行不报错但 `first_time=false`——给"我是不是已经看过了"一个可查答案（`:74-86`）。
4. **与 boundary 的 reserved 语义对齐**：`holdout=reserved` 时拒绝——保留区还没划出来就谈不上揭示（`:55-60`）。boundary 侧 `reserved` 表示"按 mine∪oos 补集标记，不看保留区的值"（见模块 01）；两侧语义闭合。

**mutation**：`WRITES_LEDGER`（`contract.py:110`），必须 `--yes`；`max_exit=4`（窗口未就绪）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 用 `test_v5_catalog_ops._plan` 生成两份计划（reserved / 显式区间），调用 `cmd_holdout`。

### CLI help（真实退出码）

```
$ venv/Scripts/python.exe -m app.cli holdout --help
usage: flow holdout [-h] --plan PLAN [--set SET] [--no-strict]
                    [--allow-constitutive-override] [--yes]
                    [--format {table,json,ndjson}]
exit=0
```

### 输入：`cmd_holdout(args)`

```
args.plan = <path>/plan.json       # 内含 boundary.time_axis.holdout
args.set  = []                     # --set 覆盖（见 D2 H-2）
args.no_strict = True
args.yes  = True                   # 缺则 require_confirmation 抛 E1004
```
`compile_plan(...require_supply=False, strict=not no_strict)` 仅为拿到 `plan_digest`（`:63-68`）。

### 输出：`data.holdout`（真实打印）

**首次揭示**：
```json
{"plan":"p",
 "plan_digest":"sha256:0fcd9a13634ab23fad149820e5ac01026c1c73e026439671eec8c49c36e2b880",
 "span":"2024-09-01..2024-09-30","start":"2024-09-01","end":"2024-09-30",
 "first_time":true,"revealed_value":"2024-09-01..2024-09-30",
 "ledger":"C:\\…\\db\\events.jsonl"}
```
**二次揭示（真实）**：同参数再跑，`first_time=false`、`revealed_value` 不变；`events.jsonl 行数 = 1`（账本只写了一行——幂等由 `EventLedger` 一次性键保证）。

### 失败输入 → 错误输出（fail-closed，全部实测）

| 非法输入 | 错误码 | exit | 真实 message |
|---|---|---|---|
| 缺 `--yes` | E1004 | 2 | `holdout 会写全局账本，必须显式 --yes`（require_confirmation） |
| 计划 `holdout=reserved` | E1004 | 2 | `计划的 holdout 是 reserved，没有可揭示的区间`（`:56-60`） |
| `holdout` 格式非 `a..b` | E1008 | 2 | `holdout 必须是 'YYYY-MM-DD..YYYY-MM-DD'…`（`:32-36`） |
| 日期非法 / 起点晚于终点 | E1008 | 2 | `holdout 日期不合法…` / `holdout 起点晚于终点…`（`:38-46`） |

> 与 boundary 的对齐：boundary 遇 `holdout=01-03..01-04` 且与 mine 相交 → E4002（`01_boundary.md` 实测）；holdout 遇 `reserved` → E1004。二者从不同方向守"保留区不可乱用"。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| H-1 | **span 与 plan_digest 的取值时点不一致**：`raw=axis.get("holdout")` 在 `compile_plan` **之前**读（`:54`），`digest` 在编译**之后**取（`:68`）。若用 `--set boundary.time_axis.holdout=…` 覆盖区间，揭示的 `span` 是原计划值、`plan_digest` 反映覆盖后计划 → 一次性键 `Reveal:<digest>:<span>` 可能对应错位的两份计划。 | **P2（边界，需确认意图）** | `holdout.py:52-70` | 实测默认路径（无 --set）二者一致。需裁决：`--set` 是否允许改 holdout？若允许，应在编译后从 `compiled` 读回区间。 |
| H-2 | **文档 09 §2.4 的 holdout 子命令为 `plan`/`reveal`，最大退出码写 4（窗口未就绪）**：实现是单命令无子命令；`reserved` 报 E1004/exit 2，不是 4。 | P2（文档，见 D5） | `09-app-storage.md:90,193`；本模块 `:55-60` 实测 | 文档-实现差距；exit 4 由编译期供给门禁给（explain/run 路径），holdout 不走供给门禁，文档应说明。 |
| H-3 | **data_keys 声明 `("holdout",)`** 与实际一致（实测 `data.holdout`），无缺口——此条为对照 promote/factors 的"元数据对账"，holdout 通过。 | ✅ 无差距 | `contract.py:110`；实测 | 无需改。 |
| H-4 | **`_span` 只做字符串字典序比较 `left > right`**（`:44`）：格式 `YYYY-MM-DD` 下字典序=时间序，正确；但依赖严格零填充，格式检查（`date.fromisoformat`）已保证。 | P2（实现细节） | `holdout.py:37-46` | 已闭合，不列问题。 |

**多了什么**：无冗余。`revealed_value` 回读账本值（`:85`）让"揭示了什么"可核对，是好设计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 一次性事件账本（event sourcing / exactly-once） | 采纳 | `EventLedger.reveal` 用 `Reveal:<scope>:<span>` 一次性键，二次揭示 `first_time=false`（`:74-86`，实测账本 1 行） |
| 保留区区间随计划冻结（plan-digest scoped） | 采纳 | scope 含 plan_digest 前 16 位（`:68-69`），不同计划=不同揭示实验 |
| CLI 临时指定保留区（`--holdout 2024-09-01..…`） | 拒绝 | 保留区是研究设计，可临时指定=可 p-hacking；本模块只从 plan 读（`:53-54`） |
| 揭示时改数据/打标 | 拒绝 | 只记录事实，不动 parquet（docstring `:3-8`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **区间只从计划读**：`axis=plan.boundary.time_axis`，无命令行入口（`:52-54`）。
2. **reserved 拒绝**：E1004（`:55-60`，实测）。
3. **一次性语义**：`existed=events.get(...) is not None` 先查，`events.reveal(...)` 写；二次 `first_time=false`（`:74-86`，实测账本 1 行）。
4. **计划身份绑定**：`plan_digest` 进 scope（`:68-69`），实测 `sha256:0fcd…`。
5. **确认守卫**：`require_confirmation` 缺 `--yes` → E1004 exit2（实测）。
6. **格式校验完整**：`..` 分隔、ISO 日期、起≤终（`:31-47`）。

**发现的问题**：H-1（span/digest 时点错位，P2 需确认意图）、H-2（文档差距）。**无 P0/P1 级代码 bug**；默认路径主流程 + 四条失败路径实测符合 D1，且与 boundary 的 reserved 语义两侧对齐。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md:90` | holdout 有 `plan`/`reveal` 子命令 | 改为单命令揭示；说明区间来自 plan 不可临时指定。 |
| `09-app-storage.md:193` | data_keys=`windows[]`（id/state/revealed_at） | 改为实测 `holdout{plan,plan_digest,span,start,end,first_time,revealed_value,ledger}`。 |
| `09-app-storage.md:90`（max_exit=4） | "窗口未就绪"给 4 | 说明 holdout 的 `reserved` 是 E1004/exit2；exit4 留给编译期供给门禁路径。 |
| `s0/05-orchestration.md` / `01_boundary.md` | boundary reserved=补集标记 | 补一句"holdout 命令在 reserved 时拒绝揭示"，与 boundary 对齐说明闭环。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 分段/多次揭示（如先揭示 OOS 再揭示 holdout） | ⚠️ 半配置化 | key=`<scope>:<span>`，不同 span 是不同揭示；但 scope 绑定单一 plan_digest，需新增揭示类型标识才能区分"哪一类"。 |
| 揭示后自动跑认证 | ❌ 不支持 | 本命令只记录，不触发执行；Certify 由 `replay`/`pipeline` 单独跑（职责分离）。 |
| 保留区"检查"（看是否已揭示而不写） | ❌ 不支持 | 无 `--check` 模式；可用 `inspect` 查账本或直接读 `events.jsonl`。 |
| `--set` 覆盖 holdout 区间 | ⚠️ 行为歧义 | 见 H-1：当前 span 取原计划、digest 取覆盖后；需先裁决意图再决定是否修。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为披露记录，不直接参与加密事实判定。
- **C10 判据不被放松（相关）**：✅ 保留区只看一次 + 区间锁定在 plan，是防"看了结果再调判据"的关键治理措施。
- **R2 结论可信（核心相关）**：✅ 保留区披露治理是样本外可信度的一环。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ 强支撑（一次性揭示记录 + 区间锁计划，防重复使用保留区）。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：中性。
- **R6 迭代复用**：中性（揭示记录可跨轮查）。
- **R7 fail-closed**：✅ 强支撑（reserved 拒、格式非法拒、缺 --yes 拒）。

---

## 结论

**本模块功能正确，可确认通过。无 P0/P1 级 bug。** 与 boundary 的 reserved 语义两侧对齐（boundary 侧补集标记、holdout 侧拒绝揭示），一次性揭示实测账本 1 行、`first_time` 翻转正确。唯一需裁决的是 **H-1**：`--set` 覆盖 holdout 时 span（编译前取）与 plan_digest（编译后取）可能错位——默认路径无此问题，属边界意图问题；H-2 为文档差距，随 U11 更新。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 72 个模块 `live.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ H-1 `--set` 覆盖 holdout 的意图想当场裁决 → 告诉我
