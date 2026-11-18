# 模块审查 42 · `orchestration/events.py`

> 审查日期：2026-11-18
> 依据：源码 99 行全文通读；依赖 `contracts/errors.py`（E3006/FlowError）、`storage/ledger.py`（Ledger.append/get，见 30 号文档）。设计 `s0/05-orchestration.md` §5（三类一次性事件）、`09-app-storage.md`（mutation 等级）；测试 `test_v5_orchestration.py`、`test_v5_s5_single_step.py`、`test_v5_full_review_regressions.py`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_orch1.py`（EV 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

EventLedger 是**三类一次性事件的唯一门卫**（`:1-8`）：Reveal / Budget / Publish 绑定一次执行，已存在→返回旧值不重复触发——这是"计数单调"与"揭示一次"的结构保证。

1. **三类事件**（`:43-50`）：
   - `reveal(ticket, window)`：key=`ticket:window`，value=window_id；在 Boundary 之前；
   - `budget(ticket, family, count)`：key=`ticket:family`，value=实测去重候选数；在 Search 落盘后；
   - `publish(ticket, digest)`：key=`ticket:publish`，value=结论 digest；在 Certify 通过后。
2. **两种后端**（`:10-15,33-41`）：
   - 不传 ledger：纯内存 `self._records`（单测/`--probe`），语义与旧实现完全一致；
   - 传 ledger：每条事件 append 到 `db/events.jsonl`（Ledger 的 `events` register），这是 AC8"跨进程不重复"的唯一落地。
3. **跨进程一次性**（`:52-74`）：内存命中→返回旧值；另一进程已写（Ledger.append 抛 E3006）→ `get` 读回那条旧记录，**返回旧值不改写**——后到执行不得覆盖先到的计数/揭示/发布。只有 E3006 才读回，其他 FlowError 照抛（`:64-66`）。
4. **单步不自行揭示**（`require_reveal`，`:90-99`）：单步缺 Reveal 记录→E3006，不自行揭示；先跑整体执行。
5. **get 跨进程可见**（`:76-88`）：本进程没触发但磁盘上有（run 后另起进程跑单步）→ 从 Ledger 读回并回填内存视图。

**事件账本落盘但单步 events 仍内存语义**（用户必查点/已知遗留）：executor 默认 `EventLedger()` 无 ledger（40 号 `executor.py:260`），run 事件只活内存；本模块**具备**落盘能力（传 Ledger 即写 `db/events.jsonl`），但 executor 当前未默认注入——单步 events 仍是内存语义，跨进程一次性需上层显式传 Ledger。这是 plan §U4 D2 已列的 S5-M2 遗留，非本模块缺陷。

**依赖方向**（`:35-38` 注释）：编排层可依赖存储层，只 import Ledger 的接口形状（append/get），不 import 存储层实现细节；能力层禁止依赖存储层。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测，复现脚本 `probe_orch1.py` EV 段。

### 例 1：内存 EventLedger 三类事件 + 重复返回旧值（真实输出）

```
reveal1        = 'W'
reveal_repeat  = 'W'         （同 key 返回旧值，不重新触发）
budget         = 7
publish        = 'sha256:x'
get_known      = 'W'
get_missing    = None
```

### 例 2：单步缺 Reveal → E3006（真实输出）

```
require_reveal("T1","W") 未先 reveal →
  E3006|单步缺 Reveal 记录: T1:W
