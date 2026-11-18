# 模块审查 68 · `v5/app/cli/promote.py`

> 审查日期：2026-11-18
> 依据：源码 164 行全文通读；依赖 `catalog.py`（add/entries/expressions/read_ticket）、`contract.py`（mutation F_L / data_keys）、`errors.py`（码表）、`common.py`（envelope_ok）；设计 `09-app-storage.md` §2.2/§2.4/L88/L191、`s0/05-orchestration.md`；测试 `test_v5_catalog_ops.py:92-155`；真实调用 `venv/Scripts/python.exe` + `fm_review_a.py` / `fm_review_d.py`（系统临时目录）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响；"生产可用=能真挖因子"（plan §0.0）。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`promote` 是"把已裁决的候选变成跨 run 可复用资产"的唯一入口，回答五件事：
1. **只认裁判，不认主观**：能不能入库由 Verdict 的 `decision`（`pass`/`watch`）决定（`:30,119`）。`--candidate` 只能**收窄**范围——把 `reject` 拉进库等于绕过唯一裁判（`:85-94,116-121`）。
2. **表达式必须来自 Search 产物**：按 `candidate_id` join，库里只有一个哈希既不可复算也不可当先验池；裁决说 pass 却找不到表达式 → E3003，不凭 id 入库（`:122-130`）。
3. **幂等**：同一 `factor_id` 已在库就跳过并如实报告，不写第二行（`:148-154`；`catalog.add` 在文件锁内二次去重）。
4. **证据等级诚实**：`evidence_grade` 取自 Certify 产物；因零晋升跳过 Certify 时是 `in_sample`——"回测过"不能被读成"认证过"（`:34-41,142`）。
5. **门禁可选**：`--min-grade out_of_sample` 时若本轮无样本外证据，直接空结果返回 `reason=not_out_of_sample`，不静默降级（`:57-62`）。

**加密特化**：本模块不碰时间轴/资金费/市场维度——它只搬运上游已算好的结论与表达式；加密语义由 Verdict/Search/Certify 保证，promote 的职责是"不篡改地固化"。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下全部是 `venv/Scripts/python.exe` 实际打印结果（Python 3.12.1）。临时脚本 `fm_review_a.py` / `fm_review_d.py` 置于系统临时目录，`FLOW_DATA_ROOT` 指向 `tempfile.mkdtemp()`。Ticket 按 `test_v5_catalog_ops.py:29-60` 的 `_ticket` 手工构造，不跑全链路。

### CLI help（真实退出码）

```
$ venv/Scripts/python.exe -m v5.app.cli promote --help
usage: flow promote [-h] --run-id RUN_ID [--candidate CANDIDATE]
                    [--min-grade {any,out_of_sample,in_sample}] [--yes]
                    [--format {table,json,ndjson}]
exit=0
```

### 场景（围绕因子库闭环）

- Ticket `20260101T000000Z-abcdef12`：3 个 Search 候选；Verdict 判 `CID1=pass`、`CID2=reject`；无 Certify（grade=`in_sample`）。
- Ticket `20260102T000000Z-abcdef13`：单候选 `CID3=pass`；Certify `evidence_grade=out_of_sample`。
- 期望：只 `pass/watch` 入库；二跑幂等；`--min-grade oos` 在无 Certify 时挡住、有 Certify 时放行并留痕。

### 输入：`cmd_promote(args)`

```
args.run_id   = "20260101T000000Z-abcdef12"
args.candidate= []            # 默认全部 pass/watch
args.min_grade= "any" | "out_of_sample"
args.yes      = True          # 缺则 require_confirmation 抛 E1004
```
读取 `read_ticket` 的 `payloads`：`Search.candidates[].payload.expression`、`Evidence.per_candidate[].metrics`、`Verdict.per_candidate[].decision/statistics`、`Certify.evidence_grade`。

### 输出：`data`（真实打印，节选）

**首次晋升**（`promoted=1`，reject 被跳过）：
```json
{"promoted":[{"factor_id":"sha256:aaaa…","name":"gp_symbolic:aaaaaaaa","decision":"pass"}],
 "skipped":[{"factor_id":"sha256:bbbb…","reason":"decision:reject"}],
 "counts":{"candidates":2,"promoted":1,"already":0,"skipped":1},
 "evidence_grade":"in_sample","catalog_total":1}
```

**二跑（幂等，已确认）**：
```json
{"promoted":[],
 "skipped":[{"factor_id":"sha256:aaaa…","reason":"already_in_catalog"},
            {"factor_id":"sha256:bbbb…","reason":"decision:reject"}],
 "counts":{"candidates":2,"promoted":0,"already":1,"skipped":1},
 "catalog_total":1}
```

**`--candidate` 收窄（两个方向都实测）**：
```
# 指定 reject 候选 CID2 → 不入库
{"promoted":[],"skipped":[{"reason":"not_selected"},{"reason":"decision:reject"}],"counts":{"promoted":0,…}}
# 两候选均 pass，只指定 CID4 → 仅 CID4 入库，CID5 not_selected（fm_review_d.py）
库内 factor_ids = ['sha256:44444']   C4 in库=True  C5 in库=False
```

