# 19 供给存储：文件即事实（取代 s0/06 的事务协议）

> 决策：**存储层极简**。一份数据集 = 一组 parquet 文件 + 目录约定，没有版本目录树、
> 没有别名表、没有租约、没有 manifest。供给身份（checksum / coverage / 间隔）在**读取时现算**。
> 本文取代 `s0/06-upstream.md` 的 §3–§7；s0/06 保留作为设计推导的历史记录。

---

## 1. 为什么改

旧方案（S2）把「多写者并发 + 在线服务」的问题提前解决了：暂存 → 定稿 → 门禁 → 登记 →
移动别名，配租约 TTL、心跳续约、抢占留痕、`--resume` 可复用性四条件。这套协议 1697 行，
但当前场景是**单机、批量、离线研究**：

| 旧协议解决的问题 | 当前是否需要 |
|---|---|
| 两个写者同时移动别名 | 不需要。单机用一把 `filelock` 足够 |
| 采集中断后从断点续 | 不需要。append-only 天然可续 |
| 回滚到上一个版本 | 不需要。历史值本来就不该被改 |
| 版本目录树供审计 | 不需要。checksum 现算即可判定「用的是哪份」 |

复杂度放错了位置：它长在**写入侧**（每次采集跑一遍事务），而真正需要严格的是
**读取侧**（这次研究的输入到底覆盖了什么、有没有缺口）。所以把复杂度搬过去。

## 2. 契约

### 2.1 路径

```
<root>/ohlcv/<exchange>/<market>/<SYMBOL>-<tf>.parquet      # bar 序列：tf 是身份的一部分
<root>/<dataset>/<exchange>/<market>/<SYMBOL>.parquet       # 事件流/快照：不带 tf
```

- `ohlcv` 保留 `tf`：`1d` 与 `4h` 是**两份不同的数据**，不是同一份的两种读法。
- `funding` / `open_interest` / `basis` **不带 tf**：结算间隔实测 1h/4h/8h 混合且
  同一合约会变（I34），把它写进文件名就是写一个假标签（V4 的 `*-1d-funding_rate.feather`
  实际存的是 4 小时事件流）。
- `SYMBOL` 用 `BTC/USDT:USDT → BTC_USDT_USDT`，与 V4 `naming.safe_symbol` 同一规则。

### 2.1.1 市场维度（spot / futures）

`market` 是**独立于 `exchange` 的维度**：`binance` 同一时间既有现货也有合约，而
`binanceusdm` 这种 id 只在合约侧存在。把两个概念分开，换交易所时路径规则不用动
（V4 的 `data/binance/{spot,futures}` 就是这个形状）。

计划的 `supply.market` 取 `spot` / `futures`，省略即 `futures`；它是路径的一部分，
所以**同一交易所的两个市场永远是两份数据**，不可能互相覆盖或串读。

市场决定可用的数据集：

| 市场 | 可采数据集 | 理由 |
|---|---|---|
| `futures` | `ohlcv` / `funding` / `open_interest` / `basis` | 四类都定义在合约上 |
| `spot` | 仅 `ohlcv` | 现货没有资金费、没有持仓量；`basis` 是「合约相对现货的溢价」，定义域在合约侧 |

三条护栏（都是 fail-closed，都不依赖「采集器最后会不会碰巧报错」）：

1. **计划层**：`spot` + 非 `ohlcv` → `E1004`（`plan_schema._check_cross_field`）；
2. **采集层**：`store.require_market_dataset` 在**连网之前**拒绝（否则失败会被记成
   `skipped` 而命令仍返回 0）；
3. **编译层**：门禁 B 核对 `supply_meta.market == plan.supply.market`（`E4002`）——
   少了这一条，用现货数据跑出的因子会被标成合约因子，而其它门禁全绿。

符号形态只做**单向**校验：`spot` 下出现 `BTC/USDT:USDT`（`:` 后是结算币，即永续标志）
当场拒绝；反方向不强制，因为并非所有交易所的合约符号都带后缀。那类情况由
`flow fetch` 的「零成功即失败」兜底——比猜命名约定可靠。


### 2.2 列

| 数据集 | 列 |
|---|---|
| `ohlcv` | `timestamp, open, high, low, close, volume` |
| `funding` | `timestamp, funding_rate` |
| `open_interest` | `timestamp, open_interest` |
| `basis` | `timestamp, basis` |

`timestamp` 为 UTC、时区感知的 `datetime64[ns, UTC]`。不存本地时间、不存 naive 时间。

### 2.3 append-only（这是「数据不会变」的唯一保证）

