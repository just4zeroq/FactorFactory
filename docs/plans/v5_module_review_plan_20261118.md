# V5 逐模块 Review 计划（加密货币专项）

> 日期：2026-11-18
> 范围：`v5/` 全部 80 个 Python 模块（22,583 行）+ 36 个测试文件 + 26 篇设计文档。
> 目标：对**每个模块**回答同样的六个问题，结论一律落到"加密场景是否成立"。
> 本文件是**计划**，不是结论。六个维度的答案在逐单元执行时填写，按"开发-测试-review-验收"闭环交付。

---

## 0.0 范围界定：本轮"生产可用" = 可用于真实因子挖掘（2026-11-18 用户裁决）

**本轮不做**：可观测性/告警、备份容灾、容器化部署、密钥合规、成本预算、oncall 交接——这些是"系统级运维生产化"，不是本轮目标。

**本轮做**：把 V5 变成研究员可以**真的拿来做因子挖掘**的工具。下列七条是统一判据，每个模块的六维审查最终都要回答"对它有帮助还是有破坏"：

| # | 判据 | 当前证据基线 |
|---|---|---|
| **R1 真数据跑通** | `--live` 自采数据八工位全绿，非 dummy | 已通（20261117/18 多次真跑） |
| **R2 结论可信** | DSR/FDR/置换零分布正确、PIT 无前视、资金费按事件、样本外认证 | 判据已修；watch 满权重待审 |
| **R3 因子零代码扩展** | 资金费动量 / OI 变化 / 基差回归 / 期限结构 / 跨所价差 → 不改 Python | **未通**：五个加密流未进面板、列集未开放、合约维度未拆 |
| **R4 搜索有效** | 多算法 + 正确计数去重 + 随机基线对照 + 能自主发现非平凡因子 | 已通（GP 自主发现 basis carry `sum(ref(basis,6),10)`） |
| **R5 单因子可归因** | 组合收益能拆到成员（六步隔离），不被组合掩盖 | 流程已定（`20-mining-algorithms` §6），工具链靠手工拼命令 |
| **R6 迭代复用** | 因子库闭环、先验回灌、上轮结论进下轮假设 | 已通 |
| **R7 fail-closed** | 不可观测有因、缺数据/缺标的拒、口径与判据不可静默改写 | 大部分已通，E1019 等个别待审 |

**由此调整优先级**：直接决定 R3（扩展）与 R2/R5（可信/归因）的 **U6 数据面板、U7 搜索内核、U8 裁判、U9 组合回测、U10 认证** 是全轮重点；U3/U4/U5 次之；U1/U2/U11/U12 是接口与验证。

---

## 0. 统一审查框架（每个模块都问这六问）

| 维度 | 问题 | 输出形态 |
|---|---|---|
| **D1 应然** | 抛开现有实现，这个模块在加密研究系统里**应该**做什么、边界在哪 | 一页理想态规格：输入/输出/不变量/禁止项 |
| **D2 差距** | 实现**少了什么、多了什么**；明确"删什么、补什么" | gap 清单，每条标 P0/P1/P2 + 依据 |
| **D3 业界** | 业界有哪些方案，哪些**值得借鉴**，哪些是股票遗留必须拒绝 | 方案对比表 + 采纳/拒绝裁决 + 加密理由 |
| **D4 代码** | 实现层面的 bug、静默成功、口径漂移、边界错误、性能陷阱 | 文件:行号 + 可复现证据 + 严重度 |
| **D5 文档** | 设计文档是否需要更新（契约演进需先改文档） | 篇目:行号 + 应改成什么 |
| **D6 扩展** | 模块是否需要扩展；当前实现是否支持该扩展（**加因子不改 Python**） | 扩展场景 × 当前支持度矩阵 |

### 0.1 每个模块都必须过的加密专项检查表

不分模块，逐条判定"成立/不成立/不适用"，不成立的进 D2：

