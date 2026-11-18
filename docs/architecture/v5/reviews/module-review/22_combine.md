# 模块审查 22 · `v5/capability/ops/combine.py`

> 审查日期：2026-11-18
> 依据：源码 459 行全文通读；依赖 `compute_digest`/`method_id`、`numpy.linalg.eigh`；设计 `s0/04-capability.md` §3.8（L461–489）、`06-evaluation.md`（L283–334）、`05-contracts.md` I23、`07-components.md` §3；测试 `test_v5_s4_combine.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Combine 是组合域合成器（S7 完整版），回答："把通过裁决的候选合成为一条组合分数，权重只在 fit 段拟合一次，eval 段保持不变。"四件硬事：
1. **三合成器（closed_multi，实现名=`portfolio.combiner`）**：equal_weight（截面标准化后逐候选一票）、score_weighted（权重 ∝ fit 段带符号 IC）、fixed_ridge（fit 段岭回归闭式解）。换实现名即换指纹。
2. **去相关与正交化只在拟合期**：corr_max<1 按 |fit IC| 降序贪心保留、超限剔除并记 `dropped_members`（不静默）；orthogonalise=symmetric 对 fit 段相关阵做 Löwdin `A^{-1/2}`，eval 段沿用同一变换。
3. **窗口隔离（X6）**：`fit.end + embargo ≤ eval.start`；旧实现只读 embargo 不用（当装饰），现必须真正隔离（`E1021`）。
4. **暴露一份真相**：逐候选暴露读 candidate_set.exposures（来自 evaluate 的 values_ref），不重算表达式、不从标签派生分数。

成员准入设计文字（`s0/04:451`）：redundant 候选 watch "不进组合"。净表现由 allocate+backtest 在同 caliber 下算，`evidence_grade=in_sample` 由编排层写。

**加密特化**：相关阵用 fit 段池化相关（缺失格自动跳过，不插补）；标准化在标的维（截面）而非候选维；embargo 长度 ≥ horizon（重叠标签隔离，06 §7）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` 调用 `run(call)` **实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_rest.py`（系统临时目录）。

### 场景
3 标的 × 12 根 1d bar；c1/c2 两条随机暴露；decisions 分别设为 pass/watch/pass；窗口 `fit=01-01..01-06, embargo=1, eval=01-08..01-12`。

### 输入
```
capability="combine" impl="equal_weight|score_weighted|fixed_ridge"
params={"domain":"cs","orthogonalise":"none|symmetric","corr_max":1.0|0.5,
        "window":{"fit":"...","embargo":1,"eval":"..."}}
