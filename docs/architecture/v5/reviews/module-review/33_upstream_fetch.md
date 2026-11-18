# 模块审查 33 · `v5/upstream/fetch.py`

> 审查日期：2026-11-18
> 依据：源码 372 行全文通读；依赖 `store.py`（路径/护栏/upsert）、`app/cli/fetch.py`（cmd_fetch 零成功即失败）、`contracts/errors`。设计 `19-supply.md` §2/§7；测试 `test_v5_supply_store.py:214-298`（V4 导入/零成功/护栏）。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `fm_review_26_33/probe3_fetch.py` 与修正后的命令行实测。

---

## D1 · 应然（抛开实现，这个模块该做什么）

fetch 是**四数据集采集器**（ohlcv/funding/open_interest/basis），回答"数据从交易所或 V4 快照怎么采下来、落到方案 B 路径"。职责边界：

1. **两来源同一落点**：ccxt 交易所分页拉取 + V4 feather 快照导入（`--from-v4`），最终都经 `store.upsert` 落到同一 parquet，读取侧不关心来源（`:1-15`）。
2. **ccxt 唯一客户端**：交易所路径只有一条客户端构造点（`app/cli/fetch.py:64-73` `_client_factory`，测试替换注入假客户端）——无第二套分页/重试实现。
3. **市场护栏在连网前**：`require_market*` 在 `fetch_ccxt`/`import_v4` 开头调用（`:169-172,319-322`），且 `cmd_fetch` 在构造客户端前先校验（`app/cli/fetch.py:95-99`）——现货声明 funding 必须在连网前失败，不能 skip 完再报成功。
4. **零成功即失败**：`cmd_fetch` 检查每份 report 的 `symbols==0`，全跳过则 E4002/exit4（`app/cli/fetch.py:133-148`）——堵住"用错标的集/交易所/区间无数据却返回 ok"的假成功。
5. **事件原生网格保留**：funding/OI 按事件时间戳原样落盘，不 resample；周期由数据决定（`:248-286`，`store` 现算）。
6. **单标的失败不中断其它**：逐标的 try/except，失败记 `skipped`，其它继续（`:328-352`）——但由零成功检查兜底。

**时间语义按数据集分开**：bar（ohlcv/basis）等距、tf 是身份一部分；事件流（funding/OI/basis 值）间隔会变、不接受 tf（`:17-20`，与 store 的 dataset_file 一致）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> venv Python 实测，假交易所 `FakeEx`（fetch_* 全返回空）。临时根 `...\data3`。

### 例 1：fetch_ccxt 零成功 report（实测）

```
=== 1 fetch_ccxt 零成功 report ===
{'report.symbols': 0, 'skipped': [{'symbol': 'BTC/USDT:USDT', 'reason': 'empty'}]}
```

- 交易所返回空 → 该标的记 `skipped{reason:"empty"}`（`:353-355`），`report.symbols=0`。
- **采集器本身不抛错**（设计：单标的失败不中断，`:316`），零成功的失败由上层 `cmd_fetch` 判定。

### 例 2：cmd_fetch 零成功 → E4002/exit4（实测）

```
零成功: [E4002] ['ohlcv'] 没有任何标的采集成功（1 个标的全部跳过） (at datasets) exit= 4
```

- 所有标的 skipped → `empty` 列表非空 → E4002，退出码 4（SUPPLY，实测 `exit=4`）。
- 堵住假成功：即使 `fetch_ccxt` 返回 ok report，cmd_fetch 仍判"这次什么都没拿到"并失败（`app/cli/fetch.py:133-148`）。

### 例 3：连网前市场护栏（实测）

```
连网前护栏: [E1004] spot 市场不支持数据集 funding (at datasets) exit= 2
```

- spot + funding：客户端工厂被替换成"若调用就抛"的 boom，实测**未触发 boom**，而是 E1004——证明护栏在连网前拦住（`app/cli/fetch.py:97-99` + `store.require_market_dataset`）。
- 退出码 2（USAGE，E1xxx）——与 E4002/exit4 区分：市场/数据集不自洽是用法错误。

### 例 4：事件原生网格保留（实测）

```
=== 4 事件原生网格保留 ===
{'added': 3, '落盘时间戳': ['2026-01-01T00:00:00+00:00', '2026-01-01T08:00:00+00:00', '2026-01-01T16:00:00+00:00'],
 'funding_rate': [0.0001, 0.0002, 0.0003], '是否原样保留(未resample)': True}
```

- funding 3 个结算事件（8h 间隔）原样落盘，未 resample 成等距 bar——符合 C2（按事件）与 I34。

### 例 5：退出码映射（实测）

