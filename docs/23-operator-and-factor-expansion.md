# 23 · 算子扩表与加密原生因子（v2）

> 口径：本次扩表把 `capability/registry/operators.json` 从 **19 个算子扩到 34 个**、
> `capability/factors.py` 从 **33 个内置因子扩到 37 个**。
> 因子取舍全部有实测，测量方法见 §4，被淘汰的候选见 §5（**不要重测**）。

---

## 0. 一句话结论

算子按 Qlib/Alpha158 的时序族补齐，成本极低、收益是**搜索空间变宽**（GP 与 beam 单步扩展同时变宽）；
内置因子只增 4 个，因为 12 个候选里有 8 个实测无信号或与库内因子高度重复——
**因子库的价值是锚点，不是数量**，塞进弱因子只会让下一轮 `prior_pool` 的裁决变脏。

---

## 1. 算子扩表：19 → 34

### 1.1 新增 15 个

| 算子 | impl_symbol | 形状 | 语义 |
|---|---|---|---|
| `ts_rank` | `ts_rank` | method_param | 窗口内**末值**的平均名次百分位 ∈(0,1] |
| `ts_argmax` | `ts_argmax` | method_param | 窗口内最大值位置，0 基、从**窗口起点**数 |
| `ts_argmin` | `ts_argmin` | method_param | 同上，最小值 |
| `ts_skew` | `ts_skew` | method_param | 偏差校正样本偏度 |
| `ts_kurt` | `ts_kurt` | method_param | 偏差校正 Fisher 超额峰度 |
| `ts_mad` | `ts_mad` | method_param | 相对自身均值的平均绝对偏差 |
| `product` | `product` | method_param | 窗口连乘（Alpha158 的 SUMP/SUMN 基元） |
| `decay_linear` | `decay_linear` | method_param | 线性衰减加权均值，最近一根权重最大 |
| `ema` | `ema` | method_param | 均值种子 EMA，α = 2/(d+1) |
| `slope` | `slope` | method_param | 对 0..d-1 的滚动 OLS 斜率 |
| `rsquare` | `rsquare` | method_param | 同一回归的 R² |
| `resi` | `resi` | method_param | 同一回归的残差 |
| `neg` | `neg` | method | 取负 |
| `power` | `power` | call_binary | `qw.power(a, b)`，逐点幂 |
| `signed_power` | `signed_power` | call_binary | 保号幂：`sign(x)·|x|^p`，负底不返回 NaN |

新增一种调用形状 `call_binary`：左右都是**表达式**、没有窗口参数。原来只有
`method_binary`（`expr.op(rhs)`），而 qweave 把 `power`/`signed_power` 暴露成模块级函数
（`qw.power(a, b)`），不挂在 `Expr` 上。形状表与 `_Builder` 各加一个分支，`_check_registry`
的三向比对（注册表 ↔ 形状表 ↔ qweave 符号）自动覆盖到它。

### 1.2 与 Qlib 的对应

Qlib 约 50 个算子（`qlib/data/ops.py`），本次补齐了其中"表达式语言里最常用、且在 qweave 里有原语"的一批：

- **已覆盖**：Abs / Log / Sign / Add / Sub / Mul / Div / Power / Greater(→留给条件算子) /
  Ref / Mean / Sum / Std / Max / Min / Var / Skew / Kurt / Mad / Rank / ArgMax / ArgMin /
  Slope / Rsquare / Resi / Corr / Cov / Delta / EMA / DecayLinear / TSRank / SignedPower
- **仍未覆盖**（见 §6）：`Quantile`（需要两个参数位）、`IdxMax/IdxMin`（等价 ArgMax/ArgMin）、
  `If/And/Or/Not`（布尔算子）、`Med`（qweave 无中位数原语）、`GroupRank/GroupNeutralize`（需要分组字段）。

### 1.3 不进 GP 的 4 个算子

`capability/mining.py::_GP_EXCLUDE = {product, power, signed_power, delay}`：

- `product`：窗口连乘落在价格上，1e5 的 30 次方即溢出 inf，之后按 `degenerate_policy=nan`
  整条因子作废——白烧预算。
