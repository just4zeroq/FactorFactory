# S0 · 01 数据契约

> 本篇冻结全系统唯一的类型字典：七类核心结构、八个指纹、错误分层、序列化规则。
> 任何一层的新增类型都不得绕过这里，也不得在这里之外定义第二套「差不多」的结构。

---

## 1. 类型总览

| 类型 | 性质 | 谁产出 | 谁消费 | 生命周期 |
|---|---|---|---|---|
| `Digest` | 值对象 | 指纹服务 | 全部 | 永久 |
| `Ref` | 值对象，指向不可变内容 | 存储层 | 全部 | 跟随内容 |
| `Pin` | 值对象，供给版本引用 | 编译期 | 编排层、能力层 | 跟随版本 |
| `Call` | 值对象，一次调用的完整输入 | 编译器 | 能力层 | 一次调用 |
| `Parcel` | 不可变落盘产物 | 编排层 | 下游工位、认证 | 永久 |
| `Ticket` | 一次执行的状态主体 | 编排层 | 排障、复现、审计 | 永久 |
| `Blueprint` | 冻结方案 | 运行时编译器 | 编排层 | 一次执行 |

三条通用规则：

1. **值对象不可变**：`Digest` / `Ref` / `Pin` / `Call` / `Parcel` / `Ticket` / `Blueprint` 构造后只读。修改一律产出新实例，旧实例不失效、不删除。
2. **不存活对象**：任何字段不得是模型实例、连接句柄、生成器、内存视图、文件句柄。只允许标量、字符串、列表、映射、`Ref`。
3. **可序列化即可传递**：一个字段若不能无损写进 JSON（或明确声明为二进制引用），它就不允许出现在跨层结构里。

---

## 2. 规范化与指纹

### 2.1 规范化 JSON

所有指纹都作用在**规范化字节串**上，不是作用在内存对象或原始字符串上。规范化规则：

| 规则 | 内容 | 不遵守的后果 |
|---|---|---|
| 键顺序 | 对象键按 UTF-8 字节序升序 | 同一内容产生不同指纹 |
| 分隔符 | `,` 与 `:` 后无空白 | 同上 |
| 数字 | 整数不带小数点；浮点用最短往返表示；`-0.0` 归一为 `0.0` | `1` 与 `1.0` 产生不同指纹 |
| 非法值 | `NaN` / `Infinity` 直接拒绝（`E2002`） | 不可比较、不可复现 |
| 字符串 | Unicode NFC 归一；禁止孤立代理项 | 同义字串指纹不一致 |
| 时间 | 一律 UTC，RFC3339，显式 `Z`，微秒精度 | 时区偏移导致指纹漂移 |
| 路径 | 相对根，`/` 分隔，禁止 `..` 与绝对路径 | 换机器即失配 |
| 集合 | 有序集合显式声明排序键；无序集合按其元素 `digest` 升序 | 迭代顺序影响指纹 |
| 空值 | `null` 与「字段缺失」不等价；可选字段缺失时**不得**补 `null` | 增删字段无法被区分 |

**禁止把浮点数组放进规范化 JSON**。大数组走二进制引用（`Ref`），指纹只覆盖引用与其形状摘要。

### 2.2 指纹公式

`H(x) = "sha256:" + hex(sha256(canonical_bytes(x)))`。

八个指纹分两组，**分组理由比公式更重要**：

| 组 | 指纹 | 公式 | 变化意味着 |
|---|---|---|---|
| 意图 | `plan_digest` | `H(plan_raw)` | 用户改了计划 |
| | `blueprint_digest` | `H(baseline_version, values, caliber, supply_pins, domain_digest)` | 展开后的方案变了（含数据版本） |
| | `policy_digest` | `H(baseline_version, constitutive_subset, **caliber_digest**, domain_digest)` | **口径变了**——所有产物必须失效 |
| | `domain_digest` | `H(sorted(members: id, version, available_from), fit_window, embargo, eval_window)` | 组合域或窗口变了 |
| 物化 | `supply_digest` | `H(sorted(pins 的指纹投影))`，投影**排除 `acquired_at`** | 用的事实版本变了 |
| | `environment_digest` | `H(version_subset, clock_basis, rng_impl, resource_limits)` | 跑的环境变了 |
| | `call_digest` | `H(station@revision, impl@version, phase, params_digest, input_digests, policy_digest, runtime_facts_digest, composed_digests, seed)` | **同一步的输入变了**——缓存键 |
| | `parcel_digest` | `H(call_digest, payload_digest)` | 产物变了 |

