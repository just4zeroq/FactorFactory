# S0 · 05 编排层

> 编排层是唯一有权的层：它开执行、定顺序、注入口径、代写账本、发布结论。
> 本篇冻结的是**执行器的行为**——同一个 `execute()` 服务整体跑、单步跑和恢复，不允许存在第二份逻辑。

---

## 1. 工位规格

### 1.1 `StationSpec` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `str` | 稳定标识，八个之一 |
| `revision` | `str` | 契约版本；输入/输出 schema 变化才升 |
| `inputs` | `list[{station, key, required}]` | 上游产物，顺序即契约 |
| `outputs_schema_id` | `str` | 本工位产物结构 |
| `default_impl` | `str` | `name@version` |
| `namespaced` | `enum` | `open` / `closed_multi` / `single` / `fixed` |
| `param_class_map` | `map[str, enum]` | 哪些参数属于构成性 |
| `side_effect` | `enum` | `pure` / `repeatable` / `append_only` |
| `guards_pre` | `list[str]` | 执行前必须成立的条件标识 |
| `guards_post` | `list[str]` | 执行后必须成立 |
| `standalone` | `enum` | `allowed` / `requires_frozen_plan` / `probe_only` |
| `event` | `str` \| `null` | 本工位触发的事件（`Budget` / `Publish`） |

`probe_only` 用于 `append_only` 工位：允许只读重算，**不存在提交式单步**。规格是**数据表**。加约束、改默认、改名字空间都只改数据，不改执行器。

### 1.2 八个实例

| # | `name` | `inputs` | 产出 | `side_effect` | `event` | `standalone` |
|---|---|---|---|---|---|---|
| 1 | `Boundary` | `Pin[]`、保留区声明 | `ScopeParcel` | `repeatable` | — | `requires_frozen_plan` |
| 2 | `Panel` | `Boundary`、`Pin[]`、Reveal 记录 | `PanelParcel` | `repeatable` | — | `requires_frozen_plan` |
| 3 | `Target` | `Panel`、`caliber.label`、`caliber.execution` | `LabelParcel` | `pure` | — | `requires_frozen_plan` |
| 4 | `Search` | `Label`、`Panel` | `CandidateParcel` | `append_only` | `Budget` | **`probe_only`** |
| 5 | `Evidence` | `Candidate`、`Panel`、`Label` | `EvidenceParcel` | `pure` | — | `allowed` |
| 6 | `Verdict` | `Evidence` | `VerdictParcel` | `append_only` | — | **`probe_only`** |
| 7 | `Portfolio` | `Verdict`、`Panel`、`Boundary` | `PortfolioParcel` | `repeatable` | — | `allowed` |
| 8 | `Certify` | 全部上游 | `CertificateParcel` | `repeatable` | `Publish` | `allowed` |

`Boundary` 的输入里是**保留区声明**（编译期常量），不是保留区数据。`Panel` 才读 Reveal 记录决定是否装载保留区。这个区分让「Boundary 要标 `reserved` 理由码」与「不能提前揭示」同时成立。

`revision` 在 S0 统一为 `r1`。升 `r2` 意味着输入或输出 schema 变了，所有历史 `parcel_digest` 必然变化，属于需要显式申报的变更。

---

## 2. 唯一执行入口

```
execute(spec, ticket, resolved_inputs, mode) -> Parcel
```

`mode ∈ {run, step, commit}`。**整体跑、单步跑、恢复都走这一个函数**，差异只体现在 `mode` 与写入目标上。

### 2.1 十五步

