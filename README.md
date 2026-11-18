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

# 一、数据同步

所有数据落在**一个数据根**里；仓库本身保持干净。

## 1.1 数据根

```bash
cp .env.example .env      # 仓库已自带一份 .env，可跳过；按需改里面的 FLOW_DATA_ROOT
ROOT=$(python -c "from runtime.store import resolve_data_root; print(resolve_data_root())")
mkdir -p "$ROOT/schemas"
cp examples/symbol_sets.json "$ROOT/schemas/"
```

数据根写在仓库根 `.env` 的 `FLOW_DATA_ROOT`（模板见 `.env.example`，`.env` 已被 `.gitignore` 忽略、不会入库）。启动 CLI 时 `.env` 会注入进程环境，无需 `export`；其它变量（凭据、线程数）也可以放进去。**进程里已有的环境变量优先**，所以 CI 或一次性命令仍可用 `FLOW_DATA_ROOT=/abs/path python -m app.cli ...` 临时覆盖，`.env` 不覆盖它。

解析优先级（`runtime/store.py`）：

```
显式入参  >  shell / CI 的 FLOW_DATA_ROOT  >  .env  >  计划里的 operations.data_root  >  ~/factorfactory
```

`.env` 查找顺序：`$FLOW_ENV_FILE` 显式路径 → 当前目录 `.env` → 仓库根 `.env`，**不向上逐级搜索**（避免静默捡到无关配置）。从别的目录启动就用 `FLOW_ENV_FILE=/abs/path/to/.env`。格式写错（不是 `KEY=VALUE`、引号未闭合、变量占位没解析）会以 `E1004` 明确失败，不会退回默认值。

相对路径直接拒绝（`E5004`）——相对路径会随 cwd 漂移，可复现性就没了；`~/...` 会展开成绝对路径，可以用。

数据根本身的布局（**路径即约定：没有 manifest、没有版本目录、没有别名表**）：

```
<数据根>/
  ohlcv/<exchange>/<market>/<STEM>-<tf>.parquet     # bar，tf 是身份的一部分
  funding/<exchange>/<market>/<STEM>.parquet        # 事件流，文件名不带 tf
  open_interest/<exchange>/<market>/<STEM>.parquet
  basis/<exchange>/<market>/<STEM>.parquet
  schemas/symbol_sets.json                          # 标的集名 → 成员
  runs/<ticket_id>/                                 # 一次执行的全部产物
  db/{ledger,trials,events,published,members}.jsonl # 登记库（append-only）
  facts/ catalog/ cache/ tmp/ plans/ baselines/     # 派生 / 因子库 / 可删 / 暂存
```

`<STEM>` = ccxt 符号把 `/` 与 `:` 换成 `_`：`BTC/USDT:USDT` → `BTC_USDT_USDT`。

OHLCV 的值列固定 5 个：`open` `high` `low` `close` `volume`；事件流各 1 列：`funding_rate` / `open_interest` / `basis`。多写一列会让 checksum 变化而无从解释，所以列集是契约。

`1d` 与 `4h` 是**两份数据**（各自的 tf 文件），不是同一份数据的两种视图。

## 1.2 标的集表

计划里 `supply.symbols` 只写**集合名**，不内联成员；编译期到 `schemas/symbol_sets.json` 解析。表不在 → `E1004` 拒绝，不猜标的池。

格式是**扁平的 `{集合名: [成员...]}`**，不要套 `{"sets": {...}}` 外壳（`fetch` / `run` / `explain` 都只认扁平形态）：

```json
{
  "crypto20": [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT"
  ],
  "crypto20_spot": [
    "BTC/USDT",
    "ETH/USDT"
  ]
}
```

成员必须是 **ccxt 权威符号**：现货 `BTC/USDT`，线性永续 `BTC/USDT:USDT`。改这张表不进任何指纹——但它决定读哪些文件，缺成员会让 `run` 在门禁 B 上以 `E4002` 失败。

## 1.3 采集（从交易所）

```bash
python -m app.cli fetch \
  --datasets ohlcv,funding,open_interest,basis \
  --symbol-set crypto20 --exchange binanceusdm --market futures \
  --start 2024-01-01 --end 2026-09-13
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--datasets` | 是 | — | 逗号分隔：`ohlcv` / `funding` / `open_interest` / `basis` |
| `--symbol-set` | 二选一 | — | 从 `schemas/symbol_sets.json` 取成员 |
| `--symbols` | 二选一 | — | 直接给 ccxt 符号，逗号分隔 |
| `--exchange` | 是 | 无默认 | ccxt 交易所 id（`binance` / `binanceusdm` / `okx`…）。**故意不设默认**：写错交易所比写错参数更难发现 |
| `--market` | 否 | `futures` | `spot` 只允许 `ohlcv`；事件流是合约的定义域 |
| `--timeframe` | 否 | `1d` | 只对 `ohlcv` 生效 |
| `--start` / `--end` | 是（交易所源） | — | RFC3339 或 `YYYY-MM-DD`（按 UTC 解释） |
| `--revise` | 否 | 关 | 覆盖已写历史（默认 append-only） |
| `--limit` | 否 | `1000` | ccxt 每页根数 |
| `--spot-exchange` | 否 | `binance` | `basis` 由 perp/spot 收盘价派生，需要第二个客户端 |
| `--format` | 否 | `json` | `table` / `json` / `ndjson` |

