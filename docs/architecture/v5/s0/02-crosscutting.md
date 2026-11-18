# S0 · 02 跨层设施

> 这一层不属于任何工位，但每一层都要用。它的设计目标只有一个：**让「同方案同种子两次执行结果一致」这句话可以被证明。**
> 只要有一层能自己取时钟、自己取随机数、自己写日志文件，这句话就永远只是口头承诺。

---

## 1. 七个设施与一条总则

| 设施 | 一句话职责 | 被谁用 |
|---|---|---|
| `log` | 结构化日志，绑定工位目录 | 编排层、能力层、维护链 |
| `trace` | 每工位一条执行记录 | 编排层 |
| `clock` | 提供墙钟与单调钟；**能力层只拿到执行器注入的逻辑时点**，拿不到墙钟 | 编排层、运行时、维护链；能力层仅逻辑时点 |
| `rng` | 按名字派生确定性随机源 | 编排层、能力层 |
| `digest` | 规范化与指纹计算 | 全部 |
| `cache` | 以 `call_digest` 为键的产物复用 | 编排层 |
| `notify` | 执行结束后的外部通知 | 编排层 |

`clock` 这一行是唯一有例外的一个，理由值得写清：墙钟会让「同输入不同输出」，因此能力层不得读；但能力层需要知道「现在是哪根 bar」，那是**从数据末端推出的逻辑时点**，由执行器作为调用单的一部分注入，不来自系统时钟。把两者混成一个 `clock`，就会出现「我在能力里 `now()` 一下」这种既方便又致命的写法。

**总则：全部依赖注入，构造一次，逐层向下传。**

```
Facilities = {
    log, trace, clock, rng, digest, cache, notify
}
build_facilities(scope, config) -> Facilities
```

三条硬性规则：

1. **禁止模块级获取**。不允许 `get_logger()`、`datetime.now()`、`random.random()`、`numpy.random.seed()` 这类全局入口。任何层若自行取得这些能力，等同于绕过注入（`E2007`）。
2. **禁止设施改变数值**。除 `rng` 与 `digest` 之外，任何设施的失败都不得改变计算结果。`log` 写失败要不要中断执行？——**要**，因为「跑完了但没有记录」等价于无法复现。
3. **strict 模式缺设施即失败**。与唯一裁判同级别：缺 `digest` 或 `rng` 时不得「先跑起来再说」。

`scope` 决定作用域与落盘位置：

| scope | 构造者 | 日志位置 | 缓存 | 通知 |
|---|---|---|---|---|
| `upstream` | 维护链 | `Depot/upkeep/` | 可用 | 可发 |
| `ticket` | 编排层（开 Ticket 后） | `Ticket/<id>/` | 可用 | 可发 |
| `station` | 执行器（每工位一个） | `Ticket/<id>/stations/<S>/log/` | 只读 | **禁止** |
| `probe` | 单步命令 | `Ticket/<id>/steps/<seq>-<Station>-<hash>/` | 只读 | **禁止** |

---

## 2. `log`

### 2.1 接口

```
logger = log.bind(scope, keys)
logger.event(level, event, message, **ctx)
logger.child(extra_keys) -> logger
logger.close()
```

**全局便捷访问器（F1 的显式例外）**：`install_log(logger)` 只在组合根（应用启动处）调用一次，把**已构造**的实例放进 `ContextVar`；其余各处 `get_log()` 只做只读解析，可用 `get_log().info(event, message, **ctx)` / `warn` / `error` 直接记过程日志，不必层层传 logger。退出前 `reset_log(token)` 还原上下文。三条边界不可破：

1. `get_log()` 不得自行构造、配置日志器；未 install 时抛 `E2007`（fail-closed），绝不返回 no-op 替身。
2. `clock/rng/digest` 不适用本例外，仍必须逐层注入（它们进数值与指纹）。
3. 便捷方法只省略 `level` 参数，凭据/结论词拦截与关联键校验完全不变。

| 参数 | 说明 |
|---|---|
| `scope` | 见上表；决定写哪个目录 |
| `keys` | 必填关联键，见 §2.3 |
| `level` | `debug` / `info` / `warn` / `error`，来自操作性参数 |

### 2.2 落盘与命名

日志文件名为 `<call_digest[:16]>.jsonl`，与产物同目录。

这里有一个必须解释的时序问题：**日志在产物生成之前就要写，此时 `parcel_digest` 还不存在。** 解决办法不是先写日志再补指纹，而是：

> 日志以 `call_digest` 命名（调用前已知），`parcel.json` 的 `log_ref` 指向它。因为 `parcel_digest` 含 `call_digest`，所以从任一产物都能唯一定位它的日志，反之亦然。

