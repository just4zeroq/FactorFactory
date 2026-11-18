# 模块审查 10 · `capability/factors.py`

> 审查日期：2026-11-18
> 依据：源码 239 行全文通读；依赖 `mining.py`（`normalise_ast`）、`registry.py`（fields.json 9 字段 / operators.json 19 算子）、`storage/catalog.py`（因子库 `expressions(status="active")`）、`app/cli/live.py:583-603`（名字→AST 解析的**生产调用点**）；设计 `18-plan-schema.md` §`search.prior_pool`、`02-mining-interface.md` §11、`20-mining-algorithms.md` §2.3、`10-crypto-fit.md` §7、`11-field-operator-registry.md` §6、`07-components.md` §4.2；测试 `test_v5_prior_pool_multialgo.py`。
> 判据：`v5_module_review_plan_20261118.md` §0.0/§0.1。
> 实测方式：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_factors.py`（系统临时目录；因子库用 `tempfile.mkdtemp()`，未改仓库）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Factors 是**先验因子的唯一名字→AST 定义处**（不是算法、不是注册表）。它应保证：

1. **计划只写名字**：`plan.search.prior_pool` 是 `type=ref`（只接受引用标识），AST 在编排侧解析一次；能力层 `search.py` 只消费 `market_view.prior_candidates`——口径单一来源。
2. **三类引用同判据**：内置名（`mom60` 等）、`db:<name>`（库内精确引用）、`db:*`（库内全部 active）都走同一套 `_check_ast`：算子在注册表、arity 匹配、字段在注册表、窗口/滞后在 `param_domain` 内；非法先验整体 `E1015`，不"跳过这一条"。
3. **字段门禁显式留痕**：现货集引用 `funding_rate` 不是配置错误（跨市场复用是正常需求），但要进 `skipped` 带 `reason/detail`，由 Search 产物原样带出，可事后核对"声明 5 个、进池 2 个"。
4. **闭环回灌**：本轮通过裁决并 promote 的因子，下一轮 `db:<name>` / `db:*` 能命中；库里因子不豁免判据。
5. **反免费重试**：同名去重、未知名硬拒、Alpha101/Alpha191 等加密不可用族不得进清单。

**加密特化**：只使用已注册的 9 字段（含 funding/OI/basis 事件字段）与 19 算子；资金费因子取负号（持有成本）、不写 `funding_interval_hours`（I34）；futures-only 因子按字段可用性门禁而非按市场名硬编码。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 复现：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_factors.py`。

### F1 · 内置因子真实数量（`validate_all()` 实测）

```
count = 12
names = ['basis_carry','funding_carry','hl_range','mom20','mom5','mom60',
         'oi_change20','rev1','rev5','rv20','vol_ratio','vwap_gap']
len(BUILTIN_FACTORS) = 12 ; available_names() 与 names 一致
```

**判定（用户指定必查）**：审查发现 12 个 **BUILTIN_FACTORS** 属实（`factors.py:52-78`，实测 `count=12`）。文档 `07-components.md:121/129/133/189/217/260`、`08-reuse-audit.md:164`、`11-field-operator-registry.md:176/178/186` 称"内置因子扩充的真实供给是 Alpha158 的 158 个"——**层次混淆**：那 158 个是 **qweave 内核可按模板计算的量价特征**（`07-components.md:121` 实测 158/158 产出非空），不是 `factors.py` 里可按名字写进 `prior_pool` 的先验条目。当前 `prior_pool` API 的真实供给是 **12 个**（`20-mining-algorithms.md:60` 的"mom60 等 12 个"是对的）。**代码正确，文档需改**（见 D2 F-1 / D5）。

### F2 · 12 个因子的规范化 AST（实测，节选）

