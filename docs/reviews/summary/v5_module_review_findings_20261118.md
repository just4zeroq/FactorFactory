# V5 逐模块审查总账（加密货币 · 真实因子挖掘）

> 日期：2026-11-18
> 范围：`v5/` 全部 80 模块（22,583 行）、36 测试文件、26 篇设计文档，分 12 单元 / 4 组并行只读审查。
> 判据：R1–R7（"能真的拿它挖因子"）；加密检查表 C1–C12。框架见 `v5_module_review_plan_20261118.md`。
> **本轮只读，零代码改动**。四份分组报告：`review_u8_u9_u10_20261118.md`、`review_u6_u7_20261118.md`、`review_u3_u4_u5_20261118.md`、`review_u1_u2_u11_u12_20261118.md`。

---

## 0. 一句话总评

架构与判据主体的设计纪律**经受住了逐模块核对**——B1–B4 事故修复全部回归、DSR/置换/去重/年化口径正确、八工位单一执行路径、真跑证据充分。但发现 **7 项 P0**，分三类：①**结论可信**被双重计费污染（唯一可发布结论）；②**因子可用性**卡在列集/合约维度/两流缺失（R3 最大缺口）；③**验证链**在 fresh clone 上整体断裂（27/36 测试未入库）。另有 **3 处计划基线过期**，须先更正再谈修。

---

## 1. P0 总清单（7 项，按"先结论后扩展"排序）

| # | 单元 | P0 项 | 核心证据 | 影响 |
|---|---|---|---|---|
| **1** | U9/U10 | **资金费双重计费**：ml4t 引擎内部已按 `cash_delta=-qty×mark×rate` 扣一次（`broker.py:732-772`，本人核验 `:747` 更新 cash），V5 又把 `funding_df` 传入（`:332`）并在 `:346,363-364` 再减一次 | `backtest_engine.py:332,346,363-364` + `ml4t/backtest/broker.py:747` | **R2**：净收益/夏普失真；经 Certify 逐折进入**唯一可发布 OOS 结论** |
| **2** | U10/U9 | **`tested:false` 的 watch 候选拿满权重进 OOS**：verdict 产 `watch+tested=False`（`verdict.py:280-291`），该字段**全仓无下游读取**；combine `_DECISIONS={pass,watch}` 不读 tested、equal_weight 给 1/K（`combine.py:42,141,376,393`）；certify 接受 watch（`certify.py:38,236`） | 同上 + `s0/04-capability.md:451`「watch 不进组合」 | **R2**：连零分布都没跑的候选与冗余 watch 同等进认证。属计划 §3.3① 决策项 |
| **3** | U12 | **验证链版本控制断裂**：36 个测试文件仅 9 个被 git 跟踪，27 个命中 `.gitignore:145` 被忽略且未跟踪；黄金锚点数据亦未入库 | `git ls-files tests/v5/`=9；`git check-ignore -v` 27/27 命中 | **R1/R2/R7**：fresh clone 后 C1–C12、S6、包隔离、AC63 守卫全消失，"775 passed"无法复现 |
| **4** | U6/U7 | **清算流、跨所价差两流未接面板**：供给层仅 4 数据集、注册表无字段 | `fetch.py:1`、`fields.json`、`live.py:317-321` | **R3/C8**：爆仓失衡、跨所价差两类因子无法挖 |
| **5** | U6/U7 | **合约四维身份零实现**：`underlying/contract_type/expiry/settle` 全仓 Grep 零命中 | 全仓零命中 | **R3/C4**：期限结构因子、同标的跨合约横截面不可做 |
| **6** | U6/U7 | **列集白名单按"列名固定"**：`read.py:257` 精确匹配注册表 9 字段；事件→字段映射硬编码 dict（`live.py:317-321`），加事件流仍需改 Python | `read.py:257-275`、`live.py:317-321` | **R3/C8**：违反"配置声明即引用；加因子零代码" |
| **7** | U6/U7 | **事件注入静默跳过**：配置声明的流若在注册/列缺失，`live.py:378-380` 用 `continue`，与 `read.py` 的 E1014 fail-closed 冲突 | `live.py:378-380,382-384` | **R7/C8**：声明了却缺列时静默，数据退化不可见 |

> P0-1 与 P0-2 同源（Certify 逐折复用生产引擎，`capability_bridge.py:965-981`），修一处即同时闭合认证链的两处污染。

---

## 2. 三处计划基线须先更正（审查推翻了旧假设）

| 旧基线（写在本轮计划/历史报告） | 审查实测 | 后果 |
|---|---|---|
| "五个加密流都未进面板，当前最大 alpha 缺口" | **3/5 已进**：funding/OI/basis 已注入面板且 GP 可引用（`live.py:358-438`，实测候选字段含 basis/funding/OI）；缺口缩为清算+跨所两流 | P0-4 范围收窄，不必重做三流接线 |
| "GC/index 未做（已知遗留）" | **已实现**（`storage/index.py:38 IndexDB`、`gc.py` 有 collect 路径） | 从遗留清单删除；计划 §U3 需改 |
| "内置因子只有 12 个 / 文档称 Alpha158 的 158 个" | 代码 `BUILTIN_FACTORS` **确实 12 个**（mom5/20/60、rev5/1、rv20、hl_range、vwap_gap、vol_ratio、funding_carry、oi_change20、basis_carry）；文档 `11-field-operator-registry.md §6`、`07-components.md §4.2` 称 158 个可用不符 | **D5**：改文档对齐代码，或补 158 个内置池（决策项） |

