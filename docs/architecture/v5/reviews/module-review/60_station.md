# 模块审查 60 · `v5/app/cli/station.py`

> 审查日期：2026-11-18
> 依据：源码 186 行全文通读；上游 `orchestration/executor.py`（execute_station/load_committed_parcel）、`orchestration/station.py`（BY_NAME/STATION_SPECS）、`runtime/store.py`（TicketWriter）、`upstream/store.py`（read_dataset/BAR_DATASET）、`capability/params.py`；设计 `s0/05-orchestration.md` §6（单步 probe/commit 四不变）、`s0/02-crosscutting.md` §4、`09-app-storage.md`；测试 `tests/v5/test_v5_s5_single_step.py`、`tests/v5/test_v5_executor_capability.py`。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`station.py` 是**单步执行某工位**的核心命令（`writes_facts`），回答五件事：

1. **probe（默认）vs commit 互斥**：`--probe`（无 flag 同义）产物写 `steps/`，不推进正式状态、不写 state.jsonl、不触发事件、不碰已发布结论（四项不变 = AC7 判据）；`--commit` 产物写 `parcels/`、追加 state.jsonl，**仍不触发事件**。同时给即 E1001，不静默选一个。
2. **无 --plan，只认冻结方案**：构成必须来自 ticket 目录 `frozen/plan.json`+`frozen/context.json`（`run.py:freeze_context` 落盘），否则「单步重放」成「拿别的参数重算」。缺冻结即 E3001。
3. **stale 闸**：单步前重算供给 checksum，与冻结时的 `supply_pins` 比对；数据被重新采集过就 E3002 拒绝续跑——防「单步在另一份数据上算出看似正常但与 ticket 不同源的产物」。老 ticket 无 supply_pins 则标 `legacy_ticket`，不假装检查过。
4. **M2 单步跨进程对象重建**：单步进程没有 run 的内存对象表，必须按顺序载入上游正式产物、经 bridge.rebuild_upstream 重建对象（这步在 Executor 里做，本模块负责正确注入上下文）。
5. **同 seed 同 scope**：master_seed 与 seed_scope 都从冻结产物读，与 run 一致——否则 sub_seed 不同、指纹对不上，等于另一套随机流重算。

**加密视角**：单步不改变任何口径/判据——它消费 frozen 的 caliber/policy_view，CLI 不提供任何口径参数（P1 守卫在 `__init__.py`）。`stale_check` 让 24/7 加密数据「被重新采集过」这一事实显式暴露，不静默续跑。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 全部真实调用（`venv/Scripts/python.exe -m v5.app.cli station ...`，ticket 来自 59 号 probe_run.py 的真实 dummy run），非推演。

### 例 1：缺冻结方案失败路径（真实）

```
$ flow station Boundary --on NOPE
→ {"ok":false,"exit_code":1,"errors":[{"code":"E3001",
   "layer":"execution","message":"ticket NOPE 没有冻结方案",
   "path":"ticket.NOPE.frozen",
   "hint":"先 flow run 生成 ticket；单步只认冻结方案，不接受现场补参数"}]}
EXIT=1
```

E3001→exit1，fail-closed（不猜默认参数）。

### 例 2：--probe 与 --commit 互斥（真实）

```
$ flow station Boundary --on <ticket> --probe --commit
→ {"ok":false,"exit_code":2,"errors":[{"code":"E1001",
   "layer":"usage","message":"--probe 与 --commit 互斥",
   "hint":"观察与提交必须分开，见 s0/05 §6"}]}
EXIT=2
```

P2「不静默选一个」落地（`:93-95`）。

### 例 3：probe 成功（真实，含 stale_check）

```
$ flow station Boundary --on 20261118T064333Z-0585b103 --probe
→ {"ok":true,"exit_code":0,"run_id":"20261118T064333Z-0585b103","data":{
   "station":"Boundary","mode":"step","scope":"steps",
   "payload_digest":"sha256:cf6696326d2af662...","parcel_digest":"sha256:ea5c89449b81...",
   "call_digest":"sha256:c0fdfe156fe32...",
   "trace":{...,"station":"Boundary","origin":"step","status":"done","skip_reason":null},
   "state":"running","idempotent":false,"state_recovered":false,"live":false,
   "stale_check":{"checked":true,"stale":false,"changed":[]},
   "stage":"S5-single-step"}}
EXIT=0
```