**证据等级门禁**：
```
min-grade=out_of_sample + 无 Certify → {"promoted":[],"reason":"not_out_of_sample","counts":{"candidates":0,…}}
min-grade=out_of_sample + 有 Certify → {"promoted":[{"factor_id":"sha256:cccc…","decision":"pass"}],
                                       "evidence_grade":"out_of_sample","catalog_total":2}
catalog.get(CID3).evidence_grade = out_of_sample
```

### 失败输入 → 错误输出（fail-closed，全部实测）

| 非法输入 | 错误码 | exit | 真实 message |
|---|---|---|---|
| 缺 `--yes` | E1004 | 2 | `promote 会写全局账本，必须显式 --yes` |
| `--candidate` 不在候选集 | E1008 | 2 | `--candidate 不在本次执行的候选集里: ['sha256:ffff…']` |
| 无 Verdict（`per_candidate` 空） | E3003 | 1 | `本次执行没有裁决结果，无法晋升`（`:67-72`，零候选跳过语义） |
| 裁决 pass 但 Search 无表达式 | E3003 | 1 | `候选 … 在 Search 产物里没有表达式`（`:126-130`） |
| `--min-grade bogus` | E1008 | 2 | `未知 --min-grade 'bogus'`（argparse choices 先挡，exit 2） |

> 幂等判定发生在 `catalog.add` 的文件锁内（`:170-173`），不只靠 promote 侧的 `get`——两个 promote 并发启动也不会各写一行。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| P-1 | **mutation 等级虚报**：`contract.py:106` 把 promote 标为 `F_L={writes_facts,writes_ledger}`，但源码全文（`:21-28` imports）只导入 `Catalog`，无任何 `Ledger`/`EventLedger` 写调用——它写的是 catalog，不是账本。对照 `factor_db.py:42-44` 明确写道"把 mutation 谎报成 writes_ledger 会让 `--yes` 语义失准"并因此**不用** `require_confirmation`；promote 却以错误等级换 `--yes` 守卫。 | **P2（语义/契约一致性，需确认意图）** | `contract.py:75,106`；`promote.py:1,96,148`；对照 `factor_db.py:42-49` | 功能未坏（`--yes` 仍生效，且写库确实该确认），但元数据说谎。需裁决：是让 promote 真写一条晋升账本事件，还是改标 `WRITES_FACTS` 并像 factor_db 一样自守 `--yes`。 |
| P-2 | **data_keys 声明与实际不符**：`contract.py:106` 声明 `("promotion",)`，实测返回键为 `promoted/skipped/counts/evidence_grade/catalog_total`，无 `promotion`。测试只断言集合相等（`test_v5_catalog_ops.py:415-425`），不校验 data_keys。 | P2（元数据陈旧） | `contract.py:106`；`promote.py:156-163` 实测 | 补元数据对账测试或改声明；不影响 CLI 实际输出。 |
| P-3 | **`evidence_grade` 不校验取值域**：Certify 产物里若是非空字符串（如 `"bogus"`）即原样入库（`:38-40`）；`min_grade=any` 路径不拦。 | P2（数据质量） | `promote.py:34-41,142` | 上游 Certify 是唯一真相，正常路径不会产 bogus；但若产物被手工篡改会污染先验池。可在入库时加白名单校验（`_GRADES` 的后两段）。 |
| P-4 | **文档 09 §2.4 的 promote data_keys 为 `factor_id/target_path/lineage`**，与实测五键不一致。 | P2（文档，见 D5） | `09-app-storage.md:191` | 并入 D5，不改代码。 |

**多了什么**：无冗余。`seen` 去重（`:106,112-114`）防同一 Verdict 里重复 candidate_id 被处理两次，是必要守卫。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| event-sourcing append-only 资产目录 | 采纳 | `catalog.add` 逐行 O_APPEND + 文件锁内二次去重（`:169-178`）；状态变更追加行，历史可回放 |
| 内容寻址身份 `factor_id=candidate_id=H(表达式+字段集)` | 采纳 | 同一表达式由 GP/随机/先验产生只入库一次（`catalog.py:8-16`）；跨 run 去重成立 |
| "研究liked 候选先入池再评审"的工作流 | 拒绝 | 本模块把裁判前置（`decision` 闸门 + 表达式 join），主观入选无法绕过 Verdict |
| 数据库 UPSERT 覆盖式写因子 | 拒绝 | 覆盖会丢"何时因何下架"；本模块 append-only + `status` 折叠（`catalog.py:65-81`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **只认裁决**：`_PROMOTABLE={"pass","watch"}`（`:30`），`decision not in` → `skipped decision:<x>`（`:119-121`）；实测 reject 候选即使被 `--candidate` 指定也不入库（取证 D/A）。
2. **`--candidate` 只收窄不扩权**：先校验 `missing=wanted-set(ast_by_id)` → E1008（`:89-94`），再逐个 `if wanted is not None and cid not in wanted: not_selected`（`:116-118`）；实测两候选均 pass 时只入被指定的那个（`fm_review_d.py`）。
3. **表达式 join 不可绕过**：`source["payload"]["expression"] is None` → E3003（`:122-130`），不是跳过。
4. **幂等**：`catalog.add` 在 FileLock 内重扫 + `self.get` 双判（`:170-173`），二跑 `already=1` 实测。
5. **证据等级透传**：`_grade_of` 只读 Certify 的 `evidence_grade`，缺省 `in_sample`（`:34-41`）；`min_grade=oos` 双路径实测（`reason=not_out_of_sample` 与 `catalog.get(CID3).evidence_grade=out_of_sample`）。
6. **计数一致**：`counts.candidates=len(seen)`、`promoted`、`already`、`skipped=len(skipped)-already`（`:159-160`），实测各字段自洽。
7. **异常即失败**：无 Certify 却又 pass 候选仍入库但标 `in_sample`，不伪装成认证过——诚实降级。

