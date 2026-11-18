# V5 验收核对报告（S0–S7 + AC1–AC63）

> 范围：本轮「补充剩余模块 → 开发-review-bug修复-验收核对」四阶段的**第四阶段**。
> 依据：`12-stages.md` 各阶段退出门槛 + `05-contracts.md` §4 AC 清单（编号不重定义）。
> 证据格式：测试名（`tests/v5/`，基线 **662 passed**；全仓 **1413 passed**，80 subtests）
> + 真实跑数字（数据根 `user_workspace/v5_run_20261117`，20 USDT 永续 / ohlcv@1d+funding+OI+basis）。
> 日期：2026-11-18。

---

## 0. 结论速览

| 维度 | 结论 |
|---|---|
| 测试基线 | tests/v5 **662 passed**（本轮 +104：601→662）；全仓 1413 passed |
| S0–S5 退出门槛 | **全过**（S5 有两处已声明的 M2/M3 遗留，见 §5） |
| S6 退出门槛 | **未判**（`doctor`/`gc`/`estimate`/`pipeline` 已交，7 天无人值守等运维项不在本轮） |
| S7 退出门槛 | **机制全过**；AC63 已知答案对拍**未完成**（见 D-003） |
| 真实数据端到端 | 4 次实跑全通（§3）；资金费事件链本轮接通（D-002 修复） |

本轮在验收过程中发现并修复两个实质缺口（正是验收核对的价值所在）：

- **D-001 求不出暴露的候选无痕消失**：Search `count=65` → Evidence `per_candidate=57`，差额 8 个只活在代码注释里。修复：Search 新增声明输出 `unobservable`，编排层逐候选回填 `{candidate_id, reason, detail}`；实测 8 条全部实名落盘（全部为 `E3003 常数暴露`，GP 产物），`65 − 8 = 57` 精确闭合。
- **D-002 资金费事件未进真实成本链**：引擎支持 `taker_with_funding`，但 `live.py` 不构造 funding 数组——即使口径开了，三条成本链（Evidence 逐候选 / Portfolio / Certify）的 `funding` 输入恒 `None`，真实跑 `funding_total=0`。修复：`_funding_grid` 按 `(t_{k-1}, t_k]` **对事件求和**聚合到面板网格（I34：结算间隔 1h/4h/8h 混合，按周期常数乘必错），口径开了而缺数据集/缺标的一律 `E4002` fail-closed；桥的三个 backtest 调用点全部透传。实测（`plan_db_fund.json`，fee_model=taker_with_funding）：Evidence 62 候选中 4 个计入资金费、Portfolio funding=−0.0568（组合净收资金费）、Certify funding=+0.0117。

---

## 1. S0 — 契约冻结

| 门槛 | 证据 | 结论 |
|---|---|---|
| 十二条非法输入全拒 + 退出码正确 | `test_v5_s0_acceptance.py` s0_1/2/5/6/20 + `envelope_ok_exit_code_consistency` + `error_object_has_no_exit_code` | 过 |
| 正向冒烟（八 Parcel、八 trace、exit 0） | `smoke_eight_parcels_eight_traces`、`cmd_run_persists_parcels_and_traces` | 过 |
| 49 错误码无悬空 | `every_error_code_has_layer_and_exit`、`unknown_error_code_rejected` | 过 |

## 2. S1 — 编排真实化

| 门槛 | 证据 | 结论 |
|---|---|---|
| AC1/AC3–AC5/AC7/AC10–AC12 | `plan_digest_is_raw_plan_not_merged`、`deterministic_digests_same_seed`、`o1_replay_in_fresh_process`、`origins_cover_exactly_every_leaf`、`explain_is_readable_and_has_leaf_paths`、`baseline_change_moves_blueprint_and_policy`、`stale_upstream_e3002`、`fingerprint_chain_upstream_change_propagates` | 过 |
| 植入信号过、纯噪声拒、符号翻转结论翻转 | `verdict_passes_genuinely_strong_candidate`、`pure_noise_empirical_p_not_degenerate`、`inference_matches_v4_*` 六例（与 V4 统计原语对拍） | 过 |
| 十次重放逐位一致 | `repeated_digest_is_bit_identical`、`deterministic_digests_same_seed`；真实跑复测：同计划两次执行 `plan_digest` 相同（`12f64dc8` 两次 run 目录）、Search `count=65` 两次一致 | 过 |

