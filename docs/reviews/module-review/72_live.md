# 模块审查 72 · `app/cli/live.py`

> 审查日期：2026-11-18
> 依据：源码 624 行全文通读；依赖 `common.build_supply_context`、`upstream.store`、`capability`（read/evaluate）、`capability.factors.resolve_prior_pool`、`orchestration.Executor`、`facilities`；设计 `09-app-storage.md`、`19-supply.md`、`10-crypto-fit.md` §7/§8、plan §0.0 R1/R3、§0.1 C2/C8/C9；测试 `test_v5_live_cli.py`（596 行）、`test_v5_prior_pool_multialgo.py`；真实调用 `venv/Scripts/python.exe` + `fm_review_b.py`（FakeExchange 真数据，系统临时目录）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改任何生产代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`live.py` 是 `--live` 路径的**应用层组合根**，回答三件事（docstring `:1-27`）：
1. **参数展开（`expand_station_inputs`）**：把计划的嵌套 `params/time_axis/caliber` 摊平成能力消费的扁平 `params + policy_view`，做唯一一次转换；schema 之外一个字段不造，缺的必填给**确定性默认**（与注册 enum 对齐），没有"运行期取不到就猜"的口子（`:72-91,256-301`）。
2. **供给注入（`build_live_supply`）**：从数据根读 parquet → `SupplyBatch` → 调 `read` 能力得 `FieldStream`；`supply_fn(station, params)` 按工位返回外部事实（Boundary 要 pin+field_stream、Panel 要 field_stream、Search 要 market_view）。能力层不读盘（AC31），打开文件只发生在这里（`:510-620`）。
3. **可复现 as-of**：`market_view.available_from` 取供给版本覆盖末端（发布事实），不取墙钟（`:572,599`）。

**三条硬边界**：
- **自采数据不实盘下单**：本模块只读文件、只调能力、不写账本、不产结论（docstring `:26`）；`supply_fn` 只返回事实通道，无任何 order/下单 API。
- **E1004 live≥1 算法**：校验在展开器 `_search_params`（`:134-136`），不在 compiler。
- **八工位跑通**：Boundary→Panel→Target→Search→Evidence→Verdict→Portfolio→Certify 全走真实能力（`test_run_live_completes_all_eight_stations`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实跑（Python 3.12.1）。`fm_review_b.py` 用 `test_v5_live_cli._plan/_bars/FakeExchange` 生成 8 标的 95 根日 bar，经真实 `fetch` 落 parquet，再 `run --live`。这是"自采数据→真能力"路径（非 dummy）。

### CLI 层：`run --live`（真实退出码与输出）

```
fetch exit = 0
  data = {"exchange":"binanceusdm","market":"futures","datasets":[
    {"dataset":"ohlcv","source":"ccxt:binanceusdm","symbols":8,"rows_added":760,"revised":0,"skipped":[]}]}
run --live exit = 0, ok = True
  parcels = 8, stage = S7-live-capability, live = True
ticket = 20261118T062714Z-727eca3f
八工位 trace = {Boundary:'done',Panel:'done',Target:'done',Search:'done',
                Evidence:'done',Verdict:'done',Portfolio:'done',Certify:'done'}
Boundary observable_mask_ref = True   # 真产物，非 dummy
Certify evidence_grade = out_of_sample
```

### 输入：`expand_station_inputs(plan)`（纯函数，实测）

```
station_params 键 = [boundary,certify,evidence,panel,portfolio,search,target,verdict]  # 8 个
policy_view 键    = [allocate,execution,label,net]                                   # caliber 四子块
```
**展平要点（实测断言，`test_expand_flattens_nested_plan…`）**：`boundary.k=5`、`time_axis.mine` 提升同级、`mask_policy=pit_dynamic` 默认补齐；`target.entry=next_close/horizon=3` 来自 caliber 镜像；`neutralise_window` 只在 policy_view 不进 params；`verdict.family=cs_live_a` 来自 trial_family；`combiner` 不进 params（走 impl 通道）。