- `power` / `signed_power`：右操作数一旦是表达式就量级不可控（`close^close`），
  或把自己变成 NaN（负底的分数次幂）。
- `delay`：与 `ref` 同一个 `impl_symbol`，进 opcode 表只会让 GP 把预算花在去重上。

排除表被 `tests/test_v5_search.py::test_param_ops_come_from_the_registry` 钉死：
**想放宽搜索空间必须同时改测试**，避免"往排除表里塞名字"让断言永远通过。

---

## 2. 搜索空间的连带变化

`capability/mining.py` 原来有三份手写名字表（GP 一元、GP 二元、GP 带参、beam 单步扩展），
扩表时必须四处同改，漏一处就是"声明与生成分叉"（`corr`/`cov` 曾经只存在于 GP 一侧）。
本次统一为 `_gp_operator_order(reg)`：

- 一元 = `arity==1` 且无 `param_domain`
- 二元 = `arity==2` 且无 `param_domain`
- 带参 = 有 `param_domain`（arity 取 `registry.arity - 1`，窗口由 decode 追加）

顺序规则：`_GP_ORDER_HINT` 里的历史算子钉在原位，新算子按名排序追加在后。
理由不是洁癖——opcode 索引就是基因整数值，重排会让历史种子解出另一批表达式。

---

## 3. 内置因子：33 → 37

四个新增，全部带实测数字写进注释（`capability/factors.py`）：

| 因子 | 表达式要点 | IC | IR | 同号年 | 与库内最大 \|ρ\| |
|---|---|---|---|---|---|
| `low_parkinson20` | `-sqrt(mean(ln(H/L)²,20)/(4ln2))` | **+0.0887** | 0.283 | 7/7 | 0.89（`rv20`） |
| `near_high60` | `close / max(high, 60)` | +0.0414 | 0.147 | 7/7 | 0.51（`mom20`） |
| `funding_zscore7` | `-(mean(f,7)-mean(f,60))/std(f,60)` | +0.0131 | 0.066 | 5/7 | 0.48（`funding_carry_rank7`） |
| `trend_r2_20` | `rsquare(close, 20)` | +0.0098 | 0.044 | 6/7 | **0.12** |

三点说明：

1. **Parkinson 是本库最强信号**，比 `low_vol20`（0.0780）高 14%，且 7 年全正（最小 0.055）。
   它与 `rv20` 的 ρ=0.89 属于"同一条低波动异象、换测量方式"：收盘对收盘的波动会被单根大 K 线
   带偏，日内幅度不受"收盘恰好落在哪里"影响；加密 7×24 无隔夜跳空，高低价几乎覆盖全天，
   这个估计量比在股票上更贴合。先验池按需取一个即可，不必同时进。
2. `near_high60` 问的是"离前高多远"，前高随新高上移，不会被 60 天前的单一基准价钉住——
   这是它与 `mom60`（ρ=0.51）的本质区别。
3. `trend_r2_20` 的 IC 只有 0.0098，但**与库内所有因子的 |ρ| ≤ 0.12**。低 IC 高正交的因子在
   组合里的边际价值高于高 IC 高相关的因子，这也是唯一保留它的理由。

---

## 4. 测量口径（复现用）

- 标的池：`top200_long`（38 个标的，长历史）
- 区间：2020-01-01 ~ 2026-11-01，日线
- 标签：fwd5 简单收益（同标的内 `close.shift(-5)/close-1`），**未中性化**
- 求值：走**生产路径** `capability.ops.evaluate` → `qweave`，不是 pandas 重写
- 指标：逐时点截面 Spearman IC → 按年均值 → 同号年数 / IR
- 冗余度：逐时点截面 Spearman 的时序均值（与 `observe.dedup` 同口径）

诊断脚本是仓库根的 `.v2_factor_diag.py`（`*_diag.py` 一族都是本地诊断，不参与 CLI）。
它同时回答两个问题：新算子能不能算（NaN/溢出/常数暴露）、算出来有没有信号。

---

## 5. 被淘汰的 12 个候选（不要重测）

