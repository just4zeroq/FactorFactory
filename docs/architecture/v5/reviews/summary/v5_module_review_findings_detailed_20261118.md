# V5 逐模块深度审查 · 汇总总账（78 模块）

> 日期：2026-11-18 · 范围：78 个生产模块逐模块六维审查，每份含 venv 真实运行输入输出
> 本轮**只读审查，零代码改动**。逐模块文档见 `module_review/01_*.md`–`78_*.md`，进度见 `00_progress.md`。
> 判据：生产可用 = 研究员能真的拿它挖因子（R1–R7）；加密检查表 C1–C12。

---

## 一、总体结论

78 个模块逐模块过六维后，**架构与判据主体经住核对**：B1–B4 四类历史事故（raw/score 隔离、同根无延迟、rebalance 冻结、√(365/h) 年化 + bridge 传 h）在 `19_observe.md` 实测全部闭合；八指纹唯一公式、Call 唯一实现、49 错误码连续、门禁 B 三条件、因子库闭环、GC/index 均已实现——这些"地基级"模块质量高，绝大多数无 P0/P1。

汇总出的可裁决缺陷集中在 **4 个根因**（下表），不少在多个模块重复出现（同一根因的不同表现），去重后实际需裁决的技术问题不多。

---

## 二、P0 清单（影响"唯一可发布结论"或"能否真挖因子"，必修）

| # | 根因 | 定位（文档:行号） | 实测证据 | 影响 |
|---|---|---|---|---|
| **P0-A** | **资金费双重计费** | `24_backtest_engine.md`：`backtest_engine.py:332` 传 funding_df + `:346` 再扣 prev_w×funding；ml4t `broker.py:747` 内部已按 `-qty×mark×rate` 扣并更新 cash | 净收益被扣 **约 2.33 倍**；Certify 逐折复用同一引擎 → 错误进 OOS 净收益/夏普 | 污染**唯一可发布的 OOS 结论**（R2） |
| **P0-B** | **`tested:false` 的 watch 拿满权重进 OOS** | `20_verdict.md:287,299`（tested 写入但全仓无读取）+ `22_combine.md:42,141,423` + `25_certify.md:235-240` | verdict 写的 tested 字段无任何下游读；combine 不区分"证据不足 watch"与"未检验 watch"，两者均拿满权重；违反 `s0/04:451`「watch 不进组合」 | 未检验因子进认证（R2）——**设计裁决**，需定"降权/排除"还是"改文档承认" |
| **P0-C** | **加密流缺口（R3 最大 alpha 缺口）** | `03_read.md` + `06_stream.md` + `72_live.md` | 清算流、跨所价差两流未接面板；合约四维身份（underlying/contract_type/expiry/settle）全仓零实现；列集白名单按列名固定 | 期限结构/跨合约/资金费动量因子无法零代码扩展（R3）——全局缺口，非单模块可补 |
| **P0-D** | **测试验证链未入库** | 总账层（此前审查已记） | 27/36 测试文件被 `.gitignore:145` 忽略未跟踪，黄金锚点未入库 → fresh clone 后"775 passed"无法复现 | 验证链地基（本轮只读未复核状态，沿用上轮结论） |

> P0-A 是本轮唯一"纯代码 bug 且量化污染结论"的缺陷，优先级最高。修 P0-A 后 `39_capability_bridge.md` 三调用点（`:645,785,970`）传播问题自动闭合。

---

## 三、P1 清单（去重后，按根因归并）

