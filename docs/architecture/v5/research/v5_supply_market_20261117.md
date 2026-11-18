# V5 供给：双市场支持 + upstream 精简（2026-11-17）

> 回答两个问题：① 数据要支持现货与合约；② `v5/upstream/` 那一堆文件都是做什么的、能不能删。
> 结论：**已支持双市场**；**upstream 从 10 文件 2742 行降到 3 文件 1032 行**，V5 全量 **601 passed**。

---

## 一、现货与合约

### 1.1 契约

`market` 是**独立于 `exchange` 的维度**，不是 exchange 的一部分：

```
<root>/ohlcv/binance/spot/BTC_USDT-1d.parquet          # 现货
<root>/ohlcv/binanceusdm/futures/BTC_USDT_USDT-1d.parquet  # 合约
```

`binance` 同一时间既有现货也有合约，而 `binanceusdm` 这种 id 只在合约侧存在。
把两个概念分开，换交易所时路径规则不用动——V4 的 `data/binance/{spot,futures}` 就是这个形状。

| 项 | 取值 | 说明 |
|---|---|---|
| `plan.supply.market` | `spot` / `futures` | 可选，缺省 `futures`；显式写出时进 `blueprint_digest` |
| `flow fetch --market` | 同上 | 默认 `futures` |
| 市场决定数据集 | `futures`：四类全用；`spot`：仅 `ohlcv` | 见下 |

现货没有资金费、没有持仓量；`basis` 是「合约相对现货的溢价」，定义域在合约侧。
所以 `MARKET_DATASETS` 这一张表是唯一真相：

```python
MARKET_DATASETS = {"spot": ("ohlcv",), "futures": ("ohlcv","funding","open_interest","basis")}
```

### 1.2 三层护栏（都是 fail-closed）

| 层 | 检查 | 失败码 | 为什么必须在这一层 |
|---|---|---|---|
| 计划 | `_check_cross_field`：spot + 非 ohlcv | `E1004` | 编译期就拒，不浪费一次采集 |
| 采集 | `store.require_market_dataset` | `E1004` | **连网之前**拒；否则逐标的失败被记成 `skipped`，命令仍返回 0 |
| 编译 | 门禁 B：`meta.market == plan.supply.market` | `E4002` | 少了这层，用现货跑出的因子会被标成合约因子，而其它门禁全绿 |

符号形态只做**单向**校验：`spot` 下出现 `BTC/USDT:USDT`（`:` 后是结算币，即永续标志）
当场拒绝。反方向**不强制**——并非所有交易所的合约符号都带后缀（有的靠 `defaultType`
区分），硬要求会误伤合法命名。那类情况由「零成功即失败」兜底。

> 这一条我改过一版：最初写了「futures 必须含 `:`」，结果所有合成符号（`S0..S7`）的
> 测试全挂——那次失败恰好说明「用命名约定推断市场」是脆的。

### 1.3 实测

| 市场 | 标的集 | 数据 | run --live |
|---|---|---|---|
| futures | `crypto20`（20 个 `X/USDT:USDT`） | 4 数据集，28280 行 OHLCV | 八工位 `state=done`，exit 0 |
| spot | `crypto20_spot`（20 个 `X/USDT`） | 52301 + 4439 行 OHLCV | 八工位 `state=done`，exit 0 |

三条拒绝路径实测（都在写盘/连网之前）：

```
fetch --datasets ohlcv,funding --market spot     → E1004「spot 市场不支持数据集 funding」
fetch --symbol-set crypto20_spot --market futures → E1004「现货市场收到衍生品符号」
explain（plan 里 spot + basis）                    → E1004「spot 市场不支持数据集 basis」
```

---

## 二、upstream 那些文件是做什么的

删之前 10 个文件 2742 行，其中**只有 923 行在做事**，另外 1819 行是「多写者并发 + 在线服务」
的事务协议——单机批量研究不需要。

### 2.1 已删（8 个文件 2097 行）

