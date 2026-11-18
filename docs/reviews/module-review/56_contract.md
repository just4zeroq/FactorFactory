# 模块审查 56 · `app/contract.py`

> 审查日期：2026-11-18
> 依据：源码 164 行全文通读；上游 `contracts/errors.py`（E1004/E1020/exit_code_for/_EXIT_BY_LAYER）、`runtime/compiler.py`（CALIBER_FIELDS 唯一真相）；设计 `09-app-storage.md` §2.2–§2.4（L70–126）、`s0/01-data-contracts.md` §4；测试 `tests/tests/test_v5_app_commands.py:22-77`。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Contract 是**命令面唯一真相**：CLI 与编排层的边界契约，回答四件事：

1. **命令清单**：哪些命令存在、分几组（核心五条 S0 冻结 / 业务条 S1 起逐步）、别名映射（`experiment→run`）。新增/删除命令必须改这里，散落在 CLI 里的字面量不算。
2. **每条命令的元数据**：mutation 等级（read_only/writes_supply/writes_facts/writes_ledger）、是否产 Ticket、最大退出码、`data` 顶层键、参数 schema（字段名）。任何层想知道「这命令是否写账本」「是否该弹 --yes」只读这里。
3. **三条硬规则的静态守卫**：P1 口径字段不得进 CLI（→E1020）、P2 缺参报错不猜、P3 writes_ledger 以上必须 --yes（→E1004/exit2）。
4. **错误→退出码映射**：错误对象只带 `code/layer/retryable`，**不带** exit_code；退出码由 `exit_code_for(code)` 从层推导（E1→2 / E2→3 / E3→1 / E4→4 / E5→1）。5 个码语义互不重叠，3（契约违反=代码 bug）与 1（执行/环境失败）严格分开。

**边界止于声明**：本模块只声明元数据与静态守卫，不执行任何命令、不读数据、不碰口径值。真正的执行在 `cli/<name>.py`，渲染在 `render/`。

**加密视角**：本模块是纯应用层壳，C1–C12 加密事实不在此落点；但它守住「CLI 参数不得泄漏口径」（P1）——否则命令行能传 `--annualisation=252`，Web 一个请求就能绕开编译期一致性检查，把加密 365 年化悄悄改回股票 252。这是 C1 在应用层的唯一守卫。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下由仓库 `venv/Scripts/python.exe` 实际调用打印（`PYTHONPATH=d:/code/github/FactorMiner`，脚本在 `%TEMP%/fm_review56_67/probe_contract_common.py`），非推演。

### 例 1：命令清单与关键命令元数据（真实打印）

```
CORE_COMMANDS    = ('run', 'station', 'explain', 'inspect', 'stations')
BUSINESS_COMMANDS= ('doctor', 'estimate', 'fetch', 'pipeline', 'factors', 'backtest',
                    'evaluate', 'promote', 'factor_db', 'holdout', 'certify', 'reconcile')
ALL_COMMANDS len  = 17
  run     : mutation=['writes_facts']                 ticket=True  max_exit=4 confirm=False
  fetch   : mutation=['writes_supply']                ticket=False max_exit=4 confirm=False
  holdout : mutation=['writes_ledger']                ticket=False max_exit=4 confirm=True
  promote : mutation=['writes_facts','writes_ledger'] ticket=False max_exit=3 confirm=True
  certify : mutation=['writes_facts','writes_ledger'] ticket=False max_exit=3 confirm=True
  inspect : mutation=['read_only']                    ticket=False max_exit=2 confirm=False
  backtest: mutation=['read_only']                    ticket=False max_exit=3 confirm=False
```

逐字印证 `contract.py:32-38` 的两组清单与 `_specs():76-116` 的 16 行元数据。`promote/certify` 用 `F_L = frozenset({WRITES_FACTS, WRITES_LEDGER})`（`:75`）——丢掉 `writes_ledger` 会让 P3 的 --yes 守卫失效，这里实测 confirm=True，守卫成立。

### 例 2：三条守卫的失败路径（真实码 + 退出码）

| 调用 | 输出（真实） | 退出码 |
|---|---|---|
| `get('nope')` | `E1004` 未知命令 | 2 |
| `assert_no_caliber_args(['--horizon'])` | `E1020` 命令参数含口径字段 | 2 |
| `require_confirmation('holdout', yes=False)` | `E1004` holdout 会写全局账本，必须显式 --yes | 2 |
| `assert_no_caliber_args(['--plan','--set','--format'])` | 通过（正常参数不误伤） | — |

### 例 3：错误 → 退出码映射（真实调用 `exit_code_for`）

```
E1004 -> 2   (usage)
E1020 -> 2   (usage)
E2001 -> 3   (contract)
E3004 -> 1   (execution)
E4002 -> 4   (supply)
E5001 -> 1   (storage 并入 execution)
```

映射来自 `errors.py:49-66`：`_LAYER_BY_PREFIX` 按前两位分 5 层，`_EXIT_BY_LAYER` 把 storage 并入 1。**契约违反 E2→3 与执行失败 E3→1 严格分开**，符合 `exit_codes.py:27-28` 的红线。

