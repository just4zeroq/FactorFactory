# 模块审查 35 · `v5/runtime/gate_b.py`

> 审查日期：2026-11-18
> 依据：源码 280 行全文通读；依赖 `contracts/types.py`（Pin 九字段）、`contracts/digest.py`（compute_digest）、`contracts/errors.py`（E4002/E4003/E4005）。设计 `s0/03-runtime.md` §8、`19-supply.md` §3（OI dormant）、`s0/01` Pin 契约；测试 `test_v5_runtime_real.py:246-399`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_u4.py`（G 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Gate B 是**第 9 步唯一接触外部世界的只读适用性检查**（`:1-4`）：不补采、不降级用旧版本、不缩短时间轴，失败不创建 Ticket/不写目录/不触发采集。五道检查 + 三条件核心：

1. **门禁 A 前置**（`:113-119`）：`pin.gate_a.passed is True`（严格 True，`1`/`"true"` 不算）否则 E4005；版本保留供排查，编译不补采。
2. **覆盖区间**（`:121-168`）：pin.coverage 必须覆盖 `boundary.time_axis.mine` 起止；时间轴解析不了（含 `reserved`）即 E4002，不静默跳过（与标的集同一标准）。
3. **三条件**（用户要求核心）：
   - **标的集**：Pin 只携带 `symbols_digest`（无标的清单），所以比对是「计划标的集规范摘要 == pin.symbols_digest」（`:170-195`）——相等比子集更严，不需第二真相。`BAR_DATASET=ohlcv` 才逐位比；辅助数据集只要求"有标的"（缺个别标的是新上市/下架事实）。
   - **新鲜度**：`acquired_at >= coverage.end`（纯函数，无墙钟 `:197-205`）；plan_schema 无 `max_age_days` 键，故不实现墙钟分支，**绝不 `datetime.now()`**（F1）。
   - **供给维度**：`plan.supply.frequency/exchange/market` == 事实清单的 `timeframe/exchange/market`（`:215-280`）。Pin 九字段不含这三项，只能核对 seed manifest；缺 meta 就拒（"查不到来源"≠"来源没问题"）。`market` 是独立维度（同 exchange 下 spot/futures 是两份数据）。`frequency` 只对 ohlcv 比（资金费/OI/basis 结算周期是数据属性，实测 1h/4h/8h 混合且会变，I34）。
4. **末端闭合**（`:206-213`）：`pin.end_closed` 为真，否则 E4003（未完成 bar 不得研究）。
5. **partial/OI 边界**（`:157-168`）：open_interest 交易所只留约 30 天，覆盖不足时标 dormant 不阻断（19-supply §3），其他数据集照常 E4002。这是承认上游事实边界，不是放宽完整性。

**时区语义**：`_parse_day`（`:34-45`）把 `YYYY-MM-DD` / RFC3339（`Z`/`+00:00`）解析成带时区 datetime，naive 视为 UTC；等价写法得到同一时刻（D1 防"Z 与 +00:00 两种写法产生不同结论"）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 调用 `check_gate_b` 实际打印结果。Pin 用 `Digest.parse(compute_digest(...))` 构造（实测先用字符串 checksum 会 E1004，已修正）。复现脚本 `probe_u4.py` G 段。

### 基线输入

```
pin = Pin(dataset="ohlcv", version="aaaa...", checksum=sha256:..., 
          coverage={start:2024-01-01Z, end:2026-09-13Z},
          symbols_digest=sha256(compute_digest(["BTC","ETH"])),
          end_closed=True, acquired_at=2026-11-15Z,
          gate_a={passed:True, reason_code:"ok"})   # 故意无 symbols 键
plan = {"boundary":{"time_axis":{"mine":"2024-02-01..2026-06-30"}},
        "supply":{"symbols":["BTC","ETH"],"frequency":"1d","exchange":"binance"}}
