# 模块审查 65 · `app/cli/doctor.py`

> 审查日期：2026-11-18
> 依据：源码 224 行全文通读；上游 `upstream/store.py`（read_dataset/DATASETS/BAR_DATASET/scan_symbols/Pin）、`runtime/charter.py` load_plan、`storage/layout.py`、`cli/common.py`（envelope_ok/load_plan）；设计 `19-supply.md`（供给体检）、`15-operations.md` §3、`09-app-storage.md` §2；实测 `venv/Scripts/python.exe -m app.cli doctor`（空数据根 + 真供给 runroot）；测试 `tests/tests/test_v5_live_cli.py`。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`doctor.py` 是**供给体检**的只读命令：扫数据根下所有 `<dataset>/<exchange>/<market>/`，逐组现算身份并回答四个问题（模块 docstring `:3-18`）：

1. **有没有数据**：文件数、标的数、总行数。
2. **覆盖到哪**：coverage 起止、interval_ns（实测中位步长）、end_closed。
3. **有没有洞**：gaps 逐段列出（缺口不是「少几行」，是「这段时间的结论不可信」）。
4. **能不能用**：`passed` = 末端闭合 且 无缺口。

三条「成功」判断自己写，不让调用方猜：
- 目录存在但读出来零行 → `empty`，不是 ok；
- 有缺口 → `reason_code=E4002`，不是 warning；
- 标的行数差异过大（少于中位 50%）→ 单列 `thin_symbols`，因分位/覆盖率分母会被拖偏。

`--plan` 时额外做「计划要的标的 vs 实际有的标的」比对，缺一个就报（因为 `read` 给清单会 fail-closed，等到 run 才发现浪费一次完整执行）。

**边界止于体检**：只读、不改数据、不下结论（「能不能用」交给 pipeline 的 blocking 判定，见 67 号）。体检结果全在信封的 checks/warnings/summary 里。

**加密视角（必查点 启动自检/环境依赖检查）**：24/7 加密数据无交易日历，「未收盘」靠 `end_closed`（供给事实）判，不靠日历（C7）；缺口（维护/合约迁移）逐段列出而非 ffill；薄标的（晚上市/低流动性币）单独标——都是加密特有的体检维度。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 真实调用 `venv/Scripts/python.exe -m app.cli doctor ...`，非推演。

### 例 1：空数据根失败（真实）

```
$ FLOW_DATA_ROOT=<空目录> venv/Scripts/python.exe -m app.cli doctor
→ {"ok":false,"exit_code":2,"errors":[{"code":"E1008","layer":"usage",
   "message":"数据根下没有任何已采集数据集: C:\\...\\emptyroot",
   "path":"doctor.data_root","hint":"先 flow fetch --datasets ohlcv --exchange <id> --start .. --end .."}]}
EXIT=2
```

`_datasets_in(root)`（`:52-53`）返回空 → E1008/exit2，fail-closed（不返回「体检通过但无数据」）。

### 例 2：真供给体检（真实，59 号 runroot）

```
$ FLOW_DATA_ROOT=<runroot> venv/Scripts/python.exe -m app.cli doctor
→ {"ok":true,"exit_code":0,"data":{
   "datasets":[4 个数据集],
   "checks":[... 4 组 ...],
   "warnings":[...],
   "summary":{"datasets":4,"groups":4,"rows":<总行数>,"passed":?},
   "plan":null}}
```

`pipeline` 正常路径实测（67 号 probe）：`doctor groups=4, warnings=1`（1 条 warning 是薄标的或末端判断，非 blocking），`blocking=[]` → 体检通过，pipeline 继续 run。

### 例 3：thin_symbols 告警逻辑（真实代码语义，`:61-68`）

```
median = sorted(rows.values())[len(rows)//2]      # 标的行数中位数
if median and n*2 < median: thin.append(sym)      # 少于中位一半 → 单列
```

docstring（`:14-15`）解释：分位/覆盖率的分母会被薄标的拖偏。这是加密池「20 个标的上市时间不一」的典型场景（C6）。

### 例 4：`--plan` 标的比对（源码 `:176-208`，语义确认）