| 步 | 动作 | 失败 |
|---|---|---|
| 1 | 取 `spec`，校验 `revision` 与 `index.json` 记录一致 | `E2005` |
| 2 | 校验 `ticket` 与 `blueprint` 的四个指纹一致 | `E3002` |
| 3 | 按 `spec.inputs` 解析 `Ref`，逐个重算 `digest` 并比对 | `E3001` / `E3002` |
| 4 | 检查每个上游的 `state`，`stale` 即拒绝 | `E3002` |
| 5 | 取 `spec` 的实现名与参数，按 `policy_reads` 组装 `policy_view`、按账本快照填 `runtime_facts`、按 `composes` 预构造 `composed` 子调用单，算出 `call_digest` | `E1002` / `E2008` |
| 6 | 若 `cacheable` 且 `mode=run`，以 `call_digest` 查缓存 | — |
| 7 | 命中且 `payload_digest` 校验通过 → 构造 `Parcel(origin=cache)`，跳到第 13 步 | — |
| 8 | 构造 station-scope 设施（log / rng / digest），调用能力 `run(call)`；有状态实现先 `phase=fit` 再 `phase=generate` | `E3004` |
| 9 | 校验返回结构符合 `outputs_schema` | `E3003` |
| 10 | 重算 `payload_digest`，与返回值声明比对 | `E3002` |
| 11 | 落盘 `payload/`（内容寻址，不存在才写） | `E5001` |
| 12 | 构造 `Parcel`（`digest = H(call_digest, payload_digest)`），落盘 `parcel.json` | `E5004` |
| 13 | 写 `trace.jsonl`（失败也写，`status=failed`） | `E5001` |
| 14 | 若 `spec.event` 且 `mode=run`，执行事件（见 §5）；`mode=step/commit` **一律不触发** | `E3006` |
| 15 | 状态机转移（`mode=step` 时只写 `steps/`，不动正式状态） | `E3005` |

**第 5 步是这条流水线唯一允许构造调用单的地方**。实现永远拿不到「自己拼一个调用」的能力：口径、事实、子调用三个通道全部由执行器填。这比在每个能力里加校验可靠——它连拼装的机会都没有。

`mode=commit` 与 `mode=run` 的差别只在写入目标（前者覆盖正式工位目录、后者写首次产物）与状态推进，**事件一律只在 `mode=run` 触发**。

第 8 步之后任何一步失败，都必须把 `status=failed` 与错误码写进 trace，**并且不留下半成品 `parcel.json`**。半成品是最坏的形态：它会让下一轮读到一个「存在但不完整」的产物。

### 2.2 输入解析的三条规则

1. **顺序即契约**。`spec.inputs` 是有序列表；实现按位置取，不按名字匹配。这样「少传一个」必然错位而不是被静默补空。
2. **必须重算指纹**。读 `parcel.json` 里的 `digest` 不够——文件可能被改。重算 `payload_digest` 并与声明比对（`E3002`）。
3. **不信任 `lineage`**。`lineage` 用于审计与展示，依赖判定用 `inputs` 的实际解析结果。二者不一致本身是 `E3002`。

### 2.3 重建失败必须显式

产物加载后若无法还原成可用结构（列缺失、索引不一致、形状不符），必须 `failed`（`E3003`）。

**禁止**降级为「本步跳过、产物为空」。空产物成功是这套系统历史上最贵的错误形态：流水线全绿，结论建立在空输入上。

---

## 3. 八工位产物结构

每个 `Parcel.payload` 的必填字段。少一个字段即 `E3003`。

| 工位 | payload 字段 |
|---|---|
| `Boundary` | `observable_mask_ref`、`tradable_mask_ref`、`tiers_ref`、`reason_codes_ref`、`axis_resolved` |
| `Panel` | `panel_ref`、`shape`、`time_index_ref`、`field_manifest`、`causal_report`、`quality_report`、`holdout_visibility` |
| `Target` | `label_ref`、`score_ref`、`width_series_ref`、`effective_n`、**`valid_bar_mask_ref`**、**`dropped_bars`（数量 + 原因码）**、`overlap_policy_used` |
| `Search` | `candidates_ref`、`count`、`dedup_digest`、`fitted_state_ref`（可空）、`generators` |
| `Evidence` | `observations_ref`、`method_ids`、`null_model_id`、`cost_attribution_ref`、**`single_factor_net_ref`** |
| `Verdict` | `decisions_ref`、`reason_counts`、`n_trials_used`、`criteria_digest`、`caliber_digest` |
| `Portfolio` | `score_ref`、`weights_ref`、`members`、`domain_digest`、`windows_used`、`cost_breakdown`、`net_ref`、`binding_constraints`、**`evidence_grade`** |
| `Certify` | `metrics`、`folds_detail`、`reproducible_manifest`、`verdict_refs`、`holdout_status`、`evidence_grade` |

三条跨工位要求：

