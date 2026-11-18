# 模块审查 64 · `v5/app/cli/fetch.py`

> 审查日期：2026-11-18
> 依据：源码 159 行全文通读；上游 `upstream/fetch.py`（fetch_ccxt/import_v4/FetchReport）、`upstream/store.py`（require_market_dataset/require_market_symbols/DATASETS/BAR_DATASET/DEFAULT_MARKET）、`contracts/errors.py`（E1004/E4002）、`cli/common.py`（load_json_dict/envelope_ok）；设计 `19-supply.md` §2/§7（方案 B 四数据集采集）、`s0/06-upstream.md`；测试 `tests/v5/test_v5_live_cli.py:102-114`；**实测** `%TEMP%/fm_review56_67/probe_fetch.py`（假客户端零成功）与 CLI help/失败路径。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`fetch.py` 是**四数据集采集**命令（`writes_supply`），一条命令两种来源同一落点：V4 快照导入（`--from-v4`）或交易所增量（`--start/--end`）。它应然回答五件事：

1. **参数校验**：`--datasets` 必填且在词表 `{ohlcv,funding,open_interest,basis}`；`--symbols` 与 `--symbol-set` 互斥且必给其一；`--exchange` 必填；区间必给且 start<end。缺参报错不猜（P2）。
2. **市场维度护栏（连网前）**：`require_market_dataset(market, dataset)` + `require_market_symbols(market, symbols)` 在任何网络调用之前执行——「spot 目录读 funding」必须在逐标的 skip 之前失败，不能等跑完一堆 skip 再报成功（C3 红线）。
3. **零成功即失败**：一次采集若每个标的都被记 skipped（写盘为零）却返回 ok=true，是标准假成功（用错标的集/交易所/区间无数据都落这里）→ E4002/exit4。
4. **basis 双客户端**：basis 由 perp/spot 收盘价派生，必须两边客户端都在（`--spot-exchange`）。
5. **唯一 ccxt 构造点**：`_client_factory`（`:64-73`）是唯一 `ccxt.<exchange>()` 构造点，测试替换它注入假客户端。

**加密视角（必查点）**：市场维度（C3）——`market∈{futures,spot}` 独立于 `exchange`，spot 只许 ohlcv；数据集与周期绑定（C2）——funding/OI/basis 不带 timeframe（周期会变，按事件存），只有 ohlcv 带 `--timeframe`（见 `fetch.py:115` 与 store）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 全部真实调用（`venv/Scripts/python.exe -m v5.app.cli fetch ...` 与 `%TEMP%/fm_review56_67/probe_fetch.py`），非推演。

### 例 1：市场护栏（连网前失败，真实）

```
$ flow fetch --datasets ohlcv,funding --symbols BTC --exchange binance --market spot --start 2025-01-01 --end 2025-12-31
→ {"ok":false,"exit_code":2,"errors":[{"code":"E1004","layer":"usage",
   "message":"spot 市场不支持数据集 funding","path":"datasets",
   "hint":"spot 只能采 ['ohlcv']；funding / open_interest / basis 的定义域是合约，改用 --market futures"}]}
EXIT=2
```

C3 护栏在 `_client_factory` 之前触发（`:97-99`），未连网即拒。

### 例 2：零成功 E4002/exit4（真实假客户端探针）

> 脚本注入 `EmptyExchange.fetch_ohlcv → []`，所有标的 skipped。

```
CMD_FETCH_ERR: E4002 | message: ['ohlcv'] 没有任何标的采集成功（2 个标的全部跳过） | exit: 4
```

来自 `fetch.py:136-148`：`empty = [r.dataset for r in reports if r.symbols == 0]` → E4002，hint 提示「标的集与市场不匹配/交易所 id 错/区间无数据」，`context.skipped_sample` 带前 5 个跳过原因。**exit4 实测**（`exit_code_for(E4002)=4`）。

### 例 3：参数校验失败（真实）

| 输入 | 输出 |
|---|---|
| `--datasets liquidations` | E1004「未知数据集 'liquidations'」hint「支持 ohlcv/funding/open_interest/basis」exit2 |
| 交易所采集缺 `--start/--end` | E1004「从交易所采集必须给 --start 与 --end」hint「不给区间等于抓全部，那是猜」exit2 |
| `--symbols BTC --symbol-set cs20` | argparse「not allowed with argument --symbols」exit2（**stderr 文本，非信封**——M-1） |
| 无 `--exchange` | E1004「fetch 必须显式给 --exchange」exit2 |

