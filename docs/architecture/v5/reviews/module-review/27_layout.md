# 模块审查 27 · `v5/storage/layout.py`

> 审查日期：2026-11-18
> 依据：源码 200 行全文通读；被所有 storage/upstream/runtime/cli 模块依赖（路径唯一真相）。对照 `09-app-storage.md` §3.2/§3.4、`s0/03 §1`（逻辑分区映射）、`tests/v5/test_v5_storage.py:318-345`（AST 守卫 / 九区暴露）。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用脚本见 `fm_review_26_33/probe1_store.py`（venv Python 实测）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Layout 是**物理路径的唯一定义**（`:1-5`），回答"数据根下每个逻辑分区落在哪个目录"。职责边界：

1. **纯路径构造**：全部是 `@property`，不读环境变量、不读配置、不产生任何 IO；数据根由 `runtime.store.resolve_data_root` 解析后传入（`:12-13,44-53`）。
2. **单一真相守卫**：任何模块不得自己拼 `facts`/`parcels` 等字面量（AC31）。`ZONE_NAMES` / `FACT_KINDS` 是被测试 AST 守卫引用的常量集（`:22-27,29-37`）。
3. **逻辑→物理映射**：Charter/Depot/Ticket/Scratch 四权限分区到物理目录的映射只在这里定义一次（`:180-201`）。
4. **FACT_KINDS 白名单唯一来源**：`facts/` 下对象类别（含 `parcel_data`）必须一处定义，供 index 重建、gc 引用计数、`fact_kind()` 落盘共用——历史事故：index 曾手抄 5 类漏掉 `parcel_data`，导致该类永不进索引、GC 与 index 对"谁算活对象"分叉（`:29-37` 注释）。
5. **磁盘原子写支撑**：`tmp_file` / `sidecar` 提供同盘暂存与 schema sidecar 命名（`:171-177`），供 objects 的 atomic write 使用。

**术语澄清**：「四区」是权限分区（runtime/store 的 ReadOnly/Ticket/Depot），「九区」是文档对物理目录的分组说法；`ZONE_NAMES` 实际 11 个（多 `plans`/`schemas`/`catalog`）——两者不是同一概念，别拿数字对照（`:7-10`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> venv Python 实测。临时数据根 `C:\Users\lansa\AppData\Local\Temp\fm_review_26_33\data1`。

### 例 1：11 个区 + 关键落点（实测输出）

```
=== 1a Layout 区路径 ===
{'zones': ['supply', 'facts', 'parcels', 'runs', 'db', 'cache', 'tmp', 'baselines', 'plans', 'schemas', 'catalog'],
 'facts': '...\\data1\\facts',
 'catalog': '...\\data1\\catalog',
 'ledger': '...\\data1\\db\\ledger.jsonl',
 'index_db': '...\\data1\\db\\meta.duckdb'}
```

- `ledger = <root>/db/ledger.jsonl`（`:113-115`），`index_db = <root>/db/meta.duckdb`（`:118-120`）——一个真相（jsonl）+ 一个可重建索引（duckdb），符合 U3 D1。
- 11 个区名与 `ZONE_NAMES` 逐项一致（实测 zones 列表）。

### 例 2：FACT_KINDS 含 parcel_data（实测）

```
=== 1b FACT_KINDS 含 parcel_data ===
('panels', 'labels', 'exposures', 'ledgers', 'reports', 'parcel_data')
=== 1c fact_kind('parcel_data') ===
...\\data1\\facts\\parcel_data
=== 1d fact_kind('bogus') -> ValueError ===
ValueError: 未知对象类别: 'bogus'
```

- 6 类白名单（`:35-37`），`parcel_data` 在列——推翻了旧基线"FACT_KINDS 漏 parcel_data"的假设（findings §2 已登记该基线过期）。
- `fact_kind('bogus')` 直接 `ValueError`（`:137-138`），fail-closed。

### 失败输入 → 错误输出（实测）