1. **`members` / `candidates` 不可为空**，且每项可回溯到 `candidate_id` 与裁决记录。
2. **`n_trials_used` 必须来自账本快照**，不能由参数传入。
3. **`criteria_digest` 必须等于 `policy_digest` 的判据子集**，且 `Verdict` / `Portfolio` / `Certify` 三处的 `caliber_digest` 必须逐字相同——这是「同一口径」在产物层的落点。

第四条关于证据等级：`Portfolio` 的 `evidence_grade` 恒为 `in_sample`（它在搜索区间内），`Certify` 的恒为 `out_of_sample`。发布流程只接受后者，这条让「用探索段表现发布结论」在结构上做不到。

### 3.1 payload 字段类型与可空性

上一张表只给字段名，不够写实现。类型词汇沿用 `01-data-contracts.md` §3：`Ref` / `Digest` / `str` / `int` / `float` / `bool` / `enum` / `map` / `list[...]` / `timestamp` / `duration`。后缀 `?` 表示**可为 `null`，但键必须存在**——用缺键表达「没有」会与「还没写」混为一谈。类型不符、缺必填键、`Ref` 解析后 `digest` 不符，分别是 `E2003` / `E2003` / `E3002`。

**Boundary**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `observable_mask_ref` | `Ref` | 否 | `(T,N)` 布尔，进入统计与中性化 |
| `tradable_mask_ref` | `Ref` | 否 | `(T,N)` 布尔，允许下权重；与上一张不得同源 |
| `tiers_ref` | `Ref` | 否 | `(T,N)` 流动性分层（`int8`），未入池为 `-1` |
| `reason_codes_ref` | `Ref` | 否 | `(T,N)` 排除理由码；`0` 表示纳入，其余取 §`boundary` 的枚举 |
| `axis_resolved` | `map` | 否 | `{mine:{start,end}, oos:{start,end}, holdout:"reserved"}`，已按供给覆盖裁剪 |

**Panel**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `panel_ref` | `Ref` | 否 | 对齐后面板；列集 = 字段注册表 ∩ 已固化供给覆盖 |
| `shape` | `map` | 否 | `{bars:int, symbols:int, fields:int}`，三个键都要有 |
| `time_index_ref` | `Ref` | 否 | 唯一时间轴（UTC，右闭合）；下游不得自建时间轴 |
| `field_manifest` | `list[map]` | 否 | 每项 `{name, dtype, unit, available_lag, supply_version}`；空列表即 `E3003` |
| `causal_report` | `map` | 否 | 结构见 `04-capability.md` §3.3 |
| `quality_report` | `map` | 否 | 同上 |
| `holdout_visibility` | `enum` | 否 | `reserved` / `revealed`；取自 `runtime_facts`，不得由 `params` 传入 |

**Target**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `label_ref` | `Ref` | 否 | `(T,N)` 前瞻收益，与因子同索引 `t` |
| `score_ref` | `Ref` | 否 | 截面变换 + 中性化后的分数 |
| `width_series_ref` | `Ref` | 否 | `(T,)` 逐时点有效成员数 |
| `effective_n` | `int` | 否 | 通过宽度门禁的时点数，不是 bar 总数 |
| `valid_bar_mask_ref` | `Ref` | 否 | `(T,)` 布尔；下游一律按它过滤，不再自行判定 |
| `dropped_bars` | `map` | 否 | `{count:int, by_reason:{width_below_min, not_estimable, holdout, holdout_leak}}`；`count=0` 也必须写。四类互斥：窗口越界 / 落在保留区 / **窗口触及保留区** / 宽度不足 |
| `overlap_policy_used` | `enum` | 否 | 必须逐字等于 `caliber.label.overlap_policy`；未声明时为 `"none"`。`nw_adjusted` 不改样本，调整责任在 Evidence——若不回写这个字段，下游会把重叠标签当成独立观测算朴素标准误 |

**Search**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `candidates_ref` | `Ref` | 否 | 去重后候选；每项含 `candidate_id`、`payload`（JSON AST）、`lineage` |
| `count` | `int` | 否 | 交付候选数（**不是** `n_trials`） |
| `dedup_digest` | `Digest` | 否 | 去重键集合指纹 |
| `fitted_state_ref` | `Ref?` | **是** | 有状态算法必须非空；无状态必须显式 `null` |
| `generators` | `list[map]` | 否 | `[{name, impl_version, params_digest, produced:int}]`；多算法一次计数 |

