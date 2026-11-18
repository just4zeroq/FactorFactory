# V5 · combine 完整版 + 三命令接通 + search 分派重构（2026-11-18）

> 用户裁决：先做前三项，索引/GC/供给调度暂不做。
> 结果：`tests/v5` **697 passed**（上轮 686 → +11 净增，其中新增 12 条、改写 2 条过期断言）。

---

## 1. combine 完整版（S7 组合域）

### 1.1 三个合成器（实现名 = `portfolio.combiner`）

| 合成器 | 权重重来源（只读 fit 段） | 关键点 |
|---|---|---|
| `equal_weight` | 每人 1/K | **旧路径逐位保留**（legacy 分支），黄金对拍 2 条仍过 |
| `score_weighted` | `fit` 段池化 IC，**带符号**归一 | 负 IC 成员自动反向；全零退化为等权 |
| `fixed_ridge` | `fit` 段岭回归闭式解 | `λ = 1e-3·tr(XᵀX)/K`，完备案例不插补；奇异时退 lstsq，无随机性 |

三者共用 `combine.build`，按 `call.impl` 分派；`impls.py` 各注册一行，**换合成器就换 impl 指纹**（不是只换一个 params 键）。

### 1.2 正交化与相关上限

- `corr_max < 1`：按 `|fit IC|` 降序贪心保留，与任一已保留成员 `|ρ|>corr_max` 即剔除，逐条写入新输出 `dropped_members{reason=max_abs_corr}`，**不静默丢**。
- `orthogonalise=symmetric`：对保留成员的 `fit` 段相关阵做 Löwdin 正交化 `A^{-1/2}`，权重变 `A^{-1/2}w`；特征值下限 1e-10，共线成员被压平而不是让相关阵奇异（有测试：两个完全共线成员 → 各 0.5，分数有限）。
- 排序键含 `candidate_id`：同 `|IC|` 时按内容定序，避免成员顺序随上游序列化漂移。

### 1.3 修掉的一个真问题：embargo 读了却没用

旧 `_check_window` 读了 `embargo` 但只比 `fit_end` 与 `eval_start`，隔离期等于装饰。现在按 X6 强制 `fit_end + embargo <= eval_start`，违反即 **E1021**（与 s0/04 §3.8 的「三段窗口越界」同码）。18-plan-schema 的 X6 行原写 E1008，已同步改为 E1021。

### 1.4 一处**没有**实现、并写清理由的契约条款

s0/04 §3.8 要求 `fit.start >= max(available_from)`（E1019）。但编排层把候选的 `available_from` 置成**供给覆盖末端**（`live.build_live_supply` 的 `as_of`），该条件在真实计划上永远不成立——实现它等于把所有计划判死。取舍是：**不实现**，并在模块 docstring 里写明；正确修法是让 `available_from` 回到「候选首次可计算日」的语义（需编排层改）。这是本轮发现的**未闭环契约不一致**。

### 1.5 契约改动（都是追加，向后兼容）

| 位置 | 改动 |
|---|---|
| `contracts/plan_schema.json` | `portfolio` 块新增可选 `orthogonalise` / `corr_max`（缺省 none / 1.0） |
| `capability/registry/params.json` | `combine.outputs` 新增 `dropped_members: list`；注释改写为 S7 语义 |
| `s0/04-capability.md` §3.8 | 返回字段补 `dropped_members`；新增三合成器与正交/相关约束的语义表 |
| `18-plan-schema.md` | `portfolio` 行补两键；X6 错误码改 E1021 |
| `09-app-storage.md` | backtest/evaluate/certify 三条命令的参数与输出 |

---

## 2. search 算法分派重构

**旧实现的隐患**：`if spec.stateful → generate_gp else → generate_random`。这等于把「有 fitness」当成「是 GP」——注册表里多一个算法不会报错，它会被当成 GP（参数全错）或当成随机（进化白做），产物看起来正常。

现在：

- `_RUNNERS = {"gp_symbolic": _run_gp, "random_expression": _run_random}`，按**算法名**分派；
- `_config()` 统一把算法参数变 `GPConfig`，`population/generations/max_depth` 校验从「所有算法」收回到「用它们的算法」；
- 注册表有 spec、没有 runner → **E3004** fail-closed（并给出已接列表）；
- 守卫测试：`set(_algorithms()) == set(_RUNNERS)`；另一条测试把 `random_expression` 的 runner 换成 spy，确认 GP 路径不受影响。

