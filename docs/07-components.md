# 07 · 组件选型：哪些装库，哪些自己写

> 本篇回答一个工程问题：这 12 项能力，各自由什么代码实现。
> 结论先说：**没有一项是完全自研的，也没有一项能只是装个库**。分界线不在能力之间，而在每项能力内部。
> 两个关键部件已确定外部来源：因子计算用 **qweave**（Rust DAG），成交与账本用 **ml4t-backtest**。两者都不自己写。
> 本篇是**能力视角**；按层逐步骤的清点（含配置、编排、事实层、跨层设施、供给链）在 `08-reuse-audit.md`，应用层与存储层的选型在 `09-app-storage.md`，加密适配在 `10-crypto-fit.md`。

---

## 1. 分界线：算子复用，口径自研

每项能力都同时含两部分：

| 部分 | 内容 | 处理 |
|---|---|---|
| **算子 / 算法本身** | 滚动均值、Spearman 相关、BH 校正、Ridge 回归、QP 约束投影、撮合与账务 | **一律复用成熟库**。这些是几十年的公共知识，自己写只会写错 |
| **口径的组装方式** | 用哪个窗口、怎么对齐、NaN 怎么传、成本怎么归集、判据取哪个量 | **必须自研**。这是本系统的差异化所在，也是判据所在 |

判断标准只有一句：**如果开源库已经做了这件事，且语义与我们的口径一致，就用；语义不一致，就在外面套一层，不改它的内部。**

反例（会被判为造轮子）：自己写 rolling mean、自己写 Spearman、自己写 BH 校正、自己写撮合循环、**自己写表达式引擎或 DAG 求值器**。

正例（必须自研）：`caliber` 的组装、`overlap_policy` 的处理、零分布的时间轴置换、`available_from` 的判定、两档输出的聚合契约、字符串与表达式树之间的那一层解析。

---

## 2. 十二项能力的复用总表

| 能力 | 复用组件 | 自研部分（只有这一层） |
|---|---|---|
| `read` 事实读取 | **polars** / pyarrow | 字段清单 → 精确版本引用的解析（编译期） |
| `boundary` 边界构建 | **pandas-market-calendars**（ml4t 已依赖） | 币圈 24/7 无休市、下架判定、合约类型排除 |
| `align` 面板对齐 | **polars** pivot/join | NaN 语义、未收盘判定、因果探针 |
| `label` 标签生成 | **qweave** `with_labels` + polars | 跨度与变换的自由度组装、中性化、`overlap_policy` |
| `search` 候选生成 | **deap**（GP，默认）/ 自研（随机表达式、模板枚举）/ sklearn（线性）/ sb3（RL） | `fit` 适应度通道、树 → JSON AST 转换、试验去重计数 |
| `evaluate` 表达式求值 | **qweave**（Rust DAG） | 算子白名单、JSON AST → `Expr` 构造、跨调用指纹 |
| `observe` 证据采集 | **qweave** `evaluate` + **scipy.stats** | 零分布构造、时间轴置换、分层漏斗 |
| `verdict` 唯一裁决 | **scipy** `false_discovery_control`（BH/BY） | DSR、判据组合、`reason_codes` 枚举；Holm 8 行自研并 dev 对拍 |
| `combine` 合成 | **scikit-learn**（Ridge/Lasso）/ **scipy.linalg**（对称正交 `eigh`） | 三段窗口、成员可用时间约束、正交化顺序 |
| `allocate` 持仓构造 | **numpy**（分位路径）/ **cvxpy**（约束路径，`Parameter` 复用） | 横截面分位构造、`binding_constraints` 归因、`min_members` 丢弃策略 |
| `backtest` 回测 | **ml4t-backtest**（已装，见 §3） | 两档输出契约、`caliber` 注入、杠杆参数贯通 |
| `certify` 认证 | **scikit-learn** `TimeSeriesSplit` / ml4t walk-forward | embargo、holdout 揭示状态、可复现清单 |

