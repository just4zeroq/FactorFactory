# 模块审查 25 · `v5/capability/ops/certify.py`

> 审查日期：2026-11-18
> 依据：源码 359 行全文通读；依赖 `compute_digest`（指纹）、`backtest.bars_per_year`；编排层 `capability_bridge.py:_run_certify_folds`（L844–1015，逐折净表现回填 + 资金费透传）逐行核对；设计 `s0/04-capability.md` §3.12（L626）、`05-contracts.md` I23/I24、`06-evaluation.md` §8、`12-stages.md` S7；测试 `test_v5_s7_certify.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Certify 是八步里**唯一**允许产出 `out_of_sample` 结论的工位（§3.12 / 06 §8.1）。四条不可动摇的边界：
1. **只认证"未参与搜索"的那段**：`oos_window` 必须严格晚于组合层 `windows_used.eval.end`（I24）；组合自己的样本内净值是诊断量，拿它当结论就是把"构造是否可行"说成"能不能交易"。
2. **折是 walk-forward 的折**：时间序列只有前向可用；折间留 `embargo` 根 bar 隔离段，宽度由参数给、不由实现猜（标签重叠跨折泄漏，06 §7）。
3. **判据不自算**：返回的 `verdict_refs` 引用 Sentinel 裁决结果；认证段只复算**净表现**，不另起显著性口径。
4. **可复现清单只放确定性事实**：策略、口径、面板、分数、域、折参数、种子都进清单；墙钟/路径/耗时一律不进（否则同一结论两次认证指纹不同）。

**关键定位**：本模块只做"解析窗口、切折、装配清单"三件事；**逐折的持仓与净值由编排层驱动 allocate/backtest 子调用算出后回填**（s0/04 §1.3：能力不自建子调用）。因此 `metrics` 里净表现字段此处恒为空壳——本模块不给自己发一张没有回测的合格证。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 仓库 `venv/Scripts/python.exe` 调用 `run(call)` **实际打印**；脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_rest.py`（系统临时目录），复用 `tests/v5/test_v5_s7_certify.py` 的 `_fixture/_call`。

### 场景
60 根 1d bar（2024-01-01 起）；`oos_window=2024-02-06..02-29`，`fold=2`，`embargo=1`（bar）；组合声明 `evidence_grade=in_sample`、`windows_used.eval=2024-01-25..02-05`；成员 c1 裁决 pass。

### 输入
```
capability="certify" impl="walk_forward"
params={"folds":2,"embargo":1,"oos_window":{"start":"...","end":"..."}}
inputs=(portfolio{score,members,domain_digest,windows_used,evidence_grade},
        panel, tradable_mask, tiers, verdict_set)
policy_view={"allocate":{...},"net":{...}}  runtime_facts={"holdout_visibility":"reserved"}  seed=11
```

### 必查要点逐项实测输出

**walk-forward 逐折净表现（编排层回填）**——certify 本身真实输出折结构：
```
folds_detail = [(fold=1, start_index=36, end_index=48, n_bars=12),
                (fold=2, start_index=49, end_index=60, n_bars=11)]
```
折间 `48→49` 留 1 根 embargo gap；本模块 metrics 里 `net_return/net_sharpe/turnover/max_drawdown` 初始为 None（`:338-342`）。编排层 `capability_bridge.py:938-1015` 逐折切出 `[b0,b1)` 的 score/price/tiers/tradable/time/funding，调用 allocate→backtest，再回填：
```
entry["net_return"]=fold_ret; entry["sharpe"]=_sharpe(sample,bpy);
entry["turnover"]; entry["max_drawdown"]; entry["cost_total"]; entry["net_ref"]
metrics["net_return"]=_compound(combined); metrics["net_sharpe"]=_sharpe(combined,bpy)
```
（`:996-1014`，t=0 结构性 0 从样本剔除）。**逐折回填完整，已确认。**

**reproducible manifest 逐字节**——同一 fixture 两次调用：
```
manifest 关键字段 = {schema:'v5.certificate_manifest@1', folds:2, embargo:1,
                     oos_window:{start:'2024-02-06',end:'2024-02-29'}, impl:'walk_forward@1'}
manifest 逐字节一致 = True
```
代码 `_manifest:250-275` 只放确定性事实（policy_view_digest/panel_digest/score_digest/domain_digest/member_ids/folds/embargo/oos_window/seed/impl），无墙钟/路径。

