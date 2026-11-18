# 模块审查 50 · `facilities/cache.py`

> 审查日期：2026-11-18
> 依据：源码 132 行全文通读；依赖 `contracts/digest.py`（blob_digest/compute_digest/assert_relative_path）、`errors.py`（E2001）；编排层 `orchestration/executor.py:241,258,308-352,939-940`（cache_stations opt-in、命中验 payload、trace origin=cache、计数不跳过）；设计 `s0/02-crosscutting.md` §7、F5/F6；测试 `tests/tests/test_v5_facilities.py`（23 例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_facilities.py` 的 F-cache 段。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`cache.py` 是以 `call_digest` 为键的产物复用设施。它该做的：

1. **opt-in，默认不生效**（用户必查点）：`Executor(cache_stations=...)` 必须显式列出允许复用 payload 的工位，默认 `cache_stations=()` 空（executor.py:241,258）。原因是能力调用除 payload 还产出内存对象（掩码/候选集/面板），命中时无法从 payload 复原，无差别开启会让下游拿到空对象表（s0/02 §7，实测 `node_enabled` 默认 False）。
2. **只有两类合法键**：`call`（工位级）与 `node`（节点级，默认关），不允许自定义键/手工拼字符串；station/probe 作用域只读（`writable` 仅 upstream/ticket）。
3. **命中重验**（F5）：`get()` 命中后必须重验"文件存在 + 内容指纹"（既认 `blob_digest` 原始字节，也认 `compute_digest` JSON 结构），任一项不符视为未命中，不抛错、不返回空产物；路径经 `assert_relative_path` + `resolve()` 防目录穿越。
4. **命中只跳过计算，不跳过计数与落盘**（F6/G11）：命中由执行器统一把 trace 的 `origin` 标成 `cache`，预算计数仍为 1，产物仍走落盘。
5. **内存键表有界**（F5 同族）：`DEFAULT_MAX_ENTRIES=1024` 的 `OrderedDict` 兼作 LRU（get 命中 `move_to_end`，put 满员淘汰队首），淘汰只丢键表映射不删文件（文件由 GC/引用计数管理，缓存无权碰）。

**不变量**：缓存无权删内容寻址文件；`evictions` 是观测字段（供 doctor/预算核对），不进指纹；`node_enabled=False` 时 `get_node/put_node` 直接 no-op。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `fm_probe_facilities.py` 的 F-cache 段。

### 例 1 · opt-in + 命中验 payload + LRU（实测）

```
node_enabled default = False
writable ticket  = True
writable station (只读) = False
get miss = None
put+get hit = (True, 'sha256:5d47ea16...e2392c')     # 写入后命中，返回 payload_digest
tampered payload -> miss (F5) = True                  # 文件内容被改 → 视为未命中
deleted file -> miss (F5) = True                      # 文件被删 → 视为未命中
path escape in payload_ref -> miss = True             # '../escape.json' → 视为未命中
evictions after 3 puts cap=2 = 1
keys remaining = ['C1', 'C2']                          # LRU 淘汰最先 put 的 C0
station invalidate (只读) 拒: E2001
```

**解读**：
- **opt-in 默认空**：`Cache(node_enabled=False)` 默认节点级缓存关（实测）；真正的 opt-in 在执行器 `cache_stations=()` 默认空——只有显式列出的工位才查/写缓存。
- **命中验 payload（用户必查点）**：`put("CALL1", "p.json", digest)` 后 `get` 命中；篡改文件内容、删除文件、路径穿越三种情况全部视为未命中（F5）——"返回被篡改的产物比 miss 昂贵得多"。
- **LRU 有界**：cap=2 连 put 3 个键，淘汰 1 次，剩 C1/C2（C0 被淘汰），`evictions=1`（观测字段，不进指纹）。
- **只读真只读**：station 作用域 `invalidate` 也被拒 E2001（源码:97-102 注释记录"put/put_node 都查 writable，唯独 invalidate 漏了"的 P1-9 已修）。

### 例 2 · 执行器接线：命中验 payload + trace origin=cache + 计数不跳过（G11）

> 编排层实证（executor.py，非本模块代码，但是用户必查点的完整落点）：

- `_cache_get`（:308-324）：`Cache.get` 重验文件+内容指纹后，**再验一次反序列化结果**——`json.loads` 后 `self.fx.digest.of(payload) != hit.payload_digest` 则 invalidate + miss（双重复验）。
- `_cache_hit_last`（:346）+ `_trace`（:939-940）：命中时 `origin` 被强制标成 `cache`（"不能让产物看起来像刚算过——否则跑了几次无法审计"）。
- `_from_cache_or_invoke`（:339-352）docstring："命中跳过**计算**，不跳过落盘、计数与 trace（F6）"。

**判断**：用户必查点"命中验 payload + trace origin=cache；落盘与计数不跳过（G11）"在 cache+executor 协作层完整成立，实测代码路径确认。

### 失败路径（实测）

| 非法输入 | 结果 |
|---|---|
| `Cache(scope='bogus')` | E2001 |
| `Cache(max_entries=0)` | E2001 |
| station 作用域 `put`/`invalidate`/`put_node` | E2001 |
| 篡改/删除 payload、路径穿越 | 视为 miss（不抛错） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| C-1 | **opt-in 默认空 cache_stations**（用户必查点，正面）：`Executor(cache_stations=())` 默认空，节点缓存 `node_enabled=False`，实测确认。 | —（正面） | executor.py:241,258；cache.py:42,105-106；实测 | **已确认符合设计**。s0/02 §7 明确理由是内存对象无法从 payload 复原。 |
| C-2 | **命中验 payload + trace origin=cache + 落盘/计数不跳过**（用户必查点 G11，正面）：三层双重复验 + origin 强制标 cache + F6 计数。 | —（正面） | executor.py:308-352,939-940；cache.py:64-88；实测 | **已确认符合设计**。无 P0/P1 缺口。 |
| C-3 | `Cache.get` 读取整个 payload 文件字节到内存（`target.read_bytes()`:84）再哈希；大 payload 落盘对象若很大，命中路径有 IO+内存开销。 | P2（性能） | `cache.py:84`；`_digest_matches`:121-132 | **需确认意图/已接受**：当前产物是 JSON payload（中小），"双重复验优先于性能"（篡改比 miss 昂贵）。若未来 payload 是大 parquet，应考虑流式哈希或信任 Ref.digest。低优先。 |
| C-4 | `put` 的 `produced_at` 字段写死空串（:93,30），`CacheHit.produced_at` 未填实际时间。 | P2（完整性） | `cache.py:27-31,93` | **需确认意图**：`produced_at` 疑似预留字段，当前无消费者；空串不影响命中（命中只验 ref+digest）。建议要么填值要么删字段，低优先。 |
| C-5 | `invalidate` 只读保护是后补修复（源码:98-99 注释"唯独 invalidate 漏了"的 P1-9）——说明历史上只读边界曾有洞。 | P2（历史） | `cache.py:97-102` | **已确认已修**：实测 station invalidate→E2001。测试钉住，无残留。 |

**多了什么**：无冗余。`_node` 一级缓存虽默认关，但为将来"节点级复用"预留且默认安全（no-op），不算死代码。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 内容寻址 + 命中重验（content-addressed cache） | 采纳 | `call_digest` 为键，命中验文件存在+内容指纹（F5），篡改/删除即 miss——"宁可重算不可用错产物" |
| LRU（OrderedDict，有界 1024） | 采纳 | 长跑进程内存有界；淘汰只丢键表不删文件（文件由 GC 管）——缓存与存储职责分离 |
| opt-in 白名单工位 | 采纳 | 默认不生效，避免内存对象（掩码/面板）命中后无法复原的假成功 |
| 全局无差别缓存（默认全开） | 拒绝 | s0/02 §7 实测 65× 净亏 + 内存对象不可复原；无差别开会破坏正确性 |
| 信任路径不验内容 | 拒绝 | 命中只认文件名 → 文件被替换/篡改后返回错产物，比 miss 昂贵（F5） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **opt-in 默认空**（用户必查点）：`node_enabled=False`（实测）、`cache_stations=()`（executor.py:241）。
2. **命中验 payload**（用户必查点）：`get()` 重验文件+内容指纹（:64-88），篡改/删除/穿越实测 miss；执行器再验反序列化结果（executor.py:321）——双重复验。
3. **trace origin=cache**（用户必查点）：命中时 `_cache_hit_last` 触发 `_trace` 强制 origin=cache（executor.py:939-940）。
4. **落盘与计数不跳过 G11**（用户必查点）：`_from_cache_or_invoke` docstring 明确命中只跳计算（executor.py:339-352）；F6 预算计数仍为 1（s0/02 F6）。
5. **LRU 有界**：cap=2 实测淘汰 1 次、`evictions=1`（观测字段不进指纹）；F5 内存键表有界。
6. **只读边界完整**：station 的 put/invalidate/put_node 全 E2001（实测 invalidate E2001，P1-9 已修）。
7. **路径安全**：`assert_relative_path` + `resolve().relative_to(root)` 防目录穿越（:71-80），穿越实测 miss。
8. **两种摘要都认**：`_digest_matches` 先试 `blob_digest`（原始字节），再试 `compute_digest`（JSON 结构）（:121-132），覆盖产物两种落盘形态。

**发现的问题**：
- C-3（`read_bytes` 大 payload 开销）、C-4（produced_at 空串）、C-5（只读边界历史洞已修）——均 P2，无 P0/P1 级 bug。测试 23 例覆盖命中/淘汰/只读/路径安全。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/02-crosscutting.md` §7 | opt-in 默认不生效、65× 净亏、双重复验、origin=cache 已写明 | 无需更新 |
| `s0/02-crosscutting.md` F5/F6 | 与实现一致 | 无需更新 |
| `15-operations.md` §3（存储增长>1.5×判缓存泄漏） | 与 `evictions` 观测字段呼应 | 无需更新 |
| `s0/02-crosscutting.md` §7 | 未提 `produced_at` 字段用途 | 可补一句说明 C-4（该字段预留/空串），或建议代码删除——低优先 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增可缓存工位 | ✅ 配置化 | 在执行器 `cache_stations` 列表加名字即可，cache.py 无需改；但要确认该工位 payload 能否复原内存对象 |
| 开启节点级缓存 | ✅ 支持 | `Cache(node_enabled=True)` + `put_node/get_node`（:104-118），但 s0/02 §7 实测 65× 净亏，默认关是对的 |
| 新缓存键类型 | ❌ 需改代码 | 当前只有 call/node 两类（docstring:3-4），不允许自定义键——刻意收紧 |
| 更大 payload 命中 | ⚠️ 需改哈希策略 | C-3：`read_bytes` 全读内存，大对象应流式哈希或信任 Ref.digest；低优先 |
| 缓存命中不影响指纹 | ✅ 支持 | `evictions` 不进指纹，缓存键/命中状态都不进任何产物指纹——纯性能优化 |