| 非法输入 | 输出 |
|---|---|
| `Layout(root=<str>)` | `TypeError: Layout.root 必须是 Path，得到 str`（`:50-52`，实测于 probe3 触发） |
| `fact_kind("bogus")` | `ValueError: 未知对象类别: 'bogus'` |
| `register("bogus")` | `ValueError: 未知登记账本: 'bogus'`（`:126-130`） |
| 其他模块拼 `root/"facts"` 字面量 | `test_zone_literals_defined_only_in_layout` AST 守卫报错（`:22-23` 注释） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| L-1 | **方案 B 数据文件路径不在此模块**：`<root>/<dataset>/<exchange>/<market>/<SYMBOL>[-tf].parquet` 由 `upstream/store.dataset_file` 构造（见模块 32）。本模块的 `root` 是运行时数据根（含 facts/db/runs 等九区），与供给根是同一 root 下不同子树的两个概念。 | P2（职责边界说明） | `layout.py` 全文无 dataset/exchange/market 路径；`store.py:166-209` | **已确认符合设计**：Layout 管运行时物理分区，store 管供给事实路径；两者共享 root 但不同子树。文档需说清，避免误以为 layout 应含方案 B 路径。 |
| L-2 | **「九区」名不副实**：`ZONE_NAMES` 有 11 个，文档叫"九区"。 | P2（术语） | `layout.py:7-10,24-27`；docstring 已自澄清 | **已确认是文档分组说法 vs 物理清单的差异**，代码注释已说明，无需改代码；D5 建议文档统一术语。 |
| L-3 | **`log_file` 延迟 import `facilities.log.log_path`**（`:168-169`）：破坏"纯路径构造"的纯粹性（虽无 IO，但引入跨层依赖）。 | P2 | `layout.py:161-169` | **需确认意图**：注释解释了命名规则复用避免两处写前缀处理，属合理权衡；storage→facilities 是向下依赖，方向可接受。 |
| L-4 | **`parcel_dir` 的 digest 去前缀**（`split(":",1)[-1]`，`:154`）：若 digest 无前缀也不会报错，只是原样用。 | P2（边界） | `layout.py:152-155` | **低优先**：调用方保证传 `sha256:` 前缀；无前缀是内部误用，不影响路径唯一性。 |

**多了什么**：无冗余。`charter_paths`/`scratch_paths` 等逻辑映射是跨层契约所需。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 路径常量集中 + AST 守卫（本模块） | 采纳 | 杜绝各模块手拼字面量导致目录漂移；`test_zone_literals_defined_only_in_layout` 实测守卫 |
| frozen dataclass 存 root | 采纳 | 不可变、防运行中改根；`__post_init__` 校验 Path 类型（`:50-52`） |
| 单一白名单驱动多消费者（FACT_KINDS→index/gc/fact_kind） | 采纳 | 修复历史"index 漏 parcel_data"分叉（`:29-37`），三处引用同一常量，测试 `test_fact_kind_whitelist_has_single_source` 钉住 |
| 逻辑分区与物理路径分离 | 采纳 | 权限分区（四区）与物理分区（九区/11 区）解耦，映射只在此定义（`:180-201`） |
| 运行时计算路径（拼字符串散落各模块） | **拒绝** | V4 缺陷：目录名散落导致改名即全仓搜索；AC31 明确禁止 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **纯路径构造、无 IO**：全 `@property`（`:55-201`），root 外部注入（`:12-13`）——可在无数据根的机器上安全实例化。
2. **路径唯一真相 + AST 守卫**：ZONE_NAMES/FACT_KINDS 集中定义（`:24-37`），`all_zone_literals()`（`:39-41`）供测试引用，避免测试自己抄字面量。
3. **FACT_KINDS 含 parcel_data 且单一来源**：实测 6 类（`:35-37`）；index `_rebuild_object_rows`、gc `_KINDS`、`fact_kind` 均引用它（见模块 29/31），历史分叉已闭合。
4. **白名单 fail-closed**：`fact_kind`/`register` 对未知名直接 ValueError（实测）。
5. **sidecar 命名集中**：`sidecar(name) = name+".schema.json"`（`:171-173`），objects 与 index 共用，避免两处拼后缀分叉。
6. **atomic write 支撑**：`tmp_file` 同盘暂存（`:175-177`），配合 objects 的 `os.replace`；`log_file` 用 call_digest 命名打破"指纹←→日志路径"循环（`:161-169`）。
7. **Ticket 内部结构隔离**：`run_parcels_dir`（Ticket 侧顺序产物）与 `parcel_dir`（Depot 侧 digest 归置）明确不是同一处（`:144-155` 注释）——catalog.read_ticket 只读前者，避免误读 depot 侧。