## 3. S2 — 供给链真实化（方案 B 形态）

| 门槛 | 证据 | 结论 |
|---|---|---|
| append-only、旧值不覆盖、冲突只计数 | `upsert_keeps_old_values_and_only_counts_conflicts`、`upsert_appends_new_timestamps`、`checksum_is_order_independent_and_stable` | 过 |
| 同内容重复发布命中同一身份 | `ticket_writer_digest_mismatch_and_idempotence`、`object_store_content_addressing_and_schema_sidecar`；文件即事实：`version = checksum[:16]` 读取时现算 | 过 |
| 末根未闭合 → E4003 | `unclosed_tail_is_rejected`、`unclosed_bar_is_observable_but_not_tradable` | 过 |
| 零成功即失败（fetch） | `fetch_fails_when_every_symbol_is_skipped` | 过 |
| 门禁 B（版本/覆盖/时间轴/市场） | `gate_b_*` 14 例（coverage_gap/market_mismatch/frequency_mismatch/end_not_closed/acquired_before_coverage_end 等） | 过 |
| 双市场（spot/futures 不共享文件） | `spot_and_futures_never_share_a_file`、`spot_market_rejects_futures_only_datasets` | 过 |
| 真实端到端 | 20 永续 + 20 现货采集入库；`doctor_reports_clean_supply` / `doctor_surfaces_gap` | 过 |

> 备注：S2 原退出门槛中的「租约/别名/原子发布」已随 `19-supply.md` 方案 B 取消，对应 AC14（别名编译期固化）**不适用**，其职责由「文件即事实 + Pin 现算」承担。AC51 的「租约冲突」同理由此不适用。

## 4. S3 — 搜索与评估真实化

| 门槛 | 证据 | 结论 |
|---|---|---|
| AC20–AC23、AC29、AC37–AC39 | `search_unknown_algorithm_rejected`、`search_concurrent_fit_is_e2009`、`search_reproducible_and_global_rng_untouched`、`candidate_has_no_metrics_or_verdict_fields`、`cross_source_dedup_merges_generators`、`operators_version_is_content_not_package_version`、`fitness_evaluate_subcalls_actually_succeed`、`budget_event_uses_real_search_n_trials` | 过 |
| n_trials 与手工统计一致（一次计数） | 真实跑 `n_trials=70 ≥ count=65`；`two_algorithms_both_run_and_share_trial_count`（多算法共享 fitness/seen）；`verdict_n_trials_from_facts_not_params` | 过 |
| 置换零分布校准 | `pure_noise_empirical_p_not_degenerate`、`drift_panel_time_shift_null_biased_but_cross_section_not`（V4 头号 bug 的行内跨资产置换已修） | 过 |
| 多算法 + 先验池 + 因子库先验（本轮新增） | `test_v5_prior_pool_multialgo.py` 20 例；真实跑 `prior_pool_used=37`（36 库内因子 + mom20）、`db:*` 端到端命中 | 过 |
| **AC34 资金费周期无关性（本轮补）** | `funding_grid_1h_and_4h_events_converge_to_the_same_bar`（1h 8 条 vs 4h 2 条，总额相同 → 同 bar 聚合相等）；`funding_grid_attributes_events_on_bar_open_to_that_bar` | 过 |
| 求不出暴露的候选留痕（本轮修 D-001） | `unobservable_candidates_are_named_not_silently_dropped`、`search_payload_declares_the_unobservable_channel`；实测 8 条实名 + `65−8=57` 闭合 | 过 |

**DSR sigma 口径裁决（本轮落地，未改判据只改口径归属）**：`sigma_mode` 默认 `sampling`（采样误差 `1/√(n_obs−1)`，与 ACCEPTANCE「1.53@255」自洽），横截面离散度降级为诊断字段 `sigma_sr_cross`；`cross_section` 仍可选（`cross_section_mode_remains_selectable`、`unknown_sigma_mode_is_e1004`）。实测门槛落盘 `dsr_benchmark_sr=0.0846/期`（年化 1.617）。**没有下调任何判据**——`dsr_bar_rises_with_trials_not_a_knob` 仍然成立。

## 5. S4 — 成本与成交真实化