**全部标的被跳过 → 命令失败**（`E4002` / exit 4），并逐标的给出 `skipped` 原因。「标的集与市场不匹配」「交易所 id 写错」「区间内没数据」在结果上长得一样，所以不能返回成功。

实测（2 标的，ohlcv + funding）：

```json
{"dataset": "ohlcv",   "symbols": 2, "rows_added": 2828, "revised": 0, "skipped": []}
{"dataset": "funding", "symbols": 2, "rows_added": 8480, "revised": 0, "skipped": []}
```

### 1.3.1 从币安备份站下载 + 扩大可用币种（`vision`）

交易所逐标的翻页慢，且新币没有多年历史。**备份站 `<https://data.binance.vision>`** 是币安官方按月归档，
`ohlcv` / `funding` 可回溯到 2019/2020 年，一次一个 zip 就是一整月。它和交易所来源落到**同一份 parquet**，
读取侧无需区分。每个 zip 带 `.CHECKSUM`（sha256），下载后强制校验，不符即拒收。

`vision` 把「发现全市场 → 按历史长度筛选 → 拉齐数据」固化成三步：

```bash
python -m app.cli vision list
python -m app.cli vision scan   --min-days 1200 --write-symbol-set pit_all
python -m app.cli vision fetch  --min-days 1200
```

**直连优先**：备份站 `data.binance.vision` 通常可直连且很快（GET 约 0.3s），不要默认挂代理——
实测经 HTTP 代理时 SSL 连接会被成串重置，反而拖垮扫描/下载；只有直连确实不通时才用 `--proxy`。
（`list` 用 ccxt 拉交易所清单，交易所 API 在部分网络下需代理，此时单独给 `list --proxy`。）

- `list`：ccxt 拉全市场 USDT-M 永续清单（约 900 个），缓存到 `schemas/all_perps.json`。
- `scan`：对每个标的用二分探测备份站**最早有归档的月份**（归档月份单调，二分把请求数从
  8 万降到约 6 千），算出可用天数，按 `--min-days` 筛选；产出 `schemas/vision_candidates.json`
  （含每标的 `first_month` / `days` / 筛选条件，可复算），`--write-symbol-set` 同时写标的集供 plan 引用。
- `fetch`：从筛中清单按最早 `first_month` 起下载 `ohlcv` + `funding` 到挖掘数据根。

也可以对已知标的直接用 `fetch --from-binance-vision`（与 `--from-v4` 互斥）：

```bash
python -m app.cli fetch --datasets ohlcv,funding --symbols "BTC/USDT:USDT,ETH/USDT:USDT" \
  --exchange binanceusdm --start 2020-01-01 --end 2026-11-01 \
  --from-binance-vision
```

注意：**备份站无 `open_interest` 月度历史**（交易所只留约 30 天），`basis` 需 perp/spot 同日对齐，
两者仍走交易所来源。`vision scan` 筛出的老币标的集可直接填进 plan 的 `supply.symbols`，
再用已有的 `pit_dynamic_topk` 边界逐时点选流动性 top-k——晚上市标的在上市前的格子天然缺失，不会污染面板。

## 1.4 增量同步（每日）

**append-only**：同一 `(symbol, timestamp)` 的旧值永不覆盖，冲突只计数进 `revised`。所以「再采一次」天然就是增量同步，不需要删旧文件、不需要算断点。

```bash
# 每天把窗口往前推一天；重叠 7 天用来发现交易所改写历史
END=$(date -u +%Y-%m-%d)
START=$(date -u -d "-7 days" +%Y-%m-%d)

python -m app.cli fetch \
  --datasets ohlcv,funding,open_interest,basis \
  --symbol-set crypto20 --exchange binanceusdm --market futures \
  --start "$START" --end "$END"
```

重叠是刻意的：新增行计入 `rows_added`，**历史被改写**只在 `revised > 0` 时暴露。要真的覆盖历史必须显式 `--revise`（唯一会改写已落盘数据的方式，应当记档）。

重复导入同一区间是幂等的——实测 `rows_added: 0, revised: 0`。定时任务每天跑一次即可（cron 不读你的 shell profile，所以显式给变量最稳；`cd` 到仓库根时也会读到那里的 `.env`）：