**发现的问题**：
- 见 D2 表 P-1（mutation 虚报，P2 需确认）、P-2（data_keys 陈旧）、P-3（grade 值域）。**无 P0/P1 级代码 bug**；实测六条主路径 + 五条失败路径全符合预期。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md:88` | `promote` 标 `writes_facts+writes_ledger` | 与 P-1 联动裁决：若确认写库即写账本，文档需说明账本记了什么；若改标 facts，文档同步改。 |
| `09-app-storage.md:191` | promote data_keys=`factor_id/target_path/lineage` | 改为实测 `promoted/skipped/counts/evidence_grade/catalog_total`。 |
| `contract.py:106` | data_keys=`("promotion",)` | 与上行同改；补一条"spec.data_keys ⊆ 实际返回键"的守卫测试。 |
| `09-app-storage.md:217`（I31） | `promote` 无 `--yes` → exit 2 | 实测一致，无需改。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增证据等级（如 `walk_forward_oos`） | ⚠️ 半配置化 | `_GRADES`（`:31`）加值 + Certify 产出该 grade；promote 透传不需改，但白名单校验（P-3）要同步。 |
| 新增裁决类别（如 ` provisional`） | ❌ 需改代码 | `_PROMOTABLE`（`:30`）硬编码；加值需改一处，且要确认 Verdict 侧语义。 |
| 按 `name` 而非内容 id 去重 | ❌ 不支持 | 身份写死 `factor_id=candidate_id`（`catalog.py:10`）；改名/换来源都同一行，这是刻意的（不可为"好认"破坏内容身份）。 |
| 种子/先验回灌（`db:*`） | ✅ 已支持（在 `live.py:583-597`） | promote 负责写，`resolve_prior_pool(db:*)` 负责读；promote 本身不需改。 |
| 因子下架（`factor_db --retire`） | ✅ 已支持 | 写侧分离（见模块 70）；promote 不碰下架。 |

---

## C1–C12 相关项判定

- **C1 365 年化 / C2 资金费 / C3 市场维度 / C4 合约四元组 / C5 计价货币 / C6 上市下架 / C7 24/7 缺口 / C8 列集开放 / C9 as-of / C10 判据 / C11 流动性 / C12 embargo**：本模块均不直接参与——它是"搬运已算好的结论与表达式"，不重算任何统计、不读盘、不判显著。
- **R6 迭代复用（核心相关）**：✅ 强支撑——因子库闭环的写侧，且 `evidence_grade` 区分"回测过/认证过"。
- **R2 结论可信（间接相关）**：✅ promote 不篡改 Verdict statistics（`:143` 原样 dict 拷贝）。

## R1–R7 影响

- **R1 真数据跑通**：中性（手法工 ticket 实测，整链真跑由 `live.py`/`pipeline` 保证）。
- **R2 结论可信**：✅ 强支撑（只认裁决 + 证据等级留痕）。
- **R3 因子零代码扩展**：中性（不决定因子空间，只固化已挖到的）。
- **R5 单因子可归因**：中性（表达式+字段集+operators 一并入库，归因链可追）。
- **R6 迭代复用**：✅ 强支撑（幂等入库 + 先验回灌的唯一写入口）。
- **R7 fail-closed**：✅ 强支撑（reject 拒入、无表达式 E3003、候选集外 E1008、oos 门禁不静默降级）。

---

## 结论

**本模块功能正确、可确认通过。无 P0/P1 级代码 bug。** 六条主路径（首次/幂等/--candidate 双向/min-grade 双向）与五条失败路径全部实测符合 D1。唯一实质问题是 **P-1 mutation 等级虚报**（标 `writes_ledger` 却只写 catalog）——它不破坏功能，但让 `--yes` 的语义说明失真，且与同层 `factor_db.py` 的处理相矛盾，**需用户裁决**是补写账本事件还是改标元数据；P-2 data_keys、P-3 grade 值域为低优先元数据/数据质量项，可并入 U11 整体对账。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 69 个模块 `factors.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ P-1 mutation 等级想当场裁决（补写账本 vs 改元数据）→ 告诉我
