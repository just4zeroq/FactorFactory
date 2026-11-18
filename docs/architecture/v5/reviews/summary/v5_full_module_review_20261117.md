# V5 分模块完整代码审查报告

日期：2026-11-17
范围：`v5/` 全部 Python 代码（contracts / facilities / runtime / orchestration / app / capability / storage / upstream），对照 `IMPLEMENTATION-PLAN.md`、`12-stages.md`、`s0/` 六篇与三份已有报告。
方法：五个只读审查代理按模块并行 → 主审实测复核 → 分级修复 → 新增回归守卫 → 全量回归。

---

## 1. 阶段对齐结论

| 阶段 | 状态 | 证据 |
|---|---|---|
| S0/S1 | 完成 | 八指纹、十步编译、executor 六通道与四处落盘、app 16 命令元数据 |
| S2 供给链 | 完成 | `v5/upstream/` 七文件 + 门禁 A/B 真实契约 + refresh；S2 报告 347 passed |
| S3-M1（面板/标签/边界/对齐） | 完成 | `panel/stats/stream/params/impls` + `label/boundary/align`；S3-M1 报告 456 passed |
| S3 剩余（evaluate/observe/verdict/search） | **未做** | `ops/evaluate.py` 部分实现；observe/verdict/search 仍是壳 |
| S4/S5/S6/S7 | 未做 | backtest/allocate、probe/commit、GC/F1 静态守卫、certify/combine |

**当前代码与计划的偏离**：`read` 仍是 S1 空壳（S3-M2 应真实化）；`doctor`/`estimate` 未实现（S6/S4 归属，非 bug）；`inspect` 仍返回空记录（S5 归属，但当前实现会伪装成功，见 §4）。

---

## 2. 本轮确认并修复的缺陷

### P0（1 项）

**P0-1 供给 checksum 在 8193 行触发 E2002，真实规模采集/发布全失败**
`v5/upstream/gate_a.py:75-91`。原实现把每行构造成 dict 后整体交给 `compute_digest`，撞上 `MAX_INLINE_SEQUENCE=8192` 硬护栏。实测 8192 OK / 8193 E2002；8200 根 1m bar 端到端采集失败。
修法：逐行 `canonical_text` 排序后流式 sha256，保持"同内容同摘要、行序无关"，去掉内联上限。
回归：`test_payload_checksum_handles_more_than_inline_limit`（8300 行 + 篡改检测）。

### P1（8 项，全部已修）

| # | 缺陷 | 修法 | 回归 |
|---|---|---|---|
| 1 | `compile_plan` 就地改调用方 plan，`--set` 覆盖值污染 `plan_digest`，二次编译指纹漂移 | 入口 `deepcopy(plan/baseline/overrides)`；`plan_raw` 最先深拷 | `test_compile_is_pure_and_plan_digest_is_raw` |
| 2 | `--no-strict + --set` 放行跨字段矛盾（X1–X7），与"只放行必填缺失"的设计冲突 | override 后补 `validate_cross_field` | `test_no_strict_override_still_checks_cross_field` |
| 3 | ledger 一次性去重是 check-then-write，双实例/16 线程各写一份 | filelock 临界区内重扫 + 写入；`records()` 跳过半行 | `test_ledger_dedup_across_instances`、`test_ledger_records_survive_torn_line` |
| 4 | NDJSON 含 list 时丢掉整个信封（`ok`/`exit_code` 消失） | 信封永远第一行，列表项作为明细行追加 | `test_ndjson_always_emits_envelope_line` |
| 5 | 日志凭据闸门：`tolist()/item()` 在扫描后才展开，密钥明文落盘 | 扫描前统一物化，keys/bind/ctx 同一闸门 | `test_tolist_secret_is_rejected_before_serialization` |
| 6 | publish 租约只在入口校验一次，长阶段后旁落写者仍提交别名 | 阶段 5 写别名前 `leases.renew()` 复核 | 现有发布/端到端 34 例回归 |
| 7 | 注册表"加载即自检"漏检 arity/unit/datasets/schema_version 一致性 | 补四类必填与三文件版本一致校验 | 现有能力层 76 例回归 |
| 8 | index `assert_consistent` 只比行数且不查 objects 表 | 抽出 `_ledger_row`/`_rebuild_object_rows` 共享投影，逐行内容比对；JSON 对象 schema_version 用常量 | `test_index_consistency_checks_objects_table` |