### 例 4：正常路径（源码 `:150-155`，成功信封结构）

```
envelope_ok("fetch", None, {"exchange","market","data_root","datasets":[FetchReport.to_dict(),...]})
```

`FetchReport.to_dict()` 含 dataset/source/symbols/rows_added/revised/skipped（`fetch.py:49-66`）——每个数据集实际采到多少、跳过多少都在信封里。本轮未连真实交易所（需网络），成功路径由 `test_v5_live_cli.py:102-114` 的假交易所覆盖。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| F-1 | **数据集词表在 CLI 里硬编码**，与 plan_schema.json / store.DATASETS 三处重复。 | P2（一致性） | `fetch.py:83-87` 字面量四值；`store.DATASETS` 另一处；hint 自称「与 plan_schema 同一词表」 | **需确认意图**：词表三处维护，新增数据集（如 liquidations 链上）要改三处。建议 CLI 从 `store.DATASETS` 派生（单一真相），零代码扩展（plan §U3 D6 决策项）。 |
| F-2 | **`--from-v4` 路径不查市场护栏对应的来源相容**：`import_v4` 在 `fetcher` 内部是否再查 `require_market_dataset` 需确认；CLI 层 `:97-99` 已查一次。 | P2（需确认） | `fetch.py:97-99,106-112` | **低优先**：CLI 层已查，来源侧重复查是纵深防御，非必需。 |
| F-3 | **`_request_symbols` 用 `dict.fromkeys` 去重保序**（`:49,58`），静默去掉重复标的——若用户重复给同一标的，不提示。 | P2（P2 原则边界） | `fetch.py:49,58` | **需确认**：去重是合理容错（避免重复采集同一标的写冲突），但按「缺参报错不猜」的严格解读，静默去重可算轻微违反。低影响。 |
| F-4 | **`_client_factory` 只在非 `--from-v4` 路径使用**（`:119-131`），测试注入假客户端必须走交易所采集路径——与 `test_v5_live_cli.py:109` 一致。 | — | `fetch.py:119` | **符合应然**，无差距。 |
| F-5 | **零成功判定用 `r.symbols == 0`，但部分成功（如 2/5 标的成功）不告警**：只拒「全失败」，部分失败靠信封的 `skipped` 字段让人自己看。 | P2（可观测性） | `fetch.py:136-148` | **需确认意图**：设计选择——不阻断部分成功（已采到的标的不浪费），但部分失败不进 warnings 只进 data。可加 warning 提示「3/5 跳过」提升可见性。 |

**多了什么**：无。市场护栏前置、零成功拒、唯一客户端构造点都是必要防线。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 事件数据不带 timeframe（按事件存） | 采纳 | funding/OI/basis 周期会变且历史上换过（C2）；CLI `--timeframe` 只对 ohlcv 生效（`:165` help + `:115`），防止「按文件名推频率」。 |
| 市场×数据集/标的护栏前置 | 采纳 | spot 只 ohlcv、`require_market_symbols` 防现货符号不带 `:USDT` 等错配（实测例 1），fail-fast 省去无效网络调用。 |
| 零成功 fail-closed | 采纳 | E4002/exit4 实测（例 2），杜绝「用错标的集还报 ok」的假成功。 |
| 单标的失败不中断（partial success） | 采纳 | `fetch_ccxt` 记 skipped 继续（upstream/fetch.py:345-355），已采到的不浪费；但部分失败可见性弱（F-5）。 |
| 原地 to_feather 静默改写历史 | 拒绝 | append-only，`--revise` 才覆盖（`:170-171`），冲突只计数——V4 缺陷不重演。 |
| 从 `store.DATASETS` 派生词表 | **应采纳（F-1）** | 当前 CLI 硬编码三处重复，新增数据集要改多处——零代码扩展前置。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **市场护栏在连网前**：`:97-99` 早于 `_client_factory`（`:119`）/ `import_v4`（`:108`），实测 spot+funding 未连网即 E1004（例 1）。
2. **零成功 E4002/exit4**：`probe_fetch.py` 实测（例 2）。
3. **区间校验**：`_parse_dt`（`:31-38`）RFC3339 解析 + timezone 补全；`start>=end` 拒（`:103-104`）。
4. **symbols 互斥**：`bool(raw)==bool(named)` 即 E1004（`:44-47`）+ argparse 互斥组（`__init__.py:158-160`）双保险。
5. **未知数据集/交易所拒**：数据集词表校验（`:82-87`）、`_client_factory` 校验 ccxt 交易所可实例化（`:64-73`）。
6. **basis 双客户端**：`"basis" in datasets` 时强制构造 spot 客户端（`:122-125`）。
7. **唯一构造点**：`_client_factory` 是唯一 `ccxt.Exchange()` 点，测试可替换（`test_v5_live_cli.py:109`）。

