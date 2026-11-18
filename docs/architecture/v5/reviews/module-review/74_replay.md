# 模块审查 74 · `v5/app/cli/replay.py`

> 审查日期：2026-11-18
> 依据：源码 254 行全文通读；依赖 `station._frozen`、`orchestration.Executor`（`persist=False`）、`capability_bridge.rebuild_upstream`、`executor.load_committed_parcel`、`capability.evaluate/ic_series`、`contract.py`（READ_ONLY / F_L）；设计 `09-app-storage.md`、`06-evaluation.md` §4/§8、`IMPLEMENTATION-PLAN.md` S7；测试 `test_v5_live_cli.py:554-597`、`test_v5_s7_certify.py`；真实调用 `venv/Scripts/python.exe` + `fm_review_b.py` / `fm_review_c.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`replay.py` 提供 evaluate / backtest / certify 三条**冻结 ticket 单点重算**命令（docstring `:1-17`），回答一件事：**这张 ticket 上这件事重算出来是什么**。三条边界：
1. **只认冻结 ticket**：无 `--plan`，构成全部来自 `frozen/plan.json` + context（`:51-56`）；否则"重算"就成了"换参数再算一遍"。
2. **不产新结论、不改已发布事实**：三者都在 `steps/` 语义下重算（probe），读已落盘正式产物，不写 `steps/`、不写 trace——`Executor(persist=False)`（`:75-77`）。`certify` 仍要求 `--yes`，因契约等级是 F_L（writes_facts+writes_ledger），不因实现是重算就降级（`:230-231`）。
3. **走同一能力链**：evaluate 用 `evaluate` 能力、backtest 用 Portfolio 工位（combine→allocate→backtest）、certify 用 Certify 工位；命令层不自己算指标——自己算一份就等于多了一套口径（`:14,104-182,201-250`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_b.py` 先 `fetch + run --live` 落一张真 ticket，再逐条重算；`fm_review_c.py` 测不存在 ticket 失败路径。

### CLI help（真实退出码，三条都有 `--format`）

```
$ venv/Scripts/python.exe -m v5.app.cli evaluate --help
usage: flow evaluate [-h] --on ON [--candidate CANDIDATE] [--format {table,json,ndjson}]
exit=0
$ ... backtest --help   → --on ON [--format]                       exit=0
$ ... certify --help    → --on ON [--yes] [--format]               exit=0
```

### 输入：`--on <ticket_id>`（只认冻结 ticket，无 `--plan`）

### 输出：`data`（真实打印）

**backtest**：
```
exit=0  state=done
summary keys = [annualisation_used, bars_per_year, cagr_pct, cost_breakdown,
                funding_total, max_dd_pct, net_return, net_sharpe, periods,
                sharpe, turnover, turnover_mean]
steps/*-Portfolio-* 残留 = []        # read_only 证据（未落 steps/）
```

**evaluate**（指定单候选）：
```
exit=0  count=1
row = {"status":"observed",
       "stats":{"ic_mean":-0.021193092621664054,"rank_ic":-0.021193092621664054,
                "icir":-0.11247206625227638,"n_periods":91,
                "coverage":0.25274725274725274}}
```

**certify**：
```
certify --on <ticket>          → exit=2, E1004     # 缺 --yes，契约等级 F_L
certify --on <ticket> --yes    → exit=0, evidence_grade=out_of_sample, folds=2
```

### 失败输入 → 错误输出（fail-closed，全部实测）

| 非法输入 | 错误码 | exit | 真实 message |
|---|---|---|---|
| `backtest/evaluate --on` 不存在 ticket | E3001 | 1 | 上游产物缺失（`_Replay` 读 frozen/context 失败） |
| `certify --on` 不存在 ticket 且缺 `--yes` | E1004 | 2 | 先被 `require_confirmation` 挡（确认先于读 ticket，符合 P3 顺序） |
| `evaluate --candidate` ticket 里不存在 | E3003 | 1 | `ticket 里没有候选 '<cid>'`（`:128-132`） |
| ticket 非 live（`bridge is None`） | E3003 | 1 | `ticket 不是 live 运行，无法重算表达式`（`:106-109`） |
| 无法重建 panel/label | E3001 | 1 | `无法从 ticket 重建 panel/label`（`:120-124`） |
| Portfolio 无净值摘要 | E3003 | 1 | `Portfolio 重算没有产出净值摘要`（`:215-220`） |