**只有三处需要真正写算法**：`label` 的口径组装、`observe` 的零分布、`allocate` 的分位构造。其余要么直接调库，要么套一层适配。

---

## 3. 回测引擎：ml4t-backtest，不是造一个

`net` 升格为 `backtest` 之后，它的实现就是 **`ml4t-backtest`**（`requirements.txt:25` 固定 commit `f0143a8`）。

### 3.1 实测（本机 venv，20 资产 × 1187 bar，每 5 bar 调仓）

| 场景 | 单次耗时 | 成交笔数 | 指标数 | 最大回撤 |
|---|---|---|---|---|
| 1x 无杠杆 | **0.388 s** | 1700 | 56 | 12.4% |
| 3x 杠杆 + 2.5x gross | **0.388 s** | 1704 | 56 | 30.6% |

（首次调用 1.5s 是导入与预热；稳态 0.388s。）

外推：200 候选 ≈ **78 秒**；1000 候选 ≈ **6.5 分钟**，单线程。这个量级意味着 **`summary` 档不需要向量化捷径**——可以对每个候选真跑一遍完整成交模拟。上一轮担心的「两档实现不一致」问题因此不存在：两档读同一份账本。

### 3.2 它已经有的（不需要补）

| 需求 | 对应实现 |
|---|---|
| 杠杆 | `BacktestConfig.allow_leverage` / `initial_margin`（库侧字段名；v5 侧只有 `caliber.net.max_leverage`，适配器负责换算） |
| 维持保证金与强平 | `long_maintenance_margin` / `short_maintenance_margin`；`risk/portfolio/manager.py` 的 liquidation 路径 |
| 永续资金费 | `Engine(funding_df=...)`；要求 `rate` 或 `amount_per_unit`，重复事件直接报错 |
| 逐笔成交账本 | `result.to_fills_dataframe()`（28 列，含 commission/slippage/spread/bid/ask/exit_reason） |
| 权益曲线 | `result.to_equity_dataframe()`（含 drawdown / high_water_mark） |
| 持仓与组合状态 | `to_holdings_dataframe()` / `to_portfolio_state_dataframe()` |
| 拒单记录 | `to_rejected_orders_dataframe()`——比静默跳过强 |
| 目标权重 → 订单 | `TargetWeightExecutor` + `RebalanceConfig`（正是横截面组合需要的） |
| 指标 | 56 项（sharpe/sortino/calmar/maxdd/drawdown duration…） |
| 下一根 bar 成交 | `ExecutionMode.NEXT_BAR` + `next_bar_execution_price` |

### 3.3 现有适配器（已存在的 760 行）

`core/backtest/backtest_engine.py` 已经是 FactorMiner → ml4t 的适配器，且已经处理了两个易错点：

- `load_funding_series` 把 8h 结算**聚合到 bar 频率**（`min_count=1` 求和），避免「每 bar 收一次日费率」的 24x/288x 错算；
- `run_backtest` 返回原始 ml4t `Result`，因此 `full` 档所需账本**已经拿得到**，不需要重新实现。

v5 要做的是把它包进 `run(call)` 契约，不是重写它。

### 3.4 需要补的三处

| 缺口 | 补法 |
|---|---|
| 杠杆与保证金参数未透出 | `build_backtest_config` 目前硬编码 `allow_leverage=True`，未从 `caliber.net.max_leverage` / `initial_margin` / `maintenance_margin` / `liquidation_buffer` 读。**目标 gross 不是口径**（它是构造的结果），适配器只接受上限 |
| 输出只有 `metrics` + `summary`，无账本对象 | 把 `Result` 序列化成 v5 的 `Ledger`（`{t, asset, target_w, actual_w, fill_qty, fill_px, fee, slippage, funding}`），两档都从它聚合 |
| 资金费的 bar 对齐 | 已解决（`load_funding_series`）；但 ml4t 要求 funding timestamp 精确等于 feed 时间戳，8h 网格必须先在适配器里归集 |

