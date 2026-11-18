# S0 · 03 运行时

> 运行时回答三个问题：**跑之前有什么、跑的时候东西放哪、谁能写。**
> 它不参与任何计算，但它决定了「可复现」这句话有没有物质基础。

---

## 1. 四区与权限矩阵

| 区 | 名称 | 内容 | 运行期写入 | 跨执行共用 |
|---|---|---|---|---|
| **W1** | `Charter` 静态章程 | 基线清单、计划、schema、名字目录、契约版本表 | **禁止** | 是 |
| **W2** | `Depot` 共享仓 | 事实种子、可重建派生物、只追加登记 | **禁止**（只有维护链在编译前写） | 是 |
| **W3** | `Ticket` 执行区 | 冻结方案、环境快照、产物索引、八工位目录、事件记录 | 允许，限本执行 | 否 |
| **W4** | `Scratch` 暂存区 | 临时文件、失败半成品、日志缓冲 | 允许 | 否 |

**四区是「权限分区」，不是「存储形态」**。存储形态另有两类，见 `09-app-storage.md` §3.1：

| 存储形态 | 落在哪 | 不变式 |
|---|---|---|
| 对象（parquet，内容寻址） | 主要在 W2 与 W3 | 一次写永不改 |
| 登记（append-only jsonl） | W2（试验/揭示/晋升账本） | 只追加；**这是全局单调的唯一真相** |
| 索引（duckdb） | W2 | 可从上面两类完整重建；坏了就删 |

判据：把索引库删掉后必须能靠一次 `reconcile` 恢复全部查询能力；做不到就说明有真相藏在索引里。

### 1.1 权限靠接口形状保证

「执行期不得写共享仓」不能靠文档约定，必须让写入能力在类型上不存在。因此存储层提供**两个互不兼容的接口**：

```
ReadOnlyStore:
    read_ref(ref) -> bytes
    resolve_pin(dataset, alias) -> Pin
    list_versions(dataset) -> list[str]

TicketWriter(ReadOnlyStore):        # 只对 Ticket 根与其下目录生效
    write_parcel(ticket_id, parcel) -> None
    write_trace(ticket_id, record) -> None
    append_ledger(ticket_id, entry) -> None
    write_step(ticket_id, station, seq, parcel) -> None
```

| 构造者 | 拿到 | 拿不到 |
|---|---|---|
| 维护链 | `DepotWriter`（唯一） | 不接触 Ticket 区 |
| 执行器 | `TicketWriter` | **无任何 Depot 写方法** |
| 能力层 | 无存储句柄（只收 `Ref`，由执行器代读） | 全部 |
| 单步命令 | `TicketWriter` 的 `write_step` 子集 | 其余写方法 |

能力层连存储句柄都不该有：它需要的数据由执行器解析 `Ref` 后以只读数组传入。否则「能力自己查一下最新数据」就有了路径。

### 1.2 写入即校验

`TicketWriter` 每次写入必须做四项检查，任一失败即 `E5004`：

1. 目标路径规范化后仍位于 `Ticket/<ticket_id>/` 之内（拒绝 `..` 与符号链接逃逸）；
2. `ticket_id` 等于当前执行上下文，不接受参数传入另一个 id；
3. 内容寻址路径与内容 `digest` 一致；
4. 目标文件不存在，或存在但内容相同（幂等）；内容不同则拒绝，**不覆盖**。

第 4 条把「Parcel 不可变」变成一个写入层的拒绝，而不是一条纪律。

---

## 2. 目录形态

```
Charter/
  baselines/vN/baseline.json
  plans/<plan_id>.json
  schemas/{plan,baseline,catalog}.schema.json
  catalog/impls.json
  contracts/stations.json

Depot/
  seeds/<dataset>/<version>/
      manifest.json
      payload/...
  pins/<dataset>.json              别名，只存 {version, checksum}
  derived/<digest>/                可重建，可回收
  registers/
      trials.jsonl                 试验计数，只追加
      events.jsonl                 一次性事件，只追加
      published.jsonl              已发布结论，只追加
      members.jsonl                因子成员登记，只追加
  upkeep/
      log/<dataset>/<version>.jsonl
      leases/<dataset>.lock
      staging/<dataset>/<version>/ 原子发布暂存

Ticket/<ticket_id>/
  charter.json                     冻结方案副本 + 来源表 + 指纹 + pins
  environment.json                 环境快照
  index.json                       产物索引 + 状态机 + 失效标记
  trace.jsonl                      每工位一条
  ledger.jsonl                     本执行事件（只追加）
  stations/<Station>/
      parcel.json
      payload/
      log/<call_digest[:16]>.jsonl
      diagnostics/
  steps/<seq>-<Station>-<hash>/           单步观察，结构同上
  report/
      certificate.json
      reproducible.json

Scratch/<run_id>/                  随时可清
```

