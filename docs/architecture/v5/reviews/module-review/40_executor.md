# 模块审查 40 · `v5/orchestration/executor.py`

> 审查日期：2026-11-18
> 依据：源码 1025 行全文通读；依赖 `station.py`（八规格表/input_producers）、`capability_bridge.py`（BridgeResult/CapabilityBridge）、`events.py`（EventLedger）、`state.py`（StateMachine）、`runtime/store.py`（TicketWriter）、`storage/objects.py`、`facilities/`（trace/cache/rng/clock）。设计 `s0/05-orchestration.md` §2/§4/§6、`s0/01 §2.2`（六通道）、`09-app-storage.md`；测试 `test_v5_executor_real.py`、`test_v5_executor_capability.py`、`test_v5_s5_single_step.py`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_orch1.py`（EX 段）、`probe_orch2.py`（CA/M3/STEP/FORK 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Executor 是**唯一执行器**（`:1-28`）：整体跑、单步跑、恢复共用这一个 `execute()`/`execute_station()`，不存在第二条执行路径。职责：

1. **八工位固定顺序唯一路径**（`:554` `for seq, spec in enumerate(STATION_SPECS)`）：Boundary→Panel→Target→Search→Evidence→Verdict→Portfolio→Certify，顺序由规格表定（非硬编码循环变量）。
2. **调用单六通道**（`:434-462` `_call_digest`）：impl 解析（plan 声明优先）、params（工位块整体进，含 time_axis，`:249-252`）、policy、runtime_facts、runtime_facts_digest、seed、phase 全进 `call_digest`。**工位块整体进 params**（`:250-252`）：`time_axis` 改数据窗口却不进 station.params 会让两个方案同 call_digest 串缓存。
3. **指纹链 + 真实依赖**（`:464-492`）：按 `spec.inputs` 解析上游（不是线性链），上游缺失 E3001、stale E3002、产物空 E3003、`verify()` 手改 E3002。
4. **`_parcel_for()` 收口**（`:882-914`）：`execute` 与 `execute_station` 共用一份产物构造——12 字段构造、空产物判据、`verify()` 自检。曾抄两份，判据只改一处时另一条路径继续接受空产物且两份产物长得一样谁都不报错。
5. **落盘顺序**（`:15-17,590-593,916-928`）：payload.json → log.jsonl → parcel.json（原子写）→ trace.jsonl；失败只写 failed trace、不留半成品 parcel.json。
6. **cache opt-in**（`:255-258,308-352`）：默认空 `cache_stations=frozenset()`；按工位白名单开；命中验 payload（反序列化 + `digest.of` 重验，不符 invalidate）+ 执行器统一把 trace 的 origin 标 `cache`（`:344-346,939-940`）——不能让"复用产物"看起来像刚算过。
7. **stale 传播 E3002**（`:484-489`）：上游实现/口径变化后拒绝在失效产物上续跑；`skip_reason` 结果性跳过传播 `upstream_skipped`（`:554-563,674-680`）。
8. **一次性事件**（`:494-522,639-672`）：Reveal 在 Boundary 前（写 runtime_facts 非 params，防声明未揭示为已揭示）、Budget 在 Search 落盘后（真值取 payload.n_trials）、Publish 在 Certify 后（只认 `evidence_grade=out_of_sample`）；只有 `run` 模式触发事件，`step`/`commit` 不触发（I7 防后门）。
9. **单步模式**（`:687-875`）：`step` 写 `steps/`、`commit` 写 `parcels/`+推进 state.jsonl；上游从磁盘 `_load_committed`（只读 parcels/ 不读 steps/）；`probe_only` 工位禁 commit（E3006）。
10. **M2/M3**（`:738-763,794-827`）：单步能力模式按序重建上游对象（bridge.rebuild_upstream，digest 对拍）；commit 幂等短路 + 断电恢复（产物在、state 缺 → 补状态，`state_recovered=True`）。
11. **ticket 双读者**（`:978-1025` `load_committed_parcel`）：从 parcels/ 读正式产物并逐项校验，与 CLI 单命令共用同一信任规则（注释 `:981-983`："复制一份解析逻辑就等着两份规则分叉"）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测，复现脚本 `probe_orch1.py` / `probe_orch2.py`。

### 例 1：八工位唯一执行路径（真实输出）

```
execute("T") → state=done
stations      = [Boundary, Panel, Target, Search, Evidence, Verdict, Portfolio, Certify]
trace_order   = [Boundary, Panel, Target, Search, Evidence, Verdict, Portfolio, Certify]
events        = [{kind:Budget, value:0}, {kind:Publish, value:'sha256:1ad8e5d6...'}]
```
读法：八工位严格按规格表顺序，`execute` 与痕迹顺序一致；dummy 桩 n_trials=0 故 Budget=0，Publish 取 Certify parcel_digest。

### 例 2：依赖校验 + 收口 + 指纹防篡改（真实输出）

| 场景 | 实测输出 |
|---|---|
| stale 上游 Boundary（Panel 依赖它） | `E3002\|Panel 上游 Boundary 已 stale，拒绝续跑` |
| 缺上游产物（空 parcels） | `E3001\|Panel 缺上游产物: Boundary` |
| `_parcel_for(payload={})` | `E3003\|Boundary 产物为空（静默降级）` |
| mode="bogus" | `E1004\|execute mode 必须是 run/step/commit: 'bogus'` |
| 手改 payload 后 `verify()` | `E3002\|Boundary 产物指纹失配（payload 被改写）` |

### 例 3：cache opt-in + 命中验 payload + trace origin（真实输出）

```
cache_stations(默认) = frozenset()
cache_stations(白名单 Boundary) = {'Boundary'}
hit_payload           = {'x':1}        （命中）
off_whitelist_miss    = None           （白名单外不查）
tampered_miss         = None           （payload 字节被改为 {'x':2} → digest 不符 invalidate）
trace_origin_on_hit   = cache
trace_origin_normal   = run
cache_writable        = True
```
读法：默认空、按工位开、命中重验 payload、trace 统一标 origin=cache——F1/F6/§7 全部实测。

### 例 4：M3 断电恢复（真实输出）

```
first_commit_state        = committed
state_lines_before        = 1
recovery_idempotent       = True
recovery_state_recovered  = True      （删 state.jsonl 模拟断电后重放 commit）
parcel_dirs_unchanged     = True      （恢复不重写产物）
state_lines_after         = 1
third_commit_idempotent   = True
third_commit_state_recovered = False  （状态已在 → 纯 no-op）
```
读法：产物在、状态缺时补一条 state 并置 `state_recovered=True`，再提交纯 no-op——幂等且 append-only。

### 例 5：probe 四项不变 + append_only 拒 commit（真实输出）

```
step（Evidence --probe）:
  state=running, events=[]
  parcels_unchanged=True, state_unchanged=True
  steps_dir_exists=True, step_parcel_count=1
  trace 含 origin=step
