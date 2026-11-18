# 模块审查 55 · `facilities/notify.py`

> 审查日期：2026-11-18
> 依据：源码 109 行全文通读；被 `app/cli/pipeline.py:116-119`（dedup_key=alert kind）消费；设计 `s0/02-crosscutting.md` §8、F7/F10、`15-operations.md` §3（告警阈值）、I52（阈值改判据调整）；测试 `tests/tests/test_v5_facilities.py`（`test_build_notifier_is_single_f7_configuration` 等 23 例）、`tests/tests/test_v5_logging.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_facilities.py` 的 F-notify 段。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`notify.py` 是执行结束后的外部通知设施。它该做的：

1. **唯一允许失败但不影响结论的设施**（docstring:3-4）：它不参与任何判定；失败写进 trace 附加字段，不改变退出码、不改变结论。这个例外必须显式写在这里，防止被当成普遍先例。
2. **F7 告警阈值 + 去重限流**（用户必查点）：唯一构造入口 `build_notifier()` 固定告警口径——**去重窗口 3600s / 每窗 10 条**（:36-45）。阈值写在 dataclass 默认值里会与 `Notifier(scope=...)` 的"无去重"默认混在一起（同一类两种口径），所以收敛到 `build_notifier` 一处。
3. **允许载荷白名单**：只允许锚点指纹、status、error_code、seconds（:20-23）；禁止产物内容与凭据（E2004）；载荷值必须是标量（防 `{"status":{"api_key":...}}` 穿透，P1-8）。
4. **作用域护栏**：station/probe 禁止通知（F10，E2001，不是静默忽略）。

**去重机制**（:82-96）：
- `dedup_window_seconds>0`：同一 `dedup_key`（缺省=event）在窗口内只投一次，重复返回 `NotifyResult(delivered=False, deduped=True)`——"静默重复只会训练人忽略告警"。
- `max_per_window>0`：滑动窗口内最多 N 条，超出抑制（洪泛保护），记 trace note。
- 阈值只能改判据调整（I52），不许为"减少噪音"静默放宽（15-operations §3："告警不许静音"）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `fm_probe_facilities.py` F-notify 段。

### 例 1 · 告警阈值 + 去重限流（用户必查点，实测）

```
build_notifier dedup_window = 3600.0
build_notifier max_per_window = 10
raw Notifier defaults (window,max) = (0.0, 0)          # 直接构造无去重
same event twice -> (delivered,deduped) = [(True, False), (False, True)]
after window (+3601s) -> delivered = (True, False)
12 emits delivered count = 10
throttled trace_notes count = 2
station notify -> E2001
payload blob -> E2004
payload secret -> E2004
sink failure -> delivered=False, error recorded, exit unaffected = (False, True, 1)
```

**解读**：
- **阈值（用户必查点）**：`build_notifier` 实测 `dedup_window=3600.0`、`max_per_window=10`——S6 冻结的告警口径（15-operations §3 阈值表的去重/限流实现）；直接 `Notifier(scope=...)` 默认 `(0.0,0)` 无去重，两者刻意不同，`build_notifier` 是唯一告警口径入口（F7）。
- **去重**：同一 event 连发两次，第一次 delivered、第二次 deduped=True 被抑制；窗口 +3601s 后再次投递成功。
- **限流**：12 条不同 event，只投递 10 条（每窗 10 上限），2 条被抑制并记 trace note（洪泛保护）。
- **护栏**：station 作用域 E2001、载荷含结构/凭据 E2004。
- **失败不影响结论**：sink 抛异常时 `delivered=False`、error 记录、trace note +1，退出码不变（唯一允许的例外）。

### 失败路径（实测）

| 非法输入 | 错误码/结果 |
|---|---|
| `build_notifier('station').emit(...)` | E2001 |
| payload 含 `token`/`secret` 等 | E2004 |
| payload 值非标量（dict/list） | E2004 |
| sink 抛异常 | delivered=False, error 记录, 退出码不变 |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| N-1 | **告警阈值 + 去重（用户必查点，正面）**：3600s/10 条在 `build_notifier` 单点固定，去重/限流/sink 失败实测通过。 | —（正面） | F-notify 实测 + `notify.py:36-45,82-96` | **已确认符合设计**。无 P0/P1 缺口。 |
| N-2 | 告警阈值（3600s/10 条）是硬编码常量，不是配置化——15-operations §3 的阈值表（数据新鲜度 1 bar、覆盖率 0.95、缺口 2 bar 等）不在此模块，而在各检测点。 | P2（说明性） | `notify.py:44-45`；`15-operations.md:53-67` | **已确认符合设计**：F7 的去重/限流阈值是"防止告警风暴"的传输层参数，与 §3 的业务告警阈值（何时触发告警）是两回事。I52 要求阈值改判据调整——改这里须走判据流程，不能当运维旋钮。 |
| N-3 | `clock` 默认 `time.monotonic`（:62）——设施层自取时间。F1 禁止模块级自取时钟，但用 callable 默认值 + 测试可注入，且 monotonic 是设施内部计时非业务时间。 | P2（F1 边界） | `notify.py:62`；F1 原文 | **需确认意图**：默认 `time.monotonic` 是函数引用（非调用结果），组合根/测试可注入冻结钟（docstring:61 "测试注入冻结时钟"）。与 clock.py"实例级隔离"略有张力，但 notify 是设施自身计时，F1 主要针对业务层绕过注入。低优先。 |
| N-4 | `trace_notes`（限流/失败的 note）是内存 list（:52），落盘由调用方/pipeline 处理；note 不含去重抑制（deduped）的记录——去重只返回标志，不写 note。 | P2（可观测性） | `notify.py:52,88,95,105` | **需确认意图**：限流/失败有 note、去重无 note（只 deduped 标志）。若运维想知道"哪些告警被去重掉了"看不到。低优先，去重本身是正常抑制非故障。 |

