# 模块审查 18 · `capability/ops/evaluate.py`

> 审查日期：2026-11-18
> 依据：源码 504 行全文通读；依赖 `panel.py`（Panel/array_digest）、`registry.py`（default_registry/operators_version）、`qweave`（内核求值）；设计 `s0/04-capability.md` §3.10、`11-field-operator-registry.md` §4；测试 `test_v5_evaluate.py`。
> 判据：六维 D1–D6 + C1–C12 相关项 + R1–R7 影响。
> **本轮只读审查，未改代码。**

---

## D1 · 应然（抛开实现，这个模块该做什么）
Evaluate 是证据链第 1 工位"表达式求值器"，回答一件事：把 JSON AST 在冻结的面板上算成一条暴露 `(T,N)`，并给出逐格有效性。四条硬边界：
1. **白名单唯一真相**：算子名/arity/param_domain/impl_symbol 全在 `operators.json`；`_SHAPES` 只补 JSON 表达不了的"调用形状"。两边条目集、arity、impl_symbol 必须逐条对得上，否则 `E1015`——白名单写着支持、运行到那步才发现没有绑定，是最坏的静默失败。
2. **`values` 与 `valid_mask` 分离**：无效值不能写成 0（0 会被截面排序当成中间值）。`drop` 策略只削弱 `valid_mask`，绝不动 `values`（先去缺失行再算时序窗口会让窗口跨过缺失去用更早观测）。
3. **`values_ref` 唯一暴露来源**：`observe`/`combine`/`certify` 只读这份摘要，不得各自重算——重算会让 NaN 传播、tie 策略、除零约定三处分叉，而三处都自洽时没有任何检查点能发现。
4. **退化即拒**：常数暴露、全 NaN、空截面超阈（20%，与 I61 同项目约定）都是 "没有样本"，不是"有点稀疏"，必须 fail-closed。

**加密特化**：列集开放（C8）——funding/OI/basis 等字段只要进了 Panel 就应能被 AST 引用，**求值器内不写任何字段特判**；24/7 时间轴用整数纳秒，不转 datetime（少一次时区解释）；年化不在此模块（C1 不适用）。

---

## 具体例子 · 输入什么 / 输出什么（真实运行，非手写示意）

> 以下用仓库 `venv/Scripts/python.exe` 调用 `capability.run(call)` **实际打印**，不是推演。
> 复现：临时脚本 `C:/Users/lansa/AppData/Local/Temp/fm_verify_observe.py`（系统临时目录），PYTHONPATH 指向仓库根。

### 场景（围绕加密货币）
- 面板：8 标的 × 60 根 1d bar（2024-01-01 起，24/7 无日历）。
- 字段：`close`、`funding`（每根 `rng.normal×1e-4` 的费率）。演示**资金费字段透传**：AST `sum(funding,3)` 直接引用 funding，求值器无需为 funding 写任何代码。
- `nan_policy=propagate`、`node_cache=false`、`operator_set=registry.operators_version`。

### 输入（`Call`）
```
capability="evaluate" impl="qweave" impl_version=<qweave 版本>
params={"operator_set":<摘要>,"nan_policy":"propagate","node_cache":False}
inputs=(InputRef("payload", <sum AST>, data={"op":"sum","args":[{"field":"funding"},{"const":3}]}),
        InputRef("panel", panel.digest(), data=Panel(fields={"funding":…,"close":…})))
```

### 输出（`run(call)` 返回 dict 的关键字段，真实打印）
```
node_stats.fields = ['funding']
values[3] = [-2.1683e-04, 2.1615e-04, -2.2469e-04, -1.1769e-04,
             -3.4035e-04, -5.4370e-05, 1.6203e-04, -1.2400e-05]
```
要点：`funding` 字段与其他字段走**完全相同**的求值通道（C8 列集开放），`values[3]` 恰是 `funding[t-2:t+1]` 三根之和（前两根窗口未满为 NaN）。`values_ref`/`valid_mask_ref` 是内容摘要，`node_stats` 含 nodes/depth/operators/fields/dropped_bars/empty_bars/degenerate_cells/valid_cells/cache。

同批真实调用（取自 `test_v5_evaluate.py` 的断言）：`mean(close,3)` 在 t<2 全 NaN、t=2 起为最近三根均值（`evaluate.py:414-415` 按 `(t,s)` 行序写回）；`rank(close)` 输出 `[1/3,2/3,1.0]`（按有效成员数归一）；`ref(close,1)` 首行 NaN、其后等于 close 前移一行。

