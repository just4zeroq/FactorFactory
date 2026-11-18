# 10 · 加密适配：每个组件在哪一档，哪些要去掉

> 本系统只做加密。所以「组件是否支持加密」不是加分项，是**准入门槛**。
> 本篇逐个过一遍，分三档：**加密原生** / **资产无关但默认值对加密是错的** / **股票专用，必须去掉**。
> 所有数字都是本机实测；脚本在 `user_workspace/bench_*.py`。
> 结论先给：**已发现并修掉一处会影响结论的口径错误**（回测年化用了 252 天，Sharpe 少报 20.4%），
> 并确认**最大的一处能力浪费不在组件，而在数据**——755 个合约的行情、758 个合约的资金费已经下载，但搜索只用 20 个资产、5 个 OHLCV 列。

---

## 1. 三档判定

| 档 | 含义 | 处置 |
|---|---|---|
| **A · 加密原生** | 本身就是加密基础设施 | 直接用 |
| **B · 资产无关** | 不假设交易日历、市值、行业、结算制度 | 直接用，但要验证默认值 |
| **C · 股票专用** | 假设 252 天、交易日历、财报、市值/行业 | **去掉**，或降为纯参考 |

判定一个组件属于哪档，只问四个问题：

1. 它假设一年有多少个交易日？
2. 它需要一个「交易日历」才能工作吗？
3. 它需要市值 / 行业 / 板块 / 财报这些加密没有的字段吗？
4. 它默认「一天只能交易一次」或「有收盘价」吗？

---

## 2. 全组件矩阵

| 组件 | 档 | 加密支持情况（有证据的） | 处置 |
|---|---|---|---|
| **ccxt** | **A** | 加密原生；`markets` 给 `contractType` / `expiry` / `settle` | 采用（核心） |
| **qweave** | **B** | 列进列出，不假设资产类型；Alpha158 全量可用；Alpha101 需市值/行业、Alpha191 需风格因子 → 均报错 | 采用；需补 `vwap` 列 |
| **ml4t-backtest** | **B** | `_ANNUALIZATION_FACTORS` 里有 `"crypto": 365`，`_session_minutes_per_day("CRYPTO")` 返回 1440 → 1h 得 8760，**正确**；永续资金费、保证金、强平、`TIERED` 手续费都有 | 采用；**必须显式传 `calendar="crypto"`**（见 §4） |
| **polars / pyarrow** | **B** | 纯列计算，无日历概念 | 采用 |
| **numpy / scipy** | **B** | 无关；`false_discovery_control` 提供 BH/BY | 采用 |
| **scikit-learn** | **B** | 无关；`TimeSeriesSplit` 按行切，24/7 下就是按天切 | 采用 |
| **cvxpy** | **B** | 纯优化，无关 | 采用（`allocate` 约束路径） |
| **duckdb** | **B** | 无关 | 采用（事实层查询面） |
| **filelock / structlog** | **B** | 无关 | 采用 |
| **deap** | **B** | 进化框架，与资产类别无关；全局 `random` 需 save/restore | 采用（默认 GP 实现，见 `07-components.md` §7） |
| **gplearn** | **B** | 资产无关，但无多目标、AST 需靠私有字段反推 | 不采用 |
| **skfolio** | **B** | 24/7 索引直接可用；19 种风险度量；CVaR 57ms / HRP 39ms；**无分位打分优化器**；`budget=0` 单独给全零；无法直接约束 gross | **采用，仅两处**（见 §9） |
| **pandas-market-calendars** | **C** | 只有股票/期货交易所日历，无 24/7 | **不得用于业务判断**；仅作为 ml4t 传递依赖存在 |
| **alphalens-reloaded** | **C** | 逐日 pandas groupby、`periods=(1,5)` 以天计、按 sector 分组 | **去掉** |
| **quantstats / empyrical** | **C** | 默认 252 年化，需要逐处覆盖 | **去掉**（ml4t 已给 56 项指标） |
| **yfinance** | **C** | 美股/ETF 数据源，与加密无关 | **已移除**（2026-11-16：`requirements.txt` 无该行、venv 无该包、`loader.py` 重写为 ccxt-only 门面） |
| **WorldQuant Alpha101** | **C** | 实测 `ValueError: missing column cap`——需市值、行业、板块 | **去掉**（不可用） |
| **国泰君安 Alpha191** | **C** | 实测报错需 `hml`/`mkt`/`smb`/`index_close`（Fama-French 风格因子） | **去掉**（不可用） |
| **PyPortfolioOpt** | **C** | 1.6.0 在 scipy 1.18 下 HRP 与 EfficientFrontier 双崩；且前端假设股票组合约束 | 去掉（已卸） |
| **riskfolio-lib** | **C** | 同 cvxpy 底座但更重依赖；无增量能力 | 去掉 |

