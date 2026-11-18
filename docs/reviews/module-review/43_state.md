# 模块审查 43 · `orchestration/state.py`

> 审查日期：2026-11-18
> 依据：源码 50 行全文通读；依赖 `contracts/errors.py`（E3005/FlowError）。设计 `s0/05-orchestration.md` §4（六状态机）、`03-orchestration.md` §4.3。测试 `test_v5_orchestration.py`、`test_v5_s5_single_step.py`（state.jsonl 幂等/恢复）。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_orch1.py`（ST 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

StateMachine 是**显式六状态转移表**（`:1-5`）：每次执行一个状态实例，非法转移 E3005，reason 枚举不允许自由文本。

1. **六状态 + 转移表**（`:12-19` ALLOWED）：
   - pending → {running, skipped}
   - running → {done, failed, skipped}
   - done → {stale}
   - failed → {pending}
   - skipped → {}（终态）
   - stale → {pending}
2. **两条判定规则**（`:4-5`）：无候选通过 = skipped（**结果**，exit 0）；折数 0/依赖缺失/重建失败 = failed（故障）。
3. **skipped 必须带枚举 reason**（`:21-24,47-50`）：`REASONS = {no_candidate_passed, no_candidates_generated, upstream_skipped, precondition_unmet, insufficient_cross_section}`；非 skipped 转移清空 reason（`:50`）。
4. **非法转移 E3005**（`:40-46`）：消息带当前状态 + 允许的下一状态集合（实测 `done → failed` 拒、hint 列 allowed）。

**状态机 vs state.jsonl**（边界）：本模块是**内存状态机**（每次 execute 新建实例，`:539` executor），记录单次执行的生命周期；落盘真相是 `TicketWriter.append_jsonl("state.jsonl", ...)`（executor `:850-856` 的 append-only 事实，见 40 号 M3）。state.py 不持久化——断电恢复靠 executor 重放 commit + state.jsonl 对拍（`:794-827`），不靠把 StateMachine 序列化。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `StateMachine`，复现脚本 `probe_orch1.py` ST 段。

### 例 1：转移表 + 合法链（真实输出）

```
ALLOWED = {'pending':{'running','skipped'}, 'running':{'failed','skipped','done'},
           'done':{'stale'}, 'failed':{'pending'}, 'skipped':set(), 'stale':{'pending'}}
REASONS = ['insufficient_cross_section','no_candidate_passed','no_candidates_generated',
           'precondition_unmet','upstream_skipped']
