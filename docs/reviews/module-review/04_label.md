# 模块审查 04 · `capability/ops/label.py`

> 审查日期：2026-11-18
> 依据：源码 340 行全文通读；依赖 `stats.py`（forward_return/TRANSFORMS/cs_neutralise/rolling_beta/nanmean）、`panel.py`（Panel/holdout_mask）、`params.py`（duration_bars）；设计 `s0/04-capability.md` §3.4（L349–368）、§3.6 IC 衰减（L413-419）、`06-evaluation.md` §4/§5.6/§7、`10-crypto-fit.md`；测试 `test_v5_label_panel.py:193-345`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Label 产出"未来会怎样"的度量（**不是因子**）。三条边界：

1. **未来值的算法只写在这里**：`forward_return` 用 `t+h` 价算标签合法；同一段未来价拿去算因子分数才是前视。故本模块输出只进 `observe` 的相关性计算、不进 `evaluate` 表达式。IC 衰减曲线需其他跨度标签时调 `build_raw_by_h`（同一个 `forward_raw`），不另写一份——两套实现必然分叉且都"看起来合理"。
2. **变换只用当根截面**：`cs_zscore`/`cs_rank` 的均值/标准差来自该时点，不是全期（全期标准化是最隐蔽的前视）。
3. **宽度不足不是零信号**：`min_width` 不满足则该时点 `valid_mask=False`，由下游统一消费；丢弃占比超 20%（I61）直接 `E3003`。

口径只有 `caliber` 一个来源：`params` 是编译期从 `caliber` 展开的快照（进 call_digest），运行期与 `policy_view` 逐项比对，不等即 `E1013`（C9：判据口径与回测口径必须同一值，分叉时停下、不以后者为准）。`horizon_bars` 是标签自身属性、必须输出，供下游把"每 h bar 收益"与"每 bar 成本"换算到同一时间单位。

**加密特化**：24/7 下 1 观测=1 天，`horizon` 用 `duration_bars` 按实测步长换算成 bar（整数/`5d`/`5h`），不靠 252/交易日日历；`entry=next_close/next_open` 滞后 1 bar（下一根才成交），避免用当根未收盘价；高波动下判据不被"加密波动大"放松（C10）——宽度丢弃 20% 是硬门槛。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 用 `venv/Scripts/python.exe` 调用 `capability.run(call)` 与直调 `build_raw_by_h` **实测打印**。复现按 `tests/tests/test_v5_label_panel.py:29-63` 的 `_panel/_label_call` 构造；临时脚本在系统临时目录，未改仓库。

### 例子的场景（围绕加密货币）

40 根 1d bar、6 标的，价格 `100·exp(cumsum(N(0,0.02)))`（加密高波动随机游走），字段 open/high/low/close/volume。`horizon=3`、`rebalance=3`、`entry=next_close`、`transform=none`、`neutralise=[]`。

### 输入：`Call`

```
capability = "label"   station = "Target"   impl = "forward_return_cs"   impl_version = "1"
params = {"horizon":3,"rebalance":3,"entry":"next_close","transform":"none","neutralise":[]}
policy_view = {"label": {"horizon":3,"transform":"none","neutralise":[],
                          "overlap_policy":None,"min_width":None,"width_policy":"keep"},
               "execution":{"entry":"next_close","rebalance":3}}
inputs = (InputRef("panel", panel.digest(), data=Panel(shape=(40,6), ...)),)
```

### 输出：`run(call)` 返回的 dict（10 个键，实测）

```
keys = ['label_ref','score_ref','score','raw','valid_mask','width_series',
        'dropped_bars','overlap_policy_used','effective_n','horizon_bars']
```

**实测值**：
```
label_ref    : sha256:301d8bf3795114a19...     score_ref : sha256:301d8bf3795114a19...
score.shape  : (40, 6)     raw.shape : (40, 6)     raw.dtype : float64
valid_mask.sum = 36        effective_n = 36       horizon_bars = 3
overlap_policy_used = "none"
width_series[:6] = [6, 6, 6, 6, 6, 6]
dropped_bars = {'count':4, 'by_reason':{'width_below_min':0,'not_estimable':4,
                                        'holdout':0,'holdout_leak':0}}
score[0,:] = [-0.02, 0.0185, -0.0415, -0.0057, -0.0328, 0.0051]
raw[0,:]   = [-0.02, 0.0185, -0.0415, -0.0057, -0.0328, 0.0051]   # transform=none 故与 score 相同
```
→ 尾部 4 根（horizon=3 + offset=1）标签不可估，`not_estimable=4`，不误算成宽度丢弃；`effective_n=36`（40−4）。`horizon_bars=3` 已输出。

