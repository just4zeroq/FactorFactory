# 模块审查 32 · `upstream/store.py`

> 审查日期：2026-11-18
> 依据：源码 657 行全文通读；依赖 `contracts`（Digest/Pin/E1004/E1014/E3003/E4002/E4003/E4007）、`pandas/pyarrow`。设计 `19-supply.md` §2（方案 B）、§3（时间语义）；测试 `test_v5_supply_store.py` 全文（21 用例）。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `fm_review_26_33/probe1_store.py`（路径/append-only/checksum/gaps）与 `probe2_storage.py` A 段（funding 8h 误判）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

store 是**方案 B 供给事实的文件式存储**，回答"一份数据集怎么落盘、怎么读、身份怎么现算"。核心是与 S2 事务协议的区别一句话：**没有 manifest**（`:1-3`）。

1. **方案 B 路径**：`<root>/<dataset>/<exchange>/<market>/<SYMBOL>[-tf].parquet`。ohlcv 带 tf（`1d`/`4h` 是两份数据），funding/OI/basis 不带 tf（周期会变，写进文件名是假标签 I34，`:178-209`）。无 manifest / 版本目录 / 别名表。
2. **版本=checksum[:16]**：`version` 是内容摘要前 16 位，仍是内容寻址但无对应目录（`:423-426`）；coverage/interval_ns/gaps 全部读时现算（`:617-656`）。
3. **append-only**：同 symbol+timestamp 旧值不覆盖，冲突只计数 `revised`；改历史须显式 `revise=True`（`:342-401`）。
4. **市场维度独立**：`market∈{spot,futures}` 独立于 exchange；`MARKET_DATASETS` 是采集与编译共用的唯一真相（`:45-57`）；三层护栏的第一层在这里（路径校验 + 市场×数据集 + 市场×符号）。
5. **安全与原子**：路径段白名单 `[A-Za-z0-9][A-Za-z0-9_.-]*` + `fullmatch`（`:36-38,81-99`）；写盘只经 `_atomic_write`（同盘 tmp + os.replace，`:311-325`）。
6. **UTC 严格**：timestamp 必须 UTC aware，拒 naive、拒本地时间（`:212-230,287-296`）。

**不变量**：一个文件只装一个标的（`:256-285`）；列清单是契约（多写一列 checksum 变而无从解释，`:59-65`）；`_normalise` 读写共用一处避免两套口径（`:251-308`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> venv Python 实测（pandas 3.1.1）。临时根 `...\data1`/`data2`。

### 例 1：方案 B 路径（实测输出）

```
=== 2 方案B 路径 ===
{'ohlcv_1d': 'ohlcv\\binanceusdm\\futures\\BTC_USDT_USDT-1d.parquet',
 'ohlcv_4h': 'ohlcv\\binanceusdm\\futures\\BTC_USDT_USDT-4h.parquet',
 'funding': 'funding\\binanceusdm\\futures\\BTC_USDT_USDT.parquet',
 'oi': 'open_interest\\binanceusdm\\futures\\BTC_USDT_USDT.parquet',
 'basis': 'basis\\binanceusdm\\futures\\BTC_USDT_USDT.parquet',
 'spot_ohlcv': 'ohlcv\\binance\\spot\\BTC_USDT-1d.parquet'}
```

- ohlcv 带 tf、事件流不带 tf、市场是独立路径段（spot vs futures）——实测符合方案 B。
- 失败路径实测：`funding` 给 tf → E1004；`ohlcv` 缺 tf → E1004；symbol=`../../evil` → `symbol 不能作为路径段: '.._.._evil'` E1004；market=`swap` → E1004（白名单拦）。

### 例 2：append-only 冲突只计数（实测）

```
=== 3 append-only 冲突只计数 ===
{'first': {'added': 3, 'revised': 0, 'unchanged': 0, 'rows': 3},
 'second_no_revise': {'added': 0, 'revised': 1, 'unchanged': 2, 'rows': 3},
 'kept_close_row2': 100.5, 'kept_vol_row2': 1000.0}
=== 3b revise=True 覆盖历史 ===
{'revise_stats': {'added': 0, 'revised': 3, 'unchanged': 0, 'rows': 3},
 'close_row2_after_revise': 888.0}
```

