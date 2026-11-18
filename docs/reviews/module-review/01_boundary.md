# 模块审查 01 · `capability/ops/boundary.py`

> 审查日期：2026-11-18
> 依据：源码 340 行全文通读；依赖 `stream.py`（REASON_CODES/OBSERVABLE_CODES/Scope）、`timeaxis.py`（resolve_interval）、`stats.py`（trailing_mean/coverage）；设计 `s0/04-capability.md` §3.2（L207–280）、`10-crypto-fit.md` §7；测试 `test_v5_boundary_align.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Boundary 是八工位第 1 步"范围说明书"，回答三件事：
1. **时间轴解析**：把 `mine/oos/holdout` 区间解析成精确时点，重叠、中间有洞、保留区相交必须拒绝；`holdout=reserved` 按 `mine∪oos` 补集标记（不看保留区的值）。
2. **两张掩码**：`observable_mask`（能否进统计/中性化）与 `tradable_mask`（能否下权重）分开。未收盘 bar 是典型：可观测、不可交易。
3. **逐格理由码 + 优先级**：一格一个码，先命中先记（否则同一数据两次跑码值漂移 → Scope 指纹漂移）；结构性原因排在质量原因之前。
4. **流动性分层 `tiers`**：按 trailing 报价成交额（`close×volume`）在**入池成员**上取分位；分 `pit_dynamic` / `static`。
5. **边界止于事实层**：不读盘、不判显著，只消费编排层注入的 Pin/FieldStream。

**加密特化**：24/7 无交易日历；未收盘靠供给版本末端事实（`pin.end_closed`）；稳定币/杠杆代币单列（不进可交易）；low_liquidity 用成交额分层（C11）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下矩阵是用仓库 `venv/Scripts/python.exe` 调用 `capability.run(call)` **实际打印**的结果，不是推演。
> 复现方式：按测试 `tests/tests/test_v5_boundary_align.py:55-100` 的 `_stream/_pin/_boundary_call` 构造，参数见下。

### 例子的场景（围绕加密货币）

5 个标的、8 根 1d bar：

| 标的 | close | volume（每根） | 报价成交额 | symbol_classes |
|---|---|---|---|---|
| BTC | 100 | 1e3 | 1e5 | spot |
| ETH | 100 | 2e3 | 2e5 | spot |
| SOL | 100 | 3e3 | 3e5 | perp |
| BNB | 100 | 4e3 | 4e5 | perp |
| USDT | 100 | 5e3 | 5e5 | **stable** |

- 时间：`2024-01-01 … 2024-01-08`（24/7，无交易日历）。
- 时间轴：`mine=01-02..01-05`，`oos=01-06..01-08`，`holdout=reserved`。
- **供给末端未收盘**：`pin.end_closed=False` 且 `stream.end_closed=False`（两者必须一致，否则 E3003）。
- 注意 USDT 成交额最高但为稳定币——用来演示"流动性排序"与"可交易性"是两个独立维度。

### 输入：`Call`

```
capability   = "boundary"
station      = "Boundary"
impl         = "pit_dynamic_topk"
impl_version = "1"

params = {
    "k": 3,                       # top-k：每根只留成交额前 3
    "tier_window": 1,             # trailing ADV 窗口（bar 数）
    "min_coverage": 0.5,          # 回看覆盖率门禁
    "mask_policy": "pit_dynamic", # 分层逐 bar 取分位（另一个值 static）
    "time_axis": {
        "mine":    "2024-01-02..2024-01-05",
        "oos":     "2024-01-06..2024-01-08",
        "holdout": "reserved",
    },
}