---

## 3. 逐单元一句话结论

### U8 证据与裁判（无 P0）
- **observe/evaluate/verdict**：统计原语与 fail-closed 扎实，B1（raw vs score 分离）、B2（ls 同根）、B4（horizon_bars 年化）事故修复均已回归（`observe.py:626-634/258/658-667`）；DSR sigma 默认 sampling、BH/Holm/HAC 标准、`_SOFT_REASONS` 软降级正确。
- **P1**：Stage A 多空/IC 用**全池暴露**，不看 `tiers/tradable`，与 S4 分层持仓口径不一致——证据层的分位收益和组合层真实可交易持仓不是同一批标的。

### U9 组合与回测（P0-1）
- **allocate/combine/backtest/backtest_engine**：三合成器、corr_max 贪心去相关（带 dropped_members）、Löwdin 正交化、年化闭合（1h→8760、sharpe=日频×√24）均正确；杠杆>1 E1004。
- **P0-1** 双重计费见上。**P1**：funding_df 无条件传、rebalance 两路径分叉、embargo 单位天/bar 不一致、skfolio 风险最优模式未接线（决策项）。

### U10 认证（P0-2 + P0-1 同源）
- **certify**：walk-forward 逐折净表现回填、reproducible manifest 逐字节确定、只认 out_of_sample、embargo 按 bar——机制正确。
- **P0-2** watch 满权重进 OOS 见上；**P0-1** 逐折复用生产引擎使资金费错误进 OOS。

### U6 工位基础（P0-4/5/6/7）
- **boundary**：双掩码+流动性分层+理由码完整，但未收盘靠供给末端标记而非墙钟；市场维度用 symbol_classes 非 `market∈{spot,futures}`。
- **align/read/label/panel/stream/timeaxis**：未收盘丢弃、field_lags 对账、保留区 P3、缺口不静默填充全 fail-closed；label 的 `forward_raw` 单实现被主标签与 IC 衰减共用，是质量最高的模块之一。
- **P0** 四项见上；**P1**：内置因子文档不符、`available_from`/E1019 语义待裁决。

### U7 搜索与因子内核（无独立 P0，承接 U6 的 P0）
- **search/mining**：四方法齐全、generate 不评估、跨算法共享一次 fitness/seen、内容身份去重、`_RUNNERS` 分派；deap 全局 random 快照/还原**实测通过**、arity 与注册表一致实测通过。
- **factors/inference/stats/params/registry/impls/runtime**：先验池两类来源（内置名+db:）、DSR/BH/BY/HAC/PIT 唯一实现、行内置换、params 默认值与口径默认值严格分离、Call 单点定义+派生指纹。
- **P1 疑点**：`search.fitness` 未见 `mine_slice` 裁剪（若坐实影响 R2，待确认）；`METHOD_STAGES`/`method_id` 重复定义两次（P2）。

### U3 存储与供给链（无 P0，2 项最接近 P0 的 P1）
- 方案 B 路径、append-only、现算身份、市场三护栏、对象存储全部落地（实测通过）。
- **P1-1**：funding 1h 主导 + 合法 8h 间隔被误判 interior gap → `gate_a=false` → E4005，资金费因子无法进成本链（实测复现）。
- **P1-2**：`read_ticket` 取首 / `load_committed_parcel` 取末，同一 append_only 工位双读者分叉，promote 与 certify 可能认证不同因子（实测复现）。

### U4 运行时/意图（无 P0）
- 门禁 B 三条件 + supply.market 第三护栏、指纹唯一公式、E1020/E1004/E1019 现状符合契约。
- **P1**：C2/C3/C13–C17 编译期校验仅注释占位、`explain` 不输出逐条 C 检查、标的集 digest 只对 ohlcv 强制、错误提示仍指向已删除的 manifest/别名表。

### U5 编排层（无 P0）
- 八工位固定顺序、`_parcel_for`/`_scoped_view` 收口、缓存 opt-in、三处 backtest funding 透传、M2 digest 对拍、M3 `state_recovered` 均落地。
- **P1**：ticket 双读者分叉（同 U3-P1-2）、单步 CLI 未接落盘事件账本致已揭示 holdout 重放 E3006、缓存命中返回空对象表。

### U1 契约 / U2 设施 / U11 应用 / U12 测试（P0-3 在 U12）
- **U1**：49 码零重复缺号、10 kind 唯一、Call 单点、exit 3≠1 实测通过；10 个错误码（含 E1019）生产代码从未引用，E1019"未实现"声明三篇文档未清干净。
- **U2**：F5 1024 LRU、F7 限流、32MB 轮转、E5001、rng 确定性全落地，日志借 structlog 非过度自研；`_cache_hit_last` 在 skipped 路径可能残留 `origin=cache`。
- **U11**：17 条命令三者一致、CLI 口径零泄漏、evaluate/backtest/certify 只读冻结 ticket、`--live` 已注入 3 个加密流；`estimate` 忽略 `slippage_bp` 与回测口径分叉。
- **U12**：740 个测试定义零重复、AC63 三层容差、S6 运维测试齐全；**P0-3** 27/36 测试未跟踪（见上）。

