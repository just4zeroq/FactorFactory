# V5 修复执行报告（第一批）

- 时间：2026-11-18
- 基线：`tests/v5` **697 passed** → 本轮结束 **726 passed**（+29）
- 规划依据：`v5_fix_plan_20261118.md`
- 真实复跑对拍：同一 plan 三次跑（`4d2c9ca6`），`plan/policy/blueprint/caliber/domain` 五个指纹逐位不变
- **未提交**：改动仍在工作区（与此前 S4/S5/S7 遗留变更一起）

---

## 一、完成了什么

### A 组 · 对账与契约正确性（5/5）

| # | 改动 | 验证 |
|---|---|---|
| A1 | `certify` 清单的 `policy_digest` 改名 `policy_view_digest`（如实描述），真 `policy_digest` 由编排层在 `_run_certify_folds` 回填 | 端到端断言 `manifest.policy_digest == ticket.policy_digest`；实测 `sha256:39bfec07…` 对账成功 |
| A2 | 新增 `layout.FACT_KINDS` 唯一白名单，`fact_kind` / `index` / `gc` 三处引用同一常量 | 守卫测试断言三者同源；索引从此覆盖 `parcel_data` |
| A3 | `_collect_params` 补 `portfolio`（`allocate` 不是 plan 顶层块，已在注释说明） | 新增 E1020 用例：`portfolio.params.cost_tiers` → 拒 |
| A4 | `plan_schema` X6 错误码 `E1008` → `E1021`（与 `combine._check_window`、18-plan-schema、s0/03 C21 对齐） | 跨字段用例改断言 |
| A5 | `ledger.append` 锁内重扫结果回并 `self._seen` | 新用例：两实例交叉写后视图一致 |

### C 组 · 因子重复检测（4/4，含契约变更）

**文档先行**（s0/04 §3.6/§3.7、06-evaluation §9.1、18-plan-schema 未改因为 `evidence.params` 是开放 map）

| 落点 | 内容 |
|---|---|
| `observe.params.dedup` | `{corr, method, rep_by}`，**缺省 `corr=1.0` = 关闭**，既有计划行为逐位不变 |
| `observe` 输出 | 新增 `redundancy{enabled, threshold, method, rep_by, n_candidates, n_effective, n_unobserved, clusters, max_pair_corr}` |
| 每候选 metrics | 新增 `redundant_of`（同簇代表 id，代表自身 `null`） |
| `verdict` | 新增 `reason_code = redundant` → **`watch`**（不是 reject）；`statistics` 新增 `n_effective_trials`（诊断，**不进判据**） |
| `_decide` | 重写为「先剔软原因、看还剩什么」，同时消掉原来的不可达分支 |

**真实数据效果（45 候选）**：

```
n_candidates=45 → n_effective=10
簇1: 32 个成员（GP 反复产出同一信号），min|ρ|=0.903
簇2:  6 个成员，min|ρ|=0.913
其余 8 个单成员
max_pair_corr = 1.0   ← 存在 score 完全相同的不同 candidate_id
```

这是 `n_trials` 虚高最直接的证据：真实独立试验只有 10 个，记账写了 45（verdict 侧 51，含被淘汰个体）。

### D 组 · 评估指标补全（3/5，D2/D4 未做）

| # | 改动 |
|---|---|
| D1 | `inference.pearson_ic_row` + `ic_series(method=)`；`observe` 新增 `pearson_ic`（只在被请求时算）。**`ic` 的 Spearman 语义不动**，否则历史对比全失去基准 |
| D3 | `inference.sample_shape()`（偏度/峰度，非超额）；`_longshort` 返回多空序列；`observe` 新增 `skew`/`kurtosis`；`verdict` 改为优先读这两个（`net_skew`/`net_kurtosis` 保留回落）。**DSR 的肥尾修正从此真正生效** |
| D5 | 删除 `verdict` 的 `ir`/`n_obs` 回落分支（V5 全仓无生产方，永不命中） |

