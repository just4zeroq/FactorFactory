# 模块审查 12 · `v5/capability/stats.py`

> 审查日期：2026-11-18
> 依据：源码 264 行全文通读；依赖 `ops/boundary.py:273`（`trailing_coverage` 门禁）、`ops/label.py`（forward_return / neutralise）、`ops/observe.py`（row_corr 等）、`ops/allocate.py` / `ops/backtest.py`（rebalance_freeze）；设计 `06-evaluation.md` §4/§5.6/§7、`11-field-operator-registry.md` §2.3、`s0/04-capability.md`；测试 `test_v5_boundary_align.py`、`test_v5_label_panel.py`、`test_v5_dsr_units.py`、`test_v5_full_review_regressions.py`。
> 判据：`v5_module_review_plan_20261118.md` §0.0/§0.1；用户清单必查：`trailing_coverage`/`trailing_mean`、`rebalance_freeze`、HAC t / BH-FDR / Holm 公式标准、DSR sigma 默认 `1/√(n_obs−1)`、`n_trials` 不许被任何路径改写。
> 实测方式：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_stats.py`（系统临时目录，未改仓库）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Stats 是横截面 / 时序基础算子的**唯一纯函数层**：只回答"一个数怎么算"，不判显著、不出结论。它应保证：

1. **NaN = 缺席，不填充**：所有函数输入 `(T,N)` 浮点数组，NaN 一律不补零、不替换常数（§4 三条硬约束之二）；`cs_zscore` 在 std≈0 时整行 NaN，绝不除零。
2. **三个 transform 唯一定义**：`cs_rank`（升序百分位、并列平均）、`cs_zscore`（ddof=1）、`cs_rank_zscore`——一处定义，避免"同名两种算法"事后对拍才暴露。
3. **回看窗口语义正确**：`trailing_mean` 分母应是窗口内**有效计数**；`trailing_coverage` 分母必须是"本该有几根"（实际回看跨度），否则稀疏数据里门禁永远不触发。
4. **标签前视边界**：`forward_return` 的未来值只出现在标签里，`entry=close`(offset=0) / `next_close`(offset=1) 两条路径明确；分母为非有限 → NaN，不得产生 inf。
5. **再平衡冻结**：`rebalance_freeze` 按 `t % rebalance == 0` 网格重算，非网格行沿用最近成功网格行（数据缺口不斩仓）；`rebalance<=1` 逐 bar 原样返回。
6. **回归/相关原语**：`rolling_beta` 只用 `[t−w+1,t]` 且共同时点<2 给 NaN（不回落 1.0）；`cs_neutralise` 只用当期截面、欠定返回 NaN；`row_corr` 有效对<3 返回 NaN。

**注（用户必查落点核对）**：HAC t / BH-FDR / Holm / DSR sigma / `n_trials` 守卫**不在本模块**——它们分别实现于 `inference.py`（见 11_inference.md 实测）与 `verdict.py` / `params.py`。本模块职责止于基础算子；统计原语唯一实现在全仓范围成立（见 D2 S-1 / 具体例子 T6–T7）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 复现：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_stats.py`。

### T1 · 三个 transform（实测，含并列/不足/零方差）

输入 `x`（3×5）：

```
[[ 1,   3,   3, nan,   5],
 [ 2,   2,   2,   2,   2],
 [ 7, nan, nan, nan, nan]]
```

输出：

```
cs_rank =
 [[0.25  0.625 0.625  nan   1.   ]   # 并列 3,3 取平均秩 2.5/4=0.625
  [0.6   0.6   0.6    0.6   0.6 ]   # 全并列→同一百分位（n=5 → 3/5=0.6）
  [ nan   nan   nan   nan   nan ]]  # 有效成员<2 → 整行 NaN
cs_zscore =
 [[-1.2247  0.      0.      nan   1.2247]
  [ nan     nan     nan     nan   nan   ]]  # std=0 → 整行 NaN，不除零、不补0
  [ nan     nan     nan     nan   nan   ]]
cs_rank_zscore = 与 cs_zscore 同形（先 rank 再 zscore）
TRANSFORMS keys = ['cs_rank','cs_zscore','cs_rank_zscore','none']
```

### T2 · `trailing_mean` vs `trailing_coverage`（稀疏数据分母，实测）

输入：5×1 数组，仅 t=0 有值 10，其余 NaN；mask=`isfinite(raw)`。