```cron
0 1 * * * cd /opt/FactorFactory && venv/bin/python -m app.cli fetch \
  --datasets ohlcv,funding,open_interest,basis \
  --symbol-set crypto20 --exchange binanceusdm --market futures \
  --start $(date -u -d "-7 days" +\%F) --end $(date -u +\%F) >> logs/sync.log 2>&1
```

> 数据刷新**不会**逼你重建试验族：`policy_digest` 不含 supply pins，`blueprint_digest` 才含。但同一张 ticket 上的单步/重算会因 checksum 变化以 `E3002` 拒绝续跑（防「在另一份数据上重算出看似正常的结果」）。

## 1.5 体检（采完必须做）

```bash
python -m app.cli doctor --plan examples/plan_demo_crypto20.json
python -m app.cli doctor --dataset ohlcv,funding --format table
```

逐数据集给出：标的数、行数、`version`（= checksum 前 16 位）、`coverage` 起止、`interval_ns`（实测中位步长）、`end_closed`、`gaps`（逐段）、`median_rows` 与偏薄标的。

三条判据写死在命令里，不让调用方猜：

| 情况 | 结果 |
|---|---|
| 目录存在但读出来零行 | `empty`，不是 ok |
| 有缺口（按各自原生间隔判定） | `reason_code=E4002`，不是 warning |
| 末端未闭合（最后一根 bar 还没走完） | `passed=false` |
| 某标的行数 < 中位数一半 | 单独列出（分位/覆盖率的分母会被拖偏），只告警不拦截 |

`--plan` 时额外核对「计划声明的数据集与标的集成员是否都到位」；缺一个就报出来，因为 `read` 在给清单时 fail-closed，等到 `run` 才发现会白烧一次执行。

实测（20 永续 + 现货 + 四数据集，5 组）：

```
ohlcv | binance     | spot    | 1d | 22 标的 | 56740 行 | 2017-08-17..2026-11-14 | passed=true
ohlcv | binanceusdm | futures | 1d | 20 标的 | 29389 行 | 2019-12-19..2026-11-14 | passed=true
funding       | binanceusdm | futures | 20 标的 | 84804 行 | interval_ns=28800000000000（8h）
open_interest | binanceusdm | futures | 19 标的 |   475 行 | 仅 2026-10 起（交易所不给长历史）
basis         | binanceusdm | futures | 19 标的 | 26847 行 | passed=true
plan: missing_datasets=[], missing_symbol_count=0
```

`open_interest` 这种短历史是**数据源事实**，不是采集失败——但它会让 `oi_change20` 类因子在长面板上不可观测，`Evidence` 会给出 `unobservable` 并留痕。

---

# 二、一次完整的因子挖掘

下面这条序列就是一次完整实验，按顺序执行；每步都能单独重跑。

```
0 写计划 → 1 编译自检 → 2 体检 → 3 跑八工位 → 4 看产物
        → 5 重算单点 → 6 入库 → 7 回灌 → 8 保留区 → 9 对账
```

## 2.0 全景（懒人版）

```bash
# 数据根取自 .env；临时换位置用 FLOW_DATA_ROOT=/abs/path python -m app.cli ...
python -m app.cli pipeline --plan examples/plan_demo_crypto20.json --live
```

`pipeline` = 体检 → 跑 → 评估告警。体检不通过就停在 `state="stopped"`（`reason="supply_not_ready"`），不烧执行。它的参数与 `run` 完全一致，另加两个：

| 参数 | 说明 |
|---|---|
| `--dataset` | 只体检指定数据集（逗号分隔） |
| `--force` | 跳过体检；结果里恒带 `doctor_skipped: true`，不假装检查过 |

首次跑建议逐步来，先看清每一环的输出。

## 2.1 写计划

计划是**唯一输入**；`--plan` 只吃 JSON（YAML 是给人读的规范文本，`charter.load_plan` 用 `json.loads`）。

```bash
cp examples/plan_demo_crypto20.json my_plan.json
```

| 样例 | 形态 | 说明 |
|---|---|---|
| `examples/plan_demo_crypto20.json` | 最小可跑 | 20 永续、3 个先验因子 + 60 条随机表达式、单算法 |
| `examples/plan_cs_2026q4.json` | 完整形态 | 多算法（GP 200×30 + 随机 300）、固定 ridge 组合、5 折认证。它引用的标的集 `cs_top60_2026q3` **需要自备**（样例表里没有），照 1.2 的格式加一项即可 |

全字段含义见第三章。

## 2.2 编译自检

```bash
python -m app.cli explain --plan my_plan.json
```

输出每个叶子参数的**来源标注**（`plan` / `baseline` / `override` / `default`）与五个顶层指纹。它和 `run` 走同一套十步编译（含门禁 B），所以可以这样用：

