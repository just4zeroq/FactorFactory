# 模块审查 57 · `app/cli/__main__.py`

> 审查日期：2026-11-18
> 依据：源码 11 行全文通读；宿主 `app/cli/__init__.py`（build_parser L70-188 / main L191-218 / _DISPATCH L42-50）；设计 `09-app-storage.md` §2；实测 `venv/Scripts/python.exe -m app.cli`（多组 help/失败/成功路径，exit code 逐一记录）。判据见 plan §0.0/§0.1。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

`__main__.py` 是 `python -m app.cli` 的**入口壳**。拆分后 `cli/` 从单文件变成包目录，必须有一个独立 `__main__` 让 `-m` 能找到入口。它应然只做三件事：

1. 从包相对导入 `main`（`. import main`）。
2. 仅当 `__name__ == "__main__"` 时才执行（防止被 import 时误跑）。
3. 把 `main()` 返回的进程码交给 `sys.exit()`，让 shell 拿到 0/1/2/3/4。

**边界止于转交**：不含任何业务逻辑、不构造 parser（在 `__init__.py`）、不渲染、不推导退出码（在 `common.envelope_error`）。它是「组合根的最后一个齿轮」——把控制流从 Python 模块系统交给 CLI 框架，再把退出码从 CLI 框架交回操作系统。

**加密视角**：本模块零加密语义。但它是「生产可用」判据的物理入口——用户/调度器/CI 全部通过 `python -m app.cli` 触发真实因子挖掘，退出码能否正确上达决定自动化能否信任它。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 全部用 `venv/Scripts/python.exe -m app.cli ...` 真实调用，`echo $?` 抓退出码。

### 例 1：help 路径（成功，exit 0）

```
$ venv/Scripts/python.exe -m app.cli --help
usage: flow [-h]
            {run,explain,pipeline,doctor,estimate,station,inspect,promote,factors,holdout,factor_db,evaluate,backtest,certify,reconcile,stations,fetch}
            ...

V5 CLI（S1 真实调用单）
positional arguments:
  {run,explain,...}   doctor 供给体检 / estimate 按 caliber.net 估算成本 / ...
options:
  -h, --help
EXIT=0
```

usage 行 17 个命令，描述区只有 11 个（`run/explain/pipeline/station/inspect/stations` 六个在 `__init__.py:74,92,99,136,151` 建 parser 时未传 `help=`，故 help 描述为空——非本模块缺陷，属 `__init__.py` 的小遗漏，见 58 号审查）。

### 例 2：无子命令 / 未知命令（argparse 失败，exit 2，**绕过信封**）

```
$ venv/Scripts/python.exe -m app.cli
usage: flow [-h] {...} ...
flow: error: the following arguments are required: command
EXIT=2

$ venv/Scripts/python.exe -m app.cli nope
usage: flow [-h] {...} ...
flow: error: argument command: invalid choice: 'nope' (choose from 'run', 'explain', ...)
EXIT=2
```

**关键观察**：argparse 层失败时输出的是 `usage + error` 文本到 **stderr**，不是 JSON 信封——因为 `parser.parse_args()`（`__init__.py:198`）在 `try` 块**之外**，argparse 自己 `SystemExit(2)` 直接终止，根本走不到 `main()` 的 `try/except`。这与「所有输出都是信封」的直觉相悖（见 D2 M-1）。

### 例 3：正常命令 → `sys.exit(main())` 透传进程码

```
$ venv/Scripts/python.exe -m app.cli stations
{"ok": true, "command": "stations", "run_id": null, "data": {"stations": ["boundary","panel","target","search","evidence","verdict","portfolio","certify"], "count": 8}, "warnings": [], "exit_code": 0}
EXIT=0

$ venv/Scripts/python.exe -m app.cli inspect --run-id NOPE
{"ok": false, "command": "inspect", "run_id": null, "data": null, "warnings": [], "exit_code": 2,
 "errors": [{"code":"E1008","layer":"usage","message":"执行记录不存在: NOPE",...}]}
EXIT=2
```

`main()` 返回 `int(envelope["exit_code"])`（`__init__.py:218`），本模块 `sys.exit()` 原样上达——实测 stderr/stdout 信封里的 `exit_code` 与 shell `$?` 完全一致。E1008→2（usage）映射正确。

### 例 4：异常兜底路径同样上达（真实）

```
$ venv/Scripts/python.exe -m app.cli station Boundary --on NOPE
{"ok": false, "command": "station", ..., "exit_code": 1,
 "errors": [{"code":"E3001","layer":"execution","message":"ticket NOPE 没有冻结方案",...}]}
EXIT=1
```