**净结论**：核心链路（qweave + ml4t + cvxpy + polars + duckdb）全部落在 A/B 档。需要去掉的都是外围（股票数据源、股票因子分析、股票因子库）。

---

## 3. 必须去掉的（逐条附理由）

| 项 | 位置 | 理由 | 动作 |
|---|---|---|---|
| `yfinance` | 原 `requirements.txt`、`core/data_feed/loader.py` | 加密系统不需要美股数据源。它还是 `data/` 存在非加密文件的来源 | **已完成**：`loader.py` 重写为 ccxt-only 门面（无 Yahoo/Alpha Vantage 分支），requirements 移除该行 |
| Alpha101 / Alpha191 | 由 qweave 提供，非本项目代码 | 101 个里 0 个可用、191 个里 0 个可用（缺字段报错） | **配置层禁止引用**；不要写进「内置因子清单」 |
| `alphalens-reloaded` | 未装 | 股票因子诊断，且零分布/置换那部分它没有 | 不装 |
| `quantstats` / `empyrical` | 未装 | 252 默认 + 与 ml4t 指标重复 | 不装 |
| `pandas-market-calendars` 的业务使用 | ml4t 传递依赖 | 用它判「未收盘」或「是否交易日」在 24/7 下必错 | 全局禁止；唯一例外是 ml4t 内部 |

### 3.1 取数链路只有一条（2026-11-16 定稿）

```
ccxt 交易所接口 ──fetch──▶ canonical store（data/{exchange}/{trade_type}/*.feather） ──read──▶ DataFrame
```

| 环节 | 唯一实现 | 不许做什么 |
|---|---|---|
| 下载 | `SmartBatchDownloader.download_ohlcv_batch`（分页走 `pagination.paginate_ohlcv`） | 不在别处重写分页/重试/合并；`loader.fetch` 只做委派 |
| 读取 | `DataLoader.get_data`（`naming.data_path` 规范路径 + `bar_clock` 丢未收盘 bar） | 不猜文件名、不 fallback 到旧命名；缺文件不得返回空 DataFrame |
| 交易所元数据 | `ccxt.markets`（`contractType` / `expiry` / `settle`） | 不硬编码资产类别，不把交易所写死为常量 |
| 资金费 / OI / basis | `crypto_streams`（同一 ccxt 客户端，按交易所能力分支） | 不按固定周期乘资金费（I34） |

`exchange_id` 是**参数**：`DataLoader(exchange_id="okx")` 与 `get_data(exchange_id="bybit")`
走同一条链路并各自落盘到 `data/{exchange}/...`。默认值 `binance` 只出现在配置兜底，
不是代码分支。回归测试：`tests/test_data_source_ccxt_only.py`（11 例，含"仓库内不得再有
yfinance import / yahoo 字面量"的静态扫描）。

---

## 4. 已修的加密口径缺陷：回测年化用了 252 天

这是本轮唯一改动生产代码的地方，因为它直接进报告。

**原因**：`build_backtest_config` 没传 `calendar`，ml4t 于是用默认 252 期/年。

**实测**（`user_workspace/bench_calendar_crypto.py`，同一条权益曲线，20 资产 × 1187 根日线）：

| 配置 | `sharpe` | 差异 |
|---|---|---|
| 不传 `calendar`（改前） | **0.2091** | — |
| `calendar="crypto"` | **0.2517** | **+20.4%** |
| `periods_per_year=365` | 0.2517 | 同上 |

比值 0.2517 / 0.2091 = **1.2037**，与理论 √(365/252) = 1.2035 吻合到四位。小时线更严重：不传得 1638 期/年，正确应为 8760（**5.35 倍**）。

**影响范围**：`run_backtest` 把 `metrics["sharpe"]` 原样塞进 `summary`，所以报告的 Sharpe 一直偏低 20%。这是**偏保守**的方向（门槛显得更难达到），但「0 晋升」这个结论里有一部分来自这个偏差，不是来自信号本身。

**修法**：`build_backtest_config(..., calendar="crypto")`，并加 `tests/test_backtest_calendar.py`（5 例）钉住口径。

---

## 5. 数据面：已经下载的东西，只用了一部分

实测 `data/binance/futures/`：

