# 08 · 逐层复用清点：每一步该装哪个库

> 07 回答「十二项能力各由什么实现」。本篇回答更细的一层：**原五层架构（意图/编排/能力/事实/跨层设施）里每一个步骤，逐个查有没有现成组件**（应用层与存储层见 `09`，加密适配见 `10`），
> 并把结论分为三档——直接采用 / 套一层适配 / 不采用（自研）。
> 所有「采用」与「不采用」都有本机实测数字支撑；脚本在 `user_workspace/bench_*.py`。
> **凡是被判为「不采用」的，都必须写出不采用的理由是「语义不合」还是「实测更差」，不能只写「觉得没必要」。**

---

## 1. 判据与三档结论

| 档 | 含义 | 入库要求 |
|---|---|---|
| **采用** | 语义与我们的口径一致，直接调用 | 进 `requirements.txt`，锁版本，进 `environment` 指纹 |
| **套一层** | 库做了 80%，缺的 20% 是本系统的口径 | 适配层归本仓库，库不进主干逻辑 |
| **不采用** | 语义与口径冲突，或实测比自己写更差 | 必须在本文留一行理由 + 实测数字 |

三条硬规则（与 `07-components.md` §1 同源，本篇只是落到每一步）：

1. **库的版本是口径的一部分**。凡影响因子数值的库（qweave、cvxpy、ml4t），版本进 `call_digest`。
2. **不引入自带试验计数的库**。任何自己数「跑了多少次」的组件（超参搜索框架）都与 `n_trials` 冲突，`verdict` 会失真。
3. **不引入自带调度/插件容器的库**。它是 `01-capability.md` §3 已经拒掉的形态。

---

## 2. 一页总表

| 层 | 步骤 | 采用 | 套一层 | 不采用（自研） |
|---|---|---|---|---|
| 意图层 | 配置解析 | PyYAML | — | — |
| 意图层 | 配置校验 | pydantic v2 | 跨字段不变量 | C1–C22 编译检查 |
| 意图层 | 命令行 | argparse（沿用） | — | typer（不必须） |
| 意图层 | 编译成 Blueprint | — | — | 全部自研 |
| 编排层 | 工位执行 / 状态机 | — | — | `transitions` |
| 编排层 | 一次性事件 / 账本 | — | — | 全部自研 |
| 编排层 | 唯一裁判 | — | — | 全部自研（禁止外部） |
| 编排层 | 并发 | `concurrent.futures` | 串行强制 | joblib 并行（评估器实测 0.58x） |
| 编排层 | 重试 | tenacity（仅 `pure` 工位） | — | — |
| 能力层 | `read` / `align` | polars | NaN 语义 | — |
| 能力层 | `boundary` | — | ccxt markets | `pandas-market-calendars`（加密无休市） |
| 能力层 | `label` | qweave `with_labels` | 跨度组装 | `overlap_policy` |
| 能力层 | `search` | **deap**（GP）/ 自研（随机、模板）/ sklearn | `fit` 适应度通道、试验去重计数 | `gplearn`（无多目标、AST 依赖私有字段）、`optuna`（自带 trial 计数） |
| 能力层 | `evaluate` | **qweave** | AST 构造器 | — |
| 能力层 | `observe` | scipy.stats | 分层漏斗 | 零分布与行内置换自研 |
| 能力层 | `verdict` | scipy `false_discovery_control` | — | DSR、判据组合 |
| 能力层 | `combine` | sklearn Ridge/Lasso | 可用时间掩码 | 三段窗口、对称正交（5 行） |
| 能力层 | `allocate` | **cvxpy**（约束路径） | 问题结构复用 | 分位构造（1.01 ms 全面板） |
| 能力层 | `backtest` | **ml4t-backtest** | 两档聚合 | — |
| 能力层 | `certify` | sklearn `TimeSeriesSplit` | embargo | 揭示状态 |
| 事实层 | 列存 | parquet / pyarrow | — | — |
| 事实层 | 库查询 | **duckdb** | — | — |
| 事实层 | 内容寻址 | hashlib（stdlib） | — | blake3（快 4x 但绝对值可忽略） |
| 事实层 | 跨进程锁 | **filelock**（新代码） | 旧实现保留 | 手写 msvcrt/fcntl |
| 跨层设施 | 日志 | structlog（新代码） | 旧模块不改 | — |
| 跨层设施 | 随机源 | numpy `SeedSequence` | — | `random` 模块 |
| 跨层设施 | 缓存 | 进程内 LRU | qweave 同批 CSE | **节点级磁盘缓存（实测净亏）** |
| 供给链 | 下载 | ccxt | — | — |
| 供给链 | 原子发布 / 租约 | filelock + `os.replace` | — | — |
| 供给链 | 数据质量 | — | — | pandera（只覆盖 3/11 项检查） |