inputs = (
  InputRef("pin",           <digest>, data=Pin(
        dataset="ohlcv", version="v1", checksum=<digest>,
        coverage=(t[0], t[-1]+1d), symbols_digest="s0", end_closed=False)),
  InputRef("field_stream",  <digest>, data=FieldStream(
        times=t[8], symbols=("BTC","ETH","SOL","BNB","USDT"),
        fields={"close": (8,5) ndarray, "volume": (8,5) ndarray},
        manifest={"close": {"dataset":"ohlcv","version":"v1","unit":"quote",
                            "available_lag":0}, ...},
        end_closed=False, symbol_classes={"USDT":"stable"})),
)
```

要点：Boundary **不读盘**——`pin` 与 `field_stream` 都由执行器解析好注入（`boundary.py:30-58`）；拿不到就 E3003。

### 输出：`run(call)` 返回的 dict（7 个键）

```python
{
  "observable_mask": ndarray[bool]    (8,5),
  "tradable_mask":   ndarray[bool]    (8,5),
  "tiers":           ndarray[int8]    (8,5),   # -1 = 未入池
  "reason_codes":    ndarray[int8]    (8,5),
  "reason_code_legend": {"valid":0,"reserved":1,"not_listed":2,"delisted":3,
                         "missing_data":4,"unclosed":5,"low_liquidity":6,
                         "stable_or_leveraged":7},
  "axis_resolved": {"mine":{...},"oos":{...},"holdout":"reserved"},
  "scope": Scope(...),   # 供 align 消费的不可变对象
}
```

**`axis_resolved`**（区间被解析成精确时点）：

```
mine    : 2024-01-02 .. 2024-01-05
oos     : 2024-01-06 .. 2024-01-08
holdout : reserved        # 按 mine∪oos 的补集标记，不看保留区的值
```

**`reason_codes`**（一格一码，真实输出）：

```
  date          BTC          ETH          SOL          BNB         USDT
  2024-01-01 reserved    reserved    reserved    reserved    reserved
  2024-01-02 low_liquidity low_liquidity valid        valid       stable_or_leveraged
  2024-01-03 low_liquidity low_liquidity valid        valid       stable_or_leveraged
  2024-01-04 low_liquidity low_liquidity valid        valid       stable_or_leveraged
  2024-01-05 low_liquidity low_liquidity valid        valid       stable_or_leveraged
  2024-01-06 low_liquidity low_liquidity valid        valid       stable_or_leveraged
  2024-01-07 low_liquidity low_liquidity valid        valid       stable_or_leveraged
  2024-01-08 unclosed     unclosed     unclosed     unclosed     stable_or_leveraged
```

**`tradable_mask`（T=能下权重）**：

```
  2024-01-01  .  .  .  .  .
  2024-01-02  .  .  T  T  .
  2024-01-03  .  .  T  T  .
  2024-01-04  .  .  T  T  .
  2024-01-05  .  .  T  T  .
  2024-01-06  .  .  T  T  .
  2024-01-07  .  .  T  T  .
  2024-01-08  .  .  .  .  .
```

**`observable_mask`（T=能进统计）**：运行窗口内（01-02…01-08）**全 T**——包含 low_liquidity、unclosed、stable；窗口外（01-01）全 `.`。

**`tiers`（-1=未入池）**：

```
  2024-01-01  -1 -1 -1 -1 -1
  2024-01-02  -1 -1  0  1  2     # 分位只在入池成员 SOL/BNB/USDT 上取
  ...（01-02..01-08 同）...
