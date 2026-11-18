# 模块审查 62 · `app/cli/inspect.py`

> 审查日期：2026-11-18
> 依据：源码 74 行全文通读；上游 `storage/catalog.py`（read_ticket L223-263 / station_of L215-220）、`orchestration/executor.py`（load_committed_parcel L978-1025）、`storage/layout.py`、`contracts/errors.py`（E1008）；设计 `09-app-storage.md` §2（查产物）、`s0/05-orchestration.md`；测试 `tests/tests/test_v5_s5_single_step.py`；**实测探针** `%TEMP%/fm_review56_67/probe_ticket_fork.py`（构造同工位双 parcel，对比两读者）。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`inspect.py` 是**ticket 查验**的只读命令：回答「某次执行落盘了什么、有没有内容、零晋升是结论还是故障」。它应然：

1. 按 `run_id` 读 `runs/<ticket>/parcels/*/payload.json`，逐工位给摘要：station 名、payload 顶层 keys、count（候选/逐候选/folds/parcels 数量）、关键字段（family/dedup_digest/evidence_grade/state/skip_reason/net_return/n_trials）。
2. `--station` 只看某一站（不存在的站报 E1008，带「已落盘哪些」提示）；`--full` 带完整 payload（默认只摘要，避免一次把候选集倒进终端）。
3. 零晋升可读性：Verdict 为空时必须能看出是结论（skip_reason/state）而非故障。
4. 只读、零副作用、统一信封。

**关键正确性要求（本模块的核心风险）**：inspect 展示的产物必须与 `evaluate/backtest/certify`（走 executor 重算）**读的是同一份**。若「查验看到 A、重算用的是 B」，用户会被查验误导——这正是本模块暴露的 P1 双读者分叉（见 D2 I-1 / D4）。

**加密视角**：本模块零加密语义；但「零候选/DSR 全失败的结论可见性」是加密高波动场景下判读结果的入口——若摘要看不到 skip_reason，研究员会误以为挖到了东西。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

### 例 1：真实 ticket 查验（成功，摘要模式）

```
$ flow inspect --run-id 20261118T064333Z-0585b103   （59 号 probe_run.py 的真实 dummy run）
→ {"ok":true,"exit_code":0,"data":{
   "records":[
     {"station":"Boundary","keys":["kind","payload","station"],"count":{}},
     {"station":"Certify",...,"count":{}}, ... 8 站 ...
   ],
   "stations":["Boundary","Certify","Evidence","Panel","Portfolio","Search","Target","Verdict"],
   "root":"...\\runs\\20261118T064333Z-0585b103","plan":"cs_2026q4_a"}}
EXIT=0
```

**关键观察**：dummy run 的 8 个 payload 都是 `{"kind":"dummy","payload":{},"station":"..."}`（实测 cat payload.json），所以 `_summary` 的 `_COUNT_KEYS=(candidates,per_candidate,folds,parcels,traces)` 一个都命中不了 → `count:{}`。这不是 inspect 的 bug，而是 dummy 桩无内容；**真实内容要看 `--live` run**（见 D2 I-3）。

### 例 2：不存在的 run_id（真实失败）

```
$ flow inspect --run-id NOPE
→ {"ok":false,"exit_code":2,"errors":[{"code":"E1008",
   "layer":"usage","message":"执行记录不存在: NOPE","path":"inspect.run_id",
   "hint":"检查 runs/ 下的目录名（含 Z 后缀的 UTC 时间戳 + plan_digest 前 8 位）"}]}
EXIT=2
```

`read_ticket` → 目录不存在 → E1008/exit2，fail-closed。

### 例 3：P1 — ticket 双读者分叉（真实探针构造，非推演）

> 脚本 `%TEMP%/fm_review56_67/probe_ticket_fork.py`：构造一个 ticket，Verdict 工位有**两个**合法 parcel 目录（first/末，各自 payload.json + parcel.json 指纹链自洽），分别调用两个读者。

```
FIRST dir prefix = 357b8f2726de3624 | marker = FIRST-1
LAST  dir prefix = 5e89a117d9f5fbf9 | marker = LAST-1
catalog.read_ticket             Verdict marker = FIRST-1   ← inspect 看到"首"
executor.load_committed_parcel  Verdict marker = LAST-1    ← evaluate/backtest/certify 用"末"
FORK = True
```

