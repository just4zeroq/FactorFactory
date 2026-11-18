# 模块审查 03 · `v5/capability/ops/read.py`

> 审查日期：2026-11-18
> 依据：源码 375 行全文通读；依赖 `stream.py`（Pin/SupplyBatch/FieldStream）、`registry.py`（字段 available_lag / 参数 schema）、`timeaxis.py`（唯一区间实现，本模块自带 `_to_ns` 但时间窗口用 RFC3339）；设计 `s0/04-capability.md` §3.1（L176–205）、`10-crypto-fit.md` §5/§7.1/§8、`s0/01-data-contracts.md`；测试 `test_v5_read.py`（360 行）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Read 是八工位第 2 步"事实读取"，把执行器解析好的 `SupplyBatch`（版本事实 + 长表列）转成 `FieldStream`。**只读已发布版本**，没有"下载/补齐/最新"参数。四件事：

1. **版本与数据集一一对应**：一个 supply 输入承载一个版本；`datasets` 只能声明一个，且与 `pin.dataset` 一致；`symbols` 等于供给版本记录的标的集名。任一不符即拒，不"取交集继续跑"（交集会读一段更短的历史而无人察觉）。
2. **字段清单三者取交集**：请求字段必须同时满足「注册表登记 × 该字段声明的数据集 × 已发布版本的列」，否则 `E1014`；`available_lag` 只从注册表取，不从载荷取——载荷能改 lag 就等于把前视藏进数据层。
3. **窗口裁剪 + 频率校验**：`window ⊆ pin.coverage`，超出即 `E4002`（不截断）；声明频率必须与**供给版本基准步长**一致且所有间隔是其整数倍——频率是口径的一部分，把 1d 当 1h 会让所有窗口长度整体错 24 倍。
4. **长表 → (T,N) 面板**：时间轴取窗口内时点并集，标的取**版本宇宙**（不是窗口内出现过谁，否则 N 随窗口漂移）；`(symbol,time)` 重复即 `E4007`；`missing_ranges` 只报首末有效观测之间的内部缺口，上市前/退市后空段不算缺数据。

**加密特化**：24/7 无交易日历；频率只对 bar 数据集（ohlcv）校验，事件流（funding/OI/basis）间隔本身不是常量（实测 1h/4h/8h 混合、同合约会换周期），用频率去卡无解——这条与 `gate_b.BAR_DATASET` 是同一裁决；开放列集：`datasets` enum 已含 funding/open_interest/basis，为加密流接入留了契约位置。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下用 `venv/Scripts/python.exe` 调用 `v5.capability.run(call)` **实测打印**。复现按 `tests/v5/test_v5_read.py:43-105` 的 `_pin/_source_ref/_columns/_batch/_call` 构造；临时脚本在系统临时目录，未改仓库。

### 例子的场景（围绕加密货币）

30 天 1d bar（`2024-01-01…2024-01-30`）、3 个标的（S0/S1/S2）、字段 `close`/`volume`；`pin.version=0123456789abcdef`、`checksum=sha256(...)`、`end_closed=True`、`symbol_set="top3"`。

### 输入：`Call`

```
capability = "read"   station = "Panel"   impl = "published_only"   impl_version = "1"
params = {
  "datasets":  ["ohlcv"],
  "fields":    {"ohlcv": ["close","volume"]},
  "frequency": "1d",
  "window":    {"start":"2024-01-01T00:00:00.000000Z","end":"2024-01-30T00:00:00.000000Z"},
  "symbols":   "top3",
}
inputs = (InputRef("supply", batch.digest(), data=SupplyBatch(
    pin=Pin(...), symbol_set="top3", source_ref=Ref(kind="supply",digest=pin.checksum,...),
    columns={"symbol":[...],"timestamp":[RFC3339 Z...],"close":[...],"volume":[...]})),)
```

要点：read **不读盘**——载荷由执行器解析成 `SupplyBatch` 注入（`:52-66,16`），拿不到即 `E3003`。`source_ref.digest` 必须与 `pin.checksum` 一致（`E4004`），否则路径与版本指向不同内容。

### 输出：`run(call)` 返回的 dict（5 个键，实测）

```
keys = ['stream_refs','field_manifest','coverage','missing_ranges','stream']
```