新增第三个算法（`template_enum` / `mcts_value`）现在只需要：写 runner + `_algorithms()` 加 spec + 注册表加参数定义，编排层零改动。

---

## 3. 三条单命令接通（evaluate / backtest / certify）

### 3.1 为什么现在能做

`_UNIMPLEMENTED_HINT` 里三条的拒绝理由都是「需要 S5-M2 的对象重建」，而 `CapabilityBridge.rebuild_upstream` 早已实现并跑通。**阻塞理由过期**，本轮把提示字典清空（保留结构给未来命令）。

### 3.2 实现方式

新模块 `v5/app/cli/replay.py`：

| 命令 | 做什么 | 输出 |
|---|---|---|
| `evaluate --on T [--candidate C]` | 重建 Boundary/Panel/Target → 对 Search 候选逐个调 `evaluate` → 算 IC/ICIR/覆盖率 | `evaluation[{candidate_id, expression_digest, series_ref, stats{ic_mean,rank_ic,icir,n_periods,coverage}}]` |
| `backtest --on T` | 重跑 Portfolio 工位（combine→allocate→backtest），读重算出的净值摘要 | `summary{sharpe,cagr_pct,max_dd_pct,turnover,net_return,cost_breakdown,periods,…}` |
| `certify --on T --yes` | 重跑 Certify 工位（逐折 allocate+backtest 回填） | `certificate{metrics,folds_detail,evidence_grade,verdict_refs}` |

三条都**只认冻结 ticket**（无 `--plan`），与 `station` 同一取舍：否则「重算」就是「换参数再算一遍」。

### 3.3 为了让 `read_only` 是真的

契约把 evaluate/backtest 标为 read_only。若为省事走 `execute_station(mode="step")`，会往 `steps/` 落一份 probe 产物，`read_only` 就是假的。所以给 Executor 加了 `persist=False`：`_persist` 与 `_write_trace` 都短路，产物只在内存。测试里硬断言重算后 `steps/` 下没有 Portfolio 产物。

`certify` 仍要求 `--yes`：它的契约等级是 `writes_facts + writes_ledger`，不因「这次实现恰好是重算」而降级。

### 3.4 支撑改动

- `ExecuteResult` 新增 `objects`：单命令要读「这次重算出来的净值/证书」，它们不进 payload，只活在内存对象表；
- `execute_station` 把本工位 `invoked.objects` 并入对象表（之前只并入上游重建结果）；
- combine 尾链新增 `objects["net"]` / `objects["tail_summary"]`；certify 新增 `objects["certificate"]`；
- `_load_committed` 的逻辑提取为模块级 `load_committed_parcel(layout, ticket, station)`，CLI 与执行器共用一套信任规则（重推导 + payload_digest 对拍）。

---

## 4. 测试

| 文件 | 新增/改写 |
|---|---|
| `test_v5_s4_combine.py` | +7（fit-only 防泄漏、带符号 IC、corr_max 剔除、共线正交化、embargo E1021、未知合成器、ridge 压噪声、逐位决定论）；删 1 条「E3004 fail-closed」过期断言 |
| `test_v5_search.py` | +2（注册表=分派表、按名分派） |
| `test_v5_live_cli.py` | +1 端到端（三条命令对同一 ticket：重算摘要/单候选 IC/证书记录 + 缺 --yes 拒绝 + 不落 steps）；改写 1 条过期断言 |
| `test_v5_catalog_ops.py` | 改写 1 条：由「三条不在分派表」改为「声明=分派」正向守卫 |

全量：`tests/v5` **697 passed**；黄金对拍（AC63）与包隔离守卫均通过。

---

## 5. 遗留与风险

1. **E1019 未实现**（见 §1.4）：需要编排层先修 `available_from` 语义，否则实现即误杀全部计划。
2. `combine` 的 legacy 分支保留了一份「旧算法」（`nansum/count`）与新的加权路径并存：等权 + 无条件时走旧路径以保证指纹不变。将来若要让等权也走统一路径，需接受一次全量指纹变化。
3. `fixed_ridge` 的 `λ` 目前是硬编码相对强度，没有暴露到计划（避免多一个可调旋钮）。若后续需要按候选数自适应，应先改文档。
4. 三条单命令都走**完整重算**（`evaluate` 逐候选求值、`backtest` 重跑组合链），在 87 候选 × 1277 bar 上是秒级；但若候选数上千，需要 `--candidate` 收窄或引入缓存。
5. 索引 / GC / 供给调度仍未做（用户明确暂缓）。
