# 模块审查 51 · `v5/facilities/trace.py`

> 审查日期：2026-11-18
> 依据：源码 59 行全文通读；依赖 `contracts/types.py`（PARCEL_ORIGINS 枚举对照）、编排层 `executor.py:930-955`（_trace 落盘与 origin=cache）、`storage/ledger.py`（trace.jsonl 只追加）；设计 `s0/02-crosscutting.md` §3（每工位一条、失败也留痕）；测试 `tests/v5/test_v5_facilities.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_facilities.py` 的 F-trace 段。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`trace.py` 是每工位一条的**执行事实记录**，只追加。它该做的：

1. **每工位一条，失败也必须有**（docstring:3-4）：没有 trace 的失败等于系统没留下"发生过这件事"的证据。status 三态 `done/failed/skipped`，失败必带 `error_code`、跳过必带 `skip_reason`（:37-40）。
2. **字段一致性**（用户必查点）：`TraceRecord` 的字段必须与 `Parcel`/日志对齐——同一套 origin 枚举（`run/step/commit/resume/cache`）、同一套 status、同一 call_digest/parcel_digest。`origin=cache` 让"跑了几次"可审计（见 50 号文档）。
3. **seq 连续**：`TraceLog.append` 要求 `seq == len(records)+1`，跳号即拒——trace 是"只追加序列"，缺号意味着有记录被吞。
4. **只报事实，不报结论/指标值**（docstring:6）：trace 是执行事实记录，不是报告；不写结论、不写指标值。

**字段 14 个**（实测 to_dict keys）：seq / station / station_revision / impl / impl_version / params_digest / inputs / call_digest / parcel_digest / status / origin / seconds / error_code / skip_reason。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `fm_probe_facilities.py` F-trace 段。

### 例 1 · 字段一致性 + origin=cache + seq 连续（实测）

```
TraceRecord.to_dict keys = ['call_digest','error_code','impl','impl_version','inputs',
  'origin','params_digest','parcel_digest','seconds','seq','skip_reason',
  'station','station_revision','status']
origin=cache accepted = cache
append seq=1 ok; append seq=3 (gap) -> rejected: trace seq gap: got 3, expected 2
failed w/o error_code -> rejected: failed trace must carry error_code
trace origin set == PARCEL_ORIGINS ? True
```

**解读**：
- **字段一致性（用户必查点）**：14 字段齐全；`origin='cache'` 被接受；实测 `trace origin set == PARCEL_ORIGINS` 为 True——trace 与 `types.py:127` 的 `PARCEL_ORIGINS` 枚举一致，无漂移。
- **seq 连续**：append seq=1 成功，seq=3（跳号 2）被拒——"只追加序列"不被跳号破坏。
- **失败留痕**：`failed` 无 `error_code` 被拒，保证每条失败 trace 都带排查信息。

### 失败路径（实测）

| 非法输入 | 结果 |
|---|---|
| `status` 非 done/failed/skipped | ValueError |
| `origin` 非 run/step/commit/resume/cache | ValueError |
| `status=failed` 但 `error_code=None` | ValueError |
| `status=skipped` 但 `skip_reason=None` | ValueError |
| `TraceLog.append` seq 跳号 | ValueError（seq gap） |

> 注：本模块校验用裸 `ValueError` 而非 `FlowError(E2xxx)`。这是设施内部不变量断言，编排层（`executor.py:950-955`）捕获 `ValueError` 后降级为"跳过内存登记、落盘照走"——属设计取舍（见 D2 T-3）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| T-1 | **trace 字段一致性（用户必查点，正面）**：origin 集合与 `PARCEL_ORIGINS` 实测一致，status 三态、失败/跳过留痕齐全。 | —（正面） | F-trace 实测 + `trace.py:32-40` | **已确认符合设计**。无 P0/P1 缺口。 |
| T-2 | `TraceLog` 是纯内存 list（:49-59），持久化的 trace.jsonl 在编排层 `executor._write_trace` 落盘（:930-933）；单步场景 seq 不从 1 起时跳过内存登记（executor.py:951-955）。 | P2（分层说明） | `trace.py:46-59`；`executor.py:930-955` | **已确认符合设计**：trace 内存对象用于审计序列，落盘由执行器统一。单步跳过内存登记但落盘照走，属编排层取舍，非本模块 bug。 |
| T-3 | 校验抛裸 `ValueError` 而非 `FlowError(E2xxx)`（:34,36,38,40,55）；编排层需 try/except ValueError 处理。 | P2（一致性） | `trace.py:32-40,52-56`；`executor.py:950-955` | **需确认意图**：trace 内部不变量用 ValueError 是轻量子集选择；但与全仓 `FlowError` 契约略有不一致。若统一为 E2xxx 需改本模块 + 编排层捕获。低优先，不影响正确性。 |
| T-4 | `TraceRecord.to_dict()` 用 `dataclasses.asdict`（:42-43），`inputs` 是 `list[tuple]`——asdict 会把 tuple 保留为 tuple，JSON 序列化时变 list；单步 events 内存语义 vs 落盘形态可能不一致。 | P2（待核实） | `trace.py:23,42-43` | **需确认**：plan U5 D2 提到"事件账本落盘但单步 events 仍内存语义（已知遗留）"。本模块 `inputs` tuple 落盘为 list 属正常 JSON 转换，无信息丢失。低优先。 |

