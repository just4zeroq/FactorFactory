# FactorFactory

**声明式加密因子挖掘流水线**：一份配置单进，一条经过多重检验的因子出。

不是「又一个回测框架」，而是把**研究结论的可信度**当作一等公民的工程化系统：口径唯一、判据不可放宽、数据不可前视、结论不可手工修饰。

---

## 状态

实现推进到 **S7（认证）**，八工位全链可跑（`--live` 走真实采集数据）。

| 能力 | 状态 |
|---|---|
| 契约 / 指纹 / 编译期校验 | 完成 |
| 供给链（采集 → 门禁 → 原子发布） | 完成 |
| 搜索（先验池 / GP / 随机基线 / 多算法） | 完成 |
| 评估 / 裁判（DSR / FDR / 置换检验） | 完成 |
| 组合 / 回测（含资金费） | 完成 |
| 单步执行 / 断电恢复 | 完成 |
| walk-forward 认证 | 完成 |
| 因子库（promote / db: 回灌） | 完成 |
| Web 界面 | 不提供（CLI 单入口） |

---

## 三条红线

1. **名字拼错、参数写错、数据版本不适用 → 编译失败**，绝不静默换成别的。
2. **裁判只有一个**：配置里不能指定另一个实现，也不能配一个更宽松的判据。
3. **口径类参数改动 = 新实验**：成本、入场价、零分布、阈值都进指纹，不能当普通升级。

第 4 条与加密强相关：**年化一律 365 天 / 24 小时制；资金费只按事件求和，不按固定周期乘**。

---

## 架构

七层，权限单向收紧——**只有编排层有权开执行、计试验次数、揭示保留区、发布结论**；能力层「给什么算什么」，不读配置、不写账本、不自选口径。

```
应用层    CLI 壳（无业务逻辑、无口径参数）
  ↓
意图层    配置 → 冻结方案（跑起来之后不许改）
  ↓
编排层    固定八步 / 状态机 / 事件账本 / 唯一裁判
  ↓
能力层    按名字调用，纯函数式
  ↓
事实层    不可变 / as-of / 版本 / lineage
  ↓
存储层    对象内容寻址 / 登记 / 索引 / GC
  ↑
跨层设施  日志 / 追踪 / 计时 / 随机源 / 指纹 / 缓存
```

**固定八步**（顺序不可换、不可跳）：

```
Boundary → Panel → Target → Search → Evidence → Verdict → Portfolio → Certify
```

回测不是工位，是共享能力 `backtest`——第 5/6/7/8 步共用同一份账本口径。

---

## 安装

需要 Python **3.11+**（开发用 3.12）。

```bash
git clone https://github.com/just4zeroq/FactorFactory.git
cd FactorFactory

python -m venv venv
source venv/Scripts/activate        # Windows Git Bash
# source venv/bin/activate          # Linux / macOS

pip install -r requirements.txt
```

> `ml4t-backtest` 不在 PyPI，从 git 固定 commit 安装（见 `requirements.txt`）。**不要**手动改它的 SHA：适配器依赖那个 build 的执行语义，换版本等于换研究口径。
>
> `qweave` 精确锁 `0.6.2`：算子集合与 NaN/窗口语义是 caliber 的一部分。

---

## 快速开始

跑通只需要一个「数据根」——供给、票据、账本、日志都写在里面，**默认 `~/factor-miner-v5`**，也可用 `FLOW_DATA_ROOT` 指到别处。仓库本身保持干净。

### 0. 准备数据根

```bash
export FLOW_DATA_ROOT=~/factor-miner-v5        # Windows PowerShell: $env:FLOW_DATA_ROOT="C:\data\fm-v5"

mkdir -p "$FLOW_DATA_ROOT/schemas"
cp examples/symbol_sets.json "$FLOW_DATA_ROOT/schemas/"
```

`schemas/symbol_sets.json` 是「标的集名 → 成员清单」的表。计划的 `supply.symbols` 只写集合名（**不内联成员**），编译期到这里解析；表不在，`run` 会以 `E1004` 拒绝，而不是猜一个标的池。

### 1. 采集

```bash
python -m v5.app.cli fetch \
  --datasets ohlcv,funding,open_interest,basis \
  --symbol-set crypto20 --exchange binanceusdm --market futures \
  --start 2024-01-01 --end 2026-09-13
```

`--exchange` 没有默认值：写错交易所比写错参数更难发现。OHLCV 之外的三条事件流只存在于合约市场；现货只允许 `ohlcv`。

**全部标的被跳过 → 命令失败退出**（`E4002` / exit 4），并逐标的给出 `skipped` 原因。「标的集与市场不匹配」「交易所 id 写错」「区间内没数据」在结果上长得一样，所以不能返回成功。

### 2. 体检

```bash
python -m v5.app.cli doctor --plan examples/plan_demo_crypto20.json
```

逐数据集给出：标的数、行数、`version`、`coverage`、`interval_ns`、`end_closed`、`gaps`、行数偏薄的标的。带 `--plan` 时还会核对计划声明的数据集与标的集成员是否都到位。

### 3. 跑一次完整流水线

```bash
python -m v5.app.cli run --plan examples/plan_demo_crypto20.json --live
```

产出冻结方案（五个顶层指纹）+ 八个工位的 parcel + 每条候选的判据 + walk-forward 认证结果，落在 `$FLOW_DATA_ROOT/runs/<run_id>/`。

去掉 `--live` 走 dummy 桩——只验证链路，不读真实数据。

### 4. 看结果

```bash
python -m v5.app.cli inspect --run-id <run_id>
python -m v5.app.cli inspect --run-id <run_id> --station Verdict --full
```

### 5. 入库与复用