- 写入 close=999/volume=7e3 的冲突值、不带 revise → `revised=1` 但**旧值保留**（close 仍 100.5、volume 仍 1000，`:383-388`）。
- `revise=True` → 真正覆盖（close=888）——改历史须显式。NaN==NaN 算没变（`:386-388`）避免空值误报修订。

### 例 3：checksum / version / 现算身份（实测）

```
=== 4 checksum/version/现算 ===
{'checksum': 'sha256:41f6f75a2e5ad54602f45fb18d5f931e0100fe0f3494be48c03fca800dc144f7',
 'version': '41f6f75a2e5ad546', 'version_ok': True, 'checksum_reversed_equal': True,
 'rows': 4, 'interval_ns': 86400000000000,
 'coverage': {'start': '2026-02-01T00:00:00+00:00', 'end': '2026-02-04T00:00:00+00:00'},
 'gaps': ()}
```

- `version == checksum[:16]`（实测 True）；行序打乱后 checksum 相同（`checksum_reversed_equal=True`，`:233-248` 排序后逐行 canonical_text 流式 sha256）。
- coverage/interval_ns/gaps 读时现算（`:617-656`），无 manifest。

### 例 4：funding 8h 合法间隔被误判 gap → E4005（P1，实测复现）

```
=== A funding 1h主导+合法8h → 误判gap(P1) ===
{'相邻间隔小时': [1.0,1.0,1.0,1.0,1.0,1.0,1.0,8.0,1.0,1.0],
 'interval_ns': 3600000000000, 'interval_小时': 1.0, 'tolerance_小时_3x': 3.0,
 'gaps': ({'kind': 'interior', 'symbol': 'BTC/USDT:USDT',
           'from': '2026-01-01T07:00:00+00:00', 'to': '2026-01-01T15:00:00+00:00'},),
 'passed': False, 'reason_code': 'E4002',
 'gate_a': {'passed': False, 'reason_code': 'E4002'}}

=== A2 对照 8h主导(测试用例) 不误判 ===
{'interval_小时': 8.0, 'gaps': (), 'passed': True}
```

**这就是本模块 P1 的实证**：
- `_gaps`（`:504-530`）对事件流用 `tolerance = 3.0 × interval_ns`（`EQUIDISTANT` 只含 ohlcv/basis，funding 不在，`:68`）。
- 当 funding 序列 1h 占主导（中位数=1h）而中间出现一个**合法的 8h 结算间隔**（交易所结算周期变化，C2：周期会变且历史上换过），tolerance=3h < 8h → 报 interior gap → `passed=False` → `pin().gate_a={passed:False,reason_code:E4002}`（`:429-451`）。
- 门禁 B（`gate_b.py:113-119`）见 `gate_a.passed is not True` → 抛 **E4005**，该 funding 版本不得被引用 → 资金费因子无法进成本链。
- 对照：现有测试 `test_event_stream_tolerates_irregular_intervals` 用的 1h/8h/8h 中位数=8h、tolerance=24h，恰好不误判——**测试输入掩盖了 bug**（1h 主导 + 单个 8h 才触发）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| S-1 | **funding 合法 8h 间隔被误判 interior gap → E4005**（P1）：事件流 gap 容差用 `3×中位间隔`，1h 主导时合法 8h 结算间隔超容差 → 假缺口 → gate_a=false → E4005，资金费因子无法进成本链。 | **P1** | `store.py:504-530`（`_gaps` tolerance=3×中位）；`:68`（EQUIDISTANT 不含 funding）；实测 A 段 gaps 含 interior、passed=False | **已确认**：findings 登记 U3-P1-1，本轮真实复现。修复方向：事件流 gap 容差应基于"最大合法结算周期"或按相邻间隔的分布自适应（如用 8h 作下界），而非中位数的固定倍数；并补 1h 主导+单 8h 的测试。本模块只读不改。 |
| S-2 | **`EQUIDISTANT` 含 `basis` 但 basis 是派生等距 bar**：与"事件流不带 tf"一致（basis 在 fetch 由 perp/spot 派生等距），但 `DATASETS` 注释把 basis 归 EVENT_DATASETS（`:42`），两处归类靠 `EQUIDISTANT` 区分——易混淆。 | P2 | `store.py:42,68`；`fetch.py:289-298` | **需确认**：语义正确（basis 值列由等距 bar 派生，落盘无 tf 因为间隔由数据决定），仅命名分组易误读，文档说清即可。 |
| S-3 | **`_atomic_write` 对 Windows PermissionError 重试一次**（`:319-325`），但 objects 层无此重试（见模块 28 O-4）；两处原子写策略不统一。 | P2 | `store.py:311-325` | **低优先**：供给写临时句柄冲突重试合理；objects 失败则 E5001 fail-closed，均可接受。 |
| S-4 | **`MARKET_DATASETS` 硬编码 spot 只有 ohlcv**：新增数据集（清算/链上/跨所价差）需改代码；与 plan §U3 D6"是否升注册表"对应。 | P2（扩展） | `store.py:54-57` | **需确认意图**：与 R3 因子扩展相关（新数据流前置）；但新数据流还需 fetch/字段注册配合，非本模块单独可补。 |
| S-5 | **`file_stem` 只替换 `/` 与 `:`**，反斜杠/`..` 留给 `_safe_segment` 拦（`:71-78`），注释明确"调用方负责"。 | P2（职责） | `store.py:71-78,195`；实测 `.._.._evil` 被 E1004 拦 | **已确认正确**：dataset_file 必过 `_safe_segment`（`:195`），实测防逃逸。 |