**`stream`（FieldStream）实测**：
```
type = FieldStream   symbols = ('S0','S1','S2')   shape = (30,3)
times[0] = 1704067200000000000 (01-01)   times[-1] = 1706572800000000000 (01-30)
close[0,0]=100.0   close[29,2]=131.0   volume[5,1]=1001.0
end_closed = True
```
**`field_manifest`（实测）**：
```
{'close':  {'dataset':'ohlcv','version':'0123456789abcdef','unit':'quote','available_lag':0},
 'volume': {'dataset':'ohlcv','version':'0123456789abcdef','unit':'base',  'available_lag':0}}
```
→ `available_lag/unit` 来自注册表，非载荷。
**`coverage`（实测）**：`{'start':1704067200000000000,'end':1706572800000000000,'bars':30,'symbols':3}`
**`missing_ranges`（实测）**：`[]`
**`stream_refs`（实测）**：`['supply']`（provenance，指**输入**，不是产物引用；产物身份是 `stream.digest()`）

### 窗口裁剪与缺口（实测）

```
window 01-11..01-16 → shape (6,3)，times[0]=1704931200000000000(01-11)，times[-1]=1705363200000000000(01-16)
删掉 (S0,01-06) 制造内部缺口 → missing_ranges =
    [{'symbol':'S0','start':1704499200000000000,'end':1704499200000000000,'bars':1}]
    close[5,0] = nan                       （缺失用 NaN，不填充）
S2 从 day10 才有数据（上市前空段） → missing_ranges = []（结构性空段不算缺数据）
    close[0,2] = nan                       （用 NaN 表示"还不存在"，不报缺口）
```

### 失败输入 → 错误输出（fail-closed 路径，同样实测）

| 非法输入 | 输出（错误码 + 真实消息） |
|---|---|
| `window.end` 超 coverage | `E4002` "window 超出供给版本 coverage" |
| `fields.ohlcv=["funding_rate"]`（字段属 funding 数据集） | `E1014` "字段 'funding_rate' 不属于数据集 'ohlcv'（注册表：['funding']）" |
| `fields.ohlcv=["bogus"]`（未登记） | `E1014` "字段 'bogus' 未在字段注册表登记" |
| `symbols="top9"`（≠版本标的集） | `E4002` "标的集 'top9' 与供给版本记录的 'top3' 不一致" |
| `frequency="1h"`（数据实为 1d） | `E4002` "基准步长 86400000000000ns 与声明 frequency='1h'（3600000000000ns）不符" |
| `window` 倒置（start>end） | `E4002` "window.start 晚于 window.end" |
| 长表重复 `(S0, 01-04)` | `E4007` "(S0, 1704326400000000000) 在版本 ohlcv/0123456789abcdef 里出现两次" |
| `datasets=["funding"]`（版本是 ohlcv） | `E3001` "请求的数据集 'funding' 与供给版本的 'ohlcv' 不符" |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| R-1 | **五个加密流（funding/OI/basis/清算/跨所价差）未真正接进面板**：`datasets` enum 已含 funding/open_interest/basis，注册表也有 `funding_rate/open_interest/basis` 字段，但本模块**只做单版本长表→面板、没有 as-of 对齐到 bar 网格**。要把事件流对齐到 ohlcv 的 bar 时间轴（`asof_rule=bar_close, fill=ffill_only`），当前 read 无此能力——这是 R3 最大 alpha 缺口（100% 资金费数据未进搜索）。 | **P0（R3 缺口，全 U6 共同）** | `read.py:45,162-193,304-364`；`registry/fields.json`（有 funding 等）；`10-crypto-fit.md:118,168,177-193` | **已确认为缺口，但落点不在 read 单模块**：事件流接入涉及供给层分区 + 面板列集 + as-of 对齐，是全 U6 前置。read 能读单个事件数据集到其原生网格，但"多流对齐到 bar"无实现。 |
| R-2 | **频率校验的已知盲区**：`_check_frequency` 用"基准步长==声明值 + 所有间隔是整数倍"，注释自承"拦不住 1h 数据里混 4h 间隔"（4h 是 1h 整数倍，与连续 3 根缺失同形）。 | P2（文档已声明边界） | `read.py:162-193`（尤其 L169-172） | **已确认符合设计**：作者写明能拦/不能拦，不伪装成更强检查。加密数据混合频率靠供给层 as-of 处理，非本层能补。 |
| R-3 | **`_FREQ_NS` 硬编码 6 档**，与 `params.json.read.frequency` enum 是两份；新增频率（如 1s/1m 细分）要改两处。 | P2（重复真相） | `read.py:34-41`；`registry/params.json` read.frequency | **需确认**：与 G3/G5 单一真相原则略悖，但 enum 与步长强绑定，拆开反而易漂移。建议加测试钉住两处一致，非本轮代码 bug。 |
| R-4 | **时间戳 sanity 的措辞强于实际能力**：`_to_ns`/`_checked`（及 `timeaxis.check_ns`）用 1900–2100 纳秒区间拦"毫秒/微秒/秒当纳秒"。实测：2024 的微秒值 `1.7e15`、毫秒值 `1.7e12` 当纳秒落在 1970 年，**仍在 1900–2100 区间内、不被 `_checked` 拒绝**。真正拦截靠：① `run()` 的 `validate_params` 要求 window 是 RFC3339 字符串（裸 int 立即 E1004，实测）；② window 超 coverage 检查。sanity 只拦极端值（如 5e18/-3e18，实测 E4002）。 | P2（说明/纵深防御） | `read.py:69-126`；实测 `_checked(1.7e15)` 通过、`validate_params` 对裸 int → E1004 | **需确认意图**：端到端 `run()` 路径已被 RFC3339 校验闭合，不构成前视/静默错误；但 docstring 说"毫秒/微秒/秒会落在区间之外"对 1970 后的日期不成立。建议改 docstring 措辞（属 s0/01 与时限模块共用），非本模块代码 bug。 |

