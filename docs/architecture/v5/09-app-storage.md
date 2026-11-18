# 09 · 应用层与存储层

> 本篇补上原五层架构缺的两层：
> **应用层**在最上面——用户碰到的只有它，现在只做 CLI，但契约必须能承载 Web；
> **存储层**在最下面——中间产物落到哪、怎么写、怎么查、怎么清。
> 加完这两层是七层。原五层（意图 / 编排 / 能力 / 事实 / 跨层设施）语义不变，但**事实层与存储层必须分工**，否则「什么可信」和「存在哪」会混成一团。

---

## 1. 七层与依赖方向

```
        ┌─────────────────────────────────────────────┐
   L6   │  应用层  Application                        │  CLI（现在） / Web（预留）
        │  把用户意图变成一次提交，把结果渲染成人能看 │  不含业务逻辑
        └───────────────┬─────────────────────────────┘
                        │ 提交配置 / 只读查询
        ┌───────────────▼─────────────────────────────┐
   L5   │  意图层  Intent                             │  配置 → 编译 → 冻结 Blueprint
        └───────────────┬─────────────────────────────┘
                        │ Blueprint
        ┌───────────────▼─────────────────────────────┐
   L4   │  编排层  Orchestration                      │  8 工位 / 状态机 / 一次性事件 / 唯一裁判
        └───────────────┬─────────────────────────────┘
                        │ Call
        ┌───────────────▼─────────────────────────────┐
   L3   │  能力层  Capability                         │  12 项纯计算，无权限
        └───────────────┬─────────────────────────────┘
                        │ 只读 handle
        ┌───────────────▼─────────────────────────────┐
   L2   │  事实层  Facts     语义：什么可读、不可变、什么时点可用 │
        └───────────────┬─────────────────────────────┘
                        │ 读写原语
        ┌───────────────▼─────────────────────────────┐
   L1   │  存储层  Storage   物理：格式、路径、事务、GC │
        └─────────────────────────────────────────────┘

  跨层设施：日志 / 追踪 / 计时 / 随机源 / 指纹 / 缓存     （横切所有层）
  供给维护链：采集 → 门禁 A → 原子发布 → 移动别名          （流水线之外，只读写 L1）
  ↑ 19-supply.md 之后：采集直接落 <root>/<dataset>/<exchange>/<market>/*.parquet，
    没有暂存/版本目录/别名表；供给路径规则归 v5/upstream/store.py，不属九区布局
```

### 1.1 事实层与存储层的分工（这是新增两层里最容易做错的一处）

| | 事实层（L2） | 存储层（L1） |
|---|---|---|
| 回答 | 「这份数据可不可信、能不能在这个时点用」 | 「它落在哪个文件、用什么格式、怎么原子写」 |
| 核心概念 | as-of、不可变、版本、lineage、可用窗口 | 路径、schema、事务、引用计数、GC |
| 换格式时 | 不变 | 全变（feather → parquet） |
| 换机器时 | 不变 | 全变（路径、盘符） |
| 谁碰它 | 编排层、能力层（只读） | **只有事实层与供给链** |

**因此有一条硬边界**：能力层**不得** import `duckdb` / `pyarrow` / 拼任何文件路径。它只拿到事实层给的 handle。这条可以用 AST 扫描机器检查（见 §2.6 AC31）。

---

## 2. 应用层（L6）

### 2.1 它只做三件事

| 做 | 不做 |
|---|---|
| 把命令行参数翻译成「一份配置引用 + 一次提交」 | 不含任何口径参数 |
| 调用意图层编译、发起 Ticket | 不直接调能力层 |
| 把事实层查出来的东西渲染成表 / JSON / ndjson | 不读 Parcel 内部结构、不解析 parquet |

判据一句话：**应用层删掉，八步流水线仍然完整可跑（只是没人能启动它）。**

### 2.2 CLI 命令面

每条命令必须声明三件事：**mutation 等级**、**是否产 Ticket**、**退出码语义**。

