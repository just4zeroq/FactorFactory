# S0 · 04 能力层

> 能力层是「给什么算什么」的一组服务。它的契约里最重要的部分不是能算什么，而是**拿不到什么**。
> S0 阶段全部实现体只做两件事：校验参数、按 schema 校验返回值，然后抛「未实现」。这让契约先于算法成立。

---

## 1. 统一调用协议

```
run(call: Call) -> Result
```

| 阶段 | 动作 | 失败 |
|---|---|---|
| 1 | 校验 `call.call_digest` 与自身重算一致 | `E2002` |
| 2 | 按 `param_schema` 校验 `call.params` | `E1003` / `E1004` / `E1008` |
| 3 | 校验 `call.inputs` 数量、顺序、`kind` 与本能力声明一致 | `E3001` |
| 3.5 | 校验子调用：只能是 `composes` 里声明过的、深度 ≤ 2、共享父 `policy_view` | `E2008` / `E1020` |
| 4 | 执行实现体 | `E3004` |
| 5 | 按 `outputs_schema` 校验返回值 | `E3003` |
| 6 | 返回，**不落盘** | — |

第 3.5 步是后补的，因为「子调用自建」与「子调用自带口径」两种越界都不在第 1–3 步的校验范围内：
声明外能力、深度 3、父子口径不一致，三者没有一个会在参数或输入校验里暴露（`E2001` 管的是
`policy_view` 里出现未声明子块）。补这一步之后，`composes` 才真的是一条**可执法的边界**，
而不是一段描述。

第 3 条是「输入顺序即契约」的执行方式：`inputs[i]` 只对应声明第 `i` 项。
中间的可选输入必须用 `None` 占位（`backtest` 的 `funding` 就是中间可选），
省略它会让 `tiers` 前移到 `funding` 的位置；两个 `kind` 相同时就是静默错位绑定。

指纹侧另有两处细化：`call_digest` 除六通道外，还纳入 `capability` 与
每个输入的 `kind`。少了 `capability`，`evaluate@qweave@1` 与 `align@qweave@1`
会共享同一个缓存键；少了 `kind`，把输入角色从 `payload` 改成 `panel` 指纹不变，
`E3001` 就只剩运行期瞬时成立。

第 3.5 步的判据是**整条调用链声明过的子块并集**，不是只看父：`observe` 声明
`execution/net`、`allocate` 声明 `allocate`，而 §1.3 强制父子共享同一 `policy_view`，
只按父判会让合法组合无法成立。没人声明过的子块出现在 `policy_view` 里，才是
被偷塞进来的。残留风险（子实现读兄弟声明的子块）无法在本层消除，只能靠 C5 式静态检查。

第 6 条是分工的关键：**能力只返回，不写盘。** 落盘、写 trace、写日志摘要、登记事件，全部由执行器完成。能力一旦能写盘，就有了「先写产物再算指纹」和「写别的工位目录」两条后门。

### 1.1 能力的元信息

```
describe() -> CapabilitySpec
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` / `version` | `str` | 与目录一致 |
| `param_schema` | `map` | 见 §2 |
| `inputs_schema` | `list[{kind, schema_id, required}]` | 期望的上游引用 |
| `outputs_schema_id` | `str` | 返回值结构 |
| `deterministic` | `bool` | 同输入同种子是否可复现 |
| `needs_seed` | `bool` | 是否需要种子 |
| `stateful` | `bool` | 是否需 `fit` / `transform` 分离 |
| `cacheable` | `bool` | 产物能否复用 |
| `side_effect` | `enum` | `pure` / `repeatable` / `append_only` |
| `composes` | `list[str]` | 本实现会调用的子能力名，编译期据此构造子调用图 |
| `policy_reads` | `list[str]` | 会读 `caliber` 的哪些子块（`execution` / `label` / `allocate` / `net`），编译期据此组装 `policy_view` |
| `forbidden` | `list[str]` | 显式声明不得触碰的设施，用于静态检查 |

`describe()` 必须**纯静态**：不读数据、不读环境、不取时钟。编译期要在没有数据的机器上拿到它。

### 1.1.1 参数 schema 落在哪

`param_schema` / `inputs_schema` / `outputs_schema` 不是实现里的判断，而是**契约数据**：

| 项 | 位置 | 谁读 |
|---|---|---|
| 三份 schema | `capability/registry/params.json` | 唯一加载器 `capability/params.py` |
| 调用协议 | `capability/runtime.py` 的 `run(call)` | 执行器与单步命令 |

三条规则与 `11-field-operator-registry.md` §4.1 同构：

1. **JSON 是真相**。实现里不得再写一份「必填哪些键」——两份规则必然漂移，而漂移时
   通常只有一份被测试覆盖。
2. **加载即自检**（`E2003`）：缺 `params`/`inputs`/`outputs`、未知类型、枚举无取值、
   `list` 缺 `item`、`map` 既无 `keys` 又无 `item`，任何一条不满足即整体失败。
   跳过一条等于把它从契约里悄悄删掉。
3. **多余字段一律 `E1003`**（`additional_properties=false` 的等价物）。`verdict.params.n_trials`、
   `certify.params.holdout` 属于这一类：不是「配了没用」，而是**配了就错**（C16）。
4. **返回结构对称执法**：`outputs` 里多一个字段同样是 `E3003`。多带一个字段就是给下游
   一条未声明的通道（结论、指标、排序都可能藏在那里）。核心产物（掩码、权重、换手）
   用 `array` 标记，字符串或标量不得冒充数组；`float` 通道拒绝 NaN / Infinity——
   否则「NaN 一路传到 `rank()` 变成噪声排名」这条 V4 病理会在 V5 重演。
5. **契约数据在进程内不可变**：加载器返回深拷贝。`schemas()["verdict"]["params"]["n_trials"] = …`
   曾能让 C16 永久失效且没有任何报错，一条赋值废掉一条验收。
6. **按参数分档的返回**（`outputs_by`）：`backtest` 的 `summary` 与 `full` 字段不同。
   只声明 summary 档会让 `detail=full` 缺 `ledger_ref` 也算成功，而「两档同源」正是
   §3.11 的核心要求，缺档必须 `E3003`。

### 1.2 能力能拿到什么

| 允许 | 禁止 |
|---|---|
| `call.params`（已校验、已全量展开） | 配置文件对象、基线、计划 |
| `call.inputs`（只读引用） | 存储写句柄 |
| `call.policy_view`（**只含声明过的口径子块**，只读） | 完整配置树、其他口径子块 |
| `call.runtime_facts`（计数快照、揭示状态、审核状态） | 账本句柄、计数器写入口、事件触发 |
| `call.composed`（已构造好的子调用单） | 自建调用单、临时解析实现名 |
| 执行器解析后的只读数据数组 | 别名解析入口、「最新」版本 |
| `log`（station scope） | `cache`、`notify`、`clock.wall` |
| `rng`（按作用域派生的叶子实例） | 全局随机源 |
| `digest` | 判据、结论出口 |

这份清单是 `forbidden` 字段的实际内容，也是 S0 静态检查的依据。

三条解释，因为它们最容易被实现者试探：

- **`policy_view` 不是配置入口**。它是编译器按 `describe().policy_reads` 组装的一个只读映射，只含本能力声明会读的子块。想读没声明的子块 → 编译期就组装不出来，运行期 `E2001`。
- **`runtime_facts` 是快照不是通道**。它由执行器在构造调用单时一次性填入，能力不能回写、不能再读第二遍。计数进这里而不是进 `params`，是因为参数可以被配置改小。
- **`clock` 的不对称是有意的**：能力拿不到 `clock.wall`（墙钟会让同输入不同输出），但能拿到执行器注入的**逻辑时点**（用于日志与进度）。`clock.wall` 只有执行器与维护链可读。

### 1.3 子调用协议

有些能力必须调用别的能力：证据要调构造与成本，组合要调合成、构造、成本，认证要调成本与构造。这不是依赖混乱，而是同一份口径被多处使用的正常形态。

```
run(call)  →  实现内部：run(call.composed[i])   # 只能执行预构造好的子调用
```

| 规则 | 强制点 | 违反 |
|---|---|---|
| `describe().composes` 必须列出全部子能力 | 编译期解析子调用图 | 未声明即调用 → `E2008` |
| 子调用单由编译器构造，不是实现构造 | `call.composed` 是唯一入口 | 实现尝试自行构造 → `E2001` |
| 组合深度 ≤ 2，`backtest` / `evaluate` / `read` 是叶子 | 编译期遍历图 | 深度 3 → `E2008` |
| 子调用失败原样上抛，不换码 | 错误对象透传 | 底层 `E4xxx` 被包装成 `E3xxx` → 契约违反 |
| 子调用的 `call_digest` 进父 `call_digest` | 指纹计算 | 换成本实现而父产物命中旧缓存 |
| 子调用**共享同一 `policy_view`** | 编译器按父调用的口径组装 | 父子读不同口径 → `E1020` |

