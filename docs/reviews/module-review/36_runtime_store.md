# 模块审查 36 · `runtime/store.py`

> 审查日期：2026-11-18
> 依据：源码 220 行全文通读；依赖 `storage/layout.py`（九区物理路径、Layout.run_dir/fact_kind/tmp_file）、`contracts/digest.py`（blob_digest）、`contracts/errors.py`（E3003/E5001/E5004）。设计 `s0/03-runtime.md` §1（四区权限接口）、`09-app-storage.md` §3（原子写/不可变产物）。测试 `test_v5_storage.py`、`test_v5_full_review_regressions.py:162-186`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_u4.py`（S 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Runtime Store 回答两件互不混淆的事（`:1-9`）：

1. **数据根解析**（纯函数，`:37-64`）：优先级 `显式 > FLOW_DATA_ROOT > operations.data_root > ~/factor-miner-v5`；必须绝对路径（相对路径随 cwd 漂移破坏可复现，E5004）；不 mkdir（是否建目录由调用方决定）。

2. **四区权限接口**（接口形状保证，s0/03 §1.1）：三者互不兼容，执行期不得写共享仓——
   - **ReadOnlyStore**（`:67-91`）：只有 `read_bytes`，路径必须在数据根内（防 `..` 与符号链接逃逸，E5004）；产物缺失是执行事实 E3003（非"别名指向不存在版本"的 E4001）。
   - **TicketWriter**（`:94-172`）：继承 ReadOnlyStore（能读共享事实），但**无任何 Depot 写方法**；只对本 Ticket 目录写。写入即校验四条（s0/03 §1.2）：① ticket_id 单路径段正则（`:30,104-110`）；② 目标规范化后仍在 `Ticket/<id>/` 内（`:116-123`）；③ 内容寻址一致（expected_digest）；④ 目标不存在或内容相同才写，异内容拒覆盖（不可变产物，`:125-154`）。落盘用 tmp + `os.replace` 同盘原子替换（`:140-144`）；磁盘满/权限不足转 E5001 结构化错误（不让裸 OSError 变 E3004 误判为执行 bug，`:145-153`）。
   - **DepotWriter**（`:175-201`）：供给链专用，唯一能写共享仓；只准写 `supply/` 区（`:188-189`），防错误派生路径覆盖别的 Ticket 产物或自建 `db/ledger.jsonl` 绕过账本去重。
   - **StoreContext**（`:204-220`）：一次运行的 Layout + 三视图工厂。

**ticket 单路径段校验防 `../..` 逃逸**（用户必查点）：`_TICKET_ID = ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`（`:30`），`TicketWriter.__init__` 拒非法 id（`:104-110`），且 `run_dir` 结果必须落在 `runs/` 内（双保险，`:112-114`）。注释明确：`TicketWriter("../../db")` 会把 ticket_root 挪到数据根外，之后所有 `relative_to` 校验都以逃逸后的根为基准——所以这一条不能省。

**事件账本落盘 `db/events.jsonl` 跨进程一次性**：本模块提供 `append_jsonl`（`:156-172`，只接受 `.jsonl`，防把字节追加进已落盘不可变产物），事件账本本身由 `orchestration/events.py` 经 Ledger 落 `db/events.jsonl`；跨进程一次性语义实测见 42 号文档 EV-3（两实例同 key 只一行）。本模块是它的**落盘接口底座**。

**四区 = 权限分区，九区 = 物理路径**：物理路径只在 `storage/layout.py`，本模块不重复定义（`:8`）。`StoreLayout = Layout` 兼容别名（`:32-34`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 调用真实 API 打印结果，临时根 `...\fm_review_34_43\data_u4`。复现脚本 `probe_u4.py` S 段。

### 例 1：数据根优先级（真实输出）

```
explicit : ...\data_u4\e        （显式 > 环境）
env      : ...\data_u4\env      （FLOW_DATA_ROOT）
config   : ...\data_u4\cfg      （operations.data_root）
relative : E5004|数据根必须是绝对路径: 'relative'
```

### 例 2：ticket_id 单段校验防逃逸（真实输出）

| 输入 ticket_id | 结果 |
|---|---|
| `20261116T053000Z-1a2b3c4d` | `OK` |
| `../..` | `E5004\|ticket_id 非法（必须是单个路径段）: '../..'` |
| `a/b` | `E5004\|ticket_id 非法（必须是单个路径段）: 'a/b'` |
| `ok\n`（尾换行） | `E5004\|ticket_id 非法（必须是单个路径段）: 'ok\n'` |
| `""` | `E5004\|ticket_id 非法（必须是单个路径段）: ''` |

读法：`$` 在 Python 正则允许末尾一个 `\n`，本模块用 `fullmatch`（`:104`）而非 `match+$`，尾换行也被拒（与 `full_review_regressions:162-167` 一致）。

### 例 3：TicketWriter 写 / 幂等 / 拒覆盖 / 拒逃逸（真实输出）

```
write            = runs\TK1\parcels\x\payload.json
idempotent_same  = runs\TK1\parcels\x\payload.json   （同内容返回同一路径）
diff_rejected    = E5004|目标已存在且内容不同，拒绝覆盖: ...\runs\TK1\parcels\x\payload.json
escape_rejected  = E5004|写入路径逃逸 Ticket: ../evil.txt
digest_mismatch  = E5004|内容与声明的 digest 不一致
```

### 例 4：接口形状 + append_jsonl + 区域隔离（真实输出）

```
ReadOnlyStore.has_write_bytes   = False
ReadOnlyStore.has_publish_bytes = False
read_bytes(数据根外路径)         = E5004|路径逃逸数据根: ...\outside.bin
append_jsonl("trace.jsonl",...) = OK（写 runs/TK1/trace.jsonl）
append_jsonl("parcel.json",...) = E5004|append_jsonl 只接受 .jsonl 目标: parcel.json
DepotWriter 写 supply/x.bin     = OK（supply\x.bin）
DepotWriter 写 db/hack.bin      = E5004|路径逃逸数据根: ...\db\hack.bin
```

读法：三种视图能力互斥（读句柄无写方法）；`append_jsonl` 的白名单防绕过不可变产物保护；DepotWriter 只认 supply/ 区，防覆盖 `db/ledger.jsonl` 类账本文件。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| S-1 | **ticket_id 正则允许 `.` 和 `-` 组合成 `..` 以外的逃逸形态？**：`_TICKET_ID` 允许点号，`fullmatch("..")` 实际被拒（实测 E5004，因首字符须字母数字）；但 `a..b` 这类含连续点号合法——不过它在 `runs/` 下只是普通目录名，不构成逃逸。 | P2（低） | `store.py:30`；实测 `..`/`a/b` 拒 | **已确认安全**：`..` 因首字符规则被拒；`a..b` 无路径分隔符，`relative_to` 双保险兜底。无实际逃逸面。 |
| S-2 | **`read_bytes` 不做产物指纹校验**：只读字节并返回，不验 blob_digest；校验在调用方（如 `executor.load_committed_parcel` 重算 payload_digest 对拍）。 | P2（职责） | `store.py:73-79`；`executor.py:998-1011` | **已确认符合分层**：存储层只保证路径安全与存在性，内容真实性由执行器"重推导+对拍"负责（见 40/43 号文档 M2）。 |
| S-3 | **`append_jsonl` 非原子**：用 `open("a")` 直接追加，无文件锁；多进程同时 append 同一 jsonl 可能交错行。 | P2 | `store.py:156-172` | **需确认意图**：trace/本执行事件是单 Ticket 内单进程写；跨进程一次性事件走 Ledger（带 OS 文件锁，见 30/42 号文档）。若将来多进程写同一 trace 需加锁，当前非落点。 |
| S-4 | **四区是权限语义非物理隔离**：TicketWriter 与 DepotWriter 在同一 Layout（数据根）下，靠方法集合与 `_assert_inside` 区分子集，非 OS 级权限。 | P2（确认设计） | `store.py:1-9,188-189` | **已确认符合设计**（s0/03 §1.1 明确"接口形状保证"）：能力层拿不到存储句柄，执行器拿不到 DepotWriter；契约层隔离足够。 |

**多了什么**：无冗余。`StoreLayout=Layout` 兼容别名是迁移期一版保留（`:32-34`），合理。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 接口形状保证权限（只读/票写/仓写三视图） | 采纳 | 编译期（类型）即阻止"执行期写共享仓"，比运行时检查更难绕过 |
| 内容寻址 + tmp + `os.replace` 原子写 | 采纳 | 不可变产物无"存在但不完整"半成品；同盘 replace 防跨盘撕裂 |
| 异内容拒覆盖而非允许覆盖 | 采纳 | 可复现：同一 digest 永远同一字节；覆盖会打开"改写历史"口子 |
| 单路径段正则 + `relative_to` 双保险 | 采纳 | 防 `../..` 与符号链接逃逸（实测四类非法 id 全拒） |
| 裸 `OSError` 透出 | **拒绝（已修）** | 磁盘满/权限不足转 E5001，避免运维把存储故障误判执行 bug（`:145-153`） |
| 原地 `to_feather` 覆盖历史 | **拒绝** | 方案 B 不可变事实层，产物只能新增版本 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **数据根纯函数 + 绝对路径**：优先级实测四档正确，相对路径 E5004（`:37-64`）。
2. **ticket_id 单段校验**：`fullmatch` 拒 `../..`/`a/b`/尾换行/空（实测，`:104-110`）。
3. **ticket_root 双保险**：`_assert_inside(self.ticket_root, layout.runs)`（`:112-114`），防 layout 被改坏。
4. **路径逃逸防护**：`_target` 的 `resolve().relative_to()`（`:116-123`）+ ReadOnlyStore 的 `_assert_inside`（`:81-91`），实测逃逸 E5004。
5. **内容寻址一致**：`expected_digest` 不符 E5004（实测，`:128-129`）。
6. **幂等 + 拒覆盖**：同内容返回、异内容 E5004（实测，`:130-138`）。
7. **原子写**：tmp + `os.replace`（`:140-144`）；目录先 mkdir -p。
8. **磁盘满结构化错误**：OSError → E5001（`:145-153`）。
9. **append_jsonl 白名单**：非 `.jsonl` E5004（实测，`:162-167`），防追加污染不可变产物。
10. **Depot 区隔离**：`publish_bytes` 只认 supply/ 区（`:188-189`），实测试图写 db/ 被拒。
11. **接口形状**：ReadOnlyStore 无写方法（实测 hasattr=False），执行器拿不到 DepotWriter。

**发现的问题**：S-1~S-4 均为 P2/需确认意图，**无 P0/P1 级代码 bug**。测试覆盖 ticket_id 各类非法形态、逃逸、内容寻址、append_jsonl（`test_v5_full_review_regressions:162-186`、`test_v5_storage.py`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/03-runtime.md` §1（四区权限） | 描述三视图接口形状 | 与实现一致，无需更新 |
| `09-app-storage.md` §3（原子写/不可变） | 描述 tmp+replace、异内容拒覆盖 | 一致，无需更新 |
| `s0/03-runtime.md` §1.2（写入四条校验） | 描述四条 | 一致；可补"ticket_id fullmatch 而非 match+$，尾换行也拒"的细节（实测已守） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增权限视图（如审计只读视图） | ⚠️ 需改代码 | 三视图是 dataclass/subclass 硬编码，加视图需改 StoreContext 工厂（`:204-220`） |
| 新增可写区（如 reports/） | ❌ 需改代码 | DepotWriter 写死 supply/ 区（`:188`）；新区需加白名单判定 |
| 内容校验（读时验 digest） | ❌ 不支持（职责外） | 存储层只保路径安全；内容真实性归执行器对拍（S-2） |
| 多进程写同一 trace.jsonl | ⚠️ 当前非原子 | append 无锁（S-3）；若需要须借 Ledger 的 OS 文件锁模式 |
| 事件账本落盘 db/events.jsonl | ✅ 底座已支持 | 本模块提供 append_jsonl；一次性跨进程语义由 events+Ledger 实现（见 42 号） |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为通用存储与权限分区，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在存储层处理）。
- 间接支撑 **R7 fail-closed**：路径逃逸、异内容覆盖、磁盘满三类均以明确错误码拒绝（实测）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（不可变产物原子落盘是八工位执行地基）
- **R2 结论可信**：✅ 正向（内容寻址 + 拒覆盖保证历史产物不被改写）
- **R7 fail-closed**：✅ 强支撑（逃逸 E5004、异内容 E5004、磁盘满 E5001、append 白名单，全实测）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 必查点全部实测坐实：ticket 单路径段校验拒 `../..`/`a/b`/尾换行/空（E5004），四区权限三视图方法互斥且区域隔离（DepotWriter 写 db/ 被拒），`append_jsonl` 只接受 `.jsonl` 为事件账本落盘 `db/events.jsonl` 提供底座（跨进程一次性见 42 号）。原子写 + 内容寻址 + 异内容拒覆盖 + 磁盘满 E5001 全部按设计实现。四项 P2 均为职责边界或确认意图（S-3 多进程 append 无锁当前非落点）。**生产可用（能真挖因子）= 是**：执行期存储安全与可复现性成立。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 37 个模块 `envelope.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 S-3（trace 多进程 append 是否加锁）→ 告诉我
