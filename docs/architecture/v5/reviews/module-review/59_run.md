# 模块审查 59 · `v5/app/cli/run.py`

> 审查日期：2026-11-18
> 依据：源码 181 行全文通读；上游 `runtime/compiler.py`（compile_plan/STATIONS）、`runtime/store.py`（TicketWriter/resolve_data_root）、`orchestration/executor.py`（Executor.execute）、`orchestration/events.py`（EventLedger）、`capability/params.py`（implementation_defaults）、`cli/common.py`、`cli/live.py`（expand_station_inputs/build_live_supply）；设计 `s0/05-orchestration.md` §6（冻结方案）、`09-app-storage.md` §2；测试 `tests/v5/test_v5_app_commands.py:138-170`、`tests/v5/test_v5_live_cli.py`。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`run.py` 是**唯一生产入口**（核心命令，`writes_facts`）：把一份计划编译成冻结方案、跑完八工位、落盘全部产物、返回带指纹的信封。它应然回答五件事：

1. **两段编译**：先「无供给」编译暴露结构/跨字段/实现名错误（否则键拼错会被误报成「数据不存在」，把人骗去先采数据），再「带真实供给身份」编译（门禁 B）。
2. **dummy vs --live 等价壳**：默认 dummy 桩（S0-S2 验收/无数据场景可用）；`--live` 时展开扁平 params + policy_view、注入真实供给与能力、把 combine 实现名放实现通道（不放 params）。两条路径共用同一调用链、指纹链、落盘逻辑。
3. **冻结方案落盘**：`frozen/plan.json` + `frozen/context.json` 先落盘再执行，单步命令 `flow station` 只认这份，不认调用方记忆。内容相同幂等，内容不同拒。
4. **产物真落盘**：`TicketWriter` 必须传，否则「信封报 parcels=N 却什么都没留下」是契约违背。`EventLedger(Ledger(layout))` 让 Reveal/Budget/Publish 一次性跨进程成立。
5. **指纹输出**：信封带五个指纹（plan/blueprint/caliber/domain/policy）+ origins_count + parcels/traces 计数 + stage/live 标记。

**加密视角**：`master_seed` 取自 `plan.search.seed`（`:84-85`），缺省才给 42——硬编码常量会让「改 seed 只改指纹、不改随机流」，用户写进 plan 的 seed 形同虚设。`seed_scope=plan_digest`（`:113`）保证同计划同随机流（可复现，R2）。`supply_pins`（`:151-159`）记冻结时的 checksum，供单步 stale 判定。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 真实调用（`venv/Scripts/python.exe` + 自建真供给 parquet，脚本 `%TEMP%/fm_review56_67/probe_run.py`），非推演。

### 例 1：`cmd_run(dummy)` 成功信封（真实打印）

```
RUN ok=True exit=0 run_id=20261118T064333Z-0585b103
RUN stage=S2-real-supply state=done live=False
RUN parcels=8 traces=8 origins_count=62
RUN fingerprints = {
  "blueprint_digest": "sha256:d0308238129d9f9a00515dd1d45489402d502e49e4555f8967190e70caacf8b2",
  "caliber_digest":   "sha256:1e314dd15b659e3316d7a4483962a0d6e28fcd74fbf462d975adbf639a50eb6a",
  "domain_digest":    "sha256:8220751d2294b4bba51058ad94e0af24eafef6b3f470d51cad204930ab7a904e",
  "plan_digest":      "sha256:0585b103835178283c3f413590016a4efc86e6b6c8c479bfd369f2827f683ad9",
  "policy_digest":    "sha256:72aafcac93823980e3c9ef6cb4d0d697e5e1fd2fbbb4d04eebfa69294d2e5447"
}
RUN supply_datasets = ['basis','funding','ohlcv','open_interest']
```

`plan_digest` 前 8 位 `0585b103` == ticket_id 后缀，印证 `new_ticket_id`（`:75`）。8 parcels/8 traces 与磁盘真有 8 份（`test_v5_app_commands.py:138-170` 钉住）。dummy 下 8 个 payload 均为 `{"kind":"dummy","payload":{},"station":"..."}`（实测），属 S0-S2 桩预期。

### 例 2：`cmd_explain` 与 run 指纹逐位相同（真实）

```
EXPLAIN ok=True exit=0
EXPLAIN fingerprints_match_run = True
EXPLAIN origins_type=dict len=62
EXPLAIN origins_sample = [["plan","plan"],["baseline","plan"]]
```

证明 `run` 与 `explain` 走同一套编译（含门禁 B）——「声明了参数却不生效」的假话不成立（`explain.py:3-5` 的要求落地）。

### 例 3：`--live` 缺供给失败路径（真实 CLI）

```
$ FLOW_DATA_ROOT=<空> venv/Scripts/python.exe -m v5.app.cli run --plan tests/fixtures/v5/plan_valid.json --live
→ {"ok":false,"exit_code":4,"errors":[{"code":"E4002",
   "message":"ohlcv 在 binance/futures 下没有任何数据文件",
   "path":"supply.ohlcv","hint":"先 flow fetch --datasets ohlcv --exchange binance"}]}
EXIT=4
```