`caliber_digest = H(execution, label, allocate, net)`，四个子块整体参与口径；`runtime_facts_digest = H(计数快照, 揭示状态, 审核状态)`；`composed_digests` 是各子调用单的 `call_digest` 按声明顺序排列。

这两个字段是公式里后补的，理由都归于同一条原则——**改变结果的东西必须进指纹**：

- **`phase` 必须进 `call_digest`**。`fit` 与 `generate` 是同一个实现的两条不同计算路径。不写进去就会出现「拟合阶段的结果被生成阶段命中」这种错误缓存，而两边的 `params` 完全相同、无法靠参数区分。
- **`supply_digest` 排除 `acquired_at`**。它是采集完成时间，属 §2.3 表里「会随时间变化因此不得进任何指纹」的硬性排除项。重采一次逐字节相同的数据不该让全部下游产物失效。参与哈希的投影是 `dataset / version / checksum / coverage / symbols_digest / end_closed / schema_version / gate_a`——保留 `end_closed` 与 `gate_a`，因为它们决定能否通过门禁 B。

三条关键分离：

- **`call_digest` 与 `parcel_digest` 必须分开。** 前者在计算前就能算出，用于查缓存与判依赖；后者含输出，用于标识产物。合成一个的话，缓存就永远无法命中。
- **`policy_digest` 不含 `supply_pins`，`blueprint_digest` 含。** 数据刷新是事实演进，不是研究自由度：它让下游失效，但不新建试验族。
- **`environment_digest` 不进任何产物指纹。** 一旦进去，换台机器或升级无关依赖就会让全部历史产物「失效」，可复现反而不可比；它的用途是单独记录、在重放不一致时作为第一排查对象。

`seed` 必须进 `call_digest`：它改变结果却不改变参数时，缓存会错误命中。这是最容易漏掉的一项，因为它「只是一个数字」。

### 2.3 什么进指纹、什么不进

这张表是 S0 最容易被写错的地方，逐项列出。

| 内容 | plan | blueprint | policy | parcel |
|---|---|---|---|---|
| 构成性参数（池、成本档、跨度、判据阈值） | 是 | 是 | **是** | 间接 |
| **`caliber` 四子块（执行 / 标签 / 构造 / 成本）** | 是 | 是 | **是** | 间接 |
| 操作性参数（并行度、日志级别、超时） | 是 | 是 | **否** | 否 |
| 实现名与版本 | 是 | 是 | 否 | **是** |
| 数据版本 pin | 否 | **是** | **否** | 经输入 |
| 组合域成员与可用时间 | 是 | 是 | **是** | 间接 |
| 组合三段窗口（fit / embargo / eval） | 是 | 是 | **是** | 间接 |
| 基线版本 `@vN` | 是 | 是 | **是** | 间接 |
| 种子 | 是 | 是 | 否 | **是** |
| 工位契约版本 `revision` | 否 | 是 | 否 | **是** |
| 执行期事实（计数快照、揭示状态） | 否 | 否 | 否 | **是** |
| 子调用单指纹 | 否 | 否 | 否 | **是** |
| 时间戳、耗时 | 否 | 否 | 否 | **否** |
| 产物路径、日志路径 | 否 | 否 | 否 | **否** |
| 环境摘要 | 否 | 否 | 否 | 否（单独记录） |
| 试验计数本身（数值） | 否 | 否 | 否 | 否 |

最后四行是硬性排除：**只要一个字段会随时间或机器变化，它就不能进任何指纹**。路径进指纹是最常见的隐形 bug——同一份产物换个目录就「不同」了。

倒数第三行需要解释：「试验计数本身」不进指纹，「执行期事实的摘要」进。区别在于前者是账本里的一个滚动数字（每次执行都不同），后者是**本次执行读到的快照**——它确实改变了判据，因此必须让缓存失效。不区分这两者，就会得到「要么缓存永远失效、要么用过期计数判显著性」二选一。

### 2.4 规范化必须可测

指纹服务必须满足三条性质，并在 S0 写成单元测试：

