# 模块审查 61 · `app/cli/stations.py`

> 审查日期：2026-11-18
> 依据：源码 17 行全文通读；上游 `orchestration/__init__.py`（STATION_SPECS）、`runtime/compiler.py`（STATIONS 常量）、`cli/common.py`（envelope_ok）；设计 `s0/05-orchestration.md` O1（工位规格自省）、`09-app-storage.md` §2；实测 `venv/Scripts/python.exe -m app.cli stations`。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`stations.py` 是**八工位规格自省**的只读命令（O1 用），回答一件事：「这套系统有哪些工位、一共几个」。

它应然：
1. 列出工位名清单（来自 `compiler.STATIONS`，与执行器实际顺序一致）。
2. 给出工位数量（来自 `orchestration.STATION_SPECS`）。
3. 只读，零参数（除 `--format` 展示参数），不读数据、不碰口径、不产 ticket。
4. 返回统一信封（envelope_ok），`run_id=None`（自省命令不绑定某次执行）。

**边界止于自省**：设计 `09-app-storage.md:83` 说 stations 要列「八工位规格、默认实现、名字空间、副作用等级」——但本模块当前只返回名字 + count，更细的规格（impl/mutation/输入）不在信封里。这是应然与实现的差距（见 D2 T-1），属「接口应然」问题而非 bug。

**加密视角**：本模块零加密语义；唯一价值是给研究/CI 一个「当前工位拓扑」的机器可读入口——加第九工位时此命令是第一个该反映变化的观测点。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 真实调用，非推演。

### 例 1：`flow stations` 成功（真实打印）

```
$ venv/Scripts/python.exe -m app.cli stations
{"ok": true, "command": "stations", "run_id": null,
 "data": {
   "stations": ["boundary","panel","target","search","evidence","verdict","portfolio","certify"],
   "count": 8
 },
 "warnings": [], "exit_code": 0}
EXIT=0
```

- `stations` 来自 `STATIONS`（`compiler.py` 常量，小写名），8 个。
- `count` 来自 `len(STATION_SPECS)`（orchestration），也是 8。
- 两者实测一致（8==8）——若两处不同步，会出现「列了 9 个但 count=8」的自相矛盾；有测试守卫（见下）。

### 例 2：`--format` 三格式（真实 help）

`stations --help` 实测：`usage: flow stations [-h] [--format {table,json,ndjson}]`，唯一参数 `--format`。无口径参数（P1 守卫无触发面）。

### 例 3：无失败路径

本模块无输入文件、无参数校验、无数据访问——除渲染器异常（未知 format 在 `render()` 抛 ValueError→main 转 E1004）外无业务失败路径。实测正常路径 exit 0。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| T-1 | **只返回名字 + count，未返回设计要求的「默认实现/名字空间/副作用等级」**。 | P2（接口完整度） | `stations.py:14-17` 仅 `stations`/`count`；`09-app-storage.md:83` 要求列规格/默认实现/副作用等级 | **需确认意图**：`STATION_SPECS` 里其实有 impl/mutation/input 等信息（`orchestration/station.py`），但本模块未暴露。若 O1 验收只看「工位名 + 数」，现状够；若要「规格自省」，需补字段。属 D5 设计对齐问题，非 bug。 |
| T-2 | **`stations` 列表来自 compiler 常量，`count` 来自 orchestration 规格——两个真相源**：若新增工位只改一处，会出现名字与 count 不一致。 | P2（一致性） | `stations.py:7-8` 两 import；实测 8==8 一致 | **需确认**：有 `test_station_names_match_specs` 守卫（见 catalog.py:208 注释提及），但那守卫在 storage.catalog，非本模块/compiler 处。两源不同步风险低但存在。建议统一从一个源派生（如都用 STATION_SPECS 的名字）。 |
| T-3 | **小写名（boundary）与文档/`inspect` 的 TitleCase（Boundary）不一致**：`STATIONS` 给小写，`inspect` 的 `station` 子命令 help 写「Boundary/Panel/...」，catalog 的 `_STATION_NAMES` 也是 TitleCase。 | P2（命名） | 实测 stations 输出小写；`inspect.py:101` help TitleCase；`catalog.py:209-212` TitleCase | **需确认意图**：目录名用 TitleCase（`01-Boundary-...`），协议名用小写——两套命名在不同层各有理由（路径可读 vs 配置键统一）。但跨命令对照时需人工转换，轻微体验问题。 |