**发现的问题**：无 P0/P1 级代码 bug。L-1~L-4 均为职责/术语/边界说明或 P2。

**测试覆盖**：`test_v5_storage.py:318-345` 覆盖 AST 守卫、九区暴露、index_db 落点；`test_fact_kind_whitelist_has_single_source`（`:174-194`）钉住三处同源。充分。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §3.2/§3.4 | "九区"分组说法 | 补一句"物理区实际 11 个（ZONE_NAMES），九区是文档分组口径"，消除名不副实（L-2） |
| `09-app-storage.md` §3 | 未明确 Layout 与方案 B 供给路径的关系 | 补"Layout 管运行时九区；`<root>/<dataset>/...` 供给路径由 store 管，共享 root 不同子树"（L-1） |
| `s0/03 §1` 逻辑分区映射 | 与实现（charter_paths/depot_*/scratch_paths）一致 | 无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增物理区 | ❌ 需改代码 + 测试 | ZONE_NAMES 加值 + property + 更新 AST 守卫测试的期望集；非配置化（设计如此，防漂移） |
| 新增对象类别（如 `clearing_data`） | ⚠️ 半配置化 | FACT_KINDS 加一处即可，index/gc/fact_kind 自动跟上（单一来源）；但测试 `test_fact_kind_whitelist_has_single_source` 需同步预期 |
| 改数据根解析策略 | ✅ 支持（外部注入） | root 由 runtime.store 解析传入，Layout 不关心来源 |
| 新增逻辑分区映射（如 W5 区） | ❌ 需改代码 | charter_paths 等是硬编码 property 元组 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（纯路径模块）。
- **C2 资金费按事件**：不适用。
- **C3 spot/futures 维度**：⚠️ 本模块不编码市场维度；方案 B 的 `<market>` 段在 store 构造（见模块 32）。
- **C4 合约四元组**：不适用。
- **C5–C7**：不适用。
- **C8 开放列集**：不适用（路径模块不涉列）。
- **C9–C12**：不适用。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（路径唯一真相是八工位数据不串区的前提，实测 11 区正确）。
- **R2 结论可信**：✅ 正向（FACT_KINDS 单一来源消除 index/gc 分叉，防对象漏索引导致的一致性误判）。
- **R3 因子零代码扩展**：⚠️ 中性偏正（新对象类别需改 FACT_KINDS，但一处即可；不阻碍加因子）。
- **R7 fail-closed**：✅ 强支撑（白名单 + AST 守卫 + 未知名 ValueError）。

---

## 结论

**本模块代码质量高，无 P0/P1，可确认通过。** 它是 U3 的"地基中的地基"：11 个物理区、FACT_KINDS 6 类（含 parcel_data）、逻辑分区映射、atomic-write 支撑全部落地且实测正确；历史"index 漏 parcel_data"分叉已通过单一来源 + 测试守卫闭合。三处差距均为文档/术语/职责说明（L-1 供给路径不在此、L-2 九区名不副实、L-3 延迟 import 权衡），不影响"能真挖因子"。**建议**：D5 两处文档补句即可，代码不动。
