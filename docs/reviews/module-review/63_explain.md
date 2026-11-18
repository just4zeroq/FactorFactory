# 模块审查 63 · `app/cli/explain.py`

> 审查日期：2026-11-18
> 依据：源码 52 行全文通读；上游 `runtime/compiler.py`（compile_plan / origins 来源表 / CALIBER_FIELDS）、`cli/common.py`（build_supply_context/parse_overrides/load_plan/envelope_ok）、`runtime/store.py`、`storage/layout.py`；设计 `09-app-storage.md` §2.2（explain 只读但走同套编译，max_exit 4）、`s0/03-runtime.md`；测试 `tests/tests/test_v5_app_commands.py`、`tests/tests/test_v5_runtime_real.py`；**实测** `%TEMP%/fm_review56_67/probe_run.py`（explain 指纹与 run 逐位一致 + origins 全表）。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`explain.py` 是**逐叶子展示编译来源与四锚点指纹**的只读命令，回答「这套方案的每个参数从哪来、指纹是什么」。它应然：

1. **与 run 同语义编译**：`--set/--no-strict/--allow-constitutive-override` 必须与 run 完全同解——同一方案 run 与 explain 编译出的指纹逐位相同。声明了参数却不生效会让「explain 告诉我从哪来」变成假话（模块 docstring `:3-5`）。
2. **同序两段编译**：先无供给编译暴露结构/跨字段错误（键拼错先说话，别报成「数据不存在」），再带供给身份编译（门禁 B）。
3. **只读但与其它编译共享失败模式**：explain 是 read_only，但走与 run 完全相同的编译，供给不可用同样给 E4xxx/exit4（`contract.py:92-95` 的 S2-M5 修正）。
4. **输出 origins + fingerprints**：origins 是逐叶子来源表（baseline@vN / plan / override），fingerprints 是五锚点（plan/blueprint/caliber/domain/policy）。

**加密视角（必查点）**：「每个参数来源可追溯」——研究员用 explain 确认 `annualisation=calendar_365`（C1）、`funding=per_contract_events`（C2）、`dsr_min/fdr_max` 没被悄悄放松（C10）。**「口径默认来源标注」**——若参数来自缺省（如 `master_seed=42`、impl defaults），origins 必须标出 `default`，否则研究员误以为所有值都写在 plan 里（见 D2 E-2）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

### 例 1：explain 成功 + 指纹与 run 逐位一致（真实，59 号探针）

```
EXPLAIN ok=True exit=0
EXPLAIN fingerprints_match_run = True          ← 与 cmd_run 的五指纹逐位相同
EXPLAIN origins_type = dict, len = 62
origins 取值分布 = {'plan': 62}                 ← 该 fixture plan 完整，所有叶子来自 plan
```

实测 `hard-coded fingerprints` 对照（explain 与 run 完全相同，节选）：
```
plan_digest      = sha256:0585b103835178283c3f413590016a4ef...
blueprint_digest = sha256:d0308238129d9f9a00515dd1d45489402...
```

这直接印证 `explain.py:3-5` 的要求——同一方案 run/explain 指纹逐位相同，没有「参数声明了却不生效」。

### 例 2：origins 逐叶子来源（真实打印）

```
caliber.label.horizon          -> "plan"
caliber.net.cost_tiers         -> "plan"
search.seed                    -> "plan"
boundary.time_axis.mine        -> "plan"
search.algorithms              -> "plan"
caliber.execution.annualisation-> "plan"
origins_sample = [["plan","plan"],["baseline","plan"]]
```

来源取值来自 `compiler.py:434-475`：baseline 合并标 `baseline@vN`，plan 标 `plan`，override 标 `override`。**每个叶子都有来源**，无遗漏（编译器有 E1007 守卫「origins 未覆盖全部叶子」，`compiler.py:328-338`）。

### 例 3：缺供给失败路径（真实 CLI）

```
$ FLOW_DATA_ROOT=<空> venv/Scripts/python.exe -m app.cli explain --plan tests/fixtures/plan_valid.json
（走 build_supply_context → read_dataset 缺文件）
→ E4002 "ohlcv 在 binance/futures 下没有任何数据文件" | exit 4
```