这条替代了「日志文件名含 `parcel_digest`」的朴素做法，避免了一个循环依赖：指纹需要日志路径，日志命名需要指纹。

### 2.3 关联键

| 键 | scope=upstream | scope=ticket | scope=station | scope=probe |
|---|---|---|---|---|
| `dataset` / `version` | 必填 | — | — | — |
| `ticket_id` | — | 必填 | 必填 | 必填 |
| `station` | — | 可选 | 必填 | 必填 |
| `call_digest` | — | 可选 | 必填 | 必填 |
| `seq` | — | 必填 | 必填 | 必填 |

缺任一必填键 → `E2003`。没有关联键的日志等于一条无法归属的噪声。

### 2.4 禁止

| 禁止 | 原因 | 检查 |
|---|---|---|
| 写凭据值、令牌、密钥 | 泄露，且产物无法安全归档 | 写入前对 ctx 做模式扫描，命中即 `E2004` |
| 写结论（`pass` / `reject` / 显著性判断） | 结论只能由 Sentinel 写 | 同上，字段白名单 |
| 写非本工位目录 | 归属串线，料包失效时无法判断日志可信度 | 路径由一个只接受 `station` 的写入器产生，无法传任意路径 |
| 改变退出码 | 日志是旁路，不是判定 | 接口不返回判定值 |
| 缓冲后丢弃 | 崩溃时最需要日志 | `close()` 前必须 flush；进程退出前强制 flush |

---

## 3. `trace`

每工位一条，写 `Ticket/<id>/trace.jsonl`，只追加。字段固定：

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | `int` | 单调递增，从 1 开始 |
| `station` / `station_revision` | `str` | |
| `impl` / `impl_version` | `str` | |
| `params_digest` | `Digest` | 参数摘要，不放全量参数 |
| `inputs` | `list[(str, Digest)]` | 上游指纹 |
| `call_digest` / `parcel_digest` | `Digest` | 前者失败时也必须有 |
| `status` | `enum` | `done` / `failed` / `skipped` |
| `origin` | `enum` | `run` / `step` / `commit` / `resume` / `cache` |
| `seconds` | `float` | 单调钟测得 |
| `error_code` | `str` \| `null` | 失败时必填 |
| `skip_reason` | `str` \| `null` | 跳过时必填 |

两条规则：

1. **失败也必须有一条 trace**。没有 trace 的失败等于「系统没留下任何发生过这件事的证据」。
2. **trace 不写结论、不写指标值**。它是执行事实的记录，不是报告。指标在 `Parcel.evidence` 与认证报告里。

---

## 4. `clock`

```
clock.wall()      -> timestamp   # UTC，仅用于记录
clock.monotonic() -> float       # 仅用于计时
clock.today()     -> date        # 仅用于维护链的交易日判定
```

| 用途 | 允许用哪个 |
|---|---|
| 记录 `started_at` / `acquired_at` / `created_at` | `wall` |
| 计算 `seconds` / 超时 | `monotonic` |
| 判断「数据是否足够新」 | **两个都不用**：用 `Pin.acquired_at` 与计划时间轴比对 |

第三条是最容易犯的错。用「现在」判断数据新鲜度，会让同一份计划在不同日期跑出不同结论，且这种漂移不会留下任何指纹变化。新鲜度判定必须写成「计划要求的时间轴」对「pin 记录的覆盖区间」的比较，是一个纯函数。

`clock` 在 S0 里还必须支持**冻结时钟**：测试与重放时给定固定时间，所有 `wall()` 返回同一值。这是让「记录字段不进指纹」可被验证的前提。

---

## 5. `rng`

### 5.1 派生规则

```
sub_seed(master_seed, scope_path) = int(sha256(f"{master_seed}:{scope_path}").hexdigest()[:16], 16)
```

`scope_path` 用冒号分隔的稳定标识，例如：

| 场景 | `scope_path` |
|---|---|
| 算法 `gp_symbolic` 在族 `cs_q4_a` 下 | `cs_q4_a:search:gp_symbolic` |
| 该算法的第 3 代第 7 个体 | `cs_q4_a:search:gp_symbolic:g3:i7` |
| 置换检验第 200 次 | `cs_q4_a:evidence:within_bar:perm200` |

规则：

1. **不含时间、不含进程 id、不含路径**。任何一处混入，复现立刻失效。
2. **叶子路径必须由语义位置构成**，不能由迭代顺序隐式产生。
3. 每个作用域返回**独立实例**，不得共享全局状态。

### 5.2 禁止

| 禁止 | 原因 |
|---|---|
| 调用全局 `random` / `numpy.random.*` | 全局状态跨调用污染，顺序一变结果全变 |
| 用 `seed(None)` 或从时间取种 | 不可复现 |
| 在多线程中共享同一实例 | 抽取顺序不确定 |
| 把 `seed` 写进 `params` 之外的地方 | 指纹会漏掉它 |