> 注：`transform=none` 且 `width_policy=keep` 时 `label_ref==score_ref`（raw 与 score 逐位相同）。设计文档 `s0/04:368` 说"两个 ref 相等下游就无法复核变换定义"——恒等变换下确无可复核之物，此为预期、非 bug（详见 D2 L-3）。

### NEXT_BAR 滞后 1 bar（实测）

```
entry="close"       raw[0,0] = 0.02642306  = close[1]/close[0]-1   （当根收盘进场）
entry="next_close"  raw[0,0] = -0.04543603 = close[2]/close[1]-1   （下一根收盘进场，滞后 1 bar）
```
→ `next_close` 用 `t+1` 价进场、`t+1+h` 价出场（`:98-103,151-153`），不会用当根未收盘价（加密 24/7 关键）。

### 变换只用当根截面（实测）

```
cs_zscore: score[20] 截面和 = 0.0（当根标准化），NaN 数 = 0
cs_rank  : score[0] = [0.5, 1.0, 0.1667, 0.6667, 0.3333, 0.8333]（当根排序分位）
score is raw → False（变换产生新数组，raw 保留变换前）
```

### `forward_raw` / `build_raw_by_h` 与 IC 衰减共用（实测）

```
build_raw_by_h(panel, policy, [1,3,5,10]) keys = ['1','3','5','10']
h=3 与 label.raw 逐位相同 = True      ← 同一 forward_raw，口径跨度档逐位一致
各档有效点 = {'1':228,'3':216,'5':204,'10':174}   ← h 越大、触及保留区/尾部越多、掩码越紧
_holdout_touch([0,0,1,0,0,0], reach=3) = [1,1,0,0,0,0]
```
→ 证实"主标签与 IC 衰减共用同一实现"成立、且每跨度按自己的 `reach=offset+h` 重算保留区触达（h 大掩码更紧）。

### 保留区 / 宽度（实测）

```
panel holdout=(20,29), horizon=3 → effective_n=16
dropped_bars = {'count':14, 'by_reason':{'width_below_min':0,'not_estimable':1,
                                          'holdout':9,'holdout_leak':4}}
```
→ 四类互斥：末根 not_estimable=1、保留区 9、**窗口触及保留区 4**（t 的标签读到保留区价）、宽度 0；可消费时点 0..15 共 16 根。

### 失败输入 → 错误输出（fail-closed 路径，同样实测）

| 非法输入 | 输出（错误码 + 真实消息） |
|---|---|
| `rebalance=2 < horizon=10` 且无 `overlap_policy` | `E1022` "rebalance=2 < horizon=10 且未声明 overlap_policy" |
| 宽度不足丢弃 36/36（>20%） | `E3003` "宽度不足丢弃时点 36/36 超过 20%（I61）" |
| `entry="bogus"` | `E1004` "label.params.entry='bogus' 不在白名单 ['next_open','next_close','close']" |
| `neutralise=["beta"]` 但无 `neutralise_window` | `E1008` "neutralise 含 beta 但 caliber.label.neutralise_window 未给出" |
| `params.horizon=3` 但 `caliber.label.horizon=5` | `E1013` "horizon=3 与 caliber.label.horizon=5 不一致" |
| 有效样本 <2 | `E3003` "标签有效样本 <2，无法评估" |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| L-1 | **中性化回归量仅 `beta`/`log_dollar_volume` 两种、写死**：`_regressors`（`:106-140`）硬编码，加密常用中性化（如资金费敞口、期限、交易所虚拟变量）无法零代码加入。 | P2（扩展受限） | `label.py:106-140`；`params.json` neutralise enum 仅 2 值 | **需确认意图**：当前 2 种覆盖基本场景（06 §4），加新回归量属"加因子不改 Python"的边界议题。改一处 enum + 回归量注册即可，非本轮 bug。 |
| L-2 | **IC 衰减 `build_raw_by_h` 不做宽度掩码、不做重叠抽样**（设计已定为刻意取舍），代价是 `width_policy=drop` 时"h=口径跨度"档与主标签 `raw` 不再逐位相同。 | P2（设计取舍，已声明） | `label.py:174-213`（尤其 L186-189）；`s0/04:418` | **已确认符合设计**：注释与测试 precondition 均写明。默认 `keep` 下逐位相同（实测 True），不破坏主路径。 |
| L-3 | **`label_ref` 与 `score_ref` 在 `transform=none` 时相等**：实测均 `sha256:301d...`。文档 `s0/04:368` 强调"两个 ref 相等下游无法复核变换定义"，但恒等变换下 score==raw 是数学必然。 | P2（说明） | `label.py:247,248,328-330`；实测 | **需确认意图**：非 bug（恒等变换无可复核），建议文档补"transform=none 时两 ref 可相等"。若要求任何情况都异，需给 label_ref 加变换前缀——过度设计，不建议。 |
| L-4 | **`neutralise_window` 从 `policy_view.label` 读、不在 params**（正确），但若调用方直调 `build()` 绕过 `run()` 的 params 校验，把 `neutralise_window` 塞进 params 不会触发 E1003（E1003 在编排层）。 | P2（入口一致性） | `label.py:122-129,18-19`；测试 `test_label_neutralise_window_is_not_a_param` 用 run() 得 E1003 | **需确认**：经 `run()` 路径已 E1003 挡住（实测来自编排层），直调是内部入口，并入 U4 编译期校验，不单独修。 |