| 命令 | 做什么 | mutation | 产 Ticket | 可能的最大退出码 |
|---|---|---|---|---|
| `doctor` | 体检 11 项，不起执行 | `read_only` | 否 | 2 |
| `estimate` | 预估成本与耗时 | `read_only` | 否 | 2 |
| `refresh` | 更新行情与加密流 | `writes_supply` | 否 | **4**（供给失败） |
| `run` | 编译 + 跑一遍八步（核心） | `writes_facts` | **是** | **4**（编译期供给门禁） |
| `station` | 单步执行指定工位（核心，观察不是重试） | `writes_facts` | **是** | **4** |
| `explain` | 只编译不执行，输出逐叶子来源与 C1–C22（核心） | `read_only` | 否 | **4**（与 `run` 同一套编译，含门禁 B） |
| `inspect` | 按 `run_id` 查产物、状态与指纹链（核心） | `read_only` | 否 | 2 |
| `stations` | 列出八工位规格、默认实现、名字空间、副作用等级（核心） | `read_only` | 否 | 2 |
| `pipeline` | `refresh → doctor → run` | `writes_facts` | **是** | **4** |
| `factors` | 查候选、拒绝原因、排序 | `read_only` | 否 | 2 |
| `backtest` | `--on <ticket>`：在冻结 ticket 上重算组合净表现（combine→allocate→backtest，只在内存里，不落 steps/） | `read_only` | 否 | 3 |
| `evaluate` | `--on <ticket> [--candidate <id>]`：重算候选表达式暴露与 IC/ICIR | `read_only` | 否 | 2 |
| `promote` | 晋升到 `factor_db` | `writes_facts` + `writes_ledger` | 否 | 3 |
| `factor_db` | `list/show/corr/compare/export/combine/rm` | 依子命令 | 否 | 3 |
| `holdout` | `plan` / `reveal` | `writes_ledger` | 否 | **4**（窗口未就绪） |
| `certify` | `--on <ticket> --yes`：重算 walk-forward 认证（复用同一判据） | `writes_facts` + `writes_ledger` | 否 | 3 |

> `explain` 是只读命令，但**只读 ≠ 不会遇到供给失败**：它走与 `run` 完全相同的十步编译（含门禁 B），
> 数据缺版本/不适用/末端未闭合同样必须给 `E4xxx` → 退出码 4。原表写 2 会让「同一份计划，
> `run` 报 4、`explain` 报 2」这种无法解释的差异出现（S2-M5 修正）。

**命令面分两组，不是两套。** 上一版这里只列了业务命令，而 `s0/README.md` §4 又另外写了 `flow run / station / explain / inspect`，两边从未对齐——开发时「该实现哪四个名字」没有答案。现在固定为：
| 组 | 命令 | 何时冻结 | 判据 |
|---|---|---|---|
| **核心五条** | `run` / `station` / `explain` / `inspect` / `stations` | **S0** | 只依赖 plan + ticket，是流水线本体；`stations` 是只读自省（`s0/05` 的 O1 要用它）。S0 的十二条验收全部要靠它们断言退出码 |
| **业务命令** | `doctor` / `estimate` / `refresh` / `pipeline` / `factors` / `backtest` / `evaluate` / `promote` / `factor_db` / `holdout` / `certify` | S1 起逐步，S7 齐 | 每条都建立在核心五条之上，不新增执行语义 |

两条配套规则：

- **`experiment` 是 `run` 的旧名**，保留为别名一个版本用于迁移，之后删除。同一件事两个名字，日志与文档里就会出现两种说法，排查时先要翻译一遍。
- **`pipeline` 不是第二条流水线**，它只是 `refresh → doctor → run` 的串联与单一退出码汇总。它的存在理由是「一次调度只产生一个退出码」，不是「另有一套执行路径」。

`mutation` 四个等级的含义：

| 等级 | 能改什么 | 撤销方式 |
|---|---|---|
| `read_only` | 什么都不改（可以写 `tmp/`） | — |
| `writes_supply` | 只能改供给链发布的行情与流 | 重新发布 |
| `writes_facts` | 新增不可变对象（Parcel、暴露、账本） | 不能撤销，只能新增版本 |
| `writes_ledger` | 追加试验 / 揭示 / 晋升登记 | **不可撤销**（这是全局单调的来源） |

### 2.3 三条硬规则