### 3.5 明确不用它的部分

| 不做 | 原因 |
|---|---|
| 用它的 `Strategy` 装载表达式求值 | 表达式求值归 `evaluate`，回测只消费权重 |
| 让它自己推断方向（`infer_orientation`） | 已改为 `allow_inferred_orientation=False` 默认；方向必须来自证据层或决策，不能从评估窗口学 |
| 用它的 walk-forward 做认证分折 | 认证的折与 embargo 口径归 `certify`，只用它的引擎跑单折 |

---

## 4. 因子计算：qweave，不自研表达式引擎

`evaluate` 不需要写。**qweave 0.6.2**（MIT，`pip install qweave`，已装）就是这个东西：Python 写表达式，执行进入 Rust DAG，统一做公共子表达式复用、中间 slot 复用、elementwise chain 融合与节点级并行。

```
qw.col("close").ts_std(20) * qw.col("close").ts_mean(5) / qw.col("volume").ts_mean(10)
   └─▶ Rust Expr 树 ─▶ DAG（CSE + slot 复用 + 节点级并行）─▶ 因子列
```

### 4.1 实测（本机，20 资产 × 1187 bar = 23740 行）

| 工作负载 | 耗时 | 说明 |
|---|---|---|
| 1 个表达式（含首次预热） | 17.2 ms | 冷启动开销为主 |
| 20 个表达式（共享 `ts_std(20)`） | 12.3 ms | 3 次中位数 |
| 100 个表达式 | 14.2 ms | 共享度高时几乎不随数量增长 |
| **Alpha158 全量 158 个** | **17.3 ms** | 补 `vwap` 后 **158/158 全部产出非空** |

对比自研：同样的能力要写算子表、DAG 调度、CSE、窗口边界、NaN 传播、分组截面，几万行，且每个边界都会错。这是「不要造轮子」最彻底的一处。

### 4.2 450 个内置因子对加密只部分可用（重要）

| 因子库 | 数量 | 加密可用性 |
|---|---|---|
| Qlib Alpha158 | 158 | **可用**，只需补 `vwap = (h+l+c)/3` 即 158/158 全出 |
| WorldQuant Alpha101 | 101 | **不可用**：需 `cap`（市值）、`industry` / `sector`，加密没有这些概念 |
| 国泰君安 Alpha191 | 191 | **不可用**：需 `hml` / `mkt` / `smb` 与 `index_close` |

所以「内置因子扩充」的真实供给是 **Alpha158 的 158 个量价因子**，不是 450 个。这条必须写进配置说明，否则会以为有 450 个免费因子。

### 4.3 三处必须自研（qweave 没有）

| 缺口 | 为什么它不提供 | 自研量级 |
|---|---|---|
| **AST → `Expr` 构造** | 表达式是 Python 侧构建的对象；`dir(qw)` 里没有 `parse` / `from_str`，`roundtrip` 只是把输入 DataFrame 原样返回 | 约 **20 行**：递归把 `{"op", "args", "field"}` 转成 `qw.col(...).op(...)`。前提是 `payload` 的规范形式是 **JSON AST**，不是字符串 |
| **算子白名单（C16）** | 它不做因果校验，也不区分「这个算子能否出现在搜索里」 | 一张表：算子名 → 分类（时序/截面/逐元素）+ 窗口右闭合 + NaN 传播；遍历 JSON AST 即可校验 |
| **跨调用缓存** | CSE 只覆盖**同一批**表达式；两批候选之间不复用节点 | 按 `H(node_ast + panel_digest + asof + operator_version)` 落一层（`operator_version` 当前即 qweave 版本） |

第一行之所以只有 20 行：**规范形式选 JSON AST，不选字符串**。字符串表达式由算法自己产出、自己在算法侧转成 AST（`search` 适配层），`evaluate` 只认 AST。这样字符串解析的责任落在「算法知道自己的语法」那一侧，而不是让 `evaluate` 去猜所有算法的语法。