实测：DSR 在全部 45 个候选上都变了（修正生效），但 decision 分布不变（43 reject / 2 pass）——说明修正是保守的，没有翻转结论。

### B 组 · 回测与成本口径（4/5，B4 未做）

| # | 改动 | 验证 |
|---|---|---|
| B1 | `_price_frame` 允许 NaN：单格缺失 → 不建价不成交；**整列缺失**（标的从未存在）与**整根 bar 全缺失**（时间轴断裂）仍 E3003。新增 `missing_price_cells` 落盘 | 三个用例分别覆盖容忍/整列拒/整行拒 |
| B2 | `ops/backtest` 对总敞口 > 1 显式 E1004（它没有保证金与强平模拟）。原先四个口径字段只是校验、不参与计算 | 新用例断言 E1004；原杠杆用例改为「声明允许但不超」 |
| B3 | `caliber.net.slippage_bp`（可选，单边 bp）：给出就覆盖 `fee/2` 派生规则，`slippage=none` 仍为 0 | 10bp 与 30bp 的滑点成本正好 3 倍；缺省路径逐位不变 |
| B5 | `gross_bar` 长度只允许「多」不允许「少」；少了直接 E3003，不再尾部补 0 | — |

### 其它

| # | 改动 |
|---|---|
| F2 | `objects.write_table` 显式固定 `compression="snappy" / version="1.0" / write_statistics=False`，让「同表两次落盘字节一致」不再依赖 pyarrow 默认值 |
| E2 | `CALIBER_FIELDS` 改为从 `plan_schema.caliber_field_names()` 派生（新增公开函数），消灭 24 字段的手抄清单 |
| E3 | `catalog._STATION_SEQ`（无用序号）→ `_STATION_NAMES` 元组 + 守卫测试断言与 `STATION_SPECS` 一致（storage 不反向 import orchestration） |
| G13 | `combine` legacy 路径的 `fit_ic` 由 `0.0` 改 `None`：区分「没拟合」与「拟合得 0」 |
| H1 | `inference` 两处 docstring 的 `sigma_sr` 口径**写反了**（说横截面离散度，默认其实是采样误差），已改正并注明「两处必须同时改」 |
| H2 | `layout` docstring 的「九区」与实际 11 个名字对齐，并说明「四区是权限、九区是文档说法」 |
| H3 | `read._check_frequency` docstring 写明能力边界：拦得住「1d 声明成 1h」，**拦不住**「1h 里混 4h」（与「连续 3 根缺失」同形） |

### 本轮新发现的真 bug（review 漏项）

**`supply.market` 不是合法 schema 键**。代码里 `gate_b`（门禁 B 第三道护栏）、`common`、`station`、`_check_cross_field`（spot 不含 funding/OI/basis）全都消费它，但 `plan_schema.json` 的 `supply.keys` 没有这一项 → **任何写了 `market` 的计划在编译期直接 E1001**。门禁 B 的市场维度护栏因此永远走不到。

已补：`market: enum{spot, futures}`，`required: false`（缺省 `futures`，与代码一致）+ 三个用例（合法值 / 非法值 / spot 拒合约数据集）。

这也解释了为什么此前的 7 份 plan 能跑——它们都没写 `market`，走缺省。

---

## 二、没做什么，为什么

| 项 | 理由 |
|---|---|
| **B4** 真实 symbols 进 backtest | 核实后收益低于预期：symbols 只影响 ml4t 内部 fills dataframe 的 `asset` 列，而 `fills_ref` 落盘的是**数组**（不含名字）。改了不改变任何产物，等 `ledger` 真落盘标的名时再做 |
| **D2** 分组单调性 / **D4** IC 衰减 | 都是新增指标（非 bug），要扩 `metrics` enum + 文档 + 用例，适合单独一批 |
| **G1/G2** S 标签（`@S3`/`@S4`） | 这些字符串进 `domain_digest` / parcel digest。改它们 = 一次全链指纹失效，收益只是字符串语义。属"新试验族"级别操作，需要单独决策 |
| **F1** 缓存接线 | 规划里已定为「本轮不接」，把 F5/F6 从 S6 验收口径划掉 |
| **E1/E4/E5/E6/E7/E8/E9/E10/E11**、**F3**、**G3–G12/G14**、**H4–H6** | 纯清理与文档，不改变行为，可随时插入。`E1`（两套 `Call`）改动面最大，建议单独一批 |