**P1 · 命令行不允许出现口径参数。**
`--horizon` / `--cost-tier` / `--rebalance` / `--annualisation` 一律不得出现在任何命令上。这些只能写在配置的 `caliber` 块里（C20）。理由：一旦命令行能传，Web 一个请求就能绕开编译期的口径一致性检查——「判据用的策略」和「报出来的策略」会再次分家。

**P2 · 缺参数就报错，不猜。**
除纯展示参数（`--limit` / `--format`）外，任何缺省都会让「这次跑的是什么」变得不可复述。`--dry-run` 也不许「顺手用上次的配置」。

**P3 · `writes_ledger` 以上必须显式确认。**
`promote` / `holdout reveal` 需要 `--yes` 或交互确认；`--dry-run` 必须先能给出完整预览。

### 2.4 输出契约

每个命令都必须支持 `--json`，且 JSON schema 是**稳定契约**（脚本与将来的 Web 都依赖它）。人类可读格式只是渲染层。

```
{ "ok": true, "command": "factors", "run_id": "...", "data": {...},
  "warnings": [...], "exit_code": 0 }
```

规则：

- `ok` 与 `exit_code` 必须一致（历史问题：`cli.main()` 丢弃返回值导致退出码恒 0）。
- 警告与错误分开：`warnings` 不影响退出码，`errors` 影响。
- 渲染层不得做单位换算的「美化」（历史问题：前端 `pct()` 把 cagr 缩小 100 倍）。**数值原样输出，单位写在字段名里**（`sharpe` / `cagr_pct`）。

**信封只有一个，内容的形状由命令声明。** 上例只定了信封；`data` 里有什么，必须逐命令写死，否则「稳定契约」就是一句空话——调用方只能靠试。

```
{ "ok": false, "command": "run", "run_id": null, "data": null,
  "warnings": [], "exit_code": 2,
  "errors": [ { "code": "E1020", "layer": "usage", "retryable": false,
                "message": "工位参数里出现口径字段 'horizon'",
                "path": "target.params.horizon",
                "hint": "口径只写在 caliber.label，工位自带一份就会有第二份",
                "context": { "field": "horizon", "expected_block": "caliber.label" } } ] }
```

错误对象的六条规则：

| 字段 | 必填 | 约束 |
|---|---|---|
| `code` | 是 | 必须是 `s0/01` 码表里的 49 个之一；**不得出现自定义码** |
| `layer` | 是 | 由 `code` 前缀推导，不得独立赋值（存两份必然漂移） |
| `message` | 是 | 说清「哪里不对」，一句 |
| `retryable` | 是 | 只有供给类（`E4xxx`）与存储类（`E5xxx`）可为 `true`；**为 `true` 也不代表可以自动重试**，它只说明「稍后可能成功」 |
| `path` | 视情况 | 能定位到配置键或产物路径时必须有 |
| `hint` | 视情况 | **必须是下一步动作**（「写在 caliber.label」），不是「不允许这样」 |
| `context` | 否 | 只放定位信息（键名、期望值、边界）；**不得放数据样本或整份配置**——它会进日志与 Web 响应 |

**错误对象不带 `exit_code`。** 进程退出码取**第一条** error 的推导值（`ok` 与之一致）；把 `exit_code` 放进每个 error，会出现「三条错误各带一个退出码，进程只能返回一个」的歧义，而这个歧义会一路传进 Web 的 HTTP 状态映射。

两条与信封相关的硬规则：

- `ok=false` 时 `data` 必须是 `null`，不是 `{}`。空对象会被读成「查询成功但结果为空」，那是另一种结论。
- `warnings` 与 `errors` 不混用。`warnings` 不改变退出码；任何改变了退出码的东西都是 `error`。

各命令的 `data` 键（`s0/01` 的 Parcel 是它们的来源，渲染层不得自己算数）：

