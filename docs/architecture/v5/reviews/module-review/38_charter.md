# 模块审查 38 · `v5/runtime/charter.py`

> 审查日期：2026-11-18
> 依据：源码 53 行全文通读；依赖 `contracts/errors.py`（E1008/FlowError）。设计 `s0/03-runtime.md` §3（章程加载）、`04-runtime.md`；测试 `test_v5_runtime_real.py:412-443`。判据 plan §0.0/§0.1。
> **本轮只读审查，未改代码。** 真实调用见 `C:\Users\lansa\AppData\Local\Temp\fm_review_34_43\probe_u4.py`（CH 段）。

---

## D1 · 应然（抛开实现，这个模块该做什么）

Charter 是**编译期章程加载器**（`:1-5`），回答"baseline / plan / catalog 三类章程文件如何安全读入"：

1. **编译期只读，不属任何执行**：加载后交给 compiler 做深合并与校验；本模块不读数据、不写文件、不取时钟。
2. **四类统一 JSON 读取器**（`load_json`，`:16-30`）：文件不存在、JSON 解析失败、顶层非对象——三种失败**一律 E1008**，不静默返回空（缺必填/未解析占位）。三个加载器（baseline/plan/catalog）共用这一个 reader（`:33-53`），避免规则分叉。
3. **baseline 单文件、不做 preset 继承**（Q3 已收敛，`:37-43`）：出现 `inherits`/`extends`/`preset` 任一键即 E1008，避免"同一路径多处定义"的第二真相；缺 `baseline_version` 即 E1008（`:35-36`）。
4. **plan / catalog 不强加结构**：只保证合法 JSON 对象，结构校验交给 compiler 与实现注册表（职责边界，不在本模块做 schema 校验）。

**章程裁判边界**（用户必查点）：charter 是**加载边界**不是**裁判边界**——它只保证"文件可读、是对象、baseline 单文件"，不判断配置是否合法（那是 compiler 的 C1–C22）、不判断实现是否存在（那是 bridge 的 `_resolve_impl`）、不判断数据是否可用（那是 gate_b）。跨这条边界的判定必须 fail-closed（E1008），不能把"读不到"伪装成"读到空的合法章程"。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测，复现脚本 `probe_u4.py` CH 段，临时根 `...\data_u4`。

### 例 1：三类加载器正常读写（真实输出）

```
baseline : {'baseline_version': 'v1'}   （load_baseline）
plan     : {'k': 'plan'}                （load_plan）
catalog  : {'k': 'catalog'}             （load_catalog）
```
读法：三个加载器都返回 dict；baseline 额外校验 `baseline_version` 存在。

### 失败输入 → 错误输出（全部实测）

| 非法输入 | 输出（错误码\|消息） |
|---|---|
| `load_baseline(不存在路径)` | `E1008\|baseline 文件不存在: ...\none.json` |
| `load_plan(不存在路径)` | `E1008\|plan 文件不存在: ...\none.json` |
| `load_catalog(不存在路径)` | `E1008\|catalog 文件不存在: ...\none.json` |
| `load_plan("{bad")` | `E1008\|plan JSON 解析失败: Expecting property name ... line 1 column 2` |
| `load_plan("[1,2]")`（顶层 list） | `E1008\|plan 顶层必须是对象，得到 list` |
| baseline 含 `inherits` | `E1008\|基线不得声明 'inherits'（基线单文件，不做 preset 继承）` |
| baseline 含 `extends` | `E1008\|基线不得声明 'extends'（...）` |
| baseline 含 `preset` | `E1008\|基线不得声明 'preset'（...）` |
| baseline 缺 `baseline_version` | `E1008\|baseline 缺 baseline_version` |

读法：三类"读不到/读不对"全部 E1008 且带 label（baseline/plan/catalog）与 path，便于定位是哪份章程出问题；preset 继承三键逐一拒绝，坐实"第二真相"防线。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| CH-1 | **load_plan / load_catalog 不校验结构**：只读成 dict 即返回，plan 的非法键/类型由 compiler 的 validate_plan 拒、catalog 的 station→name 结构由 implementation registry 消费时校验。 | P2（职责边界） | `charter.py:47-53`；`compiler.py:146` | **已确认符合设计**（`s0/03:261` 明确 charters 只加载、结构校验在下游）。若 charters 也校一遍会造成两套规则。 |
| CH-2 | **无文件编码/BOM 特殊处理**：`read_text(encoding="utf-8")`，带 BOM 的 JSON 会解析失败 E1008（UTF-8-BOM 首字符）。 | P2（低） | `charter.py:20` | **需确认意图**：章程文件由系统生成（utf-8 无 BOM），人工编辑若存成 BOM 会 E1008 而非自动剥离。属低优先容错，不阻塞。 |
| CH-3 | **load_json 返回可变 dict**：调用方可就地改，无冻结/不可变保证。 | P2（确认设计） | `charter.py:16-30` | **已确认**：compiler 会 `deepcopy(dict(plan))`（`compiler.py:125`）保护调用方入参，charter 返回的 dict 只是传输载体；冻结由 compiler 的 CompiledPlan 承担。 |

