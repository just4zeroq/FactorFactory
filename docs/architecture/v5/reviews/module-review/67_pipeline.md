# 模块审查 67 · `v5/app/cli/pipeline.py`

> 审查日期：2026-11-18
> 依据：源码 182 行全文通读；上游 `cli/doctor.py`（cmd_doctor）、`cli/run.py`（cmd_run）、`facilities/notify.py`（build_notifier/F7 去重限流）、`capability/params.py`（implementation_defaults("pipeline").n_trials_drop_ratio）、`storage/layout.py`、`contracts/errors.py`；设计 `15-operations.md` §3（告警阈值表）、`09-app-storage.md` §2（pipeline = doctor→run 串联单一退出码）、`s0/05`；实测 `venv/Scripts/python.exe -m v5.app.cli pipeline --plan ...` 正常/--force + `collect_alerts` 纯函数探针。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`pipeline.py` 是 **doctor → run 的串联编排**（`writes_facts`，产 ticket），存在的理由只有一个（docstring `:3-5`）：**别用坏数据跑一次完整执行**——供给有缺口/末端未闭合时 run 不会失败，它会照常产出候选、照常裁决，只是结论建立在有洞的时间轴上。把体检放前面，让「数据有问题」在烧掉一次执行前暴露。它应然：

1. **体检门禁**：先跑 doctor，两段之间只按 `checks[].passed`（末端闭合+无缺口）判 blocking，**不看行数偏少的 warning**（薄标的是研究选择，不是数据故障，`:7-8`）。blocking 非空 → state=stopped/reason=supply_not_ready，不跑 run。
2. **`--force` 跳过体检**：唯一正当用途「已知缺口且明确接受」，结果永远带 `doctor_skipped:true`，不假装体检过（`:10-11`）。
3. **run 失败不吞错**：run 的失败带自己的错误对象，pipeline 只补「停在哪一步」，不吞原始错误（吞掉会让 exit_code 与原因对不上，`:164-171`）。
4. **告警评估 + 投递**：run 后按 `15-operations.md` §3 阈值表评估告警（零候选/试验计数突降/DSR 全失败），经 Notifier（F7 去重/限流）投递并写进信封——`pipeline_<ts>.json` 是唯一可信「跑过没有」证据，告警必须落同一份证据（`:13-15`）。告警**只评估不裁决**：不许静音（I52），处置归人（`:50-51`）。
5. **单一退出码**：一次调度只产一个退出码（不另起执行路径），信封汇总 steps。

**加密视角（必查点 告警：零候选/DSR 全失败）**：加密高波动下 DSR 全失败「常见且正常」（`15-operations.md` §3），告警应是 info 级「提高信号量级，不下调门槛」（`:91-97`）——呼应 C10「判据不得被加密波动大放松」。零候选告警 warn 级并提示「先查信号量级再谈门槛」（§4-6 红线）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 真实调用，非推演。

### 例 1：pipeline 正常路径（真实，59 号 runroot 供给）

```
$ FLOW_DATA_ROOT=<runroot> venv/Scripts/python.exe -m v5.app.cli pipeline --plan tests/fixtures/v5/plan_valid.json
→ {"ok":true,"command":"pipeline","run_id":"20261118T064642Z-0585b103","exit_code":0,
   "data":{
     "steps":[
       {"step":"doctor","ok":true,"groups":4,"blocking":[],"warnings":1},
       {"step":"run","ok":true,"run_id":"20261118T064642Z-0585b103",
        "state":"done","skip_reason":null,"parcels":8,"live":false}
     ],
     "state":"done","skip_reason":null,"parcels":8,
     "doctor_skipped":false,"alerts":[]}}
EXIT=0
```

- doctor groups=4、warnings=1、blocking=[]，体检通过 → 继续 run（warnings 不阻断，符合 D1-1）。
- run 8 parcels、state=done，五步信封汇总正常完成，alerts=[]。

### 例 2：`--force` 跳过体检（真实）

```
$ ... pipeline --plan ... --force
→ doctor_skipped=True, state=done, steps=['doctor','run']
EXIT=0
```