| 命令 | `data` 顶层键 |
|---|---|
| `doctor` | `checks[]`（`id` / `status` / `detail`）、`summary{fatal,warn,pass}` |
| `estimate` | `stages[]`（`station` / `seconds` / `cost_bp`）、`total_seconds` / `total_cost_bp` |
| `refresh` | `datasets[]`（`name` / `version` / `written` / `skipped_reason`） |
| `run` | `run_id` / `ticket` / `stages[]` / `candidates` / `promoted` |
| `station` | `run_id` / `station` / `parcel_ref` / `status` / `seconds` |
| `explain` | `plan_digest` / `leaves[]`（`path` / `value` / `source`）/ `checks[]`（`C1`…`C22`） |
| `inspect` | `run_id` / `ticket` / `parcels[]` / `fingerprint_chain_ok` |
| `stations` | `stations[]`（`name` / `revision` / `default_impl` / `side_effect` / `namespace`） |
| `certify` | `ticket` / `folds` / `metrics{}` / `reproducible_manifest` |
| `pipeline` | `run_id` / `stages[]`（含 `exit_code`）/ `doctor` |
| `factors` | `run_id` / `rows[]` / `reason_counts{}` |
| `backtest` | `summary{sharpe,cagr_pct,max_dd_pct,turnover,net_return,cost_breakdown,periods}` |
| `evaluate` | `expression_digest` / `series_ref` / `stats{}` |
| `promote` | `factor_id` / `target_path` / `lineage` |
| `factor_db` | `rows[]`（`list/show/corr/compare`）或 `ref`（`export/combine`） |
| `holdout` | `windows[]`（`id` / `state` / `revealed_at`） |

`*_ref` 是 `Ref` 的 JSON 形态（见 `s0/01` §5）：调用方拿它去事实层取数，而不是让渲染层把数字抄进 JSON。抄一次就有两份，两份就会不一致。

### 2.5 Web 预留：不实现，但契约要能接住

不写 Web 代码，只保证一件事：**加 Web 时不需要改业务代码**。

| 要求 | 做法 |
|---|---|
| 同一套命令语义被两种壳共用 | 命令定义（名、参数 schema、输出 schema、mutation）集中在一处，CLI 只是其中一个 shell |
| 退出码能映射到 HTTP | `0→200/201`、`1→500`（执行/存储失败）、`2→400`（用法或编译错误）、`3→500`+告警（契约违反，按 bug 处理）、`4→503`（供给失败，「稍后重试」的语义） |
| 长任务不阻塞请求 | 提交返回 `ticket_id`，进度从事实层查；**HTTP 层不持有任务状态**（历史问题：API 单 worker 才能工作，任务状态在进程内存里） |
| 多用户隔离 | 应用层不带身份概念；身份只决定「能看哪个 workspace」，不决定口径 |

**明确不做**：Web 端不提供「改口径重跑」按钮。要改口径就是改配置、重新编译、新 Ticket。

### 2.6 不变量与验收

> 编号与陈述以 `05-contracts.md` §1（不变量）与 §4（验收）为准；本表只补本篇的检查点，不重复定义。

| 编号 | 内容 | 检查点 |
|---|---|---|
| I30 | 应用层不含口径参数；所有命令的参数列表里不得出现 `caliber` 内任何字段名 | 向任一命令加 `--horizon` → 启动即报错 |
| I31 | 每个命令声明 mutation 等级，`writes_ledger` 以上必须二次确认 | 无 `--yes` 调 `promote` → 拒绝，**退出码 2（用法错误，与 s0 的码表一致）** |
| I32 | `--json` 输出是稳定契约；`ok` 与 `exit_code` 一致 | 对每条命令跑一次 `--json`，校验字段与退出码一致 |
| AC31 | 能力层不碰存储层 | AST 扫描 `capability/**`：不得出现 `import duckdb` / `import pyarrow` / `import feather` / 拼接 `facts`/`parcels` 路径的字符串字面量 |
| AC32 | 加 Web 不改业务 | 新增 `app/web/` 后，`git diff --stat` 里 `core/` 与 `capability/` 零改动 |

---

## 3. 存储层（L1）

### 3.1 三类存储，三种语义

最常犯的错是把三类混成一类。它们的不变量完全不同：

| 类 | 存什么 | 形态 | 不变式 | 坏了怎么办 |
|---|---|---|---|---|
| **对象存储** | 面板、暴露、标签、账本产物、因子值 | parquet（列存，内容寻址） | 一次写永不改；同名即同内容 | 缺了就从上游重算 |
| **登记库** | 试验账本、揭示账本、晋升、事件 | append-only `jsonl` | 只追加，永不修改或删除 | **不能坏**——这是全局单调的唯一来源 |
| **索引库** | 可查询的元数据面 | `duckdb` | 可从对象 + 登记**完整重建** | 删掉重建 |