> `explain` 通过 ⇒ `run` 只可能因执行本身失败。

编译器先用**无供给**编译挡一遍（结构层、跨字段、实现名），再去读数据算 pin。顺序反了会把「键拼错」报成「数据文件不存在」，把人骗去先采一遍数据。

常用覆盖开关（`run` / `explain` / `pipeline` / `holdout` 同义）：

| 参数 | 用途 |
|---|---|
| `--set a.b.c=v` | 点路径覆盖，值自动转 int/bool/JSON；来源标 `override`，指纹随之改变 |
| `--no-strict` | 跳过必填字段检查（跨字段一致性仍校验） |
| `--allow-constitutive-override` | 允许覆盖构成性块（`supply`/`caliber`/`boundary`/…），不加则 `E1005` |

```bash
python -m app.cli explain --plan my_plan.json --set search.seed=7
```

## 2.3 体检

```bash
python -m app.cli doctor --plan my_plan.json
```

判据见 1.5。`summary.passed=false` 时先修数据再往下跑：`run` 不会因为缺口而失败，它会照常产出候选、照常裁决，只是结论建立在一段有洞的时间轴上。

## 2.4 跑八工位

```bash
python -m app.cli run --plan my_plan.json --live
```

| 参数 | 说明 |
|---|---|
| `--plan` | 必填，JSON |
| `--live` | 八个工位调真实能力层。**不加**则走 dummy 桩（链路演示，不产真实指标） |
| `--set` / `--no-strict` / `--allow-constitutive-override` | 同 2.2 |
| `--format` | `json`（默认）/ `table` / `ndjson` |

即使不加 `--live`，编译期仍要读供给文件算 checksum（进 `blueprint_digest`），**所以数据必须已经采过**——`--live` 决定的是能力层走真实实现还是桩，不是「要不要数据」。

实测输出（20 永续、63 候选）：

```json
{"run_id": "20261118T090912Z-bcdfece8",
 "state": "done", "parcels": 8, "traces": 8, "origins_count": 62,
 "fingerprints": {"plan_digest": "sha256:bcdfece8…", "blueprint_digest": "sha256:36bf5087…",
                  "policy_digest": "sha256:89d59a88…", "caliber_digest": "sha256:a9d99a1d…",
                  "domain_digest": "sha256:e89ad436…"},
 "supply_datasets": ["basis", "funding", "ohlcv", "open_interest"],
 "stage": "S7-live-capability", "exit_code": 0}
```

`run_id` 形如 `<UTC 紧凑时间>-<plan_digest 前 8 位>`，后面所有命令都用它定位。

`state` 的三种结局都是**结论**，不是故障：`done`（跑完）、`skipped`（零候选过闸，`skip_reason` 说明原因，退出码仍为 0）、失败（带 error 对象与非零退出码）。

## 2.5 看产物

```bash
python -m app.cli stations                                   # 八工位清单（顺序）
python -m app.cli inspect --run-id <run_id>                  # 逐工位摘要
python -m app.cli inspect --run-id <run_id> --station Verdict --full
```

摘要字段：`keys`（payload 顶层键，判断这站算没算）、`count`（候选/逐候选数量）、`evidence_grade`、`state`、`skip_reason`。

实测这次样例跑：

```
Boundary  {}                                    Search    {"candidates": 63}
Panel     {}                                    Evidence  {"per_candidate": 38}
Target    {}                                    Verdict   {"per_candidate": 38}
Portfolio {}                                    Certify   evidence_grade=out_of_sample
```

63 条候选 → 38 条进入评估（其余记在 `Search.payload.unobservable`，带 candidate_id 与原因）→ 38 条全部 `watch`、`tested:false`。**随机表达式样例本来就不该产出 pass**：这是判据在工作，不是链路坏了。

单步重跑某一工位（只认 ticket 里的冻结方案，没有 `--plan`）：

```bash
python -m app.cli station Boundary --on <run_id>              # 观察：产物写 steps/
python -m app.cli station Boundary --on <run_id> --commit     # 提交：产物写 parcels/ + 追加 state
```

- `--probe` 与 `--commit` 互斥（`E1001`）；无 flag 等同 probe——「没说要提交」不等于「允许提交」。
- `append_only` 工位（Search / Verdict）拒绝 `--commit`（`E3006`）。
- 数据被重新采集过 → `E3002`：不许在「另一份数据」上续跑。

## 2.6 重算单点结论

三条只读命令，全部只认**已落盘的 ticket**（不接 `--plan`，否则「重算」就成了「换参数再算一遍」）：

```bash
python -m app.cli evaluate --on <run_id>                        # 全部候选的暴露 + IC/ICIR
python -m app.cli evaluate --on <run_id> --candidate sha256:21d31199…
python -m app.cli backtest --on <run_id>                        # combine → allocate → backtest
python -m app.cli certify  --on <run_id> --yes                  # walk-forward（写账本必须 --yes）
```

