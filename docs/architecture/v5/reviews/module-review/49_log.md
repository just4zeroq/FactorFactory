# 模块审查 49 · `v5/facilities/log.py`

> 审查日期：2026-11-18
> 依据：源码 477 行全文通读（已用 structlog 重写）；依赖 `clock.py`（注入时钟）、`contracts/errors.py`（E2003/E2004/E2007）、structlog 26.1.0；设计 `s0/02-crosscutting.md` §2（F1/F3/F9）、`15-operations.md` §2（I16 日志与产物同目录同指纹）、§3（告警阈值）；测试 `tests/v5/test_v5_logging.py`（22 例，含嵌套凭据/轮转同目录）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_facilities.py` 的 F-log 段 + `fm_probe_log_rotate.py`（小 max_bytes 模拟 32MB 触发）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`log.py` 是横切所有层的结构化日志设施，职责是"在成熟序列化链外套一层**契约闸门**"（源码:3）。它该做的：

1. **闸门归我们**：scope 必填关联键（E2003）、凭据与结论词（E2004）、路径不可自选（Logger 连 `path` 属性都没有，只收注入 `LogSink`）、时间只来自注入 clock。这是 F1「禁止模块级 `get_logger`」的落点——通过 `install_log`/`get_log` 的 `ContextVar` 解析组合根注入的实例，未注入即 E2007（绝不返回静默丢弃的 no-op 替身）。
2. **序列化归 structlog**：processor 链做 level 归一、异常栈渲染（`format_exc_info`，不是 `str(exc)`）、JSON 编码（稳定键序 `sort_keys=True`、`ensure_ascii=False`）。`_json_default` 拒绝不可序列化对象（不用 `default=str`，否则对象地址进日志、同输入不同输出）。
3. **落盘归我们**：`Layout` 给 append fd（"a" 模式 = `_O_APPEND`，多进程追加不覆盖），32MB 轮转 + 2 备份。

**关键不变量**：
- **设施不得影响指纹**（用户必查点）：日志文件名由 `call_digest` 反推（`log_path`:415-423，去前缀取 hex 前 16 位），日志内容不进任何指纹；`Parcel.log_ref` 只是引用，报告时间 `at`/耗时都在观测字段。`environment_digest` 含 `clock_basis` 但不含具体时钟读数。
- **凭据/结论不落原文**：`_forbidden_keys` 递归无深度上限扫描（用 visited id 防自引用），`_materialize` 在扫描前展开 `tolist()`/`item()` 防止"扫描时是对象、落盘时变成密钥字典"绕过闸门（P1-3/P1-4 回归修复）。
- **关联键不可覆盖**：ctx 传 `ticket_id`/`station` 等同名键 → E2003（P1-5，防 trace/缓存串线）。

**关于 F5/F7/E5001 的归属（用户必查点澄清）**：
- **F5**（cache 命中文件被删视为未命中）与 **F7 去重限流**（notify 3600s/10 条）**不在 log.py**——F5 属 `cache.py`，F7 属 `notify.py`（见 50/55 号文档）。log.py 自身的"内存有界"是 `DEFAULT_BUFFER=1000` 的 `deque(maxlen)`（15-operations 的"运行时长"故障防护）。
- **磁盘满 E5001** 由产物落盘路径（`runtime/store.py:150`、`storage/objects.py:68`）实现，**不在 log.py**——log sink 写失败走 `_RotatingWriter.write` 的 `flush`，当前无专门 E5001 转换（见 D2 L-3）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测。

### 例 1 · 契约闸门 + F1/F9（实测）

```
DEFAULT_BUFFER = 1000
StructlogLogSink.max_bytes default = 33554432   (= 32*1024*1024)
backups default = 2
log_path('sha256:abcdef0123456789'+'0'*48) = abcdef0123456789.jsonl
ctx token -> E2004
nested api_key (req={'api_key':'sk-X'}) -> E2004
ctx override 关联键 seq -> E2003
entries ring buffer maxlen = 1000
get_log 未注入 -> E2007
get_log after install == injected logger: True
get_log after reset -> E2007
```

**解读**：
- **凭据闸门**：顶层 `token`、嵌套 `req.api_key` 都 E2004，不落原文；覆盖关联键 `seq` → E2003。
- **F1**：未 `install_log` 时 `get_log()` → E2007，绝不静默降级；install/reset 用 ContextVar token 精确隔离（异步/多线程不污染）。
- **F9**：日志文件名 `abcdef0123456789.jsonl` 由 `call_digest` 前 16 位反推（去 `sha256:` 前缀，避免 Windows 冒号非法），产物可经 `log_ref` 定位日志。
- **32MB 轮转**：`max_bytes` 默认 33554432 字节，`backups=2`。

### 例 2 · 轮转真实落盘（小 max_bytes 模拟 32MB 触发）

```
files after rotation: ['run.jsonl', 'run.jsonl.1', 'run.jsonl.2']
  run.jsonl:   0 bytes, 0 lines
  run.jsonl.1: 248 bytes, 1 line
  run.jsonl.2: 248 bytes, 1 line
