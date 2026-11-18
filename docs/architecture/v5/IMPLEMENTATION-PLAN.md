# V5 研发计划（模块级）

> 状态基准：2026-11-16。`v5/` 27 个 py / 3336 行，S0 骨架完成，全量 871 passed。
> 排期只认 S，验收只认 AC。本计划**不引入任何新契约**，只把已冻结的 `12-stages.md` 与各 `s0/` 篇目拆成可执行的模块任务。
> 目录保持现状：`v5/` 为唯一 V5 代码根，CLI 在 `v5/app/`；V4 的 `core/` 保留到最后再处理。

---

## 0. 四条已定拍板（本计划的前提）

| # | 决定 | 落地影响 |
|---|---|---|
| 1 | V4 等 V5 完成再处理 | `core/` / `api/` 本轮零改动；不迁、不删、不复用其代码 |
| 2 | 所有 V5 代码在 `v5/`，CLI 在 `v5/app/` | 不建 `core/`；`app/cli.py` 保持位置，后续拆成 `app/cli/` |
| 3 | 数据九区在仓库外、可配置、默认 C 盘个人目录 | `store.py` 重写：`StoreLayout.root` 由 `FLOW_DATA_ROOT` 环境变量 / `operations.data_root` 配置解析，默认 `Path.home()/"factor-miner-v5"`；解析结果写入 Envelope 的 `data_root_resolved`（`s0/03` §7） |
| 4 | 能力按阶段填 | `capability/ops/` 12 项在 S1 建契约壳，真实实现随 S3/S4/S7 逐项填入 |

---

## 1. 模块总览矩阵

状态图例：**完整** = S0 验收覆盖；**骨架** = 接口在、行为简化；**空白** = 目录都不存在。

| 包 | 文件数 / 行数 | 当前状态 | 主要落地阶段 | 依赖 | 主验收 |
|---|---|---|---|---|---|
| `v5/contracts/` | 6 py + 1 json / 1998 | 完整（少量往返待补） | S1 补往返 | 无 | S0-1…S0-8 |
| `v5/facilities/` | 8 py / 460 | 骨架 | S6 补 F1/F5/F6/F7/F9 | contracts | F1–F10 |
| `v5/runtime/` | 5 py / 305 | 骨架（缺 envelope） | S1 真实化 | contracts, storage(S1) | C1–C22, AC1/3 |
| `v5/orchestration/` | 5 py / 311 | 骨架（dummy） | S1 真实 → S5 提交语义 | runtime, capability | O1–O11, AC4–8 |
| `v5/app/` | 2 py / 175 | 骨架（核心五条） | S1 补 contract/render → S5–S7 补业务 | orchestration | AC2, 09 §2.2 |
| `v5/capability/` | 0 | **空白** | S1 建壳 → S3/S4/S7 填实现 | contracts, facilities | AC20–28, AC31 |
| `v5/upstream/` | 0 | **空白** | S2 | storage, contracts | AC13–17, U1–U7 |
| `v5/storage/` | 0 | **空白** | S1 最小 → S6 GC | contracts | 09 §3, AC52 |

---

## 2. 地基先行：数据根配置（S1 第 1 件事）

这是所有落盘代码的地基，必须先于 `storage/` 和 `runtime/store.py`。

**任务**

1. 新建 `v5/storage/layout.py`：九区路径的唯一定义（`supply/ facts/ parcels/ runs/ db/ cache/ tmp/ baselines/ plans/ schemas/ catalog/`），按 `09` §3.2 + §3.4。任何模块不得自己拼这些字面量。
2. 重写 `v5/runtime/store.py`：`StoreLayout.root` 解析顺序 = 显式参数 > `FLOW_DATA_ROOT` > `operations.data_root` > `Path.home()/"factor-miner-v5"`。删掉现在的 `charter/ Depot/ tickets/ scratch/` 四个逻辑名当物理路径的写法（它们是逻辑分区，不是目录名）。
3. `v5/runtime/envelope.py`（新建）：环境快照 + 凭据三条禁令（`s0/03` §7）。`data_root_resolved` 是其中字段。
4. 测试：不同配置源优先级、默认路径落在个人目录、无凭据泄漏、九区字面量只有一处定义（AST 守卫）。

**验收**：`AC31` 的路径唯一定义检查；`s0/03` F10。

---

## 3. 逐模块详细计划

### 3.1 `v5/contracts/` — 完整，仅补落盘往返

