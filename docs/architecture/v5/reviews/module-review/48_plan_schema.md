# 模块审查 48 · `v5/contracts/plan_schema.py`

> 审查日期：2026-11-18
> 依据：源码 434 行全文通读 + `plan_schema.json` 161 行逐键核对；依赖 `errors.py`（E1001/E1004/E1008/E1020/E1021）；设计 `18-plan-schema.md`、`s0/01-data-contracts.md`、`U9 组合回测`（allocate 风险最优模式 I23）；测试 `tests/v5/test_v5_plan_schema.py`（22 例）+ `tests/fixtures/v5/plan_valid.json`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_contracts.py` 的 E5 段（load_schema / caliber_field_names / validate_plan / cross-field）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`plan_schema.py` + `plan_schema.json` 是"计划该长什么样"的**唯一真相**：JSON 定义键全集与类型，Python 做结构层校验。它该做的四件事：

1. **结构层校验，不校 params（故意）**：只管键是否存在、类型/枚举/范围、跨字段自洽；各工位的 `params` 内部是 `type: map`，由能力注册表 `param_schema` 校验（源码:3-4）。这是刻意分层——若这里也校 params，就等于与 9 个工地的 param_schema 各维护一份真相。
2. **只返回违规清单，不抛错**：`validate_plan` 返回 `list[Violation]`（带 code/path/hint），由调用方决定是否 `assert_valid_plan` 抛 `FlowError`（源码:6-8）。未知键一律 E1001 拒绝，不"警告后忽略"。
3. **派生而非手抄**：`caliber_field_names()` 从 schema 的 `caliber` 四子块动态派生（:244-260），用于 E1020 口径泄漏检查——历史上编译器手抄 24 字段清单与 schema 两份真相，加键不同步就能让口径字段同时出现在两处且不报错。
4. **跨字段检查 X1–X7**：保证金关系、成本档升序、`max_weight×min_members≤1`、overlap_policy、neutralise_window、portfolio 三段窗口、time_axis 重叠、供给市场护栏——即使 `--no-strict` 也不放行自相矛盾（`validate_cross_field`，:411-420）。

**`plan_schema.json` 是唯一真相**：文件头 comment 写明"18-plan-schema.md 是它的可读渲染；改这里必须同步改文档（测试双向核对）"。实测 schema 根键 13 个（plan/baseline/supply/caliber/boundary/panel/target/search/evidence/verdict/portfolio/certify/operations），`schema_version=1`。

**加密特化落地**：
- `supply.market ∈ {spot,futures}` 已在 schema（:20，缺省 futures），且 `_check_cross_field` 强制 spot 只能用 ohlcv（:352-368）——现货无 funding/OI/basis，防止采集器逐个失败后记 skipped 的假成功。
- `caliber.execution.annualisation` 只允许 `calendar_365`（json:34），252 校验期即拒（C1）。
- `caliber.execution.funding` 只允许 `per_contract_events`（json:35），结算周期字段被 forbidden_paths 拒绝（C2）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `fm_probe_contracts.py` E5 段。

### 例 1 · schema 唯一真相 + 必查点（实测）

```
schema_version: 1
root keys: ['baseline','boundary','caliber','certify','evidence','operations',
            'panel','plan','portfolio','search','supply','target','verdict']
supply.market in schema: True  values=['spot','futures']
portfolio in schema: True
caliber.allocate keys: ['long_short','max_weight','min_members','net_exposure',
                        'quantiles','turnover_max','weighting']
allocate has constraint_mode-like key: []          # ← 用户必查点：约束模式未暴露
caliber_field_names count: 27
```

**逐条解读**：
- **`supply.market`、`portfolio` 已在 schema**（用户必查点，实测 True）——这是 U4/U9 回归修复的落点。spot 市场护栏也在 `_check_cross_field` 实现。
- **`caliber.allocate` 无约束模式字段**（用户必查点 P1）：七个键是 quantiles / long_short / weighting / max_weight / net_exposure / turnover_max / min_members，**没有任何 `mode`/`constraint`/`projection` 类字段**。这意味着 U9 决策项"combine 风险最优模式（`skfolio` HRP/CVaR）返回权重而非分数、allocate 是否 pass-through"在 schema 层**没有入口可声明**——当前是"分位建仓 + 约束投影"单路径。详见 D2 A-1。
- `caliber_field_names` 27 个，从 schema 四子块派生，供 E1020 检测口径泄漏。

### 例 2 · caliber_field_names() 与 params.json 同步（实测）

```
caliber_field_names count: 27
params.json top-level keys: ['capabilities','comment','defaults','schema_version','types']
caliber 字段名在 params.json 文本中出现的:
  ['entry','funding','horizon','initial_cash','min_width','neutralise',
   'overlap_policy','rebalance','transform','width_policy']
