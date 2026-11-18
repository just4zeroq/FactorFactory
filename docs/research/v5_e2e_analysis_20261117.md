# V5 端到端挖掘测试与流程分析报告（2026-11-17）

> 范围：D-004 / D-005 / D-006 三模块交付后的端到端真实数据挖掘 + 流程分析；算法调研另见 `docs/20-mining-algorithms.md`。
> 测试基线：686 passed（tests/v5，只验 V5）。

## 1. 本轮交付（缺口闭合）

| 缺口 | 内容 | 关键文件 | 测试 |
|---|---|---|---|
| D-005 S5-M2 | 单步跨进程对象重建：`rebuild_upstream` 重推导 + payload_digest 逐字节对拍（不一致 E3002）；Evidence/Verdict 结论集直取 payload；Search 不重跑（append_only）；np.float64 指纹双层修复（`_encode_float` 归一 + `_jsonable` `.item()`） | `orchestration/capability_bridge.py`、`contracts/digest.py` | 八工位单步 payload 与整体 run 逐字节一致（此前 Evidence 之后单步 E3003 fail-closed） |
| D-005 S5-M3 | 断电恢复：commit 幂等分支补齐 state.jsonl 缺条（`state_recovered=true`） | `orchestration/executor.py` | 两段写入间崩溃演练通过 |
| D-004 F5 | Cache 容量护栏：LRU 键表（OrderedDict，默认 1024 条），淘汰只丢键表不动文件 | `facilities/cache.py` | 容量/命中顺序/驱逐计数 |
| D-004 F7 | 告警去重限流：`dedup_window_seconds` + `max_per_window`，时钟注入，sink 失败不更新 last_emit | `facilities/notify.py` | 窗口内抑制/过期重发/限流 |
| D-004 日志 | StructlogLogSink 按 32MB×2 轮转（`.jsonl→.1→.2`） | `facilities/log.py` | 轮转演练 |
| D-004 磁盘满 | `write_bytes`/对象写入 OSError → E5001（exit 4，hint 先归档再 GC），不再落 E3004 | `runtime/store.py`、`storage/objects.py` | os.replace 打点注入 |
| D-004 reconcile | 三方核对（登记/产物链 payload digest 对拍/派生 ref 对象），损坏 E2005 exit 3，violations 进 error.context | `app/cli/reconcile.py` | 干净过/篡改检出 |
| D-004 告警 | pipeline run 后按 15-operations §3 评估（零候选 / n_trials 突降>50% / DSR 全失败），去重投递并写进信封 `alerts` | `app/cli/pipeline.py` | 零候选 / 突降触发 |
| D-006 | OI/basis 事件字段注入：as-of 前向填充（`searchsorted(side="right")-1`），无数据标的全 NaN；live 供给 `available_fields` 含事件字段 | `app/cli/live.py` | as-of 语义 / 外标的计数 / no-op |

命令规格新增 `reconcile`（12 条业务命令，READ_ONLY、exit 3 on 损坏）；旧 ticket（20 张）declared digest 因 np.float64 修复**永久失真**，reconcile 如实报 mismatch，重跑即修复，值本身不受影响。

## 2. 端到端首轮实测（宽松判据，run 20261117T164232Z-a8d268bc）

plan_e2e：binanceusdm/futures crypto20 1d，四数据集（ohlcv+funding+OI+basis），prior_pool=[db:*, mom20, mom60, funding_carry, oi_change20, basis_carry]，双算法（gp_symbolic + random_expression）。

- 逐工位全绿：Boundary→Panel→Target→Search→Evidence→Verdict→Portfolio→Certify 八 parcel。
- Search：count=74（先验 5 + gp_symbolic 31 + random_expression 38），n_trials=87（跨算法去重），unobservable=12 全实名（oi_change20 等 E3003 常数/无有限值），65−12=62 闭合。
- Panel field_manifest 含 basis/funding_rate/open_interest（各有 supply_version）——D-006 事件字段真实进面板。
- Evidence：Stage A/B 两阶段，funding_carry 覆盖 0.991、basis_carry 0.941。
- Verdict：4 pass / 58 reject（本报告 §4 有重大更正）。
- Portfolio：4 members，fee=0.435、funding=0（fee_model=taker 正确）。
- Certify：walk-forward OOS 3 折，net_return=17.2%、net_sharpe=0.862。

### 2.1 已确认的流程问题（非 bug，数据事实）