同一 `(symbol, timestamp)` 的旧值**永不覆盖**。新采集与之冲突时：

- 保留旧值；
- 冲突行只计数（`revised`），不写盘；
- 要改历史必须显式 `--revise`，且整份文件重写、记一条 `revision` 记录。

V4 的 `drop_duplicates(keep="last")` + 原地 `to_feather` 是静默改写历史——交易所修订一根
K 线，过去跑出的结论就无法解释。这一条是本方案与 V4 采集的**唯一实质分歧**。

### 2.4 身份现算（不落盘）

读一次数据集，当场算出：

```python
checksum    # 行序无关的流式 sha256（对排序后的逐行 canonical_text）
rows        # 总行数
coverage    # (min(ts), max(ts))，逐标的分别记账
interval_ns # 相邻时间戳差的中位数（从数据推，不信文件名）
gaps        # 逐标的的 start / end / interior / unclosed
symbols     # 出现过的标的清单
```

`version = checksum[:16]`——仍是内容寻址、仍是 16 位小写十六进制，仍满足 `Pin.version`
契约，只是不再有对应的目录。`Pin` 保留为**内存值对象**，由 `store.read_dataset()` 构造：

| Pin 字段 | 来源 |
|---|---|
| `dataset` / `exchange` / `market` | 路径 |
| `version` | `checksum[:16]` |
| `checksum` | 现算 |
| `coverage` | 现算 |
| `symbols_digest` | `compute_digest(sorted(symbols))` |
| `end_closed` | 现算：每个标的 `max(ts)+interval <= now` |
| `schema_version` | `"1"` |
| `acquired_at` | 文件 mtime（**不是**采集完成时间，见 §5） |
| `gate_a` | 现算：`{passed, reason_code}` |

`Pin.digest_view()` 仍进 `blueprint_digest`，所以「这份结论用的哪份数据」可判定。

## 3. 四数据集的时间语义

| 数据集 | 语义 | 间隔检查 | 覆盖要求 |
|---|---|---|---|
| `ohlcv` | bar | 必须等距（容差 1.5×） | 覆盖 `time_axis.mine` 与 `oos` |
| `basis` | 派生日频 | 必须等距 | 覆盖 `time_axis` |
| `funding` | 事件流 | **不检查等距**（容差 3×），只检查单调、无重复 | 覆盖窗口即通过 |
| `open_interest` | 快照 | **不检查等距** | 交易所只保留约 30 天；覆盖不足时**不阻断**研究，只标记 `dormant` |

`basis` 由 perp 与 spot 收盘价派生，必须先有 `ohlcv`。它是唯一有上游依赖的数据集。

OI 的 30 天限制是交易所事实，不是实现缺陷。用它做成本建模时窗口必须相应缩短，或按
`dormant` 处理（因子模板不激活）——**不得**用「自动缩窗」掩盖。

## 4. 读取时的六项检查（取代门禁 A）

门禁 A 原来在**写时**跑七项，现在改在**读时**跑六项。理由：写时的完整性不等于
本次研究的适用性，而后者只有读到 plan 才知道。

| 检查 | 判据 | 失败码 |
|---|---|---|
| 覆盖 | `coverage ⊇ 本次读取 window`（逐标的；bar 基座还要求覆盖 `mine` 起点 → `oos` 终点） | `E4002` |
| 末端闭合 | 每个标的 `max(ts)+interval <= now` | `E4003` |
| 字段 | 请求字段都在文件里，且无整列为空 | `E1014` |
| 时间单调 | 无重复、无逆序 | `E4007` |
| 间隔 | 仅 bar 数据集：相邻间隔 ≤ 1.5× 中位数；事件流 3× | `E4002` |
| 市场 | `market` 与数据集相容（`MARKET_DATASETS`） | `E1004` |

覆盖检查里的「逐标的」不是修饰语：整体 coverage 取 `min/max`，晚上市的标的会在
前半段留 NaN，而那条 NaN 会一路走到 `allocate` 的 price 才炸——报出来的是「价格必须
是有限正数」，与真实原因（这只标的 2023-03 才上市）相差十万八千里。现货 20 标的里
只要有 ARB 一只，整个研究窗口前三个月就是空的。

缺口**显式列出**，不静默缩短研究窗口。研究窗口由计划声明，不由数据可用性倒推。

门禁 B（编译期、只读、回答「这份事实能回答本次问题吗」）保留，检查项不变，只是
`supply_meta` 改由 `store` 从路径与数据推，而不是读 manifest；新增 `market` 一项。

## 5. 明确接受的代价