`_plan_symbols`（`:94-114`）从 `schemas/symbol_sets.json` 读计划声明的标的集成员；与 `scan_symbols` 扫到的实际标的做差集 → `missing_symbols`。缺数据集/缺标的进 warnings（E4002），不直接改退出码（体检是报告，裁决在 pipeline）。本例 runroot 的 plan 比对未触发缺失（数据齐全）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| D-1 | **`--plan` 时 `load_plan(args)` 只读 plan 文件，但 `doctor` 的 parser 里 `--plan` 非 required（`__init__.py:82`）**：若用户给了坏 plan（JSON 损坏），`load_plan` 会抛 E1004/exit2，体检中断——合理（坏 plan 是该报错）。无差距。 | — | `__init__.py:82`；`doctor.py:179` | 符合应然。 |
| D-2 | **`_plan_symbols` 读 symbol_sets.json 用 `except json.JSONDecodeError: return None` 静默跳过**（`:104-108`）：标的集表损坏时体检不报，plan_check.symbol_set=None，`missing_symbols=[]` ——「计划要哪些标的」被静默忽略。 | P2（fail-open） | `doctor.py:104-108` | **需确认意图**：注释说「标的集表不在就返回 None（不阻塞体检）」，但「文件损坏」与「文件不存在」被同一分支处理。损坏应至少进 warning，否则用户以为比对过了。与 58 号 `load_json_dict`（坏文件用调用方码）的处理风格不一致。 |
| D-3 | **`passed = checks and all(passed) and not warnings`（`:210`）**：任何 warning（包括薄标的、plan 缺标的）都让 `passed=False`——但 pipeline 的 blocking 只看 `checks[].passed`（`pipeline.py:136-141`），不看总体 passed。两处对「passed」定义不同。 | P2（语义） | `doctor.py:210` vs `pipeline.py:136-141` | **需确认意图**：doctor 的 `summary.passed` 是「完全干净」的严格口径（含 warning），pipeline 的 blocking 是「数据故障」的宽口径（只看缺口/未闭合）——设计合理（67 号 docstring 明确），但同名 `passed` 两义易误读。建议改名 `summary.all_clean`。 |
| D-4 | **只体检磁盘已有数据集（`:52-53`），不体检「计划声明但完全没采」的数据集**——除非给了 `--plan` 且该数据集在 checks 里缺席。 | — | `doctor.py:52-53,118-124`；`--plan` 时 `:182-184` 补 missing_ds | 符合应然：无 --plan 就是「扫盘上有什么」；缺数据集检查靠 --plan 或 pipeline/run 的门禁 B。 |
| D-5 | **`gaps[:20]` / `thin_symbols[:10]` 截断**：缺口超 20 段只显示前 20，`gap_count` 给总数——摘要不丢信息（有 count），但看不到全部缺口位置。 | P2（可观测性） | `doctor.py:85,88` | **低优先**：终端可读性取舍，`--full` 之类开关未给。低影响。 |

**多了什么**：无。四问 + 三判断 + --plan 比对是必要体检项。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 供给体检前置（preflight check） | 采纳 | pipeline 把 doctor 放 run 前（67 号），「别用坏数据跑一次完整执行」——24/7 数据缺口隐蔽，前置体检是必要防线。 |
| 实测 interval_ns（中位步长）判频率 | 采纳 | 不靠文件名/配置，靠数据实测（`:83`）——加密多交易所频率不一，实测可靠。 |
| end_closed 判未收盘 | 采纳 | 24/7 无交易日历，靠供给末端事实（C7），不用 pandas-market-calendars。 |
| 缺口当 error 不当 warning | 采纳 | `reason_code=E4002`（docstring `:13`），缺口是「这段时间结论不可信」，不是小瑕疵。 |
| 交易日历判「未收盘/交易日」 | 拒绝 | 24/7 无日历（C7），end_closed 事实替代。 |
| 静默 ffill 缺口 | 拒绝 | gaps 逐段列出（`:69-73`），不填充——与 C9「只 ffill 不 bfill、缺口显式」一致。 |
| 坏标的集表静默跳过 | **应改（D-2）** | 当前 `JSONDecodeError → None` 与「文件不存在」同处理，至少应进 warning。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **空数据根 fail-closed**：E1008/exit2（实测例 1）。
2. **四问齐全**：文件/标的/行数、coverage/interval_ns/end_closed、gaps、passed/reason_code（`:74-91`）。
3. **thin_symbols 中位判定**：`n*2 < median`（`:67`），防分母拖偏。
4. **`--dataset` 过滤 + 未知数据集拒**：`:128-135`，未知 → E1008 带可用列表。
5. **逐层遍历 exchange/market**：`:139-148`，多交易所/多市场全扫（C4/C5 跨所前置）。
6. **ohlcv 按 timeframe 分组，事件流不分组**：`:144-147`，`_tf_of` 用 `_KNOWN_TF` 白名单防盲切错符号（`:32-42`）——细节到位。
7. **体检失败不崩全局**：`_check_one` 抛 FlowError 时进 warnings 并 continue（`:149-158`），一个坏组不阻断其它组体检。
8. **warnings 同时进信封顶层和 data**：`envelope_ok(..., warnings=warnings)`（`:211-223`），告警可见。