| 门槛 | 证据 | 结论 |
|---|---|---|
| AC9/AC24/AC26/AC28/AC33 | `caliber_key_in_params_is_e1003_and_bad_enum_is_e1004`、`label_requires_overlap_policy_when_rebalance_shorter_than_horizon`、`backtest_full_and_summary_share_the_same_fields`、`backtest_summary_with_leverage_still_walks_the_full_ledger`、`production_backtest_calls_ml4t_with_crypto_calendar`、`hourly_time_axis_annualises_with_8760_bars`、`backtest_bars_per_year_follows_the_time_axis` | 过 |
| 两档同源 | `summary_and_full_share_the_same_fields`（ml4t 生产路径） | 过 |
| 时间单位（本轮修） | `gross_return_is_per_bar_equivalent`（÷h）、`gross_sharpe_annualises_by_independent_periods`（√(365/h)）、`label_reports_horizon_bars`；1h 轴年化 8760 已验 | 过 |
| 成本真实化 | `v5_tiered_slippage_costs_are_charged`、`allocate_quantiles_shape_nan_exposure_and_cap`、`allocate_constraint_path_uses_cvxpy_once_and_keeps_position_shape`（AC30 一次 canonicalize）、`s4_backtest_receives_the_panel_time_axis` | 过 |
| **资金费接真实链路（本轮修 D-002）** | `funding_supply_requires_dataset_and_symbols_when_fee_model_asks`（E4002 fail-closed ×2 + 口径未开不读盘）、`bridge_funding_input_is_aligned_or_none`；实测三链计费（§0） | 过 |
| 资金费按事件（AC34） | `funding_only_when_model_and_switch_allow_it` + 本轮 1h/4h 对拍（见 S3 行） | 过 |

## 6. S5 — 单步提交与账本推进

| 门槛 | 证据 | 结论 |
|---|---|---|
| AC4–AC8、AC19 | `probe_changes_none_of_the_four_things`（四项逐项断言）、`probe_does_not_feed_downstream`、`commit_writes_formal_parcel_and_state_without_events`、`commit_is_idempotent_when_payload_is_unchanged`、`commit_is_rejected_for_append_only_stations`、`available_from_propagates_to_candidates`、`reveal_once` / `holdout_reveals_once` | 过 |
| 事件一次性（跨进程） | `EventLedger` 落 `db/events.jsonl`；`holdout_reveals_once` 断言账本单行；`ledger_dedup_across_instances`、`ledger_records_survive_torn_line` | 过 |
| 供给 staleness（本轮新增） | `supply_staleness_detects_checksum_change`（frozen `supply_pins` 重算比对 → E3002）、`supply_staleness_legacy_ticket_is_not_checked`、`supply_staleness_missing_dataset_is_unreadable` | 过 |
| commit 不触发事件（E3006） | `commit_mode_runs_the_chain_but_never_fires_events`、`o6_duplicate_budget_idempotent` | 过 |

**已声明的 M2/M3 遗留（不算作 S5 失败，属下一步工作）**：
1. 能力模式单步（`--live`）对 Search/Portfolio 的内存对象（Candidate/Scope/portfolio）无磁盘重建 → `live_single_step_is_honest_about_missing_objects`（E3003 诚实报缺，不假成功）；Boundary/Panel 等供给型工位已能真跑。
2. 断电恢复演练（kill 后重启不重复计数）未构造测试——账本机制已具备（append-only + 幂等短路），演练脚本未做。

## 7. S6 — 运维化（部分交付，**整体未判定**）

已交：`doctor`（逐 dataset/exchange/market 体检 + `--plan` 比对）、`estimate`（tier/滑点/年化同口径）、`pipeline`（doctor→run，`--force` 显式跳过）、`gc`（引用计数 + dry-run + 删前重核）、`factor_db --retire`、结构化 trace/log 全量落盘。

| 已覆盖 | 证据 |
|---|---|
| AC52 零晋升不误判 | `zero_promotion_is_skipped_not_failed`（`skipped/no_candidate_passed` + exit 0）；真实首跑实测过 `state=skipped` 后经正确口径复跑为 `done` |
| AC53 GC 安全 | `gc_keeps_referenced_and_flags_orphans`（**本轮补**：dry-run 集合 == 实际删除集合；`db/ledger.jsonl` 字节不变） |
| 故障可处置（部分） | 数据缺口 `doctor_surfaces_gap`、末根未闭合 `unclosed_tail_is_rejected`、holdout 已揭示 `holdout_reveals_once`、指纹失配 `tampered_upstream_payload_is_rejected` / `object_store_detects_tampering` |