supply_meta = {"timeframe":"1d","exchange":"binance","market":"futures"}
```

### 三条件 + 前端检查（真实输出）

| 场景 | 实测输出（错误码\|消息） |
|---|---|
| 三条件全过 | `OK`（返回 None，无副作用） |
| **标的集 digest 不符**（改 DOGE） | `E4002\|标的集与供给版本不符: 计划 sha256:3ccf69b5..., 供给 sha256:1241b224...` |
| **新鲜度**（acquired_at=2026-08-01 < end=2026-09-13） | `E4002\|数据不新鲜: acquired_at=2026-08-01T00:00:00+00:00 < coverage.end=2026-09-13T00:00:00+00:00` |
| **频率维度**（计划 1d / 事实 4h） | `E4002\|频率不符: 计划 1d, 事实 4h` |
| **交易所维度**（计划 binance / 事实 okx） | `E4002\|交易所不符: 计划 binance, 事实 okx` |
| **市场维度**（计划 futures / 事实 spot） | `E4002\|市场不符: 计划 futures, 事实 spot` |
| 缺 manifest（不传 supply_meta） | `E4002\|ohlcv 缺少可读的 manifest，无法核对频率与交易所` |
| 门禁 A 未过（passed=False） | `E4005\|ohlcv 门禁 A 未通过，不得被别名指向` |
| 末端未闭合（end_closed=False） | `E4003\|ohlcv 末端未闭合` |
| coverage 起点缺（mine 2023 起） | `E4002\|ohlcv 覆盖区间不足: 起点缺 2023-01-01..2024-01-01` |

**逐条解读**：标的集失败时错误 context 同时带 `required_symbols` / `required_digest` / `pin_symbols_digest`（`:187-189`），供人工定位是哪批标的变了；新鲜度消息同时打印两侧 ISO 时点（`:201-202`），时区无歧义。

### 时区 / partial 边界（真实输出）

```
时区解析 _parse_day:
  "2026-06-30T00:00:00Z"        → 2026-06-30T00:00:00+00:00
  "2026-06-30T00:00:00+00:00"   → 2026-06-30T00:00:00+00:00
  equal = True
  "2026-06-30"（naive）          → 2026-06-30T00:00:00+00:00

partial 边界（同一覆盖缺口 2024-02..2026-06 但只有 2025-01..2025-06）:
  open_interest → OK（dormant，不阻断；acquired_at=2025-07 满足 >= coverage.end）
  ohlcv         → E4002|ohlcv 覆盖区间不足: 起点缺 2024-02-01..2025-01-01; 终点缺 2025-06-01..2026-06-30