最后一条是这套协议存在的核心理由：**同一工位内的所有子调用必须读同一份口径**。若子调用可以自带口径，「证据层用便宜成本判可交易、组合层用昂贵成本回测」就会在同一个工位内部发生，比跨工位更难发现。

`allocate` 与 `backtest` 被三个工位（证据、组合、认证）复用，且参数全部来自 `caliber`。因此它们**不接受工位级参数覆盖**：想改构造方式就是改 `caliber`，即新实验。

---

## 2. 参数 schema 类型系统

| 类型 | 约束字段 | 说明 |
|---|---|---|
| `int` / `float` | `range: [lo, hi]` | 闭区间；浮点另可给 `multiple_of` |
| `bool` | — | 不接受 `0` / `"false"` |
| `str` | `pattern`, `max_len` | |
| `enum` | `values: [...]` | 值必须是字符串 |
| `list[T]` | `min_len`, `max_len` | 元素类型递归校验 |
| `map[str, T]` | — | 键必须是字符串，值递归 |
| `duration` | `unit: bar\|day` | 与频率绑定 |
| `timestamp` | — | UTC RFC3339 |
| `ref` | `kind` | 引用类型，不接受内联内容 |

每个字段必须声明四项：

| 属性 | 取值 | 作用 |
|---|---|---|
| `type` | 见上 | 类型校验 |
| `required` | `true` / `false` | 缺失是否失败 |
| `class` | `constitutive` / `operational` | 是否进 `policy_digest` |
| `default` | 值或省略 | **只在编译期展开**；运行期不存在「取不到就用默认」 |

三条规则：

1. `class=constitutive` 且 `required=false` 且给 `default` 是**允许**的——但默认值必须在编译期写入 `values` 与 `origins`，运行期看到的永远是具体值。
2. 不允许 `type=any`、不允许 `additional_properties=true`。多余字段一律 `E1003`。
3. schema 本身有版本，改 schema（增删字段、改范围）等于改契约，须随工位 `revision` 显式升版。

---

## 3. 十二项能力

每项给出：参数、返回、前置、失败、副作用。**S0 只实现参数校验与返回 schema 校验，实现体抛 `E3004`。**

### 3.1 `read` · 事实读取

| 项 | 内容 |
|---|---|
| 服务工位 | Panel |
| 名字空间 | 单实现 |
| 参数 | `datasets: list[enum]`、`fields: map[dataset, list[str]]`、`frequency: enum`、`window: {start, end}`、`symbols: ref` |
| 输入 | 已解析的 `SupplyBatch`（`kind=supply`）：`Pin` 版本事实 + 长表列 + `source_ref` |
| 返回 | `FieldStream`：`{stream_refs: list[Ref], field_manifest: map[field, {dataset, version, unit, available_lag}], coverage: {start, end, bars, symbols}, missing_ranges: list[{symbol, start, end, bars}]}` |
| 前置 | `window` ⊆ `pin.coverage`；`datasets` 与 `supply` 输入一一对应；`symbols` 等于供给版本记录的标的集名 |
| 失败 | 版本缺失 `E4001`；字段不在已发布版本覆盖内 `E1014`；`window` 超覆盖或步长与 `frequency` 不符、标的集不符 `E4002`；数据集与输入不对应 `E3001`；同一 `(标的, 时点)` 出现两次 `E4007`；列值非数值 `E3003` |
| 副作用 | `pure`，可缓存 |

`read` **只读**已发布版本。它没有任何「下载」「补齐」「最新」参数——这不是遗漏，是设计。

#### 输入为什么是 `SupplyBatch` 而不是 `Pin`

`Pin` 只说「读哪个版本」，不说「内容在哪」；而**打开文件这件事必须留在事实层**——能力层不得 import `pyarrow` / `pathlib`（AC31），否则「只读 handle」的边界就破了，`C20` 的结构保证随之失效。

所以执行器在解析 `supply` Ref 时做三件事：读已发布 parquet、重算 payload 指纹并比对 `pin.checksum`、把列转成 Python 序列，然后构造 `SupplyBatch(pin, symbol_set, source_ref, columns)` 传进来。`read` 因此既没有路径、也没有版本发现能力，它只能读「已经交到手上的那一个版本」。

`source_ref.digest` 与 `pin.checksum` 必须一致（`E4004`）：一条指路径、一条指版本，两者指向不同内容时，读到的可能已经是另一个版本，而两边都不会报错。

#### 窗口、频率与字段清单

- **窗口超出覆盖即失败**，不取交集。取交集会让「读一段不存在的历史」悄悄变成「读一段更短的历史」。
- **`frequency` 必须与时间轴的实测步长（中位数）一致**。频率不是装饰性参数：把 `1d` 当 `1h` 用，`tier_window=20` 的回看会从 20 天变成 20 小时，而没有任何一处会报错。
- **字段清单是三者取交集**：注册表登记（`11-field-operator-registry.md`）× 该字段声明的数据集 × 已发布版本的列。任何一处不满足即 `E1014`。取并集的话，缺失要到 `align` 甚至 `evaluate` 才炸，那时离现场已经很远。
- **`field_manifest` 的 `available_lag` 只从注册表取**，不从载荷取：载荷能改写 lag 就等于把前视藏进数据层。`align` 的 P2 检查比的正是这两个值。
- **`missing_ranges` 只报内部缺口**：首末有效观测之间、所有请求字段同时缺失的连续行。上市前与退市后的空段不是「缺数据」，那是标的不存在（`boundary` 用 `not_listed` / `delisted` 表达）。把结构性空段也报成缺口，读者会在这个信号上学会忽略它。

### 3.2 `boundary` · 边界构建

| 项 | 内容 |
|---|---|
| 服务工位 | Boundary |
| 参数 | `k: int`、`tier_window: duration`、`tier_edges: list[float]`、`min_coverage: float`、`time_axis: {mine, oos, holdout}`、`mask_policy: enum` |
| 返回 | `Scope`：`{observable_mask, tradable_mask, tiers, reason_codes, reason_code_legend, axis_resolved}` |
| 前置 | 交易额字段存在且覆盖时间轴 |
| 失败 | `k` 超可选标的数 `E1004`；时间轴与覆盖不符 `E4002` |
| 副作用 | `repeatable`，可缓存 |

两张掩码必须分开返回：`observable_mask` 供统计与中性化，`tradable_mask` 供权重约束。`reason_codes` 逐格给出排除理由（`not_listed` / `delisted` / `low_liquidity` / `missing_data` / `unclosed` / `reserved` / `stable_or_leveraged`）。

#### 时间轴：字符串怎么解析

`time_axis` 的每个值是 `18-plan-schema.md` 的 `interval` 字面量：`<YYYY-MM-DD>..<YYYY-MM-DD>`（UTC 日期，闭区间）或 `reserved`。能力层把它解析成**半开索引区间** `[start, end)`：

- `start` = 时间索引上第一个 `ts >= start_day 00:00:00Z` 的时点（早于数据首个时点则取首个）；
- `end` = 时间索引上第一个 `ts > end_day 23:59:59.999999999Z` 的时点（超出则取末端）；
- 解析后 `start >= end` → `E4002`（不是「空区间跑出空结果」）；不与供给覆盖相交同样 `E4002`。

`holdout` 为 `reserved` 时**不解析**：`axis_resolved.holdout` 保持字符串 `"reserved"`，保留区在掩码上的含义是「`mine ∪ oos` 的补集」。声明与数据分开（`05-orchestration.md` §1 O18、§3.1）：Boundary 只需要知道「哪些时点被保留」，不需要看到它们的值，所以它按补集标 `reserved`，而不是先去问保留区在哪里。

**`mine` 与 `oos` 之间不得有洞。** 两段之间若跳过一段（`mine` 到 01-10、`oos` 从 01-15 起），`align` 会把洞两侧按索引拼成相邻两行，而 `forward_return` / `trailing_mean` / `rolling_beta` 全都按「相邻一根」解释——`mine` 最后一根的标签会直接用上 `oos` 的价格。被跳过的区间只有一个合法去处：声明为 `holdout`（那样它就不该出现在面板里）。违反即 `E4002`。

`axis_resolved` 恒为：

```
{mine: {start: <int ns>, end: <int ns>},
 oos:  {start: <int ns>, end: <int ns>},        # 未声明时 end = start（空区间）
 holdout: "reserved" | {start: <int ns>, end: <int ns>}}
```