| 候选 | 实测 | 淘汰理由 |
|---|---|---|
| `quiet_trend` = `ts_efficiency10 × (-rv20)` | IC −0.0430，7/7 | 与 `ts_efficiency10` 的 **ρ=0.99**，纯重复；且 \|IC\| 更低（0.043 < 0.050） |
| `volume_zscore20` | +0.0158，7/7 | 与 `vol_ratio` 的 ρ=0.87，只是换个缩放；IC 绝对值仍低 |
| `drawdown60` | −0.0299，2/7 | 符号不稳（2/7）；与 `near_high60` 同源，后者更强更稳 |
| `basis_zscore7` | −0.0191，1/7 | 1/7 同号，且被 `basis_delta` 覆盖（都是基差异常→反转，但方向随时间翻转） |
| `skew20` | −0.0177，1/7 | 1/7 同号。加密收益偏度的方向在不同年份完全翻转 |
| `funding_slope14` | −0.0137，2/7 | 2/7；资金费变化率不如水平稳定 |
| `basis_slope14` | −0.0206，1/7 | 1/7，同上 |
| `mom_accel_20_60` | −0.0113，3/7 | 3/7，且与 `mom20`/`mom60` 共线 |
| `ts_rank_ret1_60` | −0.0093，2/7 | 2/7 |
| `kurt20` | −0.0088，3/7 | 3/7，峰度估计在 20 根窗口上方差过大 |
| `ts_rank_mom20` | −0.0047，3/7 | 3/7。**秩化抹掉截面水平差异**——加密币的动量是截面现象，转到自身历史分位后信号消失 |
| `lowvol_ts_rank20` | +0.0044，3/7 | 同上：`ts_rank` 版低波动（0.0044）远弱于原始 `low_vol20`（0.0780） |

`oi_price_divergence` / `funding_oi_crowding` **未入选也不代表否决**：本地 `open_interest`
数据只有 180 个有限格（跨 38 标的），无法测量。等 OI 采集补全后按 §4 口径重测，不要凭直觉入库。

---

## 6. 已知缺口

| 缺口 | 影响 | 补法 |
|---|---|---|
| `quantile` 双参数位 | **已补齐（v3）**：`param_domain.kind=fraction` + `method_param2` 形状，34→46 | 见 `docs/26-qweave-operator-surface-audit.md` §3 |
| 布尔/条件算子 `gt/lt/where` | **已补齐（v3）**：6 个比较算子 + `where_` 三元形状；不进 GP 生成器 | 见 `docs/26-qweave-operator-surface-audit.md` §1.1 / §3.3 |
| 分组算子 `group_rank/group_neutralize` | 仍缺：第二参数必须是列式分组字段，当前 `fields.json` 无分组列 | 先给面板加板块列，再登记；不能用连续字段冒充分组 |
| `vwap` 列缺失 | `vwap_gap` 在本地数据上无法求值（E1003） | 采集侧补 `vwap`；因子本身没问题，实测里被跳过 |

---

## 7. 对测试的影响

| 测试 | 变化 |
|---|---|
| `tests/test_v5_v2_operators.py`（新，12 项） | 逐算子闭式对拍：`ts_rank` 的 pct 语义、`argmax` 的 0 基起点、`slope/rsquare` 对 `np.polyfit`、`ema` 的均值种子、`decay_linear` 的权重、`power` 负底不 NaN |
| `tests/test_v5_v2_factors.py`（新，6 项） | `low_parkinson20` 闭式解 `r/(2√ln2)`、`near_high60` 恒等式、`trend_r2_20` 对完美直线 = 1 |
| `tests/test_v5_evaluate.py` | `test_evaluate_rejects_unknown_operator` 的样例从 `ts_rank` 换成不存在的 `ts_median`——扩表后用真算子名会变成"测能不能算"而不是"测白名单" |
| `tests/test_v5_search.py` | G6 增加排除表断言：声明 ⊆ 生成 ∪ 排除，且排除表被钉死 |

所有因子/算子测试都给**至少 3 个标的**：`evaluate` 对常数暴露（极差 0）会报 E3003，
单标的闭式解恰好是常数，会被那层防护正确拒掉。
