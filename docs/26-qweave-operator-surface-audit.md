# 26 · qweave 算子面全量审计（v3）

> 口径：qweave `0.6.2`。审计对象是**公开可绑定的表达式原语**，不是包里所有函数
> （`compute_alphas` / `with_labels` / `evaluate` 是求值工具，不是 AST 算子）。
> 结论：v2 的 34 个算子并不全；公开原语里还有 12 个可直接绑定，本次补齐到 **46 个**。

---

## 0. 一句话结论

qweave 0.6.2 的公开表达式原语 = **29 个 `PyExpr` 方法 + 6 个比较 dunder +
9 个模块级二元/三元函数**（其中 `wma` 是 `decay_linear` 的别名）。v2 的注册表
覆盖了其中 33 个不同的 `impl_symbol`：漏掉的是 `quantile`、`ts_count`、
`ts_rank_raw`、逐点 `max/min`、6 个比较算子与 `where_`——**共 12 个可绑定原语**。
另有 2 个分组算子被字段条件卡住、5 个内部组合原语没有公开 API，本次不入库。

---

## 1. 对照表

### 1.1 已补齐（v3，12 个）

| qweave 原语 | 形态 | 注册表名字 | 语义 / 为什么重要 |
|---|---|---|---|
| `expr.quantile(days, q)` | 双参数位 | `quantile` | 窗口分位数（q∈[0,1]）。Alpha158 的 QTLU/QTLD 基元，v2 因「只支持一个参数位」缺席 |
| `expr.ts_count(days)` | 窗口 | `ts_count` | 全窗口内非缺失根数。资金费等稀疏字段的**数据覆盖度**本身是信号 |
| `expr.ts_rank_raw(days)` | 窗口 | `ts_rank_raw` | 末值在窗口内的计数名次（0..d-1）；`ts_rank` 是它的 `(raw+1)/d` 归一版 |
| `qw.max(a,b)` / `qw.min(a,b)` | 二元 | `elem_max` / `elem_min` | **逐点**取大/取小。原来的 `max`/`min` 是 `ts_max`/`ts_min` 窗口版，名字不冲突 |
| `a>b` / `a<b` / `a>=b` / `a<=b` / `a==b` / `a!=b` | 二元 | `gt` / `lt` / `ge` / `le` / `eq` / `ne` | 返回 **0/1 浮点**，不是布尔；`where_` 的条件入口 |
| `qw.where_(cond, a, b)` | 三元 | `where_` | 条件选择。可写「只在上涨日统计下行波动」这类原本写不出的因子 |

`elem_max` 等名字与已有 `max`（滚动最大）刻意不同：同一个名字不能既指窗口归约又指逐点二元，
否则 AST 读起来一样、算出来完全不同。

### 1.2 别名（不新增）

| qweave 原语 | 处理 | 理由 |
|---|---|---|
| `expr.wma(days)` | 不登记 | 实测 `repr` 与数值都等价于 `decay_linear(days)`（源码里就是别名）。再登记一份只会让 GP 把预算花在去重上 |

### 1.3 被字段条件卡住（未登记）

| qweave 原语 | 缺口 | 补法 |
|---|---|---|
| `qw.group_neutralize(a, group)` | 第二参数**必须是列表达式**（`lit(1)` 会报 `group argument must be a column expression`）；当前 `fields.json` 没有分组字段 | 先给面板加板块列（L1/DeFi/Meme），再登记；不能用连续价格列冒充分组，否则每组只有一个标的、结果恒为 0 |
| `qw.group_rank(a, group)` | 同上 | 同上 |

### 1.4 无公开 API（无法绑定）

这 5 个名字只在 qweave 的 450 内置因子表达式里出现，`qweave.qweave` 模块与 `PyExpr`
都没有暴露：`sma`（GTJA 递归加权平滑）、`gtja_wma`、`scan_mul`、`multi_resi`、
`conditional_beta`。要复刻必须在我们这边自己实现递归算符，那已经不是「复用 qweave 算子」，
而且递归状态与 fail-closed 语义要重新设计——本次不做。

