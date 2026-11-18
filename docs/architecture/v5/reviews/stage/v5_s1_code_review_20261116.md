# V5 S1 代码审查报告（③④⑤ + 全方位 review）

日期：2026-11-16
范围：`v5/` 全部 Python 代码（contracts / facilities / runtime / storage / capability / orchestration / app）
基线：审查前全量 935 passed；审查后全量 **981 passed**（v5 单独 230 passed，含 80 subtests）
结论：**通过**。发现 2 项 P0、19 项 P1、20 余项 P2；本轮修掉全部 P0/P1 与 4 项 P2，其余按阶段归属列在 §3。

---

## 1. 本轮实现（③④⑤）

| 项 | 交付 | 验收 |
|---|---|---|
| ③ executor 真实化 | 六通道进 `call_digest`（含 phase/seed）；`parcel_digest=H(call,payload)` 指纹链；按 `spec.inputs` 真实依赖图；payload/log/parcel/trace 四处落盘 | 20 例（`test_v5_executor_real.py`） |
| ④ app 拆分 | `app/contract.py`（16 命令元数据唯一真相）+ `app/cli/<命令>.py` 逐条 + `app/render/{json,ndjson,table}` | 13 例（`test_v5_app_commands.py`） |
| ⑤ tests 归位 | 12 个 `test_v5_*.py` → `tests/v5/`，路径/fixture/cwd 全部改为基于 `__file__` | 230 passed |

## 2. 已修缺陷（按严重级别）

### P0

1. **`TicketWriter` 不校验 `ticket_id`，可写数据根之外**
   `for_ticket("../../x")` 会把 `ticket_root` 一起挪走，之后 `relative_to(ticket_root)` 恒成立。
   修：`store.py:115` 单路径段正则 + `runs/` 归属双校验；回归 5 个非法 id。
2. **基线元字段被并入计划，真实基线必报 `E1001`**
   `baseline_version` 不是 schema 顶层键，`walk()` 却把基线每个键都并进去。
   修：`compiler.py:60,133` 以 `BASELINE_META_KEYS` 剥离元字段；回归 `"baseline_version" not in c.plan`。

### P1

3. **`TraceRecord.seconds` 恒为 0.0**（`executor.py` `_trace` 写死），计时结果只赋给 Parcel 不赋给 trace。
   修：`executor.py:374` 传真实 `seconds`，并断言 trace 与 parcel 一致。
4. **落盘不完整**：payload 从未写盘；`payload_ref.relpath` 指向不存在的文件；`log_ref.digest` 由 `H(call_digest)` 伪造；parcel 目录与 Ref 路径两套命名。
   修：`executor.py:133-190,398` 确定性生成 payload/log 字节，Ref 指向真实文件，`log_ref.digest = blob_digest(实际字节)`；回归"Ref 必须指向真实文件 + 摘要可复算"。
5. **失败不写 trace**（构造后 append 到局部 list 再 raise，调用方永远看不到）。
   修：`executor.py` 失败路径写 `status=failed` + 错误码到 `trace.jsonl`，且不写 `parcel.json`；回归 1 例。
6. **工位块没进 `call_digest`**：`run` 编译出的 `params/time_axis` 没传给执行器，两个不同方案得到同一个调用单 → 缓存串。
   修：`run.py` 传 `station_params/station_impls`，整块进 params 通道；回归"params 与 time_axis 改变 → 指纹改变"。
7. **plan 的 `impl` 被静默忽略**（永远用规格表默认实现）。
   修：`Executor._impl_for` plan 优先、版本沿用规格表；回归 `parcel.impl == "pit_dynamic_topk@1"`。
8. **大小写键静默丢参**（plan 用 `boundary`，规格表用 `Boundary`）。
   修：`_canon_station` 唯一归一路径，未知名 `E1004`；回归 2 例。
9. **覆盖层写错位置且不过校验**：`--set a.b=v` 造出字面量键 `"a.b"`；`--set foo=1`/`--set caliber=oops` 直接进指纹。
   修：`compiler._apply_override:342` 按路径写入 + 清旧来源 + 重标 override + 覆盖后重新 `validate_plan`；回归 2 例。
10. **`policy_digest` 误含实现名与种子**（违反 s0/01 §2.3 的"否"列）。
    修：`compiler._constitutive_subset:311` 剔除 `impl/impl_version/seed`；回归"改 impl/seed → policy 不变、blueprint 变"。
11. **同一指纹两套公式**：compiler 用 `compute_digest(x)`，契约层用带 `{"kind": ...}` 包装的 `plan_digest/caliber_digest/...`。
    修：全部改走 `v5.contracts.digest`；回归 `plan_digest == dg.plan_digest(raw)`。
12. **供给 pin 原始映射进蓝图**：`acquired_at` 混入 `blueprint_digest`，重采一次数据即全链失效。
    修：`digest._pin_digest_view:312` 改为白名单投影；回归"仅 acquired_at 变 → 蓝图不变"。
13. **来源表与叶子不一致**：显式 `null` 不给来源、基线元字段留下多余条目。
    修：`compiler.py:258` 以 `leaf_paths(merged)` 对齐并补 `E1007` 覆盖检查；回归"origins 键集合 == 叶子集合"。