**多了什么**：无冗余。`raw` 与 `score` 双通道是必要的——`observe` 的多空序列必须用变换前 `raw`（量纲是收益），score 只进 IC（`:304-307`），避免量纲混拼。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| Qlib `Ref($close, h)/$close-1` 未来收益标签 + 截面正态化 | 借鉴（标签定义） | `forward_return` 方向"未来/进场"（`:151-153`），测试钉住下跌给负标签；拒绝写反（数值合理但符号相反） |
| 全期 z-score 标准化 | **拒绝** | 全期标准化是隐蔽前视（用了未来分布）；`cs_zscore` 只用当根截面（`:248`，stats.cs_zscore） |
| 一份实现同时产主标签与 IC 跨度标签 | 采纳 | `forward_raw` 唯一实现，`build`/`build_raw_by_h` 都调它（`:143-156,174-213`），实测 h=3 逐位相同；拒绝两套分叉实现 |
| 重叠标签当独立样本 | **拒绝** | `rebalance<horizon` 必须声明 `overlap_policy`（`:233-241` E1022）；`nw_adjusted` 保留样本、调整责任交证据层，`independent_sample` 等间隔抽样（`:285-294`） |
| 按交易日折算 embargo / 年化 | **拒绝** | `duration_bars` 按实测步长换算 bar（`:224-225`），24/7 下 1 观测=1 天，无 252/日历（C1/C12） |
| 宽度不足补 0 / 静默降级 | **拒绝** | 宽度不足 `valid_mask=False`、超 20% `E3003`（`:254-281`），I61 硬门槛，不因"加密波动大"放松（C10） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **未来值唯一实现共享**：`forward_raw` 被 `build`（`:244`）与 `build_raw_by_h`（`:203`）共用，`_regressors` 也共用（`:110-112` 注释）；实测 h=3 档与 `label.raw` 逐位相同。
2. **NEXT_BAR 滞后 1 bar**：`_entry_offset` close=0/next_*=1（`:98-103`），`next_open` 用 open 价（`:152`）；实测 next_close 用 t+1 进场。
3. **变换只用当根截面**：`TRANSFORMS[transform]` 对 raw 逐行作用（`:248`）；cs_zscore 实测截面和=0。
4. **口径镜像 E1013**：`_check_caliber` 逐项比对 params 与 policy_view（`:71-95`），实测 horizon 不一致 → E1013；分叉停下、不以后者为准（C9）。
5. **重叠标签 E1022**：`rebalance<horizon` 无策略即拒（`:233-241`），编译期 X4 + 本层双挡。
6. **宽度丢弃 20% 硬门槛**：只对"本可算标签却因宽度不足被丢"的时点计数（`:259-281`），尾部/保留区是结构性不可用、不进分母（实测 20 根 h=6 不误报）；实测 36/36 → E3003。
7. **保留区触达掩码**：`_holdout_touch` 按累积和判 `[t+1,t+reach]` 是否触保留区（`:159-171`），每跨度按自己 reach 重算（`:205`）；实测 holdout_leak=4、`_holdout_touch([0,0,1,0,0,0],3)=[1,1,0,0,0,0]`。
8. **`horizon_bars` 输出**：`:338-339`，实测=3，供成本/收益同单位换算。
9. **`raw` 变换前语义**：`raw_out` 是中性化后、变换前、同 sample 掩码（`:304-309`），供 observe 多空序列用，避免量纲混拼。
10. **只从 caliber 读 neutralise_window**：`:18-19,122-129`，不进 params。