---

## 4. C1–C12 加密检查表汇总

| 条目 | 判定 | 说明 |
|---|---|---|
| C1 365 年化 | ✅ | 单点常量，annualisation_used 恒 calendar_365，1h→8760 |
| C2 资金费按事件 | ⚠️ | 口径正确（事件求和、周期无关 AC34 有对拍）；但双重计费 P0-1 + 8h 误判缺口 P1 |
| C3 spot/futures 维度 | ✅ | 三层护栏落地；boundary 用 symbol_classes 表达（非 market 字段，属实现差异） |
| C4 合约四元组 | ❌ | 全仓零实现（P0-5） |
| C5 USDT/USD 溢价 | ⚠️ | 计价货币未作为维度处理，收益含稳定币溢价（未列 P0，观察项） |
| C6 上市首日/下架 | ⚠️ | available_from 语义未闭合、E1019 暂缓（需确认意图） |
| C7 24/7 缺口 | ✅ | 靠时间戳+墙钟，缺口不静默填充；但 funding 8h 误判 P1 |
| C8 开放列集 | ❌ | 白名单按列名固定+注入映射硬编码（P0-6/P0-7） |
| C9 as-of 只 ffill | ✅ | 严格连续性、field_lags 对账、不 bfill |
| C10 判据不放松 | ✅ | dsr_min/fdr_max/n_trials 无路径可改写 |
| C11 流动性分层 | ✅ | boundary 双掩码+tradable⊆observable 类型层执法 |
| C12 embargo 按观测 | ⚠️ | certify 按 bar 正确，但 U9 报天/bar 单位不一致 P1 |

## 5. R1–R7 判据达成

- **R1 真数据跑通 ✅**：多次真跑证据（但 fresh clone 复现性被 P0-3 破坏）。
- **R2 结论可信 ❌**：P0-1/P0-2 双重污染唯一可发布 OOS 结论，必修。
- **R3 因子零代码扩展 ❌**：P0-4/5/6/7 四项叠加，当前只能挖"已注册的 3 流 + 12 内置因子"空间。
- **R4 搜索有效 ✅**：多算法/去重/确定性/计数实测成立，GP 自主发现 basis carry。
- **R5 单因子可归因 ⚠️**：六步流程已定，但工具链靠手工拼命令；ticket 双读者分叉 P1 影响归因一致性。
- **R6 迭代复用 ✅**：因子库闭环、db: 回灌已通。
- **R7 fail-closed ⚠️**：主体扎实，事件注入静默跳过（P0-7）破坏一致性。

---

## 6. 最小修复集建议（**本轮不实施，供讨论**）

**第一优先（恢复结论可信，1 个根因 + 1 个裁决）**
1. `funding_df=None`，V5 逐格计费作唯一来源；补"净收益只扣一次资金费"对拍测试 → 闭合 P0-1 及认证链污染。
2. 裁决 watch 口径：combine/certify 过滤 `statistics.tested is False`，或明确允许未检验候选并 **先改 `s0/04-capability.md:451` 再改代码** → 闭合 P0-2。

**第二优先（恢复因子可用性，R3）**
3. 列集白名单从"列名固定"改为"载荷列存在即引用"，事件注入映射由注册表驱动、缺列 fail-closed → 一次闭合 P0-6/P0-7。
4. `fields.json` 增合约四维 + 清算/跨所两流，供给层解析落列 → P0-4/P0-5。

**地基（随时可做，不依赖裁决）**
5. `git add -f tests/tests/test_v5_*.py` + 修 `.gitignore:145` + 黄金锚点入库 → P0-3。

**P1 中建议优先**：funding 8h 缺口误判（U3-P1-1）、ticket 双读者分叉（U3/U5-P1-1）、Stage A 全池 vs S4 分层（U8-1）。

---

## 7. 待你裁决（决定修复范围）

1. **修哪些**：仅第一优先（结论可信 P0-1/2）？还是含第二优先（R3 扩展 P0-4~7）+ 地基 P0-3？
2. **watch 口径**：按文档"watch 不进组合"收紧，还是承认未检验 watch 可进并改文档？（这条是设计裁决，不是 bug）
3. **内置因子**：文档对齐到 12 个，还是补齐 Alpha158 的 158 个内置池？
4. **R3 深度**：清算/跨所两流需要**新数据源**（外部供给，可能拿不到历史），是否纳入本轮？合约四维是纯解析工作，可立即做。
5. **验证链**：27 个测试 `git add -f` 是否本轮执行（涉及 git 纳管，非代码逻辑）？

审查阶段到此结束，**未修改任何代码**。等你裁决后进入修复阶段（开发-测试-review-验收）。