| 数据 | 合约数 | 实际用于搜索 |
|---|---|---|
| 日线 OHLCV | **755** | **20 个资产** |
| `funding_rate` | **758** | **未进面板**（只用于回测成本） |
| `open_interest` | **20** | **未进面板** |
| `basis` | **20** | **未进面板** |
| `long/short_liquidations` | **0**（需外部供给） | 未进面板 |

也就是说：**97.4% 的已下载行情、100% 的资金费数据没有进入因子搜索空间**。

这不是「缺组件」，是「缺接线」。`core/data_feed/crypto_streams.py:48` 的注释已经写明这几个流是「miners 在表达式 AST 里引用的终端名」——设计意图在，接线不在。

三处需要打通：

| 环节 | 现状 | 要做的 |
|---|---|---|
| 配置声明 | 0 份配置声明 `crypto_streams` | 在 `data_feeds.crypto_streams` 里列出流名 |
| 面板构建 | 只 pivot OHLCV | 把流对齐到 bar 网格后并入面板列 |
| 求值白名单 | 只认 OHLCV 字段 | 面板列集开放，白名单按「列存在」而不是按「列名固定」 |

---

## 6. 一个必须知道的加密事实：资金费结算周期不是常数

抽样 12 个合约的 `funding_rate` 文件，统计相邻两行的间隔（小时）：

| 合约 | 行数 | 间隔分布 |
|---|---|---|
| `0G_USDT` | 3,895 | 1h × 1802，4h × 2091 |
| `LA_USDT` | 4,921 | 1h × 2337，4h × 2581 |
| `PIPPIN_USDT` | 5,296 | 1h × 1781，4h × 3510 |
| `HK1810_USDT` | 562 | 4h × 396，8h × 165 |
| `APT_USDT` | 4,240 | 8h × 4239 |
| `SYRUP_USDT` | 3,344 | 4h × 3340 |

汇总：**1h 出现 5,920 次 / 4h 出现 16,144 次 / 8h 出现 9,941 次**。

三条后果：

1. **「8 小时结算」是错的假设**。Binance 从 2024 年起把大量合约改成 4h，部分新币一开始就是 1h。同一个合约在历史上还会换周期（`HK1810` 就是前 4h 后 8h）。
2. **文件名在说谎**。文件叫 `{symbol}-1d-funding_rate.feather`，内容是**原生结算网格**（1h/4h/8h）。任何「按文件名推断频率」的代码都会错。
3. **当前实现是对的，但注释是错的**。`load_funding_series` 用 `resample(bar).sum(min_count=1)`——按 bar 聚合所有落在区间内的事件，所以周期变化被自动吸收，金额正确。但它的 docstring 写着「stores one row per 8h settlement」，会误导下一个改这段代码的人。

**必须遵守的规则**：资金费只能**按事件求和**，不能**按固定周期乘**。任何 `rate * 3`（把 8h 当日费率）或 `rate * 6` 的写法都是错的，因为周期本身在变。

**因此 `caliber.cost.funding` 不得包含 `interval_hours` 字段**——它不是口径参数，是数据属性，只能从事件时间戳推。

---

## 7. 加密特有的能力缺口

去掉不支持的之后，还要看**加密需要但架构没有**的东西。按重要性排：

| 缺口 | 为什么是加密特有 | 影响面 |
|---|---|---|
| **1. 五个加密流未进面板** | 现货系统没有「资金费/持仓量/基差/爆仓」这类数据 | 搜索空间少了一大类信号；这是当前最大的 alpha 缺口 |
| **2. 面板列集是固定 5 列** | 加密的可观测面在扩张（资金费、OI、爆仓、多空比、交易所间价差） | 加一列就要改代码 → 违反「两个扩展点」的承诺 |
| **3. 合约维度缺失** | 同一标的有现货、永续、季度；`BTCUSDT` 与 `BTCUSDT_250926` 是不同资产 | 无法做期限结构、无法做跨合约横截面 |
| **4. 计价货币不是 USD** | `USDT` 与 `USD` 有偏离；`BTCUSDT` 与 `BTCUSD` 是两个市场 | 收益里混进了稳定币溢价 |
| **5. 上下架与停牌** | 新币上市首日常有极端收益；下架前最后价格不可交易 | 不处理会把「上市首日 +300%」算进因子表现 |
| **6. 无休市但有不连续** | 交易所维护、合约迁移会让 bar 缺口 | `align` 的「未收盘判定」不能靠日历，只能靠 bar 时间戳与墙钟 |

其中 1 和 2 是**必须现在解决**的，3–6 是分阶段。