`start`/`end` 是**时间轴上的实际时间戳**，不是索引——索引换一份数据就变，时间戳不会。它们是**闭区间**的首末时点（与 `coverage` 的语义一致）：`end` 取该区间最后一根 bar 的时戳，不是「次日零点」。

#### 理由码：一格只有一个理由

`reason_codes` 是 `(T,N)` `int8`，`0` 表示纳入；`reason_code_legend` 给出「码名 → 码值」映射。一格可能同时满足多条排除条件，因此必须定优先级（**先命中先记**），否则同一份数据两次运行会得到不同码值：

| 码 | 名称 | 判据 | observable | tradable |
|---|---|---|---|---|
| 0 | `valid` | — | ✓ | ✓ |
| 1 | `reserved` | 时点在 `mine ∪ oos` 之外 | ✗ | ✗ |
| 2 | `not_listed` | 该标的在该时点及之前从未有有效值 | ✗ | ✗ |
| 3 | `delisted` | 该标的在该时点及之后再无有效值 | ✗ | ✗ |
| 4 | `missing_data` | 必需字段在该格为 NaN | ✗ | ✗ |
| 5 | `unclosed` | 末根 bar 未收盘（`pin.end_closed=false`） | ✓ | ✗ |
| 6 | `low_liquidity` | trailing `tier_window` 的报价成交额排名在 `k` 之外 | ✓ | ✗ |
| 7 | `stable_or_leveraged` | 标的是稳定币或杠杆代币 | ✓ | ✗ |

**优先级与码值不是同一个序。** 码值是稳定标识（改码值等于让已发布产物失效），
优先级是判据求值序。实际执行序为：`reserved` → `not_listed` → `delisted` →
`stable_or_leveraged` → `missing_data` → `unclosed` → `low_liquidity`。
标的结构性原因排在数据质量之前：一个稳定币被标成「流动性不足」，读者会去查
成交量；被标成「稳定币」才会去看标的集。

未收盘 bar 的收盘价是半成品：它**能被观测**（价格正在形成），**不能用来下单**（成交价还不存在）。这正是两张掩码必须分开的原因——合成一张就只能二选一，两种做法都错。

#### `tiers`：流动性分层

`tiers` 是 `(T,N)` `int8`，`-1` 表示未入池，`0..len(tier_edges)` 表示由低到高的档位。分档量是**报价成交额**（`close * volume`）在 trailing `tier_window` 上的均值：

- `mask_policy=pit_dynamic`：逐时点重算均值与分位，档位随时间变化；
- `mask_policy=static`：全期一次，用整个 `mine ∪ oos` 区间的均值。

`tier_edges` 是分位点（升序、`0 < e < 1`），未给出时默认等分三档 `[1/3, 2/3]`。分位点必须在**该时点入池成员**上取，不是在全体标的上取——否则「池子变小」会让档位整体上移，而档位同时被用于分层回测与组合约束。

#### `min_coverage`：回看覆盖率，不是全期覆盖率

`min_coverage` 判的是「最近 `tier_window` 根里有多少根该标的有价」，分母是**实际回看的根数**：

- 分母是 `min(t+1, tier_window)`，不是「窗口内有效计数」。用有效计数会让一个孤立样本的覆盖率恒为 1.0，门禁永不触发——它测的就变成「已有数据里有没有缺」，而缺的正是那些本该存在、却根本不在数组里的根。
- 它必须是**回看**的。用整个运行区间的覆盖率决定逐格可用性，等于让一根 bar 能不能用由它**之后**的缺口决定：一个只在后半段出现过的标的，它前半段本有效的格会被整列抹掉。这是本层唯一一处容易混进来的前视。
- 低于阈值的格**不入池**：退出 top-k、退出分层（`tiers=-1`）、理由码记 `missing_data`。只标「不可观测」而不退出排名，会让一个半空的列继续占掉名额，把真正可交易的标的挤出去。

### 3.3 `align` · 面板对齐

| 项 | 内容 |
|---|---|
| 服务工位 | Panel |
| 参数 | `drop_unclosed: bool`、`causal_probe: enum{none, strict}`、`quality_gates: {max_missing_ratio, min_obs}` |
| 返回 | `Panel`：`{panel_ref, shape, time_index_ref, field_manifest, causal_report, quality_report, holdout_visibility}` |
| 前置 | `FieldStream` 与 `Scope` 就位 |
| 失败 | 因果探针发现未来信息 / 时间轴有洞 `E3004`；`min_obs` 不满足、`field_lags` 与注册表不一致、面板含保留区取值 `E3003` |
| 副作用 | `repeatable`，可缓存 |

缺失用空值表示，**不做静默前向填充**。`causal_probe` 控制的是**检查强度**，不是「跑一个探针算法」：

| 取值 | 执行 |
|---|---|
| `none` | 索引单调、保留区不进入面板 |
| `strict` | 以上 + 逐字段逐位比对（面板必须与流在保留行上的字节完全一致）+ 时间连续性（相邻行的间隔不得超出正常步长） |

原设计写的是「打乱未来值、看历史统计是否变化」。这个探测在纯数组上恒通过——它测的是 `trailing_mean` 自己（一个已经实现为因果的算子），而不是数据；上游 `shift(-1)` 过的数组照样通过。一个永远不会失败的检查比没有检查更糟：它会在报告里以 `passed=true` 出现。现在的三项都能真失败，其中「连续性」是洞（`mine` 与 `oos` 之间被跳过一段）的唯一可查证据。

两份报告此前只有名字、没有结构，等于把「面板可不可信」交给实现自由发挥。这里定死字段（缺字段即 `E3003`）：

**`causal_report`**

| 字段 | 类型 | 含义 |
|---|---|---|
| `checks` | `list[{id, scope, passed, offending}]` | 逐条因果检查；`id` 取 P1–P5（见下），`passed` 由结果计算，`offending` 给字段名与时间位置 |
| `holdout_boundary` | `{start: int?, end: int?}` | 保留窗口边界；`reserved` 时两值为 `null`（面板就是 `mine ∪ oos`，保留区不在其中）。`Panel` 不得包含其内部的任何取值 |
| `probe` | `{mode, fields, method, max_shift_used}` | `causal_probe` 的具体做法；`max_shift_used` 必须 `>= 0`（负值即前视） |
| `violations` | `list[{field, at, reason}]` | 空列表表示无违反；非空且 `mode=strict` 时整步失败 |
| `field_lags` | `map[str, int]` | 实际生效的 `available_lag`，必须与 `11-field-operator-registry.md` 逐项相等 |

面板因果检查的五个编号（`C1–C22` 是编译期配置校验，没有面板项，不要混用）：

| # | 范围 | 判据 |
|---|---|---|
| P1 | 时间索引 | 严格递增无重复（`Panel` 构造时已挡，这里给读者一个可核对的结论） |
| P2 | 字段滞后 | `field_lags` 逐项等于注册表；不一致即 `E3003` |
| P3 | 保留区 | 面板不含保留区时点；含即 `E3003` |
| P4 | 逐位一致 | 面板取值与流在保留行上的取值逐字节相同（`strict` 才跑） |
| P5 | 时间连续 | 相邻行间隔不超过正常步长（`strict` 才跑）；有洞即 `E3004` |

`field_lags` 与注册表不一致是最容易被忽略的一类前视：注册表写 `available_lag=1`、实现按 0 对齐，两边都不报错，但因子已经用上了当根未收盘的价。

**`quality_report`**

| 字段 | 类型 | 含义 |
|---|---|---|
| `bars` / `symbols` / `fields` | `int` | 必须与 `shape` 逐项相等 |
| `missing_ratio` | `map[str, float]` | 每字段缺失比例；超 `max_missing_ratio` 即 `E3003` |
| `min_obs_per_symbol` | `map[str, int]` | 低于 `min_obs` 的标的必须逐个列出，不许只报一个总数 |
| `min_obs_basis` | `str` | 有效观测数按哪个字段算（优先 `close`，无价格字段时取字典序第一个）。报告里写清判据字段，否则「该标的有多少观测」在不同面板上是两个量 |
| `unclosed_dropped` | `{count, last_ts}` | 丢弃的未收盘 bar；`count>0` 时 `last_ts` 必须非空 |
| `duplicate_index` | `int` | 重复时间戳数，必须为 `0` |
| `tz` / `tz_source` | `str` | `tz` 必须 `UTC`；`tz_source` 写清这是构造契约（`int64_utc_index`）还是上游校验结果——时间轴是整数纳秒，本身没有时区，把「未校验」写成「已校验」等于伪造门禁 |

`unclosed_dropped` 是 V4 那根「本地末根 bar 未收盘却进了面板」的唯一可查证据，因此它不是统计量，是**门禁记录**：`count>0` 时运维要能从这一条反查是哪次采集留下的尾巴（`15-operations.md` §4-1）。

#### `align` 的四条实现约束

