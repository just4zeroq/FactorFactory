# 模块审查 70 · `app/cli/factor_db.py`

> 审查日期：2026-11-18
> 依据：源码 66 行全文通读；依赖 `catalog.py`（retire/get/entries/list）、`contract.py`（mutation WRITES_FACTS / data_keys）、`errors.py`（E1004/E3006）；设计 `09-app-storage.md` §2.3/L89/L192；测试 `test_v5_catalog_ops.py:178-188,388-412`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`factor_db` 是因子库的**写侧管理**（下架），与只读 `factors` 分开，回答四件事：
1. **无动作就是只读统计**：不猜"你想干嘛"，也不默认打印全库——返回 entries/active/retired/path + hint（`:32-40`）。
2. **下架是追加一条记录，不是删行**：库是资产目录，删行就说不清"曾进过库、何时因何下架"；`status` 由最后一条生效，历史可回放（docstring `:6-8`；`catalog.retire:181-201`）。
3. **写库要显式确认**：mutation=`WRITES_FACTS`（非账本），但改现行状态风险高，故自守 `--yes`（`:42-49`）。**刻意不走 `require_confirmation`**——那条按 `WRITES_LEDGER` 判定，而这里写的是 catalog；谎报 mutation 会让 `--yes` 语义失准（`:43-44`）。
4. **不物理删除**：要回收空间走 `storage.gc`（引用计数），不在业务命令里 `unlink`（docstring `:10-11`）。

**加密特化**：本模块不碰口径/时间轴/数据——因子下架是资产目录的状态变更，加密语义已在入库条目里固化。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 先 promote 2 条（CID1/CID3），再调用 `cmd_factor_db`。

### CLI help（真实退出码）

```
$ venv/Scripts/python.exe -m app.cli factor_db --help
usage: flow factor_db [-h] [--retire RETIRE] [--note NOTE] [--yes]
                      [--format {table,json,ndjson}]
exit=0
```

### 输入：`cmd_factor_db(args)`

```
args.retire = None              # 无动作 → 只读统计
args.retire = "sha256:aaaa…"    # 下架该 factor_id
args.note   = "decayed"         # 写进库的下架原因
args.yes    = True / False
```

### 输出：`data`（真实打印）

**无动作（只读统计）**：
```json
{"entries":2,"active":2,"retired":0,
 "path":"C:\\…\\catalog\\factors.jsonl","hint":"用 --retire <factor_id> 下架一个因子"}
```

**下架成功**：
```json
{"entries":2,"retired_now":"sha256:aaaa…","status":"retired","note":"decayed",
 "path":"C:\\…\\catalog\\factors.jsonl"}
```
**append-only 验证（真实）**：下架后 `catalog 文件行数 = 3`（原 2 条 active + 1 条 retired 追加行）；`catalog.get(CID1).status = retired`；`catalog.list(status="active")` 只剩 CID3。

### 失败输入 → 错误输出（fail-closed，全部实测）

| 非法输入 | 错误码 | exit | 真实 message |
|---|---|---|---|
| `--retire <id>` 缺 `--yes` | E1004 | 2 | `factor_db --retire 会改因子库状态，必须显式 --yes` |
| 下架不在库的 id | E3006 | 1 | `因子不在库中: sha256:ffff…` |
| 重复下架同一 id | 无错 | 0 | `catalog.retire:188-189` 已 retired 直接返回 existing（幂等，不追加第二行） |

> E3006 属 E3xxx（execution，exit 1）；此处语义是"违反一次性事件语义"借用于"因子不在库"，见 D2 D-2。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| D-1 | **文档 09 §2.4 列出的子命令远多于实现**：文档写 `list/show/corr/compare/export/combine/rm`（`09:89`），实现只有 `--retire` + 无动作统计。 | **P2（文档严重落后，见 D5）** | `09-app-storage.md:89,192`；本模块 `:30-40` | 不影响"真挖因子"（下架能力齐），但文档会把研究员引向不存在的子命令。 |
| D-2 | **E3006 错误码语义借用**：E3006 定义是"违反一次性事件语义"（`errors.py:119`），用于"因子不在库"名不副实——更像 E1008（缺/未找到）或 E3003（产物空）。 | P2（语义） | `factor_db.py:51-55`；`errors.py:119` | 低优先；码值不影响 exit（都是可修输入错误），但排查时误导。需确认意图。 |
| D-3 | **data_keys 声明不完整**：`contract.py:108` 声明 `("entries",)`，实测无动作返回 `entries/active/retired/path/hint`，下架返回 `entries/retired_now/status/note/path`。 | P2（元数据） | `contract.py:108`；本模块 `:34-40,59-65` 实测 | 并入 U11 元数据对账。 |
| D-4 | **重复下架 note 不生效**：第二次 `--retire <已 retired>` 直接返回 existing（`:188-189`），新的 `--note` 被丢弃，无提示。 | P2（边界，需确认意图） | `catalog.py:188-189` | 幂等优先于 note 更新是合理取舍；若要支持"补记原因"需另开接口，非本模块单独能定。 |