实测（同一张 ticket）：

```
evaluate  → ic_mean -0.0043, icir -0.0370, status observed
backtest  → net_sharpe -2.79, net_return -0.5303, turnover 0.052,
            cost_breakdown {fee 0.3161, slippage 0.1581, funding 0.0108, total 0.4850},
            bars_per_year 365.0, annualisation_used calendar_365
certify   → folds 3, n_bars 81, embargo 5, member_count 38,
            net_sharpe -7.47, net_return -0.1016, evidence_grade out_of_sample
```

这三个数字难看是对的：样例计划的搜索空间是随机表达式，本来没有信号。它们证明的是**成本、资金费、年化、样本外划分都在真跑**。

## 2.7 入库

```bash
python -m app.cli promote --run-id <run_id> --candidate sha256:21d31199… --yes
python -m app.cli factors --status active
```

| 参数 | 说明 |
|---|---|
| `--candidate` | 只晋升指定 candidate_id（可重复）。只能**缩小**范围，不能把 `reject` 拉进库 |
| `--min-grade` | `any`（默认）/ `out_of_sample`（没有 OOS 认证就整体跳过，返回 `reason:"not_out_of_sample"`）/ `in_sample` |
| `--yes` | 必填。promote 是 `writes_facts + writes_ledger` |

三条规则：能不能入库由 Verdict 的 `decision`（`pass` / `watch`）决定，不由你决定；表达式必须来自 Search 产物（按 candidate_id join，找不到 → `E3003`）；重复执行不是错误（跳过并如实报 `already_in_catalog`）。

实测：

```json
{"promoted": [{"factor_id": "sha256:21d31199…", "name": "mom20:21d31199", "decision": "watch"}],
 "counts": {"candidates": 38, "promoted": 1, "already": 0, "skipped": 37},
 "evidence_grade": "out_of_sample", "catalog_total": 38}
```

`factors` 查库：`--status active|retired|all`、`--limit N`。`factor_db` 是写侧：`--retire <factor_id> --note "原因" --yes`（追加一条记录改状态，不删行）。

## 2.8 回灌（把挖到的因子当下一轮先验）

下一份计划的 `search.prior_pool` 直接写引用，不必从零开始搜：

```json
"prior_pool": ["mom20", "basis_carry", "db:mom20:21d31199", "db:*"]
```

| 写法 | 含义 |
|---|---|
| `mom20` | 内置因子池（26 个之一，见下） |
| `db:<name>` / `db:<factor_id>` | 因子库里指定的那条 |
| `db:*` | 库里全部 `active` |

内置因子池（26 个）：`mom5` `mom20` `mom60` `rev5` `rev1` `rv20` `hl_range` `vwap_gap` `vol_ratio` `funding_carry` `funding_carry14` `funding_carry21` `funding_carry_rank7` `oi_change20` `basis_carry` `low_vol20` `low_vol60` `funding_lowvol14` `funding_volratio` `funding_volratio14` `funding_volratio21` `funding_basis7` `funding_volratio_basis` `funding_vol_interaction` `funding14_vol_interaction` `funding14_vol_interaction_scaled`。

两种被排除的情况都会**留痕**（进 `Search.payload.prior_skipped`），不是静默丢弃：

- `field_unavailable`：因子引用了当前数据集没有的字段。例如 `funding_carry` 放进现货计划，或 `vwap_gap` 用到供给里不存在的 `vwap` 列（OHLCV 只采 5 列）。
- `unknown`：名字既不在内置池也不在库里 → `E1015`（配置错误）。

## 2.9 保留区（只看一次）

```bash
python -m app.cli holdout --plan my_plan.json --yes
```

区间来自计划 `boundary.time_axis.holdout`，**命令行不能临时指定**——保留区是研究设计的一部分。揭示写 `db/events.jsonl` 的一次性键；第二次执行不报错，但 `first_time: false`。

`holdout: "reserved"` 时拒绝（`E1004`）：还没划出保留区就谈不上揭示。

## 2.10 对账

```bash
python -m app.cli reconcile
```

三方核对：登记库（`db/*.jsonl` 每行可解析、digest 格式合法）、产物链（`parcel.json` → payload 存在且 digest 对拍、lineage 上游存在）、对象存储（派生 `*_ref` 的对象文件与 sidecar 存在、blob_digest 对拍）。发现不一致 → `E2005` / exit 3，因为「事实层不自洽」不是执行失败。

实测：`tickets 1, parcels 8, registers {trials 0, events 35, published 0, members 0}, violations 0`。

---

# 三、计划配置参考

## 3.1 顶层骨架