---

## 三、需要记录的分界点

1. **F2 让所有 parquet 对象的 digest 变了**。`facts/` 下已有对象与新写入的同一个逻辑表会有两个 digest（内容字节不同，但那是序列化差异，不是数据差异）。
   - 不影响读取：`read_table` 按 ref 的路径读。
   - 影响去重：同一张表会被存两份。
   - **处理建议**：把这次当作显式分界点，在迁移日志里记一笔；若要彻底干净，重建一次 `facts/` 与 `db/meta.duckdb`。

2. **A1 让证书的 `reproducible_manifest` 多两个键、少一个旧名的键**。历史证书里的 `policy_digest` 是旧口径的 `H(policy_view)`，与新证书的同名字段**不可直接比较**——对账时以 `policy_view_digest` 比对旧值。

3. **C 组的 `n_effective` 是诊断量**。看到 `n_effective << n_trials` 时不要「顺手」把 `n_trials` 改小：那是下调判据（DSR 门槛按 `√(2 ln n)` 上升），属于明令禁止项。

---

## 四、下次接续的推荐顺序

1. **D2 + D4**（分组单调性、IC 衰减）—— 补完评估维度，能在现有 `dedup` 之上判断"这个独立信号是否可用"
2. **E1 两套 `Call`** —— 改动面大但收益持久（每读一次代码就踩一次）
3. **E4/E5**（bridge 四个 `scoped`、executor 两段 parcel 构造）—— 消灭"改一处忘一处"的结构性风险
4. **E6**（`search.fit/generate` 与 `build` 语义分叉）—— 要么归一，要么删门面改文档
5. **G1/G2 S 标签** —— 攒一次集中迁移，与下一次 `caliber` 变更一起做

---

## 9. 第二批：D2 分层单调性 + D4 IC 衰减（2026-11-18 续）

### 9.1 做了什么

| 项 | 落点 | 内容 |
|---|---|---|
| D2 | `ops/observe.py` · `_group_stats` / `_group_config` | `groups: int`（2..20，**无默认值**）+ `group_returns` / `monotonicity` / `group_periods` |
| D4 | `ops/label.py` · `forward_raw` / `build_raw_by_h`，`ops/observe.py` · `_decay_config` | `observe.ic_decay: list[int]`（单位 bar）+ `ic_decay: {跨度: 平均 IC}` |

**D2 的口径**（与 `_longshort` 对齐）：同掩码 `valid ∩ label_valid`、同**变换前** `raw`、同除 `h`（每 bar）。**不冻结再平衡网格**——此处测的是「分数 → 收益」的截面映射，混进持仓惯性会让同一因子在 `rebalance=5` 下显得"更单调"。每期按名次 `array_split` 分组（同分不挤边）；某期有效标的 < G 时**整期跳过**（空组冒充「收益 0」会把单调性稀释向 0）。

**D4 的口径**：多跨度标签由 `label` 产，`observe` 不另算一份未来收益。为此把 label 里的 `forward_return + 中性化` 抽成 `forward_raw`，把保留区触达抽成 `_holdout_touch`，主标签与衰减共用。衰减**不做重叠抽样、不做宽度掩码**（同一批时点才能比跨度），每个 h 按自己的 `reach = offset + h` 重算 `~touch`。

### 9.2 纠了原规划里的一处说法

原规划写「IC 对『中间层排序正确、两头反了』几无分辨力」。实现时发现 **Spearman IC 对这类形状其实很敏感**（会塌到 ≈0），两者真正的分叉在别处：

> rank IC 对收益的**任何单调变换**逐位不变，而 `group_returns` / `gross_return` 会变。

