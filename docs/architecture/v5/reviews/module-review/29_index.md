# 模块审查 29 · `v5/storage/index.py`

> 审查日期：2026-11-18
> 依据：源码 167 行全文通读；依赖 `layout.py`（FACT_KINDS/fact_kind）、`ledger.py`（VALID_REGISTERS/records）、`objects.py`（SCHEMA_VERSION）。设计 `09-app-storage.md` §3.1（索引不是真相）；测试 `test_v5_storage.py:174-194,197-224,386-394`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `fm_review_26_33/probe2_storage.py`（C 段）。

> **基线更正**：plan §U3 与旧报告曾写"GC/index 未做（已知遗留）"。本模块实测**已实现**（`IndexDB.reconcile/assert_consistent/query`，38-167 行），推翻旧基线（findings §2 已登记）。本轮核实并给行号。

---

## D1 · 应然（抛开实现，这个模块该做什么）

IndexDB 是**可重建的 DuckDB 查询面**，回答"如何快速查询 ledger 登记与 facts 对象，且索引坏了能重建"。职责边界：

1. **索引不是真相**：`meta.duckdb` 不进备份；`ledger.jsonl` + `facts/*.parquet` 才进。任何人不得只写索引不写登记（`:1-4`）。
2. **可重建判据**：删掉 `meta.duckdb` 后 `reconcile()` 恢复全部查询能力（`:6`）——实测通过。
3. **从真相重放，不独立产状态**：所有写操作从 ledger + sidecar 重建，`reconcile` 幂等（重跑逐位一致，`:49-81`）。
4. **一致性校验**：`assert_consistent` 逐类比对索引行与真相，不符报 E5004（`:126-151`）——防"索引与真相分叉却不报错"。
5. **单一白名单驱动**：对象面枚举用 `layout.FACT_KINDS`（含 `parcel_data`），历史事故：曾手抄 5 类漏 `parcel_data`，该类永不进索引（`:83-103` 注释）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> venv Python 实测（duckdb 2.0.0）。临时根 `...\data2`。

### 例 1：reconcile + 一致性 + 查询（实测）

```
=== C index reconcile/一致性/重建 ===
{'reconcile计数': {'trials': 0, 'events': 2, 'published': 0, 'members': 0, 'objects': 2},
 '一致性': {'trials': True, 'events': True, 'published': True, 'members': True, 'objects': True},
 'counts': {'trials': 0, 'events': 2, 'published': 0, 'members': 0, 'objects': 2},
 '查询结果': [{'event_id': 'e1', 'kind': 'Reveal'}, {'event_id': 'e2', 'kind': 'Budget'}],
 '删库重建计数一致': True,
 '破坏后一致性检测': '[E5004] 索引与真相不一致: events 真相 3 行 / 索引 2 行 (at index.reg_events)'}
```

输入：
```
led.append("events", {"event_id":"e1","kind":"Reveal","ticket_id":"T1"})
led.append("events", {"event_id":"e2","kind":"Budget","ticket_id":"T1"})
idx=IndexDB(L); idx.reconcile(led); idx.assert_consistent(led)
idx.query("SELECT event_id,kind FROM reg_events ORDER BY event_id")
```

逐格解读：
- **reconcile 计数**：4 类登记（events=2）+ objects=2，`VALID_REGISTERS` 全建表（`:54-68`）。
- **一致性全 True**：`assert_consistent` 逐类比对，5 类（4 登记 + objects）全 ok（`:130-151`）。
- **删库重建一致**：`L.index_db.unlink()` 后 `reconcile()` 计数与首次逐位相同（`:49-81` 幂等）——"索引不是真相"判据实测成立。
- **破坏后 E5004**：再 append 一条不 reconcile → 真相 3 行 / 索引 2 行 → E5004（`:144-150`），fail-closed。

### 例 2：对象面从 sidecar 重建（实测 objects=2）

- `_rebuild_object_rows` 遍历 `FACT_KINDS`（6 类），对每类的 `*.schema.json` 提取 digest（`Path(file_name).stem`，`:93-103`），对 `*.json`（write_json 产物，无 sidecar）按 stem 补 SCHEMA_VERSION（`:104-114`）。实测 objects 索引 = 2（probe2 中写入的对象）。
- digest 用 `sha256:` 前缀（`:100,111`），与 objects.ObjectRef.digest 格式一致（测试 `test_index_objects_digest_uses_sha256_prefix` 钉住）。

### 失败输入 → 错误输出（代码路径，部分实测）

| 非法输入 | 输出 |
|---|---|
| `query()` 索引库不存在 | `E3003` "索引库不存在，先 reconcile()"（`:119-120`） |
| 索引与真相不一致（追加登记未 reconcile） | `E5004` "索引与真相不一致"（实测） |
| 索引行数/字段与真相不同 | `_rows_equal` 归一化后 sorted 比对，不符 E5004（`:153-158`） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| I-1 | **`query(sql)` 接受任意 SQL 字符串**，无只读守卫。注释称"只读查询"，但 `con.execute(sql)` 不阻止 `INSERT/DROP`（`:117-124`）。 | P2 | `index.py:117-124` | **需确认意图**：调用方都是内部只读代码；但"只读"是约定非强制。若他被外部输入拼 SQL，可写索引（不过索引不是真相，reconcile 会冲掉）。建议加 `SELECT/WITH` 前缀校验或只读连接。 |
| I-2 **`assert_consistent` 与 `_rebuild_object_rows` 共用真相投影，但 payload JSON 序列化含 `ensure_ascii=False`（`:34`）**：同一 rec 在不同 Python 版本 json.dumps 顺序一致（sort_keys 未用，但 dict 插入序稳定）。 | P2（稳定性） | `index.py:27-35` | **低优先**：`_ledger_row` 用 `json.dumps(rec, ensure_ascii=False)` 不 sort_keys；ledger.append 写入时 sort_keys=True（`:84`），两者序列化的 payload 字段顺序可能不同——但 `_rows_equal` 把 payload 当字符串比对，若顺序不同会误报不一致。实测通过（append 的 rec 是 dict，键序与 `_ledger_row` 一致）。需确认无隐藏分叉。 |
| I-3 | **objects 面只读 sidecar/json 清单，不读 parquet 内容**：索引不校验对象字节是否被篡改（E5004 由 objects.read_table 在读取时发现，不由 index 发现）。 | P2（职责） | `index.py:69-79,83-115` | **已确认符合设计**：索引只建查询面，内容校验是 objects 职责（见模块 28），两层分工清晰。 |