| 文件 | 现状 | 待办 | 阶段 |
|---|---|---|---|
| `types.py` 825 行 | 七类结构 + 指纹 + 校验齐 | `Call` / `Parcel` / `Ticket` / `Blueprint` 缺 `from_dict`（现有 `Ref`/`Pin`/`Evidence` 已有）。Parcel 落盘需要双向往返 | S1 |
| `errors.py` 273 | 49 码齐 | 无 | — |
| `digest.py` 460 | 规范化 + 八指纹齐 | 无 | — |
| `plan_schema.py` + json | 结构层唯一真相 | 无（`params` 内部由注册表校验，不在此） | — |
| `exit_codes.py` | 齐 | 无 | — |

**验收**：七类结构 `from_dict(to_dict(x)) == x` 逐类通过；未知字段仍 `E1004`。

---

### 3.2 `v5/facilities/` — 骨架，行为简化

| 文件 | 现状 | 待办 | 阶段 |
|---|---|---|---|
| `clock.py` / `rng.py` / `digest_svc.py` | 完整（冻结时钟、种子派生、规范化） | 无 | — |
| `log.py` / `trace.py` | 内存实现，凭据拦截已测 | F1：AST 静态检查全局 `time`/`random`/`logging` 绕过；F9：trace 完整字段落盘 | S6 |
| `cache.py` | 命中校验已有 | F5：容量上限 + 淘汰；`node_cache` 默认关（实测净亏 65×，不改） | S6 |
| `notify.py` | 骨架 `emit` | F7：告警阈值与去重 | S6 |
| `__init__.py` | 注入容器 + strict 模式 | 补 F2 缺设施失败的完整用例 | S6 |

**验收**：F1/F5/F6/F7/F9/F10 全绿；`s6` 六类故障都能触发告警。

---

### 3.3 `v5/runtime/` — 骨架，最需要真实化

| 文件 | 现状 | 待办 | 阶段 |
|---|---|---|---|
| `compiler.py` 186 | 步骤 1–4 真实（复用 `validate_plan`）；5/9 部分检查；**6/7/10 占位**：`fingerprints` 全是 `sha256:pending`、`_merge` 是浅合并、无来源表逐叶子 | 十步全量：深合并 + 来源表（每个叶子标 `plan`/`baseline`/默认）+ 四锚点真公式（`plan_digest`/`blueprint_digest`/`policy_digest`/`domain_digest`）+ 真实别名解析 + `C1–C22` 全量 | S1 |
| `charter.py` 24 | 只做 JSON 加载 + 缺文件 `E1008` | 基线/计划/别名/目录四类加载；基线单文件不做 preset 继承（Q3 已收敛） | S1 |
| `gate_b.py` 35 | 只查请求起点是否在覆盖内 | 四项只读适用性检查（`s0/03` §8）：版本覆盖、字段清单、时间轴、未收盘 | S1 |
| `store.py` 36 | **路径错**（见 §2） | 按 §2 重写；补四区权限矩阵（写入即校验，`s0/03` §1） | S1 |
| `envelope.py` | **不存在** | 按 §2 新建 | S1 |

**验收**：`AC1/3/5/7/10/11/12`；`flow explain` 逐叶子可解释；十次重放指纹逐位一致。

---

### 3.4 `v5/orchestration/` — dummy 完成，接真

| 文件 | 现状 | 待办 | 阶段 |
|---|---|---|---|
| `station.py` 43 | 八行 `StationSpec` 齐 | 真实 `inputs` 依赖图（现在是线性链）；`default_impl` 指向真实现名 | S1 |
| `executor.py` 144 | dummy 产物；`call_digest` 只含 ticket/station/seq；`params_digest` 是 `compute_digest({})`；事件 `family_v1` 硬编码 | 真实调用单六通道进 `call_digest`；Parcel 落盘 + 指纹链；真实依赖校验；`--probe`/`--commit` 分离；断电恢复不重复计数/揭示/发布 | S1 → S5 |
| `state.py` 50 | 状态机基础转移 | 非法转移 + 失效传播 + 单步污染防护 | S1 → S5 |
| `events.py` 58 | Reveal/Budget/Publish 一次性内存记录 | 落全局账本（`db/ledger.jsonl`）；重复触发拒绝 | S5 |

**验收**：`O1–O11`、`AC4–AC8/19`；probe 后四项（计数/事件/状态/结论）逐项不变。

---

### 3.5 `v5/app/` — 核心五条在，业务十一条缺

| 文件 | 现状 | 待办 | 阶段 |
|---|---|---|---|
| `cli.py` 167 | 核心五条 + `--json` 信封 + 退出码推导，单文件 | 拆成 `app/cli/` 逐命令一文件；新增 `contract.py`（命令名/参数 schema/输出 schema/mutation 等级唯一定义）；新增 `render/`（table/json/ndjson） | S1 |
| `doctor` / `estimate` / `refresh` / `pipeline` | 无 | 供给与体检命令 | S2 / S6 |
| `evaluate` / `factors` | 无 | 调试与候选查询 | S3 |
| `backtest` | 无 | 单次回测 | S4 |
| `promote` / `factor_db` / `holdout` / `certify` | 无 | 晋升、库管理、揭示、认证发布 | S7 |