---

## 3. 意图层：配置 → 编译 → 冻结

| 步骤 | 组件 | 结论 | 依据 |
|---|---|---|---|
| YAML 解析 | PyYAML 6.0.3 | **采用**（已装） | 无竞争 |
| schema 校验 | pydantic 2.14.3 | **采用**（已装） | 字段类型、范围、`Literal` 名字空间校验；比手写 `if not isinstance` 少一半代码 |
| 跨字段不变量 | pydantic `model_validator` | **套一层** | `rebalance ≤ horizon`、`fit_end ≤ eval_start` 这类跨块约束能写，但 C1–C22 里涉及「跑起来才知道」的（占位符、指纹）必须留在编译期 |
| 命令行 | **argparse（沿用）** | **不换 typer** | v5 命令面 10 条，`--set k=v` 用 `action="append"` 就够；typer 是纯改写收益，不是能力收益。已装但不强制使用 |
| 十步编译 | — | **自研** | 没有任何库知道「Blueprint」是什么。这十步是本系统的核心资产 |

**唯一要注意的**：pydantic 不能替代 C 系列校验。pydantic 管「字段是否合法」，C1–C22 管「组合是否合法」。前者是静态的，后者需要解析指纹、展开 sweep、比对 `caliber` 与工位参数——**这两层不要混在一个模型里**，否则 `C20` 这类跨块检查会被写成 `model_validator` 的巨块，报错定位就废了。

---

## 4. 编排层：8 工位执行

| 步骤 | 候选组件 | 结论 | 依据 |
|---|---|---|---|
| 状态机 | `transitions` | **不采用** | 它把转移写进装饰器，静态审查时要跳着看；本系统只有 6 个状态、转移表 10 行，写成一张 `dict` 更好审。引入它不减少代码量 |
| 工位执行顺序 | — | **自研** | 固定线性八步，没有分支需要框架 |
| 一次性事件（Reveal/Budget/Publish） | — | **自研** | 必须绑定 Ticket 且 append-only；没有库知道「一次」的语义 |
| 唯一裁判 | — | **自研，且禁止外部** | I7：`Verdict` 只有一个生产者。任何外部「算显著性的库」都在越界 |
| 失效传播 | — | **自研** | 本质是指纹比对，10 行 |
| 并发 | `concurrent.futures`（stdlib） | **采用** | 只用 `ThreadPoolExecutor` 管 I/O 阶段（下载、读盘） |
| 评估器并行 | ThreadPoolExecutor 跑 numpy | **不采用** | 已实测 **0.58x**（GIL）；`max_workers>1` 必须降为 1 |
| 重试 | tenacity 9.2.1 | **采用，但限 `pure` 工位** | 非确定性工位重试会改变结果却不改 `call_digest`，等于静默换答案 |
| 进度与表格输出 | rich 15.0.0 | **采用**（已装） | CLI 表格、进度条 |

**`pure` 才可重试**这条要写进 `03-orchestration.md` 的重试策略里：`verdict` / `publish` / `reveal` 属 `append_only`，重试会造成重复登记。

---

## 5. 能力层：逐项实测

### 5.1 `allocate`：cvxpy 能进热路径，但**必须复用问题结构**

`allocate` 在证据层被调用 237 次（`1187 bar ÷ rebalance 5`）× 候选数。实测（20 资产，盒约束 ±0.10 + 美元中性 + gross ≤ 1）：