**多了什么**：无冗余。`payload_checksum` 不用 `compute_digest` 而自建行序无关流式摘要（`:233-248` 注释理由充分：MAX_INLINE_SEQUENCE=8192 护栏 + 行序进摘要问题）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 方案 B 无 manifest + 读时现算身份（本模块） | 采纳 | 24/7 加密数据量大、版本多，版本目录/别名表是 V4 负担；checksum 进 blueprint_digest 仍可判定"用的哪份" |
| 内容寻址 + 行序无关 checksum | 采纳 | 同内容同值、重排不换指纹（实测）；加密面板 8192+ 行需流式摘要 |
| append-only + revise 显式 | 采纳 | 交易所修订历史 K 线不覆盖，旧结论可解释（实测旧值保留） |
| 市场维度独立 + MARKET_DATASETS 护栏 | 采纳 | spot 无资金费/OI/basis，定义域在合约侧；采集器拦一次比别处拦十次可靠（`:51-53`） |
| 周期写进文件名（tf 用于事件流） | **拒绝** | 结算间隔会变（1h/4h/8h），写 tf 是假标签 I34（`:188-209`） |
| manifest/版本目录/别名表（V4 S2） | **拒绝** | 方案 B 刻意删除；无 manifest 是核心设计（`:1-3`） |
| `compute_digest(frame)` 直接算 checksum | **拒绝** | 8192 行护栏 + 行序进摘要（`:236-237`）——已自建流式摘要 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **方案 B 路径**：ohlcv 带 tf、事件流不带、市场独立段（实测 6 条路径）。
2. **路径段白名单**：`_SAFE_SEGMENT` + `fullmatch` 拒 `..`/换行/隐藏文件（`:36-38,81-99`，实测逃逸 E1004）。
3. **append-only 旧值优先**：冲突只计数、改历史须 revise（实测）。
4. **身份现算**：version=checksum[:16]、行序无关、coverage/interval/gaps 读时算（实测）。
5. **市场护栏第一层**：`require_market`/`require_market_dataset`/`require_market_symbols`（`:110-163`，实测 spot+funding E1004、衍生符号 E1004）。
6. **UTC 严格**：naive 时间 E1014（`:218-224,290-294`）。
7. **一文件一标的**：symbol 列自描述、混装 E4007（`:270-285`）；重复时间戳 E4007（`:301-305`）。
8. **缺数据 fail-closed**：文件不存在 E4002（`:328-334`）、声明标的缺失 E4002（`:600-604`）、无文件 E4002（`:581-586`）。
9. **覆盖不足拒**：`check_window` 不足 E4002 不缩窗（`:467-487`）。
10. **原子写**：同盘 tmp + os.replace + Windows 重试（`:311-325`）。

**发现的问题**：
- **P1**：S-1 funding 8h 误判 gap → E4005（已真实复现，详见例 4）。这是本模块唯一 P1，也是 U3 最接近 P0 的问题（直接阻断资金费因子进成本链）。
- S-2~S-5 为 P2/需确认意图。