四件事都由 `align` 判定，任何一条不满足都整步失败——面板是后面所有工位的地基，没有「先跑跑看」：

1. **`field_lags` 只能来自注册表。** `causal_report.field_lags` 逐字段写实际生效的 `available_lag`，取值只允许等于 `registry/fields.json` 的值。不一致即 `E3003`（面板不可信），不是 `E1004`（参数错）——参数没错，是数据用错了滞后。
2. **`tz` 只能是 `UTC`。** 时间轴是整数纳秒，时区在进 `Panel` 之前就必须归一；`align` 不做换算（换算会让「同一时刻」出现两种表示，指纹随机器时区漂移），只声明构造契约，并在 `tz_source` 里写明这不是它算出来的检查结果。
3. **`unclosed_dropped` 是门禁记录。** `drop_unclosed=true` 且 `pin.end_closed=false` 时，末根 bar 被丢弃并记 `{count, last_ts}`；`count>0` 而 `last_ts` 为空即 `E3003`。未收盘却「记了 0 条」是同一类假成功，必须失败。
4. **`duplicate_index` 必须是 `0`。** 重复时间戳在 `Panel` 构造时已经 `E4007`；报告里这一项是给读者的自证，不是检查点——它恒为 0，出现非 0 说明有第二条构造路径绕过了值对象。

### 3.4 `label` · 标签生成

| 项 | 内容 |
|---|---|
| 服务工位 | Target |
| 参数 | `horizon: duration`、`rebalance: duration`、`entry: enum{next_open, next_close, close}`、`transform: enum`、`neutralise: list[enum]` |
| 返回 | `Label`：`{label_ref, score_ref, score, valid_mask, width_series, dropped_bars, overlap_policy_used, effective_n}` |
| 前置 | `Panel` 就位 |
| 失败 | 标签窗口触及保留区 `E1013`；`entry` / `horizon` / `rebalance` 与 `caliber` 不一致 `E1013`；`rebalance < horizon` 未声明重叠 `E1022`；宽度丢弃超 20% `E3003` |
| 副作用 | `pure`，可缓存 |

`entry` 必须与成本能力读到的入场价口径**同一个值**。`params` 里的 `entry` / `horizon` / `rebalance` / `transform` / `neutralise` / `overlap_policy` / `min_width` / `width_policy` 是编译期从 `caliber` 展开的快照（它们要进 `call_digest`），运行期与 `policy_view` 逐项比对，任何一项不等即 `E1013`。这是 C9 在能力层的落点：不是「以后者为准」，而是分叉时停下。`neutralise_window` 不进 `params`——它只从 `caliber.label` 读。

三个输出容易被写成同一件事，必须分开：

- `label_ref` 指向**毛标签**：`forward_return` 加中性化后的残差，未做截面变换、未做重叠抽样；
- `score_ref` 指向**分数**：变换 + 抽样掩码之后的数组；
- `score` 是 `score_ref` 的内容本身，`valid_mask` 是可消费时点的掩码。

如果两个 `ref` 相等，下游就没有任何办法复核 06 §4 的变换定义——它只能看到已经变换过的结果，而「变换是不是按定义做的」正是最需要被复核的一步。

### 3.5 `search` · 候选生成

| 项 | 内容 |
|---|---|
| 服务工位 | Search |
| 名字空间 | **开放**（唯一） |
| 参数 | 由算法 `param_schema` 决定；另有 `prior_pool: list[ref]`、`max_candidates: int` |
| 返回 | `CandidateSet`（见 §4.4） |
| 前置 | `Label` / `Panel` 就位；`MarketView` 已构造 |
| 失败 | 见 C13–C17；超配额 `E1017`；沙箱不通过整批 `E1015` |
| 副作用 | `append_only`（由执行器代写 Budget） |

### 3.6 `observe` · 证据采集

| 项 | 内容 |
|---|---|
| 服务工位 | Evidence |
| 参数 | `prefilter: {min_coverage, max_turnover, min_abs_ic}`、`null_model: enum`、`metrics: list[enum]`、`cost_attribution: bool`、`n_permutations: int`、`dedup: {corr, method, rep_by}`（可缺省）、`groups: int`（可缺省）、`ic_decay: list[int]`（可缺省，单位 bar） |
| 返回 | `ObservationSet`：`{per_candidate: list[{candidate_id, metrics, null_distribution, cost_attribution}], method_ids, redundancy}` |
| 前置 | `CandidateSet` 就位；成本能力可调用 |
| 失败 | `null_model` 不在白名单 `E1004`；候选为空 `E3003`；`dedup.corr` 越界 `E1004`；`groups` 越界或超过标的数 `E1004`；请求单调性却未给 `groups` `E1008`；`ic_decay` 非法或请求了却未给 `E1008` |
| 副作用 | `pure`，可缓存 |

`metrics` 里的每一项都必须带 `method_id`（算法 + 版本）。同名指标用不同算法算出来不是同一个量，没有 `method_id` 就无法审计。带阶段后缀的标签（`simple_longshort@S3`、`allocate+backtest@S4`、`combine_domain@S7`）由 `capability/spec.py::METHOD_STAGES` **唯一构造**，实现里不得再写字面量（G1/G2）。

这里的 `S3` / `S4` 是**方法版本标记**，不是「当前开发阶段」：`observe` 的 `turnover` / `gross_sharpe` 是 S3 定义的**毛**粗筛量（`est_cost=0`，绝不冒充净成本），所以保留 `@S3` 是正确的；真成本来自 `allocate+backtest@S4`。组合域在 S7 才成形，旧标签 `combine_domain@S4` 已改为 `@S7`——它是 `domain_digest` 的输入，这次改名是一条**指纹分界点**，历史域摘要与新域摘要不可直接比对。

**分层单调性（`groups` + `group_returns` / `monotonicity`）**。IC 是一个标量相关系数，测的是「整条截面排序的一致程度」；它对收益的**单调变换完全不敏感**——把最高分组的收益放大十倍，rank IC 一位不变。而持仓只吃两头、组内等权持有每一只，决定净收益的是**各组收益的水平差**，不是相关系数的大小。「上尾组 +5%、下尾组 −0.5%」与「上尾组 +0.5%、下尾组 −5%」可以是同一个 IC，但前者做多有效、后者做空有效，成本与容量也完全不同。`group_returns` 把这个形状摊开看，`monotonicity` 只是它的方向性摘要。

给出 `groups = G` 时，按 score 截面等分 G 组（按名次 `array_split`，同分不挤到同一侧），逐期取组内等权收益、跨期平均，输出：

| 字段 | 含义 |
|---|---|
| `group_returns` | 长度 G 的列表，**低分 → 高分**，单位是每 bar 毛收益（已除 `h`） |
| `monotonicity` | 组序（0..G−1）与组均收益序的 Spearman，`+1` 表示完全同向 |
| `group_periods` | 真正能分出 G 组的期数（某期有效标的 < G 时整期跳过，不降组数） |

三条口径必须与 `_longshort` 同源：同一个掩码 `valid ∩ label_valid`、同一个**变换前**的 `raw`（变换后 score 的单位是秩/标准差，当收益用就是量纲混拼）、同样除 `h`。**不冻结再平衡网格**——这里测的是「分数 → 收益」的截面映射，混进持仓惯性会让同一个因子在 `rebalance=5` 下显得"更单调"。

**`groups` 没有默认值**：3/5/10 组算出的单调性不是同一个量，默认一个数等于让同一份计划在两种口径下都"合法"。请求 `group_returns` / `monotonicity` 而未给 `groups` 直接 `E1008`。

`monotonicity` 是**诊断量**（06 §9），不进任何判据：拿它当通过条件等于凭空造第二套门槛，而门槛必须唯一。

**IC 衰减曲线（`ic_decay`）**。同一因子在 `h ∈ {1, 5, 10, 20}` 上的 IC：短跨度 IC 高但换手贵，长跨度省成本但信号钝——`h` 的取舍只能靠整条曲线定，靠单个 `h` 的 IC 定不了。给出 `ic_decay: list[int]`（**单位 bar**，不是 `duration`）后，输出 `metrics.ic_decay = {跨度: 平均 IC}`。

三处契约要点：

1. **多跨度标签由 `label` 产**（`build_raw_by_h`），`observe` 不另算一份未来收益。口径（entry / 中性化 / 保留区掩码）全部从 `policy_view` 读，与主标签同一个 `forward_raw`。可执行证明：口径跨度那一档与 `label.raw` **逐位相同**（有测试）。
2. **不做重叠抽样、不做宽度掩码**。衰减要在同一批时点上比较不同跨度——抽样步长随 `h` 变会让每条曲线的样本都不一样，曲线落差就分不清是持有期效应还是抽样噪声。代价是口径开了 `width_policy=drop` 时「h = 口径跨度」那一档与主标签不再逐位相同。
3. **每个跨度按自己的 `reach = offset + h` 重算保留区掩码**。`h=20` 的标签比 `h=1` 多读 19 根 bar，套用 `h=1` 的掩码会放过一批伸进保留区的标签。