| 写法 | 单次 | 单候选 237 次 | 200 候选 |
|---|---|---|---|
| 每 bar 重建 `Problem` | **6.61 ms** | 1.57 s | **5.2 min** |
| `Parameter` 换目标 + 复用结构 | **1.47 ms** | 0.35 s | **1.2 min** |
| 纯分位构造（全向量化，无优化） | — | **1.01 ms**（全面板） | 0.2 s |

结论：

- **默认路径不调 cvxpy**。分位构造（排序 → 取头部尾部 → 等权）是 20 行 numpy，全面板 1.01 ms，比一次 QP 还快。`caliber.allocate.mode = quantile` 走这条。
- **约束路径才调 cvxpy**，且必须 `cp.Parameter` + 结构复用，**重建一次贵 4.5 倍**。
- 结构复用有一个架构前提：**约束集合在一次执行内不变**。这正好由 `caliber` 冻结保证（C20）——`allocate` 的约束指纹是 `caliber_digest` 的一部分，所以 cvxpy 的 `Problem` 对象可以按「一次执行一个」缓存。这不是优化技巧，是 C20 的直接推论。

### 5.2 `observe`：置换用 `Generator.permuted`，不要自己写循环

| 步骤 | 组件 | 实测 |
|---|---|---|
| RankIC | `scipy.stats.spearmanr` / qweave `evaluate` | qweave 走 DAG，批量更省 |
| **行内跨资产置换** | `numpy.random.Generator.permuted(a, axis=1)` | 1187×20 单次 **0.51 ms** → 1000 次 **0.51 s/候选** |
| 零分布 | — | **自研**（这是判据核心，没有任何库知道我们的零假设） |

置换成本决定了分层漏斗是必需的：`0.51 s × 200 候选 = 102 s` 若每候选都做，占满整轮；只对 Stage A 幸存者做（约 20%）就是 20 s。

**上一轮的错误必须在这里纠正**：早期版本用「整行置换时间轴」，保留了资产漂移，零分布中位 \|t\| 反而高于观测值（`empirical_p = 0.95`）。那不能证明没有 alpha。正确做法是**行内跨资产置换**，`permuted(axis=1)` 一行就是它。

### 5.3 `verdict`：scipy 已经有 BH，**不需要 statsmodels**

| 检查 | statsmodels 0.15 | scipy 1.18 `false_discovery_control` | 仓库自研 `bh_fdr` |
|---|---|---|---|
| BH 调整 p 值 | 基准 | **逐位相同** | 最大差 **1.11e-16** |
| 耗时（n=200） | 0.028 ms | 同量级 | 0.031 ms |
| HAC t（maxlags=5） | 0.246 ms | 无 | **0.115 ms，值完全相同（1.0053）** |
| 支持 Holm | 是 | **否**（只有 `bh` / `by`） | 否 |

三条结论：

1. **不引入 statsmodels**。它为了一个 `multipletests` 拖进 pandas + formulaic + patsy；scipy 已覆盖 BH/BY，而 HAC 自研实现与它逐位一致且快 2 倍。
2. **Holm 是唯一缺口，用 8 行**（排序 → 乘 `n-i` → 累积取 max）。这不违反「不造轮子」：算法五行，且**在开发测试里与 statsmodels 对拍**（`requirements-dev.txt` 里装 statsmodels 专供对拍，不进生产依赖）。
3. DSR 与判据组合**永远自研**。任何库的「夏普显著性」都不是我们定义的那个。

### 5.4 `combine`：Ridge 用 sklearn，正交化用 `scipy.linalg.eigh`

- 回归：`sklearn.linear_model.Ridge/Lasso`，`sample_weight` 可承载「可用时间越近权重越高」。
- 对称正交化：`eigh(corr) → V·diag(w^-0.5)·Vᵀ`，**5 行，实测残留相关 2.1e-4**。不需要任何库。
- 三段窗口（fit / embargo / eval）与成员 `available_from` 约束：**自研**，这是 I24 的全部内容。

### 5.5 `PyPortfolioOpt` 与 `riskfolio-lib`：**不采用**