**根因（文件:行号）**：
- `catalog.read_ticket`（`catalog.py:240-245`）：`for child in sorted(parcels_dir.iterdir())`，`if station is None or station in payloads: continue` → 同工位多目录保留**第一个**。
- `executor.load_committed_parcel`（`executor.py:988-995`）：`found = sorted(root.glob(...))`，`record_path = found[-1]` → 取**最后一个**。

**后果**：append_only 工位（Search/Verdict）重复执行产生多目录时，`flow inspect` 展示首份结论，而 `flow evaluate/backtest/certify` 重算用末份——查验与重算不同源。注释（`catalog.py:226-228`）称取首是为了「Verdict/Search 的 append 语义要求按首次结论」，但执行器实际取末，**两读者意图相反且未对拍**。

### 例 4：`--station` 不存在站（源码 `:48-53`，语义确认）

`wanted not in info["payloads"]` → E1008「本次执行没有 'X' 的产物（已落盘: ...）」。本例 dummy run 8 站齐全，未构造缺失站重跑（属语义确认，非实测数字）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| I-1 | **P1 — ticket 双读者分叉：catalog 取首、executor 取末，inspect 与 evaluate/backtest/certify 不同源**。 | **P1** | `catalog.py:240-245` vs `executor.py:988-995`；实测例 3 FORK=True | **已确认（真实构造对拍）**：这是本模块必查点的直接暴露。修复方向：两读者统一取末（append 链最新结论）或统一取首（按 catalog 注释意图），并补一个「同工位多 parcel 两读者对拍」测试。属编排层/存储层共享语义，inspect 是暴露点非唯一落点。 |
| I-2 | **`_summary` 只看固定 5 个 count 键**：若 payload 用别的键名记候选（如新工位 `signals`/`members`），count 为空，摘要失效。 | P2 | `inspect.py:27-36`；实测例 1 因 dummy payload 键不同 count={} | **需确认意图**：`_COUNT_KEYS` 是硬编码白名单，加新工位/新结果键需改代码。可改为「凡 list/dict 且有长度即计」的通用摘要（但可能把大对象倒进摘要）。低优先。 |
| I-3 | **dummy run 的 inspect 摘要无信息量**（count={}），文档/CLI 未提示「要看内容需 --live」。 | P2（UX） | 实测例 1 + dummy payload `{"kind":"dummy","payload":{}}` | **需确认**：属 dummy 桩预期，非 bug。建议在 inspect 信封里标 `dummy: true` 或提示「此产物为 dummy 桩，无真实候选」，免研究员误判。 |
| I-4 | **信封 `data.plan` 取 `info.frozen.plan.plan`**（`:72`）：若 frozen/plan.json 不存在（legacy ticket），`plan=None`，不报错也不提示。 | P2（边界） | `inspect.py:72`；`catalog.py:253-261` frozen 可能为空 | **需确认**：查验主要看 parcels，plan 缺失不影响核心功能；但静默 None 会让「哪个计划」不可答。可加 `plan_available:False` 标记。 |

**多了什么**：无。`_summary` 的字段白名单是合理的最小摘要设计（I-2 是其代价）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 只读快照查验（read-only inspection） | 采纳 | inspect 不改任何产物，复现排障入口；evaluate/backtest/certify 另走只读重算——职责分离。 |
| 单一读者（一个 read_ticket 供所有命令复用） | **应采纳（I-1 修复方向）** | 当前 catalog 与 executor 各读一份且语义相反（取首 vs 取末），正是「复制解析逻辑两份就等着分叉」的反例（executor.py:982 自己都这么写）。 |
| 完整 payload 默认输出 | 拒绝 | 默认摘要、`--full` 才带完整（`:12-13,61-65`），避免一次倒出候选集——终端可用性。 |
| 从内存状态查验 | 拒绝 | 只信磁盘落盘（read_ticket 读 parcels/），不读 steps/（probe 观察不算正式产物）——与 executor 同源信任规则。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **不存在 run_id fail-closed**：E1008 + hint（实测例 2）。
2. **`--station` 不存在站 fail-closed**：E1008 带「已落盘哪些」（`:48-53`）。
3. **默认摘要、`--full` 完整**：`full` 分支（`:61-65`），避免信息过载。
4. **关键字段透传**：`family/dedup_digest/evidence_grade/state/skip_reason/net_return/n_trials`（`:37-40`）覆盖零晋升判读所需（C10 相关）。
5. **只读零副作用**：无 writer，多次调用输出一致。

