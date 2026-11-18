# 模块审查 69 · `v5/app/cli/factors.py`

> 审查日期：2026-11-18
> 依据：源码 62 行全文通读；依赖 `catalog.py`（entries/list/count/path）、`contract.py`（data_keys）、`errors.py`；设计 `09-app-storage.md` §2.4/L89/L192；测试 `test_v5_catalog_ops.py:158-176`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py`（系统临时目录）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`factors` 是因子库的**只读查询面**，回答三件事：
1. **展示当前条目**：按 `factor_id` 折叠后的状态（`catalog.entries` 的"后写覆盖先写"语义）——默认只列 `active`，`--status retired` 看下架记录，`--status all` 看全部（`:43-55`）。
2. **表达式与统计原样带出**：`_row` 把 expression/statistics/evidence_grade/origin 投影成可直接核对的形态，不做单位换算、不重算指标（`:19-37`）。
3. **只读零副作用**：不写库、不写账本；mutation=`READ_ONLY`（`contract.py:102`）；异常即失败（未知 status → E1008，`:50-54`）。

**加密特化**：本模块不读盘、不碰口径——因子清单是"资产目录查询"，加密语义已固化在入库条目里；查询层只负责忠实投影。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_a.py` 先经 promote 写入 2 条因子（CID1 `in_sample`、CID3 `out_of_sample`），再调用 `cmd_factors`。

### CLI help（真实退出码）

```
$ venv/Scripts/python.exe -m v5.app.cli factors --help
usage: flow factors [-h] [--status {active,retired,all}] [--limit LIMIT]
                    [--format {table,json,ndjson}]
exit=0
```

### 输入：`cmd_factors(args)`

```
args.status = "active"   # active / retired / all
args.limit  = None       # None=不限；>=0 截断
```

### 输出：`data`（真实打印，节选）

```json
{"factors":[
  {"factor_id":"sha256:aaaa…","name":"gp_symbolic:aaaaaaaa","status":"active",
   "expression":{"args":[{"field":"close"},{"const":2}],"op":"div"},
   "required_fields":["close"],"operators":["div"],"generators":["gp_symbolic@1"],
   "evidence_grade":"in_sample",
   "origin":{"family":"f1","plan":"p1","ticket_id":"20260101T000000Z-abcdef12"},
   "promoted_at":"2026-11-18T06:26:26.321976Z",
   "statistics":{"dsr":0.97,"hac_t":3.1,"n_trials":3,"family":"f1"}},
  {"factor_id":"sha256:cccc…","evidence_grade":"out_of_sample", …}],
 "count":2,"catalog_total":2,
 "path":"C:\\Users\\lansa\\AppData\\Local\\Temp\\fm_review_A_xxx\\catalog\\factors.jsonl"}
```
要点：`statistics` 只投影 6 个固定键（`:34-35`）；`origin` 逐字段带出（`:31`）；`path` 告诉用户查的是哪个库（`:60`）。

**三命令对照（真实）**：
```
factors(status=active) → count=2, catalog_total=2
factors(status=bogus)  → E1008, exit 2, "未知 --status 'bogus'"
factors(空库)          → ok=True, count=0（test_v5_catalog_ops.py:172-175）
```

### 失败输入 → 错误输出

| 非法输入 | 错误码 | exit | 真实 message |
|---|---|---|---|
| `--status bogus` | E1008 | 2 | `未知 --status 'bogus'`（`factors.py:51-54`） |
| `--limit -1`（负数） | 无错 | 0 | `limit>=0` 才截断（`:47,55`），负数被忽略=不截断——见 D2 F-3 |

> 未知 status 走 E1008 而非静默返回空，符合 P2"不猜、不返回上次配置"。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| F-1 | **data_keys 声明不完整**：`contract.py:102` 声明 `("factors","count")`，实测还返回 `catalog_total`/`path`。查询命令多带元信息无害，但元数据对账失真（同 promote P-2）。 | P2（元数据） | `contract.py:102`；`factors.py:56-61` 实测 | 并入 U11 元数据对账；不改查询行为。 |
| F-2 | **文档 09 §2.4 把查库写成 `factor_db` 的子命令** `list/show/corr/compare/...`（`09:89`），而实现拆成独立只读命令 `factors` + 写侧 `factor_db --retire`；两者命名与职责划分文档未跟进。 | P2（文档，见 D5） | `09-app-storage.md:89,192`；本模块 + 模块 70 | 需更新文档命令面表。 |
| F-3 | **`--limit` 负值语义模糊**：`limit>=0` 才截断（`:47,55`），负数被静默当作"不限"。argparse `type=int` 不挡负数。 | P2（边界，需确认意图） | `factors.py:47,55` | 低优先；要么文档说明"负数=不限"，要么改 E1004。 |
| F-4 | **`statistics` 投影白名单写死 6 键**（`:34-35`）：新增统计量（如 PBO/期限结构因子统计）不会自动展示。 | P2（扩展性，见 D6） | `factors.py:33-36` | 半配置化；加字段需改 `_row`，但查询层本就该固定视图，不算 bug。 |