| 文件 | 行数 | 原本负责 | 为什么能删 |
|---|---|---|---|
| `gate_a.py` | 443 | 写盘后的七项完整性检查：覆盖区间、标的集、校验和、末端闭合、字段完整、时间单调、上游修订 diff | 七项拆成「读取时六项」（`store.read_dataset` 现算 gaps/end_closed/e checksum）+ 路径与市场校验。写时的完整性不等于本次研究的适用性，后者只有读到 plan 才知道 |
| `refresh.py`（CLI） | 407 | 一条命令走完「采集→定稿→门禁→登记→移别名」五阶段 | `flow fetch` 一条命令直接写 parquet。没有别名要移，就没有「别名先移还是先校验」的窗口 |
| `acquire.py` | 356 | 分页采集、逐标的分页游标、坏行跳过计数、请求签名、暂存目录约定 | 分页逻辑 60 行迁进 `fetch._paginate`；其余是暂存协议 |
| `publish.py` | 348 | `staging → supply/seeds/<dataset>/<version>/` 原子发布、幂等判定（seed 完整**且**别名已指向）、别名原子移动 | 正确性只需要一条：`_atomic_write`（同盘 tmp + `os.replace`），读侧永不读到半个文件 |
| `lease.py` | 233 | 300s TTL 租约、心跳续约、抢占记 `previous_owner`、跨进程互斥 | 单机没有第二个写者；append-only 让「重跑」天然幂等 |
| `mirror.py` | 122 | 离线镜像取数（`LocalMirrorFetcher`，与 `ccxt.fetch_ohlcv` 同形） | 它的用途是断网重跑；`--from-v4` 导入覆盖了同一用例，且是无状态的 |
| `loader.py` | 101 | `DataLoader`：读 `seed/<dataset>/<version>/payload.parquet`、重算 blob 指纹、空表 fail-closed | `store.read_dataset` 就是这件事，只是路径改成约定、身份改成现算 |
| `maintlog.py` | 87 | 按版本落 `_upkeep/log/<dataset>/<version>.jsonl` 维护日志 | 没有版本目录，日志无处可挂；采集结果直接在 `FetchReport` 里返回 |

**连带的死代码也一起删了**（否则删了文件还留着接口，等于没删）：

- `Layout.published_aliases / seed_dir / staging_dir / staging_root / lease_file /
  maintenance_log / upkeep / supply_dataset / safe_segment`
- `common.load_supply_context()`、`ReadOnlyStore.resolve_pin()`
- `flow refresh` 命令（命令面 7 → 6）

### 2.2 现在只剩 3 个文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `store.py` | 657 | 路径规则（`dataset_dir` / `dataset_file` / `scan_symbols`）、读写（`read_file` / `upsert` / `_atomic_write`）、**现算身份**（`payload_checksum` / `_interval_ns` / `_gaps` / `Dataset.pin()` / `Dataset.check_window`）、市场与路径段校验 |
| `fetch.py` | 368 | 四数据集采集（ccxt 分页）+ V4 快照导入（列名 `date`→`timestamp`、naive 按 UTC 解释、无 tf 的事件流不带 tf） |
| `__init__.py` | 7 | 文档 |

一句话分工：**`store` 管「文件长什么样、它是什么」，`fetch` 管「怎么把外部世界变成这样的文件」。**

### 2.3 删掉之后靠什么保证正确

旧协议用「暂存 + 门禁 A + 原子发布 + 别名」这一串动作换取两条性质。现在换成两条纪律：

| 性质 | 旧做法 | 新做法 |
|---|---|---|
| 数据不会变 | append-only（其实旧协议也靠这个） | append-only：同一 `(symbol,timestamp)` 旧值永不覆盖，冲突只计数 |
| 用的是哪份数据 | 版本目录名 + manifest + 别名 | 读取时现算 `checksum`（行序无关流式 sha256），`version = checksum[:16]`，仍进 `blueprint_digest` |

代价写在 `19-supply.md §5`：无法回滚到旧内容、坏数据改不掉（需 `--revise`）、
`acquired_at` 是 mtime、无审计日志、无断点续传。这五条都是单机研究可以接受的。

---

## 三、本轮撞出并修掉的 3 个真实缺陷