只读命令也因供给失败 exit4（`contract.py:92-95` 的修正落地）——不是「只读就返回 2」。

### 例 4：结构错误先于数据错误（源码 `:29-35`，语义确认）

第一遍 `compile_plan(require_supply=False)` 让「horizon 拼成 horizn」先报 E1008/E1004，而非「数据文件不存在」。本例未构造坏 plan 重跑（属语义确认）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| E-1 | **`--live` 参数在 explain 的 parser 里被声明（`__init__.py:65-66` 经 `_add_plan_args` 加到 run/explain/pipeline），但 cmd_explain 完全不读 `args.live`**。 | P2（语义） | `__init__.py:74-75`（explain 用 `_add_plan_args` 含 --live）；`explain.py:23-51` 无 `getattr(args,"live")` | **需确认意图**：explain 是只读编译，live 概念不适用；但在 help 里暴露一个无效开关会误导用户以为 explain 能切能力模式。建议 explain 不用 `_add_plan_args` 或显式拒 --live。不影响编译结果。 |
| E-2 | **口径/实现缺省值未在 origins 标注 `default` 来源**。实测完整 plan 下 origins 全 'plan'；若 plan 缺 seed，`run.py/station.py` 用 `implementation_defaults("cli")["master_seed"]=42`，但 explain 的 origins 不体现。 | P2（必查点相关） | `compiler.py:434-475` 来源仅 baseline/plan/override；实测 62 项全 'plan'；`run.py:85` 缺省 42 | **需确认意图**：这是用户强调的「口径默认来源标注」缺口。当前 explain 回答「plan 里每个值从哪来」，但不回答「plan 没写的值系统用了什么默认」。建议 origins 补 default 叶子（来自 implementation_defaults），或在信封单列 `defaults_used`。 |
| E-3 | **explain 的 data_keys 声明为 `("origins","fingerprints")`（contract.py:94），实际返回正是这两个——一致**。 | — | `explain.py:48-51`；实测例 1 | **符合**，无差距。 |
| E-4 | **第二次编译也用 `aliases`（语义同 59 号 R-1）**：`:38-47` 的 `aliases` 槽位在方案 B 下是 symbol_sets 占位。 | P2（语义） | `explain.py:38,43`；见 59 号 R-1 | **并入 59 号 R-1**：compiler 参数命名问题，非 explain 单独修。 |

**多了什么**：无。52 行全是必要编译装配。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 编译期来源表（provenance / lineage per leaf） | 采纳 | origins 逐叶子标 baseline/plan/override（compiler.py:434-475），是「可追溯」的工业做法；C1/C2/C10 的口径来源可查。 |
| explain 与 run 共用编译（不另写解释逻辑） | 采纳 | 实测指纹逐位一致（例 1），杜绝「解释的和跑的不是一回事」。 |
| 只读命令仍走完整门禁 B | 采纳 | 供给失败 exit4（例 3），不因「只读」而掩盖数据问题（S2-M5 修正）。 |
| 默认来源单列（default provenance） | **应补（E-2）** | 当前缺：plan 未写的值（seed 等）无来源标注，研究员无法区分「写在 plan」与「系统默认」。 |
| 运行期解析「最新」值 | 拒绝 | 编译期冻结 + 来源表，explain 只展示冻结结果，不现场取值。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **参数与 run 同解**：`--set/--no-strict/--allow-constitutive-override` 同样传入两次编译（`:25,27,34,46`），实测指纹一致（例 1）。
2. **同序两段编译**：结构错先暴露（`:29-35`），再查数据（`:37-47`）。
3. **供给失败 exit4**：例 3 实测，只读≠不遇供给失败。
4. **输出 origins + 五指纹**：`envelope_ok("explain", None, {...})`（`:48-51`），data_keys 与契约一致（E-3）。
5. **无口径参数**：explain 只接 `--plan/--set/--no-strict/--allow-constitutive-override/--format/--live`，无 `--horizon` 类开关——P1 守卫无触发面（`--set horizon=...` 走 compiler 二道守卫，见 56 号 C-5）。

