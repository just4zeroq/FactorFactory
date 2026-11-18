# 模块审查 41 · `v5/orchestration/station.py`

> 审查日期：2026-11-18
> 依据：源码 114 行全文通读；依赖 `contracts/errors.py`（E1003/FlowError）。设计 `s0/05-orchestration.md` §1.2（StationSpec 数据表）、`03-orchestration.md`。测试 `test_v5_executor_real.py:124-126`、`test_v5_orchestration.py`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_orch1.py`（STN 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Station 是**八条工位规格的数据表**（`:1-4`）："加约束、改默认、改名字空间都只改数据，不改执行器"。职责：

1. **StationSpec 数据类**（`:13-23`）：每工位九个字段——name、revision、inputs（逻辑产物名）、output、default_impl、namespaced（open/closed_multi/single/fixed）、standalone（allowed/requires_frozen_plan/probe_only）、side_effect（pure/repeatable/append_only）、event（Budget/Publish，可选）。
2. **八工位顺序表**（`:26-43` STATION_SPECS）：Boundary→Panel→Target→Search→Evidence→Verdict→Portfolio→Certify，顺序即契约（执行器据此建 lineage 与 input_digests）。
3. **逻辑名→工位名唯一映射**（`_INPUT_TO_STATION`，`:51-60`）：inputs 用逻辑产物名（Label/Candidate…），产出工位名不同（Label←Target，Candidate←Search）；非工位引用（Pin[]/reserve_decl/Reveal/caliber.*）不在此表。
4. **导入期自检**（`_validate_specs`，`:69-86`，模块加载即跑 `:113`）：inputs 必须是工位产物名 / `all_upstream` / 白名单非工位引用 / `caliber.*` 前缀，否则 E1003；自依赖 E1003。**拼错依赖名若静默跳过，下游就会在没有上游指纹的情况下继续跑——正是"静默降级"**（`:62-64` 注释）。
5. **input_producers**（`:89-110`）：把 spec.inputs 解析为有序去重上游工位名；`all_upstream` 展开为序列中位于本工位之前的所有工位（Certify 展开为前 7 个）。返回顺序即契约。

**注意**：`input_producers` 的 `all_upstream` 用 `STATION_SPECS` 全局序列 + "遇到本工位 break"（`:103-107`），隐含"规格表顺序 = 执行顺序"的不变量——数据表与顺序表合一。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `STATION_SPECS` / `input_producers`，复现脚本 `probe_orch1.py` STN 段。

### 例 1：八工位规格表（真实输出，逐工位）

| name | inputs | output | impl | namespaced | side_effect | standalone | event |
|---|---|---|---|---|---|---|---|
| Boundary | Pin[], reserve_decl | ScopeParcel | boundary@1 | single | repeatable | requires_frozen_plan | — |
| Panel | Boundary, Pin[], Reveal | PanelParcel | panel@1 | single | repeatable | requires_frozen_plan | — |
| Target | Panel, caliber.label, caliber.execution | LabelParcel | target@1 | single | pure | requires_frozen_plan | — |
| Search | Label, Panel | CandidateParcel | search@1 | open | append_only | probe_only | Budget |
| Evidence | Candidate, Panel, Label | EvidenceParcel | evidence@1 | single | pure | allowed | — |
| Verdict | Evidence | VerdictParcel | sentinel@1 | fixed | append_only | probe_only | — |
| Portfolio | Verdict, Panel, Boundary | PortfolioParcel | portfolio@1 | closed_multi | repeatable | allowed | — |
| Certify | all_upstream | CertificateParcel | certify@1 | single | repeatable | allowed | Publish |

读法：Verdict 的 `sentinel@1`（固定实现）、Search/Verdict 的 `append_only`+`probe_only`、Portfolio 的 `closed_multi` 都如实——执行器据此拒 Search/Verdict 的 commit（实测 E3006，见 40 号）。

### 例 2：input_producers 逻辑名映射 + all_upstream 展开（真实输出）

```
Search    → ['Target', 'Panel']                 （Label→Target）
Evidence  → ['Search', 'Panel', 'Target']       （Candidate→Search）
Portfolio → ['Verdict', 'Panel', 'Boundary']
Certify   → ['Boundary','Panel','Target','Search','Evidence','Verdict','Portfolio']  （前 7 个全展开）
```
读法：非工位引用（Pin[]/reserve_decl/Reveal/caliber.*）不进上游 Parcel 依赖（由编译期 gate_b 校验）；`all_upstream` 展开顺序即 Certification 的 lineage 顺序（测试 `test_v5_executor_real.py:124-126` 断言长度 7）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| ST-1 | **规格表顺序与执行顺序合一，无独立拓扑排序**：`all_upstream` 依赖 `STATION_SPECS` 的物理顺序（`:103-107`），新增工位须插在正确位置，否则展开错误且自检不报（自检只查引用名合法/自依赖）。 | P2 | `station.py:103-107`；`:69-86` 自检 | **需确认意图**：设计明确"八工位固定顺序"，顺序即契约，非通用 DAG。但"插错位置不报错"是潜在坑——若新增工位插在 Certify 之后，all_upstream 不含它也不会报错。建议自检补"依赖的工位必须排在前面"的顺序断言（本轮不改）。 |
| ST-2 | **`side_effect` 字段未被执行器直接消费**：executor 用 `standalone=="probe_only"` 拒 commit（`:712`），但 `side_effect=append_only/repeatable/pure` 三值在执行器里没有对应分支。 | P2（数据未用） | `station.py:21` vs `executor.py:712` | **需确认**：`probe_only` 与 `append_only` 当前重合（Search/Verdict 都是两者），所以拒 commit 实际靠 standalone。side_effect 是给文档/未来扩展的元数据，当前未驱动行为。 |
| ST-3 | **`namespaced` 四值未在校验中使用**：open/closed_multi/single/fixed 声明了命名空间策略，但 station.py 与 executor 不按其派生命名/白名单，实际实现分派在 bridge._resolve_impl（按注册表实现数分单/多）。 | P2（数据未用） | `station.py:20`；`capability_bridge.py:336-375` | **已确认**：命名策略语义在文档与 bridge 实现解析中体现，规格表字段是声明性元数据。非 bug。 |

**多了什么**：无冗余。九个字段是工位完整规格，即使部分未直接驱动行为也承载设计契约。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 工位规格数据表（声明式） | 采纳 | 改约束/默认/命名空间只改数据，执行器零改；`_validate_specs` 导入期自检 |
| 逻辑产物名 ↔ 工位名唯一映射表 | 采纳 | `inputs` 用人读的 Label/Candidate，`input_producers` 翻成 Target/Search，防拼错静默跳过 |
| `all_upstream` 声明式展开 | 采纳 | Certify 不必手写 7 个依赖，顺序由表位置定 |
| 任意 DAG / 运行时拓扑排序 | **拒绝** | 八工位固定顺序是契约；非 DAG 编排，避免"第二套路径" |
| 依赖名拼错静默跳过 | **拒绝** | 白名单 + 导入期 E1003（`:69-86`），拼错即导入失败（`:62-64` 注释） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **八工位顺序表**：实测 8 条 StationSpec，顺序正确（STN-1）。
2. **逻辑名映射**：`input_producers` 实测 Label→Target、Candidate→Search、all_upstream→7 工位（STN-2）。
3. **导入期自检**：`_validate_specs()` 模块加载即跑（`:113`），未定义依赖/自依赖 E1003（`:69-86`）。
4. **白名单语义**：非工位引用白名单 `Pin[]/reserve_decl/Reveal` + `caliber.` 前缀（`:65-66`），REST 全拒——防"拼错一个依赖名被静默跳过"。
5. **去重有序**：`add()` 先判 `not in out` 保序去重（`:98-100`），lineage 顺序确定。
6. **BY_NAME 索引**：`:45` 供 executor 按名查规格（单步 `execute_station` 用）。

**发现的问题**：ST-1~ST-3 均为 P2/需确认意图或声明性元数据未用，**无 P0/P1 级代码 bug**。测试覆盖 input_producers all_upstream（7 工位）、逻辑名映射（`test_v5_executor_real.py:116-126`），导入自检靠模块加载保证。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §1.2（StationSpec） | 描述规格表字段 | 与实现一致，无需更新 |
| `03-orchestration.md` | 描述八工位与命名空间 | 可补一句"`side_effect`/`namespaced` 当前为声明性元数据，commit 拒绝实际靠 standalone=probe_only"（ST-2/ST-3 说明） |
| `s0/05 §1.2` | 若将来新增工位 | 补"新工位必须插在正确顺序位置，否则 all_upstream 展开错误且自检不报"（ST-1） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增工位 | ⚠️ 需改数据 + 顺序位置 | 加一条 StationSpec 即可（不改执行器），但须插对顺序（ST-1）；还须同步 compiler.STATIONS 与 schema |
| 改工位默认实现 | ✅ 纯数据 | 改 `default_impl`（`:27-42`），executor `_impl_for` 读取；实测 plan impl 可覆盖（`test_executor_real:86-94`） |
| 改工位命名空间策略 | ⚠️ 改数据不驱动行为 | namespaced 字段声明用，实际分派在 bridge（ST-3） |
| 新增事件（如 Reveal 工位） | ⚠️ 需改数据 + executor | event 字段目前只 Budget/Publish，executor 的 emit 逻辑按名分支（`:641-672`），新事件名需加分支 |
| 依赖名拼错 | ❌ 已 fail-closed | 导入期 E1003（ST-4 自检），不会静默跳过 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为工位规格数据表，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在规格表处理）。
- 间接支撑 **R7 fail-closed**：依赖名白名单 + 导入期自检防静默跳过（工位链路地基）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（八工位顺序与依赖表是执行器的唯一路径来源）
- **R3 因子零代码扩展**：中性（工位固定为八个；新能力经现有工位六通道接入，不新增工位）
- **R7 fail-closed**：✅ 强支撑（拼错依赖导入期 E1003、自依赖拒、probe_only 拒 commit）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 八工位规格表、逻辑名→工位名唯一映射、`all_upstream` 展开（Certify 前 7 工位实测）、导入期自检（拼错依赖 E1003、自依赖拒）全部实测成立，执行器零改即可调规格（`execute` 顺序与 trace 顺序一致，见 40 号）。三项 P2 均为需确认意图或声明性元数据未驱动行为：ST-1 插错顺序位置自检不报（建议补顺序断言）、ST-2 side_effect 未被执行器直接消费（commit 拒靠 standalone）、ST-3 namespaced 声明用（分派在 bridge）。**生产可用（能真挖因子）= 是**：工位规格可靠，不阻塞真实执行。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 42 个模块 `events.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 ST-1（是否补顺序自检断言）→ 告诉我
