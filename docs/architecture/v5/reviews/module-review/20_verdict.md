# 模块审查 20 · `v5/capability/ops/verdict.py`

> 审查日期：2026-11-18
> 依据：源码 413 行全文通读；依赖 `inference.py`（bh_fdr/deflated_sharpe_ratio/empirical_p/expected_max_sr，Holm/BY 对拍原语）、`timeaxis.py`（CALENDAR_DAYS_PER_YEAR）；设计 `s0/04-capability.md` §3.7/§5、`05-contracts.md` I38、`06-evaluation.md`；测试 `test_v5_verdict.py`、`test_v5_dsr_units.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Verdict 是全系统**唯一**允许产出 `pass / reject / watch` 的裁判（名字空间 `fixed`，不得配第二实现）。四件事：
1. **四道判据**：DSR、BH 调整后 p、成本闸、零分布经验 p，逐候选收敛成三值 decision 与可枚举 reason_codes（`dsr_fail/fdr_fail/cost_fail/null_fail/insufficient_n/redundant`）。
2. **软原因降级 watch**：`insufficient_n`（没证据/没检验）与 `redundant`（检验过但不独立）是"这个候选到底行不行还没答案"，不是 reject——reject 是结论、会进族失败计数、让同一信号换名重跑永远过不了；且"重复+DSR 失败"必须判 reject 而非 watch。
3. **n_trials 不可篡改**：从 `runtime_facts.n_trials` 读，**不从 params 读**（写进参数=把"填小计数"变成新自由度）；回写 `n_trials_used` 可查。BH 族用历史+本轮全部 raw p，只校短名单起不了校正作用（V4 让 OOS 亏损因子混进来的原因之一）。
4. **Sigma 口径固定**：DSR 的 sigma_sr 默认单策略 SR 采样误差 `1/√(n_obs−1)`，是 ACCEPTANCE "n_trials=255→门槛年化 1.53"唯一自洽口径；横截面离散度只作诊断，不作门槛。

**加密特化**：高波动下判据不得被"加密波动大"放松（C10）——不悄悄调 dsr_min/fdr_max/清零 n_trials；365 年化只用于展示门槛（`:366-367`），判据用每期 SR。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` 调用 `run(call)` **实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_rest.py`（系统临时目录）。

### 场景
若干候选，metrics 含 `hac_t=6.0, p_value=0.001, cost_adj_return=0.01, sr=0.3, sr_n_obs=100, null_distribution=[0..2]×60`；`runtime_facts.n_trials=10`，`dsr_min=0, fdr_max=0.05, cost_adj_min=0`。

### 输入
```
capability="verdict" impl="sentinel"
params={"dsr_min":0,"fdr_max":0.05,"cost_adj_min":0,"family":"f1"}
inputs=(InputRef("observation_set", data={"per_candidate":[{candidate_id,metrics}...]}),)
runtime_facts={"n_trials":10, ...}
```

### 必查要点逐项实测输出

**`_SOFT_REASONS` 软降级**——候选 c2 带 `redundant_of="c1"`：
```
redundant 候选: [('c1','pass',[]), ('c2','watch',['redundant'])]
redundant+cost_fail: reject ['cost_fail','redundant']
```
即独立软原因→watch，软+硬失败→reject（代码 `verdict.py:41,149-160`：先剔软原因看剩余，`_decide` 的旧不可达分支已修）。

**DSR sigma 默认 sampling**——`sigma_mode='sampling'`、`sigma_sr_used=0.100504`（=`1/√(100−1)`）、`n_trials_used=10`。代码 `:236` `sigma_mode=str(params.get('sigma_mode') or 'sampling')`，`:344-345` 用 `1/√(n_obs−1)`。

**n_trials 不可改**——在 `params` 里塞 `n_trials=1` → 真实返回 `E1003`（`params.py:364` 校验器拒绝 schema 之外的字段）；合法调用 `n_trials_used=10` 来自 `runtime_facts`（`verdict.py:69-83`）。双重护栏：校验器挡 + build 不读 params。

**BH/Holm/HAC**——主判据用 BH：本批 p=0.04 + 历史族 9 个 1.0 → `p_adj=0.4`（raw 0.04 被放大 10×，A4 安全网生效）。Holm/BY 作为对拍原语留在 inference（`test_v5_verdict.py:45-57` 与 V4/scipy 逐位一致）；HAC t 来自 observe、此处只消费（`:327`）。

**`tested:false` 字段（P0-2）**——候选 `stage_a_pass=False` → 产物：
```
watch ['insufficient_n']  statistics.tested = False
```
代码 `:280-291` 写 `tested:false`、`:299` 写 `tested:true`。**但全仓 grep `tested` 仅命中 verdict.py 自身的 8 处（写/内部判断），无任何下游读取点**（`combine.py`/`certify.py`/`orchestration/` 均只读 `decision`，不读 `tested`）。这是已确认的 P0-2：watch 候选（含未检验者）进组合时不因 `tested:false` 被降权，反而拿满权重（见 `22_combine.md`）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| V-1 | **`tested:false` 字段全仓无下游读取（P0-2）**：verdict 精确写出"这个候选没被检验"，但 combine 只看 `decision ∈ {pass,watch}` 就给它和 pass 相同的权重，certify 也只看 decision。结果是"没检验的 watch 候选拿满组合权重进 OOS"，与 `s0/04:451`"watch 不进组合"的设计文字冲突。 | **P0（跨模块，需裁决）** | `verdict.py:287,299` 写；全仓 grep 仅 verdict 自身；`combine.py:42,141`（watch 入 book）；见 `22_combine.md` O/P0-2 | **已确认事实**。修法二选一：①combine 按 `tested` 给 watch 降权/排除（改 combine，契约增字段读取）；②维持现状但改文档承认 watch 可入组合。用户必查项已点名，属三项历史决策之一（plan §3 决策项①），本轮只审查不改。 |
| V-2 | `_SOFT_REASONS={"insufficient_n","redundant"}` 是硬编码 frozenset，新软原因需改代码，非注册表。 | P2（扩展性） | `verdict.py:41`；D6 | 判据集合冻结是"不得扩张"的设计（`:33-37` 注释），硬编码反而防误改；可接受。 |
| V-3 | `sigma_mode=cross_section` 是诊断性旧口径，仍可从 params 选（会改门槛）。 | P2（护栏确认） | `verdict.py:236-248`；`:227-235` 注释详述为何不再作默认 | **已确认符合设计**：默认 sampling 锁定；显式选 cross_section 是新试验族（口径变更），产物落 `sigma_mode` 可查。 |
| V-4 | DSR 在 `sr` 缺失时保守取 0（`:342`），不再回落 V4 的 `ir`/`n_obs`（V5 全仓无生产方）。 | P2（说明） | `verdict.py:336-342` | **已确认正确**：留一条永不命中分支只会让人以为有两套口径。 |

**多了什么**：无明显冗余。`_REQUIRED_METRICS`、`_REASON_ORDER` 都是可枚举集合的必要性守卫。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Deflated Sharpe Ratio（Bailey & López de Prado） | **采纳** | `verdict.py:357-362`；用 n_trials 惩罚多重检验，sigma 用采样误差 |
| BH-FDR（scipy `false_discovery_control`） | **采纳（主判据）** | `:115-135`；历史+本轮全族校正，A4 用 p=1 补齐未观测试验 |
| BY-FDR / Holm | **对拍原语（非判据）** | `inference.py`；`test_v5_verdict.py:45-57` 与 scipy/V4 逐位一致；`s0/05` 规定多统计变体只走只读诊断 |
| White Reality Check / SPA；PBO/CSC | 未采用（D6 扩展位） | 需保留截面/时序结构；PBO 若加应只加在 verdict 内部不新增工位 |
| 股票 alfalens 逐日 groupby + 固定 periods | **拒绝** | 加密高波动下不得调门槛（C10）；HAC/DSR 用每期口径 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **`_SOFT_REASONS` 软降级**：`:41,149-160`，实测 redundant→watch、redundant+cost_fail→reject。
2. **n_trials 来源唯一**：`:69-83` 只认 runtime_facts，正整数校验；params 塞值被 `E1003` 挡（实测）。
3. **sigma 默认 sampling**：`:236,344-345`；横截面离散度只落 `sigma_sr_cross` 诊断字段（`:370-371`），不进门槛。
4. **BH 族用累积 p + A4 安全网**：`:115-135`；`family_pvalues` 只接受有限值并 clip 到 [0,1]（`:86-112`）；实测 p_adj 0.04→0.4。
5. **p_value=0.0 不被吞**：`:250-264` 明确不用 `or 1.0`，只对 None/非有限保守当 1.0。
6. **stage_a 不进 BH 族**：`:255-270`，未检验候选 p=1.0 占位、不进族、adjusted=None。
7. **DSR 门槛落盘**：`dsr_benchmark_sr` / `dsr_benchmark_sr_annual` / `sigma_sr_used`（`:365-369`），换口径后门槛变化可查。
8. **skew/kurtosis 回落兼容**：`:347-356` 优先非 net 命名、回落旧 net_ 名，与 observe 同源。
9. **空集/重复候选拒**：`:184-220`（空 per_candidate E3003、重复 candidate_id E3003）。
10. **缺必需统计量→watch 不拒整批**：`:313-325`，一个坏候选不让其余失去裁决。

**发现的问题**：V-1（P0，`tested:false` 无下游读取，跨模块 combined with combine 的 watch 满权重）。这是用户点名的历史事故，属三项决策之一，本轮只审查。V-2/V-3/V-4 均为已确认设计。**除 V-1 外无本模块内 P0/P1 代码 bug**。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:449,451` | decision 三值 + reason_codes 可枚举 + redundant 给 watch | 文字写"watch 不进组合"，与 combine 实际"watch 拿满权重"冲突；按 V-1 裁决结果二选一更新（改代码则保持，不改则改文档承认） |
| `s0/04-capability.md` §3.7（n_trials 来源） | 与实现一致 | 无需更新 |
| `05-contracts.md:48`（I38 试验计数单位） | 与 n_trials 唯一来源一致 | 无需更新 |
| `06-evaluation.md`（DSR sigma 口径） | 采样误差口径已定 | 补"横截面离散度仅诊断、不进门槛"的明确句（固化 V-3） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增软原因（如 `unstable`） | ❌ 需改代码 | `_SOFT_REASONS`（`:41`）硬编码 frozenset；但冻结判据集合是防误改设计 |
| 新增 FDR 方法（BY/Holm 升主判据） | ❌ 需改代码且违反 `s0/05` | 多统计变体只走只读诊断；升主判据须改文档红线 |
| 新增 PBO/CSC 判据 | ⚠️ 半支持 | 可只加在 verdict 内部（不新增工位），但需新指标生产方（observe 侧） |
| sigma 口径升级 | ❌ 需改代码+新试验族 | `sigma_mode` 是显式口径字段，默认 sampling 锁定 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 门槛展示用 `√365`（`:366-367`），判据用每期 SR（`:343-362`）✅
- **C10 判据不被放松**：✅ n_trials 不可改（实测 E1003）、sigma 默认锁定、dsr_min/fdr_max 来自 params 编译冻结
- **C11 流动性分层**：不适用（裁决消费 observe 已算好的证据）
- **C12 观测=1 天**：✅ n_obs 用每期观测数（多空组合 T-1 期），非格子数

