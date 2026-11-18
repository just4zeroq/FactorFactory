# D-003 收口报告：AC63 已知答案对拍（黄金面板，无 V4 依赖）

> 范围：用户裁决「不允许依赖 V4 的任何内容，可以迁移，历史数据直接不要」之后的
> D-003 开发 → review → bug 修复 → 对拍闭环。
> 日期：2026-11-17。数据根：`user_workspace/v5_run_20261117`（V5 自采，ccxt）。
> 测试基线：tests/v5 **671 passed**（本轮 +9：`test_v5_ac63_dimensions.py` 7 例 +
> `test_v5_ac63_golden.py` 2 例生效例，黄金数据缺失时 skip）。

---

## 1. 裁决与锚点重定义

原 AC63 锚点「mom60@5bar 年化 SR ≈ 0.675 ±10%（V4 在 20 资产 × 1124 根 1d 面板实测）」
依赖 V4 数据与 V4 实测值，按用户裁决全部作废。新锚点（文档先行，六处落笔）：

- **黄金面板**：V5 自采（ccxt）20 USDT 永续 1d，窗口 `[2023-01-01, 2026-06-30]`
  （= mine+oos 边界，holdout reserved），1277 bar × 20 标的，逐标的 checksum
  记入 `user_workspace/v5_run_20261117/db/golden_panel.json`。
- **期望值**：手写 pandas 独立复算（不 import `v5`）测定并冻结（§3 表）。
- **容差两层**：流水线 vs 独立复算（同数据同口径决定论量）**1e-9 相对**；
  跨版本重跑对冻结期望值 **±10%**。0.675 只作量级背景（O(0.5~1)），不是判据。
- 文档落笔：`14-test-strategy.md` §4.4 + §5 表（AC44 行同步重锚为 pandas 参考实现）、
  `05-contracts.md` §4.1、`ACCEPTANCE.md` §1、`17-walkthrough.md` §6/§6.1、
  `13-migration.md` §9（AC44/AC45 去 V4 化）、`06-evaluation.md` §5.6、
  `IMPLEMENTATION-PLAN.md`。

## 2. 对拍撞出的四个量纲/对齐 bug（review 实证，全修）

| # | 缺陷 | 证据 | 修复 |
|---|---|---|---|
| **B1 量纲混拼** | `observe._longshort` 的 `ls = Σ w·score` 用**变换后**标签；`transform=cs_rank` 时 `gross_return` 是「秩/bar」，桥再减真实成本（收益单位）→ `cost_adj_return` 量纲错误。旧跑实测 `gross_sharpe=2.674` 就是秩单位 + B4 的产物 | `observe.py` 旧 L209；真实跑 `plan_db`（fee 均为秩单位减收益） | `label` 新增 `raw` 输出（中性化后、变换前的 forward return，registry `label.outputs.raw`）；observe 多空收益只用 `raw`，变换后 `score` 只进 IC（rank IC 语义不变）；缺 `raw` → `E3003` fail-closed |
| **B2 对齐多延迟一根** | `ls = Σ w_t·score_{t+1}`；`entry=next_close` 的成交延迟已编在标签里（`raw_t = p[t+1+h]/p[t+1]−1`），再 +1 就是延迟算两次，且与回测链 `NEXT_BAR`（权重行 t 在 t+1 成交，恰好吃满 `raw_t` 窗口）错开一根 bar | 引擎注释 `backtest_engine.py:58` + `net_return[0]=0` 结构零 | 改为 `ls_t = Σ w_t·raw_t`；`06 §5.6` 公式同步改写（原文 `label_{t+1}` 是按 `entry=close` 写的，对 next_close 不成立） |
| **B3 rebalance 未生效** | `caliber.execution.rebalance` 声明 5，但 allocate 逐 bar 重排、observe 逐 bar 重排——执行链实际每日换仓（实测 `turnover_per_bar=0.107`），「5 根 bar 持有」从未发生 | `allocate.py` 无 rebalance 消费；`06 §3` 明文 rebalance 被 net 读取 | 新增 `stats.rebalance_freeze`：allocate 与 observe 同一网格冻结目标权重（`allocate` 的 `policy_reads` 增 `execution`）；网格行算不出 = 不调仓、持仓不动（数据缺口不斩仓，`dropped_bars` 仍记账）；`rebalance=1` 逐位保持旧语义 |
| **B4 horizon_bars 桥上丢失** | 能力模式下桥重组 label 对象漏传 `horizon_bars` → observe 默认 1 → 年化按 √365 而非 √(365/h)，**√h 倍虚高且不报错**（真实跑 gross_sharpe 2.674 的一半来源） | `capability_bridge.py` 旧 label 分支 | 桥补传 `raw` + `horizon_bars`；observe 对缺失 `horizon_bars` / `raw` 一律 `E3003`（不再静默猜 1） |