```
=== 5 E4002→SUPPLY exit4 ===
{'E4002.exit_code': 4, 'SUPPLY': 4, 'EXECUTION': 1}
```

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| F-1 | **basis 需两个交易所实例（perp + spot）**，但 `fetch_ccxt` 的 `spot_exchange` 参数若漏传会 E4002（`:336-344`）；CLI 默认 spot_id=`binance`（`app/cli/fetch.py:124`）。跨所 basis 若 perp/spot 不在预期所，需显式 `--spot-exchange`。 | P2（配置） | `fetch.py:289-298,336-344`；`app/cli/fetch.py:121-125` | **需确认意图**：默认 binance 合理（流动性最好），跨所价差场景需显式配置；非 bug。 |
| F-2 **`_basis_frame` 按 timestamp 内连接 perp/spot 收盘价**（`:289-298`），要求两边时间轴完全对齐；加密 24/7 不同所维护窗口不同，内连接可能丢点。 | P2 | `fetch.py:289-298` | **需确认**：basis 定义要求同一时刻两边都有价，内连接丢点是正确语义（缺边不算基差）；但应诊明"丢了多少点"。 |
| F-3 **`_paginate` 通用分页按"页不足 limit 视为到头"**（`:211-233`），若交易所最后一页恰好满 limit 会多请求一页；`max_pages=10000` 上限。 | P2 | `fetch.py:211-233` | **低优先**：标准分页实现，多请求一页无害；10000 页上限对 1d 数据足够。 |
| F-4 **数据集/来源硬编码**：`DATASETS` 来自 store，`V4_STREAM_SUFFIX` 三流写死（`:120`）；新增清算/跨所流需改代码。 | P2（扩展） | `fetch.py:120,329-344` | **需确认意图**：与 findings P0-4（清算/跨所两流未接）对应；属 R3 因子扩展前置，非本模块单独可补（还需 store 数据集 + 字段注册）。 |
| F-5 **`import_v4` 的 `--start/--end` 窗口过滤**（`:197-199`）可能导致 empty_window skipped；若所有 V4 文件在窗口外 → 零成功 → cmd_fetch E4002（正确）。 | P2 | `fetch.py:197-199` | **已确认正确**：与交易所路径同语义，零成功兜底。 |

**多了什么**：无冗余。`FetchReport`（`:48-67`）是给人看的账，不进指纹——与"采集结果不改 plan"一致。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| ccxt 唯一客户端工厂（本模块 + CLI） | 采纳 | 一套分页/重试/符号约定；测试注入假客户端即可离线验证（实测） |
| 两来源同一落点（交易所 + V4 快照） | 采纳 | V4 有 2000+ 文件是既有事实，重下慢且 OI 只留 30 天；统一 parquet 让读取侧无感（`:13-15`） |
| 连网前护栏 + 零成功即失败 | 采纳 | 24/7 采集易因标的集/交易所写错而"假成功"；两道防线堵住（实测 E1004/E4002） |
| 事件原生网格不 resample | 采纳 | 资金费按事件、周期会变（C2/I34），resample 会引入前视/假标签（实测原样保留） |
| 多套交易所客户端 / 回退逻辑 | **拒绝** | 复现性与内容寻址不成立；ccxt 单客户端 + 假注入是唯一路径 |
| 单标的失败就整体失败 | **拒绝** | 24/7 某标的临时断网不该中断其它；用 skipped + 零成功兜底平衡 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **ccxt 唯一客户端**：`_client_factory`（`app/cli/fetch.py:64-73`）是唯一构造点，`cmd_fetch` 通过 `factory` 参数可注入假客户端（实测）。
2. **连网前护栏**：`cmd_fetch` 构造客户端前先 `require_market_dataset`/`require_market_symbols`（实测 spot+funding E1004、未连网）；`fetch_ccxt`/`import_v4` 入口再校一次（双保险，`:169-172,319-322`）。
3. **零成功即失败**：全 skipped → E4002/exit4（实测），堵假成功。
4. **事件原生网格**：funding/OI 原样落盘不 resample（实测 3 个 8h 事件原样）。
5. **单标的失败不中断**：逐标的 try/except + skipped（`:328-352`），其它继续。
6. **V4 naive UTC 解释**：`_utc` 用 `tz_localize("UTC")`（`:82-101`），避免按本地时区解释导致日期漂移。
7. **V4 事件流假标签隔离**：`V4_STREAM_SUFFIX` + `v4_stream_path` 把 `-1d-` 当定位串，不带进目标文件名（`:120-138`），与 store "事件流不带 tf" 一致。
8. **符号显式要求**：`import_v4` 要求调用方给 symbols，不从文件名反推（`:154-163`），避免多段符号猜错（1000PEPE vs PEPE）。
9. **basis 双边实例检查**：缺 spot_exchange E4002（`:336-344`）。
10. **`_window` 时间过滤**：start/end 闭区间（`:70-79`）。