### 失败输入 → 错误输出（fail-closed 路径，源码逐条核对）
| 非法输入 | 错误码 | 来源 |
|---|---|---|
| AST 算子不在白名单 | `E1015` | `evaluate.py:245-249` |
| 算子 arity 与 `_SHAPES` 不符 / impl_symbol 不存在 | `E1015` | `:146-174` |
| `operator_set` ≠ `registry.operators_version` | `E1015` | `:363-373` |
| `node_cache=true` | `E1004` | `:381-387`（节点缓存实测净亏 65 倍，文档 §3.10） |
| payload 是 `model_ref`（推理通道未接） | `E1003` | `:106-116` |
| 字段与保留列 `symbol`/`time` 同名 | `E1003` | `:329-338` |
| 常数暴露 / 全 NaN / 空截面 >20% | `E3003` | `:435-457` |
| qweave 内核抛异常 | `E3003` | `:398-403`（不得当空结果） |

---

## D2 · 差距（少什么 / 多什么 / 删什么 / 补什么）

| # | 差距 | 级别 | 证据 | 判断 |
|---|---|---|---|---|
| E-1 | **"funding 透传"是通用列开放，不是专项逻辑**：求值器没有任何 funding 字段判断，`sum(funding,3)` 与 `sum(close,3)` 走同一条路。这是设计优点（C8：加字段不改代码），但意味着**字段单位/语义的正确性完全由上游供给与 caliber 保证**——把价格型 funding 误标成费率型，求值器不会拦。 | P2（责任边界说明） | `evaluate.py:347-352`（按 `fields` 集合逐列取）；`:217-228`（只校验字段名在 panel.fields） | **已确认符合设计**：能力层不读语义。需在文档写明"字段单位错配的护栏在供给层/编译层，不在求值器"。 |
| E-2 | **推理通道（`model_ref` payload）明确拒绝**：文档承认模型产物与表达式产物"两条路径产物形状必须一致"，但接推理通道之前不得用一条冒充另一条。当前只支持表达式 AST。 | P2（阶段边界） | `evaluate.py:106-116`；`impls.py:58-62`（只注册 `qweave`） | **需确认意图**：本轮 jih"真挖因子"用表达式已够（R4 已通），模型因子属 R3 之外的扩展，暂不补。 |
| E-3 | `node_cache=true` 直接 `E1004` 失败而非"开了没生效"：作者明确认为给一个"开了但没生效"的开关比没有更糟。 | P2（已确认设计） | `evaluate.py:28-30,381-387` | **已确认符合设计**，是正确取舍。 |
| E-4 | 算子新增要改三处（`operators.json` + `_SHAPES` + qweave impl_symbol），不是纯配置；但 `_check_registry` 保证三处不一致一定 `E1015` 暴露，不会静默。 | P2（扩展性） | `evaluate.py:125-174`；见 D6 | 属"封闭白名单"的应有代价，可接受。 |

**多了什么**：无明显冗余。`_RESERVED_COLUMNS`、`MAX_DEPTH=64`、`CONST_RTOL=1e-12` 都是合理守卫。

---

## D3 · 业界方案（借鉴什么 / 拒绝什么）

| 方案 | 裁决 | 加密理由 / 证据 |
|---|---|---|
| qweave `with_alphas`（成熟表达式内核 + CSE） | **采纳** | `evaluate.py:395`；换内核即换求值语义，故 `impl_version` 取内核版本（`impls.py:62-64`），指纹随内核变 |
| polars 长表 + 稳定排序 | 采纳 | `evaluate.py:340-353`；`time` 用 int64 纳秒不经 datetime（`:322-325`） |
| 白名单 AST 遍历（校验+构造合一条递归） | 采纳 | `evaluate.py:177-215`；分两步会"校验走一支、构造走另一支" |
| 自己写 rolling/相关系数算子 | **拒绝** | 全部委托 qweave；`s0/04 §3.10` / `07-components.md:16-21` 明确"自己写表达式引擎"是造轮子反例 |
| gplearn / LLM 直接生成可执行代码 | **拒绝** | 复现性与内容寻址不成立（全局 P0，U7 决定），本模块只吃白名单 AST |

---

## D4 · 代码问题（bug / 静默成功 / 口径漂移）