**重复因子检测（独立性判重）**。内容身份（`candidate_id = H(规范化 AST)`）只能拦住"写法和身份都相同"的候选；`mean(close,5)` 与 `mul(mean(close,5),1.0)` 是两个 id，却指向同一个信号。它们各自计一次试验，`n_trials` 因此虚高，而 `expected_max_sr` 随 `√(2·ln n_trials)` 上升——**门槛被自己的记账方式收紧了**。

`dedup` 给出经济等价判重，缺省关闭（`corr = 1.0`）以保持既有计划行为不变：

| 键 | 含义 | 缺省 |
|---|---|---|
| `corr` | 判为同一因子的 score 相关阈值（`1.0` = 关闭） | `1.0` |
| `method` | 相关性算法：`spearman` / `pearson` | `spearman` |
| `rep_by` | 簇内代表的排序依据：`abs_ic` / `hac_t` / `abs_gross_sharpe` | `abs_ic` |

判定只在 `stage_a_pass` 的候选之间进行（未过粗筛的本就不进判据）：按 `rep_by` 降序贪心，与任一簇代表 `|ρ| ≥ corr` 则入该簇，否则自立为代表。`redundancy` 给出 `{threshold, method, n_effective, clusters, max_pair_corr}`；每个候选的 `metrics.redundant_of` 指向同簇代表（代表自身为 `null`）。

**`n_effective` 只作诊断，不进判据。** DSR / FDR 的门槛继续用 `n_trials`：用 `n_effective` 会降低 `expected_max_sr`，属于放宽判据。两个数都落盘，差距本身是"这批候选有多雷同"的度量。

### 3.7 `verdict` · 唯一裁决

| 项 | 内容 |
|---|---|
| 服务工位 | Verdict |
| 名字空间 | **唯一且不可配** |
| 参数 | `dsr_min`、`fdr_max`、`cost_adj_min`、`family`；**不含 `n_trials`** |
| 执行期事实 | `runtime_facts.n_trials`（账本快照）、`runtime_facts.family_cumulative` |
| 返回 | `VerdictSet`：`{per_candidate: [{candidate_id, decision, reason_codes, statistics}], n_trials_used}` |
| 组合 | 叶子（不得再调其他能力） |
| 前置 | `ObservationSet` 就位；账本可读；判据全部非 `null` |
| 失败 | 判据缺失 `E1008`；账本不可读 `E5001`；`n_trials` 缺失 `E2003` |
| 副作用 | `append_only`（由执行器代写裁决记录） |

`decision` 只允许 `pass` / `reject` / `watch`。`reason_codes` 必须可枚举（`dsr_fail` / `fdr_fail` / `cost_fail` / `null_fail` / `insufficient_n` / `redundant`），不允许自由文本作为唯一原因。

**`redundant` 给 `watch`，不给 `reject`。** 与已判为独立发现的候选高度相关，说明它是真信号、只是不独立；判 `reject` 会把它记成"被拒绝的坏因子"，并污染族失败计数。判 `watch` 的后果是：不进组合、不占用独立发现名额，证据链完整保留。`statistics.n_effective_trials` 回写本批的 `redundancy.n_effective`，仅供追溯。

**`n_trials` 不在 `params` 里，在 `runtime_facts` 里**：写进参数就等于「把计数填小」成为新的自由度；而它又不属于配置（不能预置），所以只能由执行器从账本快照注入，并进 `call_digest`。返回值里回写 `n_trials_used`，是为了让「这次裁决用的是多少次试验」在产物里可查，而不是只在日志里。

### 3.8 `combine` · 合成

| 项 | 内容 |
|---|---|
| 服务工位 | Portfolio |
| 名字空间 | 封闭多选 |
| 参数 | `domain: ref`（冻结域）、`orthogonalise: enum{none, symmetric}`、`corr_max: float`、`window: {fit, embargo, eval}` |
| 组合 | 叶子（合成只产分数，不建仓、不计成本） |
| 返回 | `Composite`：`{score_ref, weights_ref, members, dropped_members, domain_digest, windows_used}` |
| 前置 | 域已冻结；`fit.end + embargo ≤ eval.start`；`eval.end ≤ 搜索区间末端` |
| 失败 | 域成员不足 `E3003`；三段窗口重叠或越界 `E1021`；成员口径不一致 `E1018` |
| 副作用 | `repeatable`，可缓存 |

> **`fit.start ≥ max(available_from)` 与 `E1019` 目前未实现（2026-11-18 核实）**。原因不是漏写：
> 编排层把候选的 `available_from` 置成**供给覆盖末端**（`live.build_live_supply` 的 `as_of`），
> 这个值在任何真实计划上都晚于 `fit.start`，于是「按文档实现」等于让所有计划 `E1019`。
> 正确修法是先把 `available_from` 的语义改回「该候选**首次可计算**的那一天」（需要编排层与
> `search` 一起改，并给出可复现的推导），再实现这条检查。在此之前本节不声明这条前置，
> `E1019` 保留在错误码表里给未来的实现用——但**不得**在验收里当成已通过项。
> 现状的不泄漏由另外三条保证：`fit.end + embargo ≤ eval.start`、供给 pin 的 `acquired_at`、
> 以及标签层的保留区触达掩码。

`members` 不可为空，且每项必须带 `candidate_id` / `available_from` / `evidence_ref`。权重必须**在 `fit` 段一次性拟合**并落盘，`eval` 段内保持不变；不存在「按时点重拟合」的参数。

三个合成器（实现名 = `portfolio.combiner`，closed_multi）：

| 合成器 | 权重来源（只读 `fit` 段） | 说明 |
|---|---|---|
| `equal_weight` | 每人 1/K | 基准；暴露先逐 bar 截面标准化，否则按量纲加权 |
| `score_weighted` | `fit` 段 IC（**带符号**）归一 | 负 IC 成员自动反向；全零 IC 退化为等权 |
| `fixed_ridge` | `fit` 段岭回归闭式解 | `λ = 1e-3·tr(XᵀX)/K`，完备案例，不插补 |

`corr_max < 1` 时按 `|fit IC|` 降序贪心保留成员，与已保留成员 `fit` 段相关性超限者剔除，逐条记入 `dropped_members`（`reason=corr_above_max`）。

`orthogonalise=symmetric` 对保留成员的 `fit` 段相关阵做 Löwdin 正交化（`A^{-1/2}`），权重变为 `A^{-1/2}w`；`eval` 段沿用同一变换。特征值下限使共线成员被压平而不是让相关阵奇异。

`combine` 只交分数，**不交结论**：合成后的净表现由 `allocate` + `backtest` 在同一 `caliber` 下算出，且该表现属于样本内构造诊断，不构成可交易结论。

### 3.9 `allocate` · 持仓构造（全系统唯一）

| 项 | 内容 |
|---|---|
| 服务工位 | Evidence（单因子）/ Portfolio（组合）/ Certify（复算） |
| 名字空间 | 封闭多选，但**三个调用点必须解析到同一实现同一版本** |
| 参数 | 只有 `top_k`（`caliber.allocate` 未定义的另一种构造选择）；`quantiles` / `long_short` / `weighting` / `max_weight` / `net_exposure` / `turnover_max` / `min_members` **全部来自 `caliber.allocate`，此处出现即 `E1003`** |
| 组合 | 叶子 |
| 输入 | `score`（`(T,N)` 分数或暴露）、`tradable_mask`、`tiers`、`price`（**可选**，仅 `weighting=inv_vol` 需要） |
| 返回 | `Position`：`{weights_ref, turnover_series, effective_members, binding_constraints, dropped_bars}` |
| 前置 | `score` 与掩码就位；`caliber.allocate` 已注入；`weighting=inv_vol` 时 `price` 必须就位（缺则 `E3003`，不拿分数波动冒充资产波动） |
| 失败 | 约束不可行 `E3003`；`max_weight × N < 1` `E1004`；`min_members` 不可行且未声明丢弃策略 `E1021` |
| 副作用 | `repeatable`，可缓存 |

三条硬要求：

1. **`turnover_series` 是成本的唯一输入**。不返回逐时点换手，`backtest` 就只能按名义额估成本，那是系统性的高估。
2. **`binding_constraints` 必须返回**。没有它就无法区分「策略选的权重」与「约束逼出来的权重」，也就无法判断改参数到底改了什么。
3. **`dropped_bars` 必须带原因码**（`insufficient_members` / `no_tradable` / `width_below_min`）。静默跳过会让评估窗口被悄悄缩短，而结论看起来更好。

