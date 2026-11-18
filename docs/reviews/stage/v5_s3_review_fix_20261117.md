# V5 S3 阶段 review 修复报告

- 日期：2026-11-17
- 分支：`v5/layered-impl`
- 范围：`observe` / `verdict` / `search` / `mining` / `inference` 五个模块的独立 review 缺陷修复
- 结论：review 清单 **16 项全部闭合**，新增 17 条守卫测试；v5 全量 **587 passed**（修复前 570）。

---

## 1. 修复清单（按 review 原始分级）

### 统计 / observe review

| 编号 | 缺陷 | 修复落点 | 守卫测试 |
|---|---|---|---|
| P0-1 | `cost_adj_return` 用全市场标签均值 `np.mean(score[mask])`，对所有候选相同 → 成本闸无区分度 | `ops/observe.py` `_longshort()` 返回候选自己的多空组合每期毛收益 `gross_return`；`cost_adj_return = gross_return - turnover*est_cost` | `test_cost_adj_return_uses_longshort_not_market_mean` |
| P1-1 | Stage A 粗筛拒绝的候选仍产 `p_value`，被 verdict 判 `reject`，冒充"已检验"的坏因子 | observe：未过 Stage A 的候选 `p_value/hac_t/sr` 置 `None`；verdict：`stage_a_pass is False` 强制 `watch + insufficient_n`，且不进 BH 族 | `test_stage_a_rejected_candidate_is_watch_not_reject`、`test_stage_a_candidate_excluded_from_bh_family` |
| P1-2 | `block_bootstrap` 未对 IC 序列中心化，强因子零分布自带 alpha，`empirical_p≈0.5` | `_null_tstats()` 中 `centered = ic_finite - mean` 后再做移动块自助 | `test_block_bootstrap_centers_null_distribution` |
| P1-3 | `pvalue_from_t` 的 `df` 用 `effective_n-1`（面板格子数 T×N），系统性压低 p | 改用 IC 有效时点数 `n_ic - 1` | `test_pvalue_df_uses_ic_timepoints_not_grid_cells` |

### search review

| 编号 | 缺陷 | 修复落点 | 守卫测试 |
|---|---|---|---|
| P0 | fitness 调 evaluate 用 `impl_version=registry.operators_version`（白名单摘要），全部 E1002 → 适应度恒 -1 → GP 退化随机 | 改用 qweave 包版本 `_pkg_version("qweave")`（与 `impls.py:47` 注册一致）；`params.operator_set` 仍传白名单摘要（两者语义不同，不可混）。新增 fail-closed：needs_fitness 却全部子调用失败 → E3003 拒绝 | `test_fitness_evaluate_subcalls_actually_succeed` |
| P1-a | `normalise_ast` 把参数位置 `{"const": 2.0}` 归一为 `2`，绕过"带参算子末位必须是整数"约束 | 在 `_canon_number` **之前**校验末位参数必须是 int const；同时加 AST 深度 ≤64 守卫 | `test_param_position_rejects_float_const`、`test_nested_depth_limit_enforced` |
| P1-b | `max_depth` 未真正约束解码（旧代码只靠常量 16 兜底）；交叉/变异后丢弃率约 70% | `decode(genes, max_depth)` 用配置值；初始种群递归生成时守住深度；交叉/变异失败回退父本而非丢弃，记 `repaired` | `test_max_depth_actually_constrains_candidates`、`test_generate_reuses_fit_final_genes` |
| P1-c | `fit()/generate()` 门面分叉：generate 忽略 `fitted_state` 另起随机种群 | `generate_gp` 的 `fitted_state` 保存 `final_genes`；generate 门面据此复现，标 `resumed_from_fit=True` | `test_generate_reuses_fit_final_genes` |
| P2-a | fit 门面绕过 E2009 并发守卫 | fit 门面持 `_FIT_LOCK` | （build 路径已有 `test_search_concurrent_fit_is_e2009`） |
| P2-b | Candidate 缺 `available_from` 字段 | `make_candidate` 加字段（不进 `candidate_id`）；build/fit/generate 三门面透传上层注入的 as-of，不自取墙钟 | `test_available_from_propagates_to_candidates` |
| P2-c | AlgorithmSpec 缺 6 个编译期字段 | 补 `fit_window/output_kind/required_fields/operator_whitelist/max_candidates/budget_group` | `test_describe_exposes_compile_time_fields` |
| P2-d | `calls` 统计是死代码 | 复用为 fail-closed 的 `ok/failed` 计数 | （含在 P0 守卫） |
| P2-e | 畸形 const 抛裸 `TypeError` | `normalise_ast` 统一抛 `ValueError`（evaluate 层据此转 E1003）；修正测试名实不符 | `test_malformed_const_raises_value_error` |
| P2-f | `test_v5_search.py` 有永真断言 `assert ... else True` | 删除并改为 `payload/required_fields` 相等的实质断言 | `test_candidate_id_excludes_generator_and_seed` |