| # | 加密事实 | 典型错误 |
|---|---|---|
| C1 | 年化一律 **365 天 / 24 小时**，不依赖库默认值 | 252、交易日日历、`periods_per_year` 缺省 |
| C2 | 资金费**按事件求和**；结算周期 1h/4h/8h **会变且同合约历史上换过** | `rate*3`、`funding_interval_hours`、按文件名推频率 |
| C3 | 现货无资金费；`market ∈ {spot,futures}` 独立于 `exchange` | 用 exchange 推 market、spot 目录读 funding |
| C4 | 标的身份是 `(exchange, underlying, contract_type, settle_ccy)` 四元组 | 把 `BTCUSDT` 与 `BTCUSDT_250926` 当同一资产 |
| C5 | USDT/USD 有偏离；`BTCUSDT` ≠ `BTCUSD` | 把计价货币溢价算进 alpha |
| C6 | 上市首日极端收益、下架前最后价不可交易 | 未处理的可用时间 → 前视/不可成交 |
| C7 | 24/7 **无休市但有缺口**（维护、合约迁移） | 用交易日历判"未收盘"；缺口静默 ffill |
| C8 | 数据列集**开放**（funding/OI/basis/清算/跨所价差） | 固定 OHLCV 五列、加列要改代码 |
| C9 | 事件字段 as-of 对齐，**只 ffill 不 bfill** | 前视填充把未来资金费喂给当前 bar |
| C10 | 高波动下判据不得被"加密波动大"放松 | 悄悄调 dsr_min/fdr_max/清零 n_trials |
| C11 | 可交易性由**流动性分层**决定，不是全池 | 把不可交易小币计入 RankIC 或权重 |
| C12 | 24/7 下 1 观测 = 1 天；purged CV 的 embargo 以观测为单位 | 用交易日折算 embargo |

### 0.2 执行顺序（按"真实因子挖掘的因果链"，非目录顺序）

```
U6 工位基础 → U7 搜索内核        （R3 最大缺口：数据流进面板 + 列集开放 + 搜索扩展）
U8 证据与裁判 → U9 组合与回测 → U10 认证   （R2/R5 结论可信与单因子归因）
U3 存储与供给 → U4 运行时 → U5 编排         （R1/R7 数据正确性与 fail-closed）
U1 契约 → U2 设施 → U11 应用 → U12 测试/文档 （接口、横切与验收）
```

每个单元执行闭环：**D1→D6 逐维填写 → 分级清单 → 最小修复集 → 改代码 → 补测试 → 全量测试 → 文档对齐 → 单元验收单**。未经确认不擅自大改；"评估后不改"须写明理由。

---

## 1. 覆盖矩阵：80 个文件全部归入审查单元

| 单元 | 覆盖文件 | 行数 |
|---|---|---|
| **U1 契约与指纹** | `contracts/`：types, digest, errors, exit_codes, plan_schema, `__init__` | 1,444 |
| **U2 跨层设施** | `facilities/`：log, cache, trace, clock, rng, digest_svc, notify, `__init__` | 984 |
| **U3 存储与供给链** | `storage/`：catalog, layout, objects, index, ledger, gc, `__init__`；`upstream/`：store, fetch, `__init__` | 1,967 |
| **U4 运行时/意图** | `runtime/`：compiler, gate_b, store, envelope, charter, `__init__` | 1,270 |
| **U5 编排层** | `orchestration/`：capability_bridge, executor, station, events, state, `__init__` | 2,865 |
| **U6 工位基础能力** | `ops/`：boundary, align, read, label；`capability/`：panel, stream, timeaxis | 1,968 |
| **U7 搜索与因子内核** | `ops/search`；`capability/`：mining, factors, inference, stats, params, registry, spec, impls, runtime | 2,325 |
| **U8 证据与裁判** | `ops/`：evaluate, observe, verdict | 1,774 |
| **U9 组合与回测** | `ops/`：allocate, combine, backtest；`capability/backtest_engine` | 1,863 |
| **U10 认证** | `ops/certify` | 358 |
| **U11 应用层** | `app/cli/` 19 个命令 + `__main__`；`app/contract`；`app/render/` 4 个 | 约 2,400 |
| **U12 测试与文档** | `tests/v5/` 36 文件；`docs/` 20 + `s0/` 6 + ACCEPTANCE + IMPLEMENTATION-PLAN | — |

---

## 2. 逐单元计划（六个维度）

### U8 · 证据与裁判 — `evaluate` / `observe` / `verdict`（1,774 行，P0 首发）