## R1–R7 影响

- **R1 真数据跑通**：中性（裁决逻辑与真跑无关）
- **R2 结论可信**：✅ 强支撑——DSR/BH/HAC 口径正确，A4 防单试验作弊，软/硬分级精确；**但 V-1 使"未检验 watch 进组合"削弱结论可信，需裁决**
- **R3 因子零代码扩展**：不适用
- **R7 fail-closed**：✅ 强支撑——缺 n_trials E2003、缺 metrics→watch（不 fail-open 放行）、空集/重复候选拒

---

## 结论

**本模块判据实现正确，`_SOFT_REASONS`、DSR sigma 默认 sampling、n_trials 不可改、BH/Holm/HAC 全部经真实调用确认，无本模块内 P0/P1 代码 bug。** 唯一 P0 是跨模块的 **V-1：`tested:false` 字段全仓无下游读取**——watch（含未检验者）在 combine 拿满权重进 OOS，与文档"watch 不进组合"冲突。这是用户点名的历史决策项①，属"只审查不改"范围，需用户裁决改 combine 还是改文档。

**请确认本模块审查结果**：
- ✅ 确认通过（V-1 留作跨模块决策项汇总） → 我进入第 4 个模块 `allocate.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想当场裁决 V-1（tested:false 下游处理） → 告诉我