```
trailing_mean(raw,5)[4]              = [10, nan, nan, nan, nan]  # 按有效计数分母，仅 t=0 有值
trailing_coverage(mask,5)[4]         = [0.2, 0, 0, 0, 0]        # 分母=回看跨度5 → 1/5=0.2
trailing_mean(mask.astype(float),5)[4] = [0.2, 0, 0, 0, 0]      # 实测：对布尔掩码两者数值相同
window=0 / coverage window=0 -> ValueError("window 必须 >= 1")
```

**核对说明**：`trailing_coverage` 的分母确实是回看跨度（`:110-114`），`min_coverage` 门禁在稀疏数据上会触发（0.2 < 阈值）。但 docstring `:95-101` 称"用 `trailing_mean(x!=nan)` 会让孤立样本覆盖率恒为 1.0"在本实现下**不精确**——实测对布尔掩码两者相同（都 0.2）；真正的陷阱在 `np.asarray(mask).astype(bool)` 会把 NaN 也转成 `True`（`:107`），而不是分母差异（见 D2 S-2）。

### T3 · `rebalance_freeze`（实测，网格冻结）

```
目标 w[:,0] = [1, 2, NaN, 3, 4, 5]
dropped     = [F, F,  T,  F, T, F]
rebalance=3 输出[:,0] = [1, 1, 1, 3, 3, 3]
  # t=0 网格且未丢 → last=1；t=1,2 非网格 → 沿用 1（t=2 丢了也不斩仓）
  # t=3 网格且未丢 → last=3；t=4 丢但非网格 → 仍 3；t=5 非网格 → 仍 3
rebalance=1 -> 原样返回（逐 bar 重算，含 dropped 行整行清仓的旧行为）
```

### T4 · `forward_return`（实测，未来值只在标签）

价格 4×2：

```
[[100,100],[110, 99],[121,108],[133,120]]
entry_offset=0, horizon=2 ->
 [[0.21    0.08  ],
  [0.2091  0.2121],
  [ nan     nan  ],
  [ nan     nan  ]]
entry_offset=1, horizon=2 ->
 [[0.2091  0.2121],
  [ nan     nan  ], ...]      # 下一根进场，少一行
horizon=0 -> ValueError("horizon 必须 >= 1")
```

### T5 · 其余纯函数（实测形状/边界）

```
rolling_beta(20×3, market, 5)  -> shape (20,3), 非NaN 57 个   # n<2 共同时点时给 NaN
cs_neutralise(y(8×4), 2 regressors) -> shape (8,4), 非NaN 32 个
row_corr pearson[:3] = [0.4666, -0.6084, -0.3989]
row_corr spearman[:3]= [0.4, -0.4, 0.2]
nanmean([nan,1,2]) = 1.5 ; nanmean(全 NaN) = nan
```

### T6 · 用户必查落点核对：HAC t / BH-FDR / Holm / DSR sigma（实测存在性）

```
stats.newey_west_tstat / bh_fdr / holm / by_fdr /
deflated_sharpe_ratio / expected_max_sr / pvalue_from_t  存在 = False（全部不在本模块）
inference.* 对应函数存在 = True（全部在 inference.py，实测数字见 11_inference.md）
```

跨模块实测数字（11_inference.md 已打印，此处仅引用不重复编造）：HAC 手算 `2.309401` 与函数逐位一致；`bh_fdr([0.01,0.02,0.03,0.5])=[0.04,0.04,0.04,0.5]`、`holm=[0.04,0.06,0.06,0.5]`；DSR 默认 `sigma_sr=1/√254=0.06274558`，`deflated_sharpe_ratio(0.5,255,100)=0.99999835` 与手算一致。**结论：统计原语唯一实现成立，本模块无第二份公式。**

### T7 · `n_trials` 不许被任何路径改写（实测）

```
params.schemas()["verdict"]["params"] keys =
  ['cost_adj_min','dsr_min','family','fdr_max','sigma_mode']   # 没有 n_trials
params.json 含 verdict.params.n_trials = False
validate_params("verdict", {"n_trials":5,"dsr_min":0.95}) -> E1003
  "verdict.params 含 schema 之外的字段 ['n_trials']"
params.schemas() 每次返回深拷贝 = True   # 防 `schemas()["verdict"]["params"]["n_trials"]=...` 永久改契约
```

