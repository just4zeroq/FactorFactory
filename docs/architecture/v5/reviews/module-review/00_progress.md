# V5 逐模块深度审查 · 进度

> 启动：2026-11-18 13:24 · 完成：2026-11-18 13:5x
> 规则：**逐模块审查** → 六维齐全（D1 应然 / D2 差距 / D3 业界 / D4 代码 / D5 文档 / D6 扩展）+ 真实例子（venv 实跑输入输出）+ C1–C12 相关项 + R1–R7 影响 → 生成该模块审查文档。
> 顺序：按真实因子挖掘因果链 U6→U7→U8→U9→U10→U3→U4→U5→U1→U2→U11。
> 状态：⬜ 待审 · 🔄 审查中 · ✅ 已出文档（本轮只读，未改任何生产代码/测试/文档）

## 完成情况：**78/78 模块全部出文档**（01–78 无缺号，78 份均含 venv 真实运行证据）

| # | 单元 | 模块 | 文档 | 状态 |
|---|---|---|---|---|
| 1 | U6 | `capability/ops/boundary.py` | `01_boundary.md` | ✅ |
| 2 | U6 | `capability/ops/align.py` | `02_align.md` | ✅ |
| 3 | U6 | `capability/ops/read.py` | `03_read.md` | ✅ |
| 4 | U6 | `capability/ops/label.py` | `04_label.md` | ✅ |
| 5 | U6 | `capability/panel.py` | `05_panel.md` | ✅ |
| 6 | U6 | `capability/stream.py` | `06_stream.md` | ✅ |
| 7 | U6 | `capability/timeaxis.py` | `07_timeaxis.md` | ✅ |
| 8 | U7 | `capability/ops/search.py` | `08_search.md` | ✅ |
| 9 | U7 | `capability/mining.py` | `09_mining.md` | ✅ |
| 10 | U7 | `capability/factors.py` | `10_factors.md` | ✅ |
| 11 | U7 | `capability/inference.py` | `11_inference.md` | ✅ |
| 12 | U7 | `capability/stats.py` | `12_stats.md` | ✅ |
| 13 | U7 | `capability/params.py` | `13_params.md` | ✅ |
| 14 | U7 | `capability/registry.py` | `14_registry.md` | ✅ |
| 15 | U7 | `capability/spec.py` | `15_spec.md` | ✅ |
| 16 | U7 | `capability/impls.py` | `16_impls.md` | ✅ |
| 17 | U7 | `capability/runtime.py` | `17_runtime.md` | ✅ |
| 18 | U8 | `capability/ops/evaluate.py` | `18_evaluate.md` | ✅ |
| 19 | U8 | `capability/ops/observe.py` | `19_observe.md` | ✅ |
| 20 | U8 | `capability/ops/verdict.py` | `20_verdict.md` | ✅ |
| 21 | U9 | `capability/ops/allocate.py` | `21_allocate.md` | ✅ |
| 22 | U9 | `capability/ops/combine.py` | `22_combine.md` | ✅ |
| 23 | U9 | `capability/ops/backtest.py` | `23_backtest.md` | ✅ |
| 24 | U9 | `capability/backtest_engine.py` | `24_backtest_engine.md` | ✅ |
| 25 | U10 | `capability/ops/certify.py` | `25_certify.md` | ✅ |
| 26 | U3 | `storage/catalog.py` | `26_catalog.md` | ✅ |
| 27 | U3 | `storage/layout.py` | `27_layout.md` | ✅ |
| 28 | U3 | `storage/objects.py` | `28_objects.md` | ✅ |
| 29 | U3 | `storage/index.py` | `29_index.md` | ✅ |
| 30 | U3 | `storage/ledger.py` | `30_ledger.md` | ✅ |
| 31 | U3 | `storage/gc.py` | `31_gc.md` | ✅ |
| 32 | U3 | `upstream/store.py` | `32_upstream_store.md` | ✅ |
| 33 | U3 | `upstream/fetch.py` | `33_upstream_fetch.md` | ✅ |
| 34 | U4 | `runtime/compiler.py` | `34_compiler.md` | ✅ |
| 35 | U4 | `runtime/gate_b.py` | `35_gate_b.md` | ✅ |
| 36 | U4 | `runtime/store.py` | `36_runtime_store.md` | ✅ |
| 37 | U4 | `runtime/envelope.py` | `37_envelope.md` | ✅ |
| 38 | U4 | `runtime/charter.py` | `38_charter.md` | ✅ |
| 39 | U5 | `orchestration/capability_bridge.py` | `39_capability_bridge.md` | ✅ |
| 40 | U5 | `orchestration/executor.py` | `40_executor.md` | ✅ |
| 41 | U5 | `orchestration/station.py` | `41_station.md` | ✅ |
| 42 | U5 | `orchestration/events.py` | `42_events.md` | ✅ |
| 43 | U5 | `orchestration/state.py` | `43_state.md` | ✅ |
| 44 | U1 | `contracts/types.py` | `44_types.md` | ✅ |
| 45 | U1 | `contracts/digest.py` | `45_digest.md` | ✅ |
| 46 | U1 | `contracts/errors.py` | `46_errors.md` | ✅ |
| 47 | U1 | `contracts/exit_codes.py` | `47_exit_codes.md` | ✅ |
| 48 | U1 | `contracts/plan_schema.py` | `48_plan_schema.md` | ✅ |
| 49 | U2 | `facilities/log.py` | `49_log.md` | ✅ |
| 50 | U2 | `facilities/cache.py` | `50_cache.md` | ✅ |
| 51 | U2 | `facilities/trace.py` | `51_trace.md` | ✅ |
| 52 | U2 | `facilities/clock.py` | `52_clock.md` | ✅ |
| 53 | U2 | `facilities/rng.py` | `53_rng.md` | ✅ |
| 54 | U2 | `facilities/digest_svc.py` | `54_digest_svc.md` | ✅ |
| 55 | U2 | `facilities/notify.py` | `55_notify.md` | ✅ |
| 56 | U11 | `app/contract.py` | `56_contract.md` | ✅ |
| 57 | U11 | `app/cli/__main__.py` | `57_cli_main.md` | ✅ |
| 58 | U11 | `app/cli/common.py` | `58_cli_common.md` | ✅ |
| 59 | U11 | `app/cli/run.py` | `59_run.md` | ✅ |
| 60 | U11 | `app/cli/station.py` | `60_station.md` | ✅ |
| 61 | U11 | `app/cli/stations.py` | `61_stations.md` | ✅ |
| 62 | U11 | `app/cli/inspect.py` | `62_inspect.md` | ✅ |
| 63 | U11 | `app/cli/explain.py` | `63_explain.md` | ✅ |
| 64 | U11 | `app/cli/fetch.py` | `64_fetch.md` | ✅ |
| 65 | U11 | `app/cli/doctor.py` | `65_doctor.md` | ✅ |
| 66 | U11 | `app/cli/estimate.py` | `66_estimate.md` | ✅ |
| 67 | U11 | `app/cli/pipeline.py` | `67_pipeline.md` | ✅ |
| 68 | U11 | `app/cli/promote.py` | `68_promote.md` | ✅ |
| 69 | U11 | `app/cli/factors.py` | `69_factors.md` | ✅ |
| 70 | U11 | `app/cli/factor_db.py` | `70_factor_db.md` | ✅ |
| 71 | U11 | `app/cli/holdout.py` | `71_holdout.md` | ✅ |
| 72 | U11 | `app/cli/live.py` | `72_live.md` | ✅ |
| 73 | U11 | `app/cli/reconcile.py` | `73_reconcile.md` | ✅ |
| 74 | U11 | `app/cli/replay.py` | `74_replay.md` | ✅ |
| 75 | U11 | `app/render/json_fmt.py` | `75_json_fmt.md` | ✅ |
| 76 | U11 | `app/render/ndjson.py` | `76_ndjson.md` | ✅ |
| 77 | U11 | `app/render/table.py` | `77_table.md` | ✅ |
| 78 | U11 | `app/render/__init__.py` | `78_render_init.md` | ✅ |

> U12（36 测试文件 + 26 篇文档）作为收尾整体核对，不逐文件；`__init__.py` 纯导出文件并入所属模块说明。
> 汇总总账：`docs/architecture/v5/reviews/summary/v5_module_review_findings_detailed_20261118.md`