Search(probe_only) --commit → E3006|Search 是 append_only 工位，禁止 --commit
```

### 例 6：P1 · ticket 双读者取末分叉（真实输出，用户必查点）

构造：同一 ticket `FORK_T1` 两次不同 dummy payload（v=1 / v=2）各 commit 一个 Boundary 正式产物。
```
parcel_dirs_sorted     = ['01-Boundary-9d08df43362b7770', '01-Boundary-c9ed3277a4c312a6']
executor_payload_v     = 2     ← load_committed_parcel 取末（found[-1]，executor.py:988-995）
catalog_payload_v      = 1     ← read_ticket 取首（if station in payloads: continue，catalog.py:240-245）
divergent              = True
```
读法：**两份生产代码对同一 ticket 的同一工位读出不同产物**。`executor.load_committed_parcel`（`:988` 的 `found = sorted(...); record_path = found[-1]`）取排序末项；`storage/catalog.read_ticket`（`:240-245` 首项保留）取排序首项。两者都标"只认 parcels/ 正式产物""同一套信任规则"（executor 注释 `:981-983` 明说怕分叉），但**取末/取首的分叉正是它们想避免的那类分叉**。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| **P1-1** | **ticket 双读者取末/取首分叉**：`load_committed_parcel` 取末（`found[-1]`，:988-995），`catalog.read_ticket` 取首（:240-245）。append_only 工位（Search/Verdict 重复执行）出现多个目录时，CLI 单命令（evaluate/backtest/certify 走 executor 读者）与 `flow inspect`（走 catalog 读者）看到不同 payload；M2 单步重建也走 executor 读者，与 inspect 不一致。 | **P1** | 实测 `executor_payload_v=2` vs `catalog_payload_v=1`；`executor.py:988-995`；`catalog.py:240-245` | **已确认缺陷（本轮唯一 P1）**：两处注释都声称"同一信任规则/只认正式产物"，但对"多产物取哪个"这一核心语义相反。取末假设"最后执行的是最新结论"，取首假设"首次结论不可覆盖"（append_only 语义）。**需用户裁决方向**：若 append_only iever 守首次，executor 应改取首；若允许后写覆盖，catalog 应改取末并放弃"首次不可改"。统一到单一函数（如 catalog 提供 `read_one_parcel`）最稳。本轮只读不改代码。 |
| E-1 | **事件仍内存 EventLedger（已知遗留）**：executor 构造 `EventLedger()` 默认无 ledger（`:260`），run 事件不落 `db/events.jsonl`；落盘一次性只在显式传 ledger 时（events.py 支持，见 42 号）。plan §U4 D2 明列此遗留。 | P2（已知遗留） | `executor.py:260`；`events.py:33-41` | **已确认**：单步 events 内存语义，跨进程事件去重需上层注入 Ledger。S5-M2 范围外。 |
| E-2 | **`_trace` 吞 ValueError**（`:950-955`）：单步 seq 不从 1 起时 TraceLog.append 抛 ValueError 被静默 pass，落盘 trace.jsonl 仍写。 | P2（低） | `executor.py:950-955` | **已确认符合设计**：注释说明单步只跑一个工位 seq 不连续，跳过"内存 TraceLog 登记"但落盘不受影响。无信息丢失。 |
| E-3 | **`_family()` 回退 implementation_defaults("cli")["family"]**（`:878-880`）：policy_view 无 family 时用默认族名做 Budget 事件键。 | P2（低） | `executor.py:878-880` | **已确认**：dummy 路径无 family，默认保证事件有键；real 路径由 policy_view 提供。无风险。 |

**多了什么**：无功能冗余；`_parcel_for` 与 `_scoped_view`（桥侧）的收口都是消历史分叉的必要动作。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 唯一执行器（整体/单步/恢复共用） | 采纳 | 单步是同函数窄化调用，不是第二条路径；防"两套执行规则分叉" |
| 八工位规格表驱动 | 采纳 | 顺序/依赖/事件/side_effect 都是数据表（`station.py`），改约束不改执行器 |
| 显式状态机 + append-only state.jsonl | 采纳 | 六状态转移表（E3005），一份 digest 一个事实，恢复补状态不改写 |
| 幂等短路（同 payload 不新增产物） | 采纳 | 目录名由 payload_digest 决定，同名即同内容；覆盖会开"改写历史"口子 |
| cache opt-in 白名单 | 采纳 | 命中无内存对象表，全开会让下游拿空对象；命中标 origin=cache 可审计 |
| 任意 DAG / 第二套执行路径 | **拒绝** | 规格表线性依赖 + 单一 execute，无插件式 DAG |
| commit 触发事件 | **拒绝（I7）** | 单步提交不得成计数/发布后门；只有 run 触发事件 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **八工位固定顺序**：实测 stations/trace_order 一致（`:554`）。
2. **六通道 call_digest**：plan 声明 impl 改写指纹（`:86-94` 测试）、工位块 time_axis 进 params（`:68-76` 测试）、小写工位名归一（`:78-83`，防静默漏参）。
3. **真实依赖校验**：E3001/E3002/E3003 全实测（`:464-492`）。
4. **`_parcel_for` 收口**：空产物 E3003 实测（`:882-914`）；手改 payload verify E3002 实测（`:122-130`）。
5. **落盘顺序 + 原子写**：payload→log→parcel.json→trace，失败只写 failed trace（`:590-617`）；tmp+replace 在 store.py。
6. **cache opt-in**：默认空、白名单、命中验 payload、origin=cache 实测（`:255-258,308-352,939-940`）。
7. **stale 传播**：E3002 实测；skip_reason → upstream_skipped（`:554-563,674-680`）。
8. **Reveal 前置**：写 runtime_facts 非 params（`:497-509`），单步缺记录 E3006（`:90-98` events.py）。
9. **Budget/Publish 真值**：n_trials 从 Search payload 取（`:646-651`）、evidence_grade 校验（`:659-671`）；实测 Budget=0（dummy）、Publish 取 Certify digest。
10. **单步四项不变**：step 实测 parcels/state/events 不变（`:696-700,866-875`）。
11. **M2/M3**：重建对拍（39 号 M2-2）、state_recovered 实测（`:794-827`）。
12. **append_only 拒 commit**：Search commit E3006 实测（`:712-719`）。

**发现的问题**：**P1-1（ticket 双读者分叉）为本轮唯一 P1**，已用真实调用坐实；E-1~E-3 为 P2/确认设计。**除 P1-1 外无 P0/P1 级 bug**。测试覆盖六通道、指纹链、依赖、落盘往返、cache、stale、单步、M2/M3（`test_v5_executor_real.py` 382 行 + `test_v5_s5_single_step.py`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §2/§4/§6 | 描述唯一执行器/状态机/单步 | 整体一致；补一句"ticket 多产物时load_committed_parcel 取末"的语义（P1-1 相关） |
| `09-app-storage.md` | 描述 ticket 读取 | 需明确"同一 ticket 同一工位多产物时取哪个"——当前 executor 取末、catalog 取首，**文档应裁定其一并统一代码**（P1-1） |
| `s0/05 §6.2`（单步 Ref 重建） | 描述 M2 重建 | 一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增工位 | ❌ 需改多处 | STATION_SPECS/STATIONS/CONSTITUTIVE_TOPS/schema 多处；执行器循环本身无需改（数据表驱动） |
| 新能力接入执行链 | ✅ 已支持 | `use_capabilities=True` + bridge 六通道；`_impl_for` 解析 plan impl（`:402-431`） |
| 单命令只读重算（evaluate/backtest/certify） | ✅ 已支持 | `persist=False` 不落 steps/、不写 trace（`:270-273`）；读冻结 ticket 走 load_committed_parcel |
| 事件账本落盘（跨进程一次性） | ⚠️ 需注入 Ledger | executor 默认内存 EventLedger（E-1）；上层传 `events=EventLedger(Ledger(...))` 可启用 |
| cache 全工位开启 | ❌ 明确拒绝 | opt-in 白名单（F1）；命中无内存对象表，全开会静默错 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（执行器不算年化）；bar 时间经 bridge 透传给 backtest（见 39 号）
- **C2/C3 资金费/spot**：✅ 透传——Search 的 n_trials 注入 runtime_facts（`:627-631`），Budget/Publish 只认真值；funding 透传在 bridge（39 号 BR-4）
- **C4 合约四元组**：❌ 无（全局 P0-5）
- **C6 上市/下架**：✅ 上游 Boundary 指纹经 lineage 传递，stale 拒续跑（`:484-489`）
- **C7 24/7 缺口**：✅ 依赖产物的 verify + stale 检查，不靠日历
- **C10 判据不被放松**：✅ 构成性参数改口径须 stale 重跑（E3002），判据字段只在 caliber（E1020 编译期）

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（唯一执行路径，dummy/real 双路径实测 done）
- **R2 结论可信**：✅ 强支撑（指纹链 + n_trials 真值 + evidence_grade 发布门槛 + M2 对拍）
- **R5 单因子可归因**：✅ 正向（逐工位 parcel 与 lineage 可拆）
- **R7 fail-closed**：✅ 强支撑（E3001/2/3、cache 验 payload、stale、篡改 E3002、append_only 拒 commit 全实测）；**P1-1 是 R7 的局部例外**——双读者分叉会让不同命令看到不同"真相"

---

## 结论

**本模块唯一 P1：ticket 双读者取末/取首分叉（P1-1）。** 真实调用坐实：同一 ticket 两次 Boundary commit 后，`load_committed_parcel`（executor.py:988-995，取 `found[-1]`）读到 v=2，`catalog.read_ticket`（catalog.py:240-245，取首）读到 v=1，`divergent=True`。两处都自称"只认正式产物/同一信任规则"，却对"多产物取哪个"这一核心语义相反——影响 `flow inspect` 与 CLI 单命令/单步重建的一致性。需用户裁决方向（建议统一到单一读者函数，并按 append_only"首次不可改"语义取首，或修改 catalog 放弃首项保留）。

**除 P1-1 外无 P0/P1 级 bug**：八工位唯一路径、`_parcel_for` 收口、cache opt-in 验 payload+origin=cache、stale E3002、M2 重建对拍、M3 state_recovered、单步四项不变均实测成立。E-1（事件内存遗留）为 plan 已列已知项。**生产可用（能真挖因子）= 是（带 P1 修复建议）**：全链可跑真能力，P1-1 影响的是 inspect 与单命令的一致性，不阻塞主链执行，但应在下轮统一。

**请确认本模块审查结果**：
- ✅ 确认 P1-1 → 我记录并进入第 41 个模块 `station.py`（本轮不改代码）
- ✏️ 裁决 P1-1 方向（取首 / 取末 / 统一函数）→ 告诉我，我写入结论
- ⚠️ 需补充 M2/M3 更多对拍证据 → 告诉我