14. **`append_jsonl` 可改写不可变产物**：追加到 `parcel.json` 绕过"异内容拒绝覆盖"。
    修：`store.py:157` 仅接受 `.jsonl`；回归 1 例。
15. **`DepotWriter` 可写整个数据根**（可覆盖别的 Ticket、可自建 `db/ledger.jsonl`）。
    修：`store.py:189` 限定 `supply/`；回归 1 例。
16. **`ObjectStore` 读路径可逃逸**：`relpath="../../x.parquet"` 直读根外，且不看 `kind`。
    修：`objects.py:78` 统一 `_resolve()`（相对路径校验 + 根内校验 + `/` 分隔）；回归 1 例。
17. **索引 `objects.digest` 取值错误**：`sidecar.stem` 得到 `"abc.parquet.schema"` 且无 `sha256:` 前缀，任何按 digest 的 join 都命中不了。
    修：`index.py:77` 取真正主体 + 前缀；同时把无 sidecar 的 `write_json` 产物纳入索引；回归 1 例。
18. **缓存命中不校验内容**：只看文件存在，产物被替换仍返回 hit。
    修：`cache.py:87` 重算字节/结构摘要，篡改视为 miss；回归 1 例。
19. **环境快照可把凭据值当引用名落盘**：`sk_live_…` 不含 `=` 且长度 ≤128，原逻辑放行。
    修：`envelope.py:121` 引用名必须是全大写环境变量名；回归 2 例。
20. **未知 `spec.inputs` 被静默跳过**（拼错依赖名 → 下游无上游指纹继续跑）。
    修：`station._validate_specs` 导入期校验，未定义引用 `E1003`。
21. **`--no-strict` 连跨字段一致性一起关**（缺裁决者也能编译）。
    修：`plan_schema.validate_cross_field` + compiler 在非 strict 下仍校验；CLI 帮助文案同步。

### P2（顺手修）

22. **契约自洽**：`Parcel.origin` 枚举缺 `commit`，而 s0/05 §6 与 O21 明确要求 `commit`。
    修：`types.py`、`trace.py`、`s0/01`、`s0/02`、`03-orchestration.md` 五处同步为 `run/step/commit/resume/cache`。
23. **CLI 拆分后 `python -m v5.app.cli` 不可用**：新增 `cli/__main__.py`。
24. **`experiment` 别名是死代码**（contract 认、argparse 不认）：`main()` 分派前归一。
25. **`station` 谎报 `implemented=True` 但恒抛 `E3001`**：改为 `False`，保留真实拒绝语义。
26. **`center=True` 检查永不成立**（`str(dict)` 用单引号）且 `min` 非数字被静默跳过：`registry.py` 修正。
27. **`LogSink` 兜底直接取墙钟**：改为设施 `Clock()`，不再在 `log.py` 里 `datetime.now`。
28. **`station/probe` scope 伪造 `call_digest="0"*64`**：改为缺就 `E2003`。

## 3. 未修：按阶段归属（不是遗漏）

| 阶段 | 事项 | 证据 |
|---|---|---|
| S2 供给链 | `aliases=None` 时 `supply_pins=[]`，蓝图看不到数据版本；`gate_b` 未接入编译（`E4002` 不可达、`gate_a.passed=false` 不拦）；标的集检查读 `pin.gate_a["symbols"]`（不在 Pin 契约内）恒 PASS；新鲜度读 schema 不存在的 `supply.max_age_days`；`domain` 成员只有标的集名，`available_from` 恒 None | `compiler.py` 步骤 9；`gate_b.py:62-99` |
| S3 能力 | compiler 步骤 5-8 未接目录：`E1002`（实现名不在 catalog）/`E1013`/`E1016`/`E1021`/`E1022` 编译期不可达；12 壳 `NotImplementedError` 被 CLI 映射成 `E3004`（"未预期异常"），语义上应是"能力未实现"；`rng.scope()` 重复调用返回同一序列，需在 S3 明确"一 scope 一实例" | `compiler.py:172-174`；`capability/ops/*`；`rng.py:23` |
| S5 编排提交 | `--probe/--commit` 分离、事件落全局账本、`inspect` 读落盘记录、`Budget` 值取自 `CandidateSet.count` | `executor.py` 文件头边界；`cli/inspect.py` |
| S6 设施/存储 | ledger 短写不校验、坏行 `JSONDecodeError` 直穿、并发追加无锁；cache 无容量/不落盘；notify 无超时且 `event` 字段不受白名单约束；index 重建非事务、`assert_consistent` 只比行数；`layout.parcel_dir/log_file` 与 executor 实际路径不一致（且无调用点） | 见 `test_v5_logging.py` 未覆盖项 |
| 需确认 | `--set` 是否需支持列表下标（`search.algorithms[0].name`）；`E5002` 同时用于"凭据缺失"与错误表定义的"租约冲突"，码位冲突需裁决 | `errors.py:138`；`envelope.py` |

## 4. 回归

```
tests/v5/                      230 passed
全量 pytest                    981 passed, 80 subtests passed
```

新增/更新回归用例 20 条，覆盖：指纹通道、指纹链传播、依赖缺失/stale、落盘往返与 Ref 真实性、失败 trace、路径逃逸（ticket/depot/object）、索引 digest 前缀、缓存篡改、凭据值拦截、覆盖层路径与再校验、policy 排除项、契约 origin 枚举。