**发现的问题**：F-1~F-5 均为 P2/需确认意图，**无 P0/P1 级 bug**。特别说明：本模块**不涉及** store 的 funding 8h gap 误判（那是读取侧 store._gaps 的问题，见模块 32 P1）；采集侧按事件原样落盘本身正确。

**测试覆盖**：`test_v5_supply_store.py:214-298` 覆盖 V4 列归一、符号必填、衍生符号拒、零成功失败、连网前护栏、门禁 B 市场不符。充分。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `19-supply.md` §7（采集） | 描述两来源 + 连网前护栏 + 零成功 | 与实现一致，无需更新 |
| `19-supply.md` §2（事件流不带 tf） | 描述周期由数据决定 | 与实现一致（实测原样落盘），无需更新 |
| `10-crypto-fit.md` §7（清算/跨所两流） | 列为加密缺口 | F-4：两流未接仍是 R3 缺口；待数据流扩展后更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增数据集（清算/跨所价差） | ❌ 需改代码 | DATASETS（store）+ V4_STREAM_SUFFIX + fetch 分支 + 字段注册多处；F-4，R3 前置 |
| 新增交易所 | ✅ 支持（配置） | exchange_id 参数 + ccxt 动态取类；`_client_factory` 校验可实例化（`:64-73`） |
| 新分页/重试策略 | ❌ 不支持（刻意单一） | `_paginate` 唯一实现（`:211-233`）；多实现会破坏复现性 |
| 自定义 V4 列别名 | ⚠️ 半配置化 | `V4_COLUMN_ALIASES` 字典（`:35-45`），加别名改一处即可 |
| 事件流新增字段（如 funding 多列） | ⚠️ 需改 store 白名单 | VALUE_COLUMNS 按数据集固定，fetch 的 `_funding_frame` 只取 funding_rate（`:259`） |

---

## C1–C12 相关项判定

- **C1 365 年化 / C10 判据 / C11 分层**：不适用（采集层不涉）。
- **C2 资金费按事件**：✅ 强支撑（funding/OI 按事件时间戳原样落盘，不 resample；实测 3 个 8h 事件原样）——注意 gap 误判是 store 读取侧问题（模块 32 P1），非采集侧。
- **C3 spot/futures 维度**：✅ 三层护栏的采集前层在此 + CLI：`require_market_dataset`/`require_market_symbols` 连网前拦（实测 E1004/exit2）。
- **C4 合约四元组**：❌ ccxt symbol 字符串未拆四元组（全局 P0-5），非本模块单独可补。
- **C5 USDT/USD 溢价**：不适用。
- **C6 上市/下架**：不适用（采集不判 available_from）。
- **C7 24/7 缺口**：✅ 采集按事件原样落盘；缺口判定在 store 读取侧。
- **C8 开放列集**：⚠️ VALUE_COLUMNS 白名单（store）+ 事件字段映射（本模块 `:259,281`）固定；add 事件流需改代码（F-4），与 findings P0-6 一致。
- **C9 as-of 只 ffill**：不适用（对齐在 stream 层）。
- **C12 embargo**：不适用。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑（两来源 + 原子落盘 + 单标的容错，真跑采过 3 个加密流）。
- **R3 因子零代码扩展**：⚠️ 负向（清算/跨所两流未接 F-4，R3 缺口之一）。
- **R7 fail-closed**：✅ 强支撑（连网前护栏 + 零成功即失败，实测 E1004/E4002，防假成功——这是 V4 头号缺陷的正面修复）。

---

## 结论

**本模块代码质量高，无 P0/P1，可确认通过。** ccxt 唯一客户端、连网前市场护栏、零成功即失败 E4002/exit4、事件原生网格不 resample 全部实测通过（假交易所零成功→E4002/exit4；spot+funding→E1004/exit2 且未连网；3 个 8h funding 事件原样落盘）。**注意**：必查要点中的"funding 8h 误判 gap→E4005"是 store.py 读取侧（模块 32 P1），采集侧按事件落盘本身正确，勿混为一谈。F-1~F-5 均为 P2/需确认意图，其中 F-4（清算/跨所两流未接）是 R3 因子扩展前置，需在新数据源/字段注册层协同解决，非本模块单独可补。**"生产可用=能真挖因子"角度**：采集链本身生产可用；瓶颈在扩展（新数据流）与读取侧 P1（store 8h 误判）。