| 性质 | 测试方式 |
|---|---|
| 确定性 | 同输入两次调用，结果逐字节相同 |
| 顺序无关 | 键的插入顺序打乱，指纹不变 |
| 敏感 | 任一语义字段改一个字节，指纹必变；上表「否」的字段改变，指纹**必不变** |

第三条比前两条更重要：只测「变了会变」，不测「不该变的没变」，就会把时间戳悄悄钉进口径锚点里。

---

## 3. 七类核心结构的字段

字段表中「必填」列为 `是` 的，缺失即 `E2003`（结构不完整），不允许补默认值。

### 3.1 `Digest`

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `algo` | `str` | 是 | 当前唯一值 `sha256` |
| `hex` | `str` | 是 | 64 位小写十六进制 |

序列化形态为 `"sha256:<hex>"`。不接受裸 hex，也不接受大写。

### 3.2 `Ref`（内容引用）

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `kind` | `enum` | 是 | `parcel` / `supply` / `derived` / `register` / `fitted` |
| `id` | `str` | 是 | 业务标识（工位名、数据集名、登记键） |
| `digest` | `Digest` | 是 | 指向内容的指纹 |
| `relpath` | `str` | 是 | 相对根路径，指向内容寻址目录 |
| `schema_version` | `str` | 是 | 被引用内容的 schema 版本 |

`Ref` 只描述「在哪、是什么、哪个版本」，**不携带内容**。任何持有 `Ref` 的层在解析时必须重新校验 `digest`，不信任路径本身。

### 3.3 `Pin`（供给版本引用）

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `dataset` | `str` | 是 | 数据集名，如 `ohlcv` |
| `version` | `str` | 是 | 内容摘要短标识，16 位十六进制 |
| `checksum` | `Digest` | 是 | 全量校验和 |
| `coverage` | `{start, end}` | 是 | UTC 时间，闭区间，`start < end` |
| `symbols_digest` | `Digest` | 是 | 标的集合的指纹 |
| `end_closed` | `bool` | 是 | 时间末端是否已闭合（无未完成 bar） |
| `schema_version` | `str` | 是 | 字段 schema 版本 |
| `acquired_at` | `timestamp` | 是 | 采集完成时间，UTC |
| `gate_a` | `{passed, reason_code}` | 是 | 采集侧完整性门禁结果 |

`end_closed=false` 的版本可以存在于 Depot，但**不得被别名指向**，也不能通过编译期门禁 B。

### 3.4 `Call`（一次调用的完整输入）

**定义只有一处**：`capability/runtime.py`。契约层不再另立一份——曾经有过一份 16 字段的副本（`inputs: list[Ref]`、三个子摘要存字段、校验更强），生产代码一处都不构造它，于是「哪个才算数」只能靠猜。合并后 `inputs` 是 `InputRef`（多一个 `data` 只读载荷通道），三个子摘要降为**派生属性**。

| 字段 | 类型 | 必填 | 存/派 | 约束 |
|---|---|---|---|---|
| `capability` | `str` | 是 | 存 | 十二个能力名之一；与 `station` 一起进 `call_digest`（同名参数不得跨能力共享缓存键） |
| `station` | `str` | 是 | 存 | 八个工位名之一 |
| `station_revision` | `str` | 是 | 存 | 工位契约版本 |
| `impl` | `str` | 是 | 存 | 编译期解析出的实现名，**不得为空**（空串曾静默回落到唯一实现） |
| `impl_version` | `str` | 是 | 存 | 显式 `vN` |
| `phase` | `enum` | 是 | 存 | `none` / `fit` / `generate`；有状态实现必须给后两者 |
| `params` | `map` | 是 | 存 | **全量展开**，无占位、无缺失；**不得含 `caliber` 已定义字段**（C20） |
| `inputs` | `list[InputRef \| null]` | 是 | 存 | 有序，顺序即契约；`null` 是**可选输入的位置占位符**（不能靠"少传一项"表达，那会让后继输入整体前移）；`phase=fit` 时不得含评估区间引用 |
| `policy_view` | `map` | 是 | 存 | **冻结口径的具体取值**，只含该实现 `policy_reads` 声明的子块 |
| `runtime_facts` | `map` | 是 | 存 | 执行期确定且**会改变结论**的事实：计数快照、揭示状态、审核状态；无则为空映射 |
| `composed` | `list[Call]` | 是 | 存 | 已由编排层构造好的子调用单，顺序即 `composes` 声明顺序；可为空列表 |
| `seed` | `int` \| `null` | 是 | 存 | 不需求种子的实现为 `null`（显式，不是缺失） |
| `call_digest` | `Digest` | 是 | 存 | 见 §2.2；第 1 步拿它与重算值比对 |
| `params_digest` | `Digest` | — | 派 | `H(params)` |
| `policy_view_digest` | `Digest` | — | 派 | `H(policy_view)`。**不是** `policy_digest` 本体——后者含 `caliber_digest` 与构成性子集，能力 `Call` 里没有这两个通道，只能由编排层持有 |
| `runtime_facts_digest` | `Digest` | — | 派 | `H(runtime_facts)` |