第三条的收益边界要说清：同批 100 个表达式已经只要 14ms，跨调用缓存省的不是 CPU，而是「重新解析 + 重建 DAG」的固定开销，以及让「同一节点在不同批次算出同一个值」可审计。

### 4.4 qweave 的四个硬边界

| 边界 | 影响 |
|---|---|
| **不支持注册自定义算子** | 算子集固定。新增因子只能靠表达式组合，不能新增算子语义；需要 `ts_entropy` 这类新算子只能等上游或 fork |
| 截面算子只有 4 个（`rank` / `scale` / `group_rank` / `group_neutralize`） | 没有 `winsorize` / `zscore`，要用 `clip` + `scale` 拼，或在面板层先处理 |
| **API pre-1.0** | 版本锁死到 `0.6.2` 并进 `call_digest`；升级即缓存失效 + 结果对照（符合 `01-capability.md` §7） |
| 不做回测、不做显著性裁决 | 正确：回测归 `backtest`，判据归 `verdict`。它出的 IC / 分位 / 换手是**诊断量**，不是判据 |

### 4.5 为什么不用 panda_factor

`PandaAI-Tech/panda_factor` 是**整套因子生产平台**（MongoDB + 数据源 + 自动更新 + Web + LLM），不是可嵌入的库：

| 维度 | 判断 |
|---|---|
| 许可 | **GPLv3**，传染性，不能进本项目的依赖链 |
| 依赖 | 需 MongoDB、`panda_common` 配置文件、Tushare / RiceQuant / 迅投数据源 |
| 形态 | 是平台（含前端与数据更新任务），不是「DataFrame 进、因子列出」的库 |
| 数据 | 面向 A 股（内置近五年数据） |

它的算子语义（`RANK` / `DELAY` / `STDDEV` / `IF` / `SCALE` / `CORRELATION`）与 qweave 高度重合，可作表达式口径参考，但不引入代码。

### 4.6 选型结论

| 需求 | 用什么 |
|---|---|
| 表达式 → 因子值 | **qweave** |
| forward return 标签 | **qweave `with_labels`**（`entry_lag` 与可交易性对齐已处理） |
| 标签的截面变换（变换 / 中性化 / 分位） | 自己写：用 qweave 的 `rank` / `scale` / `group_rank` / `group_neutralize` 组合，序列与口径由 `caliber.label` 决定 |
| IC / 分位 / 换手 / NW t | **qweave `evaluate`**（诊断量） |
| 显著性判据（DSR / BH / cost_adj） | 自己写（`verdict`；BH/BY 用 scipy，DSR 自研） |
| 零分布与时间轴置换 | 自己写（qweave 不做，这是判据核心） |
| 权重 → 成交 → 账本 | **ml4t-backtest** |

---

## 5. 依赖清单

**逐层的完整清点（每个步骤采用/不采用 + 实测数字）在 `08-reuse-audit.md`**；本表只是汇总。

| 组件 | 用途 | 状态 |
|---|---|---|
| **qweave** | **因子计算内核（Rust DAG + 158 个可用内置因子）** | **已装 0.6.2，精确锁** |
| polars | 读取、对齐、面板操作 | 已装 2.1.0 |
| **cvxpy** | **`allocate` 约束路径、`combine` 联合优化** | 已装 1.9.3，需锁 |
| **duckdb** | **事实层只读查询面（`factor_db list/corr/compare`）** | 已装 2.0.0，需锁 |
| **filelock** | **跨进程锁（新代码）** | 已装 4.2.10 |
| **structlog** | **新模块结构化日志（`bind_contextvars`）** | 已装 26.1.0 |
| numpy / scipy | 数值、统计检验、BH（`false_discovery_control`，需 scipy ≥1.11） | 已装（scipy 1.18.1） |
| scikit-learn | Ridge / Lasso / TimeSeriesSplit | 已装 1.9.1 |
| ml4t-backtest | 回测引擎 | 已装（`f0143a8`） |
| **deap** | **默认 GP 实现（`gp/evolutionary@1`）** | 已装 1.4.4；**LGPL-3.0**，只作未修改依赖使用 |
| torch / stable-baselines3 | NN / RL 范式 | 按需 |