单因子与组合走同一实现：单因子的 `score` 是一个候选的横截面暴露，组合的是合成分数。**允许的差异只有因子个数，不是构造方式。**

**两条内部路径，一个实现**（由 `caliber.allocate.mode` 决定走哪条，不由工位决定）：

| 路径 | 何时 | 实测 | 要点 |
|---|---|---|---|
| 分位构造 | 无约束或仅 `max_weight` | **全面板 1.01 ms** | 排序 → 取头尾 → 等权，纯 numpy；**不调优化器** |
| 约束投影 | 有净敞口 / gross / 换手 / 行业中性 | **1.47 ms / 次调仓** | cvxpy + `cp.Parameter` + **问题对象按约束指纹复用** |

> **约束投影路径当前不可达（2026-11-18 核实）**：`plan_schema.json` 的 `caliber.allocate` 只有
> `quantiles / long_short / weighting / max_weight / net_exposure / turnover_max / min_members`
> 七个键，**没有 `mode` 也没有 `constraints`**——计划里写这两个键会在编译期 `E1001` 被挡。
> 实现体（`_build_constrained`）与本节的口径描述都保留，但要走这条路必须先同时改
> schema（构成性字段）、本节、以及 AC29 的用例：只改 schema 不改判据口径，等于让一个
> 没有测试覆盖的分支悄悄上线。在此之前，`mode` 只能取 `quantile`（缺省）。

约束路径有一条硬约束（I28）：**一次执行内只允许 canonicalize 一次**。实测重建 `Problem` 是 6.61 ms/次，复用结构是 1.47 ms/次，差 4.5 倍——237 次调仓差 1.2 s/候选，200 候选就是 4 分钟。复用的合法性来自 `caliber` 冻结：约束集合在一次执行内不变，所以它的指纹就是 `caliber_digest` 的一部分。

两条路径必须产出**同一形状**的 `Position`（AC29），只有权重量级不同；不允许约束路径少返回 `binding_constraints` 或换一套 `turnover_series` 定义。

### 3.10 `evaluate` · 表达式求值

| 项 | 内容 |
|---|---|
| 服务工位 | Evidence（子）/ Certify（子） |
| 名字空间 | 单实现（求值语义只有一种） |
| 参数 | `operator_set: ref`（编译期冻结的算子白名单版本）、`nan_policy: enum`（`propagate`\|`drop`）、`node_cache: bool`（**默认 `false`**，本阶段不接受 `true`） |
| 组合 | 叶子（不得再调其他能力） |
| 输入 | `payload`（表达式 AST 或 `model_ref`）、面板引用 |
| 返回 | `{values, valid_mask, node_stats, operators_used}`——`values`/`valid_mask` 是**数组通道**，由执行器落盘后换成 `values_ref`/`valid_mask_ref` |
| 前置 | `C16` 已通过；面板与 as-of 时点就位 |
| 失败 | 算子不在白名单 `E1015`；`payload` 与面板字段不匹配 `E1003`；参数取值不被支持（`node_cache=true`、非白名单 `nan_policy`）`E1004`；窗口参数越界 `E1008`；退化/常数/空截面 `E3003` |
| 副作用 | `pure`，可缓存 |

三条硬要求：

1. **`values_ref` 是唯一的暴露来源**。`observe`、`combine`、`certify` 必须读它，不得各自重算——否则 NaN 传播、tie 策略、除零约定三处可能不同，这是 C20 管不到的口径漂移。
2. **`node_stats` 只报实际发生的事**。缓存关闭时命中数/总数/键前缀三项全为 `null`，不得回填 `0`——「没查缓存」与「查了 0 次命中」是两件事，回填 `0` 会让巡检把「缓存没开」读成「缓存开了但命中率 0%」。
3. **`valid_mask` 与 `values` 分开**。把无效值写成 0 会让截面排序把「没数据」当成「中间值」。

#### 数组通道与 `Ref` 的分工

`evaluate` 的返回里是**数组**（`values` / `valid_mask`），不是 `Ref`。这不是偷懒，是 AC31 的硬约束：能力层（`capability/ops/`）不得 import `pyarrow` / `pathlib` / `os` / `io` / `duckdb`，由 `tests/tests/test_v5_package_isolation.py` 静态扫描执法。能力层因此**没有任何落盘手段**，让它产出 `Ref` 只能靠伪造路径字符串。

分工是：

| 谁 | 做什么 |
|---|---|
| 能力层 | 算出 `values` / `valid_mask`，按 schema 校验形状，返回内存数组 |
| 执行器（编排层） | 把数组写进存储，得到 `values_ref` / `valid_mask_ref`，连同 `node_stats` 交给下游工位 |

所以 `values_ref` 的**生成位置**在执行器，而**唯一合法来源**仍是这次 `evaluate` 调用——「唯一来源」说的是不许下游重算，不是说不许上游落盘。S3-M2 只到数组通道为止，落盘接线在 S3-M6 与八工位一起做。

同理，`stream_refs` 这类 provenance 字段（`read` 的返回）指向的是**输入**，不指向本能力的产物；把输入引用当成产物引用会让审计误以为「这是它算出来的东西」。

#### `nan_policy` 的两种取值

`nan_policy` 不改变算子语义（名字空间是 `single`，求值语义只有一种），它只决定 **`valid_mask` 的完整性要求**：

| 取值 | `valid_mask` | 用途 |
|---|---|---|
| `propagate`（默认） | 逐格：`valid = isfinite(values)` | 一格缺就那一格无效，其余照用 |
| `drop` | 逐截面：某时点只要有一格不可算，`valid[t, :]` 全为 `False` | 「只保留完整截面」的研究约定 |

**`values` 在两种取值下完全相同**：`drop` 不会把缺失补成值，也不会缩短时序窗口。若它去动 `values`（例如先剔除缺失行再算滚动窗口），窗口就会跨过缺失去用更早的观测——那是比 NaN 更隐蔽的错误，而且没有任何下游检查点能发现。`drop` 的代价因此只落在「可用时点数」上，是可数的。

#### 三类拒绝：退化、常数、空截面

三者都是 `E3003`（输出不合格），不是 `E2004`（那是编译期口径冲突的码）。判定都在求值完成后、返回之前：

| 情形 | 判据 | 为什么必须拒绝 |
|---|---|---|
| 退化格 | 非有限值占比超阈值，或 `std == 0` | `I42`：整批拒绝优于部分填充 |
| 常数暴露 | `极差 <= 1e-12 × max(1, \|最大值\|)` | 通篇只差浮点末位的因子与常数因子没有区别；它会以「成功」的形态穿过证据层，然后让 IC 序列全 0 |
| 空截面过多 | **首根有效 bar 之后**，全 NaN 截面占比 > 20% | 一个因子在超过五分之一的时点上算不出任何值，它的 IC 序列不是「有点稀疏」，是「没有样本」。头部预热段不计入——那是窗口本身的代价，不是因子的缺陷 |

`nan_policy=drop` 时还要额外判一次：整块面板没有任何完整截面 → `E3003`。「全部被屏蔽」与「本来就没有值」在 `valid_mask` 上长得一样，但前者是本能力造成的，必须由本能力报。

`payload` 有两条求值路径，产物形状必须完全一致：`expression` 走 **qweave**（Rust DAG），`model_ref` 走模型推理（torch / sklearn 的 predict）。下游 `observe` / `combine` / `certify` 不区分候选来自哪条路径——否则模型类候选要在证据层单独开一条链路，算法差异就扩散到整条链了。**本阶段 `model_ref` 只做形状拒绝**（`E1003`）：模型推理需要 `FittedState` 与推理环境，属 S5 之后；给一条「能过校验但算不出值」的路径比直接拒绝更糟。

节点级缓存的键是 `H(node_ast_digest + panel_digest + asof + operator_version)`，**必须**含后三项：只按 AST 存会让换数据版本后命中旧值。**但 `node_cache` 默认关闭，且本阶段不接受 `true`**（`E1004`）：实测 93 KB 节点落盘写+读 13–16 ms，而同批重算均摊 0.2 ms/表达式（当前 23.7k 行面板）——落盘净亏 65 倍。盈亏线在 250k 行（均摊 1.7 ms，仍亏）与 4M 行（59.5 ms，已赚）之间，需在该规模重新实测。当前规模只依赖求值内核的同批 CSE，见 `s0/02-crosscutting.md` §7。**给一个「开了但没生效」的开关比没有这个开关更糟。**