**oi_change20 不可观测是数据源事实**：OI parquet 仅 25 行（2026-10-21~11-14，当初只为成本估算采集），覆盖不了面板窗口（2023-01 起）；funding 全量（2023-01 起）故 funding_carry 可用。ccxt 的 OI/basis 历史受限。系统行为正确：unobservable 实名暴露，不造零、不静默丢弃。若要 oi_change20/basis_carry 全量覆盖，需要补采且历史长度受交易所 API 限制——这是采集层限制，不是流程缺陷。

## 3. 【P0】判据误标：所有 run plan 的 dsr_min=0.05 违反文档口径

**这是本轮流程分析最重要的发现，属 plan 编写错误，不是 verdict 代码 bug。**

- 文档 `s0/03-runtime.md` L278 明文：**"`dsr_min` 是概率阈值（0.95）"**；示例 plan（`examples/plan_cs_2026q4.yaml` L78）为 `dsr_min: 0.95, fdr_max: 0.10`。
- 实测 `user_workspace/v5_run_20261117/` 下**全部 7 份 plan**（含 plan_e2e）均为 `dsr_min: 0.05`——0.05 是 p 值量级的数字被误标成概率阈值。DSR 是概率（`deflated_sharpe_ratio` 返回 0.5·(1+erf(z/√2)) ∈ (0,1)），`dsr_min=0.05` 等于"只要 5% 置信就放行"，闸门形同虚设。
- 这正是 `15-operations.md` §4-6 红线禁止的「为有产出而下调 dsr_min」（虽属无意），首轮 E2E 的「4 pass / OOS 17.2%」结论不可信。
- verdict 代码经核对无 bug：闸逻辑 `dsr < dsr_min → dsr_fail`（verdict.py L333）正确；statistics 的 `dsr` 是 verdict 重算后的概率值，`dsr_benchmark_sr/sigma_mode` 在逐候选 `statistics` 里（每候选各自 n_obs 决定 σ，设计正确）。

**按文档口径复核首轮结果**：62 个已检验候选中 61 个会 dsr_fail；唯一过 DSR 闸的是 `mom60`（60 日动量，`(close − ref(close,60))/ref(close,60)`，prior 池来源，DSR=0.9853；其 p_adj=0.0262、emp_p=0.0297、cost=0.00109 均满足其余闸）。

**处置**：用文档口径（dsr_min=0.95、fdr_max=0.10）重跑端到端——方向是**抬高**判据回文档值，与红线一致；结果见 §4。所有既有 plan（plan.json/plan_ac63/plan_db/plan_db_fund/plan_e2e/plan_prior/plan_spot 共 7 份）的 `dsr_min: 0.05` 都应视为误标，后续使用前先更正为 0.95。

## 4. 端到端严判据复跑（dsr_min=0.95 / fdr_max=0.10，run 20261117T165601Z-04b85fd7）

plan_e2e_strict（同供给/同搜索配置，仅判据回文档口径），state=done、八 parcel、exit 0：

- **Verdict：2 pass / 59 reject / 1 watch**（首轮宽松判据为 4 pass / 58 reject）。两个 pass：
  - `mom60`（prior 池，`(close − ref(close,60))/ref(close,60)`）：DSR=0.9853、p_adj=0.0272、emp_p=0.0198、cost=0.00109/期；
  - `gp_symbolic@1` 发现的 `sum(ref(basis,6),10)`：DSR=0.9932、p_adj=0.0256、emp_p=0.0297、cost=0.00087/期。常数 +10 不改变截面秩相关，实质是 **6 日滞后 basis 水平（basis carry）**——搜索在严判据下独立重新发现了经典基差动量，这是流程有效性的正面证据。
- **Portfolio：3 members**（两个 pass + 1 个 watch 各 1/3）。watch 候选是 random_expression 的 `max(div(high,basis),10)`，`tested:false`（Stage A 粗筛未过、从未被完整检验）。**准入实现与契约一致**：`combine.py` L5/L32 明示只做 pass/watch 等权（s0/04 L575「若无 pass/watch 成员则 skipped」），不是 bug；但见 §5.4 设计观察。
- **Certify：evidence_grade=out_of_sample**，OOS 2025-07-01~2026-06-30 共 359 bar、3 折：**net_return=16.42%、net_sharpe=0.789、max_drawdown=-17.4%、turnover=0.068/bar**，成本合计 0.079（fee 0.079、funding 0、slippage 0），positive_folds=2/3。较首轮（17.2%/0.862、4 成员）略降——差异主要来自成员构成（去掉了两个弱判据候选、加入一个未检验 watch）与零分布重抽。
- **告警**：无（`alerts` 挂在 `pipeline` 命令信封；本轮 2 pass、n_trials 稳定，三条规则均未触发；触发路径有单测覆盖）。