**发现的问题**：
- 无 P0/P1 级 bug。D-2（坏标的集表静默）、D-3（passed 两义）、D-5（截断）均为 P2，不影响体检基本功能。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `19-supply.md` 供给体检 | 若列「11 项体检」（doc `09:76` 写 doctor 体检 11 项） | 实测本模块体检 4 问 + 3 判断，非 11 项；统一文档口径（说明性） |
| `15-operations.md` §3 告警阈值 | doctor 的 warnings 与 pipeline 的 alerts 关系 | 补 D-3：`doctor.summary.passed`（含 warning 的严格口径）≠ `pipeline.blocking`（只数据故障），避免误读 |
| `19-supply.md` 标的集表读取 | 未说明损坏处理 | 补 D-2：当前坏表静默跳过（symbol_set=None），建议至少 warning（或改代码） |
| `docs/book` OI 25 行事实 | 已有记载（见 66 号） | doctor 会把 OI 25 行标为 thin_symbols（若启用 OI），与此事实一致，无需单独更新 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增数据集体检 | ✅ 半配置化 | `_datasets_in` 遍历 `store.DATASETS`，新数据集加入该常量后自动体检；`_check_one` 通用。 |
| 新增体检项（如 OI 行数下限） | ⚠️ 需改代码 | thin_symbols 有通用中位判定（`:61-68`），但特定数据集阈值需加分支。 |
| 体检结果导出报告文件 | ❌ 不支持 | 只在信封里；本轮不做文件报告（运维范畴，plan §0.0）。 |
| `--plan` 比对扩展（比数据集+标的更多维度，如频率） | ⚠️ 需改代码 | plan_check 只比 datasets/symbols（`:195-202`）；频率/市场比对在门禁 B（run 时），非 doctor。 |
| 缺口自动修复 | ❌ 不支持（设计禁止） | doctor 只读；缺口处置归人（补采），不自动 ffill。 |

---

## C1–C12 相关项判定

- **C6 上市首日/下架**：✅ thin_symbols 用中位行数（`:61-68`）标晚上市/低行数标的，防分位分母拖偏。
- **C7 24/7 无休市但有缺口**：✅ 强——gaps 逐段列出（`:69-73`）、end_closed 事实（`:84`）、不靠日历、不 ffill。
- **C3 market 维度**：✅ 遍历 exchange/market 目录（`:139-140`），跨所体检（C4/C5 前置）。
- **C1/C2/C4/C8/C9/C10/C11/C12**：不适用或中性（本模块不读字段值、不算年化/资金费/判据）。

## R1–R7 影响

- **R1 真数据跑通**：✅ 强支撑——体检是「数据能不能用」的第一道关，实测空根 E1008、真根 groups=4。
- **R2 结论可信**：✅ 强支撑——缺口/未闭合/薄标的显式暴露，防「在坏时间轴上跑出看似正常的结论」。
- **R3 因子零代码扩展**：中性（新数据集自动进体检遍历）。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：✅ 正向——空根 E1008、未知数据集 E1008、缺口 E4002（在 warnings/checks 中显式）；但 D-2（坏标的集表静默）是轻微 fail-open。

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 供给体检四问齐全、三判断自主、thin_symbols 中位判定、多交易所/市场遍历、缺口当 E4002 显式列出、空根 fail-closed——全部实测成立。5 项 P2：D-2（坏标的集表静默跳过，轻微 fail-open）、D-3（`summary.passed` 与 `pipeline.blocking` 同名两义）、D-5（gaps/thin 截断）等，均为一致性/可观测性问题，不影响「生产可用=能真挖因子」。作为「启动自检/环境依赖检查」的必查点，本模块是数据侧的体检落点（环境/依赖侧在 `app/cli/live.py` 与其它模块）。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 11 个模块 `cli/estimate.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ D-2 想当场裁决（坏标的集表是否改进 warning）→ 告诉我