**Evidence**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `observations_ref` | `Ref` | 否 | 逐候选 `{candidate_id, metrics, null_distribution, cost_attribution}` |
| `method_ids` | `list[str]` | 否 | 每个指标的方法名@版本，长度与指标数一致 |
| `null_model_id` | `str` | 否 | 零分布模型名@版本，必须等于冻结口径里的值 |
| `cost_attribution_ref` | `Ref` | 否 | 费用 / 滑点 / 资金费三项分解 |
| `single_factor_net_ref` | `Ref` | 否 | 必须由与组合层同一 `allocate` + `backtest` 产生 |

**Verdict**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `decisions_ref` | `Ref` | 否 | 逐候选 `{candidate_id, decision, reason_codes, statistics}` |
| `reason_counts` | `map` | 否 | 原因码 → 计数；**各项之和必须等于候选数** |
| `n_trials_used` | `int` | 否 | 来自 `Budget` 快照，不接受参数传入 |
| `criteria_digest` | `Digest` | 否 | 判据子集指纹，必须是 `policy_digest` 的子集 |
| `caliber_digest` | `Digest` | 否 | 与 `Portfolio` / `Certify` 三处逐字相同 |

**Portfolio**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `score_ref` | `Ref` | 否 | 合成后分数 |
| `weights_ref` | `Ref` | 否 | `(T,N)` 目标权重；未持仓写 `0`，不得写 `NaN` |
| `members` | `list[map]` | 否 | 非空；`{factor_id, candidate_id, available_from, weight_share}`，须回溯到裁决记录 |
| `domain_digest` | `Digest` | 否 | 必须等于 `Ticket.domain_digest` |
| `windows_used` | `map` | 否 | `{fit:{start,end}, embargo:int, eval:{start,end}}` |
| `cost_breakdown` | `map` | 否 | `{fees, slippage, funding}`，单位 USD |
| `net_ref` | `Ref` | 否 | 净收益曲线 + `detail=full` 账本引用 |
| `binding_constraints` | `list[enum]` | 否 | 真正起作用的约束；空列表必须附解释，否则视为口径未生效 |
| `evidence_grade` | `enum` | 否 | 恒 `in_sample` |

**Certify**

| 字段 | 类型 | 空 | 语义 |
|---|---|---|---|
| `metrics` | `map` | 否 | 键带单位后缀（`sharpe` / `cagr_pct` / `mdd_pct`），渲染层不换算 |
| `folds_detail` | `list[map]` | 否 | 每折 `{index, test_window, n_obs, sharpe, verdict}`；**空列表即失败**（V4 的 `0 folds` 判通过） |
| `reproducible_manifest` | `map` | 否 | 六项 `{plan_digest, blueprint_digest, policy_digest, supply_digest, operators_version, seed}`，缺一即失败 |
| `verdict_refs` | `list[Ref]` | 否 | 认证段复算所用的 Sentinel 结论引用 |
| `holdout_status` | `enum` | 否 | `reserved` / `revealed` |
| `evidence_grade` | `enum` | 否 | 恒 `out_of_sample` |

---

## 4. 状态机

### 4.1 状态

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `pending` | 未执行 | 否 |
| `running` | 执行中 | 否 |
| `done` | 成功且产物已登记 | 是（可被标 `stale`） |
| `failed` | 显式失败，带错误码 | 是（可显式重试） |
| `skipped` | 有原因的跳过 | 是 |
| `stale` | 上游或口径变化，产物不再可信 | 否（需重跑） |

### 4.2 转移表

| 从 | 到 | 允许条件 |
|---|---|---|
| `pending` | `running` | 调度开始 |
| `running` | `done` | 产物落盘 + 指纹校验通过 |
| `running` | `failed` | 任意 `E3xxx` / `E5xxx` |
| `running` | `skipped` | 带 `reason`，且属于「结果性跳过」 |
| `pending` | `skipped` | 前置条件不满足，带 `reason` |
| `done` | `stale` | 上游变化或口径变化 |
| `stale` | `pending` | 显式重跑 |
| `failed` | `pending` | 显式重试 |
| 其他 | — | `E3005` |