测试 `test_group_returns_expose_shape_that_rank_ic_cannot_see` 用「把最高分组收益放大十倍」钉住这一点：IC 相等（±1e-16）、`group_returns[-1]` 差一个量级、`gross_return` 差一个量级。文档同步改成这个说法。

### 9.3 可执行的不变量

`test_build_raw_by_h_matches_label_raw_at_the_caliber_horizon`：衰减在「口径跨度」那一档与 `label.raw` **逐位相同**（`rtol=0, atol=0, equal_nan=True`）。谁哪天在衰减里自己写一遍 `forward_return`，这条断言就红。

真实复跑也验证了同一条：`plan_diag_probe.json`（口径 h=5、`ic_decay=[1,5,20]`）的 Evidence 产物里 `decay["5"] == ic == 0.03926495957984232`。

### 9.4 真实复跑结果（run `20261118T013637Z-41f374ed`）

- 51 候选（上轮 45；差额来自 `db:*` 先验池随运行增长，非本次改动）
- `ic_decay`：`{1: 0.0142, 5: 0.0393, 20: 0.0618}` —— 长跨度更强，符合动量类信号
- `monotonicity` 全域 [-0.8, 1.0]；样例候选 `ic=0.0393 / monotonicity=0.9 / group_periods=1211`，其 `group_returns` 为 `[-1.2e-4, +2.6e-4, +1.8e-4, +8.3e-4, +1.28e-3]`——**最低组为负、第二组高于第三组**，正是 IC 看不出来的形状
- Verdict：2 pass / 49 reject，`n_trials_used=59`；Portfolio 2 成员；Certify `net_return=0.2765283449027809` / `net_sharpe=1.042087860539796`（与上轮逐位一致）

### 9.5 新增的分界点

`_METHODS` 多了三项（`group_returns` / `monotonicity` / `ic_decay`）→ `method_ids` 恒多三个键 → **Evidence payload / parcel digest 变化**（与 C 组加 `dedup` 同类）。不阻断任何判据，但历史 parcel 与新 parcel 的摘要不可直接比对。

### 9.6 测试

`tests/v5` **754 passed**（740 → 754，+14）：D2 侧 8 条（含「IC 相等但组收益差量级」「跳过过窄期」「不可测不给 0」）、D4 侧 4 条 + 参数化 8 例（非法跨度）、label 侧 2 条（单一实现不变量、按 h 收紧保留区掩码）。

---

## 10. 第三批：E1 统一 `Call`（2026-11-18 续）

### 10.1 调查结论：不是「两份定义」，是「一份死的」

规划里写「两套 `Call`：`contracts/types.py` 16 字段 vs `capability/runtime.py` 12 字段」。查使用方后发现更精确的结论：

| | `contracts/types.py::Call` | `capability/runtime.py::Call` |
|---|---|---|
| 生产代码构造点 | **0 处**（只有一条 `from_dict` 往返测试） | `bridge` / `executor` / `cli` / 全部测试 |
| `inputs` | `list[Ref]`（无载荷） | `tuple[InputRef]`（带只读 `data`） |
| 三个子摘要 | 存字段 | 现算 |
| 身份字段名 | `call_digest` | `digest` |
| 校验 | 非空串、`phase` 枚举、映射、`Ref` 类型 | **无** |
| 序列化 | `to_dict` / `from_dict` | 无 |

所以真正的代价不是"冗余"，而是**看代码的人无法知道哪个算数**：契约层的表是文档口径（`call_digest`、必填 16 项），能力层的类是运行口径（`digest`、可选 3 项、零校验），而且**校验更强的恰恰是没人用的那份**——`phase="fitted"` 在契约层是 `E1004`，在真实路径上一路跑到底。

### 10.2 改法