**明确不引入**：`statsmodels`（仅 dev 对拍）、`gplearn`（默认 GP 用 deap，见 §7）、`PyPortfolioOpt`（scipy 1.18 下崩溃）、`riskfolio-lib`（同底座无增量）、`diskcache`（比 parquet 慢）、`blake3`（绝对值可忽略）、`pandera`、`optuna`、`transitions`、`alphalens-reloaded`、`quantstats`、`empyrical`、`zarr`。理由逐条见 `08-reuse-audit.md` §11。

**不引入任何框架**（不做插件容器，见 `01-capability.md` §3）。

---

## 6. 反模式

| 反模式 | 为什么 |
|---|---|
| 自己写撮合循环 | ml4t 已有 exit-first、部分成交、保证金强平、逐笔账本；自写必然漏掉这些 |
| 自己写因子表达式引擎 / DAG 调度 / CSE | qweave 的 Rust DAG 已做 CSE、slot 复用、elementwise 融合与节点级并行；自写等于重做一个更慢的版本，且每个窗口边界与 NaN 规则都要重踩 |
| 自己写 rolling / rank / Spearman | qweave 与 scipy 的实现经过长期验证，自写只会在边界（NaN、tie、单点窗口）出错 |
| 自己写 BH 校正 | scipy `false_discovery_control` 已与 statsmodels 逐位相同（实测最大差 0）；自写最常见的错误是 family 大小取错（本仓库历史上就踩过 `p_family==1`） |
| 为了「性能」给 summary 档另写一套向量化回测 | 两套成交逻辑 = I12 破裂。实测 0.388s/候选，不需要这个捷径 |
| 引入插件框架来「支持多种回测引擎 / 多种因子引擎」 | 封闭多选档的意义是「能选不能加」，四个调用点必须解析到同一实现同一版本 |
| 以为 450 个内置因子都能用 | Alpha101 需市值与行业、Alpha191 需风格因子，加密只有 Alpha158 的 158 个量价因子可用 |
| 引入 PyPortfolioOpt / riskfolio-lib 来做约束优化 | 实测 PyPortfolioOpt 1.6.0 在 scipy 1.18 下 HRP 与 EfficientFrontier **双双崩溃**；而 cvxpy 直写十几行就够，且 cvxpy 已为 `allocate` 引入 |
| 做节点级磁盘缓存 | 实测 93 KB 节点写+读 13–16 ms，而 qweave 批量重算均摊 0.2 ms/表达式——当前规模**净亏 65 倍**。盈亏线在 1M 行以上，见 `08-reuse-audit.md` §8 |

---

## 7. 搜索：deap 做默认 GP 实现，gplearn 不注册

`search` 是四档名字空间里唯一的「开放档」，实现会一直增加。但**默认 GP 实现只能是 deap，gplearn 不作为生产实现注册**。这不是偏好，是四条契约级理由。

### 7.1 四条理由

