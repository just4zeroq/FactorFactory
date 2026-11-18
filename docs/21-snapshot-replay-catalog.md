# 21 数据快照、回测复盘与因子管理设计

> 状态：P1–P5 已实施（快照基础设施 + 计划接入 + 因子服务 + 复盘闭环 + 自动化）。
> 关联：`09-app-storage.md`（物理分区）、`18-plan-schema.md`（计划契约）、`19-supply.md`（采集事实）、`20-mining-algorithms.md`（挖掘与晋升流程）
>
> 实施落点：`storage/sqlite_store.py`（机制）、`services/{snapshot,factor,replay}_service.py`（业务规则）、
> `app/cli/{snapshot,replay_ops}.py`（`flow snapshot` / `flow replay`）。
> 因子 `factor_id` 保持内容身份，证书身份 `cert_key=snapshot:factor`；sqlite 只做
> 清单/证书/复盘的真相源，因子目录由 `FactorService.materialize()` 物化回
> `catalog/factors.jsonl`，`storage.catalog` 读取接口不变。

## 0. 对提案的三条修正

提案整体成立：快照要记录交易对/数据集/时间范围，挖掘可指定快照，缺省用最新，有效因子随新快照重跑，用 sqlite 做快照管理并分层封装。下面三条需要收紧。

1. **因子身份不能直接改成「快照 + 因子」**
   - `factor_id` 必须继续保持表达式内容身份（`H(规范化 payload + 字段集)`）。这是现有 `storage/catalog.py` 的根基，改掉会让去重、`db:<factor_id>` 先验引用、跨 run 复用全部失效。
   - 用户要表达的语义用**证书身份**承载：`cert_key = (snapshot_id, factor_id)`。同一个因子在不同快照下有各自的证书行，表达式只有一行。
   - 结果：查「因子 F 在 S2 上是否仍有效」是查证书表；查「因子 F 是什么」仍是查因子表，不随快照漂移。

2. **「没指定用最新」只在解析时成立，执行时必须冻结**
   - 计划写 `supply.snapshot = "latest"` 或具体 id。编译期把 `latest` 解析成实际 `snapshot_id`，写进 `frozen/context.json` 的 `resolved_snapshot_id`，并进指纹。
   - 否则事后无法回答「这次 run 到底用了哪版数据」，可复现性归零。

3. **sqlite 不能成为第二套真相**
   - 现有 `db/events.jsonl` 是 append-only 事件账本，`catalog/factors.jsonl` 是因子目录，`db/meta.duckdb` 是可重建索引。
   - 新增的 `db/snapshots.sqlite3` 只做三件事的真相源：快照清单、复盘记录、因子证书。不复制 ledger 语义，不直接改写 `factors.jsonl`；因子目录由证书服务物化导出，保持现有读取接口可用。

## 1. 数据快照管理

### 1.1 定义

一个快照是不可变的数据视图，回答四个问题：哪些交易对、哪些数据集、什么时间段、每份文件的字节身份。

```text
snapshot_id = snap-<UTC日期>-<序号>        例：snap-20261118-001
```

快照不复制 parquet，只存清单与 digest；底层仍是现有 `supply/<dataset>/<exchange>/<market>/` 文件。清单每一行锁定一个文件的一个时间区间，因此补采历史只会产生新快照，不会改写旧快照。

### 1.2 清单条目字段

| 字段 | 含义 |
|---|---|
| `snapshot_id` | 所属快照 |
| `dataset` | `ohlcv` / `funding` / `open_interest` / `basis` |
| `exchange` | `binanceusdm` 等 |
| `market` | `spot` / `futures` |
| `symbol` | ccxt 符号，如 `BTC/USDT:USDT` |
| `timeframe` | bar 为 `1d`；事件流沿用采集标签 |
| `start_ts` / `end_ts` | 该文件的实际最早/最晚时间戳（UTC） |
| `row_count` | 行数 |
| `relpath` | 相对数据根路径，必须过路径白名单 |
| `blob_digest` | 文件内容 digest |
| `columns_digest` | 值列清单 digest，检测列口径漂移 |
| `value_columns` | `open,high,low,close,volume` 等 |

