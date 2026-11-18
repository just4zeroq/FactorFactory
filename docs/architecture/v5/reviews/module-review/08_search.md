# 模块审查 08 · `v5/capability/ops/search.py`

> 审查日期：2026-11-18
> 依据：源码 545 行全文通读；依赖 `mining.py`（四方法实现 / `_FIT_LOCK` / `dedup_candidates`）、`inference.py`（`ic_series`）、`params.py`（`implementation_defaults`）、`registry.py`（`default_registry`）、`runtime.py`（`resolve` / `run`）、`impls.py`（实现注册）；设计 `02-mining-interface.md` §2/§6/§10/§12、`20-mining-algorithms.md` §2/§4；测试 `test_v5_search.py`、`test_v5_prior_pool_multialgo.py`。
> 判据：`v5_module_review_plan_20261118.md` §0.0/§0.1（六维 D1–D6 + C1–C12 + R1–R7；"生产可用=能真挖因子"）。
> 实测方式：`venv/Scripts/python.exe` 调用 `v5.capability.run(call)`，临时脚本 `C:\Users\lansa\AppData\Local\Temp\fm_review_search.py`（系统临时目录，未改仓库）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Search 是八工位第 4 步"候选生成"，是系统**唯一的开放扩展点**。它应回答四件事：

1. **四方法协议**：对外仍走单函数 `build(call)`（`runtime` 的 ImplFn 模型），内部落实 `describe / fit / generate / lineage`——`describe` 纯静态；`fit` 只在 `mine_only` 窗口内带反馈地评估；`generate` 只吐最终种群、**不评估不筛选**；`lineage` 可回溯父代/算子/生成器。
2. **三条禁令**：不判显著（fitness 只返回 |RankIC|，无 p 值/成本/判据）、不写计数（算法无账本句柄，计数在编排层注入的 fitness 闭包内登记）、不自选口径（fitness 调同一个 `evaluate` + 同一白名单版本 + 同一 `nan_policy`）。
3. **一次预算 + 内容身份去重**：多算法共享**一份** `seen`/`calls`，`$n_trials = |seen ∪ 最终候选|`；去重键只有 `candidate_id = H(规范化 payload + required_fields)`，不含 generator/seed；generator 只进谱系。
4. **fail-closed**：未知算法、`spec` 有而 runner 无、并发 `needs_fitness`、求值子调用全失败、零候选——全部显式报错，不静默降级成"跑了一个算法但什么都没做"。

**加密特化**：`available_from` 取编译期固化的 as-of（非墙钟）；候选可引用资金费/OI/basis 事件字段但字段门禁交给上游面板；不假设年化、不假设交易日；`unobservable` 是通道占位，逐候选原因由编排层回填。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 复现：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_search.py`。
> 面板为 40×6 的 1d bar（close/volume，随机游走，seed=0）；label 为 40×6 随机 score；`market_view.available_from="2024-02-10"`。

### 输入：`Call`（多算法，GP + 随机基线共享一次计数）

```
capability="search"  station="Search"  impl="gp_symbolic"  impl_version="1"  seed=17
params = {
  "algorithms": [
    {"name":"gp_symbolic",       "params":{"population":10,"generations":2,"max_depth":3,"max_candidates":12}},
    {"name":"random_expression", "params":{"max_candidates":12,"max_depth":3}},
  ],
  "fields":["close","volume"], "family":"f1",
}
inputs = (
  InputRef("panel",        <digest>, data=Panel(40×6, fields={close,volume})),
  InputRef("label",        sha256:111…, data={"score":(40,6) ndarray, "valid_mask":..., "effective_n":240}),
  InputRef("market_view",  sha256:333…, data={"mine_slice":[0,40], "available_from":"2024-02-10"}),
)
```

### 输出：`run(call)` 返回 dict 的实测值

```
count            = 22
n_trials         = 26            # |seen(含 fit 内评估被淘汰个体) ∪ 最终候选|，ratio=1.182
generators       = ['gp_symbolic@1', 'random_expression@1']
unobservable     = []            # 能力层只声明通道；实际逐候选原因由 capability_bridge:1270-1288 回填
dedup_digest     = sha256:6230af54bb376fa43b3dfd19c6d4f4dc66e2ace4a19dabbbe0118f9608192c74
fitted_state     = {"algorithms":[{gp 的 state}, {random 的 state}]}   # 长度=2，逐算法可审计
random.getstate() 进出不变 = True    # AC39
候选携带结论字段(ic/p_value/dsr/decision/cost/…) = False
```

`describe()` 实测（四方法之一，静态）：

```
gp_symbolic      : stateful=True  needs_fitness=True   fit_window=mine_only
                   max_candidates=40  fitness_objectives=['abs_rank_ic']