- `mode=step`（无 flag 默认观察，`mode = "commit" if args.commit else "step"`，`:101`）；
- `scope=steps`（probe 产物写 steps/，不碰 parcels/）；
- `stale_check.checked=true, stale=false`（冻结时 checksum 与当前一致，说明 run 后数据未变）；
- 指纹链 call_digest/parcel_digest 真实给出，非占位。

### 例 4：`--full` / `--station` 不在本模块（在 inspect）

`station --help` 实测只有 `name --on --probe --commit --format`，无任何口径参数——P1 守卫无触发面。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| S-1 | **`--probe` 与「无 flag」同义，但 help 不显示默认语义**：`station --help` 只列 `--probe/--commit`，无「默认 probe」提示；用户可能误以为必须显式给 flag。 | P2（UX/文档） | `__init__.py:95-96` 的 `add_argument` 无 help 文本；本模块注释（`:100`）说明默认 probe | **需确认**：行为正确（例 3 无 flag=step），只是 help 不友好。改 `__init__.py` 加 help 文本即可，非逻辑 bug。 |
| S-2 | **`_supply_staleness` 只比 checksum，未比 plan 结构**：冻结时 plan 若被手改（`frozen/plan.json`），单步不校验 plan 是否仍与 run 时一致——checksum 相同但 plan 被改，单步会用改后的 plan。 | P2（需确认意图） | `station.py:33-68`（只比 `ds.checksum`）；`frozen/plan.json` 无指纹自检 | **需确认**：`frozen/plan.json` 是内容寻址写入（run.py:freeze_context），手改会与 `frozen/context.json` 里的 fingerprints 不符。但本模块未对拍 plan_digest——守线可加「重算 plan_digest 与 ctx.fingerprints.plan_digest 比对」。低概率但属 fail-closed 完整性缺口。 |
| S-3 | **master_seed 仍从 `plan.search.seed` 读，缺 seed 时静默 42**（与 run.py:85 同）：本模块 `:125-126` 重复这段逻辑，未复用 run 的实现——两处若改一处会漂移。 | P2（重复） | `station.py:125-126`；`run.py:84-85` | **需确认意图**：两处都从冻结产物读 plan 再取 seed，逻辑相似但上下文不同（run 从 compiled.plan，单步从 frozen plan）。可抽公共函数；但当前两处数字一致，无实际漂移。并入 59 号 R-4（seed 缺省标注）。 |
| S-4 | **stale_check 对 live 单步才重算供给**：`:142-150` 只有 `ctx["live"]` 为真时才重新 `build_supply_context` 注入 supply_fn；dummy 单步不重算（只做 `_supply_staleness` 的 checksum 比对）。 | — | `station.py:142-150` | **符合应然**：dummy 无真实供给可注入；stale 比对对 dummy 也做（例 3 checked=true）。无差距。 |

**多了什么**：无。`_frozen`（缺冻结 E3001）与 `_supply_staleness`（stale E3002）两道闸是必要防线，非冗余。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 单步 observe/commit 分离（不可变快照） | 采纳 | probe 写 steps/ 不推进状态（AC7 四不变），commit 才写 parcels/+state——「观察不是重试」落地，防一次误观察污染正式链。 |
| 冻结方案 + stale 校验（immutable artifact + freshness gate） | 采纳 | 无 --plan，只认 frozen/；checksum 变了 E3002 拒续跑——24/7 加密数据重采频繁，这是防「不同源产物」的关键。 |
| 单步自带参数（可读 plan 覆盖） | 拒绝 | 否则「单步重放」=换参重算，破坏可复现与 ticket 同源性。 |
| 单步触发事件（Reveal/Budget/Publish） | 拒绝 | commit 仍不触发事件（`:7`），事件只在整体 run 的 Executor.execute 里——一次性语义集中管理。 |
| M2 跨进程对象重建（rebuild_upstream） | 采纳（间接） | 单步进程无内存对象表，靠 frozen context + 上游 parcel 重建（在 Executor 内）；本模块正确注入 policy_view/seed_scope/impls。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **probe/commit 互斥 fail-closed**：`:93-95` E1001，实测例 2。
2. **未知工位拒**：`name not in BY_NAME` → E1001（`:97-99`），带 hint 列可用工位。
3. **冻结方案双文件校验**：`_frozen`（`:71-88`）缺任一文件即 E3001，顶层非 dict 也 E3001——不猜默认。
4. **stale 闸在最前**：`:109-118` 在构造 sink/执行前查 stale，注释明确「等 Boundary 跑完再发现数据不同源已白算一个工位」。
5. **legacy ticket 显式标记**：`:44-47` 无 supply_pins 返回 `checked:False reason:legacy_ticket stale:False`——不假装检查过（诚实性）。
6. **seed_scope 从冻结指纹读**：`:124,140` 用 `ctx.fingerprints.plan_digest`，与 run 同 scope——sub_seed 一致。
7. **skip_reason 结果性跳过不报错**：`:157-168` 零合格候选等返回 envelope_ok(state=skipped)，「没有产物可给」但仍是成功结论——与 run 的 skip 语义一致。
8. **finally 重置日志**：`:153-155`，同 run。