配套源码证据：`verdict._n_trials`（`verdict.py:69-83`）**只从 `call.runtime_facts["n_trials"]` 读**、缺失或非正整数即 `E2003`；`executor.py:629-652,761-763` 是唯一写入者（从 Search payload 的 `n_trials` 注入）。`params.py:303-305` 注释明确记录"一条赋值废掉 C16 验收"的历史事故。本模块不含任何 `n_trials` 写路径。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| S-1 | **用户必查清单与本模块职责边界不符**：清单把"HAC t / BH-FDR / Holm / DSR sigma / n_trials"列在 stats.py 下，实测这些函数/守卫全在 `inference.py` / `verdict.py` / `params.py`（T6/T7）。 | P2（说明，非代码 bug） | `stats.py:1-264` 全文无这些函数；`inference.py:141-300`；`verdict.py:69-83`；实测存在性 False/True | **已确认：不是缺失，是分工**。统计原语唯一实现恰好因此成立；已在本审查包的 11_inference.md / 08_search.md 逐条实测。若确需"stats.py 聚合导出"，可加 re-export，但会破坏"单一落点"的清晰性，不建议 |
| S-2 | **`trailing_coverage` docstring 描述的分母差异不精确**：`:95-101` 称"用 trailing_mean(x!=nan) 分母是有效计数、孤立样本覆盖率恒为 1.0"；实测对布尔掩码 `trailing_mean(mask,5)` 与 `trailing_coverage(mask,5)` 数值相同（都 0.2），真正的陷阱是 `:107` 的 `astype(bool)` 把 NaN 也转成 True。 | P2（文档） | `stats.py:94-115`；实测 T2 | **已确认实现正确、注释需改**：覆盖率的回看跨度分母是真实存在的差异（对取值数组 vs 掩码），但"恒为 1.0"的说法在本实现下不成立，建议改写成"入参必须是布尔掩码；取值数组里的 NaN 经 astype(bool) 会变成 True" |
| S-3 | **`rebalance_freeze(period<=1)` 返回入参数组引用而非副本**（`:253-254`）：文档写"原样返回"，调用方若修改返回值会改到入参。 | P2（边界） | `stats.py:252-254`；实测 rebalance=1 数组相等 | **需确认意图**：当前无声明复制语义，且权重数组在编排层只读消费；如需防御性复制应同步改文档与测试，否则保留 |
| S-4 | **`cs_rank` 并列排名分母是有效数 n**（`:38`），并列多时最大值 <1（实测 0.625）；这是"有效成员内百分位"的定义，但若下游把它当 [0,1] 绝对分位可能有偏差。 | P2（说明） | `stats.py:28-39`；实测 T1 | **已确认符合设计**（docstring 已写"少于2个→NaN"、升序 pct）；下游 `cs_rank_zscore`/label 已按此语义消费，无需改 |

**多了什么**：无冗余算子。`trailing_rank_pct` 是 `cs_rank` 的别名（`:118-120`），用于分层语义命名，不是第二份实现。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 累积和实现 trailing 窗口（`:82-90,108-114`） | **采纳** | 避开 pandas rolling 的 min_periods/center 默认语义；全 NaN 窗口给 NaN 不补 0；实测稀疏覆盖率 0.2 正确触发门禁 |
| scipy `rankdata(method="average")` 做横截面秩（`:37`） | **采纳** | 并列取平均秩，升序；实测并列 3,3 → 0.625；不引入 pandas groupby |
| `np.linalg.lstsq` 逐时点截面回归取残差（`:190-212`） | **采纳** | 逐时点而非全期拟一次系数；有效成员 <2+回归元数 → 整行 NaN（欠定无意义） |
| 样本协方差/方差 `(n−1)` 的 rolling beta（`:183-186`） | **采纳** | 共同时点<2 给 NaN，不回落 1.0；实测非 NaN 数 57/60 |
| `rebalance_freeze` 网格冻结 | **采纳** | 24/7 下 t 按观测计，非交易日；缺口不斩仓（实测 t=4 dropped 仍沿用 3），契合 C7/C12 |
| pandas rolling / fillna(0) / 前视 bfill | **拒绝** | 24/7 无日历、NaN=缺席不填充（§4 硬约束）；本模块无任何 ffill/bfill |
| 把 HAC/BH/DSR 也搬进本模块做"统一统计层" | **拒绝** | 会制造第二份公式，破坏唯一实现红线；现状 inference.py 单点已满足（S-1） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：