1. **删除** `contracts/types.py::Call`（含 `from_dict`/`to_dict`、`__all__` 与包级导出），原位留注释说明为什么只有一份、契约层只持久化 `Parcel.call_digest`。
2. **`capability/runtime.py::Call` 成为唯一定义**：
   - 字段 `digest` → **`call_digest`**（与文档表、`Parcel.call_digest` 同名；不再"一个东西两个名字"）；
   - **搬来被删那份的 `__post_init__` 校验**（非空串、`phase ∈ CALL_PHASES`、三个映射、`seed` 类型、`composed` 类型），并允许 `inputs` 里出现 `None`——它是**可选输入的位置占位符**（`ABSENT_DIGEST`），与"少传一项"是两件事；
   - 三个子摘要改成**派生属性** `params_digest` / `policy_view_digest` / `runtime_facts_digest`，不存字段（存了就是能与本体分叉的副本）；
   - 口径视图那个属性**如实叫 `policy_view_digest`**，不叫 `policy_digest`——后者是编排层持有的真值（含 `caliber_digest` 与构成性子集，能力 `Call` 里没有这两个通道）。这一点是 review 时自己撞出来的：第一版写成了 `policy_digest`，正是 C 组刚清掉的那个坑。
3. `CALL_PHASES` 留在契约层（它是契约枚举），能力层 import 它做校验。

### 10.3 验证

- **`call_digest` 逐位不变**：同一份计划 `plan_diag_probe.json` 在 E1 前后各跑一次，`Boundary` / `Panel` / `Target` / `Search` 四个工位的 `call_digest`、`digest`、`payload_digest` **全部 IDENTICAL**。（公式没动，改的只是字段名与校验位置。）
- 新增两条守卫测试：
  - `test_call_has_exactly_one_definition`：AST 扫全 `v5/`，`class Call` 只允许出现在 `capability/runtime.py`。再抄一份就红。
  - `test_sub_digests_are_derived_not_stored` + `test_phase_must_be_a_known_value`：前者钉住"派生不存储"，后者钉住"以前只在死类型里生效的校验现在真的生效"。
- `test_empty_impl_is_rejected_not_fallen_back_to_single` 的期望从 `E1008`（跑到 `run`）改为 `E1004`（构造时）——**更早断**，语义不变；`resolve()` 里的空 impl 分支保留作后备。
- `tests/v5` **756 passed**（754 - 1 条失效的 `contracts.Call` 往返测试 + 3 条新守卫）。

### 10.4 观察到的运行时间

`plan_diag_probe.json` 的 Evidence 一步 **532.9 s**（51 候选 × `n_permutations=20`，零分布占绝大部分）。D4 的 `ic_decay` 每个候选多两次全样本 IC（跨度 1 与 20；跨度 5 与主 IC 同源但走同一条实现所以照算），实测约 +10%，不是主因。

一次复跑在 `Evidence` 阶段被 900 s 超时中断，留下 `runs/20261118T015818Z-41f374ed/`（四个已完成 parcel，无 `state`）。保留备查；`n_permutations=20` 下整条链本身就在 10 分钟量级。


---

## 11. 第四批：低风险清理（E4/E5/E6/E9/E10/E11 + H4/H5/H6）

### 11.1 做了什么

| 项 | 落点 | 改动 |
|---|---|---|
| E5 | `orchestration/executor.py` | 抽 `_parcel_for(...)`：两条执行路径（`execute` / `execute_station`）共用一处 parcel 构造 + 「产物为空即 `E3003`」判据 + `verify()`。原来各抄一遍 12 个字段 |
| E4 | `orchestration/capability_bridge.py` | 三处一模一样的 `def scoped(capability)` 局部函数收成模块级 `_scoped_view(capability, policy_view)`（6 个调用点改完） |
| E6 | `capability/ops/search.py` | `fit` / `generate` 门面显式拒绝 `params.algorithms`（多算法）：两套参数形状不再在同一个函数里假装兼容 |
| E9 | `capability/ops/align.py` | P2 / P3 恒真检查改**带证据**：P2 记 `fields_checked`，P3 记 `boundary` |
| E10 | `capability/spec.py` | 删除 `deterministic` / `stateful` 两个恒默认字段；`cacheable` 改**派生属性**（`side_effect != append_only`） |
| E11 | `capability/ops/verdict.py` | 已在 C 组随 `redundant` 改动一并修掉（`_decide` 现在是「先剔软原因、看还剩什么」） |
| H4 | `s0/04 §3.8 combine` | 撤下「`fit.start ≥ max(available_from)`」与 `E1019` 的**已实现**声明，写明为什么不能按文档实现 |
| H5 | `s0/04 §3.9 allocate` | 标注约束投影路径**当前不可达**（schema 无 `mode` / `constraints`），并说明放开它需要同时改 schema + 口径 + AC29 用例 |
| H6 | `contracts/plan_schema.py::_domain_members` | **该函数已不存在**（S2 方案 B 重构时消失），条目自动失效 |