`pins/<dataset>.json` 是**唯一的可变对象**：它是一个别名，只有维护链的原子发布可以移动它。除此之外 Depot 全不可变。

---

## 3. Charter：静态章程

### 3.1 基线格式

`baseline.json` 是一份**可 diff 的完整清单**，不是散落在代码里的字面量。最小形态：

```json
{
  "baseline_version": "v1",
  "released_at": "<UTC>",

  "supply":   {"exchange": null, "datasets": [], "symbols": null, "frequency": "1d"},

  "caliber": {
    "execution": {"entry": "next_close", "rebalance": 5,
                  "annualisation": "calendar_365", "funding": "per_contract_events"},
    "label":     {"horizon": 5, "transform": "cs_rank", "neutralise": [],
                  "overlap_policy": "nw_adjusted",
                  "min_width": 20, "width_policy": "drop"},
    "allocate":  {"quantiles": 5, "long_short": true, "weighting": "equal",
                  "max_weight": 0.10, "net_exposure": 0.0,
                  "turnover_max": 0.50, "min_members": 8},
    "net":       {"cost_tiers": [40, 100, 250], "slippage": "tiered",
                  "fee_model": "taker_with_funding", "max_leverage": 1.0,
                  "initial_margin": 0.10, "maintenance_margin": 0.005,
                  "liquidation_buffer": 0.01}
  },

  "boundary": {"impl": "rolling_liquidity_pool@v1", "params": {},
               "time_axis": {"mine": null, "oos": null, "holdout": "reserved"}},
  "panel":    {"impl": "point_in_time_align@v1", "params": {}},
  "target":   {"impl": "forward_return_cs@v1"},

  "search":   {"algorithms": [], "prior_pool": [],
               "trial_family": null, "seed": null},

  "evidence": {"impl": "two_stage_funnel@v1", "params": {}},

  "verdict":  {"impl": "sentinel@v1",
               "params": {"dsr_min": null, "fdr_max": null, "cost_adj_min": null}},

  "portfolio": {"combiner": null,
                "window": {"fit": null, "embargo": 5, "eval": null}},
  "certify":   {"impl": "walk_forward@v1", "params": {"folds": 5, "embargo": 5}},

  "operations": {"cache": true, "workers": 1, "log_level": "info"}
}
```

五条约束：

1. **`null` 只有一个含义：必须由计划显式给出**。编译期发现仍为 `null` → `E1008`。这条防的是「不填就通过」——给判据阈值配一个「看起来很合理」的默认值，等于把「没做决定」伪装成「做了决定」。`verdict.params` 三项与 `supply.exchange` / `supply.symbols` / `search.trial_family` / `search.seed` / `portfolio.combiner` 都属此类。
2. `search.algorithms` 与 `portfolio.combiner` 允许空，但编译期若仍为空 → `E1008`（必须显式选）。
3. 基线不得引用任何外部绝对路径。
4. **基线整体与计划同构**——同一份键、同一套类型（`v5/contracts/plan_schema.json`），只少 `plan` 与 `baseline` 两个自身字段（版本用 `baseline_version` 表达）。上一版基线用的是 PascalCase 的 `stations` 映射（`Boundary` / `Panel` …）加一个扁平 `gates`，与计划的小写工位块、`verdict.params` 是两套结构；那样「这个值来自基线还是计划」只能靠人读代码回答，来源表（§5.2）无法机械化。
5. **条件键在条件不成立时可以缺省**。`neutralise_window` 仅在 `neutralise` 非空时必填，基线默认 `neutralise: []`，因此这里没有该键。缺省不等于 `null`：缺省 = 不适用，`null` = 必须给但还没给（见约束 1）。

> 早期版本的基线有四处与计划不同构，全部已删除：扁平 `policy` 块（`horizon` / `rebalance` / `cost_tiers` / `funding_period` / `min_cross_section` 挤在一起）、PascalCase 的 `stations` 映射、独立的 `gates` 块、以及把数据属性写进口径的 `funding_period`。它们各自都「看起来合理」，合起来的效果是来源表要写三套映射，且 `funding_period` 与 I34 直接冲突。