**多了什么**：无冗余。`--yes` 自守而非误用 `require_confirmation`（`:42-44`）是刻意的正确设计，与 promote 的 P-1 形成对照——本模块的 mutation 声明诚实。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 软删除 / tombstone（追加 status=retired 记录） | 采纳 | 资产目录可回放"何时因何下架"，历史行不动（`:6-8`；`catalog.retire`） |
| 业务命令里 `unlink` 物理删 | 拒绝 | 会丢历史且绕开引用计数；空间回收归 `storage.gc`（docstring `:10-11`） |
| 把写库谎报成 `writes_ledger` 以复用确认框架 | 拒绝 | 本模块显式避开（`:42-44`），`--yes` 语义与 mutation 等级保持一致 |
| 无动作时打印全库 | 拒绝 | 不猜用户意图，给统计 + hint（`:32-40`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **append-only 下架**：`catalog.retire` 复制原条目、改 status/note/updated_at、O_APPEND 写新行（`:190-200`）；实测文件 3 行、get 为 retired、active 列表不含它。
2. **确认守卫正确**：`--yes` 缺失 → E1004 exit2（实测）；且**没用** `require_confirmation`，mutation 声明不失真（`:42-45`）。
3. **目标存在性校验**：`catalog.get(target) is None` → E3006（`:51-55`），实测。
4. **无动作只读**：不写任何文件，返回统计 + hint（实测 entries/active/retired）。
5. **不改历史行**：status 由 `entries()` 折叠时"后写覆盖"生效（`catalog.py:66-81`），原 active 行仍在。

**发现的问题**：D-1 文档严重落后、D-2 E3006 语义、D-3 data_keys、D-4 重复 note——均为 P2。**无 P0/P1 级代码 bug**；下架主路径 + 两条失败路径实测符合 D1。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md:89` | factor_db 列 `list/show/corr/compare/export/combine/rm` | 改为 `--retire` + 无动作统计；`list` 归 `factors` 命令。 |
| `09-app-storage.md:192` | data_keys=`rows[]`/`ref` | 改为无动作 `entries/active/retired/path/hint`、下架 `entries/retired_now/status/note/path`。 |
| `contract.py:108` | data_keys=`("entries",)` | 按上行补全。 |
| `09-app-storage.md:108`（mutation 四级） | 写库需确认 | 本模块 WRITES_FACTS 但自守 `--yes` 的做法值得在文档里作为"mutation 诚实"范例写明。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增管理动作（corr/compare/export） | ❌ 不支持 | 09 文档写了但实现缺；若要，需新参数 + mutation 声明 + 测试，非加开关那么简单。 |
| 下架原因结构化（如 reason code） | ⚠️ 半配置化 | 现为自由文本 `note`（`:57`）；要结构化需改库记录 schema（catalog.retire）。 |
| 恢复已下架因子（reactivate） | ❌ 不支持 | 只能追加 retired，无反向 status；要做需扩 `_STATUSES` + 新命令。 |
| 空间回收（GC） | ✅ 分离支持 | 由 `storage.gc` 按引用计数处理，不占本命令（docstring `:10-11`）。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为资产目录状态管理，不参与任何加密事实判定。
- **R6 迭代复用（核心相关）**：✅ 下架让"过期因子"能被显式排除在 `db:*` 先验池外（`resolve_prior_pool` 只取 `status=active`，`factors.py:210`）——闭环的"淘汰"一侧。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ 下架原因留痕，历史可回放。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：中性。
- **R6 迭代复用**：✅ 见上。
- **R7 fail-closed**：✅ `--yes` 守卫 + 目标存在性校验（E3006）；写库不静默。

---

## 结论

**本模块功能正确、mutation 声明诚实，可确认通过。无 P0/P1 级 bug。** append-only 下架 + `--yes` 自守 + 不物理删除三条边界都与 D1 一致，实测主路径与两条失败路径全过。主要差距是 **D-1 文档严重落后**（09 §2.4 写了一堆不存在的子命令），会把研究员引向死路，建议优先补文档；D-2/D-3/D-4 为低优先语义/元数据/边界项。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 71 个模块 `holdout.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ D-2 E3006 语义想当场裁决（换码 vs 保留）→ 告诉我