```
mom60      -> {"op":"div","args":[{"op":"sub","args":[{"field":"close"},
                                    {"op":"ref","args":[{"field":"close"},{"const":60}]}]},
                                    {"op":"ref","args":[{"field":"close"},{"const":60}]}]}
funding_carry -> {"op":"mul","args":[{"const":-1},
                   {"op":"mean","args":[{"field":"funding_rate"},{"const":7}]}]}
basis_carry -> {"op":"mean","args":[{"field":"basis"},{"const":7}]}
rv20       -> {"op":"std","args":[{mom1 的 AST},{"const":20}]}
vol_ratio  -> {"op":"div","args":[{"field":"volume"},
                   {"op":"mean","args":[{"field":"volume"},{"const":20}]}]}
（完整 12 条见临时脚本输出）
registry: fields=9  operators=19  _FUTURES_ONLY_FIELDS={'basis','funding_rate','open_interest'}
```

### F3 · 字段门禁（`resolve_prior_pool` 实测）

输入：`names=["mom60","funding_carry","oi_change20","basis_carry"]`，`available_fields={"close","volume","high","low","vwap"}`

```
进池 resolved = ['mom60']   （required_fields=['close']）
排除 skipped  = [
  {'name':'funding_carry','reason':'field_unavailable','detail':'funding_rate'},
  {'name':'oi_change20',  'reason':'field_unavailable','detail':'open_interest'},
  {'name':'basis_carry',  'reason':'field_unavailable','detail':'basis'},
]
```

注：现货集上 **12 个名义因子 → 9 个有效**（3 个 futures-only 被显式排除），不是静默少几个。

### F4 · 配置层禁引用 + 失败路径（实测错误码）

| 输入 | 实测输出 |
|---|---|
| `factor_ast("alpha001")` / `"alpha_101"` / `"Alpha191_1"` / `"KMID"` / `"ROC6"` | `E1015` "先验因子未登记" |
| `factor_ast("mom60_typo")` | `E1015`（不做模糊匹配） |
| `resolve_prior_pool(["nope"], ...)` | `E1015` |
| `resolve_prior_pool(["db:*"], available_fields=..., catalog=None)` | `E1015` "需要因子库，但调用方没有提供" |
| `resolve_prior_pool(["db:nope"], catalog=非空)` | `E1015` "因子库里没有 'nope'" |
| `resolve_prior_pool(["mom20","mom20"], ...)` | 进池 1 条（同名去重），无 skipped |

### F5 · `db:` 先验回灌（临时 Catalog，实测）