快照头另有 `manifest_digest = H(所有条目 canonical json)`、`parent_snapshot_id`（增量来源）、`created_at`、`status(active|archived)`。

关键约束：**只有条目里的 `(dataset, exchange, market, symbol, timeframe)` 能定位文件，`start/end` 是从文件读出来的事实，不接受计划填写**。这样「用了哪个时间段」不是声明而是测量。

### 1.3 sqlite schema（底层基础设施）

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  manifest_digest TEXT NOT NULL UNIQUE,
  parent_snapshot_id TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','archived')),
  span_start TEXT NOT NULL,
  span_end TEXT NOT NULL,
  note TEXT,
  FOREIGN KEY(parent_snapshot_id) REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT NOT NULL,
  dataset TEXT NOT NULL,
  exchange TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  relpath TEXT NOT NULL,
  blob_digest TEXT NOT NULL,
  columns_digest TEXT NOT NULL,
  value_columns TEXT NOT NULL,
  UNIQUE(snapshot_id, dataset, exchange, market, symbol, timeframe),
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entries_lookup
  ON snapshot_entries(dataset, exchange, market, symbol, timeframe);

CREATE TABLE IF NOT EXISTS replay_runs (
  replay_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  caliber_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','passed','failed','error')),
  requested_at TEXT NOT NULL,
  finished_at TEXT,
  run_id TEXT,
  report_ref TEXT,
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS factor_certificates (
  cert_key TEXT PRIMARY KEY,            -- snapshot_id + ':' + factor_id
  snapshot_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  caliber_digest TEXT NOT NULL,
  evidence_grade TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('active','degraded','retired')),
  metrics_json TEXT NOT NULL,
  certified_at TEXT NOT NULL,
  replay_id TEXT,
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id),
  FOREIGN KEY(replay_id) REFERENCES replay_runs(replay_id)
);
```

因子定义表另建 `factors`，`factor_id` 为主键，字段沿用现有 `catalog.add()` 记录（`expression`、`required_fields`、`operators`、`origin`、`promoted_at`），保证表达式只有一行。

### 1.4 分层

```text
storage/sqlite_store.py     底层：连接、WAL、busy_timeout、事务、迁移、路径校验
services/snapshot_service.py  服务层：create / resolve / manifest / list / archive
services/replay_service.py    服务层：enqueue / run / diff / decide
services/factor_service.py    服务层：upsert / certify / list_active / revalidate
```

底层只提供机制，不含业务判断；服务层才拥有「latest 怎么解析」「哪些因子要复验」这类规则。`SqliteStore` 提供只读连接给执行期，写连接只发给服务层，和现有 `ReadOnlyStore / TicketWriter / DepotWriter` 的权限形状保持一致。

### 1.5 与计划/执行的集成

1. `plan.supply.snapshot` 新增，值形如 `"latest"` 或 `"snap-20261118-001"`。
2. 编译器解析 `latest` → 具体 id，写 `frozen/context.json.resolved_snapshot_id`，并进入 `blueprint_digest`。
3. Boundary/Panel 读取清单选定的文件集合，不再扫描整个 `supply/` 目录；清单外的文件对本次 run 不可见。
4. `backtest --on` / `certify --on` 重建时读同一 `snapshot_id`，从根源消除我们遇到过的「补采后旧 run E3002」。

### 1.6 CLI

```text
flow snapshot create --spec snapshot_spec.json [--yes]
flow snapshot list [--status active]
flow snapshot show --snapshot snap-20261118-001 [--format json]
```

`create` 读采集目录，逐文件算 digest 和时间范围，写 sqlite；生成清单 json 落 `catalog/snapshots/<id>.json`，两者 digest 必须一致。

## 2. 回测复盘

### 2.1 复盘对象

只有同时在以下两个集合里的因子才进入复盘队列。

```text
factor_service.list_active()                       因子库 active
× snapshot_service.active_since(last_certified)    上次认证后产生的新快照
```

历史 run、被拒候选、watch、已 retired 因子都不进队列。

### 2.2 复盘流程

```text
取 (factor_id, snapshot_id, caliber_digest)
  → 在该快照上 evaluate 重算暴露
  → backtest 重算净收益与成本
  → certify 重算 walk-forward OOS
  → 与上一张证书逐指标 diff
  → 写 factor_certificates（新快照下的一条证书）
  → 物化导出到 catalog/factors.jsonl