```

写入 8 条日志、`max_bytes=200`/`backups=2` 的模拟结果：按大小轮转（`.jsonl`→`.1`→`.2` 级联），当前文件写满即转。注意**模拟阈值远小于单行长度（248B > 200B），导致每条都触发轮转、只保留最近 2 份**——真实 32MB 场景单行远小于阈值，此为极端口径（见 D4 发现）。

### 失败路径（实测）

| 非法输入 | 错误码 |
|---|---|
| `get_log()` 未注入 | E2007 |
| ctx 含 `token`/`secret`/嵌套 `api_key` | E2004 |
| ctx 覆盖关联键 `seq` | E2003 |
| `Logger(scope='unknown')` | E2003 |
| 向 closed logger/sink 写 | E2004 |
| `max_bytes<0` / `backups<0` | E2003 |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| L-1 | **477 行是否过度自研**：已用 structlog 做序列化，自研部分只剩契约闸门（凭据/scope/关联键）、`_RotatingWriter`（32MB 轮转）、ContextVar 注入——都是成熟库不提供的契约约束。 | —（正面/已裁决） | `log.py:1-20` docstring 分工；structlog 26.1.0 实测可用 | **已确认不算过度自研**：闸门是三方库没有的，序列化已外包 structlog。plan U2 D3 要求"若被问到给替换成本对比，不擅自重写"——本轮不给重写建议。 |
| L-2 | F5（cache 文件被删视为 miss）/ F7 去重限流（notify 3600s/10 条）**不在 log.py**——用户必查点把它们列在 log 项下。 | P2（归属澄清） | `cache.py:64-88`（F5）；`notify.py:36-45`（F7）；s0/02 F5/F7 原文 | **已确认是职责分工，非缺口**：F5/F7 在正确模块实现且实测通过（见 50/55 号文档）。log.py 自身的"有界"是 `DEFAULT_BUFFER=1000` 内存 deque。 |
| L-3 | **磁盘满 E5001 不在 log.py**：log sink 写盘（`_RotatingWriter.write` 的 `write`+`flush`）无 OSError→E5001 转换，`log.py` grep 无 E5001。 | P2（需确认意图） | `log.py:202-209`；E5001 落在 `runtime/store.py:150`、`storage/objects.py:68`；s0/02 F3 只规定凭据闸门 | **需确认意图**：日志是旁路设施，写盘失败是否必须 E5001（fail-closed）未在设计明文。当前 E5001 覆盖产物落盘，日志丢行不影响结论（日志是唯一真相的"旁路"）。建议文档明确"日志写盘失败的处理口径"，不擅自加转换。 |
| L-4 | 极端口径：单行日志长度 ≥ `max_bytes` 时，每写一行即轮转，backups 之外的历史行被覆盖（模拟实测 8 行只剩 2 行）。 | P2（边界） | `log.py:202-206` 写后 `st_size >= max` 即转；模拟实测 | **已确认边界行为**：真实 32MB + 单行数百字节不会触发。但可考虑"轮转前判断写完是否会超，单行超阈值时强制落盘不丢"——低优先，不影响生产。 |

**多了什么**：无冗余。`JsonlLogSink = StructlogLogSink` 别名（:235）保留旧引用，低成本兼容。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| structlog（显式 processor 链 + JSONRenderer） | 采纳 | `format_exc_info` 渲染异常栈、`sort_keys=True` 稳定键序、可插拔 processor；不用 `structlog.get_logger()` 全局单例（F1 要防的正是它） |
| stdlib RotatingFileHandler | 借鉴但自持 writer | 32MB+2 备份与 stdlib 同语义；多进程轮转交错限制也相同，因生产入口单写者（租约 E5002）不加锁（:105-109） |
| `default=str` 兜底序列化 | 拒绝 | 对象地址进日志、同输入不同输出，破坏可复现（:64-88）；`_json_default` 显式报错 |
| ContextVar 注入（get_log/install_log） | 采纳 | 异步/多线程隔离，测试用 token 精确还原；不用模块级可变全局变量 |
| 全局 no-op 空 logger 兜底 | 拒绝 | 未注入即 E2007，绝不静默丢日志（:442,459-472） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **F1 合规**：`get_log` 未注入 E2007（实测）；不构造全局 logger；模块级 `ContextVar` 而非可变单例（:443）。
2. **凭据闸门递归无死角**：`_forbidden_keys` 递归 walk 任意深度 + visited 防自引用（:375-412）；`_materialize` 扫描前展开 `tolist()/item()`（:349-372），顶层/嵌套/深度嵌套凭据实测 E2004（22 例日志测试覆盖）。
3. **关联键不可覆盖**：ctx 与 keys 交集约 E2003（:324-329，实测）。
4. **设施不影响指纹**（用户必查点）：`log_path` 由 call_digest 反推（:415-423，实测）；日志时间/耗时在观测字段，不进任何指纹；`environment_digest` 不含具体时钟读数。
5. **32MB 轮转**：`max_bytes=32*1024*1024` 默认（实测 33554432），`backups=2`，写后检查级联（:192-231），模拟落盘生成 `.1`/`.2`。
6. **内存有界**：`DEFAULT_BUFFER=1000`、`deque(maxlen=1000)`（:51-53,251，实测 maxlen=1000），长跑不线性增长。
7. **平稳失败**：`close` 阶段二次异常不再抛（:226-231）；`_json_default` 拒绝不可序列化类型（实测测试钉住）。
8. **level 不被归一**：`warn` 不被 structlog 改成 `warning`（:166-176），契约里的 level 原样落盘。

**发现的问题**：
- L-3（磁盘满 E5001 不在 log，需确认意图）、L-4（单行超阈值轮转边界）——均 P2，无 P0/P1 级 bug。测试 22 例覆盖凭据/关联键/轮转同目录/buffer 有界/F1。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/02-crosscutting.md` §2 | F1/F3/F9 与实现一致 | 无需更新 |
| `s0/02-crosscutting.md` §2.4 | 凭据闸门描述一致 | 无需更新 |
| `15-operations.md` §2 I16 | "日志与产物同目录同指纹" | 无需更新；轮转 `.1`/`.2` 同目录符合同指纹语义 |
| `s0/02-crosscutting.md` | 未明说"日志写盘失败是否 E5001" | 补一句明确 L-3 的处理口径（日志旁路失败是否 fail-closed）——说明性，非契约变更 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增 scope（如 `replay`） | ⚠️ 需改代码 | `_REQUIRED_BY_SCOPE`(:55-60) + `build_facilities` scope 校验两处；必填关联键需定义 |
| 新增脱敏 processor | ✅ 支持 | `extra_processors`(:113) 在 JSON 渲染前插入，无需改核心；正是 structlog 可插拔的价值 |
| 新增告警通道（notify 侧） | ✅ 不影响指纹 | 告警 payload 白名单（55 号文档），新增通道走 `build_notifier` 的 sink |
| 换日志库（structlog→其它） | ⚠️ 成本中等 | 闸门（凭据/scope/关联键）需保留，只换序列化/落盘；plan U2 D3 要求"给替换成本对比，不擅自重写" |
| 大单行日志不丢历史 | ⚠️ 需改轮转判断 | L-4：轮转前预判写后大小，单行超阈值时特殊处理；低优先 |

