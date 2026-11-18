# 模块审查 26 · `storage/catalog.py`

> 审查日期：2026-11-18
> 依据：源码 266 行全文通读；依赖 `layout.py`（Layout 路径）、`contracts/errors`（E1008/E3006）；对照 `orchestration/executor.py:978-1025`（ticket 另一读者）、`app/cli/promote.py`、`app/cli/factor_db.py`、`capability/params.py`（prior_pool `db:` 引用）。设计 `19-supply.md`、`09-app-storage.md`；测试 `test_v5_catalog_ops.py`、`test_v5_storage.py`。判据：plan §0.0/§0.1（R1–R7 / C1–C12）。
> **本轮只读审查，未改代码。** 真实调用脚本见系统临时目录 `fm_review_26_33/probe2_storage.py`（venv Python 实测，输出见下）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Catalog 是跨 run 累积的**因子资产目录**，回答"这个数据根里挖出过哪些因子、当前状态如何"。职责边界：

1. **内容身份**：`factor_id` 由规范化 payload（表达式 + 字段集）派生，同一表达式由 GP/随机/手写先验产生都只有一个条目；改名、换 run 不新开行。
2. **append-only 状态机**：`factors.jsonl` 只追加；retire 追加新行让最后一条生效，不改历史行——与 ledger 同一套真相语义（`catalog.py:8-17`）。
3. **不进指纹**：因子库是资产目录，不是实验构成；加一个因子不得改变任何历史 `plan_digest`/`blueprint_digest`（`:15-16`）——这是它区别于 ledger 的根本约束。
4. **prior 回灌形态**：`expressions()` 产出可直接注入 `prior_candidates` 的 `{factor_id,name,expression,required_fields}`，支撑 R6 `db:<factor_id>` 精确引用（`:106-122`）。
5. **run 产物读取**：`read_ticket()` / `station_of()` 供 promote 与 inspect 共用，只读 `runs/<ticket>/parcels/` 下的工位 payload。

**不变量**：因子身份不可变（只有 status/审计字段允许更新，`:77-80`）；折叠顺序稳定（首次字段 + 末次状态）；坏行不是真相（`:48-50` 与 ledger 同策略）；去重判定与写盘在同一把文件锁内（`:167-178`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 用仓库 `venv/Scripts/python.exe` 调用 `Catalog` / `read_ticket` 实际打印。环境：pandas 3.1.1。临时目录 `C:\Users\lansa\AppData\Local\Temp\fm_review_26_33\data2`。

### 例 1：add 折叠 + retire 历史保留（实测输出）

```
=== E catalog 折叠/retire/历史保留 ===
{'首次新入库': True, '重复再入库': False, '条目当前status': 'retired',
 '文件物理行数(append-only)': 2, '历史两行status': ['active', 'retired'],
 'expressions活跃': 0}
```

输入：
```
e1 = {"factor_id":"f1","name":"alpha",
      "expression":{"op":"ts_mean","col":"close","n":5},
      "required_fields":["close"],"status":"active"}
c.add(e1) → (record, True)      # 新入库
c.add(e1) → (existing, False)   # 同 id 同 status：不写盘（幂等）
c.retire("f1", at="2026-06-01T00:00Z", note="decayed")
```

逐格解读：
- **重复 add 返回 False**：`add()` 在锁内重读 + `get(fid)` 已存在且 status 相同即返回、不写盘（`:139-142,170-173`）——promote 可重复执行，重跑不撑大库。
- **历史两行 status=['active','retired']**：retire 用 `os.O_APPEND` 追加一条 status=retired 的新行（`:196-200`），不改历史行；`entries()` 按 factor_id 折叠时"后写状态覆盖先写"（`:66-81`），当前状态 = retired。
- **expressions 活跃=0**：`expressions(status="active")` 过滤后空——retired 因子不回灌先验池，符合 R6 语义。
- 物理文件 `catalog/factors.jsonl` 路径由 `layout.catalog` 提供（实测 `.../data2/catalog`）。

### 例 2：ticket 双读者取首/取末分叉（P1，实测复现）

构造：同一 run `20990101T000000Z-deadbeef` 下，Search 工位两个目录 `04-Search-aaaaaaaa`（payload marker=FIRST）与 `04-Search-bbbbbbbb`（marker=LAST），各有 `payload.json` 与 `parcel.json`。

```
=== F ticket 双读者取首/取末分叉(P1) ===
{'catalog.read_ticket 取': {'marker': 'FIRST'},
 'catalog保留目录数': 2,
 'executor glob候选顺序': ['04-Search-aaaaaaaa', '04-Search-bbbbbbbb'],
 'executor found[-1]取': '04-Search-bbbbbbbb',
 '两者是否同一产物': False}
```