### 例 4：CLI 真实 help（注册入口暴露的命令面）

`venv/Scripts/python.exe -m app.cli --help`（exit 0）usage 行列出 17 个命令，但**只有 11 个带 help 描述**（run/explain/pipeline/station/inspect/stations 六个无 `help=` 参数，故不显示在描述区）。`--format {table,json,ndjson}` 由 `render.FORMATS` 注入，非本模块字段。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| C-1 | **文档命令名 `refresh` 与实现 `fetch` 不对齐**：设计 `09-app-storage.md:78,84,101` 写 `refresh`，实现全用 `fetch`；`ALIASES`（`:40`）只有 `experiment→run`，没有 `refresh→fetch`。 | P2 | `contract.py:34-40`；`09-app-storage.md:78,84,101` | **已确认文档滞后**：实现名正确（fetch 语义=采集四数据集），应改文档而非加别名（加别名会让旧脚本静默走新命令，掩盖迁移）。属 D5，非代码 bug。 |
| C-2 | **模块 docstring 写「16 条命令」，实测 17 条**：S6 新增 reconcile 后业务命令 11→12，但文件头未同步。 | P2（文档） | `contract.py:3`「16 条命令」；实测 `ALL_COMMANDS len=17`；`test_v5_app_commands.py:22-26` 钉死 5+12=17 | **已确认**：纯注释滞后，改 `:3` 为「17 条」即可，不影响行为。 |
| C-3 | **`max_exit_code` 是声明性元数据，运行期零消费**：`grep max_exit_code` 全仓只有定义(`:54`)、构造校验(`:65-66`)与测试(`:35`)三处；`main()` 推导进程码走 `exit_code_for(envelope.errors[0].code)`（`cli/__init__.py:218`），从不检查是否超出声明的 max。 | P2 | `contract.py:54,65-66`；`cli/__init__.py:218`；实测 grep 仅 3 处 | **需确认意图**：字段当前只作「文档+测试契约」，非强制门禁。若要真正 fail-closed（命令报出超过声明的最大码即视为 bug），需在 envelope 构造处加断言；但 1（execution）恒小于 2/3/4，实际最大值通常不越界，风险低。 |
| C-4 | **`data_keys` / `params` 声明与实际返回未做运行期对拍**：`CommandSpec.data_keys`（如 run 的 7 键，`:88`）与 `cmd_run` 实际返回（多出 skip_reason/skipped_at/stage/live/supply_datasets，见 `run.py:167-180`）不一致；无测试钉住「声明键 ⊆ 实际键」。 | P2 | `contract.py:88`；`run.py:167-180`（实测返回 13 键） | **需确认**：注释称 data_keys「与 CLI 实际返回对齐」（`:74`），但实际 run 多 6 键。声明是「顶层键」最小集还是全集未定义。若作全集则漂移；建议明确为最小集并补对拍测试，或补全为实际键。 |
| C-5 | **P1 守卫只扫 option_strings，不扫参数值**：`assert_no_caliber_args`（`:154-163`）只比对 `--horizon` 这类开关名；若口径值经 `--set horizon=252` 注入，静态扫描抓不到。 | P2（需确认意图） | `contract.py:144-163`；`cli/__init__.py:176-183` 扫的是 `action.option_strings` | **设计如此**：`--set` 是编译期 override，口径字段进 `--set` 会被 `compiler` 的 E1020（工位参数含 caliber 字段）在编译期拦住——属另一道守卫，非本模块漏网。但两处 E1020 触发路径不同，排障时需分清。 |

**多了什么**：无冗余。`C-3` 的 `max_exit_code` 若视为「冗余声明」可删，但作为文档契约有价值，保留。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| click/typer 的命令注册表（命令名→回调→schema 一处声明） | **应借鉴（已近似实现）** | `COMMANDS` dict + `_DISPATCH` 表（`cli/__init__.py:42-50`）就是轻量注册表；CLI 只是 shell，Web 可复用同一份契约（设计 `09:203`）。 |
| typed error taxonomy + 层→码统一映射 | 采纳 | `errors.py` 49 码零重复、`exit_code_for` 唯一推导；错误对象不带 exit_code（`common.py:24-31` 实测），避免「同一错误两处写码」漂移。 |
| JSON Schema 契约优先（命令参数 schema 机器校验） | **拒绝纯 schema，采纳静态守卫** | `params` 字段（`:56`）只是字段名元组，非 JSON Schema；本模块用 `assert_no_caliber_args` 单点守卫口径泄漏（P1），比在每条命令挂 schema 更轻、更少漂移点。 |
| 每命令独立 exit_code 字段 | 拒绝 | 退出码从 `code` 层推导，命令只声明 `max_exit_code`；独立存会漂移（`errors.py:21-22` 同理）。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **mutation 四级完整且复合等级不丢**：`MUTATIONS`（`:27`）、`F_L`（`:75`），promote/certify 实测 confirm=True（例 1）。
2. **`__post_init__` 构造期 fail-closed**：非法 mutation（`:62-64`）、非法 max_exit_code（`:65-66`）直接抛 E1004，不会让坏规格进 `COMMANDS`。
3. **P1 静态守卫延迟导入避免环**：`forbidden_cli_options()`（`:144-151`）运行时才 `from runtime.compiler import CALIBER_FIELDS`，不复制第二份字段表——口径字段唯一真相在 compiler，符合 G3/G5 回归要求。
4. **P3 守卫路径带 path/hint**：`require_confirmation`（`:131-139`）消息含「先 --dry-run 看完整预览」，实测 CLI 真实调用 `flow holdout --plan ...`（无 --yes）→ E1004/exit2。
5. **别名归一在取规格前**：`get()`（`:123-128`）先 `ALIASES.get`，`experiment→run` 实测返回 name=run（`test_v5_app_commands.py:50-52`）。