### 11.2 两条需要单独记的语义决定

**`cacheable` 为什么改成派生**。它和 `deterministic` / `stateful` 一样，`_SPECS` 里一个都没赋值，于是 `describe` 恒印 `True/True/False`。`append_only` 的能力每次调用都往账本追加一条事实，命中缓存会让「试了几次」少记一笔——把 `verdict` / `search` 标成"可缓存"是错的。改派生后语义自洽，且**不需要任何地方赋值**：
`cacheable = side_effect != "append_only"`。确定性与状态性不在这里声明：确定性由 `seed` + `call_digest` 保证，有没有 `fit/generate` 阶段是**算法**的属性（`mining.AlgorithmSpec`）。

**E1019 为什么不实现**。编排层把候选的 `available_from` 置成**供给覆盖末端**（`build_live_supply` 的 `as_of`），这个值在任何真实计划上都晚于 `fit.start`——按文档实现等于让所有计划 `E1019`。正确修法是先把 `available_from` 的语义改回「该候选**首次可计算**的那一天」（要编排层与 `search` 一起改、且要可复现推导），再实现检查。现状的不泄漏由另外三条保证：`fit.end + embargo ≤ eval.start`、供给 pin 的 `acquired_at`、标签层的保留区触达掩码。**文档已不再声明这条前置**，避免验收时被当成已通过项。

### 11.3 新增守卫

- `test_parcel_is_constructed_in_exactly_one_place`：`executor.py` 里 `self._parcel_for(` 必须恰好出现 2 次（`load_committed_parcel` 是从契约记录复原，不受约束）
- `test_cacheable_is_derived_from_side_effect`：`verdict` / `search` 不可缓存，`observe` / `allocate` 可缓存；公开载荷里不再出现 `deterministic` / `stateful`
- `test_align_always_true_checks_carry_evidence`：P2 的 `fields_checked` 等于实际比对的字段数，P3 的 `boundary` 等于报告里的 `holdout_boundary`
- `test_facade_rejects_multi_algorithm_params`：`fit` / `generate` 收到 `params.algorithms` 即 `E1004`

### 11.4 测试

`tests/v5` **760 passed**（756 → 760，+4）。

### 11.5 仍未做（下一批候选）

E7（`boundary` 绕过 `timeaxis`，缺 1900–2100 sanity）、E8（GP 魔数收进注册表 `defaults`）、G3（`_INITIAL_CASH` / `_BASE_TS` 进 `caliber.net`）、G7（`_longshort` 的 30% 分位出口化）、G9（`boundary` 把 `REASON_CODES` 语义抄成字面量）、G11（pipeline 告警阈值无依据）、G12（`master_seed` 兜底 42 / `family_v1` 兜底）、G14（求解器正则魔数）、G1/G2（S 标签写死，改动=全链指纹失效）、F1（缓存接线）。

---

## 12. 第五至八批：把规划里的剩余项全部收口（2026-11-18 终轮）

### 12.1 做了什么