inputs=(candidate_set, verdict_set, panel, label{score,valid_mask})
```

### 必查要点逐项实测输出

**三合成器**——真实打印 `weight_share`：
```
equal_weight    weights = [0.5, 0.5]
score_weighted  weights = [0.4978, -0.5022]   ← 带符号，负 IC 成员反向
fixed_ridge     weights = [0.4928, -0.5072]   ← 闭式岭回归
```
代码 `:392-399` 分派；`legacy` 路径（equal+none+corr=1）逐位保留旧产物（`:364-380`）。

**corr_max 贪心去相关 + dropped_members**——两条几乎相同暴露、corr_max=0.5：
```
dropped_members = [{'candidate_id':'c2','reason':'corr_above_max','max_abs_corr':1.0}]
kept = ['c1']
```
代码 `_prune`（`:224-247`）按 |fit IC| 降序、`worst > corr_max + EPS` 即剔除并记原因；排序键含 candidate_id 防上游顺序漂移。

**orthogonalise=symmetric（Löwdin）**——真实输出 `weights=[0.5,0.5], finite=True`；实现 `_sym_inv_sqrt`（`:279-287`）用 `eigh` + 特征值下限 `_EIG_FLOOR=1e-10`，共线成员被压平而非抛 LinAlgError（`:401-406`）。

**embargo X6 检查 E1021**——fit.end=01-07、embargo=3、eval.start=01-08（07+3>08）→ 真实返回 `E1021`（`:83-91`）。

**watch 候选拿满权重（P0-2 的另一半）**——decisions={c1:pass, c2:watch}，equal_weight：
```
pass+watch 权重: [('c1',0.5,'pass'), ('c2',0.5,'watch')]   ← watch 与 pass 同权
```
代码 `_DECISIONS=frozenset({'pass','watch'})`（`:42`）、`_selected` 按此入 book（`:141`）、`weight_share=weights[idx]`（`:423`）不区分 decision。**这与 verdict 写出的 `tested:false` 无人读取共同构成完整 P0-2**：未检验/仅 watch 的候选进组合拿满权重，再进 OOS 认证。

**skfolio 风险最优是否接线**——全仓 grep `skfolio` 在 `v5/**/*.py` **零命中**；`impls.py` 只注册三个合成器（`:54-60`）。风险最优（HRP/CVaR/最小方差）**未接线**，属 plan §3 决策项②（只审查不改）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| C-1 | **watch 候选拿满权重（P0-2 另一半）**：combine 把 pass/watch 同等对待，watch 拿与 pass 相同的 weight_share，进 OOS。与 `s0/04:451`"watch 不进组合"文字冲突；与 verdict 的 `tested:false` 无下游读取叠加成完整 P0-2。 | **P0（跨模块决策项①）** | `combine.py:42,141,423`；实测 pass+watch 各 0.5；见 `20_verdict.md` V-1 | **已确认事实**。修法：①combine 按 decision/tested 降权或排除 watch（改代码+契约）；②维持现状、改文档承认 watch 可入组合。属用户明确点名的三项决策之一，本轮只审查。 |
| C-2 | **skfolio 风险最优未接线**：`_SCHEMES` 只有三个（`:44`），impls 只注册三个；全仓无 skfolio。D3/D6 的"风险最优"是规划不是现状。 | P1（扩展缺口，决策项②） | `combine.py:44`；`impls.py:54-60`；全仓 grep 零命中 | **已确认**：本轮"真挖因子"三合成器已够；接 skfolio 属 P1 决策项，用户已定为"只审查不改"。 |
| C-3 | **`available_from` 约束未实现**：s0/04 §3.8 要求 `fit.start ≥ max(available_from)`，但编排层把 available_from 置成供给覆盖末端（as_of），该条件任何真实计划都不成立。取舍是**不实现这条检查**（模块 docstring `:23-27` 明说），而非实现一条会判死所有计划的检查。 | P1（契约不一致，需编排层改） | `combine.py:23-27,149` | **需确认意图**：取舍合理但属已知契约违背；正确修法是让 available_from 回到"候选首次可计算日"语义（编排层改），非本模块单独可补。 |
| C-4 | embargo 单位是**日历日**（`fit_end + timedelta(days=embargo)`，`:86`），而 certify 的 embargo 是**bar 数**（`certify.py:282`）。同一参数在不同工位单位不同。 | P2（C12 单位一致性说明） | `combine.py:86` vs `certify.py:281-282` | **已确认符合各自设计**：combine 窗口是日期区间（fit/eval 都是 date），embargo 用天自洽；certify 按 bar 切折（C12）。但文档/参数名应强调单位，避免调用方混用。 |

**多了什么**：legacy 路径（equal+none+corr=1）保证历史产物与指纹逐位不变（`:363-380`），是必要的向后兼容，不算冗余。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 等权 / IC 加权 / Ridge（scikit-learn / 闭式解） | **采纳（三实现）** | `combine.py:392-399`；`07-components.md:39` 指定 sklearn + scipy.linalg |
| scipy.linalg.eigh 对称正交（Löwdin） | **采纳** | `:279-287`；特征值下限使共线稳定 |
| skfolio HRP / CVaR / 最小方差 | **未接线（P1 决策项②）** | 全仓零命中；`07-components.md:39` 规划为 combine/certify 的风险最优 |
| 逐时点重拟合 / 看 eval 表现调权重 | **拒绝** | 权重只在 fit 拟合一次（`:5-7`），否则前视 |
| PyPortfolioOpt | **拒绝** | scipy 1.18 崩溃历史（plan U9 D3） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **三合成器**：`_SCHEMES`（`:44`）+ impl 分派（`:315-320`），实测三者权重不同且合理。
2. **corr_max 贪心 + dropped_members**：`_prune`（`:224-247`），实测高相关成员被剔除并记录 reason。
3. **Löwdin 正交化**：`_sym_inv_sqrt`（`:279-287`），`eval` 沿用 fit 变换（`:401-406`）。
4. **embargo X6 E1021**：`:83-91` 实测越界报错。
5. **fit 段唯一拟合点**：`_fit_ic`/`_corr_matrix`/`_ridge` 都只读 `fit_mask`（`:183-276`），eval 不参与拟合。
6. **标准化在标的维**：`_cs_zscore`（`:163-179`）按 `valid.sum(axis=1)` 截面归一，常数行记 0。
7. **成员顺序内容决定**：`picked.sort(key=candidate_id)`（`:159`），防上游序列化顺序漂移导致权重顺序变（AC39）。
8. **分母用可用成员 |w| 之和**：`_combine_score`（`:290-310`），缺数据成员不"默认 0 暴露"。
9. **fit_ic=None 区分"没拟合"与"拟合得 0"**：legacy 路径（`:377-380,424`）。
10. **windows_used 键名是下游契约**：certify 读 `windows_used.eval`，fit/embargo/eval 原样保留（`:435-442`）。

**发现的问题**：C-1（P0，`tested:false`+watch 满权重，跨模块决策项①）、C-2（P1 skfolio 未接，决策项②）、C-3（P1 available_from 契约不一致，需编排层改）、C-4（P2 单位说明）。**除决策项外无本模块内新代码 bug**。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:451` | "redundant 给 watch，不进组合" | 与 C-1 实测"watch 拿满权重进组合"冲突；按 P0-2 裁决结果二选一更新 |
| `s0/04-capability.md` §3.8 / `05-contracts.md` I23 / `AC25` | 要求 `fit.start ≥ max(available_from)` | C-3：实现未做该检查且编排层 available_from 语义错；按裁决结果更新文档或修编排层 |
| `06-evaluation.md:331`（dedup vs corr_max） | 两者独立、缺省关闭 | 与实现一致，无需更新 |
| `07-components.md:39`（skfolio 规划） | 风险最优列为规划 | C-2：明确标注"当前未接线，三合成器在位" |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 三合成器切换 | ✅ 零代码（换 impl） | 换 impl 名即换指纹；`closed_multi` 封闭多选 |
| 新增第四合成器（如风险最优） | ❌ 需改代码+注册 | 加 `_SCHEMES` + build 分支 + impls 注册；skfolio 未接（C-2） |
| 合约间高相关去相关（corr_max） | ✅ 已支持 | 池化相关贪心剪枝 + dropped_members（实测） |
| 新增正交化方法 | ❌ 需改代码 | `orthogonalise` 只允许 none/symmetric（`:321-324`） |
| 按 available_from 过滤成员 | ⚠️ 契约不一致 | C-3：需编排层先修正 available_from 语义 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（合成器不算年化）
- **C10 判据不可放松**：✅ corr_max/orthogonalise 是显式口径参数，落 windows_used 可查
- **C11 流动性分层**：不适用（成员已由 verdict 筛）
- **C12 embargo 以观测计**：⚠️ combine 的 embargo 是日历日（fit/eval 是 date 区间），certify 才是 bar；单位不同但各自自洽（C-4），需文档强调
- **C4 合约维度**：❌ 相关阵按暴露算，无合约四元组分组（全局 P0-5，本模块非落点）

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（三合成器真跑可用）
- **R2 结论可信**：⚠️ 负向——C-1 使未检验 watch 拿满权重进 OOS，削弱结论可信（P0-2，待裁决）
- **R5 单因子可归因**：✅ member_weights_ref + members + dropped_members 落盘，组合分可拆到成员
- **R7 fail-closed**：✅ embargo 越界/窗口无交集/eval 越界/权重全零全拒

---

## 结论

**本模块三合成器、corr_max 贪心去相关、Löwdin 正交化、embargo X6 检查均经真实调用确认，无新代码 bug。** 三项待裁决：①**P0-2 另一半——watch 候选拿满权重**（与 verdict 的 `tested:false` 无读取叠加，用户点名决策项①）；②skfolio 风险最优未接线（P1 决策项②）；③available_from 契约不一致（P1，需编排层改）。均属"只审查不改"范围。

**请确认本模块审查结果**：
- ✅ 确认通过（三项决策汇总处理） → 我进入第 6 个模块 `backtest.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想当场裁决 P0-2（watch 满权重）→ 告诉我