```

**判断**：`caliber_field_names()` 的唯一来源是 `plan_schema.json` 的 caliber 四子块——这是设计使然（schema 是真相），与 `params.json` 并非"两份真相需同步"的关系。`params.json` 是能力注册表的参数定义，其中出现 caliber 同名键是**合理的**（能力实现需要这些参数名），关键靠 E1020 检测"用户在 plan 的工位 params 里写了口径字段"——实测 `panel.params.horizon` 触发 E1020（见例 3）。同步方向正确：schema 加口径键 → `caliber_field_names` 自动跟上（lru_cache 仅缓存，不改来源）。

### 例 3 · 校验失败路径（实测错误码）

```
spot + datasets=['funding']  -> E1004 supply.datasets "现货市场不支持数据集 ['funding']"
portfolio fit+embargo>eval   -> E1021 portfolio.window "fit 终点加 embargo 晚于 eval 起点"
未知键 bogus_key             -> E1001 bogus_key "未知键 'bogus_key'"
panel.params.horizon(口径泄漏)-> E1020 panel.params.horizon "工位参数里出现口径字段 'horizon'"
rebalance(1)<horizon(5)      -> E1008 caliber.label.overlap_policy "必须声明处理方式"
```

| 非法 plan | 错误码 | 检查类型 |
|---|---|---|
| `supply.market=spot` + `datasets=[funding]` | E1004 | 跨字段供给护栏 |
| `portfolio.window` fit 终点+embargo>eval 起点 | E1021 | 跨字段 X6 |
| 任意未知键 `bogus_key` | E1001 | 结构层未知键 |
| `panel.params.horizon`（口径写在工位） | E1020 | 口径泄漏 |
| `rebalance<horizon` 且缺 overlap_policy | E1008 | 跨字段 X4 |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| A-1 | **`allocate` 约束模式未暴露（来自 U9）**：`caliber.allocate` 只有分位/权重/上限等 7 键，无 `mode`/`constraint` 字段；U9 决策项"风险最优模式（skfolio HRP/CVaR）返回权重而非分数、allocate pass-through"在 schema 层没有声明入口，当前只能走"分位建仓+约束投影"单路径。 | **P1** | 实测 `allocate has constraint_mode-like key: []`；`plan_schema.json:49-59`；plan §U9 D2/D6（I23 风险最优模式未接线） | **已确认差距，需意图裁决**：这是 U9 决策项③的前置——要支持风险最优合成器，须先给 `caliber.allocate` 加模式字段（如 `mode: quantile\|risk_optimal`）并让编译器/allocate 消费。本轮只审查不改（plan §3 决策项默认不改），列为 P1 待裁决。 |
| A-2 | Python 层不校 `params` 内容（`type: map`），参数类型/范围错误只能在运行期能力层暴露，非编译期。 | P2（设计确认） | `plan_schema.py:3-4`；`json:79,89` params 为 map | **已确认符合设计**：刻意分层避免两份真相。但 U4 编译期参数校验（U4-P1-1）当前仅注释占位——两个缺口叠加会让类型错误晚暴露，并入 U4 那条，不在本模块单独修。 |
| A-3 | `_closest` 用字符集合相似度而非编辑距离（:209-218），拼写提示质量一般（如把 `bogus_key` 猜成某合法键）。 | P2（体验） | `plan_schema.py:209-218` | **已确认符合设计**：docstring "不引编辑距离库，规则够用即可"。hint 是辅助，错误码/path 正确即可，低优先。 |
| A-4 | `plan_schema.json` comment 的"唯一真相"依赖 `tests/test_plan_schema.py` 双向核对守卫；本审查未跑该测试验证守卫本身的强度。 | P2（待核实） | `json:3` comment | **需确认**：测试文件存在（22 例），但"文档↔JSON 双向核对"是否覆盖所有键、是否每次 CI 跑，属 U12 测试体系范畴，不在本模块结论内。 |

**多了什么**：无冗余。`Violation`/`_Ctx`/`_forbidden_rules`/`_caliber_field_names` 各有明确职责；`lru_cache` 三处（load_schema/forbidden/caliber_names）是性能与单一真相的组合。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| JSON Schema 契约优先（schema 唯一真相，代码派生） | 采纳 | `plan_schema.json` 定义键全集，`caliber_field_names()` 从它派生，杜绝编译期手抄第二份（源码:253-259 记录历史 bug） |
| 返回违规清单 + 调用方决定抛错 | 采纳 | `validate_plan` 返回 `list[Violation]`，`assert_valid_plan` 才抛；便于文档片段校验（`strict_required=False`）与一次性列全部问题 |
| 未知键 fail-closed + 近似提示 | 采纳 | E1001 拒未知键，`_closest` 给最近似合法键提示；口径字段还会提示"只能写在 caliber.X" |
| 编译期也校 params 内部 | 拒绝（分层） | params 由能力层 `param_schema` 校，避免两处真相；代价是类型错误晚暴露（并入 U4 编译期校验缺口） |
| pydantic/jsonschema 运行时动态建模 | 拒绝 | 自研轻量结构层 + JSON 真相，零运行时魔法；加密场景要求确定性、可静态审查 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **JSON 唯一真相**：`SCHEMA_PATH = Path(__file__).with_name("plan_schema.json")`（:24），`load_schema` lru_cache（:50-53），根键 13 个实测。
2. **结构层不校 params**：params 为 `type: map`（json:79/89），`_check_node` 的 map 分支只验"是不是对象"（:177-179）——故意设计。
3. **`caliber_field_names()` 派生唯一来源**：从 `schema["root"]["caliber"]["keys"]` 动态取（:244-260），27 个键实测；E1020 检测口径泄漏（:263-274），`panel.params.horizon` 实测触发。
4. **supply.market/portfolio 已在 schema**：实测 True；spot 只能用 ohlcv 护栏（:352-368），实测 E1004。
5. **跨字段检查齐全**：X1 保证金（:300-302）、X2 成本档升序（:304-307）、X3 max_weight×min_members（:309-316）、X4 overlap（:318-324）、X5 neutralise_window（:326-333）、X6 portfolio 窗口（:335-350）、X7 time_axis（:370-381）；三码一致性注释（:342-344）确保编译期/运行期同码。
6. **`_collect_params` 覆盖面**：含 boundary/panel/target/evidence/verdict/portfolio/certify + search.algorithms[*]（:231-240），注释记录曾漏 portfolio 导致口径合法存在两份；allocate 是 Portfolio 子能力（口径在 caliber.allocate）故不在此列（:227-228）。
7. **`_within` 只比先后不追日历精度**（:384-389），符合 24/7 无日历（C7）。
8. **forbidden_paths 精准提示**：`funding_period`/`funding_interval_hours` 用 E1001 而非"未知键"通用提示（json:148-149），I34 结算周期是数据属性不是口径（C2）。

**发现的问题**：
- **A-1（P1）allocate 约束模式未暴露**——schema 层无入口，是 U9 风险最优模式决策项的前置缺口，本轮只列不改。
- 其余 A-2/A-3/A-4 为 P2 设计取舍/待核实。**无 P0 级 bug**；A-1 是功能缺口（需意图裁决）而非代码错误。测试 22 例覆盖结构/跨字段/泄漏。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `18-plan-schema.md` caliber.allocate 小节 | 当前描述分位/权重/上限参数 | 若裁决支持风险最优模式（A-1），先在此加 `mode` 字段说明，再改 `plan_schema.json`，再改编译器/allocate（改契约先改文档红线） |
| `18-plan-schema.md` X1–X7 | 与实现跨字段检查一致 | 无需更新 |
| `s0/01-data-contracts.md` | 口径四子块描述一致 | 无需更新 |
| `06-evaluation.md` / I23 | 风险最优模式返回权重而非分数已登记 | 待 A-1 裁决后同步 allocate 约束模式章节 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| **allocate 风险最优模式（skfolio）** | ❌ 不支持（A-1，P1） | schema 无 `mode` 字段；需先扩 `caliber.allocate` + 编译器 + allocate/combine pass-through，是 U9 决策项前置 |
| 新增口径字段 | ⚠️ 半配置化 | 改 `plan_schema.json` caliber 子块即可，`caliber_field_names()` 自动跟上（E1020 检测同步）；但须同步 18-plan-schema 文档 |
| 新增工位 | ⚠️ 需多处同步 | schema root 加块 + `_collect_params`(:231) 加名单（否则该工位口径泄漏检测失效）+ 文档 |
| 新数据集类型（清算/链上） | ⚠️ 需改 schema | `supply.datasets` 枚举写死 ohlcv/funding/open_interest/basis（json:21），加值需改 schema + 供给层 MARKET_DATASETS（C8 列集开放缺口的一部分） |
| 文档片段校验 | ✅ 支持 | `validate_plan(strict_required=False)` 只校出现键的合法性，不要求必填——供文档示例校验 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 强支撑——`annualisation` 只允许 `calendar_365`（json:34），252 校验期即拒
- **C2 资金费按事件**：✅ 强支撑——`funding: per_contract_events`（json:35）+ forbidden_paths 拒 `funding_period`/`funding_interval_hours`
- **C3 spot/futures 维度**：✅ 强支撑——`supply.market` 枚举在 schema（json:20）+ spot 只能 ohlcv 护栏（:352-368，实测 E1004）
- **C7 24/7 缺口**：✅ `_within` 按天比先后不追日历（:384-389）；`--no-strict` 也不放行字段自相矛盾
- **C11 流动性分层**：中性——schema 不直接约束分层，但 caliber.allocate 的 min_members/weighting 为可交易成员口径提供结构
- 其余 C4/C5/C6/C8/C9/C10/C12：不适用或由其他模块承载

## R1–R7 影响

- **R1 真数据跑通**：✅ 支撑——schema 结构校验是编译期第一道闸，错误 plan 早拒
- **R2 结论可信**：✅ 强支撑——C1/C2/C3 护栏在 schema 层固化，口径泄漏 E1020 阻止第二份真相
- **R3 因子零代码扩展**：⚠️ 负向——`supply.datasets` 枚举写死四种（C8 列集不开放），加清算/链上数据集要改 schema；allocate 约束模式缺失（A-1）也限制合成器扩展
- **R7 fail-closed**：✅ 强支撑——未知键 E1001、必填缺失 E1008、口径泄漏 E1020、跨字段矛盾 E1021 全拒，`--no-strict` 只放行必填不放行矛盾

---

## 结论

**本模块（含 plan_schema.json）是计划结构的唯一真相，434 行校验逻辑分层清晰、无 P0 级 bug，可基本确认通过，但带一条 P1 功能缺口待裁决。** 用户必查点逐条落地：①plan_schema.json 唯一真相——实测 schema_version=1、13 根键、Python 派生不手抄；②py 只做结构层不校 params——实测 params 为 map 只验对象类型，属故意设计；③caliber_field_names() 与 params.json 同步——27 个键从 schema 动态派生，E1020 检测实测命中 panel.params.horizon；④supply.market/portfolio 已在 schema——实测 True；⑤**allocate 约束模式未暴露（P1，来自 U9）**——`caliber.allocate` 实测无任何 mode/constraint 字段，是 skfolio 风险最优模式决策项的前置缺口，本轮按 plan §3 默认只审查不改，列为 P1 待裁决。其余 A-2/A-3/A-4 为 P2 设计取舍/待核实。

**请确认本模块审查结果**：
- ✅ 确认通过（A-1 列为待裁决 P1，不在本轮改） → 进入第 6 个模块 `log.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想就 A-1（allocate 约束模式）当场裁决 → 告诉我