| 根因 | 出现位置 | 说明 | 裁决方向 |
|---|---|---|---|
| **ticket 双读者分叉** | `26_catalog.md:240-245`（读首）vs `40_executor.md:988-995`（读末）；暴露于 `62_inspect.md` | 同一 ticket 多 parcel 时，inspect/单命令看到 payload v=1、certify 看到 v=2（实测 FORK=True），"真相"不一致 | 统一为单一 reader；先定 append_only 语义（首不可改→取首）再改代码与文档 |
| **funding 8h 合法间隔误判 gap→E4005** | `32_upstream_store.md:504-530` | 事件 gap 容差=3×中位间隔，1h 主导 + 合法 8h 结算间隔被判 interior gap → gate_a 不过 → 门禁 B E4005，资金费因子进不了成本链（已实测复现；现有测试用 8h 主导输入掩盖） | 改容差按合法结算周期判定 + 补测试 |
| **allocate 约束模式未暴露** | `21_allocate.md:272-277` + `48_plan_schema.md` | 约束投影已实现但 caliber.allocate 无 mode/constraints 字段，CLI 不可达 | 先改 `18-plan-schema.md` 契约，再补 schema + compiler |
| **skfolio 风险最优未接线** | `22_combine.md:44` + `impls.py:54-60` | HRP/CVaR 未接入 combine/certify（用户此前裁决"只审查不改"） | 保持审查项，待单独裁决 |
| **`available_from` 语义错** | `22_combine.md:23-27,149` | 置为 as_of，`s0/04 §3.8` fit.start≥max(available_from) 检查未实现（E1019 相关，已知暂缓） | 需编排层改，确认是否本轮 |
| **杠杆>1 引擎内无 E1004** | `24_backtest_engine.md:302-309` | 由 ml4t 强平模拟，与 ops 层口径不同 | 确认意图 + 补文档 |
| **DSR 全失败告警断链** | `67_pipeline.md:92` vs `59_run.md:167-180` | `collect_alerts` 读 `run_data["dsr_all_rejected"]`，但 run 信封从不产出该键（全仓仅 1 处引用）→ 端到端永不触发 | run 信封补字段或直读 Verdict |
| **reconcile 两处缺陷** | `73_reconcile.md`：①`cli/__init__.py:150` 未注册 --format（实测 exit 2）；②`reconcile.py:114` 坏 JSON 无 try/except → E3004/exit1 而非 E2005/exit3 | 告警/核对命令可靠性 | 加 --format choices；登记 JSONDecodeError 为 violation |
| **replay evaluate 命令层自算 IC** | `74_replay.md:161-180` | 与 docstring"命令层不自己算指标"矛盾，可能与 observe IC 口径分叉 | 裁决：仅诊断值 or 复用 observe 能力 |
| **内置因子数量文档不符** | `10_factors.md`（文档 P1，非代码） | 实测 12 个 BUILTIN_FACTORS，`07-components.md:133`/`11-field-operator-registry.md:178` 称 158 个（实为 qweave Alpha158 模板能力，非 prior_pool 名字） | 改文档 |
| **`78 测试未跟踪`** | 同 P0-D | — | — |

---

## 四、已推翻的过期基线（须先更正文档，避免误导后续开发）

| 旧基线说法 | 本轮实测 | 证据文档 |
|---|---|---|
| "五加密流未进面板" | **错**：funding/OI/basis 三流已进面板且 GP 可引用；仅清算+跨所两流缺（`live.py:358-438`） | `03_read.md`/`72_live.md` |
| "GC/index 未做" | **错**：均已实现（reconcile/一致性校验/引用计数回收/删库重建） | `29_index.md`/`31_gc.md` |
| "内置 158 因子" | **错**：真实 12 个 BUILTIN_FACTORS，158 是 qweave 内核模板能力 | `10_factors.md` |
| "log 过度自研" | **不成立**：已 structlog 化；F5/F7/轮转/E5001 在对应模块，不在 log | `49_log.md` |
| "OI 25 行影响 estimate" | **不成立**：estimate 是纯算式模块 | `66_estimate.md` |

---

## 五、逐模块一句话结论总表

### U6 工位基础（7）
- `01_boundary` 两张掩码/理由码优先级/时间轴 fail-closed 全对，无 P0/P1。
- `02_align` PIT 对齐因果探针三项能真失败，无 P0/P1。
- `03_read` 读取逻辑正确，但承载 R3 加密流缺口（P0-C）。
- `04_label` forward_raw/build_raw_by_h 实测逐位一致，R2 核心，无 P0/P1。
- `05_panel` 稳健值对象、容器列集本就开放，无 P0/P1。
- `06_stream` 供给侧不变量守卫强，合约四维缺失（P0-C），无自身 P0/P1。
- `07_timeaxis` 唯一时间实现、末日最后一纳秒/洞/重叠全闭合，无 P0/P1。

### U7 搜索与内核（10）
- `08_search` 四方法/生成不评估/共享计数/内容身份去重全实测，无 P0/P1。
- `09_mining` _OpCodes 与 19 算子 arity 对齐、deap 随机状态进出不变，无 P0/P1。
- `10_factors` 代码正确，文档"158 因子"是 P1 误导。
- `11_inference` 行内置换修 V4 头号 bug（p 0.307→0.010），无 P0/P1。
- `12_stats` transform/覆盖率/冻结/标签边界实测正确，无 P0/P1。
- `13_params` defaults 不进指纹/fail-closed，无 P0/P1。
- `14_registry` 加载即自检 E1015、白名单，无 P0/P1。
- `15_spec` cacheable 派生成立，METHOD_STAGES 重复定义（P2）。
- `16_impls` 12 能力注册齐全幂等，无 P0/P1。
- `17_runtime` Call 全仓唯一 + __post_init__ 全校验 + 三派生指纹，最稳地基。

### U8/U9/U10 结论链（8）
- `18_evaluate` 求值口径 + funding 透传实测成立，无 P0/P1。
- `19_observe` **B1–B4 四项事故全部确认闭合**，无 P0/P1。
- `20_verdict` 判据实现正确，但 tested 字段无下游读取（P0-B 写端）。
- `21_allocate` 分位/约束两路径 + rebalance 真冻结，约束模式未暴露（P1）。
- `22_combine` 三合成器/corr_max/Löwdin 实测通过，watch 满权重（P0-B）。
- `23_backtest` 向量化参照口径正确，已非生产实现。
- `24_backtest_engine` **P0-A 资金费双重计费，净收益扣 2.33×**。
- `25_certify` 切折/manifest/OOS/资金费透传全确认，承接 P0-A 传播。

