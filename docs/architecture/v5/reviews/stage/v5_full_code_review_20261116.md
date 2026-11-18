# V5 全面代码审查报告（S1 完成时点）

日期：2026-11-16 22:54
范围：`v5/` 全部 Python 代码 6274 行（contracts 2198 / facilities 751 / runtime 973 / orchestration 685 / capability 457 / storage 587 / app 608），含当日刚加的日志全局访问器；另抽查 `tests/v5/` 与 `docs/architecture/v5/` 的一致性。
基线：`docs/architecture/v5/reviews/stage/v5_s1_code_review_20261116.md`（上轮已修 2 P0 / 19 P1 / 4 P2）；本轮全量 990 passed、v5 239 passed，四模块并行审查 + 主审实测复核。
结论：**有条件通过**。新增 3 P0（均在主审实测中复现）、10 P1、若干 P2。**当前 990 全绿不成立为"可交付"证据**——P0-1/2 是安全闸门被绕过，P0-3 是 CLI 执行不落盘却报成功。

---

## 0. 修复状态（2026-11-16 23:05 更新）

**三项 P0 已全部修复并回归验证**；P1/P2 状态不变，仍按本报告后续章节处理。

| P0 | 修复 | 回归测试 | 验证 |
|---|---|---|---|
| P0-1 深度 >8 静默放行 | `_forbidden_keys` 取消深度截断，改用 visited id 防自引用（同一容器只扫一次），无深度上限 | `test_deeply_nested_secret_is_rejected_not_written`（9/16 层） | 均 `E2004`，jsonl 不生成 |
| P0-2 嵌套 list 穿透 | Mapping / list / tuple / set / frozenset 的元素统一走同一 `walk()` 递归，不再只判直接 Mapping | `test_nested_list_secret_is_rejected_not_written`、`test_self_referential_ctx_terminates_and_still_scans` | `E2004`；自引用不死循环且能扫到环内 `api_key` |
| P0-3 CLI 不落盘 | `run.py` 组合根构造 `TicketWriter(Layout(resolve_data_root()), ticket_id)` 并传入 Executor；sink 与 writer 共用同一 layout | `test_cmd_run_persists_parcels_and_traces` | 信封计数 == 磁盘文件数 |

实跑复核：`flow run --plan tests/fixtures/v5/plan_valid.json` 信封 `ok:True, exit:0, parcels:8, traces:8`；磁盘 8 份 `parcel.json` + 8 行 `trace.jsonl` + 2 行 `process.jsonl`，逐项一致。
测试：相关 33 passed，全仓库 **994 passed**（原 990 + 新增 4 项），80 subtests。
改动文件：`v5/facilities/log.py`、`v5/app/cli/run.py`、`tests/v5/test_v5_logging.py`、`tests/v5/test_v5_app_commands.py`。

### 0.2 P1 修复（23:15–23:30）

按放行条件第 2 条，P1-3/4/5/8/9/10/11/12 同批修复；**已修 8 项**：

| P1 | 修复要点 | 文件 |
|---|---|---|
| 3 | `__post_init__` 对 `self.keys` 做 `_forbidden_keys`（bind 派生时统一校验），密钥不能再从 keys/bind 路径落盘 | `facilities/log.py` |
| 4 | `_json_default` 改为先 `tolist()` 后 `item()`，numpy 多元素数组 / pandas Series 不再被 ValueError 误杀 | 同上 |
| 5 | 关联键与 ctx 同名即抛 E2003（不是静默覆盖），需新增关联键走 `bind(extra)` | 同上 |
| 8 | notify 白名单键的值必须是标量，嵌套结构直接 E2004（比递归扫凭据更严格） | `facilities/notify.py` |
| 9 | `Cache.invalidate` 补 `writable` 检查，station/probe 只读作用域不能删缓存 | `facilities/cache.py` |
| 10 | executor 兜底 `except Exception` → E3004 并走同一 failed-trace 路径；补 `E3004` 导入 | `orchestration/executor.py` |
| 11 | registry 自检 min/max 双向校验（含 min≤max），`center` 改真值判断；不强制两界都在（11 个无参算子 domain 为空） | `capability/registry.py` |
| 12 | `spec.describe()` 改为逐能力数据表：`allocate.serves` 补至三处、`side_effect` 支持 `append_only`、`composes` 按 §1.3 填、`policy_reads` 限 caliber 四子块并加结构自检 | `capability/spec.py` |

