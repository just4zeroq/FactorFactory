# 17 · 端到端走查：一次完整执行长什么样

> 本篇是一份**可以照着核对**的样例：一份完整计划、一棵期望目录树、每一步的期望输出，以及三处「必须失败」的演示。
> 它的作用是文档自洽性的最终检验：如果 `01`–`16` 的描述合起来跑不出下面这条路径，那它们中间至少有一处自相矛盾。

---

## 1. 计划（`examples/plan_cs_2026q4.yaml`）

```yaml
# 完整计划见 examples/plan_cs_2026q4.yaml（唯一规范样例，测试会校验它）。
# 下面只摘三段——它们直接决定这次走查的结论。

caliber:
  execution: { entry: next_close, rebalance: 5, annualisation: calendar_365,
               funding: per_contract_events }
  label:     { horizon: 5, transform: cs_rank_zscore, neutralise: [beta, log_dollar_volume],
               neutralise_window: 60, overlap_policy: independent_sample,
               min_width: 40, width_policy: drop }
  allocate:  { quantiles: 5, long_short: true, weighting: liquidity, max_weight: 0.05,
               net_exposure: 0.0, turnover_max: 0.35, min_members: 8 }
  net:       { cost_tiers: [20, 60, 150], slippage: tiered, fee_model: taker_with_funding,
               max_leverage: 1.0 }

boundary:
  time_axis: { mine: 2024-01-01..2026-06-30, oos: 2026-07-01..2026-09-13, holdout: reserved }

verdict:
  impl: sentinel
  params: { dsr_min: 0.95, fdr_max: 0.10, cost_adj_min: 1.0 }
```

注意三件事：

1. `caliber` 里没有 `funding_interval_hours`（I34）；资金费只按事件求和。
2. `target` 与 `portfolio` 里没有任何成本、跨度、权重字段（C20 会拒绝）。
3. `holdout: reserved` 不是区间，是「保留，等 ready 再由 CLI 揭示」。

---

## 2. 编译期会发生什么

`flow run --plan examples/plan_cs_2026q4.yaml` 先编译，不执行。

```
[compile] baseline v1 + plan + overrides → 每叶子标来源
[compile] C1–C22 … 22/22 pass
[compile] supply: cs_top60_2026q3 → 60 members, 4 datasets, 3 pins
[compile] holdout: reserved → not in this run
[compile] digests: plan=a91f… blueprint=7c02… policy=3e8d… domain=b14a…
```

四个锚点指纹写进 `Ticket`。**任何一项算不出来就不开 Ticket**。

---

## 3. 期望目录树（物理布局，`09-app-storage.md` §3.2）

```
{workspace}/
  supply/binance/futures/1d/
    BTC_USDT_USDT-1d-futures.parquet
    streams/BTC_USDT_USDT-1d-funding_rate.parquet
    ...
    _published.json                       # 别名 → 精确版本 + 每份文件哈希

  facts/
    panels/{panel_digest}.parquet         # 60 资产 × 912 bar（2024-01-01..2026-06-30）
    labels/{label_digest}.parquet
    exposures/{call_digest}.parquet       # 每个候选一份
    ledgers/{call_digest}.parquet         # 回测账本（唯一真相）
    reports/{report_digest}.json

  parcels/{run_id}/
    01-Boundary-{digest}/ { manifest.json, *.parquet }
    02-Panel-{digest}/
    … 08-Certify-{digest}/

  runs/{run_id}/
    run.json            # status, exit_code, 起止时间
    trace.ndjson        # 每工位一条
    stations/01-Boundary/{parcel.json, payload/, log/, diagnostics/}
    summary.json        # 应用层只读这个

  db/ledger.jsonl       # append-only：Budget / Reveal / Publish
  db/meta.duckdb        # 可删重建
  cache/  tmp/
```

---

## 4. 每一步的期望输出（`--json` 摘要）

| 步 | 关键字段 | 期望形态 |
|---|---|---|
| Boundary | `members=60`、`masks: observable=60, tradable=41` | 可观测与可交易**不是同一张名单** |
| Panel | `panel_digest`、`dropped_unclosed=1` | 丢弃未收盘那一根，且记数 |
| Target | `label_digest`、`width_min=41`、`dropped_ts=0` | 有效宽度低于 40 的时点被丢（`width_policy=drop`） |
| Search | `candidates=1247`、`trial_family=cs_2026q4_a` | `n_trials` **≥ 1247**（含 `fit` 内评估） |
| Evidence | `stage_a_passed=214`、`stage_b_scored=214` | 粗筛先砍到 214，精算只跑幸存者（`stage_b` 不得大于 `stage_a`） |
| Verdict | `passed=0`、`reason_counts: {dsr_fail:214, fdr_fail:214}` | 「0 晋升」是合法结论；原因计数之和 = 进入裁决的候选数 |
| Portfolio | `legs: long=8, short=8`、`turnover=0.34`、`cost_bp=18` | 换手在上限内，成本双边计 |
| Certify | `folds=5`、`oos_sharpe=…`、`evidence_grade=out_of_sample` | 只有这一步能产出可发布结论 |

如果 `Evidence` 的 `stage_b_passed` 等于 `candidates`，说明粗筛没生效——那不是「全部通过」，是漏斗坏了。

---

## 5. 三处必须失败的演示

### 5.1 命令行改口径