`Call.seed` 必须显式为整数或 `null`。要求种子却给了 `null` → `E1008`；不要求却给了整数 → `E1003`（多余字段）。

### 5.3 编排层的工位级种子：作用域是内容，不是 ticket

执行器给每个工位派生一个 sub_seed，`scope_path = <seed_scope>:<station>:<seq>`。这里最容易犯的错是把 `seed_scope` 取成 `ticket_id`：

`ticket_id` 的契约是 `<UTC 紧凑时间>-<plan_digest 前 8 位>`，**它含秒级时间戳**。拿它当作用域，等于每次运行都换一套随机流——AC39「同 seed 两次逐位一致」在单元测试里成立（同进程同 ticket），在 CLI 层完全不成立。实测同一份 `plan.json` 连跑三次拿到 **31 / 24 / 22** 个候选，`n_trials` 69/76/101，候选集互不相交。

因此：

- `seed_scope` 由组合根注入**内容指纹**（`plan_digest`，它含数据版本、不含时间/路径/耗时）；
- `master_seed` 取计划声明的 `search.seed`，不能硬编码常量——否则「改 seed」只改指纹、不改随机流，用户写进计划的 seed 形同虚设；
- 单步命令从 `frozen/context.json` 读回同一组 `master_seed` / `seed_scope`，否则「单步重放」是用另一套随机流重算一遍；
- `ticket_id` 仍可作为缺省作用域（测试与单步无冻结产物时），但那是降级路径，不是默认路径。

---

## 6. `digest`

```
digest.canonical(obj) -> bytes
digest.of(obj) -> Digest
digest.array_shape_digest(array_meta) -> Digest
```

三条要求：

| 要求 | 说明 |
|---|---|
| 纯函数 | 无状态、无 IO、无时钟、无随机 |
| 版本固定 | 实现变更即全系统指纹变更，属于最高级别的变更，必须显式升版并接受「全部历史产物失效」 |
| 跨进程一致 | 同一内容在不同进程、不同机器、不同 Python 版本下必须得到同一 bytes |

`digest` 是唯一「升级代价最大」的设施：它一变，所有历史结论的可比性中断。因此 S0 必须为它写三条性质的测试（确定性、顺序无关、敏感/不敏感），并在升级流程里单列。

---

## 7. `cache`

```
cache.get(call_digest) -> {payload_ref, payload_digest, produced_at} | None
cache.put(call_digest, payload_ref, payload_digest)
cache.invalidate(call_digest)

cache.get_node(node_digest) -> {payload_ref, payload_digest} | None
cache.put_node(node_digest, payload_ref, payload_digest)
```

**两类合法键，不允许第三种**：

| 类 | 键 | 粒度 | 谁写 |
|---|---|---|---|
| 调用级 | `call_digest` | 一次能力调用一份完整产物 | 执行器 |
| 节点级 | `node_digest = H(node_ast_digest, panel_digest, asof, operator_version)` | 表达式 AST 的一个子节点值 | `evaluate` 实现 |

节点级缓存补的是「`ts_std(close,20)` 在几百个候选里重复出现被重算几百遍」。补它的同时必须守住四条：

| 规则 | 内容 |
|---|---|
| 键必须含 `panel_digest` 与 `asof` | 只按 AST 存，换数据版本后会命中旧值，等于用旧数据算新结论 |
| `asof` 必须是编译期固化值 | 与 `Candidate.available_from` 同源；用墙钟做键会破坏 AC3 逐位一致 |
| 节点缓存**从不产 `Parcel`** | 它只加速实现内部，不进工位间依赖图 |
| 命中不影响计数 | 同 F6：只跳过计算，不跳过登记 |

**但节点级缓存默认关闭（`facilities.cache.node.enabled = false`）**，实测依据：

| 面板规模 | 重算一个表达式 | 落盘写+读一个节点 | 落盘是否划算 |
|---|---|---|---|
| 23.7k 行（当前 20 资产 × 1187 bar） | 7.1 ms 单算 / **0.2 ms 批量均摊** | 13.3 ms（parquet）/ 16.2 ms（sqlite） | **净亏 65 倍** |
| 250k 行 | 38.3 ms / **1.7 ms 批量均摊** | 同上 | **仍亏** |
| 4M 行 | 864.9 ms / **59.5 ms 批量均摊** | 同上 | **划算** |

盈亏线是「单表达式重算 > 13 ms」：250k 行时批量均摊 1.7 ms（仍远低于 13 ms），4M 行时 59.5 ms（已超过），交叉点在两者之间、量级约 1M 行且需在该规模重新实测。当前规模下，求值内核的**同批 CSE**（一次调用内的公共子表达式复用）已经覆盖了绝大部分收益；跨调用落盘只增加 I/O 与失效风险。面板扩大后重新实测再启用，不得以「先开着没坏处」为由默认开。