**两条判定规则**：

- 「无候选通过裁决」是**结果**，记 `skipped(reason=no_candidate_passed)`，不是失败。
- 「折数为 0」「面板重建失败」「依赖缺失」是**失败**，不得写成绿色跳过。

这两条是状态机里唯一需要人判断的地方，因此必须写成显式枚举，不允许自由文本 `reason`。

### 4.3 `reason` 枚举

`no_candidate_passed` / `no_candidates_generated` / `upstream_skipped` / `precondition_unmet` / `insufficient_cross_section`。

---

## 5. 三类事件

事件不是工位。它们绑定一次执行，只发生一次，结果写入账本。

| 事件 | 账本键 | 触发时机 | 幂等语义 |
|---|---|---|---|
| `Reveal` | `(ticket_id, "reveal", window_id)` | **开执行后、Boundary 之前** | 已存在 → 返回旧值，不重新揭示 |
| `Budget` | `(ticket_id, family)` | `CandidateParcel` 落盘后 | 已存在 → 返回旧值，不重复计数 |
| `Publish` | `(ticket_id, "publish")` | Certify 通过后 | 已存在 → 不重复发布 |

`Reveal` 必须在 `Boundary` 之前：它决定保留区在本次执行中是否可见，而 `Panel` 要按这个决定装载数据。`Boundary` 只需**保留区声明**（编译期常量）就能标出 `reserved` 理由码，不需要看到保留区数值——声明与数据分开，是这里唯一能同时满足「不提前揭示」与「排除理由完整」的做法。

`Budget` 必须后置：它登记的是实测候选数，在候选产生前无法确定。

### 5.1 `Budget` 的两个细节

1. **值来自 `CandidateSet.count`**（去重后实测），不来自配置声明。执行器在 `CandidateParcel` 落盘成功后读取该字段并写入。
2. **全局单调**：同一 `family` 的累计值只能增加。写入前读当前累计，若新值会导致累计下降 → `E2005`。

`Budget` 一旦写入不可撤销。因此「落盘前校验候选合法性」（C14 / C16 双保险）必须在事件之前完成——否则会出现「计数已登记，但候选随后被判非法」。

### 5.2 `Reveal` 的语义

`Reveal` 决定保留窗口与保留资产在本次执行中是否可见。它的结果写入 `index.json` 的 `events.reveal`，后续所有工位读该记录，**不重新计算**。

单步模式下 `Reveal` 默认复用记录；若记录不存在（例如从未整体跑过），单步**不得自行揭示**，直接 `E3006`。

---

## 6. 单步

```
flow station <name> --on <ticket> [--probe | --commit]
```

| 规则 | `--probe`（默认） | `--commit` |
|---|---|---|
| 构成来源 | 读 ticket 目录下的 `frozen/`（`plan.json` + `context.json`），**禁止重新编译**，也没有 `--plan` | 同 |
| 依赖 | 校验上游 `Ref` 与指纹 | 同 |
| 写入位置 | `steps/<seq>-<Station>-<payload hex16>/` | `parcels/<seq>-<Station>-<payload hex16>/`（与整体执行同构） |
| `origin` | `step` | `commit`（**不是 `run`**：让「只看记录就判断是否真跑过」仍成立） |
| 正式状态 | 不变（不写 `state.jsonl`） | 追加一条 `state.jsonl` 记录（只追加，不覆盖） |
| 事件账本 | 不写 | **不写** |
| 已发布结论 | 不变 | **仍不得改变** |
| 重复提交 | — | 同一 `payload_digest` 已在正式目录 → **幂等短路**：不重写产物、不追加状态，返回 `idempotent=true` |
| 可用范围 | 八个工位全部 | **仅 `repeatable` / `pure` 工位**；`append_only` 工位直接 `E3006` |
| 退出码 | 0 / 1 / 2 / 3 | 同 |

**目录名含 `payload_digest` 前 16 位**，所以「同名目录」等价于「同内容产物」。这带来一个必须显式处理的后果：重算得到同一 payload 时，目录已存在而 `log.jsonl` 里的时间戳必然不同，`write_bytes` 的「异内容拒绝覆盖」会把它判成 E5004。正确语义不是放宽覆盖（那会打开改写历史产物的口子），而是**幂等**——这次提交没有产生新产物。