派生的三个**不存字段**：它们是本体的纯函数，存下来就多出一个能与本体分叉的副本，而"两份同名不同义的值"正是 V5 反复清掉的那类缺陷（`policy_digest` 就曾这样翻过车）。

**四个通道缺一不可**，各自解决一个此前无解的问题：

| 通道 | 解决什么 | 缺了会怎样 |
|---|---|---|
| `params` | 这个实现自己的选择 | — |
| `inputs` | 精确的输入引用 | 无法判依赖与缓存 |
| `policy_view` | 口径的**值** | 实现只能从 `params` 里再写一遍口径 → 同一口径两份来源 |
| `runtime_facts` | 计数与揭示状态 | 只能塞进 `params`（可被填小）或读全局（可被绕过），两者都破坏纪律 |
| `composed` | 内层调用 | 实现只能自己拼调用单 → 可以绕过声明去调一个更宽松的零分布 |

`Call` 是能力层能看到的**全部**输入。它里面没有配置对象、没有账本、没有判据、没有写权限——这不是约定，是类型里就没有位置。**唯一例外是 `policy_view` 携带的口径取值**：它是只读投影，且只含声明过的子块，不是配置树的入口。

`phase` 的存在让「拟合越界」变成一次可比对的断言：`phase=fit` 的 `inputs` 落在评估区间之外即 `E1016`。

### 3.5 `Parcel`（不可变落盘产物）

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `ticket_id` | `str` | 是 | 所属执行 |
| `station` / `station_revision` | `str` | 是 | 产出者 |
| `impl` / `impl_version` | `str` | 是 | 实际执行者 |
| `call_digest` | `Digest` | 是 | 输入指纹 |
| `payload_ref` | `Ref` | 是 | 产物位置 |
| `payload_digest` | `Digest` | 是 | 产物内容指纹 |
| `digest` | `Digest` | 是 | `parcel_digest`，见 §2.2 |
| `lineage` | `list[(str, Digest)]` | 是 | 上游 `(station, parcel_digest)`，有序 |
| `evidence` | `list[Evidence]` | 是 | 可为空列表，不可为 `null` |
| `origin` | `enum` | 是 | `run` / `step` / `commit` / `resume` / `cache` |
| `status` | `enum` | 是 | `done` / `failed` / `skipped` |
| `skip_reason` | `str` \| `null` | 是 | `status=skipped` 时必须非空 |
| `error` | `Error` \| `null` | 是 | `status=failed` 时必须非空 |
| `started_at` / `seconds` | `timestamp` / `float` | 是 | 仅观测，不进指纹 |
| `log_ref` | `Ref` | 是 | 本工位日志，与产物同目录 |

`Evidence` 只允许事实：

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `str` | `observation` / `diagnostic` / `count` |
| `name` | `str` | 稳定标识 |
| `value` | 标量 | 数值或字符串 |
| `unit` | `str` \| `null` | 单位 |
| `source` | `str` | 产生它的能力名 |
| `method` | `str` | 计算方法标识与版本 |
| `uncertainty` | 标量 \| `null` | 标准误、区间宽度等 |

**`Evidence` 禁止出现**：`pass` / `reject` / `significant` / `score` / `rank` / `weight` / 任何结论性词。结论只能由 Sentinel 写入 `VerdictParcel`，这一点由字段白名单在写入前校验（`E2004`）。