### `build_live_supply` 的 `supply_fn`（实测返回键）

```
supply_fn('Boundary') → ['field_stream','pin']   # 只事实，无下单
supply_fn('Panel')    → ['field_stream']
supply_fn('Search')   → ['market_view']           # available_from/prior_candidates/event_fields
supply_fn('Unknown')  → []                        # 未知工位空（见 D2 L-4）
```

### 失败输入 → 错误输出（fail-closed，全部实测）

| 非法输入 | 错误码 | exit | 真实 message |
|---|---|---|---|
| `search.algorithms=[]`（E1004 在展开器） | E1004 | 2 | `live 模式 search.algorithms 至少要有一个算法`（`:134-136`） |
| `portfolio.combiner='magic_combiner'` | E1004 | 2 | `未知组合器 'magic_combiner'`（`:176-180`） |
| `boundary.oos='reserved'` 且要认证 | E1008 | 2 | `认证需要 boundary.time_axis.oos 给出未参与搜索的区间`（`:245-250`） |
| plan 缺 `caliber.label.horizon` | E1008 | 2 | `live 展开后 label.params 缺必填 'horizon'`（`:293-300`） |
| 缺供给就 `--live` | E4002 | 4 | `版本覆盖区间、标的集或新鲜度不满足本次时间轴`（编译期门禁，`test_live_without_published_supply…` 实测） |
| `fee_model=taker_with_funding` 但无 funding 数据集 | E4002 | 4 | `fee_model 要求按事件计资金费，但供给里没有 funding 数据集`（`:455-460`） |

> 默认不带 `--live` 仍走 dummy（兼容 S0/S1/S2），`live=False`、Boundary payload `kind=dummy`（`test_default_run_still_uses_dummy…`）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| L-1 | **五个加密流只接了三个**：funding/OI/basis 经 `_inject_event_fields` 注入面板（`:316-438`），但**清算/跨所价差未接**；`build_live_supply` docstring 也承认"OI/basis 仍未被任何成本模型消费"（`:521-524`）。R3 的五个加密因子里，期限结构仍无合约维度（全局 P0-5）。 | **P1（R3 最大缺口，落点在 U6 数据流 + 本模块注入）** | `live.py:316-324,521-524`；plan §0.0 R3 未通；对照 `10-crypto-fit.md` §7 | 非本模块单独能补（需 U6 流进面板 + 采集清算/跨所价差），但本模块是注入落点。 |
| L-2 | **`taker_with_funding` 计费口径 vs `caliber.execution.funding` 两套字段**：`_funding_supply` 只看 `net.fee_model∈{taker_with_funding}`（`:314,451-454`），而测试 plan 的 `execution.funding=per_contract_events` 是描述性口径。若研究员只写后者、net 写 `taker`，则不读 funding——"按事件计费"可能没真算。 | P2（口径歧义，需确认意图） | `live.py:314,451-454`；`test_v5_live_cli.py:61,68` | 需在文档明确"计费开关是 net.fee_model，execution.funding 仅描述"；或加一致性校验。 |
| L-3 | **`supply_fn('Unknown')→{}` 静默空**：未知工位请求供给拿到空 dict，可能掩盖拼错工位名（应 fail-closed）。 | P2（边界） | `live.py:616-618` | bridge 侧有 `E2008` 门禁（未在本次取证验证）；若 bridge 只请求已知工位则无风险。 |
| L-4 | **`version=_pkg_version("qweave")` 硬依赖**（`replay.py` 复用）：本模块不直接用，但事件字段注入依赖 `default_registry()`（`:367`）；registry 不可读时 `_derived_ref_fields` 静默按空处理（reconcile 同源）——不适用本模块。 | ✅ 本模块无此问题 | — | 记录以对照 replay。 |
| L-5 | **data_keys 对账**：`run` 经本模块时 `data.stage=S7-live-capability`、`live=True`（实测），但 `contract.py:87` run 的 data_keys 未列 `stage/live`。 | P2（元数据，微） | `contract.py:87`；实测 | U11 元数据对账统一处理。 |

