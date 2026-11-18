# 模块审查 54 · `v5/facilities/digest_svc.py`

> 审查日期：2026-11-18
> 依据：源码 31 行全文通读；底层唯一公式 `contracts/digest.py`（canonical_bytes/compute_digest，见 45 号文档）；被 `facilities/__init__.py:85`、`executor.py:321`（缓存 payload 复验）消费；设计 `s0/02-crosscutting.md` §6（指纹设施封装）、F1；测试 `tests/v5/test_v5_facilities.py`（23 例）。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。**本轮只读审查，未改代码。**
> 取证：`venv/Scripts/python.exe`（3.12.1）实跑 `fm_probe_facilities.py` 的 F-digest_svc 段。

---

## D1 · 应然（抛开实现，这个模块该做什么）

`digest_svc.py` 是规范化与指纹计算的**设施封装**（docstring:1-8）。它该做的只有一件事：**在唯一真相 `contracts.digest` 之上提供注入入口**——让能力层不必直接 import 契约层，便于将来整体升版，同时不引入第二份公式。

1. **纯函数**：无状态、无 IO、无时钟、无随机（:6）。实现变更 = 全系统指纹变更，属最高级变更，必须显式升版并接受历史产物失效。
2. **唯一公式不改**：`of(obj)` 直接转 `compute_digest(obj)`、`canonical_bytes(obj)` 直接转底层（:26-31）——本模块不含任何哈希逻辑，杜绝第二份公式。
3. **版本锚点**：`DIGEST_IMPL_VERSION="v1"`（:17）是升版锚点；`DigestService.version` 默认取它，frozen 不可改（实测 FrozenInstanceError）。

**不变量**：契约层 `contracts/digest.py` 仍是公式唯一真相（45 号文档）；本模块只是"注入包装"，不是"第二实现"。绕过设施自行 import compute_digest 属风格问题（F1 主要防 time/random/logger 自取），不带 E2007 强制。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> `venv/Scripts/python.exe` 实测 `fm_probe_facilities.py` F-digest_svc 段。

### 例 1 · 纯函数封装 + 公式一致（实测）

```
DIGEST_IMPL_VERSION = v1
version default = v1
of({'a':1}) = sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862
of == contracts.compute_digest = True
canonical_bytes type = bytes
frozen dataclass (no setattr) = FrozenInstanceError
```

**解读**：
- **公式一致（用户必查点"唯一公式"延伸）**：`DigestService.of({"a":1})` 与直接调 `contracts.digest.compute_digest({"a":1})` 结果完全相同（实测 True）——本模块确实只是转发，无第二份公式。
- **纯函数**：`canonical_bytes` 返回 bytes，`of` 返回 `sha256:<64hex>`；frozen dataclass 防版本被运行期改写。

### 失败路径

底层 `compute_digest` 的失败路径（NaN→E2002、大数组→E2002、bytes→E2002 等，见 45 号文档）经 `of()` 原样透传；本模块无独立校验分支。

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| D-1 | **唯一公式不破（正面）**：`of()`/`canonical_bytes()` 纯转发 `contracts.digest`，实测与底层一致。 | —（正面） | F-digest_svc 实测 + `digest_svc.py:26-31` | **已确认符合设计**。无 P0/P1 缺口。 |
| D-2 | 本模块只暴露 `of` 与 `canonical_bytes` 两个方法；契约层的八个语义指纹（plan_digest/supply_digest 等）无设施入口，能力层需直接用 `call_digest` 等函数。 | P2（覆盖度） | `digest_svc.py` 全文；对照 `digest.py:480-497` __all__ | **需确认意图**：设施封装只覆盖"通用规范化"，语义指纹直接调契约函数。设计上说得通（语义指纹是编译期用，不经设施），但注入一致性略打折。低优先。 |
| D-3 | `version` 字段只是记录，不参与实际哈希选择——无论 version 填什么，`of()` 都调同一个 `compute_digest`。 | P2（版本语义） | `digest_svc.py:20-31` | **需确认意图**：版本是"声明/审计锚点"，不是"切换实现的开关"（真正换实现要改 import）。若有人误以为改 version 就升版会踩坑。建议 docstring 明确"version 仅声明，不切换实现"。 |
| D-4 | 无 F1/E2007 强制：能力层可直接 `from v5.contracts.digest import compute_digest` 绕过本设施。 | P2（约束强度） | `digest_svc.py` 全文；F1 主要针对 time/random/logger | **已确认符合设计**：指纹是可复现纯函数，直接 import 不会引入状态漂移，绕过风险远低于时钟/随机源。低优先。 |