- `catalog.read_ticket`（`:240-245`）用 `sorted(parcels_dir.iterdir())` + `if station in payloads: continue` → **保留第一个**（FIRST），语义理由写在 docstring：append_only 工位（Verdict/Search）按首次结论，不能被后续覆盖（`:227-228`）。
- `executor.load_committed_parcel`（`executor.py:988,995`）用 `sorted(...glob...)` + `record_path = found[-1]` → **取最后一个**（LAST）。
- `station_of` 实测 `['Boundary','Search','Certify']`（`:215-220` 靠目录名后缀精确匹配，不靠位置猜）——两个读者对"哪个目录是 Search"的判断一致，但对"同工位多目录取哪个"相反。**两者是否同一产物=False**：promote 走 catalog 取首、certify/replay 走 executor 取末，存在认证到不同因子的风险（详见 D4）。

### 失败输入 → 错误输出（实测）

| 非法输入 | 输出 |
|---|---|
| `Catalog.add({"expression":{...}})` 缺 factor_id | `E1008` "因子条目缺 factor_id" |
| `add` 有 factor_id 但 expression 非 Mapping | `E1008` "因子 X 缺 expression" |
| `add` status 非 active/retired | `E1008` "未知 status 'xxx'" |
| `retire` 不在库的 id | `E3006` "因子不在库中" |
| `list(status="xxx")` | `E1008` "未知 status 'xxx'" |
| `read_ticket` 不存在的 ticket | `E1008` "执行记录不存在" |
| `factors.jsonl` 中坏行（非 JSON） | 静默跳过（`_read_lines:48-50`，半行不是真相） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| C-1 | **ticket 双读者取首/取末分叉**：`read_ticket` 保留同工位首个 payload，`load_committed_parcel` 取末个 parcel。append_only 工位重复执行后，promote（经 catalog）与 certify/replay（经 executor）可能读取不同因子。 | **P1** | `catalog.py:240-245`（`if station in payloads: continue` 取首）vs `executor.py:988,995`（`found[-1]` 取末）；实测 `两者是否同一产物=False` | **已确认**：这是 findings 登记的 U3-P1-2（同 U5-P1）。需裁决"同工位多目录"应取首还是取末，然后统一两处（并先改文档 09-app-storage 的 mutation 语义）。本模块只读审查不改。 |
| C-2 | **身份生成不在此模块**：`factor_id` 由调用方（promote）算好传入，`add()` 不校验 `factor_id` 是否与 expression 内容相符。若调用方传了与表达式不匹配的 id，库会接受。 | P2 | `catalog.py:131-158`（只校验 id 非空 + expression 是 Mapping） | **需确认意图**：信任 promote 的 `candidate_id=factor_id` 契约。属"谁算内容身份"的单一真相问题，但落点在 promote/指纹层，不在 catalog；不单独修。 |
| C-3 | **`get()`/`entries()` 每次全表读盘折叠**：`get` 调 `entries()` 遍历整个 jsonl（`:83-87`），`add` 锁内又 `_read_lines` 一遍（`:171`）。因子库上万条后 O(n) 扫描。 | P2（性能） | `catalog.py:65-87,171` | **需确认**：当前规模（内置 12 + GP 产物）可接受；应补索引（duckdb）后再优化，属增量而非 bug。 |
| C-4 | **retire 不进锁**：`add()` 有 FileLock 去重，但 `retire()` 直接 `os.open(O_APPEND)`（`:196-200`），无锁。并发 retire 与 add 交错时，折叠顺序依赖 OS 追加顺序。 | P2 | `catalog.py:181-201` vs `:167-178` | **低优先**：retire 是人工低频操作；append-only 单行不撕裂，最坏是状态先后顺序不确定，不影响历史行。 |

**多了什么**：无明显冗余。`_STATION_NAMES` 与 `STATION_SPECS` 两份真相由测试守卫（`test_station_names_match_specs`），注释已说明为何不反向 import（`:205-208`）——合理。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Event sourcing / append-only log（本模块 jsonl） | 采纳 | 因子状态变更留全历史，历史结论可解释；与 ledger 同语义（`:13-14`） |
| 内容寻址身份（factor_id = H(payload)） | 采纳 | 同一表达式跨来源去重；改名/换 run 不新开行（`:10-12`）；但身份计算在调用方（见 C-2） |
| 文件锁内去重判定（filelock + O_APPEND） | 采纳 | 两个 promote 并发不各写一行；`add:170` 实测幂等 |
| DuckDB 索引 / 资产表 | **应借鉴（当前缺）** | `get` 全表扫描（C-3）；规模化后需索引，但索引不是真相（同 index.py 纪律） |
| 关系库 UPSERT 原地改状态 | **拒绝** | 会抹掉 retire 历史，违反 append-only；本模块用追加新行折叠（实测 2 行 active→retired） |
| 把因子库纳入 plan 指纹 | **拒绝** | 会让"加因子"使历史结论全部失效（`:15-16`）；这是本模块与 ledger 的关键分界 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **append-only 折叠**：retire 追加新行、不改历史；`entries()` 后写状态覆盖先写、其余字段以首次为准（`:66-81`）。实测物理 2 行、当前 status=retired、历史=['active','retired']。
2. **幂等 add**：同 id 同 status 不写盘（`:139-142`），锁内重扫去重（`:170-173`）。实测重复 add 返回 False。
3. **身份字段不可变**：折叠时只允许 status/updated_at/note/evidence_grade 更新，expression/required_fields 不改（`:77-80`）。
4. **不进指纹**：目录写在 `catalog/factors.jsonl`，不参与任何 plan/blueprint 摘要（设计约束 `:15-16`；promote 只写库不改 plan）。
5. **坏行容错**：`_read_lines` 跳过非 JSON 行（`:48-50`），与 ledger `_scan_keys` 同策略——半行不是真相，不因一条坏数据整体崩。
6. **paths 不自己拼字面量**：`_path` 走 `layout.catalog`（`:35-36`），符合 AC31 路径守卫。
7. **prior 回灌带 factor_id**：`expressions()` 带出 id 支撑 `db:` 精确引用，避免名字重名歧义（`:106-122`，注释理由充分）。