| 维度 | 审查要点（加密视角） |
|---|---|
| D1 应然 | 单一裁判只回答"该候选在**净成本后、样本外、多重检验修正后**是否可交易"。Evidence 只报数不判；零分布必须保留截面结构，不能用整行置换（V4 头号 bug）。 |
| D2 差距 | 待查：①观测的 `raw` 与变换后 score 是否彻底分离（B1 事故回归）；②三档漏斗（粗筛→精算→归因）是否每档落盘；③经济等价去重是否只对软原因降级 watch、不硬拒；④`insufficient_n` 的截面宽度门槛是否按加密池宽度（20）而非股票宽度设定。 |
| D3 业界 | Deflated Sharpe Ratio（Bailey & López de Prado）、BH/BY-FDR（scipy）、White Reality Check / SPA、PBO/CSC。借鉴 DSR 的试验次数惩罚；拒绝股票 alfalens 的逐日 groupby 与固定 `periods=(1,5)`。 |
| D4 代码 | 逐行核对 `inference.py` 的**行内跨资产置换**、HAC t/BH/Holm 公式；`observe._redundancy()` 相关性聚类阈值是否写死；`verdict._decide()` 软原因集合是否与文档一致；sigma 默认 `sampling` 与 `n_trials` 是否被任何路径改写。 |
| D5 文档 | `06-evaluation.md`、`s0/03-runtime.md` 的判据口径与 AC63 黄金锚；改判据必须先改文档（红线）。 |
| D6 扩展 | 新判据（如 PBO）应只加在 Verdict 内部且不新增工位；当前 `_SOFT_REASONS` 是硬编码集合，加软原因需改代码——评估是否升为注册表。 |

### U9 · 组合与回测 — `allocate` / `combine` / `backtest` / `backtest_engine`（1,863 行，P0）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 同一冻结口径跑完整成交模拟，产出唯一 Ledger；`summary`/`full` 同源。分位建仓 + 约束投影两条路径；净表现只在认证段可发布。 |
| D2 差距 | 待查：①`rebalance` 是否真正在 allocate/observe 同网格冻结（B3 事故回归）；②资金费事件链三调用点是否全透传（D-002）；③`combine` 风险最优模式返回权重而非分数是否已在 I23 登记、`allocate` 是否 pass-through；④`corr_max`/`orthogonalise` 是否覆盖加密的合约间高相关。 |
| D3 业界 | cvxpy（约束投影）、skfolio（HRP/CVaR，仅 combine 与 certify）、ml4t-backtest（撮合/保证金/强平/资金费）。拒绝 PyPortfolioOpt（scipy 1.18 崩溃）、自写撮合循环。 |
| D4 代码 | `backtest_engine` bar 频率闭合（1h→8760、√(365/h) 年化）；杠杆 >1 直接 E1004；`missing_price_cells` 单格 NaN 处理；gross_bar 只截不多；parquet 序列化固定。 |
| D5 文档 | `06-evaluation.md` §成本/三段窗口、`07-components.md` §3、`09-app-storage.md` ledger 结构。 |
| D6 扩展 | 新合成器是"封闭多选"档：能选不能加；当前 `portfolio.combiner` 三实现（equal/score/fixed_ridge）覆盖基本场景，风险最优（skfolio）尚未接入——是否补属 P1 决策项。 |

### U10 · 认证 — `certify`（358 行，P0）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | walk-forward 逐折用同一判据复算，只认 `out_of_sample`；reproducible manifest 逐字节确定。 |
| D2 差距 | 待查：①逐折净表现回填是否完整；②embargo 是否以观测为单位（C12）；③`tested:false` 的 watch 候选拿组合满权重进 OOS 的设计观察是否已处理；④holdout 揭示是否只在认证段发生。 |
| D3 业界 | CombinatorialPurgedCV（skfolio）、TimeSeriesSplit；拒绝用日历算 purge。 |
| D4 代码 | 折边界与 embargo 索引、逐字节 manifest 的字段排序与浮点序列化。 |
| D5 文档 | `05-contracts.md` I23、`s0/04-capability.md` L575 组合准入、`12-stages.md` S7 门槛。 |
| D6 扩展 | 新风险度量（CDaR/EDaR）走 skfolio 配置，不改代码——当前未接，属扩展缺口。 |