### P2（已修 7 项）

- `store.py`/`envelope.py`/`publish.py` 三处 `re.match + $` 接受尾部换行 → 全改 `fullmatch`（`test_ticket_id_rejects_trailing_newline`、`test_publish_version_rejects_trailing_newline`）。
- `facilities.strict` 只查 `is None`，`object()` 可冒充设施 → 加类型校验（`test_strict_rejects_wrong_type_facility`）。
- `log_keys` 可覆盖 `ticket_id`/`seq` 造成归属串线 → 改为 `setdefault`（`test_log_keys_cannot_override_ticket_id`）。
- `StructlogLogSink.close()` 后 `write()` 会重开文件 → 加 `_closed` 闸门（`test_sink_rejects_write_after_close`）。

测试新增 13 条，v5 **529 passed**。

---

## 3. 未修项与判定

### 需裁决（不擅自改）

1. **`policy_digest` 是否应含 `search.algorithms[].name`**：审查代理认为它是"实现名，不该进 policy"。但算法的身份（gp_symbolic vs random_expression）是**方案构成**，换算法理应新建试验族；文档只写明"工位 `impl` 名与种子不进 policy"，未覆盖算法选择。判定为**需裁决**，当前不改。
2. **`E5002` 一码三义**（租约冲突 / 凭据缺失 / index 版本冲突）：S2 报告已列为契约缺口，拆码需改错误表与三篇文档。
3. **`get_log()` ContextVar 跨线程不继承**：worker>1 的子线程调用会 E2007。保留全局访问器则需定义线程传播契约；不保留则回退 `s0/02 §2.1` 例外段落。
4. **`--yes` 确认守卫是死代码**：`require_confirmation()` 无调用点，`certify` 无 `--yes` 参数。属于契约机制与实现的对齐问题，需决定是接线还是修订元数据。

### 阶段归属（不是 bug）

- `inspect` 恒返回空记录（S5 落盘索引）；`doctor`/`estimate` 未实现（S6/S4）；observe/verdict/search 仍壳（S3 未完）。
- `nanmean` 不处理 Inf、`_EPS` 绝对阈值判零、`Pin.digest` 漏 schema_version/gate_a、`fields_digest` 含注释、`resolve_impl` 单/固定名字空间与 runtime 不一致 —— 属能力层待收敛项，不改变主链结果，排入 S3-M2。
- `array_digest` 对 `-0.0` 与契约层归一不一致（S3-M1 报告已列）。

### 与文档不一致但不影响行为（P2 排期）

- `s0/02 §2.1` 接口写 `log.bind(scope,keys)`/`child()`，实现是 `build_logger()`/`bind(extra)`。
- `contract.py` 的 `data_keys` 与实际返回/§2.4 不完全一致（run 多 `stage`、inspect 缺三键）。
- `ZONE_NAMES` 未含 S2 新增子区（`staging`/`seeds`/`_upkeep`），AST 守卫不设防。

---

## 4. 复核为正确、不要误改

- gate_b 六项（门禁 A/覆盖/标的集摘要/新鲜度/末端闭合/供给维度）实测均可 fail-closed。
- forward_return 方向、trailing 算子因果性、cs_rank/zscore、cs_neutralise、qweave 行序对齐均正确。
- 49 错误码无重复；derive_seed 不受 PYTHONHASHSEED 影响；WriteLogger 逐条 flush 属实。
- 跨层 import 守卫：v5 不 import core/api/flow；能力 ops 不碰存储/路径。

---

## 5. 回归

- v5：`529 passed`（新增 13 条回归守卫）
- 全仓库：`1280 passed, 80 subtests passed`（无回归）