1. **NaN 语义全程保持**：`_row_valid=isfinite`（`:24-25`），`cs_*`/`trailing_*`/`row_corr` 全不改 NaN 为 0（实测 T1/T2/T5）；`forward_return` 分母非有限→NaN（`:148`）。
2. **三个 transform 唯一定义**：`TRANSFORMS` 字典（`:63-68`）是唯一出口，`cs_rank_zscore=cs_zscore(cs_rank(x))`（`:59-60`）；实测并列/零方差/不足成员行为一致。
3. **`cs_zscore` 除零防护**：std≤_EPS 或样本<2 → 整行 NaN（`:49-55`），实测全并列行整行 NaN，未产生 inf。
4. **`trailing_mean` / `trailing_coverage` 的回看边界**：`lo=max(0,t−w+1)`（`:85-90,110-114`），早期行按实际跨度算；覆盖率分母是 span 不是有效计数（实测 0.2）；`window<1` 显式 ValueError。
5. **`forward_return` 两条入场路径**：分子 `price[end:]` 是未来出场价、分母 `price[start:...]` 是进场价，注释明确"写反会方向相反且两边都不报错"（`:143-146`）；实测 offset=0/1 分别产出 2/1 行，horizon 非正拒绝。
6. **`rolling_beta` 窗口与自由度**：只用 `[t−w+1,t]`（`:174-186`），cov/var 都除以 `(n−1)`；n<2 给 NaN，var≤EPS 给 NaN（不回落 1.0）。
7. **`cs_neutralise` 欠定防护**：`need=2+len(regressors)`，有效成员不足整行 NaN（`:197-204`）；lstsq 异常 `LinAlgError` 也跳过（`:207-210`）。
8. **`rebalance_freeze` 契约**：period≤1 原样返回（`:252-254`）；>1 时只在未 dropped 的网格行更新 `last`（`:258-263`），实测 [1,1,1,3,3,3]。
9. **`n_trials` 无写路径 + 判据守卫在他处成立**：本模块全文无 `n_trials`；实测 verdict.params 写 n_trials → E1003、schemas 深拷贝、`verdict._n_trials` 只读 runtime_facts（T7）。
10. **HAC/BH/Holm/DSR 唯一实现在 inference.py**：实测本模块全部 `hasattr=False`（T6），无口径漂移风险。

**发现的问题**：