**未做**（S6 整体不判定的原因）：连续 7 天无人值守运行、日志轮转、缓存容量上限与淘汰（F5）、告警阈值与去重（F7）、磁盘满注入演练、索引 reconcile。

## 8. S7 — 组合域与认证闭环

| 门槛 | 证据 | 结论 |
|---|---|---|
| AC18/AC25/AC27/AC30 | `member_without_verdict_record_is_rejected`、`rejected_member_cannot_be_certified`、`oos_overlapping_portfolio_eval_is_rejected`、`oos_outside_panel_coverage_is_rejected`、`portfolio_must_declare_in_sample`、`certificate_is_out_of_sample_and_manifest_is_deterministic`（两次认证逐字节相同）、`holdout_in_params_is_rejected` | 过 |
| 样本内不得发布 | `promote_gate_on_out_of_sample`（`--min-grade` 闸）、Publish 只认 `out_of_sample` | 过 |
| holdout 只揭示一次 | `holdout_reveals_once`（重复揭示 → `first_time=False` + 账本单行可查） | 过 |
| 认证与组合同 caliber | `_run_certify_folds` 读同一 `policy_view`；真实跑 `reproducible_manifest` 确定性已验 | 过 |
| 因子库闭环（本轮新增） | `test_v5_catalog_ops.py` 27 例：`promote_writes_catalog_and_is_idempotent`（二次 promote `promoted=0, already=36`）、`catalog_retire_keeps_history`、`factors_lists_promoted_entry`、`db:*` 先验回灌（§4） | 过 |

真实认证实测：3 折 OOS（+8.5% / −4.5% / −1.8%，另一计划 1/3 折为正），`evidence_grade=out_of_sample`；本轮 `plan_db_fund` 认证 `net_return=0.107`、`net_sharpe=0.582`（资金费计入后）。

---

## 9. AC 总表（分块）

| 块 | 状态 | 证据锚点 |
|---|---|---|
| AC1–AC2 编译完备/fail-closed | ✅ | `missing_required_is_e1008_not_defaulted`、`unknown_root_key_gets_closest_candidate`、`s0_5_constitutive_override`、`cross_field_rules` 7 例 |
| AC3–AC8 执行/单步/一次性 | ✅ | §2/§6 |
| AC9 口径一致 | ✅ | `backtest`/`verdict` 同 `policy_view.net`；`call_digest_includes_policy_channel` |
| AC10 可观测 | ✅（本轮补强） | 结构化 trace/parcel/log；**`unobservable` 通道让数量差可指名** |
| AC11–AC12 换实现/基线升版 | ✅ | `bridge_resolves_impl_name_not_station_name`（含 `declared=True` E1002 校验，本轮补）、`baseline_change_moves_blueprint_and_policy` |
| AC13 共享仓不可变 | ✅ | `object_store_detects_tampering`、append-only upsert |
| AC14 别名编译期固化 | **不适用** | 方案 B 取消别名表（19-supply.md）；身份=文件 checksum 现算 |
| AC15–AC17 执行期只读/目录归属/供给门禁 | ✅ | `station_cache_readonly`、`zone_literals_defined_only_in_layout`、`gate_b_*` |
| AC18–AC19 组合域/可用时间 | ✅ | §8 |
| AC20–AC23 算法三禁令/多算法/因果配额 | ✅ | §4；`namespace_open_allows_unregistered_name`（先验因子清单加载即自检 E1015） |
| AC24–AC28 口径单一/窗口/重叠/证据等级/两档同源 | ✅ | §5 |
| AC29–AC30 算子指纹/约束复用 | ✅ | `operators_version_is_content_not_package_version`、`allocate_constraint_path_uses_cvxpy_once...` |
| AC31 能力层不碰存储 | ✅ | `capability_has_no_storage_or_path_access`、`v5_never_imports_legacy_packages` |
| AC32 加 Web 不改业务 | ✅（结构上） | V5 无 web 目录；`app/` 只是 CLI 壳 |
| AC33 年化 365 | ✅ | `production_backtest_calls_ml4t_with_crypto_calendar`；`annualisation_used` 恒 calendar_365 |
| AC34 资金费周期无关 | ✅（本轮补对拍） | §4/§5 |
| AC35–AC36 开放列集/股票组件不入侵 | ✅ | `read_rejects_unregistered_field` + registry 开名字空间；`tests/test_data_source_ccxt_only.py` |
| AC37–AC39 fitness 受限/计数不漏/随机隔离 | ✅ | §4 |
| AC40–AC43 注册表 | ✅ | `registry_loads_and_fingerprints_content`、`self_check_rejects_*` 6 例、`evaluate_rejects_*` 13 例（含 `evaluate_rejects_model_ref_payload`、爆炸防护 `evaluate_depth_limit`） |
| AC44–AC46 V4 对拍/回测对拍/删除可追溯 | ⚠️ 部分 | 统计原语对拍已做（`inference_matches_v4_bh_holm/fdr/empirical_p/dsr/hac_t` 六例）；**暴露逐位对拍与回测对拍未做**（需 V4 快照面板，与 AC63 同源） |
| AC47–AC50 拒绝矩阵/已知答案/对拍记录/性能退化 | ⚠️ 部分 | 拒绝矩阵=上述各拒绝测试总和；**AC48 已知答案未做**；AC49/AC50 未立档 |
| AC51 六类故障 | ⚠️ 部分 | §7：租约类不适用；**磁盘满未做**；其余四类有测试 |
| AC52 零晋升 | ✅ | §7 |
| AC53 GC 安全 | ✅（本轮补强） | §7 |
| AC54–AC56 安全 | ✅ | `envelope_rejects_secret_value`、`nested_secret_in_allowed_key_is_rejected`、`secret_and_verdict_still_blocked_with_sink`、`tolist_secret_is_rejected_before_serialization`、`f5_tampered_file_is_miss` |
| AC57–AC58 走查自洽 | ✅ | `walkthrough_excerpt_matches_canonical_example`、`smoke_eight_parcels_eight_traces` |
| AC59–AC62 schema | ✅ | `test_v5_plan_schema.py` 29 例（已删键被拒、文档不手抄、跨字段反例） |
| AC63 双条件 | ⚠️ 机制过/对拍未做 | **见 D-003** |