```

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| G-1 | **`acquired_at` 新鲜度只比覆盖末端，不比墙钟**：plan_schema 无 `max_age_days` 键（`:11-13`），所以"数据是 3 年前采的但声称覆盖到今天"只要 acquired_at≥end 就过。 | P2（确认设计） | `gate_b.py:11-13,197-205` | **已确认符合设计**：模块注释明确"绝不 datetime.now()（F1）"，墙钟新鲜度未定义。若将来加 `max_age_days` 须先改 schema，本模块不擅自加。 |
| G-2 | **OI 覆盖不足直接 pass，无显式 dormant 标记返回**：`:157-168` 用 `pass` 跳过，调用方拿不到"OI 是 dormant"的结构化信号。 | P2 | `gate_b.py:157-168` | **需确认意图**：设计把 OI 当辅助数据（成本/拥挤度），覆盖不足不阻断研究。但"标 dormant"目前只是注释语义，未落到返回值（check_gate_b 返回 None），下游无从区分"OI 完整"与"OI 残缺被放"。 |
| G-3 | **标的集"相等"对非 ohlcv 只要求非空**：辅助数据集 `symbols_digest` 非空即过（`:190-195`），不保证与计划标的集一致。 | P2 | `gate_b.py:190-195` | **已确认符合设计**：新上市/下架导致辅助数据缺个别标的是交易所事实，不是损坏；面板基座 ohlcv 才逐位相等。方向正确。 |
| G-4 | **时区解析容错**：`_parse_day` 对无法解析值返回 None（`:43-44`），覆盖检查转为"终点未知"E4002；但 `acquired_at` 是 Pin 的 datetime 字段（契约保证带时区），无解析失败路径。 | P2（低） | `gate_b.py:34-45`；`types.py` Pin 校验 | **已确认**：Pin 契约强制 acquired_at 带时区，故新鲜度分支无 None 风险；解析容错只为 coverage 字符串服务。 |

**多了什么**：无冗余。`gate_a.passed` 前置 + 覆盖 + 标的 + 新鲜度 + 末端 + 维度共六道，层层 fail-closed，无静默降级。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 用正式 `contracts.Pin`（非自造简化类） | 采纳 | 避免"两套 Pin"（`:4`）；`as_pin` 接受 Pin 实例或九字段字典，字符串桩返回 None 走 E4002 |
| 标的集用 digest 比对而非读清单 | 采纳 | Pin 契约无清单是刻意（S2-M5 裁决）；相等比子集更严且不需第二真相（`:6-10`） |
| 新鲜度纯函数 `acquired_at >= coverage.end` | 采纳 | 无墙钟（F1），可复现；墙上"数据多旧"不在本轮范围 |
| 市场维度独立比对（spot/futures 两份数据） | 采纳 | 同 exchange id 下混用等于"把现货因子标成合约因子"（`:248-250`） |
| 按文件名/周期常数推资金费频率 | **拒绝** | 结算 1h/4h/8h 混合且同合约历史换过（I34）；frequency 只对 ohlcv 比（`:28-31`） |
| 墙钟 `datetime.now()` 判新鲜度 | **拒绝** | 不可复现（F1）；schema 无 max_age_days 键，代码留分支也不可达 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **门禁 A 严格 True**：`parsed.gate_a.get("passed") is not True` → E4005（`:113`），实测 `passed=False` 拒绝；测试还钉 `passed=1` 也拒（`test_v5_runtime_real.py:333-336`）。
2. **别名桩拒**：`as_pin` 对字符串/不完整 Mapping 返回 None → E4002（`:70-83,106-112`），实测字符串桩被拒。
3. **时间轴不可解析 E4002**：`mine` 拆 `..` 后 `want_start/want_end` 任一 None 或 start≥end 即拒（`:131-142`），`reserved` 实测拒。
4. **覆盖检查消息含"覆盖区间"**：只断言 E4002 会被标的集 E4002 掩盖，故消息带具体缺口（`:148-168`，实测 `起点缺 2023-01-01..2024-01-01`）。
5. **标的集 context 完整**：失败带三键（`:187-189`），实测打印计划/供给 digest。
6. **新鲜度纯函数**：`acquired_at < have_end` → E4002（`:198-205`），实测两时点 ISO 打印。
7. **末端闭合**：`end_closed=False` → E4003（`:207-213`），实测。
8. **供给维度三比对**：exchange/market 全数据集必比（`:233-265`），frequency 仅 ohlcv（`:266-280`）；实测三维度不符各 E4002，缺 manifest 也 E4002。
9. **不反向检查**："覆盖末端晚于研究终点"是正常（覆盖⊇研究），不做反向（`:103-104`），避免把"数据充足"误判失败。
10. **零副作用**：全程只读，无 mkdir/写文件/采集调用。

**发现的问题**：G-1~G-4 均为 P2/需确认意图，**无 P0/P1 级代码 bug**。测试覆盖 15+ 项门禁 B 用例（覆盖/标的/新鲜度/末端/维度/桩/门禁 A/解析），是本轮证据最扎实的模块之一。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/03-runtime.md` §8 | 描述门禁 B 三条件 | 与实现一致，无需更新 |
| `19-supply.md` §3（OI dormant） | 写 OI 覆盖不足标 dormant 不阻断 | 补一句"当前 check_gate_b 返回 None，dormant 仅语义未结构化返回"（G-2 说明缺口） |
| `s0/01`（Pin 契约） | Pin 九字段无 symbols 清单 | 与 `as_pin` 一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 加墙钟新鲜度（`max_age_days`） | ❌ 需改 schema + 本模块 | 当前无键、无 now()（G-1）；加则须先改 plan_schema，本模块再接 |
| 辅助数据集（清算/链上）门禁 | ⚠️ 半配置化 | `supply_meta` 三字段通用；但 dataset 名与 BAR_DATASET 判定硬编码（`:31`），新流是否逐位比标的需裁决 |
| OI dormant 结构化返回 | ❌ 当前不支持 | 仅 pass 跳过（G-2）；需改返回类型或加 context |
| 名义/计价货币维度（C5 BTCUSDT≠BTCUSD） | ❌ 无 | 本模块不处理 quote ccy；全局 C5 缺口，非门禁 B 落点 |
| 合约四元组身份（C4） | ❌ 无 | Pin 只有 symbols_digest；全局 P0-5，非本模块单独可补 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不算年化）
- **C3 spot/futures 维度**：✅ 强支撑——market 独立比对（实测 E4002），schema 枚举 spot/futures，禁 spot 带衍生数据集
- **C4 合约四元组**：❌ 无（全局 P0-5）；Pin 不含 expiry/settle
- **C5 BTCUSDT≠BTCUSD**：❌ 无（计价货币维度未处理，全局缺口）
- **C6 上市/下架**：✅ 标的集"相等"只用于 ohlcv（基座），辅助数据缺标的视为事实（`:190-195`）；新鲜度防旧版本
- **C7 24/7 缺口**：✅ 末端闭合 `end_closed`（E4003）；不靠交易日历
- **C11 流动性分层**：不适用（本模块是供给门禁，分层在 boundary）

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（门禁 B 是 `--live` 接真数据的守门员）
- **R2 结论可信**：✅ 强支撑（标的集/新鲜度/维度三条件防"拿错数据还全绿"）
- **R7 fail-closed**：✅ 强支撑（六道检查全实测拒绝，零副作用，无降级）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 三条件核心——标的集 digest 相等、新鲜度 `acquired_at>=coverage.end`、供给维度 frequency/exchange/market 比对——全部真实调用验证；fail-closed 六道（门禁 A/覆盖/标的/新鲜度/末端/维度）逐一拒绝并返回明确错误码与 context；时区 Z/+00:00 等价、OI partial dormant 边界均实测。四件 P2 均为设计确认或低优先：G-1 墙钟新鲜度未定义（schema 无键，符合 F1）、G-2 OI dormant 未结构化返回（需确认意图）、G-3 辅助数据只要求非空（符合设计）、G-4 解析容错（Pin 契约保证）。**生产可用（能真挖因子）= 是**：门禁 B 正确拦住错数据、放行对数据。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 36 个模块 `runtime/store.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 G-2（OI dormant 是否结构化返回）→ 告诉我