这三个都不是读代码看出来的，是**换到现货数据后跑出来的**。

### 3.1 逐标的覆盖检查是死代码

`Dataset.check_window()` 写好了，但没有任何调用点。后果：整体 `coverage` 取 `min/max`，
看不出「某只标的晚上市」，而 ARB 2023-03 才上市、研究窗口从 2023-01 开始 → 面板前三个月
是 NaN → 一路传到 `allocate` 才炸：

```
E3003 allocate 的 price 必须是有限正数   ← 报出来的原因与真实原因相差十万八千里
```

修：`common._panel_window()` 取 `mine.start → oos.end`，`build_supply_context` 对面板基座
调 `check_window`。现在的报错是：

```
E4002 ohlcv 覆盖不足: ARB/USDT 起点缺 2023-01-01..2023-03-23
```

### 3.2 符号路径注入

`file_stem()` 只替换 `/` 与 `:`，`\` 与 `..` 原样保留；`dataset_file()` 又只校验 `timeframe`
不校验 stem。Windows 上 `BTC\..\..\evil` 就是一次逃逸。旧的黑名单校验还漏 `.._.._evil`
这种「不逃逸但也不是合法名字」的形态。

修：`_safe_segment` 改**白名单** `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`（首字符必须是字母数字，
任何以 `.` 开头的形态自然被拒），并在 `dataset_file` 对 stem 调用。

### 3.3 诊断顺序倒挂

`build_supply_context` 跑在结构校验之前，于是「把 `horizon` 拼成 `horizn`」收到的错误是
「数据文件不存在」——会把人骗去先采一遍数据。

修：`run` / `explain` 先做一次 `require_supply=False` 的纯编译（微秒级），让结构/跨字段/
实现名错误先说话，再读供给。顺带给 S0 验收补了正面证据：**坏计划 + 无数据 → 报结构错误码
而不是 E4002**。

另修两处：`cmd_fetch` 零成功即失败（原来会 `ok:true` + 空表）；`E4002` 漏导入导致
`NameError` 被 executor 包成 `E3004`。

---

## 四、复核命令

```bash
# 1. 合约四数据集（V4 快照导入）
FLOW_DATA_ROOT=<绝对路径> python -m v5.app.cli fetch \
  --datasets ohlcv,funding,open_interest,basis --symbol-set crypto20 \
  --exchange binanceusdm --market futures --from-v4 data/binance

# 2. 现货单数据集
FLOW_DATA_ROOT=<绝对路径> python -m v5.app.cli fetch \
  --datasets ohlcv --symbol-set crypto20_spot \
  --exchange binance --market spot --from-v4 data/binance

# 3. 两个市场各跑一次完整挖掘
FLOW_DATA_ROOT=<绝对路径> python -m v5.app.cli run --plan <plan.json> --live
FLOW_DATA_ROOT=<绝对路径> python -m v5.app.cli run --plan <plan_spot.json> --live

# 4. 全量
python -m pytest tests/v5 -q      # 601 passed
```

---

## 五、新证据：DSR 的 `sigma_sr` 口径问题（仍未裁决）

同一份因子模板、同一时间轴、同一 `verdict` 参数：

| 市场 | 候选数 | 裁决结果 |
|---|---|---|
| futures | 32 | **32 reject**（24 `dsr_fail` / 8 `dsr_fail+cost_fail`） |
| spot | 15 | **15 pass** |

差别只在 `sigma_sr`（试验间 SR 横截面离散度）：现货这批 SR 更集中 → σ 更小 → 门槛更低。
这个对比比之前「28 候选全过 vs 32 候选全灭」更直接地说明：**横截面口径让 DSR 只有 0/1
两态，且对「种群里是否混进劣质个体」极敏**——同一批因子换个市场就整体翻转，这不是判据
想要的性状。

按约定没有动它。两个选项仍在桌上：**A** 保持并永远并列门槛与最强候选；**B** 改用采样误差
`1/√(n−1)`，横截面降级为诊断字段（与 `ACCEPTANCE.md` 的「1.53@255」自洽，判据才有梯度）。
