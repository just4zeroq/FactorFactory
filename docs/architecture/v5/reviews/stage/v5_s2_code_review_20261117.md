# V5 S2（供给链真实化）代码审查报告

- 日期：2026-11-17
- 范围：`v5/upstream/`（acquire / gate_a / publish / lease / maintlog / loader）、
  `v5/storage/layout.py` 的 upkeep 路径、`v5/app/cli/refresh.py`、`v5/runtime/gate_b.py`、
  `v5/runtime/compiler.py` 第 9 步、`v5/app/cli/common.py` 的供给上下文加载。
- 方法：按模块「实现 → 独立审查（子代理只读）→ 主审实测复核 → 修复 → 回归」闭环，
  每个模块单独判定通过后才进入下一个模块。
- 基线：S2 起点 V5 239 passed；S2 结束 **347 passed**（全仓库 1095+ passed）。

---

## 1. 交付内容（M0–M5）

| 模块 | 交付 | 对应契约 |
|---|---|---|
| M0 | `Layout` 新增 `upkeep / lease_file / maintenance_log / staging_dir / seed_dir / symbol_sets / safe_segment` | 09 §3 分区、AC31 |
| M1 | `v5/upstream/lease.py`（filelock side-car，跨进程互斥、过期抢占、续期、释放）+ `maintlog.py`（append-only、固定 action/result、凭据词扫描） | U1/U2/U10 |
| M2 | `v5/upstream/gate_a.py`：七项检查（coverage / symbols / checksum / end_closed / fields / time_order / revision），失败用 `GateResult.passed=false` 表达而非抛 | U5/U6、AC13 |
| M3 | `v5/upstream/publish.py`：五阶段 + 幂等（seed 完整 **且** 别名已指向）+ 别名 `os.replace` 原子写 + 租约校验 | U4/U7/U9 |
| M4 | `v5/upstream/acquire.py`（分页、逐标的缺口、只写 staging、resume 签名校验）+ `loader.py`（ccxt-only，无工厂 E3003，空表 E3003） | 06 §7、10 §3.1 |
| M5 | `v5/app/cli/refresh.py`（采集→定稿→发布，partial→E4002/4）+ 门禁 B 真实契约 + compiler 第 9 步接线 + 端到端验收 | AC13–AC17、U11 |

### 1.1 门禁 B 的契约裁决（S2 最重要的一条）

`Pin` 九字段冻结，**只有 `symbols_digest`，没有成员清单**。原实现读
`pin.gate_a["symbols"]`（契约外字段）并含 `datetime.now()` 默认分支，既不可达也不合法。S2 裁决为：

1. 标的集：计划声明**标的集名**（`supply.symbols` 是 `str`），编译期由 Charter 的
   `schemas/symbol_sets.json` 解析成成员清单，算规范摘要与 `pin.symbols_digest` **相等**比较。
   相等比「子集」更严：少一个标的也失败。
2. 新鲜度：`pin.acquired_at >= pin.coverage.end`，纯函数；schema 没有 `max_age_days`，
   原分支是**不可达代码**，已删除。
3. 末端闭合：`pin.end_closed = true`；不再要求「末端不晚于研究终点」（覆盖 ⊇ 研究区间是正常的）。
4. 门禁 A：`pin.gate_a.passed is true`（E4005）。
5. **供给维度（新增）**：`plan.supply.frequency` / `.exchange` 与 seed manifest 的
   `timeframe` / `exchange` 比较（frequency 只对 `ohlcv`；funding/OI/basis 的结算是数据属性，I34）。

第 5 项是审查发现的关键漏洞：`supply` 整块进 `blueprint_digest`，但 Pin 不含频率/交易所，
原实现下「计划写 4h、数据是 1d」「计划写 binance、数据来自 okx」都会**全绿通过**。

---

## 2. 审查发现与修复

### 2.1 P0（S2 各模块，均已修）