**多了什么**：无。17 行全是必要实现。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 命令自省（introspection endpoint） | 采纳 | `flow stations` 给 CI/研究一个机器可读的工位拓扑入口（O1），比翻源码查 STATIONS 可靠。 |
| 从单一真相派生（SSOT） | **应加强（T-2）** | 当前名字/count 两源，建议统一从 STATION_SPECS 派生，避免新增工位时漂移。 |
| 在自省里暴露完整规格（impl/mutation/input） | **待裁决（T-1）** | 设计文档要求，但本轮实现只给最小集；若 O1 验收需要，补字段即可（STATION_SPECS 已有数据）。 |
| Web 端点暴露工位 | 拒绝 | 本轮不交付 Web（plan §0.0），CLI 自省足够。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **只读零副作用**：无 writer、无 layout 数据访问、无日志安装——幂等，任意次数调用输出一致（实测）。
2. **信封统一**：`envelope_ok("stations", None, {...})`（`:14-17`），实测 ok=True/exit=0/warnings=[]。
3. **无口径参数**：唯一 `--format` 是展示参数，P1 守卫不触发。
4. **数量与清单实测一致**：8==8，未出现自相矛盾。

**发现的问题**：
- 无 P0/P1 级 bug。T-1/T-2/T-3 为 P2 接口完整度/一致性/命名问题，不影响自省基本功能。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md:83` stations 条目 | 声称列「规格、默认实现、名字空间、副作用等级」 | 若维持 T-1 现状，改文档为「列工位名 + 数量」；若要满足设计，补 `specs` 字段（T-1 二选一）。 |
| `s0/05-orchestration.md` O1 | 若 O1 验收要求完整规格 | 同上裁决；当前实现只满足最小集。 |
| 命名约定文档 | 小写协议名 vs TitleCase 目录名未说明 | 补一句命名约定（T-3），消解跨命令对照困惑（说明性）。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增第九工位 | ✅ 自动反映（若两源同步改） | `STATIONS` 与 `STATION_SPECS` 都加后，本命令自动列出 9 个/count=9；若只改一处会不一致（T-2）。 |
| 暴露工位完整规格 | ⚠️ 需改代码 | `STATION_SPECS` 有数据，只需在 envelope data 加 `specs` 字段（T-1）；零外部依赖。 |
| 按 mutation 过滤工位 | ❌ 不支持 | 无参数；加过滤需改 parser + 本模块（低价值）。 |
| 机器可读格式扩展 | ✅ 已支持 | `--format json/ndjson/table` 由 render 层统一处理，本模块无需改。 |

---

## C1–C12 相关项判定

- **C1–C12 全部不适用**：本模块是只读工位自省，不读数据、不算年化/资金费/判据、不下口径。唯一相关是「工位拓扑观测点」对 C8（列集开放）等扩展的中性支撑——但本模块本身不判定加密事实。

## R1–R7 影响

- **R1 真数据跑通**：中性（不接触数据）。
- **R2 结论可信**：中性（不接触结论）；但作为 O1 自省入口，帮助确认「当前跑的工位拓扑」可信（T-2 两源一致时）。
- **R3 因子零代码扩展**：✅ 弱正向——加新能力若需新工位，本命令是观测点；但工位本身不轻易加（plan U5 「新工位不做」）。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：中性（无失败路径可守；只读幂等本身不引入风险）。

---

## 结论

**本模块是 17 行只读工位自省命令，无 P0/P1 级 bug，可确认通过。** `flow stations` 实测返回 8 工位名 + count=8（两源一致）、统一信封 ok/exit0、零副作用、无口径参数。3 项 P2：T-1（只给名字+count，未达设计文档「完整规格自省」）、T-2（名字/count 两源，新增工位有漂移风险）、T-3（小写协议名 vs TitleCase 目录名不统一）——均为接口完整度/一致性问题，不影响「生产可用=能真挖因子」。若要严格对齐设计文档，建议裁决 T-1 是补 `specs` 字段还是改文档。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 7 个模块 `cli/inspect.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ T-1 想当场裁决（补完整规格字段 vs 改文档缩小承诺）→ 告诉我