**发现的问题**：
- 无 P0/P1 级代码 bug。L-1~L-4 均为 P2（扩展受限/设计取舍/说明/入口一致性）。测试覆盖充分（label 相关 ~18 个用例，`:193-345`，含逐位一致、保留区泄漏、宽度 E3003、重叠 E1022、beta 窗口、口径镜像）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:349-368` | 与实现逐条一致（口径镜像、三输出分离、变换、宽度） | 无需更新 |
| `s0/04-capability.md:368`（两 ref 不得相等） | 与"transform=none 时必然相等"冲突（L-3） | 补一句"恒等变换下 label_ref 可等于 score_ref；非恒等变换必须不同"（说明性） |
| `s0/04-capability.md:355`（参数表） | 参数表未列 `min_width`/`width_policy`/`overlap_policy`，但 schema/实现都有 | 补全参数表，与 `registry/params.json` 对齐（契约文档对齐） |
| `06-evaluation.md` §4/§7 | IC 衰减共用 forward_raw、宽度口径 | 保持；待中性化回归量扩展（L-1）后回头补 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 换 `horizon`/`rebalance`/`entry`/`transform` | ✅ 全支持 | 均为 enum/duration 参数，实测各路径可用，无需改代码 |
| 加一个新变换（如 cs_winsorize） | ⚠️ 半配置化 | 需在 `stats.TRANSFORMS` 加键 + params enum 加值，两处（`:248` + schema） |
| 加新中性化回归量（资金费敞口等） | ❌ 需改代码 | `_regressors` 硬编码 2 种（L-1）；映射 enum 与回归量函数，非纯配置 |
| IC 衰减加跨度 | ✅ 支持 | `build_raw_by_h(horizons)` 参数化（`:174`），实测 [1,3,5,10] 全产 |
| 新重叠策略 | ❌ 需改代码 | `overlap_policy` 两值写死分支（`:285-294`），加策略要改代码 |

---

## C1–C12 相关项判定

- **C1 365 年化**：✅ 强相关——`duration_bars` 按实测步长把 duration 换 bar（`:224-225`），24/7 无 252/日历依赖
- **C9 判据口径一致**：✅ 强相关——params vs caliber 逐项 E1013（`:71-95`）；判据口径与回测口径同一值
- **C10 判据不放松**：✅ 宽度丢弃 20% 硬门槛（MAX_DROP_RATIO，`:36,274-281`），不因加密波动放松
- **C12 观测=天/embargo**：✅ `duration_bars` 按 bar、不按交易日折算（`:224`）
- **C6 上市/下架**：不适用（标签层，标的可用性由 boundary 掩码决定）
- **C7 24/7 缺口**：⚠️ 标签按相邻 bar 算，跨洞前视由 align 的 P5 挡（见 02_align），本层信任面板连续性

## R1–R7 影响

- **R1 真数据跑通**：✅ 支撑（label 相关真跑全绿）
- **R2 结论可信**：✅ **强支撑**——未来值唯一实现、口径镜像、保留区触达、宽度门槛全部实测正确；这是"结论可信"的核心环节
- **R3 因子零代码扩展**：⚠️ 中性化回归量仅 2 种（L-1），新中性化因子需改代码；但主标签/跨度标签参数化良好
- **R5 单因子可归因**：✅ `raw` 变换前通道正确，observe 多空序列量纲正确
- **R7 fail-closed**：✅ 强支撑——E1022/E3003/E1004/E1008/E1013 全实测拒

---

## 结论

**本模块是全轮 R2（结论可信）的核心，代码质量高、无 P0/P1 bug，可确认通过。** 四条历史事故点均实测闭合：`forward_raw`/`build_raw_by_h` 共用同一实现（h=3 逐位相同）、`_holdout_touch` 每跨度重算、NEXT_BAR 滞后 1 bar、`horizon_bars` 已输出；rank/zscore/中性化只用当根截面、宽度不足显式丢弃且 20% 硬门槛。L-1~L-4 均为 P2（扩展受限/设计取舍/说明/入口一致性）。唯一需文档同步：`s0/04:368` 两 ref 恒等可相等、参数表补 min_width/width_policy/overlap_policy。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 5 个模块 `panel.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