---

## 2. 二次独立复核追加修复

首轮修复后由只读 agent 独立复核，发现 4 处一阶修复未闭合的问题，已全部处理：

1. **DSR 口径冲突**：`observe` 原把**年化** `gross_sharpe` 与面板**格子数** `effective_n` 喂给 DSR；而 `inference.deflated_sharpe_ratio` 明确要求每期 SR。修正为 `_longshort` 额外返回未年化 `sr_per_period` 与真实期数 `n_periods`，`sr`/`sr_n_obs` 改用后者。守卫：`test_dsr_uses_per_period_sr_and_true_nobs`。
2. **Stage A 候选污染 BH 族**：未检验候选的 `p=None` 被 verdict 当 `1.0` 塞进族、抬高他人 `p_adj`。修正为族校正只纳入 `stage_a_pass` 的候选，且未检验候选 stats 不含 `p_value`。守卫：`test_stage_a_candidate_excluded_from_bh_family`。
3. **门面 available_from 漏传**：fit/generate 门面初版未透传 as-of。已补齐。守卫同上。
4. **final_genes 与 candidates 数量对齐**：generate 门面断言 `dropped_decode==0`，确保基因与候选逐条对应。守卫：`test_generate_reuses_fit_final_genes`。

---

## 3. 关键设计裁决（记录备查）

- **`impl_version` vs `operator_set` 不可混**：前者是内核包版本（`qweave==0.6.2`，进 `call_digest`），后者是算子白名单内容摘要（`registry.operators_version`，`evaluate` 校验求值口径）。首轮把两者混用正是 P0 根因。
- **Stage A 是粗筛不是闸门**：未过的候选既不 pass 也不 reject，只能是 `watch/insufficient_n`；不得产 p 值，也不得进 FDR 族。
- **成本与 DSR 共用一条多空收益**：`cost_adj_return`（毛收益−成本）与 `sr`（每期 Sharpe）来自同一条 `ls` 序列，避免两处口径分叉；年化只在展示字段 `gross_sharpe`。
- **available_from 不进内容身份**：`candidate_id = H(payload + required_fields)`，as-of 是谱系/口径元数据，由执行器注入，能力层不自取墙钟（保 AC3 同方案两次执行一致）。

---

## 4. 测试与状态

- v5 全量：`587 passed`（修复前 570，新增 17 条守卫测试）。
- 本轮只处理 V5，范围外测试按用户要求不执行。
- 待提交文件：`capability/{impls,inference,mining}.py`、`capability/ops/{observe,search,verdict}.py`、`capability/registry/params.json`、`orchestration/executor.py`；测试 `tests/tests/test_v5_{search,observe,verdict}.py`（被 `.gitignore` 忽略，需 `git add -f`）。

## 5. 后续阶段（未开始）

S4 backtest/allocate、S5 probe/commit、S6 storage index/gc/reconcile/doctor、S7 certify/combine；executor 真实接入 capability、fit/generate 拆两张调用单属 S3-M6/S5 边界。