### U3 存储供给（8）
- `26_catalog` 因子库回灌闭环，承载 ticket 分叉 P1 一侧。
- `27_layout` 11 区 + FACT_KINDS 6 类实测正确。
- `28_objects` 内容寻址+不可变+篡改 E5004 实测通过。
- `29_index` reconcile/删库重建已实现（推翻旧基线）。
- `30_ledger` 锁内重扫回并防丢更新实测成立。
- `31_gc` 引用计数回收已实现（推翻旧基线）。
- `32_upstream_store` 方案 B/append-only 全通过，funding 8h 误判 gap（P1）。
- `33_upstream_fetch` ccxt 单客户端+连网护栏+零成功 E4002 全通过。

### U4/U5 运行时编排（10）
- `34_compiler` 五指纹唯一公式/baseline 展开/C20 E1020 实测，无 P0/P1。
- `35_gate_b` 三条件 + fail-closed 六道全实测拒绝。
- `36_runtime_store` ticket 单段校验防逃逸、四区权限互斥。
- `37_envelope` 凭据只存引用、传值 E2004。
- `38_charter` 共享 reader、读不到 E1008。
- `39_capability_bridge` 六通道 + data 落 Ref + **三调用点 funding 透传实测全绿**。
- `40_executor` 八工位唯一路径/cache/stale/M2/M3 全实测，ticket 分叉 P1。
- `41_station` 八规格表 + 映射 + 导入期自检。
- `42_events` 三类一次性事件落盘跨进程实测一行。
- `43_state` 六状态转移 + 非法转移 E3005 实测。

### U1/U2 契约设施（12）
- `44_types` 835 行无死类型，Call 唯一在 runtime（AST 实测）。
- `45_digest` 十指纹 kind 包装唯一公式，时间/路径/耗时不进指纹。
- `46_errors` 49 码唯一连续零缺号（脚本实测）。
- `47_exit_codes` 0/1/2/3/4 严格区分。
- `48_plan_schema` JSON 唯一真相，allocate 约束模式未暴露（P1）。
- `49_log` 已 structlog 化，轮转/闸门实测。
- `50_cache` opt-in 默认空、命中验 payload、落盘计数不跳过。
- `51_trace` 14 字段与 PARCEL_ORIGINS 一致。
- `52_clock` 冻结重放 F8、墙钟/单调钟分离。
- `53_rng` seed 派生确定性实测成立。
- `54_digest_svc` 纯转发无第二份公式。
- `55_notify` 3600s/10 条阈值+去重限流。

### U11 应用层（23）
- `56_contract` 17 命令元数据 + P1-P3 守卫 + 码→退出码唯一映射。
- `57_cli_main` 11 行 -m 薄壳正确透传。
- `58_cli_common` 信封/参数转换/供给上下文/ticket_id 实测成立。
- `59_run` 唯一生产入口两段编译 + dummy/live + 五指纹全绿。
- `60_station` 单步 probe/commit 四不变 + M2 重建。
- `61_stations` 17 行工位自省。
- `62_inspect` 查验逻辑对，暴露 ticket 分叉 P1。
- `63_explain` 参数来源逐叶子可追溯（default 来源未标注 P2）。
- `64_fetch` 市场护栏前置 + 零成功 E4002/exit4 实测。
- `65_doctor` 供给体检四问 + 空根 fail-closed。
- `66_estimate` 365 年化实测 73 期/年逐位验算。
- `67_pipeline` 告警逻辑对，DSR 全失败告警断链 P1。
- `68_promote` 因子库写侧幂等/裁决实测，mutation 虚报 writes_ledger（P2）。
- `69_factors` 只读查询壳。
- `70_factor_db` append-only 下架 + --yes 自守。
- `71_holdout` 一次性揭示与 reserved 语义对齐。
- `72_live` 8 工位真数据跑通，缺清算/跨所两流（P0-C 部分）。
- `73_reconcile` 三方核对对，--format 未注册 + 坏 JSON 两处 P1。
- `74_replay` 三命令跑通 read-only，evaluate 自算 IC（P1 待裁决）。
- `75_json_fmt`/`76_ndjson`/`77_table`/`78_render_init` 三格式渲染正确、逐字节等价、字段一致。

---

## 六、下一步（待用户裁决，本轮不动手）

1. **修不修、修哪些**：建议先修 P0-A（纯 bug、污染结论）→ P0-B（设计裁决）→ 测试地基 P0-D；P0-C（加密流）单独评估工作量。
2. **P0-B 方向**：combine 降权/排除未检验 watch，还是改 `s0/04` 文档承认现状？
3. **ticket 双读者**：统一取首还是取末（决定 inspect 与 certify 是否同源）。
4. **本轮 12 项 P1**：一次性纳入还是分期。
5. 裁决后再进"开发-测试-review-验收"修复阶段。