**已确认正确（逐条带证据）**：
1. **白名单三向自检**：名字集合 `declared != shaped` 报 E1015（`:133-145`）；arity、impl_symbol、param_domain 逐条校验（`:146-174`），注释举了"corr arity 3→4、第三参数被扔掉"的真实漂移场景（`:128-131`）。
2. **`values`/`valid_mask` 分离**：`np.isfinite(values)` 定 valid（`:422`）；`inf` 先转 NaN（`:419-421`）；`drop` 只加整行无效（`:460-472`），注释明确"先去缺失行再算窗口会更隐蔽"。
3. **行序保持**：`with_alphas` 后校验 `raw.shape[0]==rows.size`（`:405-412`），行数变了说明对齐失效。
4. **退化检查三重**：全 NaN（`:423-429`）、常数（`:435-443`，挡住 V4 三个全零常数因子入库）、空截面 >20% 首根之后（`:446-457`，头部预热不计）。
5. **`nan_policy` 不改算子语义**：只决定 valid 完整性，两者 `values` 完全相同（docstring `:17-26`）。
6. **保留列冲突 fail-closed**：字段叫 `symbol`/`time` 会覆盖分组列，E1003（`:329-338`）。
7. **缺输入 fail-closed**：panel/payload 缺失或不合形状都是 E3003/E1003（`:86-122`）。

**发现的问题**：见 D2 表 E-1（字段语义护栏责任边界说明）、E-2（推理通道阶段边界）。**无 P0/P1 级代码 bug**。funding 透传（必查项）经真实调用确认成立。

---

## D5 · 设计文档是否需更新

| 篇目:行号 | 现状 | 建议 |
|---|---|---|
| `s0/04-capability.md` §3.10 | 与实现一致（AST→qweave、values/valid_mask、node_cache 禁用、退化检查） | 无需更新 |
| `11-field-operator-registry.md` §4.1 | "JSON 是真相"、impl_symbol 由 registry 给 | 补一句求值器三向自检（arity/impl_symbol/param_domain）的具体校验项，便于审计 |
| `s0/04-capability.md` §3.10 末段 | 列集开放（C8）已写明 | 补 E-1 的责任边界："字段单位错配（费率 vs 价格）的护栏在供给层与编译层，求值器不校语义" |

---

## D6 · 是否需扩展 / 当前是否支持

| 扩展场景 | 当前支持度 | 说明 |
|---|---|---|
| **新增 funding/OI/basis 字段的因子** | ✅ 零代码 | 字段进 Panel 即可被 AST 引用（本次实测 `sum(funding,3)` 通过，E-1） |
| 新增白名单算子 | ⚠️ 三处改 | `operators.json` + `_SHAPES`（`:63-83`）+ qweave 实现；三处不一致由 E1015 兜底 |
| 接入 `model_ref` 推理通道 | ❌ 不支持 | `evaluate.py:106-116` 明确拒绝；需新增通道且保证产物形状一致 |
| 开启节点级缓存 | ❌ 明确不支持 | `E1004`（`:381-387`），除非面板规模越过盈亏线 |

---

## C1–C12 相关项判定

- **C1 365 年化**：不适用（本模块不算年化）；时间轴用 int64 纳秒（`:345`），无 252 依赖 ✅
- **C8 列集开放**：✅ 强支撑——funding 字段实测可直接进 AST，无字段特判（本次真实调用）
- **C9 as-of / 只 ffill**：不适用（求值不做填充；缺失即 NaN/valid=False）
- **C16 白名单**：✅ 三向自检 + 深度上限 + 参数域校验（`:125-174,192-215`）

## R1–R7 影响

- **R1 真数据跑通**：✅ 中性（qweave 求值已在真跑中使用）
- **R2 结论可信**：✅ 强支撑——values_ref 单一来源 + 退化 fail-closed，NaN/tie/除零约定不漂移
- **R3 因子零代码扩展**：✅ 正向——列集开放使新加密流因子可零代码表达（本次实测）
- **R7 fail-closed**：✅ 强支撑——白名单/形状/退化/保留列全拒

---

## 结论

**本模块代码质量高，funding 透传（必查项）经真实调用确认成立，无 P0/P1 级 bug，可确认通过。** 三处 P2 均为说明性或阶段边界：E-1 字段语义护栏的责任边界需写明；E-2 推理通道未接（本轮不影响真挖因子）；E-3/E-4 属已确认的设计取舍。

**请确认本模块审查结果**：
- ✅ 确认通过 → 我进入第 2 个模块 `observe.py`
- ✏️ 需要补充/修正某维度 → 告诉我
- ⚠️ 某条判为需确认意图，想当场裁决 → 告诉我
