# 模块审查 09 · `v5/capability/mining.py`

> 审查日期：2026-11-18
> 依据：源码 592 行全文通读；依赖 `registry.py` + `registry/fields.json`(9 字段) + `registry/operators.json`(19 算子)、`ops/search.py`（fitness 闭包/分派）、`contracts/errors.py`（E2007/E2009）；设计 `02-mining-interface.md` §2/§5/§6/§13、`20-mining-algorithms.md` §2；测试 `test_v5_search.py`（身份/G6/AC39/max_depth）、`test_v5_prior_pool_multialgo.py`（随机基线）。
> 判据：`v5_module_review_plan_20261118.md` §0.0/§0.1（六维 + C1–C12 + R1–R7）。
> 实测方式：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_mining.py` + 一条别名身份对拍（系统临时目录，未改仓库）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Mining 是"算法扩展点内核"：定义**统一候选 + 身份 + GP/随机两个内置生成器**，不改其余七工位。它应保证：

1. **统一 Candidate 与内容身份**：`candidate_id = H(规范化 payload + required_fields)`，不含 generator/seed/参数快照；AST 只接受 `{field}/{const}/{op,args}` 三种节点，键集合闭合、带参算子末位必须是整数 const、深度 ≤64——否则"语义相同写法不同"会被算成两次试验，计数虚高、DSR 门槛失真。
2. **deap GP 只在 `fit` 内有反馈**：整数基因串 + 自己确定性解码（不依赖 deap 不稳定的 `PrimitiveTree` 内部字段）；进化只改基因，候选产出仍走统一结构。
3. **复现与隔离（AC39/E2007）**：deap 依赖**全局 `random` 模块**（设施"禁止模块级取随机源"的唯一豁免），进入时 `random.setstate(局部 seed)`、退出时无条件还原；同 seed 逐位一致，且调用方全局随机状态不被污染。
4. **随机基线同空间**：`random_expression` 与 GP 共用同一 `_OpCodes` 与 `_random_individual`，是同一表达式空间的均匀采样，不是另一套空间；无 fitness、按 `candidate_id` 拒绝采样去重。
5. **fail-closed**：搜索空间为空、解码不可归约/超深、参数越界（population≥2 / generations≥0 / max_depth≥1）都显式失败或个体记 -1，不静默产出假候选。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 复现：`venv/Scripts/python.exe C:/Users/lansa/AppData/Local/Temp/fm_review_mining.py`。

### 输入：`GPConfig` + 注册表 + fitness 闭包

```
reg = default_registry()          # fields=9, operators=19（实测）
fields = ("close","volume","high","low","vwap")
cfg = GPConfig(fields=fields, population=8, generations=2,
               max_depth=3, max_candidates=8, genome_len=7)
fitness = lambda c: 0.5           # 编排层注入；算法不自建求值器
seed = 42
```

### 输出 1：AST 规范化与硬约束（实测）

```
normalise_ast(mul(close, 2.0)) == normalise_ast(mul(close, 2)) = True
  -> {"op": "mul", "args": [{"field": "close"}, {"const": 2}]}
mean(close, {"const":2.0})     -> ValueError "带参算子 'mean' 的末位参数必须是整数 const"
mean(close, {"field":"volume"})-> ValueError（同上）
mean(close, {"const":True})    -> ValueError（同上）
{"const": True}                -> ValueError "bool 不是常数"
{"op":"add","args":[],"comment":"x"} -> ValueError "非法 AST 节点键: ['args','comment','op']"
```

### 输出 2：`_OpCodes` 与注册表 arity 一致性（实测逐算子）

```
n_terminal=8  n_ops=36  max_gene=43
op         reg.arity  opcode.arity  decode.args  window  一致
rank/scale/abs/log/sign   1            1            1       None   True
add/sub/mul/div           2            2            2       None   True
ref/delta/mean/std/max/min/sum (×窗口 2/6/10)
                          2            1            2       2/6/10 True