**多了什么**：无冗余。`catalog_total` + `path` 是合理的"查的是哪个库、库里共多少"提示，非越权。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 只读视图（read-only view）与写侧命令分离 | 采纳 | 查询永远安全，写库需 `--yes`（模块 70 docstring）；mutation 等级不同不混在一个命令里 |
| 投影（projection）固定字段、原值透出 | 采纳 | `_row` 只投影不改值（`:19-37`），符合 09 §2.5"渲染/查询不得换算" |
| 在查询命令里重算指标/拼接口径 | 拒绝 | 会让"库里记的"和"看到的"分叉；本模块只读 `statistics` 原样带出 |
| Web/图形化因子浏览器交付 | 拒绝 | plan §0.1 D3 已定不交付 Web；CLI 纯壳 + 三格式渲染足够 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **只读零副作用**：无 Catalog 写调用（`:40-61`），mutation=`READ_ONLY`。
2. **折叠状态正确**：`--status all` 走 `catalog.entries()`（已按 `factor_id` 折叠、后写状态覆盖），`active/retired` 走 `catalog.list(status=...)`（`:45-55`）。
3. **投影不改值**：`expression`/`statistics`/`origin` 都是 dict 拷贝或引用透出，无单位换算（`:19-37`）。
4. **未知 status fail-closed**：E1008（`:50-54`），实测。
5. **空库不是错误**：`count=0, ok=True`（实测 + 测试 `:172-175`）——"库还没攒够"是正常状态。

**发现的问题**：F-1/F-2/F-3/F-4 均为 P2 元数据/文档/边界项，**无 P0/P1 级代码 bug**。测试覆盖 2 条（列出、空库）+ 间接经 promote 用例覆盖表达式/统计透出。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md:89` | `factor_db` 列 `list/show/corr/compare/export/combine/rm` 子命令 | 改为：`factors` 只读查询（status/limit）+ `factor_db --retire` 写侧；二者分工写清。 |
| `09-app-storage.md:192` | factor_db data_keys=`rows[]` 或 `ref` | 拆成 `factors` 的 `factors/count/catalog_total/path` 与 `factor_db` 的 `entries/...`。 |
| `contract.py:102` | factors data_keys=`("factors","count")` | 补 `catalog_total/path`；与 promote 的 data_keys 一起做 guards 测试。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增 status（如 `quarantine`） | ❌ 需改代码 | CLI choices + `catalog._STATUSES` + `_row` 三处同步；不是纯配置。 |
| 新增展示统计字段 | ⚠️ 半配置化 | `_row:34-35` 白名单加键即可；不改库结构。 |
| 按表达式/字段集筛选因子 | ❌ 不支持 | `factors` 只按 status 过滤；无 `--field close` 之类。属"单因子归因"手工拼命令的一部分（plan D6 决策项）。 |
| 因子相关性/对比视图（corr/compare） | ❌ 不支持 | 09 §2.4 文档写了但实现缺；若真要，需新命令，不是给 `factors` 加开关。 |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为资产目录只读查询，不参与任何加密事实判定（无年化、无资金费、无市场维度、无口径）。
- **R6 迭代复用（核心相关）**：✅ 因子清单展示是"上轮结论进下轮假设"的可读入口——研究员用 `factors` 确认哪些因子在库，再在 plan 里写 `db:*`/`db:<name>` 作先验。

## R1–R7 影响

- **R1 真数据跑通**：中性。
- **R2 结论可信**：✅ `statistics` 原值透出，不重算不美化。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：✅ 表达式/字段集/operators/origin 都在展示里，可追到来源 ticket。
- **R6 迭代复用**：✅ 见上。
- **R7 fail-closed**：✅ 未知 status 拒；只读无写入风险。

---

## 结论

**本模块是干净的只读查询壳，功能正确，可确认通过。无 P0/P1 级 bug。** 四项 P2 均为元数据/文档/边界项：F-1 data_keys、F-2 文档命令面、F-3 负 limit 语义、F-4 统计白名单——不阻碍"真挖因子"。建议随 U11 整体元数据对账一并处理，不单独改代码。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 70 个模块 `factor_db.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ F-3 负 limit 语义想当场定（文档说明 vs E1004）→ 告诉我