**冻结方案落盘在 `frozen/`**：`plan.json` 是编译后的方案原文，`context.json` 是 `{station_params, station_impls, policy_view, policy_digest, fingerprints, live}`。单步命令没有 `--plan`，它只能从磁盘还原构成；否则「单步重放」就成了「拿别的参数重算一遍」。缺任一份 → `E3001`，不猜默认值。

两条边界把「单步重试到通过」彻底关掉：

1. **`--commit` 不触发任何事件。** 事件只由整体执行触发。否则「单独把认证工位提交到通过」就是一条发布后门。
2. **`append_only` 工位没有提交式单步。** 提交一个 `Search` 会需要重复计数，提交一个 `Verdict` 会需要改写结论；两者都不允许。`--commit` 也不能改变已发布结论：新结论只能在完整链上产生。

`origin` 用 `commit` 而不是沿用 `run`，是因为「这个产物是不是完整跑出来的」必须能从记录里一眼看出。合并成一个值会让单步提交与整体执行在审计上无法区分。

### 6.1 单步的四个前置校验

1. `frozen/plan.json` 与 `frozen/context.json` 可读（`E3001`）；
2. 上游产物存在且 `state=done`（`E3001` / `E3002`）；`payload.json` 被改写 → `E3002`（重建失败显式，不降级为空产物）；
3. 若 `spec.side_effect=append_only` 且 `mode=commit` → `E3006`（无提交式单步）；
4. `spec.standalone = probe_only` 时不得 `--commit`（`E3006`）；
5. 若工位需要事件记录（`Panel` 需要 Reveal）而记录不存在 → `E3006`，**不得自行触发**。

### 6.2 能力模式单步的已知边界

`--live` 的工位输入分两类：**供给型**（`Pin` / `FieldStream`，由 `supply_fn` 从已发布 seed 重新注入）与**对象型**（`Panel` / `Scope` / 候选暴露，只在 `run` 的进程内存在）。前者单步能真跑（`Boundary` / `Panel` 已实测），后者当前**fail-closed `E3003`**——不拿空对象跑一遍再报 `ok`。

要让对象型输入也能单步，需要「按 Ref 从 ObjectStore 重建对象」（ndarray 走 parquet 可无损还原；`Panel` 还需要 `times`/`symbols`/`fields` 的完整 sidecar）。这是下一步 S5-M2 的范围；在那之前这条边界必须显式，不能靠「反正没人单步」掩盖。

---

## 7. 失效传播

| 触发 | 传播范围 | 是否新试验族 |
|---|---|---|
| 基线升版 `vN → vN+1` | 全部工位 `stale` | 若触及构成性参数 → **是** |
| 构成性参数变化 | 全部工位 `stale` | **是** |
| `caliber` 任一子块变化 | 全部工位 `stale`（标签、证据、组合、认证同时变） | **是** |
| 操作性参数变化（并行度、日志级别） | 不传播 | 否 |
| 某工位实现或版本变化 | 该工位及其全部下游 `stale` | 否 |
| 某工位参数变化（构成性） | 该工位及下游 `stale` | **是** |
| 数据版本（pin）变化 | `Panel` 起全部下游 `stale` | 否（事实演进） |
| 组合域 `domain_digest` 变化 | `Portfolio` 及下游 `stale` | **是** |
| 组合三段窗口变化 | `Portfolio` 及下游 `stale` | **是** |
| 单步 `--commit` 覆盖产物 | 该工位下游 `stale` | 否 |
| 单步 `--probe` | 不传播 | 否 |

「是否新试验族」这一列比 `stale` 更重要：它决定试验计数是否延续。操作性参数变化不传播、不新建族，这是让「调并行度」不必重跑整条链的前提。

---

## 8. 索引与记录

### 8.1 `index.json`

```json
{
  "ticket_id": "<...>",
  "status": "done",
  "stations": {
    "Boundary": {
      "state": "done",
      "revision": "r1",
      "impl": "rolling_liquidity_pool@v1",
      "params_digest": "sha256:<...>",
      "parcel_digest": "sha256:<...>",
      "origin": "run",
      "updated_at": "<UTC>",
      "stale_reason": null
    }
  },
  "events": {
    "reveal": {"window_id": "<...>", "recorded_at": "<UTC>"},
    "budget": {"<family>": {"count": 0, "recorded_at": "<UTC>"}},
    "publish": {"recorded_at": "<UTC>", "conclusion_ref": "<...>"}
  },
  "steps": [{"station": "Evidence", "seq": 1, "parcel_digest": "sha256:<...>", "at": "<UTC>"}]
}
```

