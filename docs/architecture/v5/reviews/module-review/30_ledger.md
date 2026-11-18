# 模块审查 30 · `v5/storage/ledger.py`

> 审查日期：2026-11-18
> 依据：源码 126 行全文通读；依赖 `layout.py`（register 路径）、`filelock.FileLock`。设计 `09-app-storage.md` §3.1（append-only 真相）、`s0/03 §2`（四类登记）；测试 `test_v5_storage.py:138-171`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `fm_review_26_33/probe2_storage.py`（D 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Ledger 是**append-only 的全局单调唯一真相**，回答"一次性事件（揭示/计数/发布）如何保证只发生一次"。职责边界：

1. **只追加、永不改删**：坏了不能重算——这是状态，不是派生物（`:1-3`）。
2. **四类登记**：trials / events / published / members（`:23`）。
3. **一次性事件去重**：有 `event_id` 即视为一次性；同键重复追加必须拒绝（E3006），否则"重放一次"就能刷计数或重复揭示——AC4–AC8 的存储层保证（`:6-7,26-30`）。
4. **跨进程原子去重**：seen 判定与写盘在同一把 OS 文件锁内（`:59-62,70-93`）。
5. **锁内重扫回并**：实例构造后若有外部写入（另一进程），下次 append 必须重扫磁盘并**回并**实例 `_seen` 视图，防止两个实例对"见过这条吗"判断漂移（`:71-75`）——这是本模块最关键的不变量。
6. **半行容错**：坏 JSON 行不是真相，`_scan_keys`/`records` 跳过（`:47-53,103-110`）。

**术语**：`get()` 让重复触发有两种正确结局——同进程返回旧值、另一进程已写则读回那条，而不是把 E3006 当异常处理或重写一份（`:113-123`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> venv Python 实测。临时根 `...\data2`。

### 例 1：append-only + 去重 + 锁内重扫回并（实测）

```
=== D ledger append-only/去重/回并 ===
{'重复event_id拒绝': '[E3006] 一次性事件重复: t1 (at trials.t1)',
 'A实例seen回并后拒绝B写入的key': '[E3006] 一次性事件重复: merge1 (at events.merge1)',
 'trials计数': 1, 'events计数': 5}
```

输入与解读：
```
led.append("trials", {"event_id":"t1","kind":"trial"})      # 写入
led.append("trials", {"event_id":"t1","kind":"trial"})      # 同键 → E3006（实测）

a=Ledger(L); b=Ledger(L)                                     # 两个实例
b.append("events", {"event_id":"merge1","kind":"Reveal"})   # B 写 merge1
a.append("events", {"event_id":"merge2","kind":"Reveal"})   # A 写 merge2（触发锁内重扫，回并 merge1）
a.append("events", {"event_id":"merge1","kind":"Reveal"})   # A 再写 merge1 → E3006（实测）
```

逐格解读：
- **重复 event_id 拒绝**：`append` 在锁内重扫 `_scan_keys(path)`，key 已存在 → E3006（`:76-82`），不写盘。
- **A 实例 seen 回并后拒绝 B 写入的 key**：A 构造时不知道 merge1（B 后写），但 A 写 merge2 时锁内重扫把 merge1 回并进 `_seen`（`:72-75`），所以 A 再写 merge1 被拒——**防丢更新实测成立**。若无回并（只 add 本次新 key），A 会长期落后于磁盘、误判"没见过 merge1"而重复写。
- **trials 计数=1 / events 计数=5**：append-only 行数正确。

### 失败输入 → 错误输出（代码路径 + 实测）

| 非法输入 | 输出 |
|---|---|
| `append("bogus", {...})` | `E3006` "未知登记账本"（`:66-67`） |
| 同 event_id 重复 append | `E3006` "一次性事件重复"（实测） |
| 无 event_id 的记录 | 不参与去重，正常追加（`_dedup_key` 返回 None，`:26-30`） |
| 坏 JSON 行 | `_scan_keys`/`records` 跳过（`:47-53,103-110`） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| Lg-1 | **去重键只看 `event_id` 存在性**：`_dedup_key` 只要记录里有 `event_id` 就当一次性；若两类事件（如 Reveal 与 Budget）误用同一 event_id 字符串，会被错误去重。 | P2 | `ledger.py:26-30` | **需确认意图**：event_id 由上层按命名空间生成（如 `reveal:T1`/`budget:T1`）。属调用方契约，存储层无法校验。低优先。 |
| Lg-2 **`records()` 每次全文件读 + json.loads**：无缓存；`get()` 遍历全部记录（`:113-123`）。高频调用时 O(n)。 | P2（性能） | `ledger.py:95-126` | **需确认**：登记量当前小（揭 1 次/计数按试验），索引层（index.py）才是查询面；属可接受。 |
| Lg-3 | **锁内重扫只读 event_id 键，不校验记录内容变化**：若外部改了已写记录的值（违反 append-only），`_seen` 不察觉。 | P2 | `ledger.py:42-57` | **已确认符合设计**：append-only 信任文件不可变；篡改检测是 objects 层职责（内容寻址），ledger 不做内容校验。 |