---

## 10. 缺口记录（本轮后仍开放）

### D-003（P1）AC63 已知答案对拍未完成
```
现象：ACCEPTANCE §1 的已知答案（mom60@5bar 年化 SR≈0.675，容差 [0.6075,0.7425]）
      没有任何测试或实跑覆盖。V5 现有面板（2026 重采）与 V4 快照面板（1124 根）
      不是同一份数据，且 V4 的 0.675 是在旧年化口径下算的，两侧口径需先对齐。
影响：AC63「机制正确 + 已知答案对拍」只有前半句成立；AC44/AC45/AC48 同源受阻。
要求：①把 V4 快照面板（data/binance/futures 的 20 标的 1d）用 `flow fetch --from-v4`
      导入 V5 存储同一窗口；②单候选计划（prior_pool=["mom60"]，双算法 max_candidates=0
      或仅 1 个干扰候选）跑 --live；③比对 mom60 的 gross_sharpe 是否落容差；
      ④容差判据若因年化口径修正（√h 多报已修）需要换算，必须先在文档层改写
      已知答案数值并说明换算，不允许改判据。
```

### D-004（P2）S6 运维项未做
七类未做清单见 §7。其中「磁盘满」无注入点，「告警」F7 仅骨架。**不许用放宽告警换安静**（S6 禁令仍然生效）。

### D-005（P2）S5-M2/M3 遗留
单步对象重建（Search/Portfolio 的 Candidate/Scope/portfolio 磁盘化）与断电恢复演练。`evaluate`/`backtest`/`certify` 独立 CLI 命令**明确不做**（依赖 M2），现给可指路拒绝：`unimplemented_command_explains_dependency`。

### D-006（P3）OI/basis 采集未消费
已入库、无成本模型读取。与 funding 不同，当前没有任何口径引用它们——保持「显式不使用」状态直到有真实成本模型需要（文件头注释已声明）。

---

## 11. 复核记录

- 全量回归：`pytest -q` **1413 passed, 80 subtests**（tests/v5 单独 662）。
- 真实数据四跑：`run_prior`（先验池）、`run_db`（db:* 首验）、`run_db2`（同计划复跑，指纹与候选数逐位一致）、`run_db_fund`（fee_model=taker_with_funding，资金费三链实测）。全部 `state=done`、parcels=8、exit 0。
- 阶段结论：**S0–S5、S7 机制门槛全过；S6 未判定（部分交付）；AC63 对拍未完成（D-003）**。按「排期只认 S、验收只认 AC」，本轮不宣称 AC63 通过，也不调任何判据。