| 库 | 实测结果 |
|---|---|
| PyPortfolioOpt 1.6.0 | **HRP 崩溃**：`AttributeError: module 'scipy.cluster.hierarchy' has no attribute '_LINKAGE_METHODS'`（scipy 1.18 移除了私有名） |
| PyPortfolioOpt 1.6.0 | **EfficientFrontier 崩溃**：`TypeError: add_objective() takes 2 positional arguments but 3 were given` |
| riskfolio-lib 7.4.1 | 未单独实测；同一 cvxpy 底座 + 更重依赖（绘图、统计包），无增量能力 |

两者能提供的（均值方差、HRP、风险平价、约束前端）**cvxpy 直写都只有十几行**，而且 cvxpy 我们已经为 `allocate` 引入了。多引一个 scipy 版本敏感的上层封装，换来的是「升级 scipy 就崩」的风险——上面两行崩溃就是实证。

### 5.6 `search`：GP 只注册 deap 一个实现

这是本层唯一**有现成库、但两个库都不能直接用**的地方。判据不是「谁更好」，而是「谁能满足 `payload` 必须是 JSON AST 这一条」。

| 范式 | 采用 | 不采用 | 备注 |
|---|---|---|---|
| 遗传规划 | **deap 1.4.4** | `gplearn 0.4.3` | 四条契约理由见 `07-components.md` §7：树→AST 原生、多目标原生、算子类型约束、可 checkpoint |
| 随机表达式 | 自研 ~50 行 | — | 按 AST 语法随机采样，没有值得引的库 |
| 模板枚举 | 自研 + qweave | — | 含 Alpha158 的 158 个量价因子 |
| 线性 / 树 | sklearn / lightgbm | — | — |
| 神经网络 | torch | — | `output_kind=model_ref` |
| 强化学习 | stable-baselines3 | — | 走同一条 `needs_fitness=true` 通道 |
| 符号回归（研究） | 暂不引入 | `PySR` | 需 Julia 运行时，Windows 部署成本高 |
| 超参搜索 | — | `optuna` | 它自己维护 trial 计数与剪枝，与 `n_trials` 冲突；`--sweep` 的计数只能由编排层写 |

**两个库都必须付代价，但代价不同**：

| | deap 的代价 | gplearn 的代价 |
|---|---|---|
| 随机源 | 用**全局** `random`，算子签名不接受 `rand` → 必须 save/restore + 禁并发（`E2009`） | 实例级，但只接受 `int`/`RandomState`，**不接受 `Generator`/`SeedSequence`** |
| 许可 | **LGPL-3.0**，不得 fork 后闭源分发 | BSD，宽松 |
| AST | 原生 | 要依赖私有字段反推，跨版本易碎 |
| 多目标 | 原生 | 无 |

选 deap 是因为前三条里「AST 原生 + 多目标原生」直接对应契约，而它唯一的硬代价（全局 random）**有确定性的工程解法**；gplearn 的缺失（多目标、AST）没有解法，只能事后补，而事后补就是口径漂移。

**这一节顺带暴露一个架构缺口，已补齐**：GP / RL 必须在 `fit` 内评估才能进化，而 `MarketView` 不含任何指标。现在 `fit(ctx)` 带一个受控的 `ctx.fitness`，只读 `mine_slice`、只返回标量或向量、每次调用登记一次试验。详见 `02-mining-interface.md` §2.1 与 §2.2。

---

## 6. 事实层：parquet + duckdb，不要自建索引

实测（100 个因子文件 × 1187 bar × 20 资产 = 118,700 行）：

| 查询 | duckdb | 说明 |
|---|---|---|
| 全库 `count(*)` | **92.2 ms** | 冷启动扫 parquet footer |
| 跨 100 文件按时间聚合 | **24.0 ms** | `read_parquet(glob, union_by_name=true)` |
| 两因子全序列相关 | **6.0 ms** | join on time + `corr` |

结论：`factor_db list/corr/compare` 这些命令**全部下推给 duckdb**，不要再写 polars 逐文件读 + 手工 join。它的 glob 直接吃目录，等于「免 ETL 的库」。