> read_only 是**实测**的：重算后 `steps/*-Portfolio-*` 目录为空（`test_v5_live_cli.py:572-573` 同断言）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| Y-1 | **metadata `implemented=False` 陈旧**：`contract.py:104,105,112` 把 evaluate/backtest/certify 标 `implemented=False`，但 `_DISPATCH`（`cli/__init__.py:49`）已接通、`main --help` 可用、实测 exit 0。`test_every_declared_command_has_an_entry_point` 只守"声明=分派集合"，没校验 implemented 标志。 | P2（元数据陈旧） | `contract.py:104-112`；`cli/__init__.py:49`；实测 | 改为 True；与 promote 的 data_keys 一起做元数据对账。 |
| Y-2 | **`_evaluate_candidates` 在命令层算 IC/ICIR**：docstring `:13-14` 承诺"命令层不自己算指标"，但 `:161-180` 用 `capability.evaluate` 得 values/valid_mask 后，**在命令层**调 `ic_series` 并自算 `ic_mean/rank_ic/icir/coverage/n_periods`。而正式 observe 能力里也有一套 IC 计算——两套口径可能分叉。 | **P1（口径漂移风险，需确认意图）** | `replay.py:13-14,161-180`；对照 `capability/ops/observe.py` 的 IC | 需裁决：evaluate 重算的 IC 是"诊断值"还是"应等于 Evidence 口径"。若应相等，应改为消费 observe 能力而非自算。 |
| Y-3 | **`version=_pkg_version("qweave")` 硬依赖**（`:137`）：qweave 未安装会抛 `PackageNotFoundError` → main 兜底 E3004。当前环境已装（实测通过）。 | P2（依赖） | `replay.py:137` | 可加 try/except 给 E3003 提示；低优先。 |
| Y-4 | **`impl_version=version` 传 qweave 包版本**（`:146`）作 `Call.create` 的 impl_version，与 `registry.operators_version`（也在 params，`:146`）可能不一致——同一算子集两个版本标识。 | P2（指纹语义） | `replay.py:144-147` | 低优先；正常路径一致，不阻碍重算。 |
| Y-5 | **`ctx.parcels(("Search",))["Search"].payload` 硬编码 + `result.parcels["Certify"]` 直接取**（`:126,239`）：若 Search candidates 被裁剪返回空 evaluation（不报错）、Certify skip 则 KeyError→E3004。 | P2（边界，静默空） | `replay.py:126-127,239` | 正常 ticket 有候选/证书；裁剪产物场景需加显式 E3003。 |
| Y-6 | **data_keys 对账**：evaluate=`evaluation`、backtest=`summary`、certify=`certificate`（实测），与 `contract.py:104-112` 一致。 | ✅ 无差距 | 实测 | 无需改。 |

**多了什么**：无冗余。`_Replay.close()` 关 log sink（`:100-101`）、`finally: reset_log(token)`（`:190-192`）防日志泄漏，是好设计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 只读回放 / CQRS 查询端（`persist=False`） | 采纳 | 在内存重算、不写 steps/trace（实测残留空），与正式产物隔离 |
| 冻结 ticket 单一真相（无 `--plan`） | 采纳 | 与 `station` 同取舍；重算不能换参数（docstring `:7-8`） |
| 重算走同一能力链 | 采纳 | combine→allocate→backtest / Certify 工位复用（`:205,235`）；但 evaluate 的 IC 自算破坏此原则（Y-2） |
| 命令层自算一套指标当"展示值" | 拒绝（本模块有偏差） | docstring 明拒（`:14`），但 `:161-180` 实际自算 IC——见 Y-2，需对齐 |
| 第二套执行路径/第二套裁判 | 拒绝 | 复用 `Executor` + `capability_run`，无新口径 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **只认冻结 ticket**：`_Replay.__init__` 用 `_frozen(writer, ticket_id)`（`:55`），无 `--plan` 入口；不存在 ticket → E3001（实测）。
2. **read-only 实测**：`persist=False`（`:76`），重算后 steps 无 Portfolio 残留（实测 + 测试 `:573`）。
3. **certify --yes 守卫**：缺 → E1004 exit2（实测）；`require_confirmation` 在 `_Replay` 构造前（`:231`），确认先于读。
4. **重算走能力链**：backtest `execute_station("Portfolio", mode="step")`（`:205`）、certify `execute_station("Certify")`（`:235`），summary/certificate 来自产物 payload；backtest 实测 12 键摘要、certify 实测 out_of_sample + 2 folds。
5. **上游重建用归一参数**：`rebuild_upstream` 传 `executor.station_params`（已归一，`:118`），修复"直接传冻结 dict 让工位拿空参数报 boundary 缺 k"的历史 bug（注释 `:111-113`）。
6. ** evaluate 候选收窄**：`--candidate` 过滤 + 不存在 E3003（`:128-132`），实测 count=1。