**多了什么**：无冗余。三加载器共用 `load_json`（`:16`）消解了"四个加载器各写一遍"的历史风险（`test_v5_runtime_real.py:428-443` 专门钉此边界）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 单一 JSON reader + 三个薄封装 | 采纳 | 规则只一份，三类文件失败语义一致（实测三缺文件全 E1008） |
| 缺文件/坏 JSON/非对象统一 E1008 | 采纳 | "读不到"不是"读到空"，fail-closed 不放行 |
| baseline 单文件、拒 preset 继承 | 采纳 | 加密因子口径需单一真相；继承会让同一路径多处定义（Q3 收敛） |
| 在加载层做 schema 结构校验 | **拒绝** | 结构校验归 compiler（C1–C22），双层会造成两套规则、哪套生效要读代码 |
| 配置文件热加载/占位符解析 | **拒绝** | 编译期只读，"未解析占位"即 E1008（`:4`） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **三个加载器共用 load_json**：文件不存在/坏 JSON/非对象三类失败全 E1008（实测九种输入逐一拒绝）。
2. **错误带 label + path**：如 `baseline 文件不存在: ...`、`plan JSON 解析失败`，定位明确（实测）。
3. **baseline 单文件**：`inherits/extends/preset` 三键逐一拒（实测），缺 `baseline_version` 拒（实测）。
4. **顶层对象校验**：`[1,2]` → E1008（实测），防后续 `data.get` 炸在类型错误。
5. **纯读取**：无写文件、无 now()、无 dir 创建（`:16-53` 全文）。

**发现的问题**：CH-1~CH-3 均为 P2/职责边界或低优先，**无 P0/P1 级代码 bug**。测试覆盖三类丢失、preset 继承、共享 reader（`test_v5_runtime_real.py:414-443`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/03-runtime.md` §3 | 描述 baseline/plan/catalog 四类与共用 reader | 与实现一致（三类加载器，方案 B 后无第四别名加载器），无需更新 |
| `04-runtime.md` | 章程加载职责 | 一致，无需更新 |
| （说明性补注） | — | 若有人误以为 charter 做 schema 校验，可在 §3 补"结构校验在 compiler，本层只保证可读/是对象/单文件"——消解 CH-1 误解 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增章程类型（如 schema 文件加载器） | ⚠️ 需改代码 | 加一个 `load_xxx` 薄封装调 `load_json`（ `:47-53` 模式），约 3 行；成本极低 |
| baseline preset 继承（若将来允许） | ❌ 明确拒绝 | Q3 收敛为单文件；放开须先改设计并更新 `s0/03` §3 |
| 结构校验前移到加载层 | ❌ 设计拒绝 | 归 compiler，双层会分叉（CH-1） |
| BOM / 非 utf-8 编码容错 | ❌ 当前不支持 | 人工编辑存成 BOM 会 E1008（CH-2，低优先） |

---

## C1–C12 相关项判定

- **C1–C12**：本模块为章程加载器，不直接涉加密业务口径。逐条不适用（年化/资金费/市场/合约/溢价/上市/缺口/列集/as-of/判据/分层/embargo 均不在加载层处理）。
- 间接支撑 **R7 fail-closed**：读不到/读不对一律 E1008，不把缺失伪装成空章程。

## R1–R7 影响

- **R1 真数据跑通**：中性（章程加载是编译前置，正确加载才能进 compiler）
- **R2 结论可信**：✅ 正向（baseline 单文件 + 缺必填 E1008，口径来源单一可追）
- **R7 fail-closed**：✅ 强支撑（三类文件失败 + preset 继承全 E1008，实测）

---

## 结论

**本模块代码质量低复杂度、无 P0/P1 级 bug，可确认通过。** 章程裁判边界清晰——只做"可读/是对象/baseline 单文件"三件事，结构校验下放 compiler、实现校验下放 bridge、数据校验下放 gate_b；实测九种非法输入全 E1008、三加载器共用 reader、preset 继承三键全拒。三项 P2 均为职责边界或低优先容错（CH-1 不校结构符合设计、CH-2 BOM 不容错、CH-3 返回可变 dict 由 compiler 保护）。**生产可用（能真挖因子）= 是**：章程加载可靠，不阻塞真实编译与执行。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 39 个模块 `capability_bridge.py`
- ✏️ 需补充/修正某维度 → 告诉我
- ⚠️ 想裁决 CH-2（BOM 容错）→ 告诉我