**命令面唯一清单**：`09` §2.2（核心五条 + 业务十一条）。`experiment` 是 `run` 的别名，保留一版后删；`pipeline` 只是 `refresh → doctor → run` 串联。

**验收**：16 条命令逐条声明 mutation / 产 Ticket / 退出码；`--json` 信封 `ok` 与 `exit_code` 恒一致。

---

### 3.6 `v5/capability/` — 全空白，最大缺口

**S1：建契约壳（12 项签名 + `NotImplemented` / `E3004`）**

| 文件 | 职责 | 真实实现阶段 |
|---|---|---|
| `registry.py` | 名字 → 实现@版本；四档名字空间（open/closed_multi/single/fixed）；加载即自检（`E1015`） | S1 壳 → S3 真 |
| `spec.py` | `describe()` 元信息 + `param_schema` 校验 + `policy_reads` 声明 | S1 |
| `ops/read.py` | 只读已发布版本，字段清单解析 | S2 |
| `ops/boundary.py` | 两张掩码（observable/tradable）+ `reason_codes` | S3 |
| `ops/align.py` | 面板对齐、`causal_report` / `quality_report` 完整结构 | S3 |
| `ops/label.py` | 跨度/变换/中性化/`overlap_policy`（三处自研算法之一） | S3 |
| `ops/search.py` | 四方法 `describe/fit/generate/lineage`；`fitness` 闭包；多算法一次计数 | S3 |
| `ops/observe.py` | 零分布构造、时间轴置换、分层漏斗（自研之二） | S3 |
| `ops/verdict.py` | DSR + BH/BY + Holm 8 行 + 唯一裁判 | S3 |
| `ops/combine.py` | 合成、三段窗口、成员可用时间 | S7 |
| `ops/allocate.py` | 分位/约束两条构造路径（自研之三） | S4 |
| `ops/evaluate.py` | qweave 求值；JSON AST → `Expr` 构造器；算子白名单；`values_ref` 唯一暴露来源 | S3 |
| `ops/backtest.py` | ml4t 引擎；`summary`/`full` 同源 Ledger；`caliber.net` 注入 | S4 |
| `ops/certify.py` | walk-forward、holdout 揭示、可复现清单 | S7 |

**注册表数据**（`v5/capability/registry/`）：`fields.json` / `operators.json`，以 qweave 0.6.2 算子表为基线删去前视构造；`operators_version = sha256(canonical(operators.json))`。

**五条结构禁令必须机器检查**（`s0/04` §6）：不评估 / 不判显著 / 不写计数 / 不自选口径 / 不自建子调用——靠签名与类型里没有位置，不靠代码评审。`AC31` 扩展为：能力层不得 import `duckdb`/`pyarrow`/拼路径。

---

### 3.7 `v5/upstream/` — 全空白，S2 一次做完

| 文件 | 职责 | 验收 |
|---|---|---|
| `acquire.py` | ccxt-only 采集、分页、缺口判定、只写 `staging/`；租约 U1 | U1/U2/U3 |
| `gate_a.py` | 采集侧门禁 A：校验和、覆盖率、字段清单、未收盘判定（`E4003`） | AC13–17 |
| `publish.py` | 原子发布 + 内容寻址 + 别名移动；`acquire` 与 `publish` 分开 | U4/U7 |
| `lease.py` | 租约获取/续期/过期，两进程互斥 | U1 |

**红线**：`acquire` 不得移动别名；`publish` 不得触发采集；同内容重复发布不重写、不改 `acquired_at`、只追加日志。

---

### 3.8 `v5/storage/` — 全空白，S1 最小 + S6 完整

| 文件 | 职责 | 阶段 |
|---|---|---|
| `layout.py` | 九区路径唯一定义（见 §2） | S1 |
| `objects.py` | parquet 读写 + 内容寻址 | S1 |
| `ledger.py` | append-only jsonl，全局单调真相 | S1 基础 → S5 完整 |
| `index.py` | duckdb 查询面，可重建（真相丢失后能从 parquet+jsonl 重建） | S6 |
| `gc.py` | 引用计数 + dry-run | S6 |
| `reconcile.py` | 索引与真相/对象对账 | S6 |

只有事实层与供给链可以 import 存储层；能力层禁止（AST 守卫）。

---

### 3.9 `tests/` — 按包归位

新建 `tests/v5/`（现在 6 个 `test_v5_*` 平铺在根）：