验证：相关测试全过，全仓库 **1003 passed**（原 994 + 新增 9），80 subtests；实跑 `flow run` 信封 `ok:true/parcels:8/traces:8`，磁盘 8 parcel + 8 trace 行，无回归。

**未修 / 待裁决（本轮未动）**：

- **P1-1 gate_b**：两项判据读的 `pin.gate_a["symbols"]`、`plan.supply.max_age_days` 在契约里不存在；`Pin` 只有 `symbols_digest`（标的集合指纹），但文档未定义「从 plan 标的清单算 symbols_digest」的公式，`plan_schema.json` 的 `supply` 也是 `{}`。`check_gate_b` 全包零调用点，compiler 第 9 步只做别名解析。**这是契约缺口而非代码 bug**——需先裁决：① plan 侧标的集如何表达（清单 / 直接给 digest）；② 新鲜度基准来自何处（plan 声明字段 or 时间轴末端）；③ 是否为 S2 接线同步改 compiler。裁决前不动代码，避免自造摘要算法成为第二套真相。
- **P1-2 E5002**：文档有四处冲突用法——`s0/01 §4.2`/`s0/06` 是租约冲突，`s0/03 §7.2`/R13 是凭据缺失，`s0/05 §8.2`/O15 是 index 并发版本冲突。拆码位会改错误码契约，需裁决后再动。
- **P1-6/7 + P2 首项**：全局访问器形态需裁决：当前 `get_log()` 生产零调用点、`ContextVar` 子线程不继承、F1 静态检查未实现。保留则补线程边界与 AST 守卫并让 executor/能力层真用 `get_log()`；不保留则收敛机制并回退文档 §2.1 例外段落。

---

## 1. P0（必须先修；已于 23:05 全部修复，详见 §0）

### P0-1　日志凭据闸门：深度 > 8 时静默放行，密钥明文落盘
`v5/facilities/log.py:261-262`（配合 254-274）。`_forbidden_keys` 超过深度上限时 `return set()` 而非拒绝，构造 9 层嵌套即可绕过 E2004。
**实测**：`_forbidden_keys(nest(9, {"api_key":"sk-DEEP"})) == set()`；端到端 `logger.event(...)` 成功，jsonl 中明文含 `sk-DEEP`。
修复：`depth > 8` 改为抛 `E2004`；或用 `id(seen)` 检测循环引用后取消深度限制。**判定：已确认 bug**（违反 F3 / §2.4）。

### P0-2　日志凭据闸门：嵌套 list 不递归，密钥明文落盘
`v5/facilities/log.py:270-273`。list/tuple/set 分支只对直接元素判 `isinstance(item, Mapping)`，`list of list of dict` 穿透。
**实测**：`_forbidden_keys({"a":[[{"api_key":"sk-LIST"}]]}) == set()`；端到端写入含明文。
修复：把 Mapping / list / tuple / set 统一成一个递归入口。**判定：已确认 bug**（与 P0-1 是两条独立绕过路径，须一并修）。

### P0-3　`flow run` 不写任何产物，但信封报成功且计数 8
`v5/app/cli/run.py:46-52`（Executor 构造无 `writer`）；`executor.py:221,400,414`（`writer is None` 时 `_persist` / `_write_trace` 直接 return）。
**实测**：临时数据根跑 `flow run`，产出文件只有 `runs/<ticket>/logs/process.jsonl`；无 `parcels/`、无 `trace.jsonl`，而 stdout 信封 `ok:true, parcels:8, traces:8`。`contract.py:81` 却声明 run 为 `writes_facts` + `produces_ticket=True`。这直接违背 I50「跑过没有只由结构化记录回答」。
修复：组合根用 `StoreContext.from_root(resolve_data_root()).for_ticket(ticket_id)` 构造 writer 传入 Executor；若确属 S1 边界，则把契约元数据与信封计数改为如实描述。**判定：已确认 bug**（不是阶段归属可解释——writer 参数存在且在 `test_v5_executor_real.py:145` 被测试使用，只是 CLI 没接）。

---

## 2. P1（下一阶段接线前必修）