| 代价 | 说明 | 缓解 |
|---|---|---|
| 无法回滚到旧内容 | append-only 只保证「不变」，不保证「可回到上周」 | 需要时自行备份目录 |
| 坏数据改不掉 | 首次写入的值不会被后续覆盖 | 显式 `--revise`（整份重写 + 记录） |
| `acquired_at` 是 mtime | 不是采集完成时间，跨机器拷贝会变 | 仅用于门禁 B 的新鲜度下界，不参与指纹 |
| 无审计日志 | 不知道谁在何时改过哪一行 | `--revise` 时打印差异摘要 |
| 无断点续传 | 中断后重跑从头拉；append-only 让重复数据只计数不重写 | 分页上限 10000 页，单次采集区间别开太大 |

这五条都是「单机研究」可以接受的，也是本决策换取 60% 代码量的代价。

**另有一条比旧协议更严**：`flow fetch` 在**全部标的都失败**时返回 `E4002`、退出码 4，
而不是「逐个 skipped 完报成功」。旧协议靠「门禁 A 不过就不发布」间接达到同一目的；
现在没有门禁 A，就必须在命令出口直接拦——否则「用错标的集」「用错市场」「区间内
没数据」三种情况长得一模一样，都是 `ok: true` 加一张空表。

## 6. 删除清单（已执行）

`upstream/` 从 **10 个文件 2742 行**降到 **3 个文件 1032 行**（`__init__` / `store` / `fetch`）。

| 删除 | 行数 | 为什么安全 |
|---|---|---|
| `upstream/acquire.py` | 356 | 分页迁入 `fetch._paginate`；规范化/缺口迁入 `store` |
| `upstream/gate_a.py` | 443 | 七项检查拆成「读取时五项」+「路径/间隔」 |
| `upstream/publish.py` | 348 | 无别名、无登记，只需原子替换文件（`store._atomic_write`） |
| `upstream/lease.py` | 233 | 单机批量研究没有多写者；append-only 让「重跑」天然幂等 |
| `upstream/loader.py` | 101 | 它只是「读 payload 并重算指纹」的门面，`store.read_dataset` 就是这件事 |
| `upstream/mirror.py` | 122 | 离线镜像的唯一用途是绕开断网重跑；V4 快照导入（`--from-v4`）覆盖了这个用例 |
| `upstream/maintlog.py` | 87 | 无版本目录，日志无处可挂 |
| `app/cli/refresh.py` | 407 | 由 `fetch.py` 取代（`flow refresh` 命令一并删除，命令面 7 → 6） |

同时删掉的三处「旧协议的影子」：

- `Layout.published_aliases / seed_dir / staging_dir / staging_root / lease_file /
  maintenance_log / upkeep / supply_dataset` —— 没有别名、没有版本目录，这些路径不再存在；
- `common.load_supply_context()` 与 `ReadOnlyStore.resolve_pin()` —— 别名表的读口；
- `Layout.safe_segment()` —— 路径段校验收进 `store._safe_segment`，避免两套规则。

**测试面**：删掉 5 个旧协议测试文件（`test_v5_upstream_{acquire,gate_a,lease,publish}`、
`test_v5_supply_e2e`，共 78 项），新增 `test_v5_supply_store.py`（21 项）直测新契约：
路径规则、append-only、身份现算、缺口容差、逐标的覆盖、市场维度、零成功即失败。

## 7. 迁移步骤（已完成）

1. ✅ 新增 `store.py`（路径 / 读取 / 身份 / append-only 写）与 `fetch.py`（四数据集 + V4 导入）。
2. ✅ 新增 `flow fetch`；`run.py` 的供给装配切到 `store`；`live.py` 支持多数据集。
3. ✅ 用 `data/binance` 导入四数据集，跑 `run --live`，八工位全通。
4. ✅ 删除 8 个旧模块与 5 个旧测试文件，重写为 store/fetch 语义。
5. ✅ 更新 `s0/06`（顶部标 superseded）、`19-supply.md`（本文）、`06-evaluation.md`。

**顺序不能反**：先简化存储、再加数据集。反过来的话，四套采集器要按事务协议写一遍，
简化时全废——这也是当时把「先简化」单列为第一步的原因。

## 8. 与 V4 的关系

本方案在**存储形态**上回到 V4（一份数据集一份文件、直接落盘），但在两条纪律上比 V4 严格：

1. **append-only**：V4 的 `keep="last"` 会静默改写历史，本方案不会。
2. **身份现算并进指纹**：V4 没有 checksum 进指纹的路径，同一份文件被改写后无法察觉。

换句话说：V4 的简单是对的，V4 的「静默可变」是错的。本方案取前者、补后者。