```bash
python -m v5.app.cli promote --run-id <run_id> --candidate <candidate_id> --yes
python -m v5.app.cli factors --status active
```

入库后，后续计划的 `prior_pool` 里可以直接写 `db:<name>` 引用它——先验池回灌，新计划不必从零开始搜。

> **关于两份样例计划**：`examples/plan_demo_crypto20.json` 是最小可跑样例（20 标的、3 个先验因子 + 60 条随机表达式）；`examples/plan_cs_2026q4.json` 是完整形态（多算法 + 固定 ridge 组合 + 5 折认证），它引自设计文档里带注释的 YAML 样例，两者内容等价。**CLI 只读 JSON**，YAML 那份是给人读的规范文本。


---

## 命令一览

| 命令 | 用途 |
|---|---|
| `run` | 跑完整八工位（`--live` 走真实能力层） |
| `doctor` | 供给体检：覆盖 / 缺口 / 末端闭合 |
| `fetch` | 四数据集采集（ohlcv / funding / open_interest / basis） |
| `explain` | 解释某个参数从哪来（编译期展开的来源标注） |
| `stations` / `station` | 工位清单 / 单步执行 |
| `inspect` | 查 run 的工位产物 |
| `evaluate` / `backtest` / `certify` | 从冻结 ticket 重算，**只读** |
| `promote` / `factors` / `factor_db` | 因子库：入库 / 查询 / 下架 |
| `holdout` | 揭示保留区（只揭示一次） |
| `reconcile` | 登记库 / 产物链 / 对象存储三方核对 |
| `estimate` | 按口径估算每期与年化成本 |
| `pipeline` | 端到端编排（带告警） |

所有命令支持 `--format {table,json,ndjson}`。

---

## 仓库结构

```
v5/
  contracts/        契约层：错误码、指纹、类型、plan schema（唯一真相）
  facilities/       跨层设施：日志 / 缓存 / 追踪 / 时钟 / 随机 / 通知
  storage/          存储层：对象、登记、索引、GC、账本
  upstream/         供给链：采集、写入、append-only
  runtime/          意图层：编译、门禁 B、票据、信封
  capability/       能力层：12 能力 + 八个工位的 ops 实现 + 算子注册表
  orchestration/    编排层：唯一执行器、能力桥、状态机、事件账本
  app/              应用层：CLI 命令 + 三种输出格式
examples/
  plan_demo_crypto20.json   最小可跑样例计划
  plan_cs_2026q4.json       完整形态样例计划
  symbol_sets.json          标的集表（先复制到数据根 schemas/）
tests/
  conftest.py       v5_supply 夹具（在 tmp_path 造真实 parquet 供给）
  v5/               36 个测试文件
  fixtures/v5/      计划样例 + 黄金面板夹具（20 标的 × 1277 根 1d）
  reference/v4/     冻结的 V4 统计实现，只作逐位对拍的数值 oracle
docs/architecture/v5/
  INDEX.md          文档总索引（从这张表开始）
  01–20-*.md        架构与设计正文
  s0/               可实现化契约
  reviews/          逐模块六维审查（78 份）+ 汇总总账
  plans/            专项计划
  research/         研究分析
```

---

## 测试

```bash
python -m pytest tests/v5 -q
```

全部为可复现的单元/集成测试，含：

- **黄金锚点对拍**：`test_v5_ac63_golden.py` 用手写 pandas 参考实现对同一面板重算，与流水线结果比对（容差 1e-9；跨版本相对冻结值 ±10%）。面板数据（20 标的 × 1277 根 1d）与冻结期望值一起提交在 `tests/fixtures/v5/golden/`，**fresh clone 直接可跑**，不需要先联网采集。
- **统计原语逐位对拍**：`test_v5_verdict.py` 与 `tests/reference/v4/` 里冻结的 V4 实现零容差比对——BH/Holm/DSR/置换 p/Newey-West 必须逐位一致。
- **历史事故回归**：多空收益误用变换后 score、收益多延迟一根、`rebalance` 未真冻结、桥漏传 `horizon_bars`——四类都钉了回归测试。
- **包独立性守卫**：AST 静态扫描，`v5/` 不得 import 旧包。

---

## 加密场景的硬约束

| 约束 | 做法 |
|---|---|
| 年化基准 | 恒 `calendar_365` + 24 小时制，不随 bar 频率变 |
| bar 频率 | 按中位步长推导；1h → 8760，夏普恰为日频的 √24 |
| 资金费 | 按 `(t_{k-1}, t_k]` 事件求和聚合，**不按固定周期乘** |
| 市场维度 | `market ∈ {spot, futures}` 独立于 `exchange`；spot 无 funding/OI/basis |
| 24/7 无收盘 | 未收盘判定靠 bar 时间戳 + 墙钟，不靠交易日历 |
| 上市 / 下架 | 由可观测掩码负责，不填充、不前视 |
| 缺口 | 事件流间隔会变（1h/4h/8h 混合），gap 判定按各自原生间隔 |
| 判据 | DSR / FDR / 置换检验，**永不因「全失败」而下调阈值** |

---

## 文档

从 [`docs/architecture/v5/INDEX.md`](docs/architecture/v5/INDEX.md) 开始——按「你想知道什么」导航。

- 想知道怎么设计的 → `README.md` §1/§3 → `01`–`06`
- 想知道一行结论怎么算出来的 → `06-evaluation.md`
- 想知道每个模块有没有问题 → `reviews/module-review/00_progress.md`
- 想知道配置单每个键什么意思 → `18-plan-schema.md`
- 想知道凭什么信它 → `14-test-strategy.md`、`ACCEPTANCE.md`

---

## 许可

见 [LICENSE](LICENSE)。