**为什么这些 bug 之前没被发现**：合成测试都在 `transform=none` 的世界里（B1/B2 的
效应互相抵消一部分），而 B3/B4 只有真实计划 + 非平凡 transform 才暴露——AC63
「已知答案是用来找量纲错误」的设计意图本身被验证了。

## 3. 黄金面板冻结值与对拍结果

冻结：`db/golden_panel.json`（`freeze_golden.py` 生成；checksum 逐标的记录，
expected 由参考实现测定，pipeline_metrics 回填验收跑实测）。

| 量 | 冻结期望值（独立复算） | 流水线实测 | 相对误差 |
|---|---|---|---|
| gross_sharpe（年化，√(365/5)） | 1.1671935881346232 | 1.1671935881346232 | **0** |
| gross_return（每 bar 等效） | 0.0013472392664799647 | 0.0013472392664799647 | **0** |
| sr（每期，未年化） | 0.13660967655524422 | 0.13660967655524422 | **0** |
| turnover（累计） | 93.16666666666666 | 93.16666666666666 | **0** |
| n_periods | 1271 | 1271 | — |

验收跑：`run 20261117T154943Z-11633943`（`plan_ac63.json`：h=5 / rebalance=5 /
next_close / cs_rank，`prior_pool=["mom60"]` + 1 随机基线，`n_trials=2` = 去重候选数，
八工位 `state=done`，exit 0；Verdict mom60 `pass`——n_trials=2 时 DSR 门槛低，
0.137/期 真实过闸，不是假成功）。

量级 sanity：年化 1.17，是「O(0.5~1)」的正常朴素动量量级；既不是 6.75 也不是
0.0675——量纲无错位。0.675（V4 旧值）与新锚点不可比：不同面板、不同窗口、
不同年化口径（√252 vs √365）、毛 vs 净，只作背景。

## 4. 固化测试（9 例）

- `test_v5_ac63_dimensions.py`（7）：raw 量纲（手算复拍 + 反证）、
  对齐 `w_t·raw_t`（尖峰行断言）、observe/allocate 网格冻结（权重相等 + 丢弃网格行
  持仓不动 + `rebalance=1` 旧语义逐位保持）、B4 fail-closed ×2（缺 `horizon_bars`
  / 缺 `raw` → E3003）、变换不变性链级（IC 变、gross 不变）。
- `test_v5_ac63_golden.py`（2 生效 + 缺数据 skip）：`reference_matches_frozen`
  （参考 vs 冻结 1e-9）、`pipeline_evidence_matches_reference_one_e_minus_nine`
  （流水线实测 vs 参考 1e-9，读 `db/golden_panel.json` 回填的 `pipeline_metrics`）。
- 独立复算参考实现：`tests/tests/test_v5_ac63_golden.py::_reference_metrics`
  （纯 pandas，不 import `v5` 的 observe/label；AC44 的暴露参考实现同源）。

## 5. 对既有口径的影响（如实记录）

- `run_db / run_db_fund` 等旧跑的 gross 指标含 B1+B4 的量纲错误，**不可与旧报告
  数字直接对比**；这些计划的 plan_digest 也会因 allocate 增读 `execution.rebalance`
  而变化（口径进指纹，属预期行为——新实验）。
- Verdict 的 DSR 输入（`sr`/`sr_n_obs`）现在是真实收益单位——此前 cs_rank 下是
  秩单位 SR，DSR 判据在此前跑里同样被污染；本轮黄金跑为修复后首个干净样本。
- `plan_db`（rebalance=3）重跑将得到不同成本/换手（B3 生效后 3-bar 持有）。

## 6. 结论

**AC63 双条件的后半句（已知答案对拍）在黄金面板口径下成立**：机制正确（AC1–AC62
现状见验收核对报告）+ 已知答案对拍逐位一致（5 个决定论量 rel=0）。AC44/AC45/AC48
同源解锁。D-003 关闭；剩余开放缺口只有 D-004（S6 运维）与 D-005（S5-M2/M3）、
D-006（OI/basis 显式不使用）。