| # | 问题 | 文件:行号 | 判定 | 最小修复 |
|---|---|---|---|---|
| 1 | gate_b 的「标的集」「新鲜度」两项结构性恒 PASS（读不存在的 `pin.gate_a["symbols"]`、`plan.supply.max_age_days`），且整个函数无调用点，还自取 `datetime.now()` 违反 E2007 | `runtime/gate_b.py:64,76,78`；`compiler.py:200-214` | 已确认（属 S2，但接线后仍 fail-open） | 接入编译；标的集读 `symbols_digest`；新鲜度改纯函数比对 plan 时间轴 vs pin 覆盖 |
| 2 | E5002 一码两义：错误表定义「租约冲突」，`envelope.py` 用作「凭据缺失」；两份文档互相冲突 | `contracts/errors.py:138`；`envelope.py:155`；`s0/01 §4.2` vs `s0/03 §7.2` | 需确认意图 | 拆码位并同步两份文档 |
| 3 | `self.keys` 与 `bind(extra)` 不做凭据扫描，密钥写入每条日志 | `log.py:226,236-245,199-206` | 已确认（实测明文落盘） | `bad = _forbidden_keys(ctx) \| _forbidden_keys(self.keys)`；bind 同样校验 |
| 4 | `_json_default` 先 `item()` 后 `tolist()`，numpy 数组 / pandas Series（size>1）被 ValueError 误杀 | `log.py:77-80` | 已确认 | 调换分支顺序，或对 size>1 跳到 tolist |
| 5 | `ctx` 在 `self.keys` 之后展开，调用方可覆盖 `ticket_id`/`station`/`call_digest`，日志归属串线 | `log.py:237-245` | 已确认（实测覆盖成功） | 改 `**ctx, **self.keys`（关联键优先） |
| 6 | `ContextVar` 跨线程不继承：子线程 `get_log()` 直接 E2007，与注释「多线程隔离」承诺冲突 | `log.py:302-303,305,321-334` | 已确认（实测） | 明确「仅主线程 / asyncio context 可用」，或提供 `copy_context()` 传播辅助 |
| 7 | F1 静态检查未实现：全仓无 `import time/random/logging` 的 AST 守卫，`get_log()` 零业务调用点，F1 目前 paper-only | `02-crosscutting.md:294` vs `tests/v5/test_v5_package_isolation.py:17-86` | 已确认 | 补 AST 测试并显式豁免 facilities 内部；重新评估全局访问器的必要性 |
| 8 | notify 白名单只查顶层键，`{"status":{"api_key":"sk-X"}}` 放行 | `notify.py:41` | 已确认（实测） | 递归扫描，或强制允许键的值只能是标量 |
| 9 | cache `station`/`probe` 可 `invalidate`（`put/put_node` 检查 writable，唯独 invalidate 不检查） | `cache.py:71-72` | 已确认（实测） | invalidate 开头加 writable 检查 → E2001 |
| 10 | 失败 trace 只捕获 FlowError；能力抛 `NotImplementedError`/`ValueError` 时无 failed trace（且 CLI 无 writer 时即便捕获也写不出） | `executor.py:329-368` | 已确认 | 兜底 `except Exception` 映射到 E3xxx 错误码再走同一 failed-trace 逻辑 |
| 11 | registry 自检只读 `param_domain.min`，`{"lag":{"max":-3}}` 的负位移 / `{"window":{"max":0}}` 绕过 E1015；`center:1` 也绕过 | `registry.py:60,71-90` | 已确认（实测注入通过） | min/max 双向校验且至少给其一；center 改真值判断 |
| 12 | `spec.describe()` 元信息与冻结契约系统性不符：`allocate.serves` 少两个调用点；`side_effect` 全 pure 且 `_VALID_SIDE_EFFECTS` 不含 `append_only`；`composes` 全空；`policy_reads=("policy_view",)` 非合法口径子块 | `capability/spec.py:16-48,59-91` | 已确认（实测） | 改为逐能力数据表；加 spec vs 设计清单一致性单测 |

> 第 10/11/12 项在上一轮报告 §3 中被归入 S3 阶段，本轮给出的是**新证据**（具体可复现的绕过方式与字段清单），不是重复报同一问题。

---

## 3. P2（排期处理 / 需裁决）