**多了什么**：无冗余。31 行全是必要封装与版本锚点。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| 设施层薄封装 + 契约层唯一真相 | 采纳 | 依赖注入便于替换/升版，公式仍只在 contracts/digest 一处（实测 `of==compute_digest`） |
| 设施层重写一份哈希 | 拒绝（红线） | 第二份公式 = 指纹漂移源头；plan U4 D4 明确"指纹公式只在 contracts/digest 一处" |
| version 切换实现 | 拒绝（当前取舍） | version 是声明锚点，实际实现靠 import（D-3）——避免运行期按字符串分支切换哈希的复杂性 |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **唯一公式不破**：`of` 与底层 `compute_digest` 实测一致（用户必查点延伸）。
2. **纯函数**：无状态/IO/时钟/随机（:6），frozen dataclass（实测 FrozenInstanceError）。
3. **版本锚点**：`DIGEST_IMPL_VERSION="v1"`（:17），升版需显式改常量。
4. **注入入口**：让能力层不直接依赖契约层（docstring:3-4），组合根统一构造（facilities/__init__.py:85）。
5. **缓存复验用途**：executor.py:321 用 `self.fx.digest.of(payload)` 复验缓存 payload 指纹（50 号文档实证）。

**发现的问题**：
- D-2（语义指纹无设施入口）、D-3（version 仅声明不切换）、D-4（无 F1 强制）——均 P2，无 P0/P1 级 bug。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/02-crosscutting.md` §6 | "底层公式来自 contracts.digest，这里只提供注入入口"一致 | 无需更新 |
| `s0/02-crosscutting.md` §6 | 未说明 version 字段语义 | 补一句"version 仅声明/审计，不切换实现；升版靠改 import + DIGEST_IMPL_VERSION"——消解 D-3 |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| 指纹实现整体升版 | ⚠️ 需显式升版 | 改 `contracts/digest.py` + `DIGEST_IMPL_VERSION` + 全仓接受历史产物失效——最高级变更 |
| 给语义指纹加设施入口 | ✅ 低风险 | 在 DigestService 加转发方法（如 `plan(plan_raw)` 转 `plan_digest`），不引入新公式——属 D-2 可选增强 |
| 新设施不影响指纹 | ✅ 支持 | digest_svc 自身无状态、不进指纹；任何设施都不携带业务口径（U2 D1） |

---

## C1–C12 相关项判定

- **C10 判据不被放松**：中性——指纹确定性是判据可信的基础，但本模块只做转发
- 其余 C1–C12：不适用（指纹封装层不含业务语义）

## R1–R7 影响

- **R1 真数据跑通**：中性（指纹设施被全链路使用）
- **R2 结论可信**：✅ 强支撑——唯一公式 + 确定性保证产物身份可信、口径不可静默改写
- **R6 迭代复用**：✅ 支撑——内容寻址指纹支持产物复用与命中判定

---

## 结论

**本模块（31 行）是 `contracts.digest` 的薄封装，无第二份公式，无 P0/P1 级 bug，可确认通过。** 实测 `DigestService.of()` 与 `contracts.compute_digest()` 结果完全一致（用户必查点"唯一公式"延伸成立）；纯函数、无状态、frozen 版本锚点、缓存复验用途均正确。D-2/D-3/D-4 三条 P2 均为覆盖度/版本语义/约束强度的说明性项，不影响生产可用。指纹设施不含业务口径、不进任何指纹，符合 U2 横切设施红线。

**请确认本模块审查结果**：
- ✅ 确认通过 → 进入第 12 个模块 `notify.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