```json
{
  "plan": "demo_crypto20",
  "baseline": "v1",
  "supply":   { "exchange": "binanceusdm", "market": "futures",
                "datasets": ["ohlcv","funding","open_interest","basis"],
                "symbols": "crypto20", "frequency": "1d" },
  "caliber":  { "execution": {}, "label": {}, "allocate": {}, "net": {} },
  "boundary": { "impl": "pit_dynamic_topk", "params": {}, "time_axis": {} },
  "panel":    { "impl": "point_in_time_align", "params": {} },
  "target":   { "impl": "forward_return_cs" },
  "search":   { "algorithms": [], "prior_pool": [], "trial_family": "", "seed": 0 },
  "evidence": { "impl": "two_stage_funnel", "params": {} },
  "verdict":  { "impl": "sentinel", "params": {} },
  "portfolio":{ "combiner": "fixed_ridge", "window": {} },
  "certify":  { "impl": "walk_forward", "params": {} },
  "operations": { "cache": true, "workers": 1, "log_level": "info" }
}
```

`plan` / `baseline` / `supply` / `caliber` / `boundary` / `panel` / `target` / `search` / `evidence` / `verdict` / `portfolio` / `certify` 进指纹；`operations` 不进任何指纹（`cache` / `workers` / `log_level` 改了不换实验）。

## 3.2 supply（数据供给）

| 键 | 必填 | 取值 | 进哪道门禁 |
|---|---|---|---|
| `exchange` | 是 | ccxt id | 门禁 B：与事实清单的 exchange 比对 |
| `market` | 否（默认 `futures`） | `spot` / `futures` | 门禁 B 第三道 |
| `datasets` | 是 | 上述四者的子集 | 逐个必须已采集 |
| `symbols` | 是 | 标的集名 | `digest(计划标的集) == pin.symbols_digest` |
| `frequency` | 是 | `1m` `5m` `15m` `1h` `4h` `1d` | 决定读哪个 `<STEM>-<tf>.parquet` |

门禁 B 的三条判据：标的集 digest 一致、数据新鲜度（`acquired_at >= coverage.end`）、供给维度（frequency / exchange / market 与事实清单一致）。

## 3.3 caliber（唯一口径源，四个子块全构成性）

| 子块 | 键 | 取值 | 说明 |
|---|---|---|---|
| `execution` | `entry` | `next_open` / `next_close` | `current_close` 被拒：权重由收盘算出却按同一根 bar 成交＝前视 |
| | `rebalance` | ≥1（bar） | 换仓间隔；`rebalance=1` 逐 bar 换 |
| | `annualisation` | `calendar_365` | 加密 24/7，只允许 365；252 直接拒 |
| | `funding` | `per_contract_events` | 只按事件求和（结算周期是数据属性，不进口径） |
| `label` | `horizon` | ≥1（bar） | 预测跨度 |
| | `transform` | `cs_rank` / `cs_zscore` / `cs_rank_zscore` | 截面变换（只作用于 score，不作用于收益） |
| | `neutralise` | `beta` / `log_dollar_volume` 的子集 | 空列表合法 |
| | `neutralise_window` | ≥2 | `neutralise` 非空时必填 |
| | `overlap_policy` | `independent_sample` / `nw_adjusted` | 重叠样本处理；`rebalance < horizon` 时必填 |
| | `min_width` | ≥1 | 截面宽度下限（20 标的计划别写 40） |
| | `width_policy` | `keep` / `drop` | 宽度不足时保留还是丢弃 |
| `allocate` | `quantiles` / `long_short` | — | 分位与多空构造 |
| | `weighting` | `equal` / `liquidity` / `inv_vol` | 权重方案 |
| | `max_weight` / `net_exposure` / `turnover_max` / `min_members` | — | 约束 |
| `net` | `cost_tiers` | bp 列表，如 `[20,60,150]` | 成本档（单边） |
| | `slippage` | `none` / `fixed` / `tiered` | `tiered` 取该档的一半 |
| | `fee_model` | `taker_with_funding` / … | 含 funding 时才读 funding 数据集 |
| | `max_leverage` / `initial_margin` / `maintenance_margin` / `liquidation_buffer` | — | 保证金与强平缓冲 |

口径字段**不允许出现在命令行**：`app/contract.py` 在启动期扫描全部开关，命中即 `E1020`（否则 Web 请求可绕过编译期一致性检查）。

估算这套口径的成本：

```bash
python -m app.cli estimate --plan my_plan.json --turnover 1.0
```

实测（`rebalance=5` → 73 期/年，`turnover=1.0`、`slippage=tiered`）：

```
20bp  → 每期 0.0030 → 年化 0.2190
60bp  → 每期 0.0090 → 年化 0.6570
150bp → 每期 0.0225 → 年化 1.6425
```

在这个换手下，150bp 档要求毛收益年化 164% 才不亏。资金费不在估算内（按事件逐 bar 计）。