**发现的问题**：
- 无 P0/P1 级代码 bug。C-1/C-2 为文档滞后，C-3/C-4/C-5 为需确认意图的元数据强度问题，均不影响「能真挖因子」。
- 测试覆盖充分（17 命令数、mutation 对齐、别名、P1/P3、格式、信封一致性共 12 例，`test_v5_app_commands.py:22-126`）。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md:78,84,101` | 用 `refresh` 指采集命令 | 改为 `fetch`（与实现 + `fetch.py` 对齐）；不加重名别名 |
| `09-app-storage.md:74-91` 表 | 16 行命令，无 `reconcile`；`factor_db` mutation 写「依子命令」 | 补 `reconcile`（read_only/exit3，S6 三方核对）；`factor_db` 明确为 `writes_facts`（实现 `contract.py:108`） |
| `contract.py:3` docstring | 「16 条命令」 | 改「17 条」（5 核心 + 12 业务） |
| `09-app-storage.md:74` 表头 | 「可能的最大退出码」措辞 | 明确该字段是声明性文档契约（C-3：运行期不强制），或补运行期断言说明 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一条命令 | ✅ 半配置化 | 在 `_specs()` 加一行 + `cli/__init__.py` 加解析器/分派 + 实现文件；`build_parser()` 启动期 `get_command(name)` 遍历（`:186-187`）保证清单与分派一致。三处仍需同步，非纯配置。 |
| 新增 mutation 等级 | ⚠️ 需改代码 | 改 `MUTATIONS` + `__post_init__` 集合，属结构性变更，合理。 |
| 新口径字段 | ✅ 零代码 | 口径唯一真相在 `compiler.CALIBER_FIELDS`，本模块 `forbidden_cli_options()` 自动派生禁用开关（`:144-151`），新增口径字段自动被 CLI 禁掉。 |
| 把 `max_exit_code` 升为运行期门禁 | ❌ 需改代码 | 当前零消费（C-3）；若要求「超声明码即 bug」需改 envelope 构造处。 |
| 旧名 `refresh` 兼容 | ❌ 未支持 | 无别名；按 D5 改文档是更干净的选择。 |

---

## C1–C12 相关项判定

- **C1 365 年化**：⚠️ 间接但关键——P1 守卫（`assert_no_caliber_args`）拦住 `--annualisation` 进 CLI，是 C1 在应用层的唯一静态防线；但 `--set annualisation=252` 路径靠 compiler 的 E1020 二道守卫（C-5）。
- **C2 资金费按事件 / C3 spot 无资金费 / C4 合约四元组 / C5 USDT 偏离 / C6 上下架 / C7 24/7 缺口 / C8 列集开放 / C9 as-of / C10 判据不放宽 / C11 流动性分层 / C12 365 观测**：均**不适用**——本模块是纯命令面壳，不读数据、不算口径、不做判据；加密事实的落点在 capability/runtime/upstream 层。

## R1–R7 影响

- **R1 真数据跑通**：中性（不参与执行）；但 C-3 若未来加严门禁需确保不误伤 `--live` 的 E4→4。
- **R2 结论可信**：✅ 正向——P1 守口径不被 CLI 改写，E2→3 与 E3→1 分开，排障不退化成猜。
- **R3 因子零代码扩展**：中性（命令面不限制因子空间）。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：✅ 正向——未知命令 E1004、口径开关 E1020、缺 --yes E1004、非法规格构造期 E1004，全拒；错误对象不带 exit_code 杜绝码漂移。

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 作为应用契约层，17 条命令元数据完整、mutation 复合等级不丢、P1/P2/P3 三守卫实测成立、错误→退出码映射唯一且与 `exit_codes.py` 红线一致，是 CLI 与编排层之间干净的静态边界。5 项差距全部是 P2：C-1/C-2（文档滞后，改文档即可）、C-3（`max_exit_code` 声明性零消费，需确认是否升门禁）、C-4（data_keys 与实际返回口径未定义，建议明确为最小集）、C-5（`--set` 路径靠 compiler 二道守卫，非本模块漏网）。均不影响「生产可用=能真挖因子」。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 2 个模块 `cli/__main__.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图（C-3/C-4/C-5），想当场裁决 → 告诉我