### 3.2 名字目录

`catalog/impls.json` 是「名字 → 实现@版本」的唯一权威表：

```json
{
  "<station>": {
    "<name>": {
      "impl_version": "v1",
      "module": "flow.capability.ops.<...>",
      "param_schema_ref": "<schema id>",
      "deterministic": true,
      "stateful": false,
      "cacheable": true,
      "side_effect": "pure",
      "namespaced": "closed_multi | single | open | fixed",
      "placeholder": false
    }
  }
}
```

`placeholder=true` 的实现**不得作为默认**（`E1011`），也不得在 strict 模式的生产计划里被显式选中（`E1002`，`reason=placeholder`）。

---

## 4. Depot：共享事实

### 4.1 三类资产

| 类 | 定义 | 能否重建 | 删除策略 |
|---|---|---|---|
| `Seed` | 外部采集的原始事实 | 否 | 默认永久；删前须确认无任何归档引用 |
| `Derived` | 由种子与冻结方案确定性重算的中间结果 | 是 | 可按配额清理 |
| `Register` | 试验计数、事件、已发布结论、成员登记 | 否，是状态 | 不删、不改、不清零 |

### 4.2 版本目录与清单

一个 `Seed` 版本不是「文件名 + 修改时间」，而是不可变快照：

| 项 | 要求 |
|---|---|
| 目录名 | `<version>`，`version = checksum[:16]` |
| 清单 | `manifest.json` 字段与 `Pin` 等价（见数据契约 §3.3） |
| 校验和 | 对 `payload/` 全部文件按路径排序后逐文件哈希再汇总 |
| 缺失区间 | 清单必须显式列出，不允许「没写就是没有」 |
| 血缘 | 记录上游来源标识（交易所、数据集、抓取批次） |
| 门禁结果 | `gate_a.passed` 与失败原因码 |

同内容重复采集：`version` 相同 → 视为同一版本，不新建目录、不改 `acquired_at`，只追加一条维护日志。

### 4.3 别名解析

`pins/<dataset>.json` 只有一个字段有意义：`{version, checksum}`。解析规则：

- **只在编译期解析一次**，结果写入 `Blueprint.supply_pins`；
- 运行期任何层解析别名 → `E2001`；
- 别名指向不存在的 `version` → `E4001`；
- 别名指向 `gate_a.passed=false` 的版本 → `E4005`。

「运行期解析别名」不是效率问题，是正确性问题：同一次执行的不同工位读到不同事实，产物之间的血缘就失去意义。

---

## 5. 配置编译

编译是**纯函数**：`compile(baseline, plan, overrides, aliases) -> Blueprint`。它不读数据、不写文件、不取时钟（`compiled_at` 由调用方注入）。

### 5.1 十步

| 步 | 输入 | 动作 | 失败 |
|---|---|---|---|
| 1 | `baseline.json` | 载入，校验 `baseline_version` 与 schema | `E1001` / `E1008` |
| 2 | `plan.json` | 按路径叠加；未知路径拒绝 | `E1001` |
| 3 | `overrides` | 叠加；构成性参数须显式放行 | `E1005` |
| 4 | 合并结果 | 合并 `caliber` 四子块；检出工位自带口径字段 | `E1020` |
| 5 | 合并结果 | 解析每个模块的名字 → `impl@version` | `E1002` / `E1011` |
| 6 | `param_schema` | 逐项校验；多余字段拒绝 | `E1003` / `E1004` |
| 7 | `composes` | 解析子调用图；未声明调用、深度 > 2 拒绝 | `E2008` |
| 8 | 实现签名 | 校验与工位契约一致、参数类别一致、三段窗口与重叠声明、经济口径自洽 | `E1012` / `E1013` / `E1021` / `E1022` |
| 9 | 供给别名 | 解析为 `Pin`，做门禁 B 四项检查 | `E4001` / `E4002` / `E4005` |
| 10 | 全部结果 | 计算四个锚点指纹（共八个）、生成来源表、冻结 | `E2002` / `E1007` |

第 9 步是唯一接触外部世界的步骤，且**只读**。它失败时不创建 Ticket、不写任何目录、不触发采集。