fail-closed：缺供给不会静默降级成 dummy（`--live` 显式要真能力）。

### 例 4：零晋升是结论不是故障（源码 `:173-175`，实测 dummy 未触发）

信封固定带 `skip_reason`/`skipped_at`：`skip_reason` 非空时 state=skipped、exit 仍 0。本例 dummy 计划 state=done 故 skip_reason 未填；语义由 `pipeline.collect_alerts` 的 `zero_candidates` 告警承接（见 67 号）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| R-1 | **无供给的第一次编译与第二次编译都传 `aliases=None`**（`:56,68`），第二次虽从 `build_supply_context` 拿到 `aliases` 变量，但方案 B 下它其实是 `symbol_sets`（见 58 号 K-1），`compile_plan(aliases=aliases)` 的语义是「标的集表」还是「别名表」需确认。 | P2（语义） | `run.py:63-72`；`compiler.compile_plan` 签名 | **需确认意图**：解法 B 取消别名，`aliases` 槽位名误导。建议 compiler 参数改名为 `symbol_sets` 或明确该槽用途。不影响结果。 |
| R-2 | **`--no-strict` 只传编译，不影响执行**：`strict = not args.no_strict`（`:50`），只进两次 compile_plan；执行期无 strict 概念。 | — | `run.py:50,58,72` | **符合应然**：strict 是编译期字段检查，不是执行期开关。无差距。 |
| R-3 | **dummy 路径下 `station_params` 是原始嵌套块，`--live` 才展开**：`:94-98` 把工位块整体塞 params；`:118-133` live 时替换为扁平 `live_params`。两条路径的 `station_params` 形态不同，但都进同一 call_digest 链。 | P2（需确认意图） | `run.py:94-98,118-133` | **设计如此**：dummy 桩不读扁平参数，live 能力读扁平参数；同一调用链不同入参形态是合理的。但测试对拍两条路径指纹一致性仅在 explain/run 间（例 2），未覆盖 dummy vs live 同计划指纹——若两者指纹不同是预期（capability 差异），需文档说明。 |
| R-4 | **`master_seed` 缺省 42 来自 `implementation_defaults("cli")`**：`:85` 读 `implementation_defaults("cli")["master_seed"]`，但 plan 无 `search.seed` 时静默用 42，未在信封 origins 标注「seed 来自缺省」。 | P2 | `run.py:84-85`；`explain.origins` 实测全 'plan'（62 项无 default 来源） | **需确认**：C1 类问题——「口径默认来源标注」（explain 必查点）在 seed 上没做到。若 plan 缺 seed，investigator 从 explain 看不出 seed=42 是缺省。建议 origins 补 default 标注或信封显式给 master_seed。 |
| R-5 | **`freeze_context` 的 context 写入用 `default=str`**（`:40`）：若 ctx 含不可 JSON 序列化对象（如 datetime），会被静默转字符串而非报错。 | P2（边界） | `run.py:38-42` | **低优先**：当前 ctx 都是 JSON 友好类型（实测 frozen/context.json 可读），无实际触发。若未来加复杂对象需警惕。 |

**多了什么**：无冗余。两次编译、冻结落盘、writer 注入、EventLedger、seed_scope、supply_pins 都是必要防线。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 编译期冻结产物（immutable build artifact） | 采纳 | `freeze_context`（`:26-42`）先落盘再执行，单步只认磁盘——避免「拿别的参数重算」；内容寻址幂等（同 ticket 重跑内容相同）。 |
| 两段编译（无供给探错 → 带供给编译） | 采纳 | 结构错误先说话（`:52-59`），再查数据——诊断方向正确，避免「键拼错被报成数据不存在」。 |
| 唯一执行器（Executor.execute 单路径） | 采纳 | run/station 共用 `Executor`，不搞第二套执行路径（plan §U5 红线）；`--live` 只切开关与入参，不切执行器。 |
| 随机源内容寻址 scope | 采纳 | `seed_scope=plan_digest`（`:113`），同计划同随机流，实测指纹逐位一致（例 2）。 |
| 硬编码 master_seed=42 | 拒绝 | `:84-85` 从 plan 取 seed——加密 GP 随机搜索必须可复现且用户可控。 |
| 运行期解析「最新」数据 / 别名表 | 拒绝 | 身份现算 + plan_digest 冻结，无别名（19-supply.md 方案 B）。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **writer 必传**：`TicketWriter(layout, ticket_id)`（`:81`）注入 Executor，注释（`:79-80`）明确「不传就只在内存，信封报 ok 却什么都没留下」——实测磁盘 8 份 parcel（例 1 + 测试 `:162-166`）。
2. **EventLedger 落全局账本**：`events=EventLedger(Ledger(layout))`（`:110`），注释（`:107-109`）指出 23 之前恒传 None 导致 AC8 空——一次性语义跨进程成立。
3. **seed_scope 内容寻址**：`:113` 用 plan_digest（不含时间），同计划同候选（可复现）。
4. **工位块整体进调用单**：`blocks`（`:94-98`）把 impl 走实现通道、其余字段走 params 通道，防「不同方案→同一 call_digest」。
5. **live 的 combine 实现名通道正确**：`:121-124` 把 `portfolio.combiner` 放 `station_impls["Portfolio"]`，不放 params——「换合成器」改 impl 指纹而非参数指纹。
6. **supply_pins 记账**：`:151-159` 记 checksum/version/rows/symbols，单步 stale 判定唯一依据（60 号审查）。
7. **finally 重置日志**：`:163-165` 无论成功失败都 `reset_log` + `close`，避免日志上下文泄漏到下一次调用。
8. **master_seed 来自 plan**：`:84-85` 实测 plan.seed=20261116 生效（fingerprint 稳定）。