合法链 pending → running → done（实测 state 终值 'done'）
恢复链 running → done → stale → pending → running（实测终值 'running'）
```

### 例 2：非法转移 + skipped reason（真实输出）

| 场景 | 实测输出 |
|---|---|
| `done → failed` | `E3005\|非法状态转移: done → failed` |
| `running → skipped`（无 reason） | `E3005\|skipped 必须带枚举 reason，实得 None` |
| `running → skipped("bogus")` | `E3005\|skipped 必须带枚举 reason，实得 'bogus'` |
| `running → skipped("no_candidate_passed")` | `(state='skipped', reason='no_candidate_passed')` |
| 非 skipped 转移后 reason | 清空为 None（实测 skipped 后再转不保留 reason；正常 done 时 reason=None） |

读法：skipped 是唯一要求 reason 的转移，且 reason 必须来自枚举——防"随便写个理由就把零晋升当结果"或"把故障写成 skipped 绕过告警"。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| ST-1 | **`done → stale` 是唯一失效出口，但 `failed → pending` 与 `stale → pending` 都不带 reason**：失败原因（折数 0/依赖缺失/重建失败）不进状态机，只在 trace.error_code。 | P2 | `state.py:12-19,40-50`；`executor.py:610-617` | **已确认符合设计**：failure 语义由 trace 的 error_code（E3001/2/3/4）承载，状态机只管状态流转；reason 专属于 skipped（结果性跳过）。 |
| ST-2 | **`REASONS` 是硬编码 frozenset**：新增 skipped 原因（如"预算耗尽"）需改代码，非配置化。 | P2 | `state.py:21-24` | **需确认意图**：五个 reason 覆盖当前所有结果性跳过场景（AC52 零晋升等）。若将来扩展可升为配置，但状态机 reason 是契约枚举，硬编码利于调用方按码分支，属合理选择。 |
| ST-3 | **StateMachine 不持久化 / 无快照**：进程崩溃后内存状态丢失，恢复靠 executor 重放 state.jsonl（40 号 M3），非本模块职责。 | P2（职责） | `state.py:27-50`；`executor.py:794-827` | **已确认符合分层**：状态机是单次执行的内存模型，落盘真相在 state.jsonl（append-only）；把 StateMachine 序列化会引入"两份状态"风险。 |

**多了什么**：无冗余。50 行实现完整覆盖六状态 + reason 枚举 + E3005。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 显式转移表（白名单） | 采纳 | 非法转移 E3005 即时拒，不留"怎么到这个状态"的模糊 |
| 结果性跳过 vs 故障分离（skipped/done vs failed） | 采纳 | 零晋升是结论 exit 0，不是故障——AC52；reason 枚举防滥用 |
| 内存状态机 + 落盘 state.jsonl 真相分离 | 采纳 | 恢复靠 append-only 事实对拍，不序列化内存对象 |
| reason 自由文本 | **拒绝** | 枚举约束（`:47-48`），防"随便写理由绕过判定" |
| 隐式状态（布尔标志组合） | **拒绝** | 六状态显式建模，转移可审计 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **六状态表完整**：ALLOWED 实测六状态及转移集合（ST-1）。
2. **非法转移 E3005**：`done → failed` 实测拒，消息带 from→to（`:40-46`）。
3. **skipped reason 强制枚举**：无 reason / "bogus" 均 E3005（实测），合法 reason 通过（`:47-48`）。
4. **非 skipped 清 reason**：`:50`，实测 done 时 reason=None，不残留跳过理由。
5. **reason 集合五值**：`insufficient_cross_section/no_candidate_passed/no_candidates_generated/precondition_unmet/upstream_skipped`（实测），与 s0/05 §4.3 一致。
6. **初始状态 pending**：`:29`，每次执行新建实例（executor `:539`），无跨执行污染。
7. **纯内存无 I/O**：50 行无 now()、无文件操作，可复现。

**发现的问题**：ST-1~ST-3 均为 P2/确认设计或职责边界，**无 P0/P1 级代码 bug**。测试覆盖合法链、非法转移、skipped reason（配合 executor 的 results skipped / state.jsonl 幂等恢复测试 `test_v5_s5_single_step.py:226-285`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §4（六状态机） | 描述六状态 + 两条判定规则 | 与实现一致，无需更新 |
| `03-orchestration.md` §4.3（skipped reason） | 描述 reason 枚举 | 一致，无需更新 |
| `s0/05 §4` | 若新增 skipped 原因 | reason 是 frozenset 硬编码（ST-2），新增须改代码并同步本节与调用方；届时更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增状态（如 paused） | ❌ 需改代码 | ALLOWED 转移表 + executor 的状态消费分支；非配置化（数据表但代码内） |
| 新增 skipped reason | ⚠️ 需改代码 | `REASONS` frozenset 加值（ST-2），同步 s0/05 §4.3 |
| 状态持久化/快照 | ❌ 职责外 | 落盘真相在 state.jsonl（ST-3），恢复靠 executor 重放 |
| 状态变更事件通知 | ❌ 不支持 | 本模块只转移 + 校验，无回调/观察者；notify 在设施层（U2） |
| 单命令只读重算的状态 | ✅ 不入状态机 | executor `persist=False` 的只读执行不新建正式状态（40 号） |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为状态机，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在状态层处理）。
- 间接支撑 **R7 fail-closed**：非法转移 E3005、skipped reason 枚举约束，防状态被随意改写。

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（六状态是八工位执行生命周期的骨架）
- **R2 结论可信**：✅ 正向（零晋升=skipped 结果而非 failed，防把"无因子"误报成故障或反之）
- **R7 fail-closed**：✅ 强支撑（非法转移 E3005、reason 枚举，实测）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 六状态转移表、两条判定规则（无候选=skipped 结果 / 折数 0·依赖缺失·重建失败=failed）、skipped 强制枚举 reason、非法转移 E3005——全部真实调用坐实（done→failed 拒、无 reason/bogus 拒、合法 reason 通过、恢复链 running→done→stale→pending→running 实测）。三项 P2 均为确认设计或职责边界：ST-1 失败原因走 trace.error_code 不进状态机、ST-2 reason 硬编码 frozenset（契约枚举，利于按码分支）、ST-3 不持久化（落盘真相在 state.jsonl，恢复靠重放）。**生产可用（能真挖因子）= 是**：状态机可靠，不阻塞真实执行与断电恢复。

**请确认本模块审查结果**：
- ✅ 确认通过 → U4/U5 十模块审查全部完成，进入 P0/P1 清单汇总
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 ST-2（reason 是否升配置化）→ 告诉我