### 3.6 `Ticket`（一次执行）

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `ticket_id` | `str` | 是 | `<UTC 紧凑时间>-<plan_digest 前 8 位>`，全局唯一 |
| `plan_digest` / `blueprint_digest` / `policy_digest` | `Digest` | 是 | 三个意图与口径指纹 |
| `caliber_digest` | `Digest` | 是 | 口径块指纹，单独留一份便于审计 |
| `baseline_version` | `str` | 是 | `vN` |
| `family` | `str` | 是 | 试验族 |
| `family_declared_at` | `timestamp` | 是 | 预注册时间 |
| `master_seed` | `int` | 是 | 主种子 |
| `pins` | `list[Pin]` | 是 | 编译期固化，运行期只读 |
| `domain_digest` | `Digest` | 是 | 组合域与三段窗口 |
| `status` | `enum` | 是 | `pending` / `running` / `done` / `failed` / `stale` |
| `created_at` / `archived_at` | `timestamp` | 是 / 否 | |
| `environment_ref` | `Ref` | 是 | 环境快照 |

**可变字段只有两个：`status` 与 `archived_at`。** 其余字段一经创建不可变，运行期修改任何一个是 `E2005`，包括「只是更新一下 pins」。这条用「白名单两个可变字段」表述，比「前 N 个不可变」可靠——后者在增删字段时必然数错。

### 3.7 `Blueprint`（冻结方案）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `values` | `map` | 是 | 全量展开的配置树，无未解析占位 |
| `origins` | `map[str, enum]` | 是 | 每个叶子路径 → `baseline@vN` / `plan` / `override` |
| `resolved` | `map` | 是 | 每个模块 → `impl@version` + `params_digest` |
| `caliber` | `map` | 是 | `execution` / `label` / `allocate` / `net` 四子块全量展开 |
| `supply_pins` | `list[Pin]` | 是 | 别名已在编译期解析完毕 |
| `domain` | `DomainSpec` | 是 | 组合域与三段窗口，含 `domain_digest` |
| `family_declaration` | `{family, declared_at}` | 是 | |
| `station_order` | `list[(str, str)]` | 是 | 八步与各自 `revision` |
| `subcall_graph` | `map[str, list[str]]` | 是 | 每个实现的 `composes` 展开，含深度校验结果 |
| `digests` | `{plan, blueprint, policy, domain, supply, caliber}` | 是 | |
| `check_results` | `list[{id, passed, detail}]` | 是 | C1–C22 逐条 |
| `compiled_at` | `timestamp` | 是 | 仅记录，不进指纹 |

`origins` 必须覆盖 `values` 的**每一个叶子**。缺一个叶子，`flow explain` 就会出现「这个值从哪来」的盲区，因此缺叶子本身是 `E1007`。

---

## 4. 错误

### 4.1 `Error` 结构

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `code` | `str` | 是 | 见 §4.2 |
| `layer` | `enum` | 是 | `usage` / `contract` / `execution` / `supply` / `storage` |
| `message` | `str` | 是 | 人类可读，一句话 |
| `path` | `str` \| `null` | 否 | 配置路径或文件路径 |
| `hint` | `str` \| `null` | 否 | **必须可执行**，例如「先执行供给维护」 |
| `context` | `map` | 否 | 结构化上下文，禁止放凭据 |
| `retryable` | `bool` | 是 | 明确可重试与不可重试 |

`hint` 的价值标准：用户读完能立刻知道下一步做什么。写「请联系管理员」等于没写。

### 4.2 错误码表

**用法与配置 `E1xxx`（退出码 2）**

| 码 | 触发 |
|---|---|
| `E1001` | 配置路径不存在于 schema |
| `E1002` | 实现名不在能力目录 |
| `E1003` | 参数含 schema 之外的字段 |
| `E1004` | 参数类型、范围或枚举不符 |
| `E1005` | 构成性参数出现在覆盖层且未放行 |
| `E1006` | 严格模式未注入裁决者 |
| `E1007` | `origins` 未覆盖全部叶子 |
| `E1008` | 缺必填参数，或出现未解析占位 |
| `E1009` | 构成性参数改动但试验族未声明；或数据版本变化后在原计划上重跑搜索（I59） |
| `E1010` | 扫参未按预注册枚举（自动遍历） |
| `E1011` | 默认实现指向占位或演示实现 |
| `E1012` | 实现参数与其声明的拟合/变换阶段冲突 |
| `E1013` | 经济口径不自洽（判据口径与回测口径不一致） |
| `E1014` | 算法声明的字段超出已固化供给版本的覆盖 |
| `E1015` | 算法声明含禁用算子，或与全局算子白名单冲突 |
| `E1016` | 有状态算法拟合窗口非 `mine_only`，或拟合结果不可重建 |
| `E1017` | 多算法配额总和超计划上限，或实际产出超上限 |
| `E1018` | 组合域成员口径不一致、缺可用时间、纳入规则含表现类条件，或跨执行域成员未显式声明（I60） |
| `E1019` | 组合拟合窗口起点早于域内最晚可用时间 |
| `E1020` | 工位参数里出现 `caliber` 已定义的字段（跨度、入场价、成本档、年化、构造约束） |
| `E1021` | 组合三段窗口非法：评估与拟合重叠、隔离段缺失、评估越出搜索区间 |
| `E1022` | `rebalance < horizon` 但未声明重叠处理，或 `width_policy` 缺失 |