**发现的问题**：
- 无 P0/P1 级 bug。S-1（help 不显示默认 probe）、S-2（plan 手改未对拍指纹）、S-3（seed 重复逻辑）均为 P2，不影响单步正确性。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §6 | probe/commit 四不变 + append_only 拒提交（Search/Verdict E3006） | 与实现一致（Executor.execute_station 处理 append_only），无需更新 |
| `09-app-storage.md` §2 station 条目 | 描述单步观察不是重试 | 补 S-1：help 应标注「无 flag=probe 观察」，改 `__init__.py:95` 的 help 文本 |
| `s0/05` §6.2 stale 判定 | 若只写「checksum 比对」 | 补 S-2：当前未对拍 frozen plan 的 plan_digest，手改 plan 不会被 stale 抓到（完整性说明） |
| `17-walkthrough.md` | 单步命令示例 | 若有示例建议用 `--probe` 显式写法，同时说明无 flag 同义 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 单步执行新工位 | ✅ 半配置化 | 工位在 `orchestration/station.BY_NAME` 注册；本模块 `name in BY_NAME` 通用判定，新工位自动可用（前提是 compiler.STATIONS 也加）。 |
| commit 模式扩展新事件 | ❌ 不支持（设计禁止） | commit 仍不触发事件（`:7`）；若未来允许需改 Executor.execute_station，属语义变更。 |
| stale 判据扩展（多维度） | ⚠️ 需改代码 | `_supply_staleness` 只比 checksum（`:63-67`）；加行数/符号集/时间轴维度需改此处 + run.py:supply_pins。 |
| 单步 `--set` 覆盖参数 | ❌ 不接受（设计禁止） | 无 --plan 无 --set；加覆盖会破坏「冻结方案同源」。 |
| probe 产物自动清理 | ❌ 不支持 | steps/ 观察产物累积，无 GC；属运维（本轮不做，plan §0.0）。 |

---

## C1–C12 相关项判定

- **C3 spot/futures**：✅ stale 重算供给时用 `plan.supply.market`（`:50`）读对应市场数据，市场维度随冻结 plan 固定。
- **C6 上市/下架**：✅ 面板全窗（common）在 run 时编译，单步继承 frozen plan，不改可用时间。
- **C7 24/7 缺口/stale**：✅ 强——checksum 重采检测（例 3 checked=true），防数据变动后单步不同源。
- **C12 1 观测=1 天**：中性（单步按工位执行，观测单位已由上游定）。
- **C1/C2/C4/C5/C8/C9/C10/C11**：不适用（本模块不读字段/不算判据/不下口径）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向——单步跨进程重建（M2）让 --live 单步在独立进程也能跑真能力；实测 dummy 单步成功（例 3）。
- **R2 结论可信**：✅ 强支撑——同 seed/scope（`:124,140`）+ stale 闸 + 冻结方案，单步产物与 run 同源可复现。
- **R3 因子零代码扩展**：中性（单步执行已有工位，不限制因子空间）。
- **R5 单因子可归因**：✅ 正向——单步隔离执行某工位是六步归因链路的基础工具（手工拼命令，plan U11 决策项）。
- **R7 fail-closed**：✅ 强支撑——缺冻结 E3001、互斥 E1001、未知工位 E1001、stale E3002、legacy 诚实标记，全拒。

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 单步 probe/commit 四不变、无 --plan 只认冻结、stale 闸前置、M2 跨进程对象重建注入、同 seed/scope、skip 语义一致——全部实测成立（缺冻结 E3001/exit1、互斥 E1001/exit2、probe 成功 scope=steps + stale_check.checked=true）。3 项 P2：S-1（help 不显示默认 probe）、S-2（plan 手改未对拍指纹）、S-3（seed 逻辑与 run.py 重复），均为 UX/完整性边界/重复问题，不影响「生产可用=能真挖因子」。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 6 个模块 `cli/stations.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ S-2 想当场裁决（是否对拍 frozen plan 的 plan_digest）→ 告诉我