```
库内容（status=active）: db_mom(div), db_abs(abs), db_fund(abs(funding_rate))
resolve_prior_pool(["db:*"], available_fields={"close"}, catalog=cat):
  进池 = ['db_mom','db_abs']
  排除 = [{'name':'db_fund','reason':'field_unavailable','detail':'funding_rate'}]
resolve_prior_pool(["db:db_abs"], ...): 进池 = ['db_abs']
```

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| F-1 | **文档"158 个内置因子"与代码 12 个不符（层次混淆）**：`07-components.md:133` "内置因子扩充的真实供给是 Alpha158 的 158 个量价因子"、`11-field-operator-registry.md:178` "内置因子（Alpha158 那 158 个）"会被读作 `prior_pool` 可用 158 个名字；实测 `BUILTIN_FACTORS` 只有 12 个，`factor_ast` 对 qlib 名（`KMID`/`ROC6`）与 Alpha101/191 名一律 `E1015`。 | **P1（文档）** | `factors.py:52-78,144-151`；实测 `count=12`；`07-components.md:121,133`；`11-field-operator-registry.md:176-186`；对照 `20-mining-algorithms.md:60`（写 12，正确） | **已确认：代码正确、文档误导**。158 是 qweave 内核模板能力，不是 V5 先验名。直接影响 R3（研究员以为有 158 个免费先验）。修文档：明确"prior_pool 12 个手写先验；Alpha158 158 个是 evaluate 层模板，当前未暴露为名字；Alpha101/191 配置层禁引用" |
| F-2 | **"加载即自检 E1015"在生产路径未发生**：docstring `:10-16` 称"三条校验在加载时全做"，但 `validate_all()`（`:226-232`）全仓只在测试里调用；生产入口 `app/cli/live.py:587-595` 直接调 `resolve_prior_pool`，它只对**被引用到**的因子在 `_emit` 里做 `_check_ast`（`:186-187`）。一条写错但没被任何计划引用的内置因子不会被加载期发现。 | P2 | `factors.py:10-16,226-232`；`live.py:587-595`；`resolve_prior_pool._emit:185-195` | **已确认（需确认意图）**：实际是"按需校验"，未使用的坏条目不触发 E1015。要么在 `extract`/`live` 启动时调一次 `validate_all()`，要么把 docstring 改成"被引用时校验" |
| F-3 | **`_FUTURES_ONLY_FIELDS` 是死常量**：`:83` 定义后全仓零引用（grep 仅此一处）；`:80-82` 注释称"这里再拦一次"，但实际门禁是 `_emit` 的 `available_fields`（`:188-193`）通用字段比对，并不是按这个集合的市场语义拦截。 | P2（代码卫生） | `factors.py:80-83,188-193`；grep 全仓仅 `:83` | **已确认**：删掉该常量或把它真正用于 `available_fields` 的语义校验；当前行为正确（3 个 futures-only 因子在现货集被排除，实测），常数只是注释性的 |
| F-4 | **加密流进面板的状态需对齐**：`10-crypto-fit.md:116` 称 funding_rate"未进面板（只用于回测成本）"，但 `live.py:580` 的 `_inject_event_fields` 已把事件字段注入、`resolve_prior_pool` 按 `available_fields` 门禁（实测 futures-only 因子在现货被排除）。文档与当前代码状态脱节。 | P2（文档，跨 U6） | `live.py:580-595`；`10-crypto-fit.md:116`；实测 F3 | **需确认意图**：属 U6 数据流文档陈旧，本模块只报告不改；建议 U6 复核后更新 `10-crypto-fit.md:116,168` |

**多了什么**：无冗余因子。`mom5/mom20/mom60` 与 `rev1/rev5` 有重复语义（反转=动量取负）但作为**不同名字的先验**是预期的（不同 horizon），不算冗余实现。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 内置先验池（预注册、整批声明、同等裁决） | **采纳** | `02-mining-interface.md:313-330`：先验不是算法；`mom60` 是 ACCEPTANCE 已知答案锚点（年化 SR≈0.675） |
| 因子库闭环（`db:*` / `db:<name>` 回灌） | **采纳** | 实测 `db:*` 命中 active 因子、缺字段显式跳过；"上轮结论进下轮假设"是研究正常节拍；库条目不进指纹（加因子不使历史 plan 失效） |
| qweave Alpha158 158 个模板 | **内核层采纳，先验名未暴露** | 实测 `factors.py` 无 qlib 名；若要 158 个先验需新增名字解析层（配置/注册表），属 R3 扩展项，不是当前 bug（见 F-1） |
| WorldQuant Alpha101 / 国泰君安 Alpha191 | **配置层拒绝** | 实测名字一律 `E1015`；`10-crypto-fit.md:48-62`：需 cap/industry/hml/mkt/smb，加密无这些概念（0/101、0/191 可用） |
| 逐条追加先验、边跑边加 | **拒绝** | `02-mining-interface.md:330,370`：借字典扩充免费重试；同名去重 + 整批声明堵口 |
| LLM/规则自动生成先验名 | **拒绝（本层）** | 名字→AST 必须唯一、可指纹；LLM 只能产出候选再经裁决 promote 入库，不能直接写进 `BUILTIN_FACTORS` |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：