**只认 out_of_sample**——输出 `evidence_grade='out_of_sample'`（`:354`）；输入组合 `evidence_grade` 必须是 `in_sample`，否则 E1021（`:182-190`，实测传 `out_of_sample` 组合 → E1021）。

**资金费三调用点透传**——全仓三个 backtest 调用点均传 funding：
- `capability_bridge.py:645-651`（组合净表现 backtest）：`include_funding=aligned_funding is not None` + InputRef funding。
- `capability_bridge.py:785-790`（单因子 backtest）：同上。
- `capability_bridge.py:970-975`（certify 逐折 backtest）：`include_funding=fold_funding is not None` + `fold_funding=funding[b0:b1]`。
**三处透传完整，已确认。** 但注意：这三处都走生产 `ledger@1`，因此 `24_backtest_engine.md` 的 P0-1 双重计费会**完整传播到认证净表现**——certify 本身无资金费逻辑，不承担该 bug，但认证数字会偏低。

**fold embargo 以观测计（C12）**——`embargo` 参数按 bar 校验（`_int_param(params,'embargo',minimum=0)`，`:282`），`_split` 用 bar 索引 `cursor += width + embargo`（`:138-163`），实测折间 1 根 gap。非日历日折算 ✅。

**其他 fail-closed（源码逐条核对 + 测试）**：oos 与 eval 重叠 → E1021（`:303-309`，实测）；oos 超面板覆盖 → E3003（`:120-134`）；成员无裁决记录 → E3003（`:230-234`）；成员裁决 reject → E1021（`:236-240`）；非 in_sample 组合 → E1021（实测）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| CF-1 | **`tested:false` 的 watch 候选进 OOS 认证（P0-2 的认证端）**：certify 的 `_verdict_refs` 只校验 `decision ∈ {pass,watch}`（`:38,235-240`），不读 `tested`；combine 给 watch 满权重（22 号文档 C-1），certify 照单认证。三处叠加：未检验候选拿满权重进 OOS。 | **P0（跨模块决策项①，认证端确认）** | `certify.py:38,235-240`；`20_verdict.md` V-1；`22_combine.md` C-1 | **已确认事实**，与 verdict/combine 同一 P0-2，不在本模块单独修。属用户三项决策之一，本轮只审查。 |
| CF-2 | **本模块净表现字段恒空壳，靠编排层回填**：`metrics.net_*=None`（`:338-342`）。若编排层漏调 `_run_certify_folds`，证书会带 None 净表现而不报错。 | P2（架构约束） | `certify.py:327-343`；`capability_bridge.py:1152` 调用点 | **已确认符合设计**（能力不自建子调用）；编排层已有回填且测试覆盖。建议测试补一条"未回填证书不得发布"的守卫。 |
| CF-3 | **P0-1 双重计费传播到认证净表现**：certify 三折 backtest 走生产 engine，资金费被扣两次（24 号文档 E-1），认证 net_return/net_sharpe 偏低。 | P0（下游影响，非本模块 bug） | `capability_bridge.py:970-975`；`24_backtest_engine.md` E-1 | **已确认传播链**：修 P0-1 本模块自动受益；certify 自身无资金费代码，不单独修。 |
| CF-4 | manifest 的 `policy_view_digest` 是口径视图摘要，不是 `policy_digest` 本体（缺 baseline_version/constitutive_subset 通道），真正 policy_digest 由编排层回填（`:255-260` 注释）。 | P2（已确认设计） | `certify.py:255-264` | **已确认正确**：如实命名避免"同名不同值且不报错"的证书对账灾难。 |