`node_digest` 必须由 `digest_svc` 计算，**不允许实现手工拼字符串**——手工拼的键会在字段增删时静默失效或静默碰撞。

其余规则两类通用：

| 规则 | 内容 |
|---|---|
| 键的构造 | `call_digest` 由执行器算，`node_digest` 由 `digest_svc` 算；**不允许自定义键或拼字符串** |
| 作用域 | 不跨 `policy_digest`（已含在 `call_digest` 内，天然隔离） |
| 命中 | 必须重新校验 `payload_digest` 与文件存在性；校验失败视为未命中并记 `warn` |
| 标记 | 调用级命中产物 `origin=cache`，保留原 `lineage` 与原 `call_digest`；节点级命中只计 `node_stats` |
| 权限 | `station` / `probe` 作用域**只读** |
| 不登记 | 命中只跳过计算，**不跳过**试验计数与事件登记（计数由编排层独立代写） |

最后一条是关键：缓存命中若同时跳过 Budget 登记，就等于「同一份计划跑第二次不计数」，与全局单调直接冲突。

---

## 8. `notify`

```
notify.emit(event, payload) -> None
```

| 规则 | 内容 |
|---|---|
| 时机 | 只在执行结束（`done` / `failed`）后，由编排层触发 |
| 作用域 | `station` / `probe` 禁止 |
| 失败 | 记录到 trace 的一个附加字段，**不改变退出码**，不改变结论 |
| 内容 | 只允许锚点指纹、状态、错误码、耗时；禁止放产物内容与凭据 |
| 超时 | 有硬超时；超时记为失败但不重试到无限 |

通知是唯一「允许失败但不影响结论」的设施，因为它不参与任何判定。这个例外必须显式写进代码注释与文档，否则会被当成普遍先例滥用。

---

## 9. 设施与五层的关系

| 层 | 能拿到 | 不能拿到 |
|---|---|---|
| 维护链 | 全部（scope=upstream）；`notify` 可发 | 不得写 Ticket 区 |
| 编排层 | 全部（scope=ticket / station） | 不得绕过 `digest` 自己算指纹 |
| 能力层 | `log`（station scope）、`rng`（叶子实例）、`digest` | **拿不到 `cache` / `notify` / `clock.wall`** |
| 流水线之外的工具 | `digest`、`log`（只读诊断通道） | 不得写任何执行目录 |

能力层拿不到 `cache` 是刻意的：能力若自己查缓存，就会出现「跳过计算但不留记录」的分支，而缓存命中必须由执行器统一标记 `origin`。

**接线状态（F1 收口）**：`clock` / `rng` / `log` / `digest` / `trace` / `notify` 六件已由组合根注入并被编排层实际调用——指纹经 `fx.digest.of`、每条 trace 经 `fx.trace.append` 后再落 `trace.jsonl`、告警统一由 `build_notifier()` 构造（F7 的 3600s/10 条只有这一处定义）。

`cache` 是第七件，也是唯一**默认不生效**的一件：`Executor(cache_stations=...)` 必须显式列出允许复用 payload 的工位。原因是能力调用除了 payload 还产出内存对象（掩码、候选集、面板），命中时无法从 payload 复原；无差别开启会让下游拿到空对象表。开启后命中仍走落盘、计数与 trace，`origin` 被标成 `cache`（F6），文件/内容指纹双重复验（F5）。节点级缓存仍默认关闭（§7 的 65× 实测结论不变）。

---

## 10. S0 验收点（本篇）

| # | 断言 |
|---|---|
| F1 | 任一模块 `import` 全局 `time` / `random` / `logging` 作为业务入口 → 静态检查报 `E2007` |
| F2 | strict 模式缺任意设施 → 启动失败，退出码 2 |
| F3 | 日志 ctx 含 `token` / `secret` / `key` 字段 → `E2004`，文件内不落原文 |
| F4 | 同一 `master_seed` + 同一 `scope_path` 两次派生相同；改一个字符则不同 |
| F5 | `cache.get` 命中但文件被删 → 视为未命中，不抛错、不返回空产物 |
| F6 | 缓存命中后，Budget 计数仍为 1（未重复、未跳过） |
| F7 | `notify` 超时 → 退出码不变、结论不变、trace 有记录 |
| F8 | 冻结时钟下两次执行，所有记录时间字段相同，所有指纹相同 |
| F9 | 日志文件名可由 `call_digest` 反推，产物可经 `log_ref` 定位到日志 |
| F10 | `station` 作用域尝试 `notify.emit` → `E2001`（越权），不是静默忽略 |