1. **名字是唯一身份**：`BUILTIN_FACTORS` 的键唯一，`factor_ast` 未知名即 `E1015`、无模糊匹配（`:144-151`）；实测 6 个非法名全拒。
2. **三条 AST 校验完整**：`_check_ast` 校验字段注册（`:95-102`）、算子注册与 arity（`:107-121`）、带参算子末位整数常量与 `param_domain` 边界（`:125-140`）；实测 12 个因子全部通过 `validate_all()`。
3. **AST 规范化后再用**：`factor_ast` 返回 `normalise_ast(...)`（`:151`），`_emit` 也先 `normalise_ast`（`:186`），与 evaluate 的内容身份规则同源（`mining.py:89-126`）。
4. **字段门禁不静默**：`_emit` 比较 `fields - have`，缺字段进 `skipped` 带 `reason/detail`（`:188-193`）；实测现货集排除 3 个 futures-only 因子并给出字段名。
5. **同名去重**：`seen` 集合（`:183,199-201`）；实测 `mom20` 两次只进池 1 条。
6. **库回灌闭环**：`db:*` 拉 `catalog.expressions(status="active")`，`db:<name>` 精确匹配 name/factor_id（`:202-221`）；实测命中/排除/未找到三态分别返回正确结构与错误码。
7. **库缺失/空库 fail-closed**：无 catalog → `E1015`（`:203-208`）；`db:<key>` 零命中 → `E1015`（`:214-218`）；实测通过。
8. **futures-only 因子语义正确**：`funding_carry` 取 `-1` 反映持有成本（`:71-73`）、`oi_change20` 用 `_ret`（`:75`）、`basis_carry` 用 `mean(basis,7)`（`:77`）；没有 `funding_interval_hours` / `rate*3` 类固定周期写法（I34）。

**发现的问题**：

- 见 D2 F-1（**P1 文档**：12 vs 158 层次混淆）、F-2（`validate_all` 生产未调用）、F-3（`_FUTURES_ONLY_FIELDS` 死常量）、F-4（加密流进面板文档陈旧，跨 U6）。**本模块无 P0/P1 级代码 bug**；`test_v5_prior_pool_multialgo.py:84-120,294-335` 覆盖自检、门禁、去重、db 回灌。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `07-components.md:121,129,133,189,217,260` | "Alpha158 全量 158 个可用""内置因子扩充的真实供给是 158 个" | **P1 必改**：明确这是 qweave **evaluate 层模板能力**，`prior_pool` 名字只有 12 个；研究员不能写 `"alpha001"`（实测 E1015） |
| `11-field-operator-registry.md:176-186` | "内置因子（Alpha158 那 158 个）是候选的预置来源" | **P1 必改**：同上，区分"内核可算"与"V5 可引用名"；补一句"当前 V5 先验清单=12 个手写 AST" |
| `08-reuse-audit.md:164` | 模板枚举"含 Alpha158 的 158 个量价因子" | 改为"内核能力；作为未来无状态算法候选，当前未接入 prior_pool" |
| `20-mining-algorithms.md:60` | "内置因子名（mom60 等 12 个）" | **正确，无需改**；可作为上面几篇修改的对齐基准 |
| `10-crypto-fit.md:48-62` | "Alpha101/191 配置层禁止引用" | **与代码一致**（实测全 E1015），无需改；保留作为反例锚点 |
| `10-crypto-fit.md:116,168` | "funding_rate 未进面板""五个加密流未进面板" | 与 `live.py:580-595` 现状不符（见 F-4）；请 U6 复核后进面板状态更新 |
| `factors.py:10-16` docstring | "三条校验在加载时全做" | 改为"被引用时校验"，或在生产入口补一次 `validate_all()`（见 F-2） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增内置先验名 | ❌ 需改代码 | 在 `BUILTIN_FACTORS` 加一条 AST（`:52-78`）；`validate_all` / `factor_ast` 自动跟上，但这是 Python 改动，不是配置 |
| 复用已挖因子作先验 | ✅ 零代码 | `db:<name>` / `db:*` 实测命中；promote → 下轮 prior_pool 闭环 |
| 引入 Alpha158 158 个先验 | ❌ 当前不支持 | `factor_ast` 只认 12 个名字；需新增"模板名→AST"解析层（配置或注册表），属 R3 扩展项（F-1） |
| Alpha101 / Alpha191 | ❌ 明确禁止 | 缺 cap/industry/hml/mkt/smb，实测 0 个可用（E1015），不得写进清单 |
| 新加密流进先验（清算/跨所价差） | ⚠️ 需两处 | 需 U6 先加字段进 `fields.json` 与面板，再在 `BUILTIN_FACTORS` 写 AST；`available_fields` 门禁自动生效 |
| 跨市场复用同一 prior_pool | ✅ 支持 | 现货缺 funding/OI/basis 时进 `skipped` 而非报错（实测），同一份清单可跨市场 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 不适用——先验只是 AST，无年化
- **C2 资金费按事件**：✅ `funding_carry=-mean(funding_rate,7)` 是"每 bar 已按事件聚合值"的滑动均值，不含 `rate*3`/`interval_hours`；上游聚合语义由 U6 负责（`10-crypto-fit.md:154-158`）
- **C3 spot/futures**：✅ 按字段可用性门禁（实测 3 个 futures-only 因子在现货被排除），没有用 exchange 推 market
- **C4 合约四元组**：❌ 先验层无合约维度（全局缺口）；`basis_carry` 用 `basis` 字段但不区分合约身份
- **C6 上市/下架**：⚠️ 本模块不判，依赖 `available_fields` 与上游面板掩码
- **C8 列集开放**：✅ 12 个先验用了 9 字段中的 close/volume/high/low/vwap/funding_rate/open_interest/basis（未用 open）；新字段需先在 registry 登记
- **C10 判据不可放松**：✅ 未知名/未注册算子/窗口越界全 E1015，配置层禁引用 Alpha101/191；同名去重防重复计数
- **C11 流动性分层**：⚠️ 本模块不判；先验候选与算法候选同走 Evidence/Verdict，不因"人工写的"豁免