**发现的问题**：
- 无 P0/P1 级 bug。F-1（词表硬编码）、F-3（静默去重）、F-5（部分失败不可见）均为 P2，不影响采集正确性与 fail-closed。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `19-supply.md` §2 四数据集词表 | 若写 `refresh`/固定四数据集 | 补 F-1：CLI 词表应从 `store.DATASETS` 派生，文档同步新数据集清单 |
| `09-app-storage.md:78,84,101` | 用 `refresh` | 改 `fetch`（并入 56 号 C-1） |
| `19-supply.md` §7 市场护栏 | 与实现一致 | 无需更新；可补 F-5「部分成功不阻断，跳过明细在 envelopes.datasets[].skipped」 |
| `s0/06-upstream.md` | 零成功语义 | 确认是否与 E4002/exit4 一致（例 2），不一致则补 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增数据集（清算/链上/跨所价差） | ⚠️ 需改多处 | CLI 词表（`:83`）+ store.DATASETS + plan_schema 三处（F-1）；应升注册表实现零代码。 |
| 新增交易所（ccxt 支持的任意 id） | ✅ 零代码 | `--exchange <id>` 传给 `_client_factory`，`getattr(ccxt, id)` 动态取类——只要 ccxt 支持即可。 |
| spot 采集新数据集 | ❌ 设计禁止 | 市场护栏拒（C3），符合「spot 无资金费/合约数据」。 |
| 采集时间范围细粒度（分页/重试） | ✅ 已支持 | `--limit` 控制每页根数（`:172`），分页/重试在 upstream/fetch.py（U3 范围）。 |
| 采集历史长度受限（如 OI 仅 25 行） | ⚠️ 数据源上限 | 币安 OI 只给约 30 天历史——CLI 正确采集但覆盖不了长窗口（见 66 号 OI 25 行事实），属数据源限制非 CLI bug。 |

---

## C1–C12 相关项判定

- **C2 资金费按事件/周期会变**：✅ 强——funding/OI/basis 不带 timeframe（`:115` 只对 ohlcv），按事件存；拒绝按文件名推频率。
- **C3 spot 无资金费/market 独立**：✅ 强——市场护栏连网前查（`:97-99`），实测 spot+funding → E1004（例 1）。
- **C4 合约四元组**：⚠️ 部分——CLI 接受 `BTC/USDT:USDT` 形态符号（`require_market_symbols`），但 exchange/underlying/contract_type/settle 四元组未拆列（全局缺口，非 fetch 单独）。
- **C5 USDT/USD 偏离**：中性（fetch 不计算价格）。
- **C7 24/7 缺口**：中性（缺口体检在 doctor，65 号）。
- **C8 列集开放**：⚠️ 数据集四选一硬编码（F-1），新加密流需改多处——与 plan §U3 D6「MARKET_DATASETS 升注册表」一致。
- **C1/C6/C9/C10/C11/C12**：不适用（本模块不计算/不判据）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑——四数据集采集是 R1「--live 自采数据八工位全绿」的数据源；实测零成功 fail-closed、市场护栏前置。
- **R2 结论可信**：✅ 正向——零成功不假成功、市场错配连网前拒，避免「用错数据还继续跑」。
- **R3 因子零代码扩展**：⚠️ 负向——数据集词表硬编码（F-1）使新加密流（清算/链上）接入需改代码，是 R3 未通的一部分（全局 U3/U6，非 fetch 单独）。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：✅ 强支撑——零成功 E4002、市场不相容 E1004、缺参 E1004、坏时间 E1004，全拒且码正确。

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 四数据集采集的两来源一落点、市场维度护栏连网前置、**零成功 E4002/exit4（必查点，实测）**、basis 双客户端、唯一 ccxt 构造点——全部成立。5 项 P2：F-1（数据集词表三处硬编码，阻碍零代码扩展）、F-3（静默去重）、F-5（部分失败不可见）等，均为一致性/可观测性问题。OI 仅 25 行是币安数据源上限（非 CLI bug），在 66 号 estimate 展开。不影响「生产可用=能真挖因子」。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 10 个模块 `cli/doctor.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ F-1 想当场裁决（是否从 store.DATASETS 派生词表）→ 告诉我