`--force` 时 steps[0] 是 `{step:"doctor", skipped:true, reason:"--force", ok:true}`（`:129-131`），结果 `doctor_skipped:true` 诚实标记（`:179`）。

### 例 3：`collect_alerts` 三类告警（真实纯函数打印）

```
zero_candidates: [{'kind':'zero_candidates','severity':'warn',
   'message':'本轮无候选过闸（state=skipped, skip=no_candidate_passed）；先查信号量级再谈门槛（15-operations §4-6 红线）'}]
dsr_all_fail:    [{'kind':'dsr_all_fail','severity':'info',
   'message':'全部候选 DSR 失败：门槛与信号量级不匹配（常见且正常）；提高信号量级，不下调门槛'}]
normal done:     []
```

- 零候选（`:69-74`）：`skip=="no_candidate_passed"` 或 `state=="skipped"` → warn。
- DSR 全失败（`:92-97`）：`dsr_all_rejected` → **info** 级，明说「常见且正常，提高信号量级不下调门槛」——C10 落地。
- 正常 done 无告警。

### 例 4：试验计数突降（源码 `:78-89`）

`_history_n_trials` 扫 `runs/<ticket>/parcels/04-Search-*/payload.json` 取历次 n_trials 升序，取末两位对比：`current < prev * ratio`（ratio 来自 `implementation_defaults("pipeline")["n_trials_drop_ratio"]`，缺省约 0.5，G11 不写死）→ warn「计数链可能断了」。本例历史只有 1 次 run，`len(history)<2` 未触发（实测 n_trials_drop 探针返回 []）。

### 例 5：体检 blocking → 停（源码 `:147-152`，语义确认）