**一条规矩**：**索引不是真相**。`duckdb` 文件不进备份；`jsonl` 与 `parquet` 必须进。任何人不得只写索引不写登记。

判据：如果把 `db/meta.duckdb` 删掉，系统应该能通过一次 `reconcile` 恢复全部查询能力。做不到就说明有真相藏在索引里。

### 3.2 目录布局

```
{workspace}/
  supply/                       # 供给维护链发布（只读给流水线）
    {exchange}/{market}/{timeframe}/
      {symbol}-{tf}-{kind}.parquet        # OHLCV
      streams/{symbol}-{tf}-{stream}.parquet
    _published.json                       # 当前别名指向 + 每份的文件哈希

  facts/                        # 不可变对象，按内容寻址
    panels/{panel_digest}.parquet
    labels/{label_digest}.parquet
    exposures/{node_or_call_digest}.parquet
    ledgers/{call_digest}.parquet
    reports/{report_digest}.json

  parcels/{run_id}/{station_seq}-{station}-{parcel_digest}/
    manifest.json               # 上游 digest、policy_digest、文件清单
    *.parquet

  runs/{run_id}/
    run.json                    # 状态、起止、退出码
    log.ndjson                  # 结构化日志（一行一条）
    trace.ndjson                # span
    stations/{seq}-{station}/
    summary.json                # 给应用层渲染

  db/
    ledger.jsonl                # append-only 真相（试验/揭示/晋升/事件）
    meta.duckdb                 # 可重建索引（视 parquet + jsonl）

  cache/                        # 可全删
  tmp/                          # 写入暂存，同盘，供 os.replace
```

**逻辑分区（`04-runtime.md` §9 的四区）与物理路径的对应**，一处定义，避免两套名字各说一套：

| 逻辑分区 | 物理路径 | 语义 |
|---|---|---|
| **Charter**（只读章程） | `baselines/ plans/ schemas/ catalog/` | 编译期只读；不属于任何执行 |
| **Depot · 种子** | `supply/` | 不可重建事实（采集结果）+ `_published.json` 别名表 |
| **Depot · 派生** | `facts/` | 可重建（面板/暴露/标签/账本/报告），内容寻址 |
| **Depot · 登记** | `db/ledger.jsonl` | **唯一真相**，只追加；`db/meta.duckdb` 只是它的索引 |
| **Depot · 维护** | `supply/_published.json`（别名）+ `tmp/`（暂存） | 发布与租约状态 |
| **Ticket** | `runs/{run_id}/` | 一次执行的状态主体；`run_id` 即 `Ticket` 的落地标识 |
| **Ticket · 工位产物** | `parcels/{run_id}/`（引用）+ `runs/{run_id}/stations/`（四件套） | 料包本体在 Depot 派生区，执行目录只存引用与日志 |
| **Scratch** | `cache/ tmp/` | 可随时清空，不得被任何产物引用 |

### 3.3 写入协议：三段，每段崩溃都可恢复

```
stage    → tmp/{uuid}.parquet           （同盘，保证 rename 原子）
commit   → os.replace(tmp, final)       （原子；Windows/NTFS 上同样原子）
register → append jsonl + upsert duckdb （非原子，但可 reconcile）
```

三个崩溃点各自的结果：

| 崩在 | 留下 | 恢复 |
|---|---|---|
| stage 后 | `tmp/` 里的孤儿 | GC 清（>24h） |
| commit 后、register 前 | 对象在、登记缺 | `reconcile` 扫 `facts/` 的 manifest 补登记 |
| register 中间 | 索引写了一半 | 把 `ledger.jsonl` 重放到 `duckdb` |

**因此必须有一个 `reconcile` 命令**，且它必须是幂等的。这是「对象与登记分离」的必然代价，不能假装没有。

### 3.4 路径与 schema 的唯一定义