| 项 | 组件 | 结论 |
|---|---|---|
| 列存 | parquet（pyarrow 26.0.1） | 采用 |
| 元数据索引 | **duckdb 2.0.0** | 采用；替代 sqlite 做只读查询面 |
| 内容寻址 | `hashlib.sha256` | 采用。**blake3 不引入**：95 KB 输入 0.132 ms vs 0.031 ms，快 4.26 倍，但 `call_digest` 只哈希元数据字符串不是数组，绝对值已在噪声内 |
| 跨进程锁 | **filelock 4.2.10** | 新代码采用 |
| 旧锁实现 | `core/evaluation/experiment_trace.py:71` 的 55 行 msvcrt/fcntl 包装 | **保留，不重写**。它已正确且有事故注释；替换只带来回归风险 |
| 大数组 | parquet | 采用。zarr 在当前规模无收益（单因子 93 KB） |

---

## 7. 跨层设施

| 设施 | 组件 | 结论 | 依据 |
|---|---|---|---|
| 日志 | **structlog 26.1.0**（仅新模块） | 采用 | v5 每行日志都要带 `run_id / ticket_id / station_id`；`bind_contextvars` 是这件事的标准解。**不替换旧模块**，旧模块用 stdlib `logging` 不动 |
| 追踪 | 自研 span | 自研 | 单进程 CLI，OpenTelemetry 的 collector/exporter 是纯负担 |
| 计时 | `time.perf_counter` | 采用 | — |
| 随机源 | `numpy.random.SeedSequence` + `PCG64DXSM` | 采用 | 每个工位从 root seed 派生独立流；禁止 `np.random.seed` 全局态 |
| 指纹 | hashlib sha256 | 采用 | 见 §6 |
| **缓存** | 进程内 LRU + qweave 同批 CSE | 采用 | 见 §8 |
| 告警 | — | 不需要 | CLI 场景只有退出码与 webhook |

---

## 8. 缓存：**撤回上一轮的「节点级磁盘缓存」**

上一轮我把「跨调用节点缓存落盘」写成了必然优化。实测它当前是**净亏**：

| 档位 | 单节点写+读（93 KB） | 1000 节点 |
|---|---|---|
| 进程内 LRU（命中） | **0.001 ms** | 0.001 s |
| parquet | 13.3 ms | 13.3 s |
| pyarrow | 14.2 ms | 14.2 s |
| diskcache（sqlite） | 16.2 ms | 16.2 s |

对照 qweave 的重算成本：

| 面板 | 1 表达式 | 50 表达式均摊 | 落盘划算吗 |
|---|---|---|---|
| 20 资产 × 1187 bar（23.7k 行） | 7.1 ms | **0.2 ms/表达式** | **净亏**（重算比读盘快 65 倍） |
| 50 资产 × 5000 bar（250k 行） | 38.3 ms | **1.7 ms/表达式** | **净亏**（批量均摊仍远低于 13 ms） |
| 200 资产 × 20000 bar（4M 行） | 864.9 ms | **59.5 ms/表达式** | **划算** |

正确结论：

1. **当前规模（20 资产 × 1187 bar）不做节点级持久化缓存**。只需 qweave 的同批 CSE + 进程内 LRU。
2. **盈亏线是「单表达式重算 > 13 ms」**。250k 行时批量均摊 1.7 ms（仍远低于 13 ms），4M 行时 59.5 ms（已超过）；**交叉点落在两者之间，量级约 1M 行，必须在那个规模重新实测确认，不能直接按插值启用**。面板扩到 200 资产 × 5 年 1h bar 时再做这件事。
3. 上一轮为节点缓存放宽的「两类合法键」（`s0/02-crosscutting.md` §7）保留——**键的规则是对的，只是不该现在启用**。把启用条件写成配置 `facilities.cache.node_cache.enabled = false`（默认关）。
4. 产物的 `call_digest` 缓存不受影响：那是「整份结果复用」，粒度粗但每次省的是整轮计算，仍然成立。

---

## 9. 供给维护链

