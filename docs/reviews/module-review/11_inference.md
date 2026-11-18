# 模块审查 11 · `capability/inference.py`

> 审查日期：2026-11-18
> 依据：源码 308 行全文通读；依赖 `scipy.stats`（t/norm 尾、`false_discovery_control`）、`ops/observe.py:35-37,328-359,679-719`（唯一消费方之一）、`ops/verdict.py:25-27,115-135,346-394`（另一消费方）、`ops/search.py:25,314`（IC 消费）；设计 `06-evaluation.md` §4/§7、`08-reuse-audit.md` §5.2、`13-migration.md` §61/§121、`10-crypto-fit.md` §头号缺陷；测试 `test_v5_observe.py`、`test_v5_verdict.py`、`test_v5_dsr_units.py`、`test_v5_ac63_golden.py`。
> 判据：`v5_module_review_plan_20261118.md` §0.0/§0.1。用户清单必查：行内跨资产置换修 V4 头号 bug、`pearson_ic_row`/`ic_series`/`sample_shape`、统计原语唯一实现。
> 实测方式：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_inference.py` + 一条 1187×20 性能对拍（系统临时目录，未改仓库）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Inference 是 observe / verdict 共用的**唯一统计推断原语层**：只回答"一个数怎么算"，不回答"算不算显著"——没有阈值、没有结论、没有账本。它应保证：

1. **IC / 形状原语**：`spearman_ic_row` / `pearson_ic_row` / `ic_series` / `sample_shape` 只有一处实现；成对剔 NaN、少于 3 个有效对给 0.0（不塞 NaN 进 IC 序列被 HAC 整段丢掉）；`sample_shape` 给 DSR 的非超额偏度/峰度（正态=3），样本<4 或零方差回正态默认。
2. **HAC t 与经验 p**：Newey-West 带宽的 IC 均值 t；样本<4 或方差估计非正 → 0.0（"无可靠证据"不是"显著为正"）；`empirical_p` 用 Phipson-Smyth `(1+#{null≥value})/(1+n)`，非有限观测/空零分布一律 1.0。
3. **零分布必须行内跨资产置换**（V4 头号 bug 的修复点）：只打乱每行内部资产，保留每行有限值的 multiset 与 NaN 位置，破坏 factor→asset 对应；绝不整行时间轴置换（保留资产漂移 → 零分布中位 |t| 虚高 → 强因子被判死）。
4. **多重检验校正**：BH-FDR / Holm / BY 公式标准、逐位对拍 V4；`by_fdr` 用 scipy，不引 statsmodels 进生产。
5. **DSR 口径**：`sigma_sr` 默认单策略采样误差 `1/√(n_obs−1)`（不是试验间横截面离散度）；`n_trials` 惩罚随试验数单调上升、不可调小；`var_term<=1e-9` fail-closed 返回 0.0（V4 E-01 把 clamp 放进根号 → 病态 DSR 虚假逼近 1.0）。
6. **唯一实现红线**：V4→V5 逐位对拍只有一个落点；observe/verdict 若各写一遍 Newey-West，改一处另一处还停在旧语义，两边数值都"看起来合理"，无检查点会报错。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 复现：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_inference.py`。

### I1 · 行内跨资产置换（实测逐格）

输入（3×4，含两个 NaN）：

```
[[ 1.  2. nan  4.]
 [ 5. nan  7.  8.]
 [ 9. 10. 11. 12.]]
```

输出（`rng=np.random.default_rng(0)`）：

```
[[ 4.  2. nan  1.]
 [ 7. nan  8.  5.]
 [12.  9. 11. 10.]]
NaN 位置逐格不变 = True
每行有限值 multiset 不变 = True（3/3 行）
映射被破坏（≥1 行顺序变化）= True
```

### I2 · V4 头号 bug 对照（实测零分布）

构造带**跨资产漂移**的面板（T=120, N=10, drift∈[-1.5,1.5]，观测 |t|=47.336），100 次置换：

```
行内跨资产置换（cross_section_permute）：零分布中位 |t| = 0.669 → empirical_p = 0.010
整行时间轴置换（V4 time_shift，rng.permutation(T)）：零分布中位 |t| = 43.742 → empirical_p = 0.307
```

结论：整行置换**保留资产漂移**，零分布被污染到接近观测值（43.7 vs 47.3），把强因子的 p 从 0.010 抬到 0.307——与文档记录的 V4 根因（中位 |t|=4.74 > 观测 3.46 → p=0.95）机制一致；行内置换已闭合该缺陷。（本例观测信号极强，故两种口径都未到 p=0.95；差异方向与实际危害已实测确认。）

性能实测（1187×20，100 次均值）：`cross_section_permute` **1.445 ms/次**（1000 次≈1.44 s/候选），`Generator.permuted(axis=1)` 对照 0.859 ms/次——约 1.7×，代价用于保 NaN 位置（见 D5）。

### I3 · IC / 形状原语（实测）

```
y = x³ (x∈[-1,1], 11点):  spearman = 1.0      pearson = 0.921649
成对剔 NaN:                spearman = 1.0      pearson = 1.0
少于 3 个有效对:           spearman = 0.0      pearson = 0.0
ic_series(F(6,4), R(6,4)): shape = (6,), spearman[:3]=[-0.4,-0.6,-0.4],
                           pearson[:3]=[-0.7239,-0.6132,-0.5333]
形状不符 -> ValueError("factor 与 returns 必须同形状")
sample_shape(standard_t3,5000点) = (skew 0.5612, kurt 43.7399)   # 非超额，肥尾被捕捉
sample_shape([1,2,3])           = (0.0, 3.0)                      # 样本不足回正态默认
sample_shape(常序列)            = (0.0, 3.0)                      # 零方差定义域外，不进噪声
```

### I4 · HAC t / 经验 p / 校正（实测）

```
newey_west_tstat([0.1,0.2,0.3])        = 0.0          # 样本<4
newey_west_tstat(常序列50点)           = 0.0          # 方差非正
newey_west_tstat(8点, max_lags=None)   = 2.309401     # 带宽 floor(4*(T/100)^(2/9))=2
手算 max_lags=2 (Bartlett核)           = 2.309401     # 逐位一致
empirical_p(2.0, [1,2,3]) = 0.75        # (1+#{≥2})/(1+3)=(1+2)/4=0.75
empirical_p(NaN, [1,2,3])= 1.0          # 非有限观测绝不假显著
empirical_p(2.0, [])    = 1.0           # 空零分布=无证据
pvals=[0.01,0.02,0.03,0.50]:
  bh_fdr = [0.04, 0.04, 0.04, 0.50]
  holm   = [0.04, 0.06, 0.06, 0.50]
  by_fdr = [0.083333, 0.083333, 0.083333, 1.0]
```

### I5 · DSR 口径（实测）

```
n_obs=255, n_trials=100, sharpe=0.5
sigma_sr 默认 = 1/√254 = 0.06274558
expected_max_sr(100, sigma_sr) = 0.19042369
deflated_sharpe_ratio(0.5,255,100) = 0.99999835
手算 z=(SR−bench)√(n−1)/√(1−skew·SR+(kurt−1)/4·SR²) → 0.99999835  # 逐位一致
显式传 sigma_sr=0.06274558 与默认结果相同 = True
n_trials=1 → 无惩罚 sr_benchmark=0.0
expected_max_sr(10) < (100) < (1000) = True
var_term<=1e-9（skew=100,kurt=1,SR=20）→ 0.0        # fail-closed
pvalue_from_t(t=2.5, df=119)=0.013781  vs 正态尾=0.012419  # HAC 用 t 尾更保守
```

### I6 · 统计原语唯一实现（实测+全仓 grep）

```
stats.newey_west_tstat / bh_fdr / holm / by_fdr / deflated_sharpe_ratio /
expected_max_sr / pvalue_from_t  存在 = False（全部不在 stats.py）
inference.* 对应函数存在 = True（全部在 inference.py）
```

消费方（`grep from capability.inference`）：`observe.py:35-37`、`verdict.py:25-27`、`search.py:25`、`replay.py:26`——**只 import，不重写**。V4 对应实现 `core/evaluation/significance.py` 仅作迁移对拍基线（`13-migration.md:61`）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| I-1 | **`__all__` 漏掉两个公开原语**：`pearson_ic_row`（`:56`）和 `sample_shape`（`:99`）在模块 docstring/设计中有明确语义，且被 `observe.py:35-37` 直接 import 使用，但 `__all__` 只列了 11 个名字（`:303-307`），`from capability.inference import *` 拿不到。 | P2 | `inference.py:56,99,303-307`；`observe.py:35-37`；实测 `pearson_ic_row in __all__ = False` | **已确认**：不影响生产（显式 import 可用），但 `from … import *` 语义不完整、对工具/反射不友好；补进 `__all__` 即可，零风险 |
| I-2 | **文档把置换说成一行 `permuted(axis=1)`，与实现不符**：`08-reuse-audit.md:118` 称"行内跨资产置换 = `numpy.random.Generator.permuted(a, axis=1)`，一行就是它"；实现是 rng 随机矩阵 + `argsort(axis=1)` + 按 finite 掩码回填（`:171-179`），因为 `permuted` 会把 NaN 位置一起打乱，而观测统计要求缺失位置不动。实测手工版 1.445 ms vs permuted 0.859 ms。 | P2（文档） | `inference.py:164-179`；`08-reuse-audit.md:113-123`；实测性能/NaN 保持 | **已确认符合设计、文档需改**：实现比文档方案更正确（保缺失位置），代价~1.7×；把文档改成"argsort + finite 掩码回填，permuted 只是近似" |
| I-3 | **HAC 带宽默认值与文档对拍值不同**：`08-reuse-audit.md:131` 写"HAC t（maxlags=5）值完全相同 1.0053"；生产默认走 Newey-West 数据驱动带宽 `floor(4*(T/100)^(2/9))`（`:152-154`），`max_lags=5` 只是 V4 对拍场景的取值。 | P2（说明） | `inference.py:152-154`；`08-reuse-audit.md:131`；实测 8 点样本默认带宽=2 | **已确认符合设计**：文档需注明"1.0053@maxlags=5 是对拍数，不是默认口径"，避免读者以为生产固定 5 阶 |
| I-4 | `bh_fdr` 依赖 scipy `false_discovery_control`（`:238` via `by_fdr`）——scipy 版本需 ≥1.11；`requirements.txt` 是否精确锁定未在本次核对。 | P2（待核实） | `inference.py:229-238`；`requirements.txt` | **需确认意图**：实际调用成功（实测 BY/FDR 数值正常）；若部署环境 scipy 更旧会在运行期 AttributeError，而非编译期拒绝。建议在 doctor/依赖自检里核对版本 |

**多了什么**：无冗余实现。`pearson_ic_row` 与 `spearman_ic_row` 是刻意并存的两个口径（`ic_series(method=...)` 切换），不是重复代码；`by_fdr`/`holm` 作为对拍原语保留但主判据固定 BH（`verdict.py:118-119`）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 行内跨资产置换（`cross_section_permute`） | **采纳** | 实测保 multiset/NaN、破坏映射；带漂移面板上 p 从 0.307（整行）降到 0.010；V4 头号 bug 关闭 |
| `Generator.permuted(a, axis=1)` | **不直接采纳** | 会把 NaN 位置一起打乱；手工版多~1.7×但语义正确（I-2） |
| Newey-West HAC（Bartlett 核 + 数据驱动带宽） | **采纳** | 手算逐位一致（2.309401）；样本<4/方差≤0 → 0.0；`13-migration.md:61` 确认公式标准 |
| BH-FDR 主判据 + BY/Holm 对拍 | **采纳** | scipy BH 与 statsmodels 逐位一致；Holm 8 行自研（`08-reuse-audit.md:136-137`）；BY 走 scipy |
| Phipson-Smyth +1 校正的经验 p | **采纳** | `(1+#{null≥value})/(1+n)`，实测 0.75；非有限观测/空分布→1.0，杜绝假显著 |
| DSR sigma=试验间横截面离散度 | **拒绝** | 测的是"候选彼此差异"，换一批候选门槛从 0.08 跳到 0.40，与因子质量无关；默认改采样误差 `1/√(n−1)`（`test_v5_dsr_units.py:124-138`） |
| 整行时间轴置换 / block bootstrap 不中心化 | **拒绝** | 前者保留漂移（实测 p 0.307），后者零分布自带 alpha（`observe.py:342-357` 已中心化修正） |
| statsmodels 进生产依赖 | **拒绝** | 仅为 multipletests 拖入 pandas+formulaic+patsy；只作 dev 对拍（`08-reuse-audit.md:136`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：

1. **V4 头号 bug 已修**：`cross_section_permute` 只打乱每行资产、NaN 原位不动（`:164-179`）；实测 NaN 位置逐格不变、multiset 保持、映射破坏；带漂移面板上行内 p=0.010 vs 整行 p=0.307。`observe.py:335-337` 默认 `within_bar_cross_asset`，`time_shift` 标 deprecated/仅诊断（`:338-341`）。
2. **IC 原语完整**：`spearman_ic_row`/`pearson_ic_row` 成对剔 NaN、<3 对给 0.0（`:37-72`）；`ic_series` 形状校验、两方法切换（`:75-96`）；实测 y=x³ 的 spearman=1.0 / pearson=0.921649，符合"秩一致但线性不足"的设计意图。
3. **`sample_shape` 非超额 + 边界 fail-safe**：scipy `skew(bias=False)`、`kurtosis(fisher=False,bias=False)`（`:123-124`），样本<4、零方差、非有限结果均回 `(0,3)`（`:108-128`）；实测肥尾 kurt=43.74 被捕捉、常序列回正态默认。
4. **HAC t 公式与 bandwidth**：`_acov` 用 `1/n` 归一化 + Bartlett 权重 `2(1−l/(L+1))`（`:132-161`）；手算 max_lags=2 逐位一致；样本<4/方差≤0→0.0。
5. **经验 p 的 Phipson-Smyth 校正**：`(count+1)/(n+1)`（`:182-196`），非有限观测/空分布→1.0；实测 0.75/1.0/1.0。
6. **BH/Holm/BY 公式标准**：BH 排序×n/rank + 反向累积 min（`:199-211`）；Holm ×(n−i) + 正向累积 max（`:214-226`）；BY 走 scipy（`:229-238`）；实测三组数值与教科书一致。
7. **DSR 口径正确**：默认 `sigma_sr=1/√(n_obs−1)`（`:274-280`）、`expected_max_sr` 用 √(2 ln n) 且 n≤1 无惩罚（`:241-249`）、`var_term<=1e-9` fail-closed（`:281-283`）、用**每期 SR** 且 n−1 自由度（`:285`）；实测手算逐位一致。`pvalue_from_t` 传给 df=n−1 时用 t 尾（`:289-300`）。
8. **统计原语唯一实现**：全仓 grep 确认 observe/verdict/search/replay 只 import、无第二份公式（I6）；V4→V5 对拍单一落点成立。

**发现的问题**：

- 见 D2 I-1（`__all__` 漏两函数）、I-2/ I-3（文档与实现口径差异）、I-4（scipy 版本未锁）。**无 P0/P1 级代码 bug**；`test_v5_observe.py` / `test_v5_verdict.py` / `test_v5_dsr_units.py` / `test_v5_ac63_golden.py` 覆盖置换、HAC、DSR sigma、校正、黄金锚。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `08-reuse-audit.md:113-123` | "行内跨资产置换 = `Generator.permuted(a, axis=1)`，一行就是它" | **需改**：实现用随机矩阵+argsort+finite 掩码回填以保 NaN 位置；实测 1.445 ms vs permuted 0.859 ms。写明"permuted 是近似，不代表缺失格不动" |
| `08-reuse-audit.md:131` | "HAC t（maxlags=5）值完全相同 1.0053" | **需改**：标注"1.0053@n_lags=5 是 V4 对拍数；生产默认 Newey-West 带宽 floor(4(T/100)^(2/9))" |
| `06-evaluation.md` §4/§7 | 统计口径描述（IC/HAC/FDR/DSR） | 与实现一致，无需改；可补 `pearson_ic_row` 与 `sample_shape` 两个原语名，消解 I-1 |
| `13-migration.md:61,121` | "HAC/BH/Holm 公式标准；零分布换成行内跨资产置换" | **正确，无需改**；实测公式逐位一致、置换修复有效，是本次审查的锚点 |
| `10-crypto-fit.md`（头号缺陷段） | V4 整行置换中位 |t|=4.74 > 观测 3.46 → p=0.95 | **正确，无需改**；本地复现同方向（43.74 vs 47.34, p 0.307 vs 0.010），可补作二次证据 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增 IC 口径（如 weighted IC） | ❌ 需改代码 | `ic_series` 只支持 spearman/pearson（`:87-92`），加口径要改方法分派 + 文档 |
| 新增零分布模型（如 stationary bootstrap） | ❌ 需改代码 | 模型分派在 `observe.py:_null_tstats:335-357`，不在 inference；新增需两处 + 参数 schema；当前 `within_bar_cross_asset` 是唯一默认 |
| 换多重检验校正（BY/Holm 作主判据） | ❌ 需改代码+文档 | `verdict.py:118-127` 主判据固定 BH，BY/Holm 只读诊断；改主判据违反 `s0/05` |
| 新 DSR 变体 | ⚠️ 半配置化 | `deflated_sharpe_ratio` 已参数化 skewness/kurtosis/sigma_sr/sr_benchmark；`verdict.sigma_mode` 支持 sampling/cross_section（后者仅诊断） |
| scipy 版本升级/降级 | ⚠️ 需核对 | `by_fdr` 依赖 `false_discovery_control`（≥1.11）；降级会在运行期失败（I-4） |
| `__all__` 补全 | ✅ 零风险 | 把 `pearson_ic_row`/`sample_shape` 加进 `:303-307` 即可，不影响任何现有调用 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 不适用/无污染——所有函数用每期值；DSR 明确要求每期 SR 并注明"调用方不得先年化再传入"（`:270`）
- **C2 资金费按事件**：✅ 不适用（统计原语不碰字段语义）
- **C3/C4/C5**：✅ 不适用（无市场/合约/计价概念）
- **C10 判据不可放松**：✅ 强支撑——非有限观测 p→1.0、HAC 方差≤0→0.0、DSR var_term≤1e-9→0.0、sigma 默认采样误差、n_trials 惩罚单调；全是 fail-closed 方向
- **C11 流动性分层**：⚠️ 本模块按传入矩阵算 IC，不判可交易性；上层掩码不当会污染 IC，责任在 observe/boundary
- **C12 观测单位**：✅ `newey_west_tstat` 按序列长度 T 算带宽（无交易日折算）；`sample_shape` 用样本矩

## R1–R7 影响

- **R1 真数据跑通**：✅ 正（唯一实现被 observe/verdict 共用，真跑已用）
- **R2 结论可信**：✅ **最强支撑模块之一**——V4 头号 bug（行内置换）已修并实测复现对照；HAC/BH/Holm/DSR 手算逐位一致；sigma 默认采样误差钉住
- **R3 因子零代码扩展**：✅ 中性（原语与因子种类无关，新因子自动复用同一判据）
- **R5 单因子可归因**：✅ `pearson_ic` / `spearman IC` 双口径落盘（`observe.py:706-711`），可拆开诊断单因子行为
- **R7 fail-closed**：✅ 强支撑（NaN/空分布/零方差/样本不足/病态 var_term 全都退化为"无证据"而非假显著）

---

## 结论

**本模块是全轮最干净的模块之一，无 P0/P1 代码 bug，可确认通过。** 用户三项必查全部实测成立：①行内跨资产置换保留 multiset/NaN、破坏映射，带漂移面板上把经验 p 从整行置换的 0.307 拉回 0.010，V4 头号 bug 已闭合；②`pearson_ic_row`/`ic_series`/`sample_shape` 全部有真实调用输出（y=x³ 的 spearman 1.0 vs pearson 0.921649、肥尾 kurt 43.74）；③统计原语唯一实现——全仓仅 inference.py 定义、observe/verdict/search/replay 只 import，无第二份公式。四处 P2 均为说明/一致性问题：`__all__` 漏两函数、置换"一行 permuted"的文档失实、HAC 对拍 maxlags 与默认带宽混淆、scipy 版本未在本次核对。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 12 个模块 `stats.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某项 P2 想当场裁决 → 告诉我