## 3.4 八个工位块

| 块 | `impl` | 关键 `params` |
|---|---|---|
| `boundary` | `pit_dynamic_topk` | `k`（池大小）、`tier_window`（分层滚动窗口）、`min_coverage`；`time_axis: {mine, oos, holdout}` |
| `panel` | `point_in_time_align` | `drop_unclosed`（未收盘 bar 丢不丢） |
| `target` | `forward_return_cs` | 无 params（口径全部来自 `caliber.label` / `caliber.execution`） |
| `search` | 由 `algorithms` 决定 | `algorithms: [{name, params}]`、`prior_pool`、`trial_family`、`seed` |
| `evidence` | `two_stage_funnel` | `null_model`（`within_bar_cross_asset`）、`prefilter.min_coverage` |
| `verdict` | `sentinel` | `dsr_min`（**概率阈值 0.95**，不是 p 值）、`fdr_max`、`cost_adj_min` |
| `portfolio` | 由 `combiner` 决定 | `combiner`、`window: {fit, embargo, eval}`、`orthogonalise`（`none`/`symmetric`）、`corr_max` |
| `certify` | `walk_forward` | `folds`、`embargo`；OOS 窗口只从 `boundary.time_axis.oos` 取 |

时间轴写法：`"YYYY-MM-DD..YYYY-MM-DD"`；`holdout` 可写 `"reserved"`（未划出）。

四个容易踩的点：

1. **`time_axis.oos` 必须存在**。认证窗口是「未参与搜索的那段」，没有 OOS 就产不出可发布结论——`--live` 时 `E1008`。
2. **`portfolio.window` 的 `eval` 不能与 `fit` 相邻**：中间要留 `embargo` 根 bar，否则 `E1021`（隔离区里不能有样本）。`fit` 终点 = `eval` 起点 − embargo。
3. **`boundary` 与 `portfolio` 的区间必须落在 `mine` 内**：`mine` 是搜索区，`oos` 是认证区，`holdout` 是保留区，三者不重叠。
4. **`rebalance < horizon` 时 `overlap_policy` 必填**（标签会重叠）；`horizon` 与 `rebalance` 都进口径指纹，改任一个都是新实验。

搜索算法（`search.algorithms[].name`）：

| 算法 | 参数 | 说明 |
|---|---|---|
| `gp_symbolic` | `population`、`generations`、`max_depth` | deap 遗传规划，需要 fitness（与其它需要 fitness 的算法不能并发：`E2009`） |
| `random_expression` | `max_candidates` | 无 fitness 的随机基线，给 GP 一个对照 |

多算法共享**同一个** fitness 计数与 seen 集合——`n_trials` 因此是全家族口径，不是单算法口径。`trial_family` 进裁决的 `family` 字段，跨 run 的同族计数靠它串起来。

## 3.5 operations

```json
"operations": { "cache": true, "workers": 1, "log_level": "info" }
```

`workers` 目前必须为 1（状态在进程内存）；`cache` 关掉只会变慢，不改变结论；`log_level` 只影响日志。三者都不进指纹。

---

# 四、命令速查

## 4.1 命令面

| 命令 | mutation | 用途 | 关键参数 |
|---|---|---|---|
| `fetch` | 写供给 | 四数据集采集 | `--datasets` `--symbol-set`/`--symbols` `--exchange` `--market` `--timeframe` `--start` `--end` `--from-v4`/`--from-binance-vision` `--proxy` `--revise` `--limit` `--spot-exchange` |
| `vision` | 写供给 | 全市场日 K 发现→筛选→拉齐 | 子命令 `list`/`scan`/`fetch`；`--min-days` `--write-symbol-set` `--datasets` `--proxy` |
| `doctor` | 只读 | 供给体检 | `--plan` `--dataset` |
| `estimate` | 只读 | 成本估算 | `--plan` `--turnover` |
| `explain` | 只读 | 编译来源与指纹 | `--plan` `--set` `--no-strict` `--allow-constitutive-override` |
| `run` | 写事实 | 跑八工位 | `--plan` `--live` `--set` `--no-strict` `--allow-constitutive-override` |
| `pipeline` | 写事实 | 体检 → 跑 → 告警 | 同 `run` + `--dataset` `--force` |
| `stations` | 只读 | 工位清单 | — |
| `station <name>` | 写事实 | 单步执行 | `--on` `--probe` `--commit` |
| `inspect` | 只读 | 查 ticket 产物 | `--run-id` `--station` `--full` |
| `evaluate` | 只读 | 重算暴露 + IC/ICIR | `--on` `--candidate` |
| `backtest` | 只读 | 重算组合净表现 | `--on` |
| `certify` | 写事实+账本 | 重算 walk-forward | `--on` `--yes` |
| `promote` | 写事实+账本 | 候选入库 | `--run-id` `--candidate` `--min-grade` `--yes` |
| `factors` | 只读 | 查因子库 | `--status` `--limit` |
| `factor_db` | 写事实 | 下架 / 统计 | `--retire` `--note` `--yes` |
| `holdout` | 写账本 | 揭示保留区 | `--plan` `--set` `--yes` |
| `reconcile` | 只读 | 三方一致性核对 | — |