**发现的问题**：
- 无 P0/P1 级 bug。E-1（explain 暴露无效 --live）、E-2（默认来源未标注）、E-4（aliases 语义）均为 P2，不影响「每个 plan 参数来源可追溯」的核心功能。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.2 explain 条目 | 描述「逐叶子来源与 C1–C22」 | 补 E-2：说明 origins 只覆盖 plan/baseline/override 叶子，implementation defaults（如 seed=42）不在 origins 中 |
| `s0/03-runtime.md` 来源表 | 若声称「每个参数来源可标注」 | 明确「参数」指 plan 内叶子；默认值的来源标注需另设字段（E-2 二选一裁决） |
| `__init__.py` explain parser | `_add_plan_args` 给 explain 加了 --live | 改文档或改 parser：explain 不接受 --live（E-1） |
| `18-plan-schema.md` | 与 origins 来源值一致 | 无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增口径字段的来源追溯 | ✅ 零代码（本模块） | origins 由 compiler 深合并自动派生；新 caliber 字段写进 plan 自动进 origins。但默认值来源仍需 E-2 扩展。 |
| 追溯 implementation defaults | ❌ 不支持 | 需 compiler 把 defaults 叶子写进 origins 或本模块加 `defaults_used`（E-2）。 |
| explain 输出所有叶子值（不只来源） | ⚠️ 需改代码 | 当前 origins 是 path→source 字符串映射（实测），不含值本身；若要「值+来源」需 compiler 改结构。 |
| explain 只解释某块（如只 caliber） | ❌ 不支持 | 无参数；可在信封 origins 上过滤（低价值）。 |
| 去掉 explain 的无效 --live | ⚠️ 需改 parser | `__init__.py:74-75` 让 explain 用 `_add_plan_args`；改为单独加参数即可（E-1）。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 可追溯——explain origins 标 `caliber.execution.annualisation` 来源（实测='plan'），研究员可确认是 calendar_365 而非库默认。
- **C2 资金费按事件**：✅ 可追溯——`caliber.execution.funding` 来源可查（per_contract_events）。
- **C10 判据不得被放松**：✅ 可追溯——`verdict.params.dsr_min/fdr_max` 的来源是 plan/override，能查出是否被改。
- **C8 列集开放 / C9 as-of / C11 流动性**：中性（字段/数据流在 panel/stream 层，explain 只反映 plan 声明）。
- **默认来源标注（用户必查点）**：⚠️ 缺口（E-2）——plan 未写的值无 default 标注。
- **C3/C4/C5/C6/C7/C12**：不适用（本模块不读数据/不算判据）。

## R1–R7 影响

- **R1 真数据跑通**：中性（explain 不执行八工位）。
- **R2 结论可信**：✅ 强支撑——run/explain 指纹逐位一致（例 1），口径来源可追溯，杜绝「解释与执行分家」。
- **R3 因子零代码扩展**：✅ 正向——新 caliber/算法字段自动进 origins，无需改本模块。
- **R5 单因子可归因**：✅ 正向——参数来源可查是归因链的一环。
- **R7 fail-closed**：✅ 正向——结构错先报、缺供给 exit4、origins 覆盖不全 E1007（compiler），全拒。

---

## 结论

**本模块 52 行只读编译展示逻辑无 P0/P1 级 bug，可确认通过。** 与 run 同语义编译（实测五指纹逐位一致）、同序两段编译、只读遇供给失败 exit4、origins 逐叶子来源（plan/baseline/override 全覆盖）——全部成立，直接满足「每个参数来源可追溯」。3 项 P2：E-1（explain 暴露无效 --live）、E-2（**口径/实现缺省值未在 origins 标注 default 来源，是用户必查点的缺口**）、E-4（aliases 槽位语义，并入 59 号 R-1）。均不影响「生产可用=能真挖因子」，但 E-2 建议裁决是否补 default 来源标注以完整回答必查点。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 9 个模块 `cli/fetch.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ E-2 想当场裁决（是否给 explain 补 default 来源标注）→ 告诉我