corr/cov (×窗口 2/6/10)   3            2            3       2/6/10 True
全部一致 = True
```

关键点：注册表的 `arity` **含**末位窗口参数（如 corr=3）；`_OpCodes` 存 `arity-1`（corr=2），decode 时再追加 `{"const": window}` → 生成 AST 的 args 数=3，与注册表一致（`mining.py:259-274,303-305`）。G6 使带参算子从 `param_domain` 派生（`corr/cov` 不再漏生成，实测都在 36 个 op 里）。

### 输出 3：deap 全局随机快照/还原 + 复现（实测）

```
random.getstate() 进出不变(第1次)  = True
两次同 seed candidate_id 序列相同  = True
两次 fitted_state 相同             = True
第1次 repaired=1  dropped_decode=0 # 交叉/变异失败回退父本，不丢个体
先 random.seed(999) 后进出仍不变   = True
max_depth=2 -> 实测最大深度 2, dropped=0
max_depth=4 -> 实测最大深度 4, dropped=0
```

### 输出 4：`random_expression` 基线（实测）

```
同 seed 逐位一致 = True
attempts=12  target=12  实际候选=12
不触碰全局 random = True
generator="random_expression@1"  needs_fitness=False
```

### 输出 5：算法表 vs 分派表（实测）

```
_algorithms() = ['gp_symbolic', 'random_expression']
_RUNNERS      = ['gp_symbolic', 'random_expression']   集合相等=True
describe_gp.fit_window="mine_only"  needs_fitness=True
```

### 失败输入 → 错误输出（实测/源码）

| 非法输入 | 实测输出 |
|---|---|
| 搜索空间为空（`codes.max_gene<1`） | `ValueError "搜索空间为空"`（`generate_gp`/`generate_random` `:388-389,522-523`） |
| 解码不可归约 / 深度超 `max_depth` | `ValueError`，GP 内个体记 -1、generate 阶段丢弃（`:294-299,473-475`） |
| `population<2 / generations<0 / max_depth<1` | `E1004`（由 `search._config:354-358` 调用侧拦截） |
| AST 参数位浮点 / bool / 额外键 / 深度>64 | `ValueError`（`normalise_ast:96-126`），evaluate 层转 `E1003` |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| M-1 | **语义等价的算子别名未归一内容身份**：`ref` 与 `delay` 在注册表里 `impl_symbol` 都是 `"delay"`（operators.json:6-30），但 `normalise_ast` 保留 op 字面名，`candidate_id` 不同。实测 `ref(close,5)` 与 `delay(close,5)` 的 id 分别为 `sha256:edd5cf2…` / `sha256:aa62629…`。`_OpCodes` 会同时生成两者（`_PARAM_ORDER` 含两个名字），因此搜索空间里存在成对的语义重复候选，`n_trials` 被小幅抬高。 | P2 | `mining.py:73-75,89-126,262-264`；`operators.json:6-30`；实测两个不同 id | **已确认**：违反 `20-mining-algorithms.md:39` "结构不同、语义相同的表达式不重复计数"的措辞。量级小（26 次里仅个别别名对），不左右 DSR 结论，但属计数诚实性缺口。修法：`normalise_ast` 按 `impl_symbol` 归一 op 名（需与 evaluate 对齐，先改文档） |
| M-2 | **`_PARAM_OPS` 手写名单与 G6 注册表派生不同步的隐患**：`normalise_ast` 用硬编码的 10 个带参算子名判断"末位必须 int const"（`:73-75`），而 `_OpCodes` 从 registry `param_domain` 派生（`:262-274`）。当前两者恰好一致（10 个），但若注册表新增带参算子，生成侧会自动覆盖、校验侧不会，新算子的 AST 参数位可写入浮点而 id 漂移。 | P2（扩展性） | `mining.py:73-75` vs `:262-274`；实测当前 registry 10 个 param_domain 算子与名单一致 | **需确认意图**：G6 只修了生成侧，校验侧仍手写。建议把 `_PARAM_OPS` 也改为从 `reg.operators[*].param_domain` 派生，保持"注册表是唯一真相" |
| M-3 | **E2007 无运行期守卫**：deap 依赖全局 `random` 是契约唯一豁免（`facilities/__init__.py:4`），本模块靠 `:405-406,496` 的快照/还原自律；实测进出状态确实不变。但没有任何运行期断言"退出时状态必须等于进入时"——若将来有人误删 `finally`，测试 AC39 能抓，代码本身不会自报。 | P2（说明） | `mining.py:405-406,495-496`；`facilities/__init__.py:4`；`contracts/errors.py:106`；实测状态不变 | **已确认符合设计**：测试 `test_search_reproducible_and_global_rng_untouched:146-156` 已钉住；属防线在测试而非代码的说明缺口 |

**多了什么**：无冗余实现。`generate_gp` / `generate_random` 分工清晰，共用 `_OpCodes`/`_random_individual`，没有第二套解码逻辑。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| deap 做进化框架、自己解码整数基因 | **采纳** | `mining.py:219-223` 注释：deap 1.4 的 `PrimitiveTree` 内部字段不稳；自解码保证同基因序列恒得同 AST（AC39）；实测同 seed 逐位一致 |
| `_OpCodes` 从 registry `param_domain` 派生（G6） | **采纳** | 实测 36 个 op 覆盖 `corr/cov`（旧手写 7 个名字时它们永不产出）；arity 用 `spec["arity"]-1` 避免多args（`:264-274`） |
| 栈式后序基因 + 递归生成保证可归约 | **采纳** | `_random_individual:335-359`；交叉/变异失败回退父本（`:444-460`），实测 `repaired=1` 而 `dropped_decode=0`，不凭空少试验 |
| Newey-West / 显著性判定进算法 | **拒绝** | 三禁令：算法不 import verdict，fitness 只返回标量，无 p 值/成本/判据（`mining.py:7-13`） |
| gplearn AST | **拒绝** | 私有字段、无法内容寻址；`20-mining-algorithms.md:83` |
| 神经网络/RL 直接产出权重 | **拒绝** | 权重不可内容寻址、GPU 归约非确定破坏 AC39；`:84-86` |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：

1. **AST 三节点闭合 + 三条硬约束**：键集合精确匹配、带参算子末位整数 const、深度 ≤64（`:96-126`）；实测 2.0 归一为 2、参数位浮点/bool/field/额外键全拒；`test_v5_search.py:83-94,274-294` 覆盖。
2. **内容身份不含谱系**：`candidate_id` 只含 payload + required_fields（`:134-141`）；实测不同 generator/seed 的同表达式 id 相同；`dedup_candidates` 只按 id 去重、合并 generators（`:197-216`）。
3. **`_OpCodes` 与 registry arity 一致**：实测 36 个 op 逐一 decode 后 args 数 == 注册表 arity（corr/cov=3，一/二元与带参都对齐）；G6 从 `param_domain` 派生，覆盖 `corr/cov`。
4. **`max_depth` 真参与判定**：decode 检查（`:298-299`）和 `_random_individual` 补齐时的深度守卫（`:353-359`）双管；实测 max_depth=2/4 分别产出最大深度 2/4，`dropped_decode=0`；旧实现用常量 16 兜底会让配置形同虚设。
5. **deap 全局 random 快照/还原（AC39/E2007 豁免）**：进入 `rng=Random(seed)` + `random.setstate(rng.getstate())`，`finally` 无条件 `random.setstate(saved_state)`（`:404-407,495-496`）；实测三种前置随机状态下进出都不变，同 seed 两次候选/fitted_state 逐位一致。
6. **交叉/变异失败修复而非丢弃**：备份父本/变异前基因，不可归约则回退并 `repaired+=1`（`:444-460`）；实测 `repaired=1, dropped_decode=0`，避免"每代凭空少 70% 试验"的历史缺陷。
7. **`generate` 不评估**：`initial_genes` 复用 fit 落盘基因、`generations=0`（`search.py:202-230`）；generate_gp 内 `if fitness is not None` 才进化（`:425`）；无 fitness 退化为纯随机种群但 `describe.deterministic` 仍成立（`:374-375`）。
8. **随机基线同空间 + 拒绝采样去重**：共用 `_OpCodes`/`_random_individual`，按 `candidate_id` 去重、最多 `count*20` 次尝试（`:499-554`）；实测 12/12 候选、同 seed 一致、不碰全局 random；空间不足时如实返回少于 target（不靠重复项充数）。
9. **配置边界 fail-closed**：`_config` 校验 population≥2/generations≥0/max_depth≥1（`search.py:354-358`）；搜索空间为空直接 ValueError（`:388-389,522-523`）。

**发现的问题**：

- 见 D2 M-1（别名 id 分歧，实测两个 sha256）、M-2（`_PARAM_OPS` 手写名单 vs G6 派生）、M-3（E2007 防线在测试）。**无 P0/P1 级代码 bug**；测试覆盖充分（AST/身份/G6/AC39/max_depth/fit-generate 衔接均有专门用例）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `20-mining-algorithms.md:39` | "去重：种群去重用内容身份，不用树结构相等——结构不同、语义相同的表达式不重复计数" | **需补 caveat**：`ref/delay` 这类同 `impl_symbol` 别名当前仍算两个候选（实测 id 不同）。若按 M-1 修，则必须先改文档声明"别名按 impl_symbol 归一"，再改 `normalise_ast` |
| `02-mining-interface.md:180-186` | "参数位置只有 `{"const": 2}` 合法；深度上限 64" | 与实现一致，无需改；补一句"带参算子名单由 registry `param_domain` 派生（G6）"以消解 M-2 的误读风险 |
| `07-components.md:21`（requirements）/ `facilities/__init__.py:4` | "禁止模块级取随机源（E2007）" | 需在跨层设施文档（`s0/02-crosscutting.md`）明确 deap 是唯一豁免，且豁免的边界是"只在 generate_gp 内 setstate/getstate、退出还原"；本模块注释已写，文档侧补一句即可 |
| `20-mining-algorithms.md:40` | "全局 random 禁用（E2007 契约层兜底）" | 措辞过强：实测并非"禁用"而是"受控使用 + 快照还原"。改为"deap 唯一豁免：进入设局部 seed、退出无条件还原，AC39 对拍" |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新算子（在 registry 加声明） | ⚠️ 半自动 | 生成侧 `_OpCodes` 从 `param_domain` 自动覆盖（G6，实测含 corr/cov）；但校验侧 `_PARAM_OPS` / `candidate_id` 别名归一需同步（M-1/M-2） |
| 新字段（在 registry 加声明） | ✅ 零代码生效 | `fields` 传入即进 `_TERMINAL`；实测当前 9 字段；上游面板供给是前置（U6） |
| 新带参算子 | ⚠️ 需两处 | 改 registry 后 `_OpCodes` 自动产出，但需把名字加进 `_PARAM_OPS` 否则末位参数校验失效（M-2） |
| 换 GP 为 NSGA-II 多目标 | ❌ 需改代码 | `fitness_objectives=("abs_rank_ic",)` 写死、`creator.V5FitnessMax weights=(1.0,)`；多目标改目标集合即新试验族 |
| 随机基线加温度（模拟退火） | ❌ 需改代码 | `generate_random` 无状态、拒绝采样；加温度需变 stateful + fit（文档 `20:75` 已列为可扩展方向） |
| 新算法接入 | ⚠️ 半配置化 | 需 `describe_*` + `search._algorithms` + `search._RUNNERS` 三处（见 08_search.md S-2）；本模块的 `AlgorithmSpec`/`make_candidate`/`generate_*` 是可复用骨架 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 不适用——本模块只编码表达式、返回适应度标量，无年化计算
- **C2 资金费按事件**：✅ 不计算字段值；带参算子的窗口是"根数"，资金费的按事件聚合在供给层（`10-crypto-fit.md:135-158` 的 `resample(bar).sum`）
- **C3 spot/futures**：⚠️ 本模块不判市场，字段由上游 registry/panel 门禁；候选可引用 funding_rate 但需上游提供
- **C4 合约四元组**：❌ 无（全局缺口）；基因空间里没有资产身份维度
- **C6 as-of**：✅ `make_candidate(available_from=...)` 由搜索透传（`:148,167`），不进内容身份
- **C8 列集开放**：✅ 终端来自 `fields` 参数 + `_TERMINAL_CONST`，算子来自 registry（实测 9/19），加字段/算子是配置侧事
- **C10 判据不可放松**：✅ 算法无 p 值/成本/判据出口（docstring `:7-13`）；计数在外部闭包；`M-1` 的别名重复是轻微计数不诚实（P2），不是放松阈值
- **C12 观测单位**：✅ `window_min/max`(2..10) 与 `genome_len` 都是 bar/观测数，不含交易日折算

## R1–R7 影响

- **R1 真数据跑通**：✅ 正（GP 已自主发现 basis carry，见 `20-mining-algorithms.md:159`；本模块实测生成器全部跑通）
- **R2 结论可信**：✅ 强支撑（内容身份 + 全局随机隔离 + 深度真约束 + 失败回退；M-1 别名重复量级小）
- **R3 因子零代码扩展**：✅ 支撑（新字段/算子走 registry；表达式空间组合出新因子不需改本模块）
- **R4 搜索有效**：✅ 强支撑（GP + 同空间随机基线；`repaired=1/dropped=0` 保证试验数不缩水）
- **R5 单因子可归因**：✅ `parents`/`operators`/`generator`/`seed`/`params_snapshot` 全谱系落在 Candidate（`:157-168`）
- **R7 fail-closed**：✅ 空间为空/解码失败/参数越界/畸形 AST 全拒；`E2007` 防线在测试（M-3）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 实测确认 `_OpCodes` 与 19 个注册算子的 arity 逐一对齐（含 corr/cov=3）、deap 全局随机进出状态不变且同 seed 逐位一致、max_depth 真约束、随机基线同空间且确定性、AST 三条硬约束全部生效。三处 P2：M-1 语义等价别名（`ref`/`delay`）id 分歧导致轻微计数虚高（需先改文档再决定是否归一）、M-2 校验侧 `_PARAM_OPS` 仍手写（G6 只修生成侧）、M-3 E2007 防线在测试而非代码。三者都不影响"能真挖因子"。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 10 个模块 `factors.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