**多了什么**：无冗余。59 行全是数据类定义 + 不变量校验，无死代码。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 只追加执行日志（append-only trace） | 采纳 | 每工位一条、失败留痕、seq 连续——"跑了几次/哪步失败"可审计，不可事后改写 |
| 失败也必须留痕 | 采纳 | status=failed 必带 error_code，避免"失败的工位没有证据" |
| 结论/指标进 trace | 拒绝 | 只报事实；结论归 VerdictParcel、指标归 Evidence（docstring:6） |
| 可变的 trace 列表（可删改记录） | 拒绝 | `TraceLog` 只 append、seq 强制连续，防止事后抹掉失败记录 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **字段一致性（用户必查点）**：14 字段齐全，origin 集合与 PARCEL_ORIGINS 实测一致（:35）。
2. **失败留痕**：failed 必带 error_code、skipped 必带 skip_reason（:37-40），实测校验生效。
3. **seq 连续**：append 强制 seq=len+1（:52-56），跳号实测拒绝。
4. **origin=cache 支持**：origin 集合含 cache（:35），命中缓存时执行器标 origin=cache（50 号文档实证）。
5. **只报事实**：字段里没有结论/指标值字段（:15-30），符合"trace 不是报告"。
6. **不可变记录**：`@dataclass(frozen=True)`（:15），trace 一旦登记不可改。

**发现的问题**：
- T-2（内存/落盘分层）、T-3（ValueError vs FlowError）、T-4（tuple→list JSON 转换）——均 P2，无 P0/P1 级 bug。本模块无专门测试文件，但被 executor/agent tests 间接覆盖。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/02-crosscutting.md` §3 | "每工位一条、失败留痕、只追加"与实现一致 | 无需更新 |
| `s0/02-crosscutting.md` §3 | 未列 TraceRecord 14 字段与 origin 枚举 | 可补字段清单 + origin 与 PARCEL_ORIGINS 同源说明（说明性，非契约变更） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增 trace 字段 | ⚠️ 需改代码 + 下游 | 加 `TraceRecord` 字段 + `to_dict` 自动带（asdict）+ 执行器 `_trace` 构造；若字段要进审计须同步文档 |
| 新增 origin（如 altered） | ❌ 需多处同步 | `trace.py:35` + `types.py:127` PARCEL_ORIGINS + 执行器——实测二者必须一致，改一处会漂移 |
| 新增 status | ⚠️ 需改代码 | `_REQUIRED_BY_SCOPE`/`Parcel` 状态/本模块三处；status 是与 Parcel 共享的契约枚举 |
| trace 不影响指纹 | ✅ 支持 | trace 字段不进任何产物指纹；origin=cache 只影响审计，不影响 call_digest/parcel_digest |

---

## C1–C12 相关项判定

本模块为执行记录设施，不含业务计算：

- 全部 C1–C12：不适用（trace 只记"哪步跑了、什么状态、什么 origin"，不含年化/资金费/市场维度等业务语义）

## R1–R7 影响

- **R1 真数据跑通**：中性（trace 是旁路记录）
- **R2 结论可信**：✅ 强支撑——失败留痕、origin=cache、seq 连续让执行历史可审计，防止"缓存产物冒充新算"或"失败工位无证据"
- **R7 fail-closed**：✅ 支撑——seq 跳号、失败缺 error_code 全拒，trace 序列不可被静默篡改

---

## 结论

**本模块（59 行）简单可靠，字段一致性实测通过，无 P0/P1 级 bug，可确认通过。** 用户必查点"trace 字段一致性"落地：14 字段齐全、origin 集合与 `PARCEL_ORIGINS` 实测一致（含 cache）、status 三态 + 失败/跳过留痕、seq 强制连续（跳号拒绝）。T-2/T-3/T-4 三条 P2 均为分层说明/一致性/待核实，不影响生产可用。新增 origin 需同步 trace 与 types 两处枚举（实测一致性是当前保障）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 9 个模块 `clock.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