**发现的问题**：
- **I-1（P1）是真实分叉**：已用合法指纹链的双 parcel 构造实测，catalog 取 FIRST、executor 取 LAST。这不是理论风险，是可复现产物不一致。
- I-2/I-3/I-4 为 P2 摘要完整性/UX/边界问题。
- 测试现状：`test_v5_s5_single_step.py` 覆盖单步流程，但**无测试钉住「inspect 与 executor 读同一 parcel」**——I-1 缺回归防线。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `catalog.py:226-228` 注释「同工位多目录保留第一个，append 语义按首次结论」 | 声明取首意图 | **必须与 executor 取末统一**：二选一后同步注释；当前注释与执行器相反，文档本身在误导。 |
| `09-app-storage.md` §2 inspect 条目 | 若写「查验与重算同源」 | 补 I-1 现状：当前两读者取首/取末不同源（P1），修复后更新为同源说明。 |
| `09-app-storage.md` §2 | 未说明 dummy vs live 产物差异 | 补 I-3：dummy run 的 payload 为 `{"kind":"dummy",...}` 占位，inspect 摘要无候选——属预期，真实内容需 --live。 |
| `s0/05-orchestration.md` 多目录语义 | 未明确 append_only 工位多 parcel 的「当前结论」定义 | 明确定义（取首/取末），作为两读者共同依据（I-1 根因）。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增工位的结果摘要 | ⚠️ 需改代码 | `_COUNT_KEYS`/关键字段白名单（`:27,37`）需加新键（I-2）。 |
| 查验某次 `--live` run 的真实候选 | ✅ 已支持 | 只要 payload 含 `_COUNT_KEYS` 里的键（candidates 等），摘要自动显示；`--full` 看全部。 |
| 修复双读者分叉 | ❌ 当前不支持（需改共享读者） | 需改 catalog.read_ticket 或 executor.load_committed_parcel 令二者统一，并补对拍测试——不在 inspect.py 单独修。 |
| 按 evidence_grade/state 过滤查验 | ❌ 不支持 | 无参数；可在信封 records 上做（低价值）。 |
| 查验 step/probe 产物 | ❌ 不支持（设计禁止） | 只读 parcels/，不读 steps/（与 executor 同源信任规则）。 |

---

## C1–C12 相关项判定

- **C10 判据不得被放松**：✅ 弱相关——`inspect` 透传 `evidence_grade`/`state`/`skip_reason`/`n_trials`（`:37-40`），让 DSR/FDR 结果可见，但不改判据。
- **C1–C9 / C11 / C12**：不适用（本模块只读查验，不读字段值/不算口径/不下判据）。

## R1–R7 影响

- **R1 真数据跑通**：中性（查验不参与执行）；但查验真实性依赖 ticket 产物完整（I-1 分叉会破坏「查验=实际」）。
- **R2 结论可信**：⚠️ **负向（I-1）**——查验看到首份、重算用末份时，「我查到的结论」与「系统据以判断的结论」可能不同，直接损害可归因性与可信度。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：⚠️ 负向（I-1）——append_only 工位多产物时来源不清。
- **R7 fail-closed**：✅ 正向（缺 run/缺站 E1008）；但 I-1 是「静默不同源」，属 fail-open 风险。

---

## 结论

**本模块本身 74 行只读查验逻辑无 P0/P1 级 bug，但它暴露了一个 P1 级跨模块缺陷：ticket 双读者分叉（I-1）。** 真实构造对拍证明：`catalog.read_ticket`（inspect 用）在 append_only 工位多 parcel 时取**首**，`executor.load_committed_parcel`（evaluate/backtest/certify 用）取**末**，两者不同源且注释意图相反——这是可复现的产物不一致，直接损害 R2 结论可信与 R5 单因子可归因。**建议列为 P1 修复项**：统一两读者取末（或取首）并补对拍测试。其余 I-2/I-3/I-4 为 P2 摘要完整性/UX/边界问题，不影响基本查验。

**请确认本模块审查结果**：
- ✅ 确认通过（P1 I-1 另案处理）→ 我进入第 8 个模块 `cli/explain.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ P1 I-1 想当场裁决取首还是取末 → 告诉我