**多了什么**：无冗余。`stream_refs` 明确是 provenance 而非产物引用（`read.py:366-369` 注释），避免审计误认，设计正确。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 版本目录 / content-address（`pin.checksum` ↔ `source_ref.digest`） | 采纳 | 一条指路径、一条指版本，必须同一内容；不一致 `E4004`（`:321-327`），契合方案 B 无 manifest 目录 |
| 字段清单"注册表 × 数据集 × 版本列"三者交集 | 采纳 | 取并集会推迟到 align/evaluate 才炸（`:253-283`）；且 `available_lag` 只从注册表取，堵住"载荷改 lag = 前视"（`:280-282`） |
| polars/DuckDB 直接查询 parquet | **拒绝（本层）** | AC31：能力层不 import pyarrow/pathlib；打开哪个文件留在事实层，read 只拿已校验的只读表（`:16,191-195`） |
| "window 超覆盖取交集继续跑" | **拒绝** | 交集会读一段更短的历史而无人察觉（`:6-8,294-302`） |
| 频率用"中位数相等"校验 | **拒绝（已修）** | 3 根 bar 有洞中位数就变 2 天、把 1d 判死；1 根时中位数不存在、任何频率都过。改用"版本最小间隔 × 整数倍"（`:147-193`） |
| pandas-market-calendars / 交易日历 | **拒绝** | 24/7 无日历；频率只对 ohlcv 校验，事件流不卡（`_BAR_DATASET`，`:42-45`） |
| 事件流 as-of 对齐（qweave/polars join-asof） | **应借鉴（当前缺）** | 加密资金费/OI/basis 必须按 `bar_close` as-of、只 ffill 不 bfill（C9）；当前 read 无此能力（R-1） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **单版本一一对应**：`datasets` 只能一个（`:207-215`），`pin.dataset` 不符 `E3001`（`:217-223`），`fields` 键 ⊈ datasets `E1004`（`:224-231`），重复字段 `E1004`（`:237-241`）。
2. **字段清单三交集 + lag 来源**：未登记/不属该数据集/不在版本列全部 `E1014`（`:253-275`）；`available_lag` 只取注册表（`:280-282`）。实测三类 E1014 全部真失败。
3. **标的集取版本宇宙**：`symbols = batch.symbols`（`:330`），不从窗口行推；注释讲清"窗口推 N 会漂移，整窗无行靠 not_listed/delisted 表达"（`:324-329`）——契合 C6/C11。
4. **窗口 fail-closed**：`w_lo>w_hi`（`:289-293`）、超 coverage（`:294-302`）、无观测（`:315-322`）全拒；实测。
5. **频率是版本属性**：`_version_step` 用全版本最小间隔（`:147-159`），不用窗口中位数；`_check_frequency` 只对 ohlcv（`:174-175`），事件流跳过——与 `gate_b` 同裁决。
6. **重复 (标的,时点) 整批失败**：`seen` 集合（`:337-346`），实测 E4007；不"保留先到的那条"。
7. **内部缺口 vs 结构性空段**：`interior_missing_runs`（`stream.py:70-112`）只报首末有效之间、所有字段同时缺失的连续行（实测内部缺口报 1 段、上市前空段不报），契合文档 L205。
8. **NaN 不填充**：长表→面板用 `np.full(nan)` 再按行填值（`:352-355`），无 ffill/bfill。
9. **列值类型门禁**：`_numeric` 把 None→NaN、布尔 `E3003`、非数值 `E3003`（`:129-144`），布尔不静默变 1.0。