**多了什么**：无冗余。`fold_count/n_bars/embargo_bars/bars_per_year` 等结构事实与净表现一起落盘，便于审计。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| CombinatorialPurgedCV / TimeSeriesSplit（skfolio / sklearn） | **未接线（P1 扩展缺口）** | `07-components.md:42` 规划；全仓无 skfolio（见 22 号文档 C-2）；当前自实现 walk-forward 折 + bar embargo |
| walk-forward 前向折 + bar embargo | **采纳** | `:138-163`；加密 24/7，embargo 以 bar 观测计（C12），不用日历 |
| 可复现清单内容寻址（确定性字段） | **采纳** | `_manifest:250-275`；墙钟/路径/耗时不进 |
| 用日历日折算 purge / embargo | 拒绝 | C12：1 观测=1 bar，日历折算会错配频率 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **只认未参与搜索段**：`window[0] <= eval_end` → E1021（`:303-309`，实测）。
2. **walk-forward 折 + embargo bar**：`_split`（`:138-163`），实测折间 1 根 gap；usable = (hi−lo)−embargo×(folds−1)，不足 → E3003（`:144-152`）。
3. **manifest 逐字节确定**：`_manifest`（`:250-275`），实测两次一致。
4. **只认 out_of_sample**：输入必须 in_sample（`:182-190` 实测），输出恒 out_of_sample（`:354`）。
5. **verdict_refs 只覆盖成员**：`:209-247`，成员无裁决/reject 分别 E3003/E1021。
6. **窗口越界 fail-closed**：oos 无 bar / 早于覆盖 / 超覆盖全 E3003（`:106-135`）。
7. **policy_view_digest 如实命名**：`:255-264`，避免同名不同值（CF-4）。
8. **bars_per_year 非有限 E3003**：`:344-345`。
9. **资金费三调用点透传完整**：bridge `:645,785,970`（已核）。
10. **逐折净表现回填完整**：bridge `:996-1014`（已核）。

**发现的问题**：CF-1（P0-2 认证端，跨模块决策项①）、CF-2（P2 架构约束）、CF-3（P0-1 传播，非本模块 bug）、CF-4（已确认设计）。**本模块无自身 P0/P1 代码 bug**。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:451`（watch 不进组合） | 与 CF-1 实测"watch 进认证"冲突 | 按 P0-2 裁决结果二选一更新（与 20/22 号文档同条） |
| `05-contracts.md` I24 / AC25（窗口不重叠） | 与 `:303-309` 一致 | 无需更新 |
| `s0/04-capability.md:626`（certify 参数） | 与 folds/embargo/oos_window 一致 | 补"embargo 以 bar 观测计（非日历日）"的说明（C12） |
| `12-stages.md` S7 门槛 | 认证段只认 out_of_sample | 与实现一致；如 P0-1 修复影响认证数字，补一条"净表现依赖回测口径、试算前先确认资金费只扣一次" |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| walk-forward 切折 + bar embargo | ✅ 已支持 | `_split`，实测折间 gap |
| 逐折净表现（allocate+backtest 回填） | ✅ 已支持 | 编排层 `:938-1015` |
| 新风险度量（CDaR/EDaR，skfolio） | ❌ 未接线 | 全仓无 skfolio（P1 决策项②，与 combine 同） |
| 组合式交叉验证（CPCV） | ❌ 需改代码 | 当前为单条前向折，非 CPCV 多路径 |
| holdout 揭示只在认证段 | ✅ 已支持 | runtime_facts.holdout_visibility=reserved，输入组合须 in_sample |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ bars_per_year 来自 backtest.bars_per_year 中位步长（`:314`），calendar_365 语义 ✅
- **C2 资金费按事件**：⚠️ 透传完整（三调用点），但生产 engine 双重计费（CF-3，见 24 号文档 P0-1）
- **C12 1 观测=1 天、embargo 以观测计**：✅ `embargo` 按 bar 校验与切折（`:282,:138-163`），非日历折算
- **C10 判据不可放松**：✅ 只引用 Sentinel 裁决、不自算显著性；oos 必须严格晚于 eval

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（认证真跑在用）
- **R2 结论可信**：⚠️ 混合——manifest 确定、窗口严格；但 CF-1（watch 满权重进 OOS）+ CF-3（P0-1 传播）削弱认证数字可信度
- **R5 单因子可归因**：✅ 逐折 net_return/cost_breakdown/net_ref 落盘，可拆到折与成员
- **R7 fail-closed**：✅ 强支撑——oos 重叠/越界/成员无裁决/reject/非 in_sample 全拒

---

## 结论

**本模块 walk-forward 切折、embargo 以观测计、reproducible manifest 逐字节确定、只认 out_of_sample、资金费三调用点透传、逐折净表现回填均经真实调用/源码核对确认，无自身 P0/P1 代码 bug，可确认通过。** 三项跨模块影响需汇总裁决：①**P0-2 认证端**——watch（含未检验者）拿满权重进 OOS（与 20/22 同条）；②**P0-1 传播**——生产 engine 双重计费使认证净表现偏低（修 engine 后本模块自动受益）；③skfolio 风险度量未接（P1 决策项②）。均属"只审查不改"范围。

**请确认本模块审查结果**：
- ✅ 确认通过 → 8 份文档全部完成，我汇总 P0/P1 清单
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想当场裁决 P0-2 / P0-1 汇总口径 → 告诉我