**多了什么**：无冗余。`get()` 的跨进程幂等语义（`:113-123`）是被容易忽略但必要的设计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Event sourcing append-only log（本模块） | 采纳 | 一次性事件留全历史；揭示/计数/发布不可重放刷账；与 AC4–AC8 对齐 |
| OS 文件锁内去重判定（filelock + O_APPEND） | 采纳 | 跨进程原子，两个实例不各写一份（实测回并） |
| 锁内重扫回并实例视图 | 采纳 | 修复"实例 `_seen` 长期落后磁盘"漂移（`:71-75`，实测成立） |
| 数据库 UNIQUE 约束 + UPSERT | **拒绝** | 会原地改状态、丢历史；且引入DB依赖违背方案 B/文件式真相 |
| 内存 seen 不落盘 | **拒绝** | 跨进程失效，重放即可刷计数（`:60-62` 注释） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **append-only**：`os.O_APPEND` 逐行写，O_APPEND 保证多写者不撕裂（`:85-90`）。
2. **一次性去重**：锁内重扫 + key 判定 + E3006（`:70-82`），实测拒绝重复。
3. **锁内重扫回并**：`self._seen[register] |= seen`（`:75`）实测有效——A 实例拒绝 B 后写的 key，防丢更新坐实。
4. **半行容错**：`_scan_keys` 与 `records` 跳过坏 JSON（`:47-53,103-110`），与 catalog/objects 同策略。
5. **路径走 layout**：`layout.register(name)` 白名单校验（`:62,69,99`），未知名 E3006。
6. **确定性序列化**：`json.dumps(sort_keys=True, separators=(",",":"))`（`:84`）保证逐字节稳定，利于 reconcile 投影一致。
7. **读回放 `records`**：按 register 过滤、只读（`:95-111`），供 index 重建真相。
8. **跨进程幂等 `get`**：读回另一进程已写的记录（`:113-123`）。

**发现的问题**：Lg-1~Lg-3 均为 P2/需确认意图，**无 P0/P1 级 bug**。

**测试覆盖**：`test_v5_storage.py:138-171` 覆盖 append-only、去重、未知 register、锁内回并（`test_ledger_merges_locked_rescan_into_instance_view`）——本模块是 U3 中测试最扎实的之一。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §3.1 | 描述 append-only 真相 + 四类登记 | 与实现一致，无需更新 |
| `s0/03 §2` register 区 | 描述 trials/events/published/members | 一致，无需更新 |
| `05-contracts.md`（AC4–AC8） | 一次性事件语义 | 实现支撑到位，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增登记类别 | ❌ 需改代码 | VALID_REGISTERS + layout.register 白名单两处（Lg-3 关联） |
| 去重键复合化（如 `(kind,event_id)`） | ⚠️ 需改代码 | `_dedup_key` 写死只看 event_id（Lg-1） |
| 内容校验（检测已写记录被改） | ❌ 不支持（设计拒绝） | append-only 信任不可变；篡改检测归 objects 层 |
| 高频查询 | ⚠️ 借 index.py | `records/get` 全表扫描（Lg-2）；查询面是 IndexDB 的职责 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为通用登记存储，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在登记层处理）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（一次性事件正确去重是八工位状态机的地基）。
- **R2 结论可信**：✅ 强支撑（揭示/计数只发生一次，防重放刷账污染证据链）。
- **R5 单因子可归因**：✅ 正向（事件账本可靠，归因链的事件序号可信）。
- **R7 fail-closed**：✅ 强支撑（重复 key E3006、未知 register E3006、半行跳过）。

---

## 结论

**本模块代码质量高，无 P0/P1，可确认通过。** 核心不变量——append-only、一次性去重、**锁内重扫回并防丢更新**——全部实测成立（重复 event_id E3006；A 实例构造后 B 写 merge1，A 靠重扫回并在下次 append 拒绝 merge1）。这是 findings 反复强调的关键防线，本轮以真实调用坐实。Lg-1~Lg-3 均为 P2/需确认意图，不影响"能真挖因子"。**建议**：代码与文档均不动。