`index.json` 是唯一可变的元数据文件。每次写入必须做「读—校验版本—写」的乐观并发控制，版本不符 → `E5002`。

### 8.2 可观测性判据

只看 `trace.jsonl` + `index.json` + `parcel.json`，必须能回答：

1. 这次执行是否真的跑过？（trace 有八条，状态 `done`）
2. 哪一步失败，错误码是什么？
3. 结论基于哪份口径？（`policy_digest`）
4. 用的是哪份数据？（`pins`）
5. 每个产物的输入是什么？（`lineage` 与 `inputs`）

任何一条需要「打开产物猜」的情况都算缺口。

---

## 9. 恢复

```
flow run --on <ticket_id>
```

| 规则 | 内容 |
|---|---|
| 起点 | 第一个非 `done`（`pending` / `stale` / `failed`）的工位 |
| 前置 | 四个指纹与基线版本全部匹配，任一不符 → `E3002`，**不得「尽量继续」** |
| `origin` | 剩余产物标 `resume` |
| 事件 | 读已记录值，不重新触发 |
| 已完成工位 | 不重跑；除非显式 `--rerun <station>` |

恢复不是「重试到成功」。它只补齐缺失的工位，不改变已经完成的部分。

---

## 10. S0 验收点（本篇）

| # | 断言 |
|---|---|
| O1 | 八条 `StationSpec` 全部注册，`flow stations` 可输出，字段完整 |
| O2 | 上游缺失 → `E3001`，退出码 1，提示指明缺哪个工位 |
| O3 | 手改 `payload` 不改 `digest` → `E3002` |
| O4 | 能力返回 `None` / 空结构 → `E3003`，状态 `failed`，**不得 `done`** |
| O5 | 非法状态转移（`failed → done`）→ `E3005` |
| O6 | 同 `(ticket, family)` 重复 `Budget` → 返回旧值，累计不变 |
| O7 | `Budget` 值等于 `CandidateSet.count`，改配置声明不影响 |
| O8 | 候选被判非法时 `Budget` 尚未写入（顺序正确） |
| O9 | `--probe` 后 `index.json` 状态、账本、正式产物全部不变 |
| O10 | 对 `Verdict` / `Search` 执行 `--commit` → `E3006`；对 `Portfolio` 执行 `--commit` → 成功但**不触发任何事件**、不改已发布结论 |
| O11 | 单步缺 `Reveal` 记录 → `E3006`，不自行揭示 |
| O12 | 改操作性参数（并行度）→ 无 `stale` 传播、试验族不变 |
| O13 | 改构成性参数 → 全部 `stale` 且试验族变更 |
| O14 | 数据 pin 变化 → `Panel` 起 `stale`，试验族不变 |
| O15 | `index.json` 并发写（两进程）→ 一成一败，败者 `E5002` |
| O16 | 八个 dummy 实现的纵切：8 Parcel / 8 trace / 退出码 0，且无任何真实数值 |
| O17 | 全新进程重放同一纵切 → 八个 `parcel_digest` 逐位一致 |
| O18 | `Reveal` 记录产生于 `Boundary` 之前；`Boundary` 在无 Reveal 记录时仍能标出 `reserved` 理由码（它读的是声明） |
| O19 | 有状态算法执行时产生两张调用单（`fit` / `generate`），`call_fit.inputs` 不含评估区间引用；人为塞入 → `E1016` |
| O20 | `Verdict` / `Portfolio` / `Certify` 三处的 `caliber_digest` 逐字相同；改一处 → 下游全部 `stale` 且试验族变更 |
| O21 | 单步 `--commit` 的产物 `origin=commit`，与 `run` 可区分；整体执行的产物 `origin=run` |
| O22 | `Portfolio` 产物的 `evidence_grade=in_sample`，`Certify` 的为 `out_of_sample`；用前者尝试发布 → 拒绝 |
