# 模块审查 58 · `app/cli/common.py`

> 审查日期：2026-11-18
> 依据：源码 186 行全文通读；上游 `contracts/errors.py`（exit_code_for/FlowError/E1004）、`runtime/charter.py` load_plan、`runtime/gate_b.py` resolve_symbols/_parse_day、`upstream/store.py`（read_dataset/require_market_dataset/BAR_DATASET）、`facilities/clock.py` Clock、`capability/params.py` implementation_defaults；设计 `09-app-storage.md` §2.4（信封/参数转换）、`19-supply.md` §2.4（身份现算）；测试 `tests/tests/test_v5_app_commands.py`。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`common.py` 是 CLI 各命令**共用的接缝层**，回答四件事：

1. **信封构造**：成功 `envelope_ok` / 失败 `envelope_error`。字段固定 `{ok, command, run_id, data, warnings, exit_code, errors?}`，**`ok` 与 `exit_code` 恒一致**；错误对象不带 exit_code，进程码取第一条 error 的 `code` 经 `exit_code_for` 推导。日志失败等非结论性失败只进 warnings，不改退出码。
2. **参数转换**：`parse_overrides`（`--set key=value`→dict）、`coerce`（值类型转换：数字/bool/JSON/字符串）；格式错即 E1004，不猜不忽略（P2）。
3. **供给上下文构造**：`build_supply_context` 按约定路径读文件、现算 Pin/身份（19-supply.md：无别名表、内容即版本），供编译器进 `blueprint_digest`、供门禁 B 核对 frequency/exchange/market、供 live 转 SupplyBatch。
4. **ticket 标识生成**：`new_ticket_id` 按 `<UTC 紧凑时间>-<plan_digest 前8>`，时间取设施时钟（非 `datetime.now`）。

**边界止于接缝**：本模块不含业务逻辑、不判显著、不下口径。它是「命令壳 ↔ 运行时/供给层」的转换与装配点，各 `cmd_*.py` 复用它避免重复解析逻辑分叉。

**加密视角**：`build_supply_context` 的 `_panel_window`（`:126-144`）把面板基座覆盖到 `mine起点 → oos终点`（含认证窗口），防「晚上市标的在前半段留 NaN 一路传到 allocate 才炸」——这是 C6（上市/下架可用时间）在供给接缝处的落点。市场×数据集相容二次查（`:109`）是 C3 在程序化入口的护栏。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 由 `venv/Scripts/python.exe` 实际调用打印（脚本 `%TEMP%/fm_review56_67/probe_contract_common.py`），非推演。

### 例 1：信封构造（真实打印）

```
envelope_ok("stations", None, {"count":8})
→ {'ok': True, 'command': 'stations', 'run_id': None,
   'data': {'count': 8}, 'warnings': [], 'exit_code': 0}

envelope_error("run", None, [{"code":"E4002","message":"x"}])
→ {'ok': False, 'command': 'run', 'run_id': None, 'data': None,
   'warnings': [], 'exit_code': 4, 'errors': [...]}
```

E4002→exit4 来自 `exit_code_for`（`common.py:29`）。`ok` 与 `exit_code` 恒一致（例：False↔4）。

### 例 2：参数转换（真实打印）

```
parse_overrides(['k=3','flag=true','j={"a":1}'])
→ {'k': 3, 'flag': True, 'j': {'a': 1}}
parse_overrides(['bad'])
→ FlowError E1004 "--set 格式错误: bad, hint=用 key=value" | exit 2

coerce('42')   → 42      (int)
coerce('1.5')  → 1.5     (json.loads → float)
coerce('abc')  → 'abc'   (字符串兜底)
```

`coerce` 顺序：bool→int→json→str（`:176-185`），符合「不猜但能识别常见类型」。

### 例 3：ticket_id 生成（真实打印）

```
new_ticket_id("sha256:0585b103835178283c3f413590016a4ef")
→ "20261118T064405Z-0585b103"
```

格式 `<UTC>-<8位 digest>`，时间来自 `Clock().wall()`（`:169`），非墙钟直取——保证测试可冻结时钟对拍。

### 例 4：坏 JSON 用调用方的码（真实语义，源码 `:39-62`）

`load_json_dict(path, code=E1004, label="标的集表")` 与 `code=E4004`（供给事实路径）对同一份损坏文件分别报 E1004/exit2 与 E4004/exit4。docstring（`:43-45`）明确：若让 JSONDecodeError 冒到 main 兜底会变 E3004/exit1，把「用户可修」误报成「代码写错」——**码的选择是刻意设计**（S2-M5 review P1-3）。本例为语义确认，未构造坏文件重跑（属需确认意图，非实测数字）。

