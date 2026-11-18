# 模块审查 16 · `capability/impls.py`

> 审查日期：2026-11-18
> 依据：源码 86 行全文通读；依赖 `runtime.py`（`register`/`impls`）、`spec.py`（名字空间）、12 个 `ops/*.py` 的 `build`（其中 evaluate → `backtest_engine.build`）；`capability/__init__.py:18-22` 在 import 时调用 `install_default_impls()`；设计 `s0/04-capability.md §3`、`20-mining-algorithms.md §4`（`:98-114`）；测试 `test_v5_capability.py:189-204`、`test_v5_capability_runtime.py:425-465`；历史审查 `review_u6_u7_20261118.md §2.9/§2.10`、`v5_module_review_plan_20261118.md §U7`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响；"生产可用=能真挖因子"（plan §0.0/§0.1）。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）

Impls 是能力层**唯一的真实实现装配入口**，回答三件事：

1. **装配 12 能力真实实现**：把各 `ops/*.py` 的 `build` 登记到 runtime 实现表；实现名沿用计划里的名字（`label→forward_return_cs`、`evaluate→qweave`、`observe→two_stage_funnel`、`verdict→sentinel`），使 `plan.<station>.impl` 与能力实现名是同一个词，不需要一张"计划名→实现名"映射表（那张表迟早与本体不一致，`:6-9`）。
2. **幂等 + 登记即执法**：`install_default_impls()` 幂等（已登记不重复）；`single`/`fixed` 名字空间重复登记触发 `E2006`（第二套执行路径），靠 `runtime.register` 执法（`:3-4,35-36`）。
3. **实现名进 call_digest**：`combine` 三个合成器各自注册（equal/score/fixed_ridge），使换合成器必须换指纹，不能只换一个 params 键；`evaluate` 版本取 qweave 包版本（内核语义变化→指纹变化）（`:48-51,64-68`）。

**加密特化**：本模块是装配层，不含加密数值语义；但"evaluate 用 qweave 包版本当 impl_version"是 C10/结论可信的底座——换求值内核就是换语义，版本不跟着变会让缓存拿旧语义产物服务新语义（`:48-51`）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 用仓库 `venv/Scripts/python.exe` 调用 `install_default_impls()` 与 `impls()` **实际打印**。
> 复现脚本：`C:/Users/lansa/AppData/Local/Temp/fm_probe_impls_runtime.py` 的 IMPLS 段。

### 例 1：`install_default_impls()` 后的完整注册表（真实输出）

```
registered capabilities count = 12
  align      -> [('point_in_time_align','1')]
  allocate   -> [('quantile','1')]
  backtest   -> [('ledger','1')]
  boundary   -> [('pit_dynamic_topk','1')]
  certify    -> [('walk_forward','1')]
  combine    -> [('equal_weight','1'),('score_weighted','1'),('fixed_ridge','1')]
  evaluate   -> [('qweave','0.6.2')]
  label      -> [('forward_return_cs','1')]
  observe    -> [('two_stage_funnel','1')]
  read       -> [('published_only','1')]
  search     -> [('gp_symbolic','1')]
  verdict    -> [('sentinel','1')]
total impl entries = 14    # 12 能力 + combine 多 2 个合成器
evaluate version (qweave pkg) = '0.6.2'
```

要点：
- **12 能力全覆盖**，14 个实现条目（combine 的 3 个合成器各占一条）。
- `evaluate.impl_version='0.6.2'` 来自 `importlib.metadata.version('qweave')`（`:51`），不是 V5 自己的序号。
- `backtest` 只注册 `ledger`（ml4t 目标权重引擎 + V5 成本口径）；`ops/backtest.py` 的向量化版只作数值参照/夹具，不注册（`:76-78`）。

### 例 2：幂等性（真实输出）

```
install twice idempotent (no new names)? True
```

连续两次 `install_default_impls()`，实现表无新增名字——靠 `_has(capability)` 守卫每个能力（`:31-36`）。注意：导入 `capability` 时 `__init__.py:22` 已自动装过一次，此处是显式再调一次仍幂等。

### 例 3：无重复注册（真实错误码）

| 非法输入 | 输出 |
|---|---|
| 对 `read`（single）再 `register('read','alt2','1',...)` | `E2006` "read 是 single 名字空间，已有实现 ['published_only']，不得再登记 'alt2'" |
| 对 `verdict`（fixed）再 `register('verdict','alt2','1',...)` | `E2006` "verdict 是 fixed 名字空间，已有实现 ['sentinel']，不得再登记 'alt2'" |