第 4 步必须排在第 5 步之前：口径块决定每个实现能读到什么（`policy_reads`），名字解析必须在口径已经完整的前提下进行，否则「这个实现该读哪个子块」无从判断。

**步骤 1–4 已经落到代码**：`v5/contracts/plan_schema.py` 的 `validate_plan()` 做键、类型、枚举、范围与 C20 的检查，`assert_valid_plan()` 抛出第一条违规（带 `path` 与 `hint`）。它**不校验 `params` 内部**——那是第 6 步的事，由各实现的 `param_schema` 负责。这条边界必须守住：两边都校验 `params`，就会出现两套参数规则，而「哪套生效」要读代码才知道。

步骤 1–4 与 5–10 的分界也是退出码与严重程度的分界：前者全部是 `E1xxx`（用法，退出码 2，改配置即可），后者才有 `E2xxx`（契约，退出码 3，按 bug 处理）。

### 5.2 来源表

每个叶子必须有来源，取值 `baseline@vN` / `plan` / `override`。`flow explain` 直接输出这张表：

```
caliber.label.horizon        5            baseline@v1    # 这份计划没写 horizon，基线给 5
caliber.execution.entry      next_close   plan
caliber.allocate.max_weight  0.05         plan
search.algorithms[0].name    gp_symbolic  plan
verdict.params.dsr_min       0.95         plan
operations.workers           4            override       # 非构成性，不进任何指纹
```

`dsr_min` 是**概率阈值**（0.95），不是「年化 Sharpe 门槛」。后者是从它反推出来的结果（某个 `n_trials` 下的隐含门槛，V4 实测约 1.53），属于输出而不是参数——把 1.53 写进 `dsr_min` 会被 `plan_schema` 按 `le > 1` 拒绝。这类「阈值与它推出的量」混淆是单位错误里最常见的一种。

`examples/plan_cs_2026q4.yaml` 里每个口径键都显式写了，因此那张表的来源全是 `plan`；本示例故意省略了 `horizon`，用来演示 `baseline@vN` 这一列。

注意路径形态：**跨度、入场价、权重上限出现在 `caliber.*` 下，不出现在 `target.*` 或 `portfolio.*` 下**。这张表本身就是「口径只有一个来源」的可读证据——如果某天表里出现 `target.horizon`，说明 C20 被绕过了。

来源表的作用不是展示，是**追责**：任何一个「我明明没改」的争议，看这张表就能定性。

---

## 6. 校验总表

C1–C22 全部 fail-closed。**没有一条允许「忽略并用默认值继续」。**

| # | 触发 | 错误码 |
|---|---|---|
| C1 | 配置路径不存在于 schema | `E1001` |
| C2 | 实现名不在目录 | `E1002` |
| C3 | 参数含 schema 外字段，或实现签名与契约不符 | `E1003` |
| C4 | 类型、范围、枚举不符 | `E1004` |
| C5 | 构成性参数出现在覆盖层且未放行 | `E1005` |
| C6 | 构成性参数改动但试验族未声明；数据版本变化后在原计划上重跑搜索（I59）；或对构成性参数做自动遍历而非预注册枚举 | `E1009` / `E1010` |
| C7 | 严格模式未注入裁决者 | `E1006` |
| C8 | 默认实现指向占位/演示实现 | `E1011` |
| C9 | 经济口径不自洽（判据与回测读到的成本、跨度、入场价、年化不一致） | `E1013` |
| C10 | 实现参数与声明的拟合/变换阶段冲突 | `E1012` |
| C11 | 别名未解析、指向未过门禁 A 的版本、或清单缺失/校验失败 | `E4001` / `E4005` |
| C12 | 已解析版本不满足本次时间轴、标的集、新鲜度或末端闭合 | `E4002` / `E4003` |
| C13 | 算法名未登记、四方法缺失、`describe()` 签名不符 | `E1002` |
| C14 | 算法参数越界、多算法配额总和超限、落盘时实际产出超限 | `E1004` / `E1017` |
| C15 | 算法声明字段超出供给覆盖 | `E1014` |
| C16 | 算法声明含禁用算子，或与全局白名单冲突 | `E1015` |
| C17 | 有状态算法拟合窗口非 `mine_only`，或拟合结果不可重建 | `E1016` |
| C18 | 组合域成员口径不一致、缺可用时间、纳入规则含表现类条件，或跨执行域成员未显式声明（I60） | `E1018` |
| C19 | 组合拟合窗口起点早于域内最晚可用时间 | `E1019` |
| C20 | 工位参数出现 `caliber` 已定义字段；实现声明与 `composes` 不符 | `E1020` / `E2008` |
| C21 | 三段窗口非法：`fit.end + embargo > eval.start`、`eval` 越出搜索区间、`fit` 与 `eval` 重叠 | `E1021` |
| C22 | `rebalance < horizon` 但未声明重叠处理，或 `width_policy` 缺失 | `E1022` |