**契约违反 `E2xxx`（退出码 3）**

| 码 | 触发 |
|---|---|
| `E2001` | 下层尝试调用上层，或能力层取得禁止的入口 |
| `E2002` | 指纹输入含 `NaN` / `Infinity` / 非法路径 |
| `E2003` | 结构缺必填字段 |
| `E2004` | 禁止内容：`Evidence` 出现结论性字段、日志出现凭据或结论、`Parcel` 被就地修改 |
| `E2005` | 不可变字段被改写（含 `Ticket` 前九项、`Pin`、`Call`） |
| `E2006` | 检测到第二套执行路径或第二套裁判入口 |
| `E2007` | 设施被绕过：某层自行取时钟、随机源或写日志文件 |
| `E2008` | 调用了未在 `composes` 中声明的能力，或组合深度超过两层 |
| `E2009` | 自适应算法（`needs_fitness=true`）被并发执行：同一进程内已有另一个拟合阶段在跑（全局随机状态不可重入） |

**执行失败 `E3xxx`（退出码 1）**

| 码 | 触发 |
|---|---|
| `E3001` | 上游产物缺失 |
| `E3002` | 上游指纹与 `lineage` 不符 |
| `E3003` | 产物为空或无法重建，含截面丢弃时点占比超 20%（I61；**不得降级为成功**） |
| `E3004` | 工位实现抛出未预期异常 |
| `E3005` | 状态转移非法（如 `failed → done`） |
| `E3006` | 违反一次性事件语义：单步推进正式状态 / 触发事件，或声称已发生但账本无记录 |

**供给失败 `E4xxx`（退出码 4）**

| 码 | 触发 |
|---|---|
| `E4001` | 别名指向不存在的版本 |
| `E4002` | 版本覆盖区间、标的集或新鲜度不满足本次时间轴（门禁 B） |
| `E4003` | 时间末端未闭合 |
| `E4004` | 校验和不符 |
| `E4005` | 门禁 A 未通过的版本被引用 |
| `E4006` | 维护动作发生在编译之后或被单步触发 |
| `E4007` | 时间索引重复或非单调 |

**存储与 IO `E5xxx`（退出码 1）**

| 码 | 触发 |
|---|---|
| `E5001` | 目录不可写或权限不足 |
| `E5002` | 租约冲突，已有写者 |
| `E5003` | 原子发布失败（暂存未完成） |
| `E5004` | 写入被拒绝：目标为共享仓、内容寻址冲突（同 digest 异内容）、或超出本执行目录 |
| `E5005` | 日志与产物指纹不一致 |

### 4.3 错误处理三条硬规则

1. **不得捕获后继续**。除明确标注为「可降级」的少数（当前为零个），任何 `Error` 都必须向上传播并决定退出码。
2. **不得换码**。底层抛 `E4002`，上层不得包装成 `E1001` 或 `E3004`。分类是排查的第一信息。
3. **不得只记日志不返回**。日志是旁路，返回值是契约。二者都要有，缺一不可。

---

## 5. 序列化与落盘格式

| 内容 | 格式 | 约定 |
|---|---|---|
| 元数据（`Parcel`、`Ticket`、`Blueprint`、`Pin`） | JSON | UTF-8、2 空格缩进、键排序，**可被规范化函数直接消费** |
| 结构化记录（trace、ledger、维护日志） | JSON Lines | 一行一条，只追加，写后不修改 |
| 数值数组（面板、暴露、权重） | 二进制列式 | 通过 `Ref` 引用，文件名含 `payload_digest` |
| 报告 | Markdown + JSON 摘要 | 摘要里的每个数字必须带 `parcel_digest` 来源 |

