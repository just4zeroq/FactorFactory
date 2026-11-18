# V5 S3-M6 · executor 真实接入 capability：实现 / review / 修复报告

日期：2026-11-17　范围：`executor` 能力接入（S3-M6，不含 S4/S7 未实现能力）

## 1. 结论

executor 已具备**真实调用能力层**的完整通路：`use_capabilities=True` 时，前六工位
（Boundary→Verdict）经 `CapabilityBridge` 调真实能力，内存对象表传递 data 通道，
落盘视图剥离/落 Ref，Portfolio/Certify 因 S4/S7 未实现 fail-closed（E3004）。
默认 `use_capabilities=False` 保持 S0/S1/S2 验收全绿。

**v5 全量 596 passed**（本轮 +9 条守卫测试），包隔离守卫 5 passed。

## 2. 本轮改动

| 文件 | 改动 |
|---|---|
| `v5/orchestration/capability_bridge.py`（新建） | 工位→能力映射、Call 六通道构造、输入对象解析、落盘投影、exposures 生成、impl 名解析、policy 裁剪 |
| `v5/orchestration/executor.py` | 新增 `use_capabilities`/`supply_fn`/`capability_fn`；`_invoke_station`；内存对象表；impl 名同源；ObjectStore 注入 |
| `v5/storage/layout.py` | `fact_kind` 白名单新增 `parcel_data` 类别（工位 data 通道落盘） |
| `tests/v5/test_v5_executor_capability.py`（新建） | 9 条接入守卫测试 |

## 3. 架构对齐（逐条对照契约）

| 契约 | 落点 | 状态 |
|---|---|---|
| s0/05 §2.1 第 5 步「唯一构造调用单处」 | `_invoke_station` 是 executor 内唯一 `Call.create` 点（能力内的子调用除外） | ✓ |
| s0/05 §2.1 第 8 步「station-scope 设施调 run(call)」 | bridge.invoke 调 `capability.run(call)`；实现未就绪由 run 抛 E3004 | ✓ |
| s0/05 §2.3「禁止空产物成功」 | 未实现能力 E3004；缺对象 E3003；产物空 E3003 | ✓ |
| s0/04 §1.3「data 通道只在调用单内传、不落盘、不进指纹」 | 对象表 `objects[station]`；`InputRef.data`（compare=False） | ✓ |
| s0/04 483-494「数组通道由执行器写存储得 Ref」 | `_write_ref` 写 ObjectStore，`<field>_ref` 进 payload | ✓ |
| s0/04 §1.3「父子共享同一 policy_view」 | bridge 按 `policy_reads` 裁剪，`_check_policy_scope` 放行 | ✓ |
| runtime.py `resolve` 名字空间规则 | single/fixed 取注册唯一名；open 按 plan 名匹配 | ✓ |
| 02 §2「fitness 求值走同一个 evaluate」 | `_build_exposures` 调 evaluate 包版本，与 search fitness 同构 | ✓ |
| 08 指纹链「工位指纹含 impl/params/inputs」 | `_call_digest` 六通道不变；impl 名同源到算法名 | ✓ |

## 4. review 发现与修复（两轮独立复核）

**第 1 轮**（4 项）：
- P1：落盘 payload 缺 §3 要求的 `*_ref` → 加 ObjectStore + `_write_ref`，数组→parquet、值对象/集合→json
- P2-a：工位 impl 名（boundary@1）与能力 impl 名（pit_dynamic_topk@1）不同源 → `_impl_for` 在能力模式解析到算法名，两层指纹一致
- P2-b：budget 测试名实不符（没断言 events 值）→ 改为混合 capability_fn 跑完八步，断言 Budget==真实 n_trials
- P2-c：test 把「剥离」当正确 → 拆成有/无 store 两种模式分别断言

**第 2 轮复核**（发现新引入的 bug，全部修复）：
- `<field>_ref` 派生覆盖了能力原生 `panel_ref`/`score_ref`，同一对象两个 digest → 先收集原生字段，派生时跳过同名；加 `test_bridge_preserves_native_ref_identity` 守卫
- 多维数组静默 reshape 丢 shape → ndim>2 fail-closed E3003
- 复核提到的「per_candidate 内 ndarray 被压成摘要」经实测为**误判**：observe 返回全可 JSON 化，ndarray 只在 exposures 内存对象里，不入 payload

## 5. 阶段边界（诚实记录，非 bug）

1. **CLI 未暴露**：`v5/app/cli/run.py` 未传 `use_capabilities`，命令行默认仍走 dummy。
   开启需补 supply 读取（从 seeds parquet 构造 Pin/SupplyBatch）与 `--live` 开关，
   属下一步。Executor API 层面已完整可用。
2. **Portfolio/Certify**：combine/allocate/backtest/certify 是 S4/S7 空壳，
   开启能力后这两工位 E3004 停机 —— 这是**正确**的 fail-closed，不是缺陷。
3. **原生 ref 身份**：能力自己算的 `panel_ref`/`score_ref` 是内容 digest；
   无原生 ref 的字段才用 ObjectStore 派生。两套 ref 都是内容寻址，互不冲突。
4. **全量端到端真实跑通**仍需 S4（backtest/allocate）+ S7（certify/combine）
   和 CLI supply 注入；本轮交付的是「executor 到能力层的真实接线」这一纵切。

## 6. 验证

- `pytest tests/v5/`：596 passed
- `pytest tests/v5/test_v5_package_isolation.py`：5 passed（v5 不 import core/api/flow）
- 六工位真实链：Boundary→Panel→Target→Search→Evidence→Verdict 全部真实能力执行成功；
  Budget 事件采用真实 search 的 n_trials
- 落盘投影带 store：5 个 Boundary 数据字段写出 `*_ref` 且对象真实存在；原生 ref 身份保留
- 无 store：对象/数组从 payload 剥离，payload 可 JSON 化+可算指纹（S0/S1 兼容）