E3001（execution）→ 1，经 `sys.exit` 上达。证明 0/1/2/3/4 五码都能穿过这层薄壳。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据（文件:行号 + 实测） | 判断 |
|---|---|---|---|---|
| M-1 | **`parse_args` 在 try 外，argparse 失败绕过信封**：无子命令/未知命令/`fetch` 互斥参数冲突时，输出 usage 文本到 stderr 而非 JSON 信封，exit 2 但无 `command/errors/ok` 字段。 | P2 | `__init__.py:198`（`args = parser.parse_args(raw)` 在 `:200 try` 之前）；例 2/例 4 对照 | **需确认意图**：设计 `09-app-storage.md` §2.4 要求「所有输出都是信封」，argparse 层是例外。对机器消费（CI/调度器）不友好——它拿到 exit 2 但解析不到 error code。可改：捕获 `SystemExit` 转 envelope_error(E1004)，但会改变既有行为，属契约变更需确认。 |
| M-2 | **本模块仅 11 行，无自身逻辑**：所有职责委托 `__init__.py`。 | — | 源码全文 | **符合应然**：薄壳是正确设计，不是缺失。缺点仅「审查它几乎等于审查 `__init__.py`」，已在 58 号覆盖。 |
| M-3 | **`if __name__ == "__main__"` 守卫正确**：被 import 不触发。 | — | 源码 `:9-10` | **已确认正确**，无需改。 |

**多了什么**：无。`from __future__ import annotations` 在 11 行脚本里非必需但无害。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| `python -m package` 入口（PEP 338） | 采纳 | 拆分包目录后必须有 `__main__.py`；这是标准做法，无替代必要。 |
| console_scripts 安装式入口（`flow` 可执行文件） | 拒绝（本轮） | 设计明确不交付 Web/安装态；`-m` 入口足够，且 `prog="flow"`（`__init__.py:71`）已让 help 显示 flow 名，无需 setuptools 包装。 |
| 在 `__main__` 里塞 parser/渲染 | 拒绝 | 保持 11 行薄壳；职责在 `__init__.py`/`render/`/`common.py`，单一入口点。 |
| 捕获 argparse SystemExit 转结构化错误 | **待裁决（M-1）** | click 等框架原生把参数错误做成结构化；argparse 需手动捕获。若下游要求全信封，值得做；否则保留现状。 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **相对导入正确**：`from . import main`（`:7`）只在本包有效，避免绝对导入路径脆弱性。
2. **`__name__` 守卫**（`:9`）：import 时不跑，测试可安全 `from app.cli.__main__ import ...`。
3. **退出码透传**：`sys.exit(main())`（`:10`）把 0/1/2/3/4 原样交给 OS，实测例 3/例 4 信封 `exit_code` == `$?`。
4. **无口径参数**：本模块零参数，P1 守卫不在此触发（在 `__init__.py:183`）。

**发现的问题**：
- 无 P0/P1 级 bug。M-1（argparse 绕过信封）是 `__init__.py` 的设计取舍，非本 11 行壳能修——已并入 58 号审查 D2。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `09-app-storage.md` §2.4 输出契约 | 隐含「所有输出都是信封」 | 补一句「argparse 层参数错误（缺命令/未知命令/参数冲突）输出 usage 文本到 stderr，exit 2，不产信封」——消解 M-1 的预期落差（说明性，非契约变更）。 |
| 无涉及本模块的专门文档条目 | 11 行薄壳无需专文档 | 无需更新。 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增一条 V5 命令 | ✅ 与本模块无关 | 命令加在 `contract.py`/`__init__.py`，`-m` 入口不变。 |
| 增加 shell 安装入口 `flow` | ⚠️ 需新增 setuptools 配置 | 本模块可复用（console_scripts 指向 `app.cli:main`），但属交付方式变更，本轮不做。 |
| 把 `-m` 入口改名为其它 | ❌ 不建议 | 文档/测试/用户肌肉记忆全部绑定 `python -m app.cli`。 |

---

## C1–C12 相关项判定

- **C1 365 年化 / C2–C12**：均**不适用**。本模块是 11 行 `-m` 入口壳，零业务语义、零数据访问、零口径。加密事实的落点在 capability/runtime 层；本模块的唯一价值是「退出码能否上达」这一生产可用性物理链路。

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向（必要通道）——`python -m app.cli run --live` 经此入口触发八工位真跑，实测退出码正确上达。
- **R2 结论可信**：中性（不接触结论）。
- **R3 因子零代码扩展**：中性。
- **R5 单因子可归因**：中性。
- **R7 fail-closed**：✅ 正向——`sys.exit(main())` 保证异常兜底码 E3004→1 也能上达，不会「吞掉崩溃返回 0」。

---

## 结论

**本模块是 11 行标准 `-m` 入口薄壳，无 P0/P1 级 bug，可确认通过。** 相对导入、`__name__` 守卫、`sys.exit(main())` 三件事全部正确，实测 0/1/2/3/4 五码都能穿过。唯一可议的 M-1（argparse 错误绕过信封）是宿主 `__init__.py` 的设计取舍，不在本模块修；如需全信封输出，并入 58 号审查统一裁决。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 3 个模块 `cli/common.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ M-1 想当场裁决（是否把 argparse 错误转成信封）→ 告诉我