**多了什么**：无冗余。`_LEDGER_COLUMNS` 投影 + `_rows_equal` 归一化是防误报的必要设计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| DuckDB 可重建查询面（本模块） | 采纳 | 列存分析快；删库可重建（实测），索引不进备份；契合 append-only 真相 |
| Event-sourcing 投影（从 ledger 重放到索引） | 采纳 | `reconcile` 从真相重放，不独立产状态；幂等（`:49-81`） |
| 单一白名单驱动多消费者 | 采纳 | FACT_KINDS 含 parcel_data 修复历史分叉（`:83-89`） |
| 索引作为真相 / 只写索引不写登记 | **拒绝** | 索引是派生，坏了能重建；真相是 ledger + parquet（`:1-4`） |
| 读 parquet 内容建索引 | **拒绝（当前）** | 内容校验交给 objects 层；索引只读 sidecar 清单，避免重复扫描大 parquet（O-3） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **索引不是真相**：删库可重建（实测 `删库重建计数一致=True`），reconcile 从 ledger + sidecar 重放（`:49-81`）。
2. **reconcile 幂等**：DROP + CREATE + INSERT 全量重建，重跑逐位一致（`:55-79`，实测）。
3. **一致性校验 fail-closed**：逐类比对，不符 E5004（`:126-151`，实测）。
4. **FACT_KINDS 单一来源**：`_rebuild_object_rows` 遍历 FACT_KINDS（`:89`），含 parcel_data，历史分叉已闭合。
5. **两种对象形态都枚举**：sidecar 型（parquet）与纯 json 型（write_json）分别处理（`:93-114`），实测 objects=2 覆盖两种。
6. **digest 格式统一**：对象面 digest 加 `sha256:` 前缀（`:100,111`），与 ObjectRef 一致。
7. **错误码语义**：索引库不存在 E3003、不一致 E5004（实测）。
8. **query 只读参数化**：SQL 参数用 `?` 绑定（`:122`），避免字符串注入。

**发现的问题**：I-1（query 无只读守卫）、I-2（payload 序列化顺序潜在分叉）均为 P2/需确认，无 P0/P1。

**测试覆盖**：`test_v5_storage.py:197-224` 覆盖幂等重建、一致性检测；`:174-194` 钉住单一白名单；`:386-394` 钉住 digest 前缀。**未覆盖"query 任意 SQL"**（I-1），属验证缺口。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §3.1 | 描述"索引不是真相，可重建" | 与实现一致（实测删库可重建），无需更新；建议把"GC/index 未做"旧句删除（findings §2 基线更正） |
| `production_gap_review` / `v5_fix_plan` 等历史报告 | 若含"GC/index 未做" | 标记为已过期：index.py 已实现 reconcile/query/assert_consistent（本轮核实） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增登记类别 | ⚠️ 半配置化 | `VALID_REGISTERS`（ledger.py）加值 + `layout.register` 白名单 + reconcile 循环自动跟上；需两处 |
| 新增对象类别 | ✅ 支持（经 FACT_KINDS） | 单一来源，reconcile 自动枚举（`:89`） |
| 新增查询索引维度（如按 ticket 查 objects） | ⚠️ 需改代码 | 表结构在建表 SQL 里写死（`:57-59,71-73`），加列需改 reconcile |
| 只读查询守卫（I-1） | ❌ 未实现 | 当前靠约定；可加 SELECT 前缀校验或只读连接 |
| 内容级索引（校验 parquet 字节） | ❌ 不支持（设计拒绝） | 内容校验归 objects 层（I-3） |

---

## C1–C12 相关项判定

- **C1–C7 / C9–C12**：不适用（索引层不涉业务口径）。
- **C8 开放列集**：✅ 对象面列清单来自 sidecar，不写死；FACT_KINDS 单一来源支持新对象类别。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（索引可重建，实测删库恢复，数据不因索引损坏丢失）。
- **R2 结论可信**：✅ 强支撑（一致性校验 E5004 防索引/真相分叉导致的错查）。
- **R3 因子零代码扩展**：✅ 正向（新对象类别经 FACT_KINDS 自动进索引）。
- **R7 fail-closed**：✅ 强支撑（索引缺失 E3003、不一致 E5004）。

---

## 结论

**本模块已实现且质量高，无 P0/P1，可确认通过。** 明确推翻计划 §U3 的"GC/index 未做"旧基线：`IndexDB.reconcile/query/assert_consistent/删库重建/一致性校验` 全部落地并实测通过（events=2 幂等重建、删库计数一致、破坏后 E5004、objects=2 含两种形态）。FACT_KINDS 含 parcel_data 的单一来源修复历史分叉，是 findings §2 基线更正的实证。I-1（query 无只读守卫）、I-2（payload 序列化顺序）为 P2/需确认，不影响"能真挖因子"。**建议**：从遗留清单删除"GC/index 未做"，历史报告标记过期；代码不动。