**发现的问题**：
- **P1**：C-1 ticket 双读者取首/取末分叉。本模块 `read_ticket` 取首本身符合其 docstring 语义；问题在于与 executor `found[-1]` 不一致。修复落点可在任一侧统一，需裁决（见 D2 C-1）。
- **无 P0**。C-2/C-3/C-4 均为 P2 或需确认意图。

**测试覆盖**：`test_v5_catalog_ops.py` 覆盖 promote 幂等、skip rejected、out_of_sample gate、retire 保历史、factor_db 动作；但**未有测试钉住"两个 ticket 读者取同一产物"**——P1 逃逸。建议补一条跨模块一致性测试。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §3（ticket mutation 语义） | 描述 append_only 工位"按首次结论" | 补一句明确"所有 ticket 读者统一取首或取末"，为 C-1 裁决提供契约依据后再改代码 |
| `19-supply.md` / 因子库章节 | 描述 factor_id 内容身份、append-only | 与实现一致，无需更新；若采纳 C-2 则补"身份由 promote 计算并传入" |
| `s0/06-upstream.md` | catalog 不在其覆盖范围 | 无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新状态（如 `under_review`） | ⚠️ 半配置化 | `_STATUSES` 加值（`:32`），但 `entries` 折叠字段白名单（`:78`）、`list`/`add` 校验都要跟上；非纯配置 |
| 因子带多版本表达式 | ❌ 不支持 | 折叠时 expression 以首次为准、不可变（`:77`）；要支持需改身份模型（契约变更） |
| 因子库加索引（规模化） | ❌ 需改代码 | `get` 全表扫描（C-3）；可借 index.py 的 duckdb 面，但索引不是真相 |
| 跨 run 去重 / 先验回灌 | ✅ 支持 | `expressions()` + `db:` 引用已通（R6）；retired 不回灌 |
| 统一 ticket 读者语义 | ❌ 当前分叉 | C-1，需裁决 + 改 catalog 或 executor |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不涉年化）。
- **C2 资金费按事件**：不适用。
- **C3 spot/futures 维度**：不适用（因子库不按市场分区）。
- **C4 合约四元组**：⚠️ 因子 `required_fields` 是字段集，不含合约身份；期限结构因子若入库，身份维度由 expression 携带——不是本模块职责。
- **C5 USDT/USD 溢价**：不适用。
- **C6 上市/下架**：不适用。
- **C7 24/7 缺口**：不适用。
- **C8 开放列集**：✅ `required_fields` 是开放集合（`:120`），不写死五列。
- **C9 as-of 只 ffill**：不适用。
- **C10 判据不放松**：不适用。
- **C11 流动性分层**：不适用。
- **C12 embargo 按观测**：不适用。

## R1–R7 影响

- **R1 真数据跑通**：✅ 中性（目录读写已实测，真跑中 promote 用通）。
- **R2 结论可信**：⚠️ 负向（C-1 分叉若坐实，promote 与 certify 可能认证不同因子，影响单因子可归因的一致性）。
- **R3 因子零代码扩展**：✅ 正向（`expressions()` 支撑 `db:` 回灌，R6 闭环。
- **R5 单因子可归因**：⚠️ 同 C-1，归因链起点（哪个因子被认证）可能分叉。
- **R6 迭代复用**：✅ 强支撑（跨 run 资产累积 + 先验回灌核心就在此模块）。
- **R7 fail-closed**：✅ 强支撑状态校验（缺 id/expression、未知 status、坏行跳过全明确）。

---

## 结论

**本模块设计纪律高，无 P0；但承载一条已确认 P1（ticket 双读者取首/取末分叉，与 executor 不一致）的"catalog 这一侧"。** 本模块 `read_ticket` 取首符合其自身 docstring 语义（append_only 工位按首次结论），问题本质是两个 ticket Reader 的语义未统一——需裁决后改 catalog 或 executor 一侧，并先补文档契约（D5）。另 C-2（身份由调用方传入不校验）、C-3（全表扫描）、C-4（retire 不进锁）均为 P2/需确认意图，不单独修。**"生产可用=能真挖因子"角度**：因子库回灌闭环成立（R6 ✅），但 C-1 不修则 R2/R5 归因一致性存疑，建议在 U3/U5 合并裁决后一并闭合。