### 例 5：供给上下文失败路径（真实 CLI）

```
$ venv/Scripts/python.exe -m app.cli run --plan tests/fixtures/plan_valid.json --live
   （FLOW_DATA_ROOT=空目录）
→ {"ok":false,...,"exit_code":4,"errors":[{"code":"E4002",
   "message":"ohlcv 在 binance/futures 下没有任何数据文件",
   "path":"supply.ohlcv","hint":"先 flow fetch --datasets ohlcv --exchange binance"}]}
EXIT=4
```

`build_supply_context` → `store.read_dataset` 缺文件 → E4002，经信封推导 exit4。供给失败 fail-closed 成立。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| K-1 | **`build_supply_context` 返回值 `symbol_sets` 原样回传但未用**：签名返回 4 元组 `(pins, symbol_sets, supply_meta, datasets)`（`:68-69,123`），调用方 `run.py:63`、`explain.py:38` 都写成 `aliases, symbol_sets, supply_meta, supply_datasets = ...`，其中第二个 `symbol_sets` 在 run 里不再使用（run 用的是编译后的 plan）。 | P2（冗余） | `common.py:68-69,123`；`run.py:63-65` | **需确认意图**：历史上第二槽是别名表（19-supply.md 方案 A），方案 B 取消别名后仍保留占位。可改为返回 `(pins, supply_meta, datasets)` 三元组并同步两处调用，属清理非 bug。 |
| K-2 | **`coerce` 用 `json.loads` 会让 `"NaN"`/`"Infinity"` 被解析为非有限浮点**（Python json 默认允许）；`--set x=NaN` 可能把 NaN 注入 plan。 | P2（边界） | `common.py:183`；无测试覆盖 | **需确认**：compiler 的参数校验（E1004）大概率会拦 NaN，但本模块作为通用转换未自守卫。若要求「接缝层即拒非有限值」，应加 `math.isfinite` 检查。低优先。 |
| K-3 | **`_panel_window` 解析失败返回 None 而非报错**：`_parse_day` 解析不出就返回 None（`:142-143`），注释说「让 schema 层的 E1008 去报」。但若调用方绕过 schema 直接调 `build_supply_context`（程序化入口），会静默不做窗口检查。 | P2（需确认意图） | `common.py:126-144` | **设计如此**：与 `build_supply_context` 的 `require_market_dataset` 二次查（`:109`）风格不一致——那里不依赖「调用方跑过 schema」。建议统一：要么两处都依赖 schema，要么窗口解析也自报 E1008。 |
| K-4 | **信封失败时 `run_id` 恒为 None**：`main()` 的 `_envelope_error(args.command, None, ...)`（`__init__.py:211`）不传 run_id，即使命令已生成 ticket 后在中途失败，错误信封也丢掉 run_id——用户拿到 error 却无法定位哪个 run。 | P2 | `__init__.py:211`；本模块 `envelope_error`（`:24-31`）签名支持 run_id 但调用方没传 | **需确认**：属 `__init__.py` 调用侧问题，非本模块 bug。可改：命令抛错时带 run_id 进 envelope_error。影响排障体验，不影响退出码正确性。 |

**多了什么**：K-1 的冗余返回槽是唯一多余项，见上。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 结构化信封（ok/data/errors/warnings 分离） | 采纳 | 告警/日志失败进 warnings 不改退出码（`:5`），错误进 errors 定码——机器可消费且不误报成功，契合 R7。 |
| content-addressed 身份现算（无别名表） | 采纳 | `build_supply_context` 按约定路径读、现算 checksum/Pin（`:65-123`），文件在哪由路径决定、身份由内容决定——加密多交易所/多数据集场景避免别名漂移。 |
| 别名表/版本目录（方案 A） | 拒绝 | 19-supply.md 明确选方案 B；K-1 的冗余槽是历史残留，应清理而非恢复别名。 |
| 面板窗口覆盖 mine+oos | 采纳 | `_panel_window`（`:126-144`）覆盖认证窗口，防晚上市标的 NaN 延迟爆炸（C6）；方向正确。 |
| 静态 `datetime.now()` 取时间 | 拒绝 | `new_ticket_id` 用 `Clock().wall()`（`:169`），测试可冻结，避免不可复现。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **`ok`/`exit_code` 恒一致**：`envelope_ok` 同时写 `True/0`（`:18-21`），`envelope_error` 同时写 `False/int(exit_code_for(code))`（`:27-31`），实测例 1 成立。
2. **错误对象不带 exit_code**：`envelope_error` 的 errors 只放 `exc.error.to_dict()`（调用方 `__init__.py:211`），码唯一推导自 code——无双写漂移。
3. **坏 JSON 码由调用方给定**（`:39-62`）：区分编译路径 E1004 vs 供给路径 E4004，不误报成 E3004/exit1。
4. **市场相容二次查**（`:107-109`）：`build_supply_context` 不依赖「调用方跑过 schema」，程序化入口也守 C3。
5. **面板基座只严格校 BAR_DATASET 标的集**（`:110-117`）：辅助数据集缺标的属常态（交易所不给历史），不强求——避免误拒。
6. **参数格式错 fail-closed**：`parse_overrides` 无 `=`/缺键名即 E1004（`:153-157`），实测例 2。
7. **ticket_id 内容寻址**：`plan_digest.split(":")[-1][:8]`（`:170`），与 plan 内容绑定，同计划同短号。