目录命名一律内容寻址：`<id>/<digest[:16]>/`。**禁止用时间戳或自增序号作为不可变内容的目录名**——那会让「同一内容两份副本」无法被识别。

### 5.1 七类结构的 JSON 形态

上表只定了容器。**「写进 JSON」具体长什么样，必须逐个写死**，否则「全新进程重放得到逐位一致的 `parcel_digest`」这条门槛（见 `README` §7 第 5 条）无法判定：同一份内容有两种合法写法，指纹就会有两种结果。

| 类型 | JSON 形态 | 说明 |
|---|---|---|
| `Digest` | 字符串 `"sha256:<hex>"` | 裸 hex、大写、缺算法前缀一律拒绝（`E1004`） |
| `Ref` | **内联对象** `{kind, id, digest, relpath, schema_version}` | 不做「只写路径」的简写；`relpath` 是相对数据根的 `/` 分隔路径 |
| `Pin` | 内联对象，键同 `§3` 的字段表 | `acquired_at` 一律 UTC 带 `Z` 微秒；不进 `supply_digest`（§2.2） |
| `Evidence` | 内联对象 `{kind, name, value, unit, source, method, uncertainty}` | 可选键省略而不是写 `null` |
| `Call` | 内联对象，`composed` 是 `Call` 数组 | 调用单随 Parcel 一起落盘，是可审计的「这一步到底调了什么」 |
| `Parcel` | **目录**：`parcels/<station>/<digest[:16]>/` 下 `parcel.json` + `payload`（二进制，由 `payload_ref` 指向）+ `trace.jsonl` + `log.txt` | `parcel.json` **不含 payload 本身**，只含 `payload_ref`。把数组塞进 JSON 会让「内容寻址」退化成「按行号对齐」 |
| `Ticket` | **目录**：`tickets/<ticket_id>/ticket.json` | 只有 `status` 与 `archived_at` 可变；其余字段改写即 `E2005` |
| `Blueprint` | **文件**：`blueprints/<digest[:16]>/blueprint.json`，含 `origins` 逐叶子来源 | `origins` 必须覆盖全部叶子（`E1007`） |

三条往返规则（每类都要有对应测试）：

1. **往返恒等**：`from_dict(to_dict(x)) == x`，且 `compute_digest(to_dict(x))` 与原 `digest` 相同。
2. **未知键拒绝、缺必填拒绝**：分别是 `E1004` 与 `E2003`，都不补默认值。允许静默忽略的键，等于允许「写错也能跑」。
3. **可选键省略不写 `null`**。`null` 与「没有这个键」在语义上不同：前者是「有值且为空」，后者是「不适用」。序列化时统一用省略，这样 diff 里出现的 `null` 就一定意味着有人真的写了 `null`——那是需要解释的事。

---

## 6. S0 验收点（本篇）

| # | 断言 |
|---|---|
| D1 | 键顺序打乱、数字写法变化（`1` / `1.0`）、时间时区偏移，均不改变指纹 |
| D2 | 时间戳、耗时、路径字段改变，**不改变**任何指纹 |
| D3 | 改一个语义字段，`call_digest` 与 `parcel_digest` 同时变化 |
| D4 | `policy_digest` 在数据版本变化时**不变**，`blueprint_digest` 变化 |
| D5 | `NaN` 进入任何指纹输入 → `E2002` |
| D6 | 缺必填字段 → `E2003`，且不补默认值 |
| D7 | `Evidence` 里写 `pass=true` → `E2004` |
| D8 | 改写 `Ticket.plan_digest` → `E2005` |
| D9 | 同一内容写入两个不同目录 → 内容寻址路径相同，被视为同一版本 |
| D10 | 每个错误码至少有一个构造用例，且退出码映射正确 |
| D11 | 只改 `seed`（其余不变）→ `call_digest` 变化，缓存不命中 |
| D12 | `runtime_facts` 里的计数快照变化 → `call_digest` 变化；账本滚动数值本身不进 `policy_digest` |
| D13 | 在 `target.params` 里写 `horizon` → `E1020`，且错误信息指出应写在 `caliber.label` |
| D14 | 实现调用一个未在 `composes` 声明的能力 → `E2008`；声明链深度为 3 → `E2008` |