**多了什么**：无冗余。`_flatten_block` 不保留 `impl`（`:76-77`）——避免"换实现同时改 params 指纹"是正确的边界感。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 组合根 / 依赖注入（`supply_fn`） | 采纳 | 打开哪个文件只在组合根发生（`:18-21`），能力层纯净、可单测（AC31） |
| 可复现 as-of（覆盖末端而非墙钟） | 采纳 | `available_from` 取供给版本 `coverage.end`（`:572`），候选身份跨机可复现（AC3） |
| 事件 as-of 前向填充（只 ffill 不 bfill） | 采纳 | `_event_field_grid` 用 `searchsorted(side=right)-1`（`:347-354`），t 行只拿 ≤t 事件（C9），实测对齐 |
| 资金费按事件求和（htt 周期常数乘） | 采纳（拒绝常数乘） | `_funding_grid` 按 `(t_{k-1},t_k]` 区间 `np.add.at`（`:475-507`），1h/4h 网格同总额收敛实测（AC34） |
| Python 交易日历判"未收盘" | 拒绝 | 24/7 无日历；未收盘靠供给 `end_closed`（C7，见模块 01） |
| 运行期取"最新"数据 / 猜测字段 | 拒绝 | 缺必填即 E1008（`:293-300`），默认只覆盖注册 enum（`:48-53`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **展平不造 schema 外字段**：`_label_params` 只写 caliber 里真有值的键（`:104-117`），避免 None 让必填检查误过；`neutralise_window` 不进 params（注释 `:96-98`）；实测 `E1008` 缺 horizon 在展开期拒。
2. **多算法全透传**：`_search_params` 用 `algorithms` 全列表（`:132-154`），修复旧实现 `algorithms[0]` 静默丢第二个算法（docstring `:128-130`）——GP 与随机基线对照成立。
3. **单一真相消歧**：`combiner` 只在 impl 通道（`:163-167`）、`oos_window` 只从 `boundary.time_axis.oos`（`:228-253`）、`domain` 用计划名固化（`:199-201`）——三处"两处真相必分叉"已闭合。
4. **资金费 fail-closed**：口径开了缺数据集/缺标的都 E4002（`:455-470`），不用全零网格冒充"已计费"；实测两失败路径。
5. **事件注入语义诚实**：数据没采集 → 不注入、不造零值（`:574-579`），NaN 语义明确；`_event_field_grid` 面板外标的中性计数（`:341-345`，实测 outside=1）。
6. **8 工位真跑**：实测 trace 全 done，Boundary 真掩码 ref、Certify out_of_sample、Search n_trials≥1（见测试 `:209-289`）。
7. **不实盘下单**：`supply_fn` 实测只返回事实通道；全模块无 order/place/buy/sell/trade API 调用。