| 步骤 | 组件 | 结论 |
|---|---|---|
| 交易所行情 | **ccxt 4.5.92** | 采用，且是**唯一**行情来源：OHLCV / funding / OI / basis 全走它 |
| 数据读取门面 | `DataLoader`（`core/data_feed/loader.py`） | 2026-11-16 重写为 ccxt-only：只做「委派下载 → 读规范路径 → 丢未收盘 bar」，不再带股票数据源与自算指标（`ma_5` 那类列属于因子，归 qweave） |
| 股票数据源 | yfinance / Yahoo / Alpha Vantage | **已移除**（依赖、import、字面量三处清零）；守卫测试 `tests/test_data_source_ccxt_only.py` |
| 合约元数据 | ccxt `markets` | 采用；`contractType` / `expiry` / `settle` 是 `boundary` 的输入 |
| 交易日历 | **不用 `pandas-market-calendars`** | 加密 24/7，无休市无半日。用它判「未收盘」会直接算错（本仓库历史上前视泄漏的一类来源）。只有 ml4t 内部作为传递依赖保留 |
| 原子发布 | `os.replace` + filelock | 采用（`os.replace` 在 NTFS/POSIX 上都原子） |
| 租约 | filelock | 采用 |
| 分页下载 | 自研 | 已修过假成功路径（`batch_downloader.py:233`），保留 |
| 数据质量 | **pandera：不采用** | doctor 的 11 项检查里 9 项是业务规则（覆盖率、新鲜度、holdout 窗口、bar 连续性）；pandera 只能盖 dtype/null/range 三类，收益 3/11，代价一个依赖 |

---

## 10. qweave 版本策略：先用原版，定制以证据为前提

本轮明确一条纪律：**阶段 1 不 fork、不 patch、不改算子**，先用上游原版把加密基线跑出来。

### 10.1 阶段 1：`qweave==0.6.2` 原版 + 三处外部自研

| 事项 | 做法 |
|---|---|
| 算子集 | 原封不动，锁死 `0.6.2` |
| 加密特有字段 | **并列**：`funding_rate` / `open_interest` / `basis` 作为普通列进面板，`qw.col("funding_rate").ts_mean(24)` 直接可用。**不改 qweave 就能表达大部分加密因子** |
| 跨合约横截面 | 面板加一列 `underlying`，用 `group_rank` / `group_neutralize` 按标的聚合。同样不需要改内核 |
| 三处自研 | JSON AST → `Expr` 构造（20 行）、算子白名单表、跨调用缓存键——全在 qweave 之外，不碰它 |

### 10.2 定制的触发条件（缺一不可）

必须同时满足，才进入阶段 2：

1. **有频次证据**：同一算子缺口在 ≥3 个候选族里出现（说明是表达力不足，不是个别因子）；
2. **无法用并列表或组合表达**：先穷尽 `clip` / `scale` / `group_*` 的组合；
3. **不属于其他工位**：逐 bar 换手惩罚属 `allocate`、重叠调整属 `label`，都不该塞进求值内核。

### 10.3 三条定制路径与代价

| 路径 | 代价 | 适用 |
|---|---|---|
| 上游 PR | 等待 + 上游口味，可能被拒 | 通用算子（任何资产类别都用得上） |
| fork + 本地 patch | 维护分叉；`qweave.__version__` 不再等于真实算子集 | 加密独有、长期需要 |
| 预计算成列 | 无代码分叉；但列膨胀，且新列进不了 CSE 复用 | 少量、一次性 |

### 10.4 一条必须现在就立的规矩

**`operators_version` 必须独立于 `qweave.__version__`，且进 `call_digest`。**

理由：一旦 fork，版本号就不再反映算子语义；若指纹仍取 `qweave.__version__`，两套算子会算出同一个 digest，缓存与对照全部失效。现在就用「算子表内容的哈希」而不是包版本号。

---

## 11. 依赖变更清单

### 新增（进 `requirements.txt`）

| 包 | 用途 | 版本策略 |
|---|---|---|
| `cvxpy` | `allocate` 约束路径、`combine` 联合优化 | `>=1.9,<2` |
| `duckdb` | 事实层只读查询面 | `>=2.0,<3` |
| `filelock` | 跨进程锁（新代码） | `>=4.2,<5` |
| `structlog` | 新模块结构化日志 | `>=26.1` |
| **`deap`** | **默认 GP 实现 `gp/evolutionary@1`** | `>=1.4,<2`；**LGPL-3.0**，只作未修改依赖 |

`deap` 进默认依赖而不是「按需」，因为它不是可选实验：`search` 的开放档要能开箱跑一个 GP，否则「新增一个挖掘算法」这条扩展点的第一跳就要先装包。