**发现的问题**：Y-1（implemented 陈旧，P2）、Y-2（命令层自算 IC，P1 口径漂移风险）、Y-3（qweave 硬依赖，P2）、Y-4（版本标识，P2）、Y-5（硬编码裁剪静默空，P2）。无 P0 级 bug；backtest/certify 路径完全符合 D1，evaluate 路径有 D1 自我矛盾（Y-2）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.4 / S7 章节 | evaluate/backtest/certify 单命令只认冻结 ticket、read-only | 已符合；补一句"evaluate 的 IC 由命令层 ic_series 算，口径需与 Evidence 对齐"（Y-2）。 |
| `contract.py:104-112` | implemented=False | 改 True（Y-1）。 |
| `06-evaluation.md` §4/§8 | Evidence 的 IC 口径 | 若 Y-2 裁决为"应相等"，文档需明确 replay evaluate 与 Evidence 共用同一 IC 实现。 |
| `09-app-storage.md` | certify 契约等级 F_L | 实测缺 --yes → E1004，文档一致，无需改。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新重算命令（如 isolate 单因子六步） | ⚠️ 需加壳 | 新文件 + `_DISPATCH` + contract spec；plan §U11 D6 把 isolate 列为 P2 决策项（当前靠手工拼命令）。 |
| evaluate 多候选批量 | ✅ 已支持 | 不给 `--candidate` 则遍历全部 candidates（`:127-139`）。 |
| 重算其他工位（如 Evidence/Verdict） | ❌ 不支持 | 现只有 evaluate/backtest/certify 三条；扩展需新命令 + `execute_station`。 |
| `--plan` 覆盖参数重算 | ❌ 明确拒绝 | 与 D1"只认冻结 ticket"冲突；不提供（docstring `:7-8`）。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ backtest summary 含 `annualisation_used`/`bars_per_year`（实测），来自 `time_axis_median`（test_v5_live_cli.py:268）；本模块不依赖 252。
- **C2 资金费按事件**：✅ backtest summary 含 `funding_total`/`cost_breakdown.funding`（实测），重算走同一成本链。
- **C3/C4/C5/C6/C7/C8/C9/C11**：本模块重算既有产物，不直接判定（由上游工位保证）。
- **C10 判据不放松**：✅ certify 判据只引用成员（test_v5_s7_certify.py:177-183），本模块不新算显著性展示值。
- **C12 embargo 观测单位**：✅ certify folds 用观测索引（test_v5_s7_certify.py:114-128），本模块只回放。

## R1–R7 影响

- **R1 真数据跑通**：✅ 三条命令在真 ticket 上实测 exit 0。
- **R2 结论可信**：✅ 回放走同一能力链；但 Y-2 的 IC 双口径是潜在风险点，需确认。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：⚠️ evaluate `--candidate` 能单因子重算，但完整六步归因链仍靠手工拼命令（plan D6 决策项）。
- **R6 迭代复用**：✅ 快照重算可跨轮核对。
- **R7 fail-closed**：✅ 不存在 ticket E3001、非 live E3003、缺 --yes E1004、无摘要 E3003。

---

## 结论

**本模块三条命令实测跑通、read-only 与 --yes 守卫成立，可确认通过（附一个 P1 口径确认项）。** backtest/certify 完全符合 D1；**Y-2 是唯一实质问题**——evaluate 在命令层自算 IC，与 docstring"不自算指标"自相矛盾，且可能和 observe 能力口径分叉，**需用户裁决**是接受"诊断值"定位还是改为消费 observe 能力。Y-1/Y-3/Y-4/Y-5 为 P2 元数据/依赖/边界项，不阻碍使用。

**请确认本模块审查结果**：
- ✅ 确认通过（Y-2 列入 P1 待确认）→ 我进入第 75 个模块 `json_fmt.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ Y-2 evaluate IC 自算想当场裁决（诊断值 vs 复用 observe）→ 告诉我