**跨 run 观察**：同一配置两次运行中 mom60 的 emp_p 0.0297 vs 0.0198——零分布置换抽样按 ticket 种子派生，同 ticket 内单步重建逐字节一致（S5-M2 测试已验），跨 ticket 的 p 值在 ±1/(n+1) 粒度上浮动属置换检验的正常统计性质，不是不可复现缺陷。

## 5. 流程分析结论

1. **契约与 fail-closed 有效**：unobservable 实名、空集 skipped(no_candidate_passed)、E4002 零成功即失败、E3002 digest 对拍、E5001 磁盘满、E2005 reconcile——全部在真实数据上按设计触发或可用。
2. **单步跨进程重建闭合**（S5-M2）：八工位单步与整体 run payload 逐字节一致，断电恢复（S5-M3）有演练覆盖。
3. **判据参数是人的输入，系统不会替你拦**：schema 只约束 `0 < dsr_min ≤ 1`，语义对错（概率阈值 vs p 值）文档说了但没人机校验。建议后续在 plan 编译时加一条**只读警告**（`dsr_min < 0.5 时提示 DSR 是概率阈值、文档口径 0.95`）——警告不拦（红线说阈值是人的决定），但要让人看见。
4. **【设计观察，未改】untested watch 拿满权重**：本轮严判据下一个 `tested:false`（Stage A 未过、从未被完整检验）的 watch 候选拿到组合 1/3 权重并进了 OOS 认证。实现与契约一致（combine 只认 pass/watch、s0/04 L575），但「证据不足」与「未检验」两种 watch 在准入上无差别——建议后续区分：untested watch 要么不入组合、要么权重上限受控（属契约演进，需先改文档再改码）。
5. **0 晋升/少晋升是诚实结论**：严判据下只有经典动量与 basis carry 过关，符合加密市场先验；「让更多候选过」的唯一正道是更好的因子，不是调门槛。
6. **OI/basis 补采受数据源限制**：历史长度受交易所 API 约束，oi_change20/basis_carry 在长面板上覆盖不足是采集层事实；Panel 的 field_manifest 已带 coverage，运维上不要误以为是 bug。

## 6. 遗留与建议

- 旧 ticket 的 declared digest 失真：重跑即修复，无数据影响；`flow reconcile` 会持续如实报告直到重跑。
- 建议补 plan 编译只读警告（§5.3，未实现，属增量建议）。
- S5-M2/M3 之外的两个旧遗留不变：事件账本仍内存（AC8 跨进程不成立）、stale 传播未接单步（S6 后续）。
- 算法调研结论见 `docs/20-mining-algorithms.md`（gp_symbolic / random_expression 已实现；NN/RL/LLM 不进 Search 的理由；扩算法五步清单）。

## 7. 单因子完整链路检验（2026-11-18 补，用户裁决：后续挖掘都走完整链路）

两个 pass 因子逐个走「隔离复跑 → OOS 认证 → 入库 → 回灌」全链路；流程已固化为 `docs/20-mining-algorithms.md` §6（强制六步）。

| 因子 | 隔离 run | Verdict | Portfolio | Certify OOS（3 折/359 bar） | 入库 |
|---|---|---|---|---|---|
| mom60 | 20261117T223442Z-03143f7e | pass（DSR=1.0@n_trials=2） | 单成员 1.0 | **net_return 0.71%、sharpe 0.177**、positive 1/3 | already_in_catalog |
| basis carry | 20261117T223503Z-8b028903 | pass（DSR=1.0）/ 陪跑 random reject | 单成员 1.0 | **net_return 18.80%、sharpe 0.919**、positive 2/3 | promoted（db:gp_symbolic:064db084） |

**归因结论（本轮最重要的增量发现）**：组合认证（§4：16.42%/0.789）的收益几乎全部来自 basis carry；mom60 在 OOS 窗口单独贡献接近零。等权组合掩盖成员归因与集中度——这就是完整链路必须包含单因子隔离的原因。后续改进方向：组合层正交化/相关性约束（S7 完整 combine 范畴）。

边界说明：隔离复跑 n_trials≈2（去重后 2 候选），DSR 门槛随之降低是诚实设计；显著性证据以全量跑（n_trials=87）为准，隔离跑承担链路机械验证与归因，不作为显著性证据。live 模式强制 ≥1 算法（E1004），故以 1 个最浅 random_expression 候选陪跑（basis 跑中陪跑候选被正确拒绝：DSR=0.1405）。