**发现的问题**：L-1（五流缺二，P1 全局缺口落点）、L-2（计费口径双字段，P2）、L-3（Unknown 空，P2）、L-5（元数据，P2）。**无 P0 级代码 bug**；L-1 是 R3 未通的已知状态，非本模块引入的回归。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `19-supply.md` §2.4 | supply_fn 按工位返回事实 | 补 funding/OI/basis 注入状态表 + 清算/跨所价差未接说明（L-1）。 |
| `10-crypto-fit.md` §7 | 五流接入列为加密缺口 | 更新为"三流已注入、二流（清算/跨所价差）未接"，与 L-1 对齐。 |
| `09-app-storage.md` | `--live` 与 dummy 路径 | 明确"计费开关是 net.fee_model=taker_with_funding；execution.funding 仅描述"（L-2）。 |
| `contract.py:87` | run data_keys 未列 stage/live | 补 `stage/live`（L-5）。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增合成器（closed_multi 多选） | ⚠️ 封闭白名单 | `_COMBINERS` 三选一（`:69`），加值改一处 + impl 注册；能选不能加（plan D6 决策项）。 |
| 新增事件数据集（清算/跨所价差）| ❌ 需改代码 | `_EVENT_FIELD_BY_DATASET`（`:317-321`）加映射 + `_inject_event_fields` 走通用 read 路径；仍属 L-1 全局缺口一部分。 |
| 新增工位块（第九工位） | ❌ 不支持 | `_LIVE_STATIONS`/`_CAPABILITY_OF`/`station_params` 三处写死（`:57-64,263-272`）；加工位需同步。 |
| 先验池 `db:*` 回灌 | ✅ 已支持 | `:583-597` 用 `resolve_prior_pool` + Catalog；实测经 promote 入库的因子可回灌（见模块 68/69）。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：本模块不直接算，但 `net.caliber.annualisation=calendar_365` 经 policy_view 透传（`:285-289`）；无 252 依赖 ✅
- **C2 资金费按事件**：✅ `_funding_grid` 按事件求和（`:475-507`），1h/4h 收敛实测（AC34）；但计费开关双字段歧义（L-2）
- **C3 spot/futures**：`build_supply_context` 里 `require_market_dataset` 校验（`common.py:109`），能力层无第二道断言（同模块 01 B-1）
- **C4 合约四元组**：❌ 无 expiry/settle，期限结构因子不可做（全局 P0-5，非本模块）
- **C5 计价货币**：本模块不处理（数据集层）
- **C6 上市/下架**：`check_window` 覆盖 `mine→oos`（`common.py:118-119`），防晚上市标的 NaN 前视
- **C7 24/7 缺口**：`read` 能力 PIT/窗口校验（`:397-409`）；医生查缺口（模块 64）
- **C8 列集开放**：⚠️ funding/OI/basis 三流注入（`:316-438`），清算/跨所价差未接（L-1）
- **C9 as-of 只 ffill**：✅ `_event_field_grid` searchsorted side=right（`:347-354`，实测对齐）
- **C10 判据不放松**：✅ 判据经 caliber 透传，本模块不改 dsr/fdr
- **C11 流动性分层**：经 boundary mask_policy 透传（`:48`），本模块不判
- **C12 embargo 观测单位**：经 certify.params 透传（`:228-253`），本模块不算

## R1–R7 影响

- **R1 真数据跑通**：✅ **强支撑**——8 工位实测全绿，非 dummy（trace done + 真掩码 ref + out_of_sample 证书）。
- **R2 结论可信**：✅ 资金费按事件、as-of 只 ffill、证据等级诚实。
- **R3 因子零代码扩展**：⚠️ **负向**——五流缺二 + 合约维度缺失（L-1，全局 P0，非单独可补）。
- **R5 单因子可归因**：中性（本模块只装配，不拆因子）。
- **R6 迭代复用**：✅ `db:*` 先验回灌唯一通路（`:583-597`）。
- **R7 fail-closed**：✅ 缺供给 E4002、缺算法 E1004、缺字段 E1008、计费缺数据 E4002。

---

## 结论

**本模块是 8 工位真跑的装配核心，功能正确，可确认通过。无 P0 级代码 bug。** 展平单一真相、多算法全透传、资金费按事件、as-of 前向填充、不实盘下单五条边界全部实测成立，是 R1"真数据跑通"的关键支撑。**L-1（五流缺二）是 R3 未通的已知全局缺口**，本模块是注入落点但非唯一修复点；L-2（计费开关双字段）需文档明确或加一致性校验；L-3/L-5 为低优先边界/元数据。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 73 个模块 `reconcile.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ L-1 五流缺口或 L-2 计费双字段想当场裁决 → 告诉我