```

复盘不重跑 Boundary/Search/Verdict，因此成本远低于完整挖掘。

### 2.3 决策规则

| 结果 | 动作 |
|---|---|
| 全部通过且无显著退化 | 新证书 `verdict=active`，续期 |
| 通过但 IC/净收益下降超过阈值 | `degraded`，保留但降权，告警 |
| OOS 失败或成本闸失败 | `retired`，写原因，发出下线通知 |
| 执行报错 | `error`，不自动改证书，人工介入 |

「显著退化」阈值写进配置，不写死在代码里。差值与阈值都要落进复盘报告。

### 2.4 报告与 CLI

报告落 `runs/replays/<replay_id>/report.json`，含：快照 digest、caliber digest、逐段指标、与上次证书的 diff、决策、原因码。

```text
flow replay enqueue --snapshot snap-20261118-002 [--all-active]
flow replay run --replay-id <id>
flow replay show --replay-id <id> [--format json]
```

## 3. 因子管理

### 3.1 身份模型

```text
factor_id  = H(expression + required_fields)        逻辑身份，稳定
cert_key   = snapshot_id + ":" + factor_id          证书身份，随快照版本化
```

这样 `db:<factor_id>` 先验引用不因换快照而失效，而「因子 F 在 S2 上是否还有效」由证书明确回答。

### 3.2 生命周期

```text
promote → factor 定义入 factors（或首次证书隐式创建）
        → 在 promoted_snapshot 上开第一张证书
新快照产生 → replay 排队 → 新证书 active / degraded / retired
显式下架 → 写 retired 证书，因子定义保留
```

### 3.3 与现有 catalog 兼容

迁移期保留 `catalog/factors.jsonl` 作为只读兼容视图：服务层每次写 sqlite 后，物化导出当前 active 因子到 jsonl，`storage/catalog.py` 现有接口不改。等所有调用方切到 `factor_service` 后再决定废弃 jsonl。

导入旧库时，为每条现有因子建一张 `snapshot_id=legacy` 的证书，`caliber_digest=unknown`，标记为待复验，避免历史资产凭空获得新快照下的有效性。

## 4. 测试与验收

- **sqlite 基础设施**：迁移幂等、WAL 并发写、路径逃逸拒绝、只读连接无法写。
- **快照服务**：清单 digest 与文件一致、`latest` 解析与冻结、归档快照不可被新计划引用、补采后旧 run 重建成功。
- **复盘服务**：只处理 active 因子、diff 计算正确、失败不改证书、重复 enqueue 幂等。
- **因子服务**：同一表达式不产生两个 factor_id、同一因子在不同快照下有两张证书、物化导出与 sqlite 一致。
- **回归**：现有 788 项测试全绿；新增测试用临时数据根，不碰真实 `~/factorfactory`。

## 5. 实施阶段

1. **P1 基础设施**：`storage/sqlite_store.py` + 迁移 + `snapshot` 三张表 + `flow snapshot create/list/show`。
2. **P2 计划接入**：`supply.snapshot` 解析、冻结、指纹、Boundary/Panel 按清单读数据。
3. **P3 因子库迁移**：`factors` 表 + 证书表 + 兼容导出 + 旧库导入。
4. **P4 复盘闭环**：`replay` 服务 + CLI + 决策规则 + 报告。
5. **P5 自动化**：新快照创建后自动 enqueue 全部 active 因子，告警接入现有 notify。

每个阶段独立可验收，P2 完成后即可解决「补采数据导致旧 run 无法重算」这个已复现问题。