`single`/`fixed` 第二实现经 `runtime.register` 拒（第二套执行路径），编译期同源守卫在 `registry.resolve_impl`（见 `14_registry.md`）。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| I-1 | **`search` 只注册 `gp_symbolic`**，基线算法 `random_expression` 未在本表登记；它走 `search._RUNNERS`/`_algorithms()` 分派（`search.py:36-48,376`），`search.describe()` 能看到但 `impls()` 枚举看不到。 | **P2（需确认意图）** | `:52-55` 注释"单实现，多算法走同一 build"；`search.py:36-48,376` `_algorithms()={gp_symbolic,random_expression}`、`_RUNNERS.update(...)`；实测 `impls()` 的 search 只有 gp_symbolic。 | **需确认意图**：设计上算法名是 `params.algorithm`/`params.algorithms` 分派的对象，不是 `impl`——open 名字空间单 build 多算法，符合 `20-mining-algorithms.md §4` "新算法只动一处（算法注册）"。故 `impls()` 看不到 random 是设计结果，不是 bug。但 `20-mining-algorithms.md §4` 第 2 步写"加入算法实现目录（impls 注册表）"，与本实现"算法不在 impls 表、在 `_RUNNERS`"存在文档-实现措辞偏差——需确认后统一文档措辞（并入 P2 清单）。 |
| I-2 | **`allocate` 只注册 `quantile` 一个实现**：注释称约束投影（cvxpy）路径已在实现内、待契约补 `mode` 键即启用（`:69-74`）。当前 `plan_schema` 未暴露 `mode`，CLI 只走分位。 | P2（能力覆盖） | `:69-74`；`review_u6_u7_20261118.md` 与 `v5_fix_execution_20261118.md`（E1019 声明"当前不可达"） | **需确认意图**：约束投影路径当前不可达（契约未开键），是已知遗留，非本模块 bug。R 影响：限制可挖因子空间（风险最优组合），但属 U9/契约层决策，本模块只装配。 |
| I-3 | **`install_default_impls` 依赖 import 时副作用**（`__init__.py:22` 自动调用）。若某路径绕过 `import capability` 直接 `from capability.runtime import run`，实现表为空 → E3004。 | P2（健壮性） | `capability/__init__.py:18-22`；`runtime.run:280-285` 空表 E3004 | **需确认意图**：`__init__.py` 注释明确"能力不靠调用方记得注册，否则 run() 会随机 E3004"，并在包 import 时兜底。除刻意绕过包的场景外不会触发；属可接受设计。 |

**多了什么**：无冗余。`_has` 守卫（`:31-32`）+ 每能力 `if not _has` 是幂等的最小实现；combine 多注册 2 个合成器是刻意设计（impl 名进指纹，`:64-68`）。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 注册表装配模式 + import 时幂等安装 | 采纳 | 12 能力一次装配；`single/fixed` 重复 E2006；避免 run() 随机 E3004 |
| 实现名 = 计划名（消除映射表） | 采纳 | `plan.<station>.impl` 与能力实现名同词；无"计划名→实现名"映射漂移风险（`:6-9`） |
| 内核版本号当 impl_version | 采纳 | qweave 0.6.2 进指纹，换内核即换语义（`:48-51`）；拒绝 V5 自增序号（会缓存串语义） |
| 多个合成器各自注册（impl 名进指纹） | 采纳 | 换合成器必须换 `call_digest`（`:64-68`），不能只改 params 键 |
| 插件热加载 / 运行时替换实现 | **拒绝** | 第二套执行路径会让裁判可替换；`single/fixed` 在 register 层拒（E2006） |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **12 能力装配齐全、14 条目**（实测）：每个 `ops.build` 已登记；`__all__=['install_default_impls']`（`:86`）公开面最小。
2. **幂等**：实测两次 install 无新增名字；`_has` 守卫每能力（`:31-36`）。
3. **无重复注册**：实测 single/fixed 第二实现各 E2006（由 `runtime.register` 执法，`:248-255`）。
4. **evaluate 版本取 qweave 包版本**（`:51`）：实测 `0.6.2`，内核语义进指纹。
5. **combine 三实现各自注册**（`:67-68`）：实测 equal/score/fixed_ridge 三条；换合成器进指纹。
6. **实现名沿用计划名**（`:40-83`）：`label→forward_return_cs`、`evaluate→qweave`、`observe→two_stage_funnel`、`verdict→sentinel`，无映射表。
7. **backtest 只注册生产实现 ledger**（`:75-78`）：向量化版不注册（数值参照/夹具），避免两套回测语义进指纹。