```
读法：单步不得自行揭示，先跑整体执行（hint 明确）。

### 例 3：落盘 `db/events.jsonl` 跨进程一次性（真实输出，用户必查点）

```
r1                        = 'W'
r2_from_other_instance    = 'W'      （第二实例模拟另一进程，同 key 再触发）
same                      = True
register_path             = db\events.jsonl
events_jsonl_lines        = 1        （磁盘只有一行，没写第二份）
e2_get_reveal             = 'W'      （第二实例 get 读回）
```
读法：两个 EventLedger 实例（模拟跨进程）对同一 `Reveal:T1:W` 触发，第二个经 Ledger.append 的 E3006 路径读回旧记录（`:64-72`），磁盘 `db/events.jsonl` 只一行——跨进程一次性实测成立，正是"后到执行不得覆盖先到"的坐实。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| EV-1 | **executor 默认不注入 Ledger，run 事件只内存**：`EventLedger()` 无 ledger（40 号 executor.py:260），单步 events 内存语义；落盘能力需上层显式传。plan §U4 D2 已列此遗留。 | P2（已知遗留） | `executor.py:260`；`events.py:33-41` | **已确认符合当前设计**：S5-M2 范围外，本模块两种后端都已实现且实测；跨进程一次性在显式 Ledger 时成立（EV-3）。属"能力具备、默认未启用"，非本模块 bug。 |
| EV-2 | **get 的跨进程回填只在 get 时发生**：`_records` 不主动重扫磁盘，若另一进程 write 而本进程从不 get 该 key，内存视图落后。 | P2（低） | `events.py:76-88` | **已确认符合设计**：与 Ledger 的"锁内重扫回并"不同（30 号），EventLedger 只在 get/_once 触发时读回；一次性语义由 Ledger 跨进程保证，内存视图只是缓存。无正确性风险。 |
| EV-3 | **`_once` 内存命中先于落盘读回**：同进程重复触发直接返回 `_records[k]`（`:54-55`），不查 Ledger；若内存有旧值但磁盘被外部改写（违反 append-only），不会察觉。 | P2（低） | `events.py:53-55` | **已确认**：append-only 信任磁盘不可变；篡改检测归 objects/ledger 层（30 号）。一次性事件不重复触发是首要目标。 |
| EV-4 | **事件 value 类型混用**（int/str）：budget 是 int、reveal/publish 是 str，`EventRecord.value: int \| str` 联合类型（`:30`）。 | P2（低） | `events.py:30,43-50` | **已确认**：value 只记录不参与判定，类型差异无碍；跨进程读回用 `prior.get("value", value)`（`:72`）保留原类型。 |

**多了什么**：无冗余。两种后端 + require_reveal 是三类事件的完整语义。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 事件溯源（一次性事件只记一次） | 采纳 | 计数单调、揭示一次；AC4–AC8 的编排层保证 |
| 内存 + 落盘双后端（可选 Ledger） | 采纳 | 单测/probe 用内存零依赖；生产跨进程用 `db/events.jsonl` |
| E3006 时读回旧记录而非重写 | 采纳 | 后到执行不得覆盖先到的计数/揭示/发布（`:67-72`） |
| 单步自行揭示 | **拒绝** | 缺记录 E3006，先跑整体执行（`:90-99`） |
| 每进程各写一份事件 | **拒绝** | 跨进程会重复计数/重复揭示；靠 Ledger 去重（实测一行） |
| 编排层 import 存储层实现细节 | **拒绝** | 只用 append/get 接口形状（`:35-38`），保持层间边界 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **三类事件 key 唯一**：reveal=`ticket:window`、budget=`ticket:family`、publish=`ticket:publish`（`:43-50`）；实测三类 value 正确。
2. **内存重复返回旧值**：同 key 再触发返回旧记录（`:54-55`，实测 reveal_repeat='W'）。
3. **落盘跨进程一次性**：两实例同 key，`db/events.jsonl` 只一行，第二实例读回旧值（实测 EV-3，`:57-73`）。
4. **只 E3006 读回**：其他 FlowError 照抛（`:64-66`），不掩盖真实故障。
5. **require_reveal fail-closed**：缺记录 E3006（实测），单步不自行揭示。
6. **get 跨进程可见**：本进程未触发但从 Ledger 读回并回填（`:76-88`，实测 e2_get_reveal='W'）。
7. **EventRecord frozen dataclass**：不可变（`:26-30`），事件记录不被就地改写。
8. **依赖倒置正确**：只依赖 Ledger 接口（append/get），不 import 存储实现（`:35-38`）。

**发现的问题**：EV-1~EV-4 均为 P2/已知遗留或确认设计，**无 P0/P1 级代码 bug**。测试覆盖三类事件、require_reveal、跨进程去重（`test_v5_orchestration.py`、`test_v5_s5_single_step.py:338-388` 的 M2 单步用 reveal 前置）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §5（三类事件） | 描述 Reveal/Budget/Publish 与一次性 | 与实现一致，无需更新 |
| `09-app-storage.md`（mutation 等级） | 描述事件 append-only | 一致；可补"落盘 `db/events.jsonl` 由 EventLedger 可选 Ledger 后端实现"（EV-1 说明） |
| `s0/05 §6.1`（单步缺 Reveal） | 写单步 E3006 | 一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增第四类事件 | ⚠️ 需改代码 | 加一个方法 + `_once(kind,...)` 调用（:43-50 模式）；kind 字符串自由，无需改数据结构 |
| 事件账本落盘（跨进程一次性） | ✅ 已支持 | 传 `EventLedger(Ledger(...))` 即写 `db/events.jsonl`；实测一行去重（EV-3） |
| executor 默认落盘事件 | ❌ 当前未启用 | executor 传无 ledger 的 EventLedger（EV-1）；需上层注入，属 S5-M2 遗留范围 |
| 事件 value 复合化（如带时间戳） | ⚠️ 需改代码 | `value: int\|str`（:30），改复合类型需同步 Ledger 记录结构与 get 回填（:72） |
| 事件消费/查询面 | ⚠️ 借 Ledger | `get` 只读单 key；批量查询/审计归 IndexDB/Ledger.records（30/29 号） |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为一次性事件账本，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在事件层处理）。
- 间接支撑 **R7 fail-closed**：揭示一次、计数单调、单步不自行揭示（实测 E3006）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（Budget/Publish 真值来自执行事实，事件门卫可靠）
- **R2 结论可信**：✅ 强支撑（揭示一次防 holdout 偷看、发布只认一次、计数单调防重放刷账）
- **R5 单因子可归因**：✅ 正向（事件序号可信，归因链的事件锚点可靠）
- **R7 fail-closed**：✅ 强支撑（单步缺 Reveal E3006、跨进程重复读回不重写、落盘一行实测）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 三类一次性事件、内存/落盘双后端、**落盘 `db/events.jsonl` 跨进程一次性**（两实例同 key 只一行、第二实例读回旧值）、单步缺 Reveal E3006、get 跨进程可见——全部真实调用坐实。"单步 events 仍内存语义"是 plan 已列的已知遗留（EV-1）：本模块具备落盘能力，只是 executor 默认未注入 Ledger，非本模块缺陷。四项 P2 均为确认设计或低优先：EV-2 get 才回填（内存是缓存）、EV-3 内存命中先于磁盘读（append-only 信任）、EV-4 value 联合类型（只记录不判定）。**生产可用（能真挖因子）= 是**：一次性事件语义可靠，不阻塞真实执行；若需跨进程事件去重，上层传 Ledger 即可。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 43 个模块 `state.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 EV-1（是否让 executor 默认注入 Ledger）→ 告诉我