| 已有 | 待补（阶段） |
|---|---|
| `test_v5_contracts` / `test_v5_plan_schema` / `test_v5_facilities` / `test_v5_orchestration` / `test_v5_package_isolation` / `test_v5_s0_acceptance` | `test_v5_storage`(S1) / `test_v5_runtime_real`(S1) / `test_v5_capability`(S1–S7) / `test_v5_upstream`(S2) / `test_v5_app_commands`(S1–S7) |

测试基线演进：871（S0）→ S1 约 +40 → S2 约 +30 → S3 约 +50 → S4 约 +30 → S5 约 +25 → S6 约 +20 → S7 约 +35。

---

## 4. 阶段任务清单

| 阶段 | 核心任务（按依赖顺序） | 退出门槛 | 禁令 |
|---|---|---|---|
| **S1** | ① 数据根+layout+store+envelope → ② compiler 十步全量+charter+gate_b → ③ storage 最小(objects/ledger) → ④ capability 12 壳+registry/spec → ⑤ executor 真实化 → ⑥ app 拆分+contract/render | AC1/3–5/7/10–12；植入信号通过、纯噪声拒绝、符号翻转结论翻转；十次重放逐位一致 | 不许报告统计显著性 |
| **S2** | upstream 四件（acquire/gate_a/publish/lease）+ ccxt-only DataLoader + refresh/doctor/estimate | AC13–17；断网/超时/半途失败不出半份版本；端到端从空目录到可引用版本 | 不许扩标的池 |
| **S3** | evaluate(qweave)+observe(零分布)+verdict + 算子/字段注册表 + Budget + search(deap) + label/boundary/align | AC20–23/29/37–39；与 V4 暴露逐位对拍；纯噪声 `empirical_p` 近均匀 | 不许说「找到 alpha」 |
| **S4** | backtest(ml4t)+allocate + `build_backtest_config(calendar="crypto")` + 两档同源 Ledger | AC9/24/26/28/33/34；年化比值 = √(365/252)=1.2035；full 聚合=summary 逐项 | 不许无人值守 |
| **S5** | `--probe`/`--commit` + 状态机推进 + 事件落账本 + 断电恢复 | AC4–8/19；probe 后四项不变；commit 不触发事件；kill 恢复不重复 | 不许用重放救结论 |
| **S6** | facilities F1/F5/F6/F7/F9 + storage index/gc/reconcile + doctor 真实检查 + 告警 | AC51–53；连续 7 天无「成功但无产物」；GC 按引用计数回收；六类故障有修复建议 | 不许放宽告警换安静 |
| **S7** | combine/certify + 组合域 + 三段窗口 + walk-forward + holdout plan/reveal + 业务命令齐 | AC18/19/25/27/30；组合样本内不得发布；holdout 只揭示一次；认证与组合同 `caliber_digest` | 不许调门槛 |

**最小可信纵切 = S0 → S4**。到 S4 才能说「净成本后可交易」这句话成立。AC63（已知答案对拍：黄金面板上 `mom60@5bar` 与独立复算 ≤1e-9 相对，2026-11-17 起不依赖 V4）在 S7 结束时与全部 AC 一并判定。

---

## 5. 关键依赖与不可并行项

```
数据根+layout+store+envelope (§2)
        │
        ├──▶ compiler 十步全量 ──▶ gate_b ──┐
        │                                    ├──▶ executor 真实化 ──▶ S5 提交语义
        └──▶ storage objects/ledger ─────────┘
                    │
capability 12 壳(S1) ──┼──▶ S2 upstream（只读 facts）
                    ├──▶ S3 evaluate/observe/verdict/search
                    ├──▶ S4 backtest/allocate
                    └──▶ S7 combine/certify
```

- **S2 与 S3 可并行**（`12-stages.md` §10），但两者都依赖 S1 的 storage 最小集。
- **S5 必须等 S1 的 executor 真实化**，不能在 dummy 上做提交语义。
- **能力层 12 壳必须在 S1 建齐**，否则 S3 起各工位会各自造调用形状，C20 的结构保证失效。

---

## 6. 明确不做（本轮）

- 不建 `v5/core/`；不迁 V4 的 miner/backtest/evaluation 代码进 V5。
- 不改已冻结的 28 篇文档中的任何契约；本计划只做排期，不新增 I/AC/错误码。
- 不做 Web（预留契约即可）、多机部署、跨地域复制。
- 不接全市场标的池（S2 明确禁止）；不自动调 DSR 门槛（`passed=0` 是结论不是故障）。
- 不启用节点级磁盘缓存（实测净亏 65×，`node_cache` 默认关）。

---

## 7. 下一步

从 §2「地基先行」开始：重写 `store.py` + 新建 `layout.py` / `envelope.py` + 相应测试。这一步做完，后面所有落盘代码才有唯一路径源。