**发现的问题**：
- 无 P0/P1 级**本模块代码 bug**。R-1 是 R3 全局最大缺口但落点在 U6 整体（供给分区 + 列集 + as-of），非 read 单独可补；R-2/R-3/R-4 均为 P2（已声明边界/重复真相/纵深防御）。测试覆盖充分（read 相关用例见 `test_v5_read.py`，含窗口/字段/频率/重复/缺口/版本身份）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md:176-205` | 与实现逐条一致（版本、字段、窗口、频率、缺口） | 无需更新 |
| `s0/04-capability.md:202` | "frequency 必须与时间轴实测步长（中位数）一致" | **与实现不符**：实现用"版本最小间隔 × 整数倍"（`:147-193`），已明确拒绝中位数。文档 L202 应同步改为"基准步长=版本最小间隔，所有间隔为其整数倍"，否则误导（口径说明，非契约变更） |
| `10-crypto-fit.md` §7.1/§8 | 事件流 as-of 对齐目标态已写明 | 待 R-1 落地（多流接进面板）后回头补 `s0/04:176-205` 的"多数据集/多输入"语义；当前 read 契约是"一个输入一个版本" |
| `s0/01-data-contracts.md` / `timeaxis.py` 共用措辞 | "毫秒/微秒/秒会落区间外"对 1970 后日期不成立（R-4） | 改措辞为"极端非纳秒值（落在 1900 前/2100 后）被拦；1970 后的 ms/us 值靠 RFC3339 校验与 coverage 检查拦截"（说明性） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一个已登记的 ohlcv 字段（vwap） | ✅ 支持 | 字段清单来自 params，只要注册表+版本列都有即可，不改代码（`:253-283`） |
| 读单个事件数据集（funding_rate 到其原生网格） | ✅ 半支持 | `datasets=["funding"]`+对应版本可跑；但 frequency 校验对非 ohlcv 跳过，事件网格本身不规则，需供给层给好长表 |
| **多流 as-of 对齐到 bar 网格（ffill-only）** | ❌ 不支持 | 无 as-of  join 能力；这是 C8/C9 与 R3 的前置（R-1），全 U6 议题 |
| 频率加档（1s/2h） | ⚠️ 半配置化 | `_FREQ_NS` 与 params.json enum 两处都要改（R-3） |
| 标的集在运行中变化（新上市进池） | ❌ 不支持 | 标的集绑版本宇宙（`:330`）；新标的需新版本，由 boundary 的 not_listed 表达，非 read 动态扩列 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不算年化）
- **C2 资金费按事件求和**：⚠️ 相关——read 不按固定周期处理事件（frequency 对非 ohlcv 跳过，`:174-175`），方向正确；但资金费尚未接进面板（R-1）
- **C3 spot/futures 维度**：⚠️ `datasets` enum 区分 funding（期货）与 ohlcv，但无 `market` 字段独立断言（能力层无第二道护栏，同 boundary B-1）
- **C4 合约四元组**：❌ 无（全局缺口，本模块按 symbol 字符串读，无 expiry/settle）
- **C5 计价货币**：不适用
- **C6 上市/下架**：✅ 标的集取版本宇宙、缺口只报内部（`:324-330`），结构性空段交 boundary 的 not_listed/delisted
- **C7 24/7 缺口**：✅ 无日历；频率只对 ohlcv、允许整数倍缺口（`:185-193`）
- **C8 开放列集**：⚠️ 字段清单开放（注册表驱动），但事件流未接进面板（R-1，最大缺口）
- **C9 as-of ffill-only**：❌ 本层不填充、无 as-of 对齐能力（R-1）；lag 来源正确（防前视源头）
- **C11 流动性分层**：不适用（交 boundary）

## R1–R7 影响

- **R1 真数据跑通**：✅ 支撑（read 相关真跑多次全绿）
- **R2 结论可信**：✅ 强支撑——字段/lag/窗口/频率/重复全部 fail-closed，实测覆盖
- **R3 因子零代码扩展**：❌ **最大负向**——事件流未接进面板（R-1，P0），列集虽开放但"多流 as-of 对齐"无实现；这是 U6 全轮重点
- **R5 单因子可归因**：中性
- **R7 fail-closed**：✅ 强支撑——八类非法输入全部明确拒（实测）

---

## 结论

**本模块核心读取逻辑正确、无自身 P0/P1 代码 bug，可确认通过；但承载 R3 最大缺口。** R-1（五加密流未接进面板/as-of 对齐缺失）是 P0 级、全 U6 共同前置，read 能读单事件版本但无多流对齐能力，需与供给层/面板列集一同解决，非本模块单独可补；R-2 频率已知盲区、R-3 重复真相、R-4 sanity 措辞均为 P2。另发现 `s0/04:202` 文档"中位数"与实现"版本最小间隔×整数倍"不符，建议文档同步（审查只读、本轮未改）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 4 个模块 `label.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