random_expression: stateful=False needs_fitness=False  fit_window=mine_only
                   max_candidates=40  fitness_objectives=[]
```

`generate()` 与 `fit()` 的衔接（实测）：`fit` 落盘 `final_genes` 6 条 → `generate` 复用后产出 6 个候选、`resumed_from_fit=True`；无 `fitted_state` 时退化为一次性随机种群、`resumed_from_fit=False`（不冒充续接）。`generate` 内 `generations=0`、`fitness=None`，**确实不评估**。

内容身份去重（实测）：`mul(close, 2)` 与 `mul(close, 2.0)` 的 `candidate_id` 相同；`generator=gp@1` 与 `generator=random@1` 的两个同表达式候选经 `dedup_candidates` 合并为 1 条、`generators=['gp@1','random@1']`。

### 失败输入 → 错误输出（实测错误码）

| 非法输入 | 实测输出 |
|---|---|
| `fit({"fields":["close"]}, {"algorithms":[...]}, 7)` | `E1004` "search.fit/generate 是单算法门面，不接受 params.algorithms" |
| `generate(..., {"algorithms":[...]}, 7, None)` | `E1004`（同上） |
| 多算法列表中 `name="not_registered"` | `E1004` "未知算法 'not_registered'" |
| 抢占 `_FIT_LOCK` 后跑 `needs_fitness` 算法 | `E2009` "检测到并发的自适应搜索" |
| `lineage("bad")` | `E3003` "lineage 的 candidate 必须是映射" |
| `Call.impl="random_expression"`（`impls.py` 只注册 gp_symbolic） | `E3004` "能力 search 无实现 'random_expression'" |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| S-1 | **`fit` / `generate` 各有一段完全重复的 `params.algorithms` 拒绝块**：`fit` 在 `:99-106` 与 `:107-119` 写了两遍，`generate` 在 `:159-171` 与 `:172-184` 写了两遍；第二段永不可达（第一段已抛 `E1004`）。 | P2 | `search.py:99-119,159-184`；实测两处均返回 E1004 | **已确认**：复制粘贴冗余，删第二段即可；不影响行为，不改计数/去重 |
| S-2 | **"新算法只动一处"表述不成立**：实际至少动 3 处——`mining.describe_*`（新算法 spec）、`search._algorithms()`（`:36-43`）、`search._RUNNERS`（`:329,376`）；`impls.py:55` 仅注册 `gp_symbolic`，若计划把 `impl` 写成算法名则实测 `E3004`。多算法靠 `params.algorithms` 绕过（impl 仍 gp_symbolic），所以"一处"只在"加进列表"这个狭义层面成立。 | P2 | `search.py:36-43,329,376`；`impls.py:52-55`；`runtime.py:269-324`；实测 `E3004` | **已确认（需确认意图）**：文档 `20-mining-algorithms.md:100-109` 已列 5 步含注册，但"只动一处/其余零改动"的措辞会误导；建议改为"改 2 文件 3 处 + 可选 impls 注册" |
| S-3 | **`unobservable` 在能力层恒为 `[]`**（`:496`），逐候选原因由编排层回填后写入 Search 产物。 | P2（说明缺口） | `search.py:492-496`；`capability_bridge.py:1270-1288` | **已确认符合设计**：留痕通道真实存在（编排层 `:1287-1288` 落盘）；只需在文档写明"能力层空、编排层补"，避免被误读为"没实现" |
| S-4 | **fitness 只算 |RankIC|，不含 p/成本/判据；但 `ic_series` 默认 `spearman`**（`search.py:314`），`AlgorithmSpec.fitness_objectives=["abs_rank_ic"]` 中的 "rank" 与之自洽。 | — | `search.py:314,318-319`；`mining.py:579` | 无需改动（`pearson` 会破坏 objective 命名一致性） |

**多了什么**：无冗余算法。两个算法各自有明确分工（GP 进化 vs 同空间均匀基线），符合 `20-mining-algorithms.md` §2.2。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| deap（GP：选择/交叉/变异框架 + 自己解码 AST） | **采纳** | `mining.py:384-463`；全局 random 快照/还原（`:405-406,496`）满足 AC39；`08-reuse-audit.md` 选型审计已定 |
| 随机表达式作常驻基线（同空间均匀采样） | **采纳** | 实测两算法都产出（`generators` 双值）；共享一次 `seen`/`calls`，零额外预算；GP 若赢不过随机才证明进化有效 |
| 跨算法共享 fitness/seen、内容身份去重 | **采纳** | `search.py:402,457-472`；实测 `n_trials=26 > count=22`（真实评估被记账），不是只登记最终种群 |
| 模板枚举（Alpha158 族） | **观望** | qlib Alpha158 158 个量价因子在 qweave 内核可用，但 `factors.py` 只暴露 12 个先验名（见 10_factors.md）；作为未来无状态算法候选 |
| gplearn | **拒绝** | 私有 AST 字段、无注册表对接、无法内容寻址；`20-mining-algorithms.md:83` |
| NN/RL/LLM 直接进 Search | **拒绝/延后** | 产出非表达式或不可复现；AC39 决定论与内容身份不成立；仅允许以先验形式进入（`:84-86`） |
| 多目标 NSGA-II（收益 vs 换手） | **观望** | 等 Verdict 积累出真实收益-换手前沿；当前单目标 + 成本闸够用，摊薄预算 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：

1. **四方法齐全且边界正确**：`describe()` 纯静态（`:46-48`）；`fit` 落盘可序列化 `final_genes`（`:142-149`）；`generate` 复用 fit 基因、`generations=0`/`fitness=None`（`:202-230`）；`lineage` 非映射即 `E3003`（`:233-242`）。实测全部通过。
2. **fit/generate 拒绝多算法参数**：`:99-119`、`:159-184`（虽然重复，但行为正确）——避免"计划写了两算法却静默只跑一个"的假成功。实测 `E1004`。
3. **跨算法共享一次计数**：`:402` 只建一个 `fitness/seen/calls`；`:471-472` `trial_ids=set(seen)∪候选`。实测 `n_trials=26`, `count=22`；两算法各自建闭包会导致同一表达式算两遍、DSR 门槛被记账错误放宽——已闭合。
4. **按算法名分派而非 stateful 布尔**：`:324-328` 注释 + `:376` `_RUNNERS.update(...)`；实测 `_RUNNERS` 与 `_algorithms()` 集合相等；注册了 runner 缺失的实现会 `E3004`（`:413-420`）。
5. **fitness 求值口径不可替换**：`:274-278` 用 `qweave` **包版本**作为 `impl_version`（不是白名单摘要），白名单/NaN 策略/node_cache 写死；`test_v5_search.py:235-271` 用打点确认 evaluate 子调用全部 `ok`、版本一致。若传错版本 → 全 `E1002` → 适应度恒 -1 → `build` 在 `:448-455` 显式 `E3003`，不让假候选流出。
6. **内容身份去重**：`candidate_id` 不含 generator/seed（`mining.py:134-141`），`dedup_candidates` 合并谱系（`mining.py:197-216`）；实测通过。
7. **`unobservable` 留痕通道**：能力层空（`:496`），编排层逐候选回填（`capability_bridge.py:460-503,1287-1288`）。
8. **`available_from` 透传**：`:135,188,404,527-532`；实测注入 as-of 后每个候选都带上，且不进 `candidate_id`。
9. **并发自适应检测**：`:421-435` 对 `needs_fitness` 算法加 `_FIT_LOCK`；实测抢占后返回 `E2009`。
10. **先验池只消费不内联**：`:503-540` 只读 `market_view.prior_candidates`，名字→AST 解析在 `app/cli/live.py`（经 factors.py），口径单一来源。

**发现的问题**：

- 见 D2 S-1（重复死代码块，`search.py:99-119,159-184`）、S-2（"只动一处"措辞与 3 处实现不符）、S-3（`unobservable` 说明缺口）。**无 P0/P1 级代码 bug**；测试覆盖充分（`test_v5_search.py` 24 个用例 + `test_v5_prior_pool_multialgo.py` 多算法/先验池用例，含 AC20/22/37–39）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `02-mining-interface.md:214` | "试验计数只认 `CandidateSet.count`，不认配置里声明的 population/max_candidates" | **需改**：与本篇 §2.2（`:87-93`，"fit 内评估也登记"）自相矛盾，且与实现 `n_trials=\|seen∪候选\|`（实测 26 vs 22）不符。改为"count=最终去重候选数；n_trials=fit 内评估去重集合∪最终候选，两者都实测、都不采信配置声明" |
| `20-mining-algorithms.md:100-109` | "新算法只动一处（算法注册），其余七步零改动" | **需改**：实测至少 2 文件 3 处（`mining.describe_*` / `search._algorithms` / `search._RUNNERS`），计划若把 `impl` 写成算法名还需 `impls.py` 注册（E3004）。把"一处"改成"算法表 + 分派表两处，impls 按需" |
| `02-mining-interface.md:243-245` | §7.1 多算法"合并后仍只有一次计数，count 等于去重后实测数" | 无需改（与实现一致）；补一句"`unobservable` 由编排层回填，能力层返回空"以消解 S-3 |
| `02-mining-interface.md:48,241` | `call_fit.inputs` 不含评估区间引用、C17 运行期 E1016 | 实现侧门面 `fit` 不接收 `call`，这一约束由编排层/编译器承担；文档无需改，但审查应在 U4/U5 单元复核该守卫真实存在 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 加一个**无状态**算法（模板枚举等） | ⚠️ 半配置化 | 需 `describe_*` + `_algorithms()` + `_RUNNERS` 三处；`build` 的共享计数/去重自动生效，无需碰判据/账本 |
| 加一个 **stateful 有 fitness** 算法 | ⚠️ 半配置化 + 并发守卫 | 同上三处 + `_FIT_LOCK` 自动适用；实测 `E2009` 守卫有效 |
| 新算法用 `impl=<算法名>` | ❌ 需改代码 | `impls.py:55` 只注册 gp_symbolic；实测 `E3004`。多算法目前靠 `params.algorithms` + `impl=gp_symbolic` 绕过 |
| RL / NN / LLM 进 Search | ❌ 当前不支持 | `20-mining-algorithms.md:85-86` 列为延后/拒绝；解锁条件（截面≥100、GP pass 饱和）当前 20 标的未满足；属全局决策非本模块 |
| 先验池新增因子名 | ⚠️ 半配置化 | 内置名要改 `factors.py:BUILTIN_FACTORS`（代码）；`db:<name>` / `db:*` 从因子库回灌可零代码（见 10_factors.md） |
| 换 fitness 目标（如加换手/复杂度） | ❌ 需改代码 | `fitness_objectives=("abs_rank_ic",)` 写死（`mining.py:579`）；且改目标即新试验族，必须改文档先行 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 不适用/无污染——fitness 只返回 |RankIC|，不年化；`step` 由面板给定，无 252 依赖（`search.py:314,318-319`）
- **C2 资金费按事件**：✅ 搜索不计算字段值，候选只带 AST；`funding_carry` 等由 `factors.py` 定义、字段门禁（`_FUTURES_ONLY_FIELDS`）在上游
- **C3 spot/futures**：⚠️ 搜索层无 market 维度，靠 `panel.fields` / `available_fields` 门禁（见 10_factors.md F4）；能力层无第二道 market 断言
- **C4 合约四元组**：❌ 无（全局缺口）；候选身份是表达式内容身份，不是资产身份
- **C6 上市/下架 as-of**：✅ `available_from` 经 build/fit/generate 透传到候选（`:135,188,404,527-532`），不取墙钟
- **C8 列集开放**：✅ `fields` 来自面板/参数（`_panel_fields`，`:543-544`），注册表 9 字段开放；C15 编译期比对供给版本
- **C10 判据不得放松**：✅ 强支撑——fitness 无 p/成本/判据（`:318-319`），无 `n_trials` 旋钮，共享计数防止 DSR 门槛被记账错误放宽
- **C11 流动性分层**：⚠️ 搜索不改掩码，依赖上游 `observable/tradable_mask`；本模块不引入小币计数偏差

## R1–R7 影响

- **R1 真数据跑通**：✅ 中性偏正（多算法 build 已在 40×6 面板实测跑通；真跑用同一路径）
- **R2 结论可信**：✅ 强支撑（不产生结论、计数诚实、求值口径锁定；`E3003` 守 synthetically 全失败）
- **R3 因子零代码扩展**：⚠️ 部分支撑（`db:` 回灌零代码；新算法/新内置因子名需改代码，见 S-2）
- **R4 搜索有效**：✅ 强支撑（GP + 随机基线同预算对照，GP 已自主发现 basis carry，见 `20-mining-algorithms.md:159`）
- **R5 单因子可归因**：✅ 候选带 `generators`/`parents`/`operators` 谱系，`lineage` 可回溯
- **R7 fail-closed**：✅ 强支撑（未知算法/无 runner/并发/求值全败/零候选/多算法喂门面全拒，实测 6 条错误码）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 实测确认四方法契约、生成不评估、fit 仅 mine_only（门面/求值闭包结构上取不到评估窗口）、跨算法共享一次 `seen` 计数、内容身份去重、按算法名分派、`unobservable` 留痕通道均成立。三处 P2：S-1 重复死代码（删即可）、S-2 文档"只动一处"措辞与实现 3 处不符（改文档）、S-3 `unobservable` 说明缺口（编排层已回填，补文档即可）。`02-mining-interface.md:214` 的计数口径自相矛盾是本轮发现的最值得修的文档问题（实现更诚实）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 9 个模块 `mining.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