**测试覆盖**：`test_v5_supply_store.py` 21 用例覆盖路径、append-only、checksum、gaps、市场、V4 导入、零成功；但 `test_event_stream_tolerates_irregular_intervals`（`:148-158`）用 8h 主导输入**未覆盖 1h 主导+单 8h 的 bug 场景**——这是 S-1 逃逸测试的原因，应补。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `19-supply.md` §2（方案 B） | 描述无 manifest、读时现算、append-only | 与实现一致，无需更新 |
| `19-supply.md` §3（事件流间隔会变） | 说"事件流容差 3×" | 补一句"1h 主导+偶发 8h 的合法结算变化需特殊处理"（S-1 修复后回填），并说明 C2 周期会变 |
| `10-crypto-fit.md` §7（资金费按事件） | 资金费因子可用性前提 | S-1 修复后，资金费进成本链才完整；届时更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新数据集（清算/链上/跨所价差） | ❌ 需改代码 | DATASETS/VALUE_COLUMNS/MARKET_DATASETS 三处硬编码（S-4）；需注册化 + fetch 配合 |
| 新市场（如 options） | ❌ 需改代码 | MARKETS/MARKET_DATASETS 硬编码 + 路径校验（`:49,54-57`） |
| 事件流间隔自适应 gap 判定 | ❌ 当前不支持（S-1） | 容差固定 3×中位；需改为按合法结算周期分布判定 |
| 新交易标的/交易所 | ✅ 支持（配置驱动） | exchange/symbol 是路径参数，无硬编码（实测多所路径） |
| 改校验和算法 | ❌ 拒绝 | payload_checksum 固定在 dataset_checksum/v3（`:240,244`），换算法=全量重算身份 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（供给存储不涉年化）。
- **C2 资金费按事件**：⚠️ **直接相关且受损**：事件按 timestamp 原生落盘、不 resample（实测 3 个 8h 结算事件原样保留），口径正确；但 S-1 的 8h 误判 gap → E4005 使资金费版本被门禁拒，因子无法进成本链（findings C2 ⚠️ 的实证）。
- **C3 spot/futures 维度**：✅ 三层护栏第一层在此：`market` 独立 + MARKET_DATASETS + require_market_symbols（实测 spot+funding E1004）。
- **C4 合约四元组**：❌ 路径用 ccxt symbol（`BTC/USDT:USDT`）字符串，未拆四元组；全局缺口（P0-5），非本模块单独可补。
- **C5 USDT/USD 溢价**：不适用（存储不判溢价）。
- **C6 上市/下架**：⚠️ `check_window` 逐标的边界检查（实测晚上市标的点名 E4002），但不处理 E1019/available_from 语义（需确认意图）。
- **C7 24/7 缺口**：✅ 靠时间戳现算 gaps，不用日历；但 S-1 使合法间隔误报为缺口。
- **C8 开放列集**：⚠️ VALUE_COLUMNS 按数据集白名单（`:59-65`），加列需改代码；与 findings P0-6（列集固定）一致，但供给层列清单是契约（多列 checksum 变），需与注册层协同。
- **C9 as-of 只 ffill**：不适用（存储层不填充；对齐在 stream 层）。
- **C10 判据不放松**：不适用。
- **C11 流动性分层**：不适用。
- **C12 embargo 按观测**：不适用。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（append-only + 现算身份 + fail-closed 缺数据，实测通过）。
- **R2 结论可信**：✅ 正向（身份现算 + 篡改可见，但 S-1 的假缺口会误拒数据，不污染结论而是阻断）。
- **R3 因子零代码扩展**：⚠️ 负向（S-4 数据集硬编码 + S-1 阻断资金费因子；R3 最大缺口的一部分）。
- **R7 fail-closed**：✅ 强支撑（路径白名单、市场护栏、缺数据/重复/naive 时间全拒，实测）。

---

## 结论

**本模块是 U3 核心，主体设计纪律高（方案 B/append-only/现算身份/市场护栏/原子写全部实测通过），但承载一条已确认 P1：funding 合法 8h 间隔被误判 interior gap → E4005。** 实测证据：1h 主导 + 单个 8h 结算间隔时，`_gaps` 容差=3×1h=3h < 8h → 报缺口 → `gate_a.passed=False` → 门禁 B 抛 E4005，资金费因子无法进成本链；现有测试用 8h 主导输入恰好掩盖此 bug。**"生产可用=能真挖因子"角度**：S-1 不修，资金费动量/carry 类因子（R3 关键场景）无法进入成本链——建议列为 U3 首修项：改事件流 gap 容差为按合法结算周期分布判定（或设 8h 下界），并补 1h 主导+单 8h 测试。其余 S-2~S-5 为 P2/需确认意图。