---

## C1–C12 相关项判定

本模块为缓存设施，不含业务计算：

- **C7 24/7 缺口**：中性——缓存产物版本由 call_digest 绑定，数据末端变化会改 digest 从而使缓存失效；缓存本身不读时钟
- **C2 资金费按事件**：中性——call_digest 含 impl@version/params/policy，资金费口径变化会进 digest 使缓存失效，不会复用旧口径产物
- 其余条目：不适用（缓存层不含业务语义）

## R1–R7 影响

- **R1 真数据跑通**：✅ 支撑——默认关闭不影响真跑正确性；开启后双重复验保证复用产物可信
- **R2 结论可信**：✅ 强支撑——命中验 payload + origin=cache 让"跑了几次/用了什么产物"可审计，不会把缓存产物当新算的
- **R6 迭代复用**：✅ 强支撑——缓存是迭代复用的性能优化，call_digest 绑定口径确保不复用过期产物
- **R7 fail-closed**：✅ 强支撑——篡改/删除/穿越/只读越权全部拒或 miss，不返回空产物

---

## 结论

**本模块（132 行）职责单一、边界清晰，无 P0/P1 级 bug，可确认通过。** 用户必查点逐条实测成立：①opt-in 默认空 cache_stations——实测 `node_enabled=False`、执行器 `cache_stations=()` 默认空；②命中验 payload——篡改/删除/路径穿越三种情况全部视为 miss（F5），执行器再验反序列化结果双重复验；③trace origin=cache——命中强制标 origin=cache；④落盘与计数不跳过（G11）——命中只跳计算，落盘/计数/trace 照走（F6）。C-3/C-4/C-5 三条 P2 均为性能/完整性/历史修复，不影响生产可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 8 个模块 `trace.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