---

## 2. 450 内置因子实际用到什么

对 `qlib_alpha158` / `worldquant_alpha101` / `gtja_alpha191` 的全部 450 个表达式
解析函数名并计数（含内部原语）：

| 类别 | 原语 |
|---|---|
| v2 已覆盖 | `div/sub/mul/add`、`delay`、`ts_sum/ts_mean/ts_std/ts_max/ts_min/ts_rank/ts_arg_max/ts_arg_min`、`delta`、`correlation/covariance`、`abs/log/sign/neg`、`scale`、`power/signed_power`、`decay_linear`、`slope/rsquare/resi`、`product` |
| v3 新覆盖 | `where`（121 次）、`gt/lt/ge/le/eq`（170 次，内置因子没用 `ne`）、`max/min`（139 次）、`quantile`（10 次） |
| 仍未覆盖 | `group_neutralize`（21 次：缺分组字段）、`sma`（82 次）、`gtja_wma`/`scan_mul`/`multi_resi`/`conditional_beta`（5 次：无公开 API） |

也就是说：补完 v3 后，450 个内置因子用到的算子只剩**分组字段**和**内部递归原语**
两类无法直接表达；v2 的缺口不是理论问题——`where` 在 450 因子里出现了 121 次。

---

## 3. 实现要点

### 3.1 双参数位：`kind = count | fraction`

`param_domain` 原来是「一个参数域、必须是 ≥1 的整数」。本次加可选 `kind`：

- `count`（默认）：正整数窗口/位移，旧 34 个算子行为不变；
- `fraction`：[0,1] 实数，必须同时声明 `min`/`max`，用于 `quantile.q`。

校验集中在 `capability/registry.py::param_error`，`evaluate._Builder`、
`mining.normalise_ast`、`factors._check_ast` 三处都调用它。不这么做就会出现
「AST 规范化放行、内置因子校验放行、到求值才炸」的分叉。

`quantile` 的 AST 写作 `{"op":"quantile","args":[field, {"const":20}, {"const":0.75}]}`：
两个参数按 JSON 声明顺序对应末尾两个 const 节点，窗口不会被 q 的域校验。

### 3.2 两个新调用形状

- `method_param2` → `expr.quantile(window, q)`；
- `call_ternary` → `qw.where_(cond, a, b)`。

`_check_registry` 现在同时校验形状与参数域个数（0/1/2），不再只管「带参恰好一个」。

### 3.3 搜索空间

`_gp_operator_order(reg)` 从注册表派生三张表，因此新的一元/二元/窗口算子自动进入
GP 与 beam 的单步扩展；`_GP_ORDER_HINT` 未改，旧算子的 opcode 索引保持不变。

两个新算子进排除表（`capability/mining.py::_GP_EXCLUDE`）：
`quantile`（生成器只会给带参算子追加一个 const，双参数位会造出 arity 不符的 AST）、
`where_`（当前生成器只支持一元/二元）。它们仍可写进先验池与计划，只是不自动生成。

---

## 4. 验证

- 新测试 `tests/test_v5_v3_operators.py`（11 项）：`quantile` 中位数闭式解与 q 端点、
  双参数 AST 在三处校验的通行/拒绝、`ts_rank_raw` 与 `ts_rank` 的换算、
  `ts_count` 的 NaN 计数、逐点 max/min、6 个比较算子的 0/1 浮点、条件分支、
  注册表形状与参数域。
- `tests/test_v5_search.py` 的排除表断言同步扩到 6 个，并确认 `lost` 只多出 `quantile`。
- 全量：**908 项测试通过**（v2 的 897 + 新增 11）。

---

## 5. 仍然开放

1. 分组字段：先定板块口径再登记 `group_neutralize`/`group_rank`。
2. `sma` 等 5 个内部原语：若要复刻，按新算子走完整语义对拍，不要直接照搬 GTJA 公式。
3. `vwap` 数据缺口：与算子无关，见 `docs/24-qweave-450-crypto-audit.md`。