### U6 · 工位基础能力 — `boundary` / `align` / `read` / `label` / `panel` / `stream` / `timeaxis`（1,968 行，P1）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 只读精确版本；未收盘 bar 丢弃靠时间戳+墙钟而非日历；可观测/可交易两张掩码分离；标签逐时点截面排序+中性化，宽度不足显式丢弃。 |
| D2 差距 | 待查：**五个加密流（funding/OI/basis/清算/跨所价差）是否真正进面板**（C8：当前最大 alpha 缺口）；面板列集是否开放；合约四元组维度是否拆列；`available_from` 是否处理上市首日/下架（C6）；流对齐是否 as-of 只 ffill（C9）。 |
| D3 业界 | ccxt `markets` 元数据（contractType/expiry/settle）、polars pivot/join-asof、qweave `with_labels`。拒绝 pandas-market-calendars 参与业务判断。 |
| D4 代码 | `stream.py` 642 行的字段映射与 `OBSERVABLE_CODES`；`align` 缺口判定与 NaN 传播；`label` 的 `forward_raw`/`build_raw_by_h`/`_holdout_touch` 与 IC 衰减共用同一实现；`timeaxis` 365 常量统一。 |
| D5 文档 | `10-crypto-fit.md` §7（加密特有缺口）、`11-field-operator-registry.md`、`s0/01-data-contracts.md`。 |
| D6 扩展 | **加一个加密因子必须零代码**（`10-crypto-fit.md` §8）：当前流接入与列集开放是 D6 的前置。逐场景测：资金费动量/OI 变化/基差回归/期限结构/跨所价差。 |

### U7 · 搜索与因子内核 — `search` / `mining` / `factors` / `inference` / `stats` / `params` / `registry` / `spec` / `impls` / `runtime`（2,325 行，P1）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 唯一开放扩展点：四方法接口 `describe/fit/generate/lineage`；`generate()` 不评估、反馈只在 `fit(mine_only)`；跨算法共享一次试验预算与内容身份去重。 |
| D2 差距 | 待查：①内置因子真实供给是否只有 Alpha158 的 158 个（Alpha101/191 不可用）；②先验池 `db:*`/内置名/算法名三来源是否同判据；③`params.json.defaults` 是否混入 plan/指纹（G3/G5 回归）；④新算法分派 `_RUNNERS` 是否只需加一处。 |
| D3 业界 | deap（默认 GP，多目标+类型约束+可续跑）、模板枚举、贝叶斯优化/CMA-ES、NSGA-II、随机对照。拒绝 gplearn（AST 靠私有字段）、gplearn/NN/RL/LLM 直接进 Search（复现性与内容寻址不成立）。 |
| D4 代码 | `mining._OpCodes` 与 registry arity 一致；deap 全局 random 快照/还原与 E2007；`impls` 实现名解析 E1002；`params.json.defaults` 不进指纹；`registry` 加载自检 E1015。 |
| D5 文档 | `02-mining-interface.md`、`20-mining-algorithms.md` §3/§4、§6 六步链路。 |
| D6 扩展 | 加新算法是否真"只动一处"：按 `20-mining-algorithms.md` §4 五步逐项实测；RL 解锁条件（截面≥100 + GP pass 饱和）当前是否成立。 |

### U3 · 存储与供给链 — `storage/*` + `upstream/*`（1,967 行，P1，加密数据红线）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 方案 B 无 manifest/版本目录/别名表：`<root>/<dataset>/<exchange>/<market>/<SYMBOL>[-tf].parquet`；版本=checksum 前 16；append-only，冲突只计数；事实层不可变、as-of、可重建。 |
| D2 差距 | 待查：①`fetch` 零成功必须失败 E4002/exit4；②市场维度护栏三层（计划 E1004/采集前拒/门禁 B E4002）；③资金费/ OI/basis 不带 tf（周期会变）；④GC/index 未做（已知遗留）；⑤跨交易所目录是否支持（C4/C5 跨所价差前置）。 |
| D3 业界 | Parquet/Arrow 列存、DuckDB 查询面、content-addressed storage、event sourcing append-only log。拒绝原地 `to_feather` 静默改写历史（V4 缺陷）。 |
| D4 代码 | `layout` 路径构造与 FACT_KINDS（含 `parcel_data`）；`ledger` 锁内重扫回并；`gc` 引用计数边界；`upstream/store` 657 行的原子写与 checksum 计算；`fetch` 分页/重试是否只有一条实现。 |
| D5 文档 | `19-supply.md`（方案 B）、`09-app-storage.md` §3、`s0/06-upstream.md`。 |
| D6 扩展 | 新数据集（清算/链上/跨所）是否只需配置：当前 `MARKET_DATASETS` 硬编码 spot 只有 ohlcv——评估是否升注册表。 |