```bash
flow run --plan plans/cs_2026q4_a.yaml --horizon 10
```
```
E1020: argument --horizon is a caliber field; write it in caliber.label.horizon
exit_code=2
```
**不创建 Ticket，不产生任何目录。**（I30 / AC24）

### 5.2 拼错实现名

```yaml
panel: { impl: point_in_time_algin }     # 少一个 g
```
```
E1002: implementation 'point_in_time_algin' not in catalog for layer panel
  candidates: point_in_time_align@1
exit_code=2
```
**不给「最接近的猜测」自动兜底。**（I1 / AC2）

### 5.3 数据版本不适用

```yaml
time_axis: { mine: 2019-01-01..2026-06-30, ... }   # 早于该版本覆盖起点
```
```
E4002: supply version covers 2024-01-01..2026-11-14; requested 2019-01-01..2026-06-30
exit_code=4
```
**不自动补采，不缩短区间。** 采集在流水线之外（I13 / AC17）。

---

## 6. 一次真实的「0 晋升」是什么样

上一份计划在真实数据上大概率得到 `passed=0`，这是**正确结论**，不是故障：

| 量 | 实测/估算 | 含义 |
|---|---|---|
| 最强朴素信号（mom60 @ 5 bar） | 年化净 SR 为 O(0.5~1) 量级（V4 历史实测 ≈0.675，仅作背景；V5 锚点见 §6.1 黄金面板） | 信号量级 |
| 71 试验下的 DSR 门槛 | ≈ 1.4 年化 SR | 多重检验门槛 |
| 净收益 vs 成本 | 2–4 bp/bar vs 18 bp 往返 | 成本吃掉大头 |

处置顺序：**先看信号量级，再看成本假设，最后才谈门槛**。调低 `dsr_min` 或清零 `n_trials` 不叫修系统，叫改判据（`15-operations.md` §4-6）。

一个反例警戒：如果 `empirical_p` 普遍接近 1，那不是「没有 alpha」，是零分布构造错（V4 整行置换保留了资产漂移，中位 `|t|=4.74` 高于观测 `3.46`）。**这两种情形必须能区分**，否则「0 晋升」这句话永远不可信。

### 6.1 验收判定（AC63）

本节走查通过的标准是**两个条件同时成立**：

| 条件 | 内容 | 检查点 |
|---|---|---|
| ① 机制正确 | AC1–AC62 全绿；§5 三处失败演示各有指定错误码与退出码，且 `runs/` 下无新目录；试验计数不少记 | §5、AC47、AC58 |
| ② 已知答案对拍 | 黄金面板上 `mom60@5bar` 全链指标与独立复算相对误差 ≤ `1e-9`，复跑对冻结期望值漂移 ≤ ±10%，且 `n_trials` 等于预注册枚举的实测去重候选数（不依赖 V4 任何内容，2026-11-17 裁决） | `14-test-strategy.md` §4.4、AC48 |

**所以上表的 `passed=0` 满足验收**：它说明系统在正确拒绝，而不是系统坏了。反过来，若出现 `mom60@5bar` 算出 6.75 或 0.0675，即使 `passed>0` 也判不通过——那说明量纲已经错了。判定定义以 `05-contracts.md` §4.1 为准。

---

## 7. 走查覆盖了哪些约束

| 走查环节 | 覆盖的不变量 |
|---|---|
| §1 计划形态 | I22（口径单一来源）、I30（命令面无口径）、I34（无资金费周期） |
| §2 编译 | I1（运行期无兜底）、I10（默认有来源）、I13/I14（供给固化） |
| §3 目录 | I16（日志与产物同目录）、I26（账本唯一） |
| §4 八步 | I17/I18（组合域与可用时间）、I19/I20/I21（候选与计数）、I24/I25（窗口与重叠） |
| §5 三处失败 | I1、I13、I30；AC2、AC17、AC24 |
| §6 零晋升 | I38（计数不少记）、I12（口径一致） |
| §6.1 验收判定 | AC63（机制正确 + 已知答案对拍）；判定定义见 `05-contracts.md` §4.1 |

---

## 8. 不变量与验收

| 编号 | 内容 | 检查点 |
|---|---|---|
| **I56** | 走查样例必须能完整编译并跑出八份 Parcel；文档与实现不一致时以本文档为准修实现，或在本文档记录修正 | CI 跑 `examples/walkthrough/`，产物与 §3 结构逐项比对 |
| **I57** | 三处失败演示必须在 CI 里各有一条自动化用例，且断言「无副作用」 | 失败后目录树与失败前一致 |
| **AC57** | 样例自洽 | 本节计划在 S7 后的实现上跑通；八步输出字段与 §4 表一致 |
| **AC58** | 失败演示有效 | §5 三条各自返回指定错误码与退出码，且 `runs/` 下无新目录 |

AC63 的判定细节见 §6.1；定义与通过标准以 `05-contracts.md` §4.1 为准，本篇不重复定义。

---

## 9. 反模式

1. **把走查写成「理想输出」**：只展示成功的路径，读者无法判断系统会不会拒绝。三处失败演示比成功路径更有信息量。
2. **样例里写 `<区间>` 占位符**：占位符让样例无法被 CI 执行，也就永远发现不了文档与实现的偏差。
3. **用走查结论当研究结论**：这是一份**结构**样例，里面的数字只用来核对形态，不用来支撑任何 alpha 主张。