| 项 | 规则 |
|---|---|
| 路径 | 只在 `storage/layout.py` 里拼。任何模块不得出现 `Path(...) / "facts"` 这种字面量（AC31） |
| schema | 每个 parquet 带 sidecar `{name}.schema.json`（列名、dtype、`schema_version`）。读时不猜 |
| 版本 | 不兼容变更 → `schema_version + 1` + 一个迁移函数；不允许「读的时候兼容两版」 |
| 编码 | 时间统一 UTC aware；数值 `float32` 存暴露、`float64` 存价格与账本 |

`float32` 存暴露是一个有意的选择：暴露是排序用的，精度足够；而账本要能核对，必须 `float64`。

### 3.5 生命周期与 GC

| 区 | 策略 |
|---|---|
| `tmp/` | 启动时清掉 >24h 的孤儿 |
| `cache/` | 容量上限 + LRU，可随时全删 |
| `facts/` | **不自动删**。删除必须走 `factor_db rm` 软删到 `_deleted_{date}/` |
| `parcels/` | 跟随所属 run 的保留策略 |
| `runs/` | 保留最近 N 次 + 有晋升记录的永久保留 |
| `db/meta.duckdb` | 可删，`reconcile` 重建 |

**引用计数**：一个 `facts/` 对象被多少 Parcel / 登记行引用，必须能数出来。删除前先查引用；有引用就拒绝。没有引用计数的 GC 一定会删掉还在用的东西。

### 3.6 加密数据的规模墙（为什么分区不是可选项）

实测当前数据面：755 个合约有日线 OHLCV，758 个有资金费，只有 20 个有持仓量与基差。

| 场景 | 行数 | 内存（float32 × 5 列） | 处理 |
|---|---|---|---|
| 20 资产 × 1187 bar（现状） | 23,740 | 0.5 MB | 单文件足够 |
| 755 合约 × 1187 bar | 896,185 | 18 MB | 单文件仍可 |
| 755 合约 × 1h × 3 年 | 约 19,800,000 | 396 MB | **必须按时间分区** |
| 加 5 个 stream 列 | ×3 | 1.2 GB | 分区 + 列裁剪 |

所以存储层必须支持：**按 `(timeframe, 年-月)` 分区，按资产成列**。读取模式是「一个时间窗 × 全部资产 × 若干列」，所以分区键选时间、列内排资产是对的；不要按资产分文件（那样每次读都要开 755 个文件）。

> `data/binance/futures/` 现在就是按资产分文件（755 个 feather）。这在 20 资产时没问题，扩到 755 时会退化成「每次读取 755 次 open + 755 次 pivot」。存储层的分区布局就是为这件事准备的。

---

## 4. 两层之间的接口

事实层给能力层的东西必须是**不可伪造的 handle**，不是路径字符串：

```
PanelHandle    { digest, timeframe, assets[], columns[], asof, storage_ref (不透明) }
ExposureHandle { digest, valid_mask_ref, columns[], asof }
LabelHandle    { digest, horizon, overlap_policy, asof }
LedgerHandle   { digest, detail_level }
```

规则：

1. **handle 不暴露可拼接的路径**。`storage_ref` 是不透明 token，只有存储层能解。
2. **handle 自带 `asof`**。能力层拿到 handle 就等于拿到了「这个时点能看到什么」，不需要自己判断前视。
3. **同一 digest 必须指向同一份内容**。这是内容寻址的直接要求，也是缓存正确性的前提。

---

## 5. 明确不做

| 不做 | 理由 |
|---|---|
| 存储层做成插件（可选 parquet / 可选 sqlite / 可选 S3） | 现在只有一个部署形态。抽象存储后端等于给「以后可能要换」付现值，而每个后端都要各写一遍事务与 GC |
| 用 ORM | 我们的查询是「100 个 parquet 按时间 join」，SQL 是表达式不是对象图 |
| 把 `duckdb` 当写入库 | 它是可重建索引。真相在 jsonl，写进 duckdb 的账本会丢掉「只追加」的语义 |
| Web 端直连存储层 | 应用层只能走事实层的只读查询；否则 Web 一改就会绕过 as-of 检查 |
| 应用层做「默认参数」 | 见 P2。默认值让「这次跑的是什么」不可复述 |