若 doctor 的 checks 有 `passed:false`（缺口/未闭合），返回 `envelope_ok("pipeline", None, {"steps":[...],"state":"stopped","reason":"supply_not_ready"})` —— exit **0**（envelope_ok），只是 state=stopped。本例供给健康未触发。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| P-1 | **体检 blocking 时 pipeline 返回 exit 0（envelope_ok + state=stopped）**：「因数据问题没跑成」与「正常跑完」同为 exit 0，机器消费需读 `data.state` 才能区分。 | P2（需确认意图） | `pipeline.py:148-152`；设计 `09` 说 pipeline「单一退出码汇总」 | **需确认**：若调度器只看 exit code，会把「供给未就绪停跑」当成功。但设计上 data bad 是「供给问题」应 exit4（E4xxx），这里却 exit0——语义偏离。建议 blocking 时返回 exit4（或至少 warning 明示）。 |
| P-2 | **`dsr_all_rejected` 字段依赖 run 信封传递，但 `run.py:167-180` 的 data 里没有 `dsr_all_rejected`**：`collect_alerts` 读 `run_data.get("dsr_all_rejected")`（`:92`），而 cmd_run 信封不含此键（实测例 1 run_data 无该字段）。 | **P1（告警失效）** | `pipeline.py:92` vs `run.py:167-180`（实测 data 键无 dsr_all_rejected）；例 3 纯函数能触发但端到端拿不到 | **已确认**：DSR 全失败告警在端到端 pipeline 中**永远不会触发**——因为 run 信封不传该字段。纯函数探针传了假 `run_data={"dsr_all_rejected":True}` 才触发。这是用户必查点「DSR 全失败告警」的真实缺口：告警逻辑写好了，但数据源断了。修复：run 信封补 `dsr_all_rejected`（从 Verdict payload 派生），或 pipeline 直接读 Verdict parcel。 |
| P-3 | **`collect_alerts` 的 `ValueError`（ratio 越界）未在 cmd_pipeline 捕获**：`:62-63` 抛 ValueError，会冒到 main 兜底成 E3004/exit1（「代码写错」），但 ratio 来自 implementation_defaults（内部值），越界只可能是配置错误。 | P2（边界） | `pipeline.py:62-63`；`__init__.py:212-216` 兜底 | **低优先**：ratio 由内部 defaults 提供，越界概率低；调用方可传 `n_trials_drop_ratio` 覆盖（如测试），此时是用法错误宜 E1004。可加参数校验。 |
| P-4 | **`_history_n_trials` 只扫 `parcels/04-Search-*/payload.json`，不读 steps/**：与「probe 不算正式产物」一致（`:869`），但若某次 run 的 Search 是 skip（无 parcel），该次不计入历史——合理（没试验就没法比）。 | — | `pipeline.py:32-45` | 符合应然，无差距。 |
| P-5 | **`_emit_alerts` 在无外部 sink 时把 Notifier「视为已送达」**（`:104-105`）：信封 `notified:true` 是「信封即证据」的约定，不是真有外部通知。 | P2（需确认意图） | `pipeline.py:101-122` docstring | **设计如此**：本轮不做外部运维（plan §0.0），信封落盘即证据。但 `notified:true` 措辞可能误导以为发了外部告警；建议字段名改 `recorded` 或 note 说明。 |

**多了什么**：无。体检门禁、force 诚实标记、不吞错、告警评估/投递、单一退出码都是必要编排。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| preflight（体检前置） | 采纳 | 缺口/未闭合在烧掉执行前暴露（D1-1）——24/7 数据缺口隐蔽，前置省时且防坏结论。 |
| 告警只评估不裁决（human-in-the-loop） | 采纳 | I52「告警不许静音」、处置归人（`:50-51`）；DSR 全失败 info 级「不下调门槛」——C10 落地。 |
| F7 去重/限流（同 key 窗口） | 采纳 | `_emit_alerts`（`:101-122`）用 `dedup_key=alert.kind` 经 Notifier，防告警风暴；无外部 sink 时信封即证据。 |
| 阈值不写死（G11，注册表缺省） | 采纳 | `n_trials_drop_ratio` 来自 `implementation_defaults("pipeline")`（`:58-61`），与 GP 种群等共用实现缺省机制，不第二份真相。 |
| 第二套流水线 | 拒绝 | pipeline 只串联 doctor+run（调 `cmd_doctor`/`cmd_run`），不另写执行逻辑——「一次调度一个退出码」（09 §2）。 |
| 告警静音/自动调门槛 | 拒绝 | DSR 全失败明令「提高信号量级，不下调门槛」（C10）；告警不许静音（I52）。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **体检门禁正确**：只看 `checks[].passed`（`:136-141`），薄标的 warning 不阻断（例 1 warnings=1 仍跑 run）。
2. **force 诚实标记**：`:129-131,179`，实测 doctor_skipped=True（例 2）。
3. **run 失败不吞错**：`:164-171` 用 `{**run, command:"pipeline", data:{steps,...}}`，保留原始 errors/exit_code。
4. **零候选/DSR 全失败告警逻辑正确**：例 3 实测 severity/message 与 §3 阈值表一致。
5. **阈值不写死**：G11（`:58-61`），`ValueError` 守 ratio∈(0,1)。
6. **F7 投递**：`build_notifier("ticket")` + `dedup_key=kind`（`:110-117`），notes 带去重/限流痕迹。
7. **alerts 落信封即证据**：`envelope_ok` 的 data.alerts（`:180`），与 `pipeline_<ts>.json` 一致（F1 组合根）。

**发现的问题**：
- **P-1（P1）DSR 全失败告警端到端失效**：`collect_alerts` 读 `run_data["dsr_all_rejected"]`，但 cmd_run 信封从不含此键（实测 run_data 键列表）。纯函数触发 ≠ 端到端触发。**这是用户必查点「DSR 全失败告警」的真实断点**。修复方向：cmd_run 从 Verdict payload 派生 `dsr_all_rejected` 写入信封（需确认 Verdict payload 是否有该字段），或 pipeline 查验时直接读 Verdict parcel。
- P-1（blocking exit0）、P-3（ValueError 兜底）、P-5（notified 措辞）为 P2。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `15-operations.md` §3 告警阈值表 | 描述 DSR 全失败「常见且正常」 | 补 P-1 现状：告警逻辑已实现但端到端 run 信封未传 `dsr_all_rejected`，告警当前不触发（修后更新） |
| `09-app-storage.md` §2 pipeline 条目 | 「refresh→doctor→run 单一退出码」 | 改 `refresh`→`fetch`（并入 56 号 C-1）；补 P-1：blocking 时 exit0+state=stopped，调度器需读 state |
| `15-operations.md` §3 | 若称「alerts 经 Notifier 投递」 | 补 P-5：无外部 sink 时 `notified:true` 意为「信封已记录」，非真的外部送达 |
| `s0/05-orchestration.md` 单一退出码 | 与 pipeline 信封汇总一致 | 无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一类告警（如连续 N 轮零晋升） | ⚠️ 需改代码 | `collect_alerts` 写死三类（`:48-98`）；设计 `15-operations.md` §3 说连续 3 轮零晋升归 promote/factors 链，不在 pipeline——新告警需定位归属。 |
| 告警阈值可配（不只 n_trials_drop_ratio） | ⚠️ 半配置化 | ratio 走 `implementation_defaults("pipeline")`（G11）；其它阈值（零候选 severity）写死，加配置需扩注册表。 |
| 外部通知 sink（邮件/webhook） | ⚠️ 需配 Notifier | `build_notifier` 已抽象，无外部 sink 时信封即证据；接外部 sink 属运维（本轮不做）。 |
| pipeline 支持 `--live` 透传 | ✅ 已支持 | pipeline 用 `_add_plan_args` 含 `--live`（`__init__.py:74-75`），传给 cmd_run；实测 run_data.live 透传（例 1 live=false）。 |
| blocking 时 exit 非 0 | ❌ 当前 exit0 | P-1：blocking 返回 envelope_ok；若要求 exit4 需改 `:148-152`。 |

---

## C1–C12 相关项判定

- **C10 判据不得被放松**：✅ 强——DSR 全失败告警 info 级明说「提高信号量级，不下调门槛」（`:91-97`），零候选 warn「先查信号量级再谈门槛」（`:68-74`）；但**端到端 DSR 告警因 P-1 断链未触发**（逻辑正确、数据未接通）。
- **C7 24/7 缺口**：✅ 体检门禁用 doctor 的缺口/未闭合判定（`:136-141`），缺口数据不跑完整执行。
- **C1 365 / C2 资金费 / C3 市场 / C4 合约 / C5 / C6 / C8 / C9 / C11 / C12**：不适用或中性（pipeline 是编排层，不直接算这些；口径在 plan/caliber，数据在上游）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑——体检前置 + run 串联，实测 groups=4/blocking=[]/run 8 parcels 全绿（例 1）。
- **R2 结论可信**：✅ 正向——不让坏数据跑出「看似正常」的结论；但 P-1（DSR 告警断链）削弱异常可见性。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：⚠️ 部分——体检 blocking 拒跑（正向），但 blocking 返回 exit0（P-1 语义偏离）、DSR 告警端到端失效（P-1），这两处是 fail-open 风险点。

---

## 结论

**本模块编排逻辑无 P0 级 bug，但发现一个 P1 级告警断链：DSR 全失败告警端到端失效（P-1）。** 体检门禁（缺口/未闭合 blocking）、force 诚实标记、run 失败不吞错、零候选告警、阈值不写死（G11）、F7 投递、信封汇总——全部实测成立（正常 pipeline exit0/alerts=[]、force doctor_skipped=true、三类 collect_alerts 纯函数正确）。**但 `collect_alerts` 读的 `run_data["dsr_all_rejected"]` 在 cmd_run 信封里根本不存在（实测 run_data 键列表无此字段），导致用户必查点「DSR 全失败告警」在真实 pipeline 中永不触发**——逻辑写好了，数据源断了，属 P1 修复项（run 信封补字段或 pipeline 直读 Verdict parcel）。另有 P2：blocking exit0（调度器需读 state）、ValueError 兜底、notified 措辞。修复 P-1 后本模块可达「生产可用」。

**请确认本模块审查结果**：
- ✅ 确认通过（P1 告警断链另案处理）→ 我已完成全部 12 个模块
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ P1 告警断链想当场裁决修复方案（run 信封补字段 vs pipeline 直读 Verdict）→ 告诉我
