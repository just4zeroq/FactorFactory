# 模块审查 53 · `v5/facilities/rng.py`

> 审查日期：2026-11-18
> 依据：源码 25 行全文通读；被 `facilities/__init__.py:84`（RngFactory）、`executor.py:300-306`（工位 sub_seed 派生）消费；设计 `s0/02-crosscutting.md` §5（按名字派生确定性随机源）、F4、`contracts/errors.py` E2007（设施被绕过）、E2009（自适应算法并发）；测试 `tests/v5/test_v5_facilities.py`（23 例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_facilities.py` 的 F-rng 段。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`rng.py` 是确定性随机源工厂。它该做的只有一件事（docstring:1-5）：**按名字派生确定性随机源**——同 `master_seed` + 同 `scope_path` 必得同 seed，改一个字符则不同；**不含时间、进程 id、路径**。

1. **派生公式**：`sub_seed(seed, scope) = int(sha256(f'{seed}:{scope}')[:16], 16)`（:19-21），64 位整数。
2. **实例级隔离**：`scope(path)` 返回一个独立 `random.Random` 实例，不与任何其他作用域共享状态（:23-25）——这是 F1"随机源实例级隔离（deap 全局 random 是唯一豁免）"的落点。
3. **无全局状态**：模块不持有任何可变全局；不同 scope 的随机流互不干扰。

**不变量**：派生只依赖 seed 与 scope 内容，同输入跨进程/跨机器一致（可复现）；改 scope 一个字符 seed 全变（敏感）。绕过设施自行取随机即 E2007；自适应算法并发共享全局随机状态即 E2009。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `fm_probe_facilities.py` F-rng 段。

### 例 1 · seed 派生确定性（用户必查点，实测）

```
derive(seed=42, scope='search:gp:1') twice = (9355974872283015912, 9355974872283015912, equal=True)
scope +1 char (gp:1 -> gp:2) = 8776618499722245353
scope changed 'p'->'q' (gp -> gq) = 4150448307587071234
all distinct = True
two independent Random(seed) same first value = ([0.0068719878907267384], ...) equal=True
no time/pid/path in formula: 64-bit int range = True
```

**解读**：
- **确定性（用户必查点）**：同 seed 同 scope 两次派生完全相同（9355974872283015912）；两个独立 `Random(seed)` 首个随机数完全相同（0.00687...）——重放一致（F4 基础）。
- **敏感**：scope 末尾 `1→2`、字符 `p→q` 都使 seed 全变（8776618499722245353 / 4150448307587071234），三个 seed 互不相同——"改一个字符则不同"成立。
- **无外部状态**：公式只有 `f"{seed}:{scope}"` 的 sha256 前 16 位（64 位整数），不含时间/pid/路径（实测落在 64 位范围）。

### 执行器中的用途（sub_seed 派生，编排层实证）

executor.py:300-306：`self.fx.rng.derive_seed(f"{scope}:{spec.name}:{seq}")`，其中 `scope = seed_scope or ticket_id`。注释明确：缺省退回 ticket_id 会让同计划两次运行随机流不同；组合根给 plan 指纹则完全可复现——**scope 用内容（plan 指纹）而非墙钟前缀**。

### 失败路径

本模块无显式校验分支（seed:int、scope:str 由类型注解与调用方保证）；非法类型会在 `f"{seed}:{scope}"` 格式化或 `random.Random` 构造时自然报错。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| R-1 | **seed 派生确定性（用户必查点，正面）**：同输入同 seed、改一字符全变、无时间/pid/路径，实测全部成立。 | —（正面） | F-rng 实测 + `rng.py:18-25` | **已确认符合设计**。无 P0/P1 缺口。 |
| R-2 | 返回的是标准库 `random.Random`（Mersenne Twister），非 deap/PCG64；`environment_digest` 的 `rng_impl` 参数示例填 "pcg64"（digest.py 测试），与实际 `random.Random` 不一致。 | P2（可观测性） | `rng.py:25`；`digest.py` 环境摘要示例；plan U7 D4（deap 全局 random 快照） | **需确认意图**：实际用的是 Python `random`，环境摘要里写 `rng_impl` 只是记录字段，若填错值会误导重放排查。建议文档/调用方把 `rng_impl` 填真实值（random.Random/Mersenne Twister）。低优先。 |
| R-3 | `derive_seed` 取 sha256 前 16 位（64 bit）作为 seed；`random.Random` 接受任意大整数，64 bit 足够但有理论碰撞空间。 | P2（理论） | `rng.py:21` | **已确认符合设计**：F4 只需确定性+敏感性，64 bit 碰撞概率对本研究规模可忽略。低优先。 |
| R-4 | deap 全局 random 是 F1 的唯一豁免（自适应算法用），本模块不管理该全局状态；并发执行自适应算法由 E2009 拦截。 | P2（边界说明） | `rng.py` 全文；`errors.py:108` E2009 | **已确认符合设计**：scope() 实例隔离覆盖绝大多数工位；deap 全局随机由 E2009 防并发，不在本模块职责内。 |

**多了什么**：无冗余。25 行全是必要实现。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 内容寻址 seed 派生（sha256(seed:scope)） | 采纳 | 同输入必同 seed、改一字符全变（实测），跨进程/机器可复现——加密多算法/多工位搜索需确定性随机流 |
| 实例级 Random（每 scope 一个） | 采纳 | 不共享全局状态，工位间随机流隔离（F1）；并发自适应算法另由 E2009 管 |
| 全局 `random.seed()` / `np.random.seed()` | 拒绝（F1/E2007） | 全局状态被多处改写会导致同计划两次运行结果不同；deap 全局 random 是唯一豁免且需并发护栏 |
| `time`/pid 参与 seed | 拒绝 | docstring:4 明确不含时间/pid/路径——否则重放无法复现 |
| PCG64 / numpy Generator | 未采用（记录项 R-2） | 当前用标准库 Mersenne Twister；若换需同步 `environment_digest.rng_impl` 与全仓，属升版 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **seed 派生确定性（用户必查点 / F4）**：同 seed 同 scope 两次相同、改字符全变、无外部状态（实测）。
2. **实例级隔离**：`scope()` 每次返回新 `random.Random`（:23-25），两个同 seed 实例首值相同但状态独立。
3. **无全局状态**：模块只 import hashlib/random/dataclass，无模块级可变变量。
4. **executor 用内容 scope**：sub_seed 用 plan 指纹而非 ticket 墙钟（executor.py:300-306），保证同计划可复现。
5. **不进指纹**：rng 的 seed/scope 不进任何产物指纹（seed 进 call_digest 是用户给的 master_seed 派生值，是确定性的，不是随机源状态）。

**发现的问题**：
- R-2（`rng_impl` 记录值与实际 Random 不符）、R-3（64 bit 理论碰撞）、R-4（deap 全局豁免边界）——均 P2 说明性，无 P0/P1 级 bug。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/02-crosscutting.md` §5 | 按名字派生确定性、无全局状态描述一致 | 无需更新 |
| `s0/02-crosscutting.md` F4 | 同 seed+scope 相同、改字符不同——实测成立 | 无需更新 |
| `s0/01-data-contracts.md` environment_digest 示例 | `rng_impl='pcg64'` 示例 | 改为真实值 `random.Random/Mersenne Twister`，或注明示例仅占位——消解 R-2 误导（说明性） |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 换新随机算法（PCG64） | ⚠️ 需改代码 + 升版 | 改 `scope()` 返回类型 + `environment_digest.rng_impl` 同步；换算法会改所有随机流 → 历史结果不可复现，属升版 |
| 新 scope 命名规则 | ✅ 零代码 | scope_path 是任意字符串，调用方按 `{plan}:{station}:{seq}` 等约定传入即可 |
| 随机源不影响指纹 | ✅ 支持 | 随机源状态/scope 不进产物指纹；只有派生的 seed 值进 call_digest（确定性，正确） |
| 自适应算法并发隔离 | ⚠️ 靠 E2009 | 本模块实例隔离不覆盖 deap 全局 random，并发由 E2009 拒绝，非本模块扩展点 |