### U4 · 运行时/意图 — `compiler` / `gate_b` / `store` / `envelope` / `charter`（1,270 行，P1）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 配置编译成冻结方案，运行期不许改；基线默认编译期展开并标来源；八个指纹唯一公式；门禁 B 三条件（标的集 digest / 新鲜度 / 供给维度）。 |
| D2 差距 | 待查：①`supply.market` 是否进入 plan schema 与门禁比对（已补，回归）；②E1020 同名字段检测；③E1004 live 至少一算法；④E1019 因 `available_from` 语义暂不实现——是否补。 |
| D3 业界 | 编译期校验（schema-first）、不可变构建产物、内容寻址指纹。拒绝运行期解析"最新"数据、插件热加载。 |
| D4 代码 | `compiler.py` 475 行 C1–C22 逐条；`gate_b.py` 280 行三条件；指纹公式是否只在 `contracts/digest` 一处（唯一公式红线）。 |
| D5 文档 | `04-runtime.md`、`18-plan-schema.md`、`s0/03-runtime.md`。 |
| D6 扩展 | 新字段/新口径是否需改编译器：口径类字段应只进 `caliber` 四处，工位出现同名字段编译失败。 |

### U5 · 编排层 — `capability_bridge` / `executor` / `station` / `events` / `state`（2,865 行，P1）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 唯一有权者：开执行、计试验、揭示 holdout、发布结论；八工位固定顺序；整体与单步共用同一执行器；单步默认观察不提交。 |
| D2 差距 | 待查：①事件账本落盘但单步 events 仍内存语义（已知遗留）；②M2 跨进程对象重建是否对拍；③stale 传播 E3002；④`use_capabilities` 开关两路径是否等价。 |
| D3 业界 | 编排器（saga/状态机）、event sourcing、幂等 commit、orchestration 与 execution 分离。拒绝任意 DAG、第二套执行路径。 |
| D4 代码 | `capability_bridge` 1,481 行六通道/对象表/data 落 Ref/exposures；`executor` 1,025 行唯一执行路径；`call_digest` 是否含工位块整体（impl+params+time_axis）；AST 守卫防第二份 `Call`。 |
| D5 文档 | `03-orchestration.md`、`s0/05-orchestration.md`、`09-app-storage.md` mutation 等级。 |
| D6 扩展 | 新工位不做；新能力通过六通道接入；`METHOD_STAGES` 派生是否支持 S7 组合域扩展。 |

### U1 · 契约与指纹 — `contracts/*`（1,444 行，P2）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 49 错误码零重复缺号；八指纹带 kind 包装、唯一公式；错误对象带 `retryable` 不带 `exit_code`；plan_schema.json 是唯一真相。 |
| D2 差距 | 待查：`types.py` 835 行是否有冗余类型；`caliber_field_names()` 是否与 `params.json` 同步；exit 3（契约违反）与 1（执行失败）是否严格区分。 |
| D3 业界 | JSON Schema 契约优先、typed error taxonomy、content hashing（git object 模型）。 |
| D4 代码 | `digest.py` 497 行公式一致性；`errors.py` 273 行码表；`plan_schema.py` 只做结构层不校 params（故意设计，不误改）。 |
| D5 文档 | `05-contracts.md`、`18-plan-schema.md`、`s0/01-data-contracts.md`。 |
| D6 扩展 | 新错误码/新指纹是否需全仓同步；中性校验函数是否可复用。 |

### U2 · 跨层设施 — `facilities/*`（984 行，P2）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 日志/追踪/计时/随机源/指纹/缓存横切所有层，不携带业务口径；随机源实例级隔离（deap 全局 random 是唯一豁免）。 |
| D2 差距 | 待查：日志轮转 32MB / F7 去重限流 / F5 LRU 1024 / 磁盘满 E5001 是否齐；notify 告警阈值；cache opt-in 默认空 `cache_stations`。 |
| D3 业界 | structlog、OpenTelemetry、成熟轮转方案。评估"自写日志封装"是否仍必要（历史上讨论过）——若被问到，给替换成本对比，不擅自重写。 |
| D4 代码 | `log.py` 477 行是否过度自研；`cache.py` 命中是否验 payload 且 trace 标 origin；`rng.py` 25 行 seed 派生确定性。 |
| D5 文档 | `s0/02-crosscutting.md`、`15-operations.md`、`16-security.md`。 |
| D6 扩展 | 新设施（指标/告警通道）是否影响指纹——不应影响。 |