### 7.1 面板列集必须开放

现在若面板是「固定 OHLCV 五列」，加密因子的表达力就被锁死。正确的形态是**声明式列集**：

```
panel = {
  base:      { symbol, timeframe, fields: [open, high, low, close, volume, vwap] }
  streams:   [ funding_rate, open_interest, basis, long_liquidations, short_liquidations ]
  derived:   { underlying, contract_type, expiry, settle, quote_ccy }
  alignment: { grid: "timeframe", asof_rule: "bar_close", fill: "ffill_only" }
}
```

规则：

- `derived` 里的字段是**维度**，不是数值——它们用于 `group_rank` / `group_neutralize` 的分组键，以及 `boundary` 的筛选条件。
- `streams` 各自有原生网格，读侧按 `asof_rule` 对齐到 bar；**不允许前视填充**（`ffill` 只能取 ≤ 当前 bar 的最后一个事件）。
- 新增一列**不需要改代码**：配置里写名字，面板构建器去供给层找同名文件，找不到就报 `E1014 字段超出已固化供给覆盖`，而不是静默跳过。

这条一旦成立，「内置因子扩充」就真的是配置工作而不是开发工作。

### 7.2 合约维度：`asset` 不等于 `instrument`

加密的资产标识至少是四元组：

```
(exchange, underlying, contract_type, settle_ccy)   例如 (binance, BTC, perp, USDT)
```

现在的 `BTC_USDT_USDT-1d-futures.feather` 已经是这种编码（`{underlying}_{settle}_{margin}`），文件名里带了，但**面板里没有拆成列**。拆开后：

- 同一 `underlying` 的多个合约可以 `group_rank`（期限结构因子）
- `contract_type=spot|perp|dated` 可以过滤（现货没有资金费）
- `expiry` 让 `boundary` 知道什么时候必须平仓

---

## 8. 加一个加密因子要动什么（目标：零代码）

| 因子例子 | 需要的列 | 要改代码吗 |
|---|---|---|
| 资金费动量 | `funding_rate` | **不**。配置声明流 + 写 AST |
| 持仓量变化率 | `open_interest` | **不** |
| 基差均值回归 | `basis` | **不** |
| 爆仓失衡 | `long_liquidations` / `short_liquidations` | **不**（但需外部供给数据） |
| 期限结构斜率 | `expiry` 维度 + `close` | **不**，但需要 §7.2 的维度拆列 |
| 跨交易所价差 | 两个 exchange 的 `close` | **不**，但需要供给层支持多交易所目录 |

判据：**如果加一个因子需要改 Python，说明扩展点没做对。** 只有三种情况需要改代码：新算子语义（要 fork qweave）、新数据源（要写下载器）、新判据（要改 `verdict`）。

---

## 9. skfolio：用，但只在两处

实测（20 资产 × 1187 根 24/7 日线，skfolio 1.12.16）：

| 能力 | 结果 |
|---|---|
| 24/7 `DatetimeIndex` | **直接可用**，不需要日历 |
| `MeanRisk`（方差最小） | **34.6 ms**（全样本拟合） |
| `MeanRisk`（mean-CVaR 95%） | **56.6 ms** |
| `RiskBudgeting`（CVaR） | **68.2 ms** |
| `HierarchicalRiskParity` | **39.3 ms** |
| 风险度量 | **19 种**：方差/半方差/CVaR/EVaR/CDaR/EDaR/Ulcer/Gini/Worst-Realization… |
| `CombinatorialPurgedCV(n_folds=5, n_test_folds=2)` | **10 个 split**，`purged_size` / `embargo_size` 以**观测**为单位（24/7 下 1 观测 = 1 天，正好） |

三处限制，都有实测：

| 限制 | 实测 |
|---|---|
| **没有「按分数分位建仓」的优化器** | `skfolio.optimization` 全是风险最优类，没有打分驱动的 |
| **`budget=0` 单独用会给出全零权重** | `MeanRisk(budget=0, max_weights=0.2)` → 权重和 0、非零 0 个（数学上正确：零风险最优解就是空仓）。必须配 `objective_function` |
| **无法直接约束 gross = 1** | `budget=0 + MAXIMIZE_RETURN` → 多头 2.000 / 空头 −2.000（gross 4.0），只能事后缩放；`budget=0 + MAXIMIZE_RATIO` → gross 0.19 |

**结论**：