| 项 | 落点 | 改动与行为影响 |
|---|---|---|
| E7 | `ops/boundary.py` | 删本地时间解析器，统一走 `timeaxis.resolve_interval`；顺带获得 1900–2100 纳秒 sanity（毫秒时间戳现在 E4002 而不是静默读成 1970 年） |
| G9 | `stream.py` / `ops/boundary.py` | 新增 `OBSERVABLE_CODES = REASON_CODES - _UNOBSERVABLE`，observable 掩码从 legend 派生，不再抄 `[0,5,6,7]` |
| G10 | `params.json.defaults` / `boundary` | `tier_edges` 缺省值进注册表，值仍 1/3、2/3 → 分区逐位不变 |
| E8/G5 | `params.json.defaults` / `ops/search.py` | 搜索魔数 20/3/4/40/7 收敛到一处；`fit` / `generate` / `_config` / `build` 四个入口不再各抄一份 |
| G6 | `capability/mining.py::_OpCodes` | 带参算子从注册表 `param_domain` 派生，`arity=registry.arity-1`（decode 另追加窗口 const）；`corr`/`cov` 终于能生成。按 `impl_symbol` 去重，`ref`/`delay` 别名不重复生成 |
| G14 | `params.json.defaults` / `ops/allocate.py` | 求解器去退化正则 1e-3 进注册表；该分支仍只有直调可达（见 H5） |
| G11 | `params.json.defaults` / `app/cli/pipeline.py` | 腰斩告警阈值 0.5 进注册表，message 里的「> 50%」随参数生成 |
| G3 | `params.json.defaults` / `plan_schema.json` / `backtest_engine.py` | `initial_cash` / `base_ts` 出口化；`caliber.net` 增可选 `initial_cash`（显式写才进 `caliber_digest`）；缺省仍 1e6 / 2020-01-01 |
| G7 | `params.json` / `ops/observe.py` | 分位多空的每边比例出口化为 `prefilter.quantile`，缺省 1/3 与旧 `n_sym//3` 逐位一致 |
| G8 | `capability/timeaxis.py` | 365 收敛为 `CALENDAR_DAYS_PER_YEAR` 一处定义，五个文件 import；**不开放为参数**（开放=同一 Sharpe 两个年化口径）。`_BLOCK_MIN=4` 同理钉住：它改一个值就改零分布与 `empirical_p`，属判据不是旋钮 |
| G12 | `params.json.defaults` + 五个调用点 | `master_seed=42` / `family_v1` 收敛到 `defaults.cli`，生产代码里裸字面量归零 |
| F1 | `facilities/notify.py` / `facilities/__init__.py` / `executor.py` / `pipeline.py` | 七件套全部接线：`digest.of` 算指纹、`trace.append` 登记后落盘、`build_notifier()` 统一 F7 配置；Cache 以 `Executor(cache_stations=...)` **按工位白名单 opt-in**（默认空集），命中重新验 payload、trace 标 `origin=cache`，落盘/计数不跳过 |
| G1/G2 | `capability/spec.py::METHOD_STAGES` + 三处调用 | 五个 `@Sx` 字面量集中到规格层；`combine_domain@S4` → **`@S7`**（组合域在 S7 才成形） |
| B4 | `backtest_engine.py::_symbol_names` + 不变量测试 | 合成标的名单点化；测试证明换掉名字后所有落盘字节逐位一致——修复方式是**钉住「名字不进产物」**，不是徒劳地把 A0 改成真名 |

### 12.2 一处需要纠正的规划判断

规划里 G1 写「observe 标签仍写 S3，S4 已接真成本链，至少改成 @S4」。核对产物后确认 **observe 的 @S3 是对的**：`turnover` / `gross_sharpe` 是 S3 的**毛**粗筛量（`est_cost=0`，注释明写「绝不冒充净成本」），真成本来自 `allocate+backtest@S4`。把毛指标改标 S4 反而制造语义错误。所以只改了真正过时的 `combine_domain`（S4 → S7）。

### 12.3 实现缺省 vs 口径默认（本轮引入的区分）

`params.json` 顶层新增 `defaults`，供 boundary / allocate / search / pipeline / backtest / cli 六类**实现细节**使用。它和 baseline 的口径默认值是两类东西：