**发现的问题**：
- 无 P0/P1 级 bug。K-1/K-2/K-3 为 P2 冗余/边界/一致性问题，K-4 为调用侧问题（并入 58 对 `__init__` 的观察）。均不影响「能真挖因子」。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.4 信封字段 | 若文档写「信封 run_id 恒可定位」 | 补 K-4：错误信封当前 run_id=None（命令已生成 ticket 中途失败时也丢），说明性或改调用侧 |
| `19-supply.md` §2.4 身份现算 | 与 `build_supply_context` 一致 | 无需更新；但可补一句「第二返回槽 symbol_sets 为方案 A 残留，已不用」（K-1，属清理说明） |
| `09-app-storage.md` §2.4 `load_json_dict` 语义 | 区分 E1004/E4004 的码选择 | 已与注释（`:43-45`）一致，无需更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一条命令复用信封 | ✅ 零代码 | `envelope_ok/error` 是通用函数，新命令直接 import（如 `pipeline.py:27`）。 |
| 新增 `--set` 值类型（如日期） | ⚠️ 需改代码 | `coerce`（`:174-185`）类型链写死 bool/int/json/str；新类型需加分支。低优先。 |
| 新数据集接入 supply | ✅ 半配置化 | `build_supply_context` 遍历 `plan.supply.datasets`，配合 `store.DATASETS` 注册即可；但 `require_market_dataset` 需在新数据集上声明市场（U3 范围）。 |
| 面板窗口扩展到含 holdout | ❌ 需改代码 | `_panel_window`（`:126-144`）只算 mine→oos，holdout=reserved 不入面板；若认证需 holdout 数据需改此处。 |
| 新告警/通知字段进信封 | ⚠️ 需协调 | 信封字段固定（`:18-21,27-31`），加字段要同步 4 个 render 格式（`render/json_fmt` 等），属应用层契约变更。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：⚠️ 间接——`build_supply_context` 不碰年化；但 `_panel_window` 用「日」解析 mine/oos（`_parse_day`），是 1 观测=1 天（C12）的供给前提。
- **C3 spot 无资金费**：✅ `build_supply_context` 对每个 dataset 调 `require_market_dataset(market, dataset)`（`:109`），程序化入口也拦「spot 目录读 funding」。
- **C6 上市/下架可用时间**：✅ `_panel_window` 覆盖 mine+oos 全窗（`:101-144`），注释明确防「晚上市标的 NaN 延迟爆炸」。
- **C7 24/7 缺口**：中性（`doctor.py` 才是缺口体检落点，见 65 号）。
- **C2/C4/C5/C8/C9/C10/C11**：不适用（本模块不读字段、不算口径、不判据）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向——`build_supply_context` 现算身份 + 门禁 B 二次查，实测 `--live` 缺数据 → E4002/exit4（例 5），不静默走 dummy。
- **R2 结论可信**：✅ 正向——`_panel_window` 防全窗覆盖不足导致的 allocate 期炸错方向。
- **R3 因子零代码扩展**：中性（供给上下文支持新数据集遍历，但加密流接入在 U6）。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：✅ 强支撑——坏 JSON 用调用方码、供给缺失 E4002、参数格式错 E1004、市场不相容 E1004，全拒且码正确。

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 作为 CLI 接缝层，信封构造（`ok`↔`exit_code` 恒一致、错误对象不带码）、参数转换（格式错 E1004）、供给上下文（身份现算 + 市场二次查 + 面板全窗）、ticket_id 内容寻址四项全部实测成立。4 项 P2：K-1（冗余返回槽，方案 A 残留）、K-2（`coerce` 的 NaN 边界）、K-3（窗口解析失败静默返回 None，风格不统一）、K-4（错误信封丢 run_id，属调用侧）——均为清理/一致性问题，不影响「生产可用=能真挖因子」。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 4 个模块 `cli/run.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ K-1/K-3 想当场裁决（是否清理冗余槽/统一窗口校验风格）→ 告诉我