求值内核是 **qweave**（Rust DAG，自带公共子表达式复用、slot 复用与节点级并行），本系统只自研三件：算子白名单表（遍历 AST 校验）、JSON AST → `Expr` 的构造器（约 240 行：34 个算子的调用形状表、注册表三向比对、递归构造 + 校验一体）、跨调用缓存键。见 `07-components.md` §4 与 `08-reuse-audit.md` §10。

**为什么构造器是 200 行而不是 20 行**：qweave 的 `PyExpr` 方法名与算义词不同（`sum→ts_sum`、`mean→ts_mean`、`ref→delay`、`add→__add__`、`corr→correlation`），且标量必须 `qw.lit()` 包装、`expr - 1.0` 直接抛 `TypeError`。这层映射必须显式写在表里并**逐条验证符号真实存在**，否则白名单上写着支持、运行到那一步才发现没有绑定。`group_rank` / `group_neutralize` 因此被移出注册表：它们要求 group 列是 string/int，而 `Panel` 强制 float64，在当前面板表示下永远不可用——留一个必然抛错的算子比删掉它更危险。

### 3.11 `backtest` · 回测

| 项 | 内容 |
|---|---|
| 服务工位 | 5 / 6 / 7 / 8（共享） |
| 名字空间 | 封闭多选，但**四个调用点必须解析到同一实现同一版本** |
| 参数 | `detail: enum{summary, full}`；另只允许「与本次调用相关的选择」（例如是否计入资金费）；**成本档、滑点、入场价、资金费周期、年化基准、杠杆与保证金一律来自 `caliber`** |
| 组合 | 叶子 |
| 输入 | 持仓或权重、价格、资金费事件、逐时点分层 `tiers`、**时间轴 `time`**（尾部可选，UTC 纳秒整数；生产链路由编排层从 `Panel.times` 传入） |
| 返回 | `BacktestResult`：`summary` 档交 `{net_return_ref, cost_breakdown, turnover, funding_total, sharpe, annualisation_used, bars_per_year, bars_per_year_source}`；`full` 档另交 `{ledger_ref, fills_ref, equity_ref, long_short_split, drawdown_episodes, attribution}` |
| 前置 | 持仓就位；价格、资金费、`tiers` 就位；`caliber.net` 已注入 |
| 失败 | 成本档位为空 `E1008`；`annualisation` 与频率不匹配 `E1013`；声明杠杆却请求 `summary` 且实现无完整账本路径 `E1013`；`time` 长度与 bar 数不符或非严格递增 `E3003` |
| 副作用 | `pure`，可缓存 |

`BacktestResult` 的两档来自**同一份 `Ledger`**（`{t, asset, target_w, actual_w, fill_qty, fill_px, fee, slippage, funding}`）：`summary` 是聚合，不是近似。**禁止实现另写一条向量化捷径**——两套成交逻辑就是两套口径。

**年化基准与 bar 频率分开记**（§5.4 的口径要求）：`annualisation_used` 只表达「一年按 365 个日历日折算」，恒为 `calendar_365`；`bars_per_year` 表达「一年多少根 bar」，由 `time` 的中位步长推导（1d → 365，1h → 8760，4h → 2190），`sharpe = mean/std × sqrt(bars_per_year)`。`bars_per_year_source` 只有两个取值：`time_axis_median`（真实时间轴）与 `assumed_daily`（缺 `time`，按日频假设）。**把频率写死成 365 会让小时线策略的 Sharpe 被低估 sqrt(24) ≈ 4.9 倍**，所以生产链路上的编排层必须传 `time`：`assumed_daily` 是可审计的降级标记，不是可接受的默认值。时间轴非等距（采集缺口）时取中位步长——标称频率才是「一年几次」的口径。

**四个调用点必须读到完全相同的参数值**。实现上不允许各自从 `params` 里取——统一由 `policy_view` 注入。`cost_breakdown` 必须按「手续费 / 滑点 / 资金费」三项分开返回，否则无法判断成本是否被算重。

杠杆与强平：`max_leverage` / `initial_margin` / `maintenance_margin` / `liquidation_buffer` 全部从 `caliber.net` 读。声明杠杆后，`summary` 聚合必须走完整账本路径（无路径依赖前提不成立），由 C20 旁的编译校验拦下。

### 3.12 `certify` · 认证

| 项 | 内容 |
|---|---|
| 服务工位 | Certify |
| 参数 | `folds: int`、`embargo: duration`、`oos_window` |
| 输入 | `portfolio` / `panel` / `tradable_mask` / `tiers` / `verdict_set`（**五项显式**，不用 `all_upstream`） |
| 执行期事实 | `runtime_facts.holdout_visibility`（`reserved` / `revealed`） |
| 组合 | `evaluate`、`allocate`、`backtest`（**必须与证据层读同一 `caliber`**） |
| 返回 | `Certificate`：`{metrics, folds_detail, reproducible_manifest, verdict_refs, evidence_grade}` |
| 前置 | 全部上游 Parcel 就位；`holdout_visibility=revealed` 时要求 Reveal 事件已记录 |
| 失败 | **上游结构**不合规 `E1021`（`oos_window` 与 eval 段重叠、成员裁决为 reject）；**形状/参数**不合规 `E3003`（折数为 0、portfolio 成员为空、成员缺裁决）；未揭示却按已揭示计算 `E3006` |
| 副作用 | `repeatable` + 触发 Publish |

认证的 `metrics` 必须引用 Sentinel 的判据结果（`verdict_refs`），不得自算一套「展示用」显著性。

**输入必须显式列五项，不能用 `all_upstream`**：`score` 这个名字同时出现在 `label` 与 `combine` 的产物里，按 kind 展开会取到「标签分数」而不是「组合分数」——两者形状相同、语义相反，错了不会报错，只会把认证变成对标签的自相关。显式五项让「认证读的是组合层结论」成为结构事实。

**逐折净表现由编排层回填，认证不自建子调用**：`certify` 只切折（`folds_detail[i]` 给 `start_index` / `end_index`）并装配 `reproducible_manifest`；每一折的 `allocate` → `backtest` 由编排层按同一 `policy_view` 驱动，结果写回 `folds_detail[i]` 的 `net_return` / `sharpe` / `turnover` / `max_drawdown` / `cost_*`。这条分工是 s0/04 §1.3「能力不自建子调用」的直接推论：认证层若自己再跑一遍回测，就等于存在第二套成交逻辑。

**`net_return` 是内存通道，不是新落盘身份**：`backtest` 的落盘身份仍是 `net_return_ref`（内容 digest）；`net_return` 数组只沿内存对象通道上浮，供认证算 Sharpe/回撤，不进 `Parcel.payload`、不进指纹。否则认证要么重跑回测、要么只能拿到一个 `ref` 却算不出数。

**逐折数组要剔除结构性的第 0 期 0**：回测的 `net_return[0]` 恒为 0（没有上一期持仓可成交），把它算进 `sharpe` / `positive_folds` 会系统性稀释每折表现——折越短偏差越大。

**零晋升是结论，不是认证失败**：`Portfolio` 若无 `pass`/`watch` 成员，工位记 `skipped(no_candidate_passed)`，`Certify` 随 `upstream_skipped`，整链 `state=skipped` 且退出码 0（s0/05 §4.2 / AC52）。认证段不产证书，也绝不产一张「样本内冒充样本外」的空证书。

**揭示状态在 `runtime_facts` 里，不在 `params` 里**：写进参数就等于允许把「未揭示」声明成「已揭示」。审核状态同理——它决定这次认证能否生成可发布结论，属于执行期事实，不属于配置。

`evidence_grade` 是本工位唯一的产出等级标记：只有认证段产出的才是 `out_of_sample`，其余一律 `in_sample`。发布流程只接受前者，这条由 `PortfolioParcel` / `CertificateParcel` 的等级字段共同保证。

---

## 4. 挖掘算法接口（S0 冻结版）

### 4.1 四方法

```
describe()                                   -> AlgorithmSpec
fit(ctx, params, seed)                       -> FittedState     # stateful 时必选
generate(view, params, seed, fitted_state)   -> CandidateSet
lineage(candidate)                           -> Lineage
```

S0 只冻结签名与类型，不实现任何算法。

`fit` 收 `ctx`、`generate` 收 `view`，**这个不对称是契约的一部分**：只有拟合阶段存在适应度通道，生成阶段连通道对象都拿不到。

```
FitContext = {
  view,                      # 与 generate 收到的同一个 MarketView
  params,                    # 该算法的参数快照
  seed,                      # 按算法名派生
  rng,                       # 实例级随机源（不用全局 random）
  fitness(payload) -> float | tuple[float, ...]
}
```

`fitness` 由编排层构造，三件事在闭包内完成：调 `evaluate`（与证据层同一实现同一版本）、按 `mine_slice` 裁范围、登记一次试验（键为 `candidate_id`，重复不记）。**算法手里没有账本句柄，因此计数不可能少记或重记。**