C18–C22 是 S0 相对概念设计新增的五条：概念设计里组合域与口径的约束写在验收项中，S0 把它们升级为编译期检查，因为「运行期才发现不合规」意味着已经消耗了一次执行。

C20 值得单独说明：它同时承担两个职责——**禁止工位自带口径**，以及**禁止实现调用未声明的能力**。这两件事看起来无关，实质相同：都是「某个局部偷偷拥有了一份本该全局唯一的东西」。

### 6.1 检查必须是纯函数

每条检查的签名统一为：

```
check_<n>(blueprint_candidate, context) -> list[Error]
```

要求：

- 返回**全部**错误，不是遇到第一个就抛。用户一次看到十个错误，比修十次好。
- 不读时钟、不读文件（除 `Charter` 与 `Depot` 清单）、不写任何东西。
- 可在无网络、无数据的环境中运行——除 C11 / C12 需要读 Depot 清单之外。

---

## 7. Envelope：环境快照

| 面 | 字段 | 落盘 | 进指纹 |
|---|---|---|---|
| 可记录 | `python_version`、`package_versions`、`platform`、`machine`、`cpu_count`、`blas_threads`、`clock_basis`（`utc`）、`rng_impl`、`resource_limits`、`device`、`data_root_resolved` | 是 | **不进 `parcel_digest`** |
| 凭据 | `credential_refs`：名称 → 环境变量名或密钥别名 | 只存引用名 | 否 |

### 7.1 为什么环境不进产物指纹

如果 `environment_digest` 进 `parcel_digest`，那么换一台机器、升级一个无关依赖，全部历史产物都会「失效」，可复现性反而变成不可比较。

因此采取三档处理：

| 情形 | 处理 |
|---|---|
| 同环境重放 | `environment_digest` 相同且 `parcel_digest` 相同 → 完全可复现 |
| 跨环境，产物指纹相同 | `env_mismatch=true`，环境差异未影响结果，记录即可 |
| 跨环境，产物指纹不同 | `env_mismatch=true` 且 `reproduction_failed=true`，在可复现清单中显著标注，**仍不判失败** |

这条设计承认一个事实：浮点结果跨平台可能不同。承认它并显式标记，比假装它不存在更可靠。

### 7.2 凭据三条禁令

1. 值绝不出现在 `Blueprint`、`Parcel`、日志、trace、报告、指纹任何一处；
2. 取值失败必须显式失败（`E5002`），不得回退到匿名或只读模式；
3. 引用名本身可以记录，因为它不泄露值。

---

## 8. 门禁 B：只读适用性检查

编译期六项，全部只读，任一失败 → 编译失败（`E4002` / `E4003` / `E4005`）。

| 检查 | 判据 | 失败提示 |
|---|---|---|
| 门禁 A 已过 | `pin.gate_a.passed = true` | 版本保留供排查；提示做维护重新发布 |
| 覆盖区间 | `pin.coverage.start ≤ 时间轴起点` 且 `pin.coverage.end ≥ 时间轴终点` | 列出缺口区间，提示先做维护 |
| 标的集 | `digest(计划标的集) == pin.symbols_digest` | 列出计划标的集与两侧摘要 |
| 新鲜度 | `pin.acquired_at ≥ pin.coverage.end`（纯函数，不读时钟） | 给出采集时间与覆盖末端 |
| 末端闭合 | `pin.end_closed = true` | 指出未闭合的末端 |
| 供给维度 | `plan.supply.frequency` / `.exchange` == seed manifest 的 `timeframe` / `exchange` | 指出计划值与事实值 |