- **本次日志改动的架构收益存疑**：`get_log()` 在生产代码零调用点，run.py 自己用 `fx.log.info()`；`install_log/reset_log` 是空转机制。且全局只装一个 ticket-scope logger，能力层若用它记 station 日志会缺必填关联键。**需裁决**：让 executor/能力层真正经 `get_log()` 输出，或收敛此例外并回退文档段落。（`log.py:298-337`；`run.py:34,55`）
- `process.jsonl` 是第三种日志约定：executor 落 `parcels/<seq>-<station>-<hex>/log.jsonl`，`layout.log_file` 定义 `<call_digest[:16]>.jsonl`（无生产调用点），本次新增 `runs/<ticket>/logs/process.jsonl`。需在文档区分运行级过程日志与工位日志。`（run.py:30-32；layout.py:134-146）
- `reset_log` 在 `fx.log.close()` 之前、`install_log` 在 try 之外：当前无泄漏窗口，但顺序脆弱且 finally 二次抛错会掩盖原始异常。（`run.py:34-56`）
- `StructlogLogSink.close()` 后 `write()` 会 `_ensure()` 重新打开文件追加，与 `Logger.closed` 语义不一致。（`log.py:113-137,154-159`）
- `build_facilities` strict 只查非 None，`overrides={"digest": object()}` 可通过；`log_keys` 可覆盖 ticket_id 造成归属不一致。（`facilities/__init__.py:83,92-105`）
- `notify.timeout_seconds` 定义但未实现（F7 未落地）；cache 命中校验失败未记 warn（§7 要求）；`extra_processors` 无法从 build_logger 传入。（`notify.py:36`；`cache.py:41-64`；`log.py:94-104`）
- `Evidence.to_dict` 可选字段写 null、`Ticket.from_dict` 把 `archived_at` 当必填，与 `s0/01 §5.1` 往返规则冲突。（`types.py:450,732,766`）
- `compiler.CALIBER_FIELDS` 是 JSON 之外的第三份硬编码真相（当前内容一致，有漂移风险）。（`compiler.py:43-50`）
- app 层：`E3004` 错误对象硬编码 layer/retryable（应由 code 推导）；`require_confirmation` 守卫定义但分派前未调用；`data_keys` 与 09 §2.4 全面不符且无消费者；NDJSON 含 list 时不输出信封行。（`cli/__init__.py:96-100,92-93`；`contract.py:74-98`；`render/ndjson.py:11-25`）
- 文档 `s0/02 §2.1` 接口写 `log.bind(scope,keys)` / `logger.child()`，实现是 `build_logger()` / `logger.bind(extra)`。（`02-crosscutting.md:52-57`）
- "九区"实为 11 个物理目录名（命名口径，非路径错误）。（`layout.py:18-21`）
- `resolve_impl` fixed 档错误码 E2003 vs 设计 E1003；closed_multi 未校验条目数 > 1。（`registry.py:139-153`）
- `StationSpec.inputs` 是纯名字串而非 `{station,key,required}`，且无拓扑序检查。（`station.py:17,69-86`）

**上轮报告 §3 的 S5/S6 未修项本轮复核仍在原位**：inspect 恒返回空 records；ledger 短写/坏行/无跨进程锁；index 重建非事务且只比行数；`layout.parcel_dir` 与 executor 实际路径不一致（且 executor 直接拼 `parcels/` 字面量违反 AC31）。按阶段归属，不计入本轮新增。

---

## 4. 已复核为正确（避免误改）

- 八个指纹公式 kind 包装、pin 白名单投影、`acquired_at` 不进蓝图、phase/seed 进 call_digest；49 个错误码无重复悬空；plan_schema JSON 与结构层 25 字段一致。
- 上轮 2 P0 + 19 P1 修复均未回归（ticket_id 单段校验、路径逃逸、对象索引 digest 前缀、缓存内容校验、覆盖层路径、policy 排除 impl/seed 等）。
- WriteLogger 逐条 flush 属实（structlog 26.1.0 源码确认）；frozen_clock 时区校验、derive_seed 公式、cache F5、notify F10、log_path 去冒号均正确。
- 跨层 import 守卫：`v5/` 无 `import core/api/flow`；app 仅依赖下层。
- **审查中剔除的误报**：`Evidence(name="score"/"rank"/"weight")` 实测被 E1004 拦截（agent 初报"放行"是构造参数用错导致的 TypeError），文档白名单要求满足。

---

## 5. 放行条件

1. 先修 3 项 P0 并补回归（日志闸门两条绕过路径 + CLI writer 接线），重跑全量。
2. P1-3/4/5/8/9/10/11/12 同批修；P1-1（gate_b）在 S2 接线前完成重构；P1-2（E5002）请先裁决码位。
3. P1-6/7 与 P2 首项需先裁决全局访问器的形态：保留则补线程边界与静态守卫、让 executor/能力层真的用 `get_log()`；不保留则收敛机制并回退文档。
4. 修完后补三类测试：凭据深层/嵌套 list 不得落盘、CLI run 落盘与信封一致、gate_b 四项都能真拦。

在上述 1-2 完成前，不应把「990 passed」当作 S1 可交付的依据——全绿套件恰好缺少这几条断言。