### 按需（不进默认依赖，声明了才装）

| 包 | 用途 | 说明 |
|---|---|---|
| `stable-baselines3` / `torch` | RL / NN 范式 | `needs_fitness=true` 或 `output_kind=model_ref` 时才装 |
| `statsmodels` | **仅 dev 对拍** | 进 `requirements-dev.txt`；生产代码不得 import |

### 明确不采用

| 包 | 理由 |
|---|---|
| `gplearn` | 无多目标选择；AST 只能靠私有字段反推；无 checkpoint。见 §5.6 与 `07-components.md` §7 |
| `PySR` | 需 Julia 运行时，Windows 部署成本高；阶段 2 再评估 |

### 版本地板调整

| 包 | 从 | 到 | 原因 |
|---|---|---|---|
| `scipy` | `>=1.9.0` | `>=1.11` | `false_discovery_control` 在 1.11 才有；BH 不再依赖 statsmodels |

### 明确不新增

`statsmodels`（仅 dev 对拍）、`PyPortfolioOpt`、`riskfolio-lib`、`diskcache`、`blake3`、`pandas-market-calendars`（已由 ml4t 传递带入）、`pandera`、`optuna`、`transitions`、`alphalens-reloaded`、`quantstats`、`empyrical`、`zarr`。

其中 `alphalens-reloaded` 的结论基于**接口判断**而非实测：它算 IC 与分位收益，但拿不到零分布与行内置换（判据核心），且是逐日 pandas groupby；本次装它时本机在 `RECORD` 文件上遇到 Windows `WinError 5`，未完成安装，因此没有性能数字。**待后续复核**。

---

## 12. 对既有结论的修正（诚实记录）

| 上一轮的说法 | 本轮实测 | 处理 |
|---|---|---|
| 「新增 `evaluate`，用 polars `LazyFrame` 做 DAG」 | qweave 的 Rust DAG 更深（CSE + slot 复用 + 元素级融合 + 节点级并行），Alpha158 全量 17.3 ms | **已被 07 §4 取代**，polars 退回「读取与对齐」职责 |
| 「跨调用节点缓存需要落盘」 | 当前规模重算 0.2 ms/表达式，读盘 13–16 ms，**净亏 65 倍** | **撤回**，默认关闭，见 §8 |
| 「`statsmodels` 用于 BH/Holm」 | scipy 的 BH 与它逐位相同；自研 HAC 与它逐位相同且快 2 倍；scipy 缺 Holm | 生产不引入，**仅 dev 对拍** |
| 「`PyPortfolioOpt` / `cvxpy` 二选一」 | PyPortfolioOpt 1.6.0 在 scipy 1.18 下 HRP 与 EF **双崩** | **只用 cvxpy** |
| 「`net` 升格 `backtest`，可以两档同引擎」 | ml4t 0.388 s/候选；cvxpy 约束路径 0.35 s/候选 | 成立；合计约 0.74 s/候选 → 200 候选 **2.5 min** |

---

## 13. 不变量与验收（接 05/06 的编号）

> 编号与陈述以 `05-contracts.md` §1（不变量）与 §4（验收）为准；本表只补本篇的检查点，不重复定义。

| 编号 | 内容 | 检查点 |
|---|---|---|
| I27 | 影响因子数值的库版本进 `call_digest`；`operators_version` 取算子表内容的哈希，不取包版本 | 改 `qweave` 版本或 fork 算子表 → digest 必须变；只改 `qweave` 的注释性版本号 → 也应变（保守） |
| I28 | 一次执行内，`allocate` 的约束集合只有一个指纹，cvxpy 问题对象按该指纹复用 | 237 次调仓只允许一次 `canonicalize`；交换约束参数 → 新对象 |
| I29 | 重试只允许发生在 `pure` 工位 | 对 `append_only` 工位注入重试 → 编译失败 |
| AC29 | 分位路径与约束路径在同一 `caliber` 下给出同一份 `Ledger` 结构 | 两条路径的 `DetailLevel=summary` 输出字段逐项相同，只有权重量级不同 |
| AC30 | 默认配置下不产生任何节点级磁盘缓存文件 | 跑完整八步后 `user_workspace/.eval_cache/node/` 不存在 |