| # | 契约要求 | deap 1.4.4（实测） | gplearn 0.4.3（实测） |
|---|---|---|---|
| 1 | `payload` 必须是 **JSON AST** | `PrimitiveTree` 遍历一次即得——`Primitive` 有 `.name` / `.arity`，`Terminal` 有 `.name` / `.value`，输出直接是 `{"op","args"}` 与 `{"field"}` | 输出是 `_Program`，只有 `str()`（形如 `X2`）与私有数组 `program` / `arities` / `indices_`；转 AST 必须依赖私有字段语义 |
| 2 | 多目标（绝对值 IC 高 + 换手低 + 复杂度低 + 与已有因子相关低） | `selNSGA2` / `selNSGA3` / `selSPEA2` 全有 | 只有 `parsimony_coefficient` 一个软惩罚，本质单目标 |
| 3 | 算子类型约束（截面算子不能吃时序算子的输出这类非法嵌套要在生成阶段就排除） | `PrimitiveSetTyped` 原生支持类型系统 | `function_set` 只按 arity 匹配，无类型概念 |
| 4 | 断点续跑（长搜索中断不能全丢） | 种群是普通 list，`pickle` 即得 checkpoints；`algorithms` 支持逐代外循环 | 无 checkpoint / 无 resume，`fit` 跑完才返回 |

第 1 条是决定性的：上一轮已裁决 `payload` 用 JSON AST 而不是字符串（字符串语法归各算法，`evaluate` 只认 AST）。deap 的树结构**天然就是**那棵 AST；gplearn 要先从私有数组反推，等于在「算法差异」和「求值层」之间又插一层易碎的转换。

### 7.2 deap 的三条代价，必须显式承认

| 代价 | 实测事实 | 应对 |
|---|---|---|
| **用全局 `random`** | `cxOnePoint(ind1, ind2)` / `mutUniform(individual, expr, pset)` 签名里**没有 `rand` 参数**，内部直接调 `random` 模块 | 进 `fit` 时 `random.setstate(快照)`，退出还原；`needs_fitness=true` 的算法**禁止并发**（`E2009`）。这是实例级 RNG 原则的唯一豁免，必须写进契约而不是靠自觉 |
| 许可证是 **LGPL-3.0** | `importlib.metadata` 的 classifier 明确 | 作为未修改的依赖使用可以；**不得 fork 后闭源分发**。依赖清单要登记许可证类型，不能只登记版本 |
| 没有原生 checkpoint | `deap.tools` 无 `Checkpoint`；但种群是 list | 自研 20 行：每 N 代 `pickle` 种群 + `random.getstate()`。**状态快照必须同时含随机状态**，否则续跑点之后不可复现 |

### 7.3 gplearn 唯一的真实优势，以及为什么不够

gplearn 的 `weighted_spearman` metric 确实能直接表达「最大化平均 IC」，且它用实例级 `random_state`（`check_random_state` 接受 `int` / `RandomState`，**不接受 `np.random.Generator` 或 `SeedSequence`**）——随机源隔离比 deap 干净。

但这两点不足以抵消：多目标缺失会让「高 IC 但换手 400%」的候选和「IC 稍低但换手 20%」的候选无法同时保留，只能靠事后过滤；而事后过滤意味着适应度目标与实际筛选标准不一致，正是「搜索优化 A、闸门判 B」那类漂移。

**结论**：`deap` 注册为 `gp/evolutionary@1`；`gplearn` 不进依赖清单，不在任何 `impl` 表里。若将来确要接，它只能作为「单目标、不做 AST 转换、仅用于对照实验」的一次性脚本，不进生产编排。

### 7.4 各范式的默认实现

| 范式 | 默认实现 | 说明 |
|---|---|---|
| 遗传规划 | **deap** | 上面四条理由 |
| 随机表达式 | **自研 ~50 行** | 按 AST 语法随机采样；没有可复用的库，也不值得引 |
| 模板枚举（含 Alpha158 的 158 个） | **自研 + qweave** | 组合爆炸由模板定义控制，不需要搜索框架 |
| 线性 / 树模型 | **scikit-learn**（Ridge/Lasso）+ lightgbm | 直接可用 |
| 神经网络 | **torch** | `output_kind=model_ref` |
| 强化学习 | **stable-baselines3** | 同样走 `needs_fitness=true` 的通道：奖励就是适应度 |
| 符号回归（研究用） | **PySR**：暂不引入 | 需 Julia 运行时，Windows 部署成本高；阶段 2 再评估 |