**发现的问题**：
- 见 D2 表 I-1（`search` 算法名走 `_RUNNERS` 不在 impls 表，文档措辞偏差，需确认意图）、I-2（allocate 约束投影路径当前不可达，契约层遗留）、I-3（import 副作用健壮性）。**无 P0/P1 级代码 bug**。测试覆盖充分：`test_v5_capability.py:189-204`（四方法存在）、`test_v5_capability_runtime.py:425-465`（各名字空间 register/resolve 执法）、`__init__.py` 自动装配有幂等保证。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md §3` | 12 能力名字空间、服务工位 | 与实现一致，无需更新 |
| `20-mining-algorithms.md §4` 第 2 步（`:107`） | "加入算法实现目录（impls 注册表），编译期 `_resolve_impl` 按声明名校验" | 与实际"算法走 `search._RUNNERS`/`_algorithms()`，不在 impls 表"不符——需确认后改这句为"加入 `search._algorithms()` 与 `_RUNNERS`（算法名是 params，不是 impl）"。属契约措辞对齐（I-1） |
| `20-mining-algorithms.md §5` 决策记录（`:123`） | `random_expression` 作常驻基线 | 与 `_RUNNERS` 注册一致；无需更新 |
| `s0/04-capability.md §3.9/§3.11`（allocate/backtest） | 服务工位与实现 | 补注"allocate 约束投影路径待契约补 mode 键启用"（I-2，说明性） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 新增能力实现 | ⚠️ 需改代码 | 在 `install_default_impls` 加 `register(...)` 一行（新能力还需 spec/bridge/params 同步，见 15_spec.md D6） |
| 新增 search 算法（如 random/贝叶斯） | ⚠️ 路径二分 | 走 `search._algorithms()`+`_RUNNERS`（算法名是 params），**不**在 impls 表加——两条扩展路径需文档说清（I-1） |
| 换 evaluate 求值内核 | ✅ 指纹自动变 | impl_version 取 qweave 包版本，换内核即新指纹（不需要改本模块代码） |
| combine 加合成器 | ⚠️ 需改代码 + 文档 | 在本表加 `register` 一行 + 实现内分派；每个合成器独立进指纹 |
| allocate 启用约束投影 | ❌ 当前不可达 | 需契约先暴露 `mode` 键（U9/契约层），本模块暂不注册第二个实现（I-2） |

---

## C1–C12 相关项判定

- **C1–C12 各项**：均不直接适用（本模块是装配层，不含加密数值语义）；间接支撑：
- **R2 结论可信底座**：✅ evaluate 用 qweave 包版本当 impl_version（`:48-51`），换内核语义必换指纹——防"缓存拿旧语义产物服务新语义"
- **R4 搜索有效**：⚠️ search 单 build 多算法经 `_RUNNERS` 分派（I-1），随机基线可跑但 `impls()` 枚举看不到——R4"多算法"在算法层成立、在能力枚举层不直观

## R1–R7 影响

- **R1 真数据跑通**：✅ 正向——import 即装配 12 能力，生产路径不会因"忘记注册"随机 E3004
- **R2 结论可信**：✅ 正向——内核版本进指纹（qweave 0.6.2）
- **R4 搜索有效**：⚠️ 中性——多算法经 `_RUNNERS` 分派；文档措辞需对齐（I-1）
- **R7 fail-closed**：✅ 正向——幂等装配、`single/fixed` 重复 E2006、无重复注册（实测）

---

## 结论

**本模块代码质量高，无 P0/P1 级 bug，可确认通过。** 12 能力实现注册齐全（14 条目）、幂等、无重复注册全部实测成立；evaluate 用 qweave 包版本（0.6.2）当 impl_version、combine 三实现各自进指纹，是 R2 结论可信的底座。三处 P2 均为需确认意图/文档偏差：I-1 search 算法名走 `_RUNNERS` 不在 impls 表（`20-mining-algorithms.md §4` 第 2 步措辞需改）、I-2 allocate 约束投影路径当前不可达（契约层遗留）、I-3 import 副作用健壮性（可接受设计）。

**请确认本模块审查结果**：
- ✅ 确认通过（I-1 列入 P2 待改文档）→ 我进入第 5 个模块 `runtime.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 想把 I-1 文档偏差当场裁决 → 告诉我