所有命令都支持 `--format {table,json,ndjson}`（默认 `json`）。`experiment` 是 `run` 的旧名别名。

## 4.2 退出码

| 码 | 含义 | 典型错误 | 该找谁 |
|---|---|---|---|
| 0 | 成功（含「零候选」这种结论性跳过） | — | — |
| 1 | 执行失败 | `E3xxx`（这一步没成功）、`E5xxx`（落盘/权限） | 代码 / 环境 |
| 2 | 用法与配置 | `E1004` 类型枚举、`E1008` 缺必填、`E1005` 构成性覆盖、`E1020` 口径开关 | 改配置 |
| 3 | 契约违反 | `E2005` 对账不一致、`E3002` 供给已失效 | 按 bug 处理 |
| 4 | 供给失败 | `E4002` 零成功/缺数据集、`E4004` 供给不可读 | 先同步数据 |

## 4.3 一次完整实验（复制粘贴版）

```bash
set -e                              # 任一命令非零退出即停：失败信封里没有 run_id
# 数据根取自仓库根的 .env；临时覆盖用 FLOW_DATA_ROOT=/abs/path python -m app.cli ...
PY="python -m app.cli"

# 1. 同步数据（首次给长区间，之后每天只传增量）
$PY fetch --datasets ohlcv,funding,open_interest,basis \
  --symbol-set crypto20 --exchange binanceusdm --market futures \
  --start 2024-01-01 --end "$(date -u +%F)"

# 2. 体检 + 成本认知
$PY doctor --plan my_plan.json --format table
$PY estimate --plan my_plan.json --turnover 1.0 --format table

# 3. 编译自检 → 跑
$PY explain --plan my_plan.json
RUN=$($PY run --plan my_plan.json --live \
      | python -c "import json,sys;print(json.load(sys.stdin)['run_id'])")

# 4. 看产物 → 重算
$PY inspect --run-id "$RUN"
$PY backtest --on "$RUN"
$PY certify  --on "$RUN" --yes

# 5. 入库 → 回灌（下一份计划的 prior_pool 写 db:*）
$PY promote --run-id "$RUN" --min-grade out_of_sample --yes
$PY factors --status active --limit 20

# 6. 对账
$PY reconcile
```

---

## 仓库结构

八个包直接位于仓库根，按依赖方向自下而上排列（上→下 = 下层→上层）：

```
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
  conftest.py       supply 夹具（在 tmp_path 造真实 parquet 供给）
  test_v5_*.py      37 个测试文件（平铺，无子目录）
  fixtures/         计划样例 + 黄金面板夹具（20 标的 × 1277 根 1d）
  reference/v4/     冻结的 V4 统计实现，只作逐位对拍的数值 oracle
docs/
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
python -m pytest tests -q
```

全部为可复现的单元/集成测试，含：

- **黄金锚点对拍**：`test_v5_ac63_golden.py` 用手写 pandas 参考实现对同一面板重算，与流水线结果比对（容差 1e-9；跨版本相对冻结值 ±10%）。面板数据（20 标的 × 1277 根 1d）与冻结期望值一起提交在 `tests/fixtures/golden/`，**fresh clone 直接可跑**，不需要先联网采集。
- **统计原语逐位对拍**：`test_v5_verdict.py` 与 `tests/reference/v4/` 里冻结的 V4 实现零容差比对——BH/Holm/DSR/置换 p/Newey-West 必须逐位一致。
- **历史事故回归**：多空收益误用变换后 score、收益多延迟一根、`rebalance` 未真冻结、桥漏传 `horizon_bars`——四类都钉了回归测试。
- **包独立性守卫**：AST 静态扫描八个顶层包，不得 import 旧仓库的 `core` / `api` / `flow` / `web`。

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

从 [`docs/INDEX.md`](docs/INDEX.md) 开始——按「你想知道什么」导航。

- 想知道怎么设计的 → 本文「架构」一节 → `01`–`06`
- 想知道一行结论怎么算出来的 → `06-evaluation.md`
- 想知道每个模块有没有问题 → `reviews/module-review/00_progress.md`
- 想知道配置单每个键什么意思 → `18-plan-schema.md`
- 想知道数据怎么落盘 → `19-supply.md`
- 想知道凭什么信它 → `14-test-strategy.md`、`ACCEPTANCE.md`

---

## 许可

见 [LICENSE](LICENSE)。