---

## C1–C12 相关项判定

- **C10 判据不被放松**：中性——随机源确定性保证搜索可复现，但 rng 本身不触碰 dsr_min/fdr_max
- 其余 C1–C12：不适用（随机源层不含业务语义）

## R1–R7 影响

- **R1 真数据跑通**：中性（随机源是设施）
- **R2 结论可信**：✅ 强支撑——确定性派生 + 实例隔离保证搜索可复现，"同输入同结果"是可信结论的前提
- **R4 搜索有效**：✅ 支撑——多算法各自 scope 隔离随机流，避免相互干扰；随机对照可复现
- **R7 fail-closed**：✅ 支撑——绕过设施取随机即 E2007、自适应并发即 E2009

---

## 结论

**本模块（25 行）短小精悍，seed 派生确定性实测完全成立，无 P0/P1 级 bug，可确认通过。** 用户重点/rng 必查点"seed 派生确定性（E2007 反义）"落地：同 seed 同 scope 两次派生相同、改一字符全变、公式不含时间/pid/路径、跨进程可复现（实测）；executor 用 plan 指纹作 scope 内容而非墙钟前缀，保证同计划可复现。R-2/R-3/R-4 三条 P2 均为记录值/理论碰撞/边界说明，不影响生产可用。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 11 个模块 `digest_svc.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