**发现的问题**：
- 无 P0/P1 级 bug。R-1/R-3/R-4/R-5 均为 P2 语义/可追溯性/边界问题，不影响八工位真跑与落盘。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/05-orchestration.md` §6 冻结方案 | 与 `freeze_context` 一致 | 无需更新；可补一句「frozen/context.json 含 supply_pins，单步 stale 判定依赖它」 |
| `09-app-storage.md` §2 run data_keys | 声明 6 键（contract.py:88） | 实际返回 13 键（例 1 + `:167-180`），补 `skip_reason/skipped_at/stage/live/supply_datasets` 到文档/契约（并入 56 号 C-4） |
| `explain` 参数来源文档 | 若声称「每个参数来源可标注」 | 补 R-4：plan 缺 `search.seed` 时 seed=42 来自 implementation defaults，explain origins 未标注 default 来源 |
| `s0/05` §6.2 能力模式单步重建 | `run` 的 live 展开（`expand_station_inputs`） | 已与 `live.py` 对齐，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一条新算法进 GP | ✅ 零代码（本模块） | 算法在 plan.search.algorithms 配置，run 只编译+执行；本模块无需改。 |
| `--live` 增加新数据集流 | ✅ 半配置化 | `supply_datasets` 来自 plan.supply.datasets，`build_live_supply` 按数据集注入；新数据集需 upstream/store 支持（U3）。 |
| 新增第九个工位 | ❌ 需改代码 | 工位清单 `STATIONS` 来自 compiler；新增工位涉及 orchestration/compiler/执行器多处（全局变更，非本模块单独）。 |
| 单步 stale 判定扩展（比 checksum 更多维度） | ⚠️ 需改代码 | `supply_pins`（`:151-159`）只记 checksum/version/rows/symbols；加维度需改此处 + 60 号 `_supply_staleness`。 |
| 同一 plan 多 seed 对照跑 | ⚠️ 需手动 | seed 从 plan 取，多 seed 需复制 plan 或 `--set search.seed=..`（走 override 通道，会改 plan_digest → 新 ticket），非内置批处理。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：中性（本模块不算年化）；`caliber` 来自 plan，run 不改口径。
- **C3 spot/futures**：✅ `build_supply_context`（common.py:109）市场×数据集二次查，run 间接继承。
- **C6 上市/下架可用时间**：✅ 面板全窗覆盖（common.py:_panel_window），run 编译期继承。
- **C7 24/7 缺口**：✅ 供给门禁 B 检查末端闭合/覆盖（compile_plan require_supply=True）；缺口体检在 doctor/pipeline（65/67 号）。
- **C8 列集开放**：中性（run 不限制字段；字段在 panel/stream 层）。
- **C12 1 观测=1 天**：✅ `master_seed`/`seed_scope` 内容寻址，与观测单位无关；`_panel_window` 按日解析。
- **C2/C4/C5/C9/C10/C11**：不适用（本模块不读字段、不算判据）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑——唯一生产入口，dummy/live 双路径实测；`--live` 缺供给 E4002/exit4 fail-closed。
- **R2 结论可信**：✅ 强支撑——plan seed 生效 + 内容寻址 seed_scope + 两段编译 + 指纹逐位可复现（例 2）。
- **R3 因子零代码扩展**：✅ 正向——新算法/数据集配置化，run 只消费冻结 plan。
- **R5 单因子可归因**：中性（组合/归因在 U9，run 只执行）。
- **R7 fail-closed**：✅ 强支撑——writer 必传、EventLedger 落账、supply_pins 冻结、结构错误先编译暴露、零晋升 skip 明确。

---

## 结论

**本模块是唯一生产入口，代码质量高，无 P0/P1 级 bug，可确认通过。** 两段编译、dummy/live 双路径、冻结落盘（frozen/plan+context）、writer 必传防假成功、EventLedger 跨进程一次性、seed 内容寻址、五指纹输出——全部实测成立（8 parcels/8 traces、run 与 explain 指纹逐位一致、`--live` 缺供给 E4002/exit4）。5 项 P2：R-1（aliases 槽位语义误导）、R-3（dummy/live 参数形态不同未对拍指纹）、R-4（seed 缺省来源未在 explain 标注）、R-5（`default=str` 静默序列化边界），均为语义/可追溯性/边界问题，不影响「生产可用=能真挖因子」。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 5 个模块 `cli/station.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ R-1/R-4 想当场裁决（compiler 参数改名 / seed 缺省标注）→ 告诉我