---

## C1–C12 相关项判定

本模块为日志设施，不含业务计算：

- **C10 判据不被放松**：✅ 支撑——结论词（`pass`/`verdict`/`significant` 等）禁进日志（:40-42），结论只能由 Sentinel 写，日志不能成为"判据被悄悄改写"的载体
- **C7 24/7 缺口**：✅ 时间一律走注入 clock（UTC），不读本地时钟；日志时间字段确定性（冻结时钟重放一致，F8）
- 其余条目：不适用（日志层不含业务语义）

## R1–R7 影响

- **R1 真数据跑通**：中性（日志是旁路，真跑反复使用）
- **R2 结论可信**：✅ 强支撑——凭据闸门、结论词拦截、关联键不可覆盖、异常栈完整，保证"谁在何时写了什么"可追溯
- **R7 fail-closed**：✅ 强支撑——未注入 E2007、凭据 E2004、缺关联键 E2003、closed 写入 E2004，不静默丢日志

---

## 结论

**本模块（477 行，已 structlog 化）契约闸门清晰、无 P0/P1 级 bug，可确认通过。** 用户必查点逐条落地：①是否过度自研——闸门/轮转/注入是三方库不提供的契约约束，序列化已外包 structlog，不算过度自研；②F5 LRU/F7 去重限流——实测确认二者分别在 cache.py（F5，见 50 号）和 notify.py（F7，见 55 号），log 自身内存有界为 DEFAULT_BUFFER=1000；③32MB 轮转——实测 max_bytes=33554432、backups=2、模拟落盘生成 `.1`/`.2`；④磁盘满 E5001——落在产物落盘路径（store/objects），不在 log，L-3 需确认日志旁路失败口径；⑤设施不影响指纹——log_path 由 call_digest 反推、日志时间/耗时不进指纹，实测成立。L-3/L-4 为 P2 需确认意图/边界，不影响生产可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 7 个模块 `cache.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ L-3（日志写盘失败是否 E5001）想当场裁决 → 告诉我