### 4.2 `AlgorithmSpec`

除 `CapabilitySpec` 的公共字段外，额外要求：

| 字段 | 取值 | 编译期用途 |
|---|---|---|
| `output_kind` | `expression` / `model_ref` | 决定落盘前走语法沙箱还是推理依赖检查 |
| `required_fields` | `list[str]` | C15 |
| `operator_whitelist` | `list[str]` | C16 |
| `max_candidates` | `int` | C14 / `E1017` |
| `fit_window` | **唯一值 `mine_only`** | C17 |
| `budget_group` | **唯一值 `inherit`** | 禁止算法自建族 |
| `needs_fitness` | `bool` | 与 `stateful` 的组合校验：`needs_fitness=true` 且 `stateful=false` → 编译失败（没有拟合阶段就没有通道）；`needs_fitness=false` 却在 `fit` 内调用 `ctx.fitness` → 运行期失败 |
| `fitness_objectives` | `list[str]` | 多目标的组成声明；改目标集合即新试验族，与 `caliber.allocate.turnover` 的定义比对 |

后两项是「只能有一个合法值」的字段。它们存在不是为了配置，而是为了让「越界」成为一种可被校验的声明——如果一个能力可以完全不说，就无法在编译期发现它越界。

### 4.3 `MarketView`

| 字段 | 说明 |
|---|---|
| `panel_ref` / `label_ref` | 只读引用 |
| `mine_slice` | 允许搜索与拟合的区间 |
| `observable_mask` | 逐时点可观测 |
| `tradable_mask` | 逐时点可交易（**与可观测分开**，两者不是同一集合） |
| `valid_bar_mask` | 标签层产出的逐时点有效掩码（宽度不足已标记） |
| `field_manifest` | 实际可用字段及版本 |
| `family` / `seed` | 只读 |

**不含**：样本外与保留窗口、任何指标、判据阈值、账本、配置、写权限、网络、别名解析、`caliber`。

`caliber` 也不给算法：算法不该知道成本档或分位定义，否则它可以隐式地朝「更便宜成本下的好表现」搜索。

### 4.4 `Candidate` 与 `CandidateSet`

`Candidate` 必填：`candidate_id`、`source`、`generator`、`payload`、`required_fields`、`parents`、`operators`、`available_from`、`params_snapshot`、`seed`。

`candidate_id = H(payload + required_fields)`，是**内容身份**，不含 `generator` / `params_snapshot` / `seed`。这三个字段保留为谱系元数据：记录「谁、用什么设置、什么种子造出了它」，但**不参与身份**。

理由：跨算法去重要求「两个算法产出同一表达式」被判为同一候选。若身份里带生成器与种子，两者必然不同，去重永远不生效，计数被系统性放大——门槛虚高，真信号被拦掉。同一表达式由两个算法生成时，`generators` 保留两条谱系，但计数只加一。

`available_from` 取**执行期 as-of 时点**（由编译期固化的数据末端确定），不是墙钟时间：它是口径的一部分，取墙钟会让「同方案两次执行」得到不同口径指纹。登记进全局因子库时才用真实登记时刻。

字段白名单之外一律拒绝（`E2004`）。禁止出现：任何指标、`p` 值、成本、权重、结论、状态、排序。

`CandidateSet` 必填：`candidates`（去重后有序）、`family`、`count`（**实测去重数**）、`dedup_digest`、`fitted_state_ref`（可空）、`generators`（来源谱系，可多于一个）、`declared_at`（观测字段，**不进任何指纹**）。

### 4.5 多算法与内置因子

| 规则 | 强制点 |
|---|---|
| 共享一个 `MarketView`、一个 `family`、一个 `mine_slice` | 执行器只构造一次 |
| 种子按 `name` 派生 | `rng.sub_seed` |
| 跨算法按 `candidate_id`（内容身份）去重 | 执行器合并 |
| **一次** Budget 登记，值为 `count` | 执行器代写 |
| 各算法 `max_candidates` 之和 ≤ 计划上限 | `E1017` |
| `generator` 仅元数据，不分族 | 账本键不含 `generator` |
| 有状态算法各自一对 `fit` / `generate` 调用单 | 执行器逐算法构造，`FittedState` 各自落盘 |
| `call_fit.inputs` 不得含评估区间引用 | 运行期比对，违反 `E1016` |

内置因子是预注册数据，转成 `source=prior` 的 `Candidate`，与算法候选一起去重、一起计数、一起裁决。

---

## 5. 名字与目录

四档名字空间由 `catalog/impls.json` 的 `namespaced` 字段承载：

| 档 | 校验行为 |
|---|---|
| `open` | 名字缺失 → `E1002`；允许目录新增条目 |
| `closed_multi` | 名字必须命中白名单，且条目数 > 1；新增条目属于契约变更 |
| `single` | 目录中该工位只有一个条目；出现第二个 → `E2006` |
| `fixed` | 配置里**不得出现** `impl` 字段；出现 → `E1003`；目录里多条目 → `E2006` |

最后一条容易被忽略：唯一裁判的「不可配」不是「配了也没用」，而是**配置里出现这个键就是错误**。静默忽略会让用户以为自己换掉了裁判。

---

## 6. 能力的三条结构禁令

| 禁令 | 结构保证 | 检查点 |
|---|---|---|
| 不评估 | `Candidate` / `Observation` 结构里没有指标字段；`MarketView` 无评估窗口、无 `caliber`。自适应算法只能在 `fit` 内经 `ctx.fitness` 拿标量/向量，且该返回值无法进入候选结构 | `E2004` |
| 不判显著 | 四方法签名无判据参数、无结论出口；`fitness` 返回值形状里没有 p 值与成本后收益 | 签名静态检查 |
| 不写计数 | 无账本句柄；`fitness` 闭包内登记（键 `candidate_id`，重复不记），Budget 由执行器在落盘后按去重后的总 `count` 代写 | `E2001` |
| 不自选口径 | 无配置对象；`policy_view` 只含声明过的子块 | `policy_reads` 静态校验 |
| 不自建子调用 | 只能执行 `call.composed[i]`；未声明调用即 `E2008` | 子调用图编译期校验 |

这五条是 S0 存在的核心理由：它们**无法靠代码评审长期保证**，只能靠类型里没有位置。

---

## 7. S0 验收点（本篇）

| # | 断言 |
|---|---|
| C1 | 十二项能力全部注册，`describe()` 可在无数据环境调用成功 |
| C2 | 每项能力传入 schema 外字段 → `E1003`；类型不符 → `E1004` |
| C3 | 每项能力实现体在 S0 抛 `E3004`，且**不写任何文件** |
| C4 | 返回结构不符合 `outputs_schema` → `E3003`，不当作成功 |
| C5 | 能力体内调用 `clock.wall` / `cache` / `notify` → 静态检查 `E2001` |
| C6 | `verdict` 目录里出现第二个实现 → `E2006`；配置里写 `verdict.impl` 另一个名字 → `E1003` |
| C7 | 算法 `describe()` 缺 `fit_window` 或值非 `mine_only` → `E1016` |
| C8 | 算法 `max_candidates` 之和超计划上限 → `E1017` |
| C9 | `Candidate` 多一个 `rank` 字段 → `E2004` |
| C10 | 两算法产出同一表达式 → `candidate_id` 相同，去重后 `count` 只加一；`generators` 保留两条 |
| C11 | `backtest` 被四个调用点调用时读到的成本档位、杠杆与保证金逐项相同（由 `policy_view` 注入，非各自取参） |
| C12 | 能力不落盘：调用前后 `Ticket/` 目录的文件列表与哈希完全不变 |
| C13 | 只改 `seed` → `call_digest` 变化、缓存不命中；只改 `runtime_facts.n_trials` → 同样变化 |
| C14 | 实现在 `composes` 之外调用另一个能力 → `E2008`；声明链深度 3 → `E2008` |
| C15 | 父调用与子调用的 `policy_digest` 不一致 → `E1020` |
| C16 | `verdict.params` 里写 `n_trials` → `E1003`（它属于 `runtime_facts`）；`certify.params` 里写 `holdout` → 同样 `E1003` |
| C17 | `allocate` 在证据与组合两处解析到不同版本 → 编译失败；`turnover_series` 缺失 → `E3003` |
| C18 | 声明 `needs_fitness=true` 而 `stateful=false` → 编译失败；声明 `false` 却在 `fit` 内调用 `ctx.fitness` → 运行期失败 |
| C19 | `fit` 内 `ctx.fitness` 调 N 次（含重复表达式）→ 登记值为去重后 `candidate_id` 数，不是调用次数 |
| C20 | `fit` 退出后 `random.getstate()` 与进入前快照逐位相同；并发第二个自适应 `fit` → `E2009` |