### U11 · 应用层 — `app/cli/*` + `app/render/*` + `app/contract`（约 2,400 行，P2）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 纯壳：不含业务逻辑、不含口径参数；13 条 V5 命令（run/explain/stations/station/inspect/fetch/doctor/estimate/pipeline/promote/factors/factor_db/holdout）+ 内部命令（live/reconcile/replay）；evaluate/backtest/certify 单命令只认冻结 ticket、read-only。 |
| D2 差距 | 待查：CLI 参数是否有默认口径泄漏；`--live` 与 dummy 路径是否等价；render 三格式字段一致性；`explain` 是否回答每个参数来源。 |
| D3 业界 | CLI 框架（click/typer）、结构化输出 NDJSON、只读回放。拒绝 Web 交付（已定不交付）。 |
| D4 代码 | `run.py` 180 行、`live.py` 623 行、`replay.py` 253 行的幂等与 read-only；CLI 名 `flow` 与文档路径 `v5/` 的一致性。 |
| D5 文档 | `09-app-storage.md`、`17-walkthrough.md`、`ACCEPTANCE.md`。 |
| D6 扩展 | 新命令是否只加壳：单因子隔离六步链路当前靠手工组合命令——评估是否需一个 `isolate` 命令（P2 决策项）。 |

### U12 · 测试体系与设计文档（P1，验收依据）

| 维度 | 审查要点 |
|---|---|
| D1 应然 | 五层测试；加密已知答案对拍；拒绝矩阵；37 个 `tests/v5` 文件覆盖每模块；`.gitignore:145` 忽略 test_v5_*，新增须 `git add -f`。 |
| D2 差距 | 待查：①是否有测试钉住 C1–C12 每条加密事实；②AC63 黄金锚 1e-9 / 跨版本 ±10% 两层容差；③S6 告警/日志轮转/磁盘满测试；④重复的测试函数定义（历史 `cat >>` 遗留）是否清零。 |
| D3 业界 | 已知答案测试、属性测试、快照/逐字节对拍、CI 门禁。 |
| D4 代码 | 测试是否只验结果不验口径来源；stub 是否覆盖新增能力字段（历史 stub 缺 redundancy/missing_price_cells）。 |
| D5 文档 | 26 篇文档与代码是否逐条对齐；`ACCEPTANCE.md` 38 项（历史）是否反映当前状态；改契约先改文档红线。 |
| D6 扩展 | 新模块是否强制配测试；参数化测试覆盖加密多频率（1h/4h/1d）。 |

---

## 3. 计划确认点（执行前请裁决三项）

1. **执行节奏**：按 U6→U7→U8→U9→U10→U3→U4→U5→U1→U2→U11→U12（真实因子挖掘因果链，见 §0.2）逐单元串行执行，每单元交付一份审查单（六维答案 + 分级清单 + 最小修复集 + 测试结果 + 文档对齐）；还是先并行只做 D1/D2/D3（不碰代码），汇总后再决定修哪些？
2. **改动边界**：D2 中 P0（影响结论/加密红线/因子可用性）默认随审查直接修；P1/P2 仅列清单等确认——是否同意？
3. **三个历史决策项**是否纳入本轮重审：①`tested:false` watch 候选进组合满权重；②`combine` 风险最优模式（skfolio）接线；③`isolate` 单因子命令。三项都直接服务于"真实挖因子"，默认**只审查不改**，除非明确要求。

---

## 4. 验收标准（每个单元）

- [ ] 六个维度逐条填写，D2 每条有 P 级与文件:行号依据
- [ ] C1–C12 加密检查表逐条判定
- [ ] 分级清单与最小修复集交付，用户确认后才动代码（P0 除外，按确认点 2）
- [ ] 修改后 `pytest tests/v5 -q` 全绿（当前基线 775 passed）
- [ ] 涉及契约的改动先改文档再改代码
- [ ] 单元审查单归档到 `docs/reviews/unit/`，工作记忆追加当日日志