## R1–R7 影响

- **R1 真数据跑通**：✅ 正（`mom60` 是已知答案锚点；`live.py:583-603` 生产路径实测可用）
- **R2 结论可信**：✅ 强支撑（先验不豁免判据、AST 与 evaluate 同源、门禁留痕）
- **R3 因子零代码扩展**：⚠️ **F-1 直接负向**——文档声称 158 个可用先验，实际 12 个；`db:` 回灌零代码是正项，但"158"误导会浪费研究员预期
- **R5 单因子可归因**：✅ 先验候选带 `generator=<name>` 与 `required_fields`，可与 GP 候选一样隔离复跑（`20-mining-algorithms.md:142`）
- **R6 迭代复用**：✅ 强支撑（`db:*` / `db:<name>` 实测闭环，库条目不进指纹）
- **R7 fail-closed**：✅ 未知名/无 catalog/空库/缺字段全显式拒绝或带原因 skipped

---

## 结论

**本模块代码质量高，无 P0/P1 级代码 bug，但发现一项 P1 级文档误导，必须修文档。** 实测确认内置因子只有 **12 个**（`validate_all().count=12`），文档 `07-components.md` / `11-field-operator-registry.md` 所称"158 个内置因子"是 qweave 内核模板能力，与 `prior_pool` 可引用名字不是一回事；`20-mining-algorithms.md:60` 的"12 个"才是正确基准。代码侧 12 个先验全部通过 AST/字段/arity/param_domain 校验，现货集上 3 个 futures-only 因子显式 skipped，`db:` 回灌与 Alpha101/191 禁引用均实测成立。三项 P2：F-2 `validate_all` 生产未调用（按需校验）、F-3 `_FUTURES_ONLY_FIELDS` 死常量、F-4 加密流进面板文档陈旧（跨 U6）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 11 个模块 `inference.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 12 vs 158 的 P1 文档裁决想当场确认 → 告诉我