| # | 问题 | 模块 | 修法 |
|---|---|---|---|
| P0-1 | `safe_segment` 用 `re.match` + `$`，允许尾部 `\n`（`"ohlcv\n"` 逃逸路径段） | M0 | 改 `fullmatch` + 拒绝控制字符 |
| P0-2 | 门禁 A 覆盖只比全局 min/max：中间整段洞、单标的停更**恒 PASS** | M2 | 按「标的 × 时间」逐段检查（边界 0.5×、内部 1.5× delta） |
| P0-3 | 阶段 4/5 之间 kill 后重发：只看 seed 存在就当幂等，别名永久卡旧版本 | M3 | 幂等 = `seed_complete and alias_points_here`，否则继续阶段 5 |
| P0-4 | `_detect_gaps` 把所有标的 pooled，别的标的能填补缺口 | M4 | 逐标的检查 |
| P0-5 | resume 不校验请求签名：换 timeframe/symbols 仍复用旧暂存 | M4 | manifest 写 `request_signature`，变了就丢弃重拉 |

### 2.2 P1（已修）

- **M1**：租约 pid 坏值抛裸 `ValueError` → 统一 E5003；`renew` 可复活过期租约 → 拒绝；
  租约日志按 lease_id 分文件导致碎片化 → 固定伪版本 `"leases"`。
- **M2**：末端闭合只看全局 max → 逐标的；revision 只给布尔 → 给 added/modified/removed 区间；
  checksum 排序键不稳定 → 整行 `canonical_text` 全序。
- **M3**：篡改 seed payload 静默记 ok → 重算 seed 字节 digest；`Pin.version` 用 `"v1"` 掩盖
  契约 → 发布边界校验 16hex；Windows `os.replace` 并发读 `PermissionError` → 短退避重试。
- **M4**：`bad_rows` 恒 0 → 回传计数；`read_payload` 可返回空表 → E3003。
- **M5（第一轮）**：
  - `--exchange` 为空时静默回落 `binance`，manifest 记空 → 拒绝空值、删默认兜底；
  - 同一份坏 `symbol_sets.json` 在 refresh 是 E3004/1、在 explain 是 E1004/2 → 统一；
  - `_prepare_resume` 只看 `partial` → 「已改名但 manifest.version 未同步」的暂存被当检查点，
    `--resume` 永久 E4004 → 判定加「目录名 == manifest.version + payload 可读且 checksum 一致」，
    并调换定稿顺序为「写 manifest.version → rename → 迁移日志」；
  - 别名键与 `Pin.dataset` 不一致（ohlcv → funding 的版本）→ E4001；
  - 别名指向的版本没有可读 manifest 仍能编译（seed 被删拖到执行期才报 E3003）→ E4001；
  - `explain` 的 `max_exit_code` 声明为 2，但它与 `run` 走同一套编译、能返回 4 → 契约修正为 4（文档已同步）。
- **M5（第二轮）**：
  - 暂存 manifest 是 JSON 数组时，清理路径 `.get` 抛 `AttributeError`，把**已成功**的发布报成 E3004/1，
    且 `--resume` 永久崩在同一文件 → 加 `isinstance(dict)` 守卫；
  - `_migrate_log` 用 `endswith` 判幂等，publish 之后目标文件已有追加 → 整段重复写 → 改 `blob not in merged`；
  - 编译器拿到 meta 后不校验 `manifest.dataset/version` 是否就是别名指向的那一份 →
    可能拿错版本的口径比对 → 加一致校验（E4004）；
  - manifest 损坏被吞成 E4001（换码）→ 原样抛 E4004；
  - `"fields": null` 靠「键存在」绕过发布比对 → 必须为列表；
  - `publish._pin_dict` 用 `str()` 把 int `schema_version` 归一，使 `Pin` 的新类型检查永不触发 → 拒绝非 str。

### 2.3 测试污染（两次）

向测试文件追加内容时，同一段被写入两次，形成**同名测试覆盖前者**（收集到的用例数少于文件内
`def test_` 数）。已清理两次，并把「追加后核对 `grep -c '^def test_'` 与 `--collect-only` 一致」
写进流程。教训：同名函数在 Python 里是**静默覆盖**，测试数量不能当作覆盖证据。