- **口径默认**：编译期展开进 plan、进 `policy_digest`；缺了 `E1008`，运行期绝不补。
- **实现缺省**：不进 plan、不进指纹；只在调用方未显式给值时读，单一真相在注册表。

混成一类会让「调一下 GP 种群」悄悄改变 `policy_digest`。`test_implementation_defaults_single_source` 把这些值冻结在测试里，防止再散落回代码。

### 12.4 指纹影响与分界点

| 变化 | 影响范围 |
|---|---|
| `combine_domain@S4` → `@S7` | Portfolio payload 内的 `domain_digest` 变化 → Portfolio/Certify 的 payload/parcel digest 变化。**plan / policy / blueprint / caliber / domain 五个顶层指纹不变**（combine 标签不进 compiler 的 domain_digest）。属新试验族级改名，历史 Portfolio 摘要不可直接比对 |
| G6 新增 corr/cov opcode | 同 seed 的候选序列变化 → Search 之后所有工位的产物变化。这是搜索空间扩展的预期后果 |
| E7 毫秒时间戳现在 E4002 | 之前会静默跑出空结果；只有用了非纳秒输入的计划会受影响 |
| 其余项（defaults 出口化、G7/G3 缺省值、F1 接线、B4 单点化） | 缺省值原样、算法原样 → **不改变任何现有产物** |

### 12.5 测试与真实复跑

- `tests/v5` **775 passed**（760 → 775，+15）。
- 清理了 **53 个重复测试函数定义**（历史 `cat >>` 重放留下的死代码；pytest 只收集最后一个，结果不受影响，但文件里一半是永不执行的副本）。备份在 `user_workspace/.tmp_test_backup/`，复查零重复。
- 真实复跑两次（同一 `plan_allfix_probe.json`，run `20261118T032428Z-1edce415` 与 `20261118T032619Z-1edce415`）：
  - 五个顶层指纹逐位一致：`plan=1edce415…`、`blueprint=599d57b8…`、`policy=e95ec926…`、`caliber=5dd24835…`、`domain=51d59c90…`
  - **八个工位的 `call_digest` / `digest` / `payload_digest` / payload 字节 SHA256 全部 IDENTICAL**
  - 8 parcels / 8 traces / state=done；Verdict 1 pass / 7 reject，`n_trials_used=10`
  - Evidence 8 候选；样例 `ic=0.03926495957984232 / monotonicity=0.9 / group_periods=1211`，`ic_decay={1:0.01416, 5:0.03926}`（口径跨度 5 与主 IC 逐位相等，再次验证单一实现）
  - Certify `net_return=0.0071190657851394334 / net_sharpe=0.17658297488709382 / max_dd=-0.23903613133127877`；`manifest.policy_digest == ticket.policy_digest`，`policy_view_digest` 同时在场

### 12.6 明确不改的三件事（附理由，不是遗漏）

1. **365 不开放为参数**：它是加密 24/7 的定义性常量，进 `bars_per_year` 与年化；开放等于允许同一 Sharpe 有两个口径。已收敛为单一定义 + 守卫测试。
2. **`_BLOCK_MIN=4` 不出口化**：它决定 block bootstrap 的零分布与 `empirical_p`，是判据的一部分，不是实现旋钮。
3. **B4 不接真实 OHLCV 帧**：生产撮合用 `ExecutionPrice.CLOSE`，open/high/low/volume 与合成标的名都不进任何落盘字节（`fills_ref` 是 `(T,N)` 数组摘要）。要做成 intrabar 撮合必须同时扩 backtest 的 inputs 契约与 ledger 落盘格式，属新特性而非 bug 修复。已用不变量测试钉住「名字不进产物」。

### 12.7 全清单核对

规划 8 组 49 项：**A1–A5、B1–B5、C0–C3、D1–D5、E1–E11、F1–F3、G1–G14、H1–H6 全部处理完毕**（含「评估后决定不改」并写明理由的项）。`dsr_min` / `fdr_max` / `n_trials` 三条红线未动；Web/API、供给调度、`core/fabric` 仍在「明确不做」范围内。