```

### 逐格解读（这张输出在讲什么）

| 格子 | 码 | 为什么 |
|---|---|---|
| 01-01 全列 | `reserved` | 在 `mine∪oos` 之外，保留区是补集（`boundary.py:288`） |
| 01-02 BTC/ETH | `low_liquidity` | 成交额 1e5/2e5 未进 top-3（`:279,315`），但**仍可观测** |
| 01-02 SOL/BNB | `valid` → `tradable=T` | 进 top-3 且非特殊标的，唯一能下权重的格 |
| 01-02 USDT | `stable_or_leveraged` | 结构性原因先于数据质量判定（`:294-300`）；成交额最高也不给权重 |
| 01-08 BTC/ETH/SOL/BNB | `unclosed` | 供给末端未收盘 → **可观测、不可交易**（`:310-313`） |
| 01-08 USDT | `stable_or_leveraged` | 先命中先记：stable 排在 unclosed 之前，码值不因窗口末端漂移（`:284-286`） |
| tiers 的 USDT=2 | 入池分层 | 分层池 = top-k（按成交额），与可交易性是两维；下游靠 `tradable_mask` 挡住，tier 值留着无副作用 |

### 失败输入 → 错误输出（fail-closed 路径，同样实测）

只改 `time_axis`、其余输入不变：

| 非法输入 | 输出 |
|---|---|
| `mine=01-02..01-06, oos=01-05..01-08`（重叠） | `E4002` "mine 与 oos 区间重叠" |
| `mine=01-02..01-04, oos=01-06..01-08`（留洞） | `E4002` "mine 与 oos 之间不得有洞" |
| `holdout=01-03..01-04`（与 mine 相交） | `E4002` "保留区与 mine/oos 相交" |
| `holdout="   "`（空串） | `E4002` "holdout 不得为空串" |

另：`stream.end_closed=True` 而 `pin.end_closed=False` → `E3003`；`k=6 > n_col=5` → `E1004`；缺 `volume` 字段 → `E1004`。这些都是"拒绝并返回明确错误"，不是"少选几个/静默降级"。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| B-1 | **市场维度不是 C3 的 `market∈{spot,futures}`**，而是 `symbol_classes∈{spot,perp,stable,leveraged}`（`stream.py:67`）。结果：spot 计划若混入 funding 字段，boundary 不会因"spot 无资金费"拒绝。`market` 维度在供给层 `fetch.py:169` 有护栏，能力层没有第二道断言。 | P2 | `boundary.py:294-300`；`stream.py:67`；对照 C3 | **需确认意图**：设计可能把 market 责任放供给层。但文档 `10-crypto-fit.md` §7.2 的合约维度（perp/dated）在能力层确实缺（见 U6/P0-2 合约四维）。 |
| B-2 | **`pit=False`(static) 用整个 run 区间均值定档**：`_tiers` 的 `rows=np.flatnonzero(run)`（`boundary.py:162-177`）覆盖 `mine∪oos` 全期，静态档位由未来 oos 数据决定。文档 `s0/04 §3.2` L270 明确写 static 就是"整个 mine∪oos 区间均值"——**这是文档定义的语义，不是实现 bug**。 | P2（文档语义确认） | `boundary.py:162-177`；`s0/04-capability.md:270` | **已确认符合设计**。但静态档位含未来数据这一点，应在文档里明说"这是构造诊断用、不是可交易信号的前视"——属说明缺口非代码。 |
| B-3 | **`params.json` 无 `boundary` 参数定义**（脚本读键为空 `{}`）；boundary 却从 `params` 读 `k/tier_window/min_coverage/mask_policy/tier_edges/time_axis`。若校验器确实不校 boundary 参数，则类型/范围错误只能在运行期 E1004 暴露，非编译期。 | P2（待核实校验路径） | 源码 `boundary.py:218,226,227,244,245,320`；`params.json` 键为空 | **需确认**：编译期参数校验在 U4 单元核（U4-P1-1 编译期校验仅注释占位）。此项并入那条，不在本模块单独修。 |
| B-4 | **`k > n_col` 用 E1004**（`boundary.py:219-223`），但 k 是构造参数不是口径；E1004 是"口径不合法"。语义上更像 E1002（参数非法）。 | P2（语义） | `boundary.py:219-223` | 低优先，错误码语义一致性问题，不影响结果。 |

**多了什么**：无明显冗余。`_MAX_TIER_LEVELS=8` 是合理上限守卫。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| ccxt `markets.contractType/settle` 做合约分类 | **应借鉴（当前缺）** | 现在只有 `symbol_classes` 4 值，无 `expiry`/dated 合约；期限结构因子不可做（C4，全局 P0-5，非本模块单独能补） |
| polars / numpy 向量化分层与 top-k | 采纳 | `boundary.py:180-190` 用 `np.argsort(kind="stable")`，同额按列序稳定 → 可复现 |
| trailing ADV 做流动性分层（成交额分位） | 采纳 | 加密无市值/行业，成交额是唯一可靠的流动性代理；`close*volume`（`:260`）正确 |
| pandas-market-calendars 判"未收盘/交易日" | **拒绝** | 24/7 无日历；本模块用 `pin.end_closed` 事实（`:201,310`），方向正确（C7/I36） |
| 全期覆盖率定逐格可用性 | **拒绝（已修）** | `trailing_coverage` 回看判（`:273`），避免后半段才出现的标的抹掉前半段有效格——这是本层唯一前视陷阱，已闭合（`:268-270` 注释） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **两张掩码分离**：`observable = isin(codes, OBSERVABLE_CODES)`、tradable=`codes==0`（`:318-319`）；OBSERVABLE_CODES 由 REASON_CODES 派生（`stream.py:63-65`），新增理由码自动跟上（G9）。`Scope.__post_init__` 强制 `tradable⊆observable`（`stream.py:589-605`）。
2. **理由码优先级先命中先记**：`_mark` 用 `codes[mask & (codes==0)]`（`:284-286`），执行序 reserved→not_listed→delisted→stable→missing→unclosed→low_liquidity（`:288-316`），与文档 `s0/04:258-259` 逐字一致。
3. **时间轴重叠/洞/保留区相交全 fail-closed**：重叠 E4002（`:78-82`）、洞 E4002（`:83-93`）、保留区相交 E4002（`:111-115`）、空串 holdout E4002（`:102-108`）。复用 `timeaxis.resolve_interval`，拿回 1900–2100 纳秒 sanity。
4. **未收盘只在供给末端**：`pin.end_closed is False and run[-1]`（`:310`），不是窗口末端；且 stream/pin 的 end_closed 不一致即 E3003（`:201-209`）——防"boundary 说未收盘、align 说已收盘"的半成品 bar 静默留存。
5. **流时间轴不得超 pin.coverage**（`:210-216`）；`k>n_col` 拒（`:219`）。
6. **top-k 稳定可复现**：`np.argsort(-row[idx], kind="stable")`（`:188`），同额按列序。
7. **分层在入池成员上取分位**（`:155,165-174`），契合文档 L272。
8. **min_coverage 回看**：`trailing_coverage`（`:273`），低于阈值退出 top-k/分层并记 missing_data（`:277,304-305`），契合 L274-280。
9. **无 ffill/bfill**：本模块不填充任何缺失。

**发现的问题**：
- 见 D2 表 B-1（market 维度）、B-3（参数校验）。**无 P0/P1 级代码 bug**。测试覆盖充分（19 个 boundary 相关用例，含未收盘可观测不可交易、top-k、tiers 静态/动态、min_coverage、时间轴拒绝，见 `test_v5_boundary_align.py:124-263`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:207-280` | 与实现逐条一致（掩码、理由码序、tiers、min_coverage） | 无需更新 |
| `s0/04-capability.md:270` | static 语义"整个 mine∪oos 均值" | 补一句"静态档含未来区间，仅作构造诊断用"，消解 B-2 的前视疑虑（说明性，非契约变更） |
| `10-crypto-fit.md` §7.2 | 合约四维身份 `underlying/contract_type/expiry/settle` 列为加密缺口 | 本模块目前的 `symbol_classes` 不满足；待全局 P0-5 落地后回头更新本节与 `stream.py:67` 的码表 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增理由码（如 `expired`） | ⚠️ 半配置化 | 需在 `REASON_CODES` 加码 + `_UNOBSERVABLE/_NOT_TRADABLE` 归类（stream.py），不是纯配置；但 observable 掩码自动派生，改一处即可 |
| 新增标的类型（稳定币/杠杆代币之外） | ⚠️ 半配置化 | `SYMBOL_CLASSES` 加值 + 供给层标 `symbol_classes`；boundary 无需改（`:294-300` 通用判定） |
| **加入 `market=spot/futures` 维度护栏** | ❌ 不支持零代码 | 需改 boundary + Scope + stream 码表；当前只在供给层有护栏。属 R3/C3 缺口的一部分 |
| **合约四维身份（期限结构分组）** | ❌ 不支持 | 全仓零实现（全局 P0-5）；boundary 的 tiers 分组键无法用 expiry/settle |
| 新增分层策略（如波动率分层） | ❌ 需改代码 | 分层量写死 `close*volume`（`:260`），无策略注册点 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不算年化）；`step=median(diff(times))`（`:225`）按观测推，无 252 依赖 ✅
- **C3 spot/futures 维度**：⚠️ 用 symbol_classes 替代，能力层无 market 第二道断言（B-1）
- **C4 合约四元组**：❌ 无（全局缺口，本模块非落点）
- **C6 上市/下架**：✅ `not_listed`/`delisted` 由 alive 首末有效观测定（`:263-266,289-290`）
- **C7 24/7 缺口**：✅ 未收盘靠 `pin.end_closed`（供给事实）+ 时间轴洞 fail-closed（`:83-93`）；不靠日历
- **C11 流动性分层**：✅ top-k + tiers 用成交额，双掩码分离

## R1–R7 影响

- **R1 真数据跑通**：中性（边界构建正确，多次真跑已用）
- **R2 结论可信**：✅ 强支撑（前视陷阱全部闭合）
- **R3 因子零代码扩展**：⚠️ 负向——合约四维（C4）与市场维度（C3）缺失，限制可挖因子空间（全局 P0，非本模块单独可补）
- **R5 单因子可归因**：✅ tiers 分层正确，分层回测口径可靠
- **R7 fail-closed**：✅ 强支撑（重叠/洞/无字段/超覆盖/未收盘不一致全拒）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 三处 P2 均为"需确认意图"或"并入其他单元"：B-1 市场维度（能力层 vs 供给层责任划分，且合约四维是全局 P0-5）、B-2 静态档前视（文档已定义该语义，补说明即可）、B-3 参数校验（并入 U4 编译期校验 P1-1）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 2 个模块 `align.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