- 见 D2 S-1（职责边界说明）、S-2（`trailing_coverage` docstring 分母描述不精确）、S-3（`rebalance_freeze` rebalance≤1 返回引用）、S-4（`cs_rank` 分母说明）。**无 P0/P1 级代码 bug**；测试 `test_v5_boundary_align.py`（覆盖率门禁）、`test_v5_label_panel.py`（forward_return/中性化）、`test_v5_dsr_units.py`（成本量纲/DSR）覆盖核心路径。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `stats.py:94-101` docstring | "trailing_mean 分母是有效计数 → 孤立样本覆盖率恒为 1.0" | **需改**：实测对布尔掩码两者数值相同（0.2）；改为"入参必须是布尔掩码；取值数组里的 NaN 经 astype(bool) 会变 True，使门禁失效"，并保留"覆盖率分母=回看跨度"的正确部分 |
| `06-evaluation.md` §4 | NaN=缺席、三 transform 定义 | 与实现一致，无需改；可补 `cs_rank` 并列分母是有效数 n 的说明（S-4） |
| `06-evaluation.md` §5.6 | `rebalance` 网格冻结语义 | 与实现一致（实测缺口不斩仓）；可补"rebalance≤1 返回入参引用"的说明（S-3） |
| `11-field-operator-registry.md` §2.3 | "未来值只在标签里，标签不参与分数计算" | 与 `forward_return` 实现一致，无需改 |
| `11_inference.md`（本审查包）/ `08-reuse-audit.md:131` | HAC/BH/Holm/DSR 在 inference.py、对拍 maxlags=5 | 本模块文档不改；若要把"统计原语唯一实现"写进总览，应在 `01-capability.md` 或 `06-evaluation.md` 加一句"stats=基础算子、inference=统计推断、verdict=判据"的三层分工 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增横截面 transform | ⚠️ 半配置化 | 在 `TRANSFORMS` 加一个纯函数键（`:63-68`）+ 文档；但 `transform` 值来自 plan.caliber，新增值需 C 系列编译期校验同步 |
| 新增 trailing 统计量（如 trailing_std） | ❌ 需改代码 | 无注册点；照 `trailing_mean` 写累积和实现，注意分母语义与 NaN 保持 |
| 再平衡策略多样化（如随机网格） | ❌ 需改代码 | `rebalance_freeze` 写死 `t % period==0`（`:259`）；随机网格需新函数 + caliber 字段 + 文档 |
| 新中性化方式（如行业哑变量） | ❌ 需改代码 | `cs_neutralise` 只接受 regressors 列表，哑变量可由上游构造传入（⚠️ 需新字段，C4 合约维度全局缺口） |
| 换 IC/HAC/DSR 口径 | ❌ 不在本模块 | 只能改 inference.py + 文档（见 11_inference.md D6）；本模块不承接，避免第二份公式 |
| `n_trials` 口径调整 | ❌ 严格禁止 | verdict.params 写 n_trials → E1003；只能由执行器从 Search payload 注入（executor.py:629-652）；本模块无路径 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 本模块不算年化；`forward_return` 是 h 根 bar 跨度收益，成本/年化换算在 observe 层（`test_v5_dsr_units.py:52-67` 实测按 365/h）
- **C2 资金费按事件**：✅ 不适用（纯算子不碰字段语义）
- **C3/C4/C5**：⚠️ `cs_neutralise` 的 regressors 可由上游传合约/市场哑变量，但本模块不判；合约四元组是全局缺口（非本模块落点）
- **C6 上市/下架**：⚠️ `trailing_coverage` 提供门禁原语（实测 0.2），是否使用/阈值在 boundary 层
- **C7 24/7 缺口**：✅ 无日历依赖；`rebalance_freeze` 按观测 t 计网格、缺口不斩仓（实测 t=4）
- **C8 列集开放**：✅ 纯数组函数，与字段数无关；新字段进面板即可被消费
- **C10 判据不可放松**：✅ 本模块无判据/阈值；`n_trials` 写路径不存在（T7），DSR/多重检验守卫在 verdict/inference（11_inference.md 实测）
- **C11 流动性分层**：⚠️ 本模块不判可交易性；`cs_rank` 在传入矩阵上算，掩码由上游注入
- **C12 观测单位**：✅ `window`/`rebalance`/`horizon` 都是 bar/观测数，无交易日折算；`forward_return` 的 h 根 bar 与 `entry_offset` 按观测计

## R1–R7 影响

- **R1 真数据跑通**：✅ 正（纯函数被 boundary/label/observe/allocate 共用，真跑已用）
- **R2 结论可信**：✅ 强支撑（NaN 语义、覆盖率回看跨度、网格冻结不斩仓、样本自由度正确；HAC/BH/DSR 唯一实现在他处已验证）
- **R3 因子零代码扩展**：✅ 正（纯数组算子与字段无关，新因子/新列自动复用）
- **R5 单因子可归因**：✅ `row_corr` / `rolling_beta` / `cs_neutralise` 提供单因子诊断原语
- **R7 fail-closed**：✅ 强支撑（NaN/样本不足/零方差/欠定回归/horizon 非正/window<1 全显式拒绝或退化，不静默补 0）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 实测确认三个 transform 的并列/零方差/不足成员行为正确，`trailing_coverage` 分母是回看跨度（稀疏数据上 0.2 会触发门禁），`rebalance_freeze` 网格冻结在 dropped 行不斩仓（[1,1,1,3,3,3]），`forward_return` 两条入场路径边界正确。用户必查清单中的 HAC t / BH-FDR / Holm / DSR sigma / `n_trials` 经实测**不在本模块**——它们分别位于 `inference.py` 与 `verdict.py`/`params.py`，已在 11_inference.md / 08_search.md 逐条验证（HAC 手算逐位一致、BH/Holm 数值标准、DSR sigma=1/√254、verdict.params 写 n_trials→E1003），统计原语唯一实现成立。四处 P2 均为文档/边界说明：`trailing_coverage` docstring 分母描述不精确、`rebalance<=1` 返回引用、`cs_rank` 百分位分母说明、职责边界说明。

**请确认本模块审查结果**：
- ✅ 确认通过 → 本轮 5 个模块（08–12）审查全部完成
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条 P2 想当场裁决 → 告诉我