**多了什么**：无冗余。`_ALLOWED_PAYLOAD_KEYS`/`_SECRET_KEYS`/`NotifyResult`/`build_notifier` 各有明确职责。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 告警去重 + 滑动窗口限流 | 采纳 | 3600s/10 条防告警风暴；"静默重复训练人忽略告警"——加密 24/7 高频运行尤需 |
| 唯一构造入口固定口径（build_notifier） | 采纳 | 避免 `Notifier(scope=...)` 无去重默认与告警口径混成"同类两种口径"（F7） |
| 允许载荷白名单 + 标量约束 | 采纳 | 只发锚点指纹/状态/错误码/耗时；嵌套/内容/凭据全挡——通知不是数据通道 |
| 告警失败 fail-closed（当错误上报） | 拒绝（唯一例外） | 通知不参与判定，失败写 trace note 不改退出码；这个例外必须显式（docstring:3-4） |
| 为"减少噪音"静默调松阈值 | 拒绝（I52） | "告警不许静音"——调松等于取消检查；改阈值须走判据流程 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **告警阈值 + 去重限流（用户必查点）**：3600s/10 条实测；去重（同 event 抑制）、限流（12 投 10）、窗口后恢复投递全通过。
2. **唯一口径入口**：`build_notifier` 固定阈值，`Notifier` 默认无去重——实测两者不同，F7 单点定义（s0/02 §9 明确"3600s/10 只有这一处定义"）。
3. **作用域护栏**：station/probe E2001（实测 F10）。
4. **载荷白名单 + 标量**：未知键/凭据/嵌套结构全 E2004（实测，P1-8 回归）。
5. **失败不影响结论**：sink 异常 → delivered=False、error + trace note、退出码不变（实测，唯一允许例外）。
6. **F7 语义**：deduped=True 且 delivered=False 不算失败（:31-33），避免把"已发过"误报成故障。
7. **不影响指纹**：通知载荷只有锚点指纹/状态/耗时，通知行为不进任何产物指纹——横切设施红线（U2 D1）。

**发现的问题**：
- N-2（阈值硬编码 vs 配置化）、N-3（默认 time.monotonic 的 F1 边界）、N-4（去重无 note）——均 P2 说明性/需确认意图，无 P0/P1 级 bug。测试 `test_build_notifier_is_single_f7_configuration` 钉住唯一口径。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/02-crosscutting.md` §8 | F7/F10 与实现一致 | 无需更新 |
| `15-operations.md` §3 告警阈值表 | 业务告警阈值（新鲜度/覆盖率等）与 notify 去重阈值是两层 | 可补一句"notify 的 3600s/10 是告警传输层去重限流，与 §3 业务触发阈值不同层"——消解 N-2 混淆 |
| `s0/02-crosscutting.md` §8 | 未提默认 clock=time.monotonic 的 F1 边界 | 可说明 notify 内部计时用注入/默认 monotonic，非业务时间——消解 N-3 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新告警通道（如 webhook） | ✅ 支持 | `sink` callable 注入，pipeline 传入即可；不影响结论、不进指纹 |
| 新允许载荷字段 | ⚠️ 需改代码 + 文档 | `_ALLOWED_PAYLOAD_KEYS`(:20) 加值，须确认是锚点/状态类而非内容；内容字段一律拒 |
| 改告警阈值 | ⚠️ 走判据流程 | `build_notifier` 改常量（:44-45）；I52 要求按判据调整，不为降噪静音；F7 单点定义不能被绕过 |
| 新告警来源（新 scope） | ⚠️ 需改代码 | scope 白名单（:68）拒 station/probe；新 scope 要确认是否允许通知 |
| 通知不影响指纹 | ✅ 支持 | 通知载荷/行为不进任何指纹；只引用已有锚点指纹 |

---

## C1–C12 相关项判定

本模块为通知设施，不含业务计算：

- **C10 判据不被放松**：✅ 支撑——"告警不许静音"、阈值只能按判据调整（I52），防止为掩盖问题放宽告警
- 全部其余条目：不适用（通知层不含业务语义）

## R1–R7 影响

- **R1 真数据跑通**：中性（通知是旁路）
- **R2 结论可信**：✅ 支撑——通知只发锚点指纹/状态/错误码，不含内容；失败不改结论，保证判定链干净
- **R7 fail-closed**：中性偏正——station/probe 越权 E2001、载荷违规 E2004；但通知失败本身不 fail-closed（唯一例外，设计如此）

---

## 结论

**本模块（109 行）告警阈值/去重护栏清晰，实测完全通过，无 P0/P1 级 bug，可确认通过。** 用户必查点"notify 告警阈值/去重"落地：实测 `build_notifier` 固定 3600s/10 条（F7 单点口径）；同 event 去重（第二次 deduped=True）、12 条限流投 10 条、窗口后恢复投递、station E2001、载荷凭据/结构 E2004、sink 失败不改退出码（唯一允许例外）——全部实测成立。通知行为不进任何指纹，符合横切设施红线。N-2/N-3/N-4 三条 P2 均为说明性/需确认意图（阈值配置化、默认 clock 的 F1 边界、去重无 note），不影响生产可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 12 份文档全部完成，进入汇总
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