**为什么必须有「供给维度」这一项**：`supply` 整块会进 `blueprint_digest`，但 `Pin` 九字段
不含 `timeframe` / `exchange`。只比对 Pin 的话，「计划声明 4h、事实是 1d」「计划声明
binance、数据来自 okx」都会**全绿通过**，而整条研究的频率与来源都是错的——这正是
门禁要消灭的那类假成功。Pin 冻结不动，改为按 `pin.version` 只读该版本的
`seeds/<dataset>/<version>/manifest.json`：`.exchange` 对所有数据集比较，
`.timeframe` 只与 **ohlcv** 比较（funding/OI/basis 的结算是数据属性，实测 1h/4h/8h
混合且同合约会变，I34；拿 bar 频率要求它们会制造假失败）。manifest 读不到 →
`E4001`（别名指向不存在的版本）；manifest 未记录交易所 → `E4002`（来源不可确认）。

**标的集为什么比摘要而不是成员清单**：`Pin` 的九字段是冻结契约，只有 `symbols_digest`，
没有成员清单（多一份清单就是第二个真相，且会随重采漂移）。因此计划声明的是
**标的集名**（`supply.symbols` 是 `str`），编译期由 Charter 的标的集表
`schemas/symbol_sets.json`（`{名字: [symbol 列表]}`）解析成成员清单，再算规范摘要
与 `pin.symbols_digest` 比较。摘要相等比「子集」更严：少一个标的也会失败，不会
出现「拿 19 个标的数据跑声明 20 个标的的研究」。解析不了（表缺失/名字未登记）即
`E4002`，不猜、不回落默认。

**新鲜度为什么没有墙钟分支**：`plan_schema` 的 `supply` 块没有 `max_age_days` 键，
编译器也不接收时钟（F1）。曾经的 `max_age_days` 分支是**不可达代码**，已删除——
留着它会让人以为「数据过期」可配置，实际上永远不生效。数据是否够新，由
`acquired_at ≥ coverage.end` 与「覆盖区间 ⊇ 研究区间」共同保证：重采到更晚的末端
必然产生 `acquired_at` 更晚的新版本。

**覆盖 ⊇ 研究区间是正常的**，不做「末端不得晚于时间轴终点」的反向检查——那会把
「数据充足」误判成失败。

**明确不做**：不补采、不降级用旧版本、不把时间轴自动缩短到数据末端。三者任一都会让「我研究的是我声明的那个问题」失效。

---

## 9. 运行时的时序

| 时点 | 动作 | W1 | W2 | W3 | W4 |
|---|---|---|---|---|---|
| T0 | 维护链：采集、门禁 A、原子发布、移动别名 | 读 | **写（唯一）** | — | 写 |
| T1 | 编译：合并、解析名字、固化 pin、校验、算指纹 | 读 | 读 | — | — |
| T2 | 编译失败 | — | — | **不创建** | 清理 |
| T3 | 开 Ticket：写冻结方案、环境快照、初始索引 | — | 读 | 写 | — |
| T4 | 八工位执行 | — | **只读** | 各写自己目录 | 写 |
| T5 | 单步观察 | — | 只读 | 写 `steps/` | 写 |
| T6 | 归档：报告、可复现清单、转只读 | — | 读 | 写后冻结 | 清理 |

T1 与 T4 的分离是整套设计的支点：**编译之后，共享仓对执行器就是只读的。**

---

## 10. S0 验收点（本篇）

| # | 断言 |
|---|---|
| R1 | 计划里写未知路径 → `E1001`，提示最近似合法路径 |
| R2 | 名字拼错 → `E1002`，不回落默认 |
| R3 | 参数多写一个字段 → `E1003`，不静默忽略 |
| R4 | 覆盖层改 `target.horizon` → `E1005`，退出码 2 |
| R5 | 基线 `verdict.params.dsr_min` 为 `null` 且计划未填 → `E1008` |
| R6 | 别名指向不存在版本 → `E4001`，**且 Ticket 目录未创建** |
| R7 | 数据末端未闭合 → `E4003`，且未触发任何采集 |
| R8 | 编译函数被调用两次（同输入）→ 除 `compiled_at` 外结果逐位相同 |
| R9 | 执行器尝试写 `Depot/` → `E5004`（接口不存在，静态检查即报） |
| R10 | 两次写同一 `parcel_digest` 但内容不同 → 拒绝覆盖，`E5004` |
| R11 | `flow explain` 输出的来源表叶子数等于 `values` 叶子数 |
| R12 | C1–C22 每条有独立用例，且返回全部错误而非第一个 |
| R13 | 凭据环境变量缺失 → `E5002`，不降级 |
| R14 | 环境摘要变化 → `env_mismatch=true`；产物指纹随之变化时追加 `reproduction_failed=true`，两者均不判失败 |