---

## 3. S2 退出门槛对照（12-stages §4）

| # | 门槛 | 状态 | 证据 |
|---|---|---|---|
| 1 | AC13–17 全绿 | ✅ | AC13 旧版本仍可读（`test_publish_moves_alias_forward_and_detects_revision`）；就地覆盖被拒（`seed_complete` 分支校验 checksum + 重算 digest）；末根未收盘 → E4003（`test_gate_b_end_not_closed`）；AC17 编译 FATAL 不建 Ticket、不触发下载（`test_compile_missing_alias_is_e4001_without_supply_writes` 断言 staging/seeds 不存在） |
| 2 | 断网/超时/半途失败 → 不出半份版本、退出码 4 | ✅ | `test_network_failure_exits_four_without_partial_object`、`test_partial_acquire_exits_four_and_keeps_alias`（staging 留证据、别名不动）、`test_kill_between_stage4_and_5_*` 两个恢复用例 |
| 3 | 从空目录 refresh → 可引用版本 → 编译通过 | ✅ | `test_e2e_refresh_then_compile_then_run`（refresh → Pin → seeds → 编译 → cmd_run 落盘 → explain） |
| 4 | 同一份数据连发两次 → 内容寻址命中同一版本 | ✅ | `test_publish_is_idempotent_same_content`（不重写、不改 acquired_at）+ `test_tampered_seed_payload_detected_on_republish` |

补充覆盖（审查新增）：计划频率/交易所不符必须失败、删 seed 必须 E4001、空 `--exchange` 必须 E1004、
坏暂存 manifest 不得影响发布、半定稿暂存 `--resume` 可恢复。

---

## 4. 未做 / 需裁决（不阻塞 S2 判定）

1. **`doctor` / `estimate` 未实现**。`IMPLEMENTATION-PLAN` 的 S2 任务表列了这两个命令，但
   `12-stages` 的 S2 退出门槛（AC13–17）不含它们，且 S6 明确负责「doctor 的真实检查项」、
   `estimate` 依赖 S4 的成本模型（`stages[].cost_bp`）。按「阶段只认 S、验收只认 AC」的纪律，
   判定为 **S6/S4 交付**，不在 S2 造空壳（造壳即「假成功」）。
2. **`E5002` 一码多义**（租约冲突 / 凭据缺失 / index 版本冲突）仍是既有错误表问题，需统一裁决。
3. **别名表 read-modify-write 与按 dataset 的租约不匹配**：两个数据集并发发布同一
   `_published.json` 会丢更新。今天 `refresh` 只允许 `ohlcv`，不可达；funding/OI/basis 落地前
   必须加跨数据集写锁或按数据集分文件。
4. **`ZONE_NAMES` AST 守卫不覆盖 S2 新增子区**（`staging` / `seeds` / `_upkeep`）：守卫只拦
   `ZONE_NAMES` 里的字面量。新方法都在 `layout.py` 内，没有破坏守卫，但守卫本身对子区不设防。
5. **`coverage.end` 是末根开盘时刻**，而计划 `mine` 的终点是日期端点：refresh `--end 2024-01-11`
   发布 `coverage.end=2024-01-10`，随后 `mine ..2024-01-11` 会 E4002。fail-closed 方向正确，
   但语义需在文档里钉死（「mine 终点是含当天 bar 的日期」）。
6. **`tests/v5/**` 被 `.gitignore` 的 `test_*.py` 忽略**，347 项验收证据进不了版本库（既有配置）。

---

## 5. 结论

S2「供给链真实化」**通过**：数据从 ccxt 进来到编译期固化 Pin 的链路是真实的，门禁 A/B 各自
fail-closed，别名只在「seed 完整 + 门禁通过」后原子移动，失败与部分成功一律不给成功码。
上面 6 项均为「契约缺口 / 后续阶段职责 / 不可达路径」，不构成本阶段缺陷。