| 用在哪 | 怎么用 | 为什么 |
|---|---|---|
| **`certify` 的分折** | `CombinatorialPurgedCV` | purge + embargo 是组合式交叉验证的标准实现，自己写容易漏掉「训练集不得跨折泄漏」 |
| **`combine` 的风险最优模式** | `MeanRisk` / `RiskBudgeting` / `HRP` | 19 种风险度量，自己写 CDaR/EDaR 不划算 |
| **`allocate`** | **不用** | 它是风险最优，不是打分驱动；我们的默认路径是「按分数分位建仓」，skfolio 没有这个形态；约束路径要 gross=1，skfolio 也不直接支持 |

`combine` 用风险最优模式时有一个架构后果要明说：**它返回的是权重，不是分数**。这意味着 `combine` 有两种输出形态，必须在 `05-contracts.md` 的 I23 里显式登记为「合成分数 / 直接权重」两种，且下游 `allocate` 在第二种形态下退化为 pass-through。不登记就会出现「有的组合走了 allocate、有的没走」而没人知道。

**依赖状态**：`skfolio` 已在本机装好并完成上述实测，但**尚未写进 `requirements.txt`**——因为它还没有被任何代码 import。接线（§11 第 6 项）时再加，避免「声明了却没人用」的依赖堆积。

---

## 10. 不变量与验收

> 编号与陈述以 `05-contracts.md` §1（不变量）与 §4（验收）为准；本表只补本篇的检查点，不重复定义。

| 编号 | 内容 | 检查点 |
|---|---|---|
| I33 | 任何年化都不得依赖库的默认值；加密一律 365/年、24 小时/日 | `build_backtest_config` 必须传 `calendar="crypto"`；评测器年化参数来自 `caliber`；AC33 |
| I34 | 资金费只按事件求和，不得按固定周期乘；`caliber` 里不得出现 `funding_interval_hours` | 配置校验拒绝该字段；`load_funding_series` 只允许 `resample(bar).sum`；AC34 |
| I35 | 面板列集是开放集；配置声明的流缺失时 fail-closed，不得静默跳过 | 声明 `basis` 但供给层无该文件 → `E1014` 中止；AC35 |
| I36 | 交易日历不得参与任何业务判断 | AST 扫描：除 ml4t 内部，`capability/**` 与 `orchestration/**` 不得 import `pandas_market_calendars` |
| AC33 | 年化口径正确 | 同一条权益曲线，`backtest` 的 `annualisation_used` 恒为 `calendar_365`（基准不随频率变）；（V5 可执行判据）同一批净收益，`time` 为 1h 网格时 `bars_per_year=8760` 且 `sharpe` 恰为日频的 √24 倍，`time` 缺省时 `bars_per_year=365` 且来源标 `assumed_daily`。V4 的「`calendar="crypto"` 与 `periods_per_year=365` 逐位相同 / 不传时比值 √(365/252)」判据在 V5 已失去对象：plan 只允许 `calendar_365`，不存在 252 分支
| AC34 | 资金费周期无关性 | 造一份 1h 网格与一份 4h 网格、总额相同的事件表，聚合到同一 bar 后金额相等 |
| AC35 | 开放列集 | 配置里新增一个已存在的流名 → 不修改任何 Python 即可在表达式里引用；新增一个不存在的流名 → 编译期 `E1014` |
| AC36 | 股票专用组件不进入依赖链 | `pip list` 不含 `yfinance` / `alphalens-reloaded` / `PyPortfolioOpt` / `riskfolio-lib`；`requirements.txt` 里无对应行 |

---

## 11. 落地顺序（按「影响结论的先后」）

| 序 | 动作 | 为什么排这个位置 |
|---|---|---|
| 1 | ~~回测 `calendar="crypto"`~~ | **已完成**。直接影响报告数值，且只需一行 |
| 2 | 面板列集开放 + `crypto_streams` 接进面板 | 释放 758 个合约的资金费供给，这是当前最大的 alpha 缺口 |
| 3 | 合约维度拆列（`underlying` / `contract_type` / `expiry`） | 期限结构因子的前提 |
| 4 | 把 20 资产扩到全 755 合约（含流动性过滤） | 横截面宽度从 20 到几百，RankIC 的信噪比直接上升；但要先有存储层分区（`09-app-storage.md` §3.6） |
| 5 | ~~去掉 `yfinance` 与股票因子库引用~~ | **已完成**（2026-11-16）：`loader.py` 改为 ccxt-only 门面，requirements 移除 yfinance；不改变结论 |
| 6 | `skfolio` 接 `certify` 的 purged CV | 认证的可信度，等 1–4 稳定后再做 |
