"""`evaluate` · 表达式求值：JSON AST → qweave `Expr` → 面板（s0/04 §3.10）。

这个能力只做四件事，其余全部交给 qweave：

1. **AST 白名单遍历**（C16）。`operators.json` 是唯一真相：算子名、`arity`、
   `param_domain`、`degenerate_policy` 都在那里。本模块的 `_BINDINGS` 只负责
   「注册表里的名字 → qweave 的调用方式」，两边条目集必须完全相等，多一条
   少一条都 `E1015`——否则白名单上写着支持，运行到那一步才发现没有绑定。
2. **求值**。`qw.with_alphas` 保持原行序追加一列，因此不需要第二次转置：
   行序就是网格的行序，直接按 `(t, s)` 下标写回。
3. **`values` 与 `valid_mask` 分开**。把无效值写成 0 会让截面排序把「没数据」
   当成「中间值」——这是本层最贵的一次静默错误。
4. **`values_ref` 是唯一暴露来源**。`observe` / `combine` / `certify` 只读它，
   不得各自重算：一旦重算，NaN 传播、tie 策略、除零约定三处都可能不同，而
   三处都自洽时没有任何检查点能发现。

`nan_policy` 不改变算子语义（名字空间是 `single`，求值语义只有一种），它决定
**`valid_mask` 的完整性要求**：

- `propagate`（默认）：逐格判定——`valid = isfinite(values)`，一格缺就是那一格无效。
- `drop`：完整截面——某时点只要有一格不可算，该时点的**全部格**都标无效
  （`valid[t, :] = False`）。这是「只保留完整截面」的那一类研究约定。

`values` 两者完全相同：`drop` 不会把缺失补成值、也不会把窗口缩短。若它去动
`values`（例如先剔除缺失行再算时序窗口），窗口就会跨过缺失去用更早的观测，
那是比 NaN 更隐蔽的错误。`drop` 的代价因此只落在「可用时点数」上，是可数的。

`node_cache` 默认 `false`，且本阶段**不支持** `true`：节点级落盘缓存实测净亏
65 倍（§3.10 末段），当前只依赖 qweave 的同批 CSE。给一个「开了但没生效」的
开关比没有这个开关更糟，所以直接失败。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import polars as pl
import qweave as qw

from v5.capability.panel import Panel, array_digest
from v5.capability.registry import default_registry
from v5.contracts.errors import E1003, E1004, E1015, E3003, FlowError

VALUE_COL = "__v5_value__"
MAX_DEPTH = 64
# 空截面上限：首根有效 bar 之后，全 NaN 的截面占比超过它即整批拒绝。
# 与 I61 的 20% 是同一个项目约定（不是同一条规则）：一个因子在超过五分之一的
# 时点上算不出任何值，它的 IC 序列不是「有点稀疏」，是「没有样本」。
MAX_EMPTY_RATIO = 0.20
# 常数暴露的相对判定阈值：通篇只差浮点末位的因子与常数因子没有区别。
CONST_RTOL = 1e-12
_NAN_POLICIES = frozenset({"propagate", "drop"})
_RESERVED_COLUMNS = frozenset({"symbol", "time"})

# 算子的**调用形状**，不含目标符号：符号由 `operators.json` 的 `impl_symbol`
# 给出（`11` §4.1「JSON 是真相」）。这里只回答「参数怎么摆」——这件事 JSON
# 表达不了，也没必要表达。
#   method_param  expr.<sym>(int)      arity 2
#   method        expr.<sym>()         arity 1
#   method_binary expr.<sym>(rhs)      arity 2
#   call_param    qw.<sym>(a, b, int)  arity 3
_SHAPES: dict[str, tuple[str, int]] = {
    "ref": ("method_param", 2),
    "delay": ("method_param", 2),
    "sum": ("method_param", 2),
    "mean": ("method_param", 2),
    "std": ("method_param", 2),
    "max": ("method_param", 2),
    "min": ("method_param", 2),
    "delta": ("method_param", 2),
    "rank": ("method", 1),
    "scale": ("method", 1),
    "abs": ("method", 1),
    "log": ("method", 1),
    "sign": ("method", 1),
    "add": ("method_binary", 2),
    "sub": ("method_binary", 2),
    "mul": ("method_binary", 2),
    "div": ("method_binary", 2),
    "corr": ("call_param", 3),
    "cov": ("call_param", 3),
}


def _input(call, kind: str) -> Any:
    for item in call.inputs:
        if item is not None and item.kind == kind:
            return item.data
    raise FlowError(
        E3003, f"evaluate 没有拿到 {kind} 输入", path=f"evaluate.inputs.{kind}",
        hint="执行器负责解析；能力层不自己读盘",
    )


def _panel(call) -> Panel:
    data = _input(call, "panel")
    if not isinstance(data, Panel):
        raise FlowError(
            E3003, f"evaluate 的 panel 输入不是 Panel，得到 {type(data).__name__}",
            path="evaluate.inputs.panel",
        )
    return data


def _payload(call) -> Mapping[str, Any]:
    data = _input(call, "payload")
    if isinstance(data, Mapping) and "model_ref" in data:
        # 表达式求值通道不认识模型引用。两条路径的产物形状必须一致，
        # 在接推理通道之前不得用其中一条冒充另一条。
        raise FlowError(
            E1003,
            f"payload 是 model_ref，表达式求值通道不处理它: {data['model_ref']!r}",
            path="evaluate.inputs.payload",
            hint="model_ref 走推理通道（尚未接入）；本阶段只接受表达式 AST",
        )
    if not isinstance(data, Mapping):
        raise FlowError(
            E1003, f"payload 必须是 JSON AST 映射，得到 {type(data).__name__}",
            path="evaluate.inputs.payload",
        )
    return data


def _check_registry(reg) -> None:
    """注册表、形状表、qweave 三者必须逐条对得上。

    只比名字集合是不够的：`corr` 的 arity 从 3 改成 4，名字集合不变，而实现
    只消费前两个参数 + 末位窗口，第三个参数既不校验也不构造、直接被扔掉——
    AST 变了、指纹变了、结果却是另一个表达式的值。所以这里同时校验：
    arity 与形状一致、`impl_symbol` 真的存在于 qweave、带参算子恰好一个参数域。
    """
    declared = set(reg.operators)
    shaped = set(_SHAPES)
    if declared != shaped:
        raise FlowError(
            E1015,
            "算子白名单与求值形状表不一致",
            path="evaluate.operator_set",
            hint="operators.json 增删算子时必须同步 _SHAPES",
            context={
                "declared_only": sorted(declared - shaped),
                "shaped_only": sorted(shaped - declared),
            },
        )
    for name, spec in reg.operators.items():
        shape, arity = _SHAPES[name]
        symbol = spec.get("impl_symbol")
        if int(spec["arity"]) != arity:
            raise FlowError(
                E1015,
                f"算子 {name!r} 的 arity={spec['arity']} 与形状 {shape}({arity}) 不符",
                path=f"operators.{name}.arity",
            )
        if shape.startswith("method"):
            if not hasattr(qw.PyExpr, str(symbol)):
                raise FlowError(
                    E1015,
                    f"算子 {name!r} 的 impl_symbol={symbol!r} 不是 qweave PyExpr 方法",
                    path=f"operators.{name}.impl_symbol",
                )
        elif not hasattr(qw, str(symbol)):
            raise FlowError(
                E1015,
                f"算子 {name!r} 的 impl_symbol={symbol!r} 不是 qweave 模块函数",
                path=f"operators.{name}.impl_symbol",
            )
        if shape in ("method_param", "call_param"):
            if len(spec.get("param_domain") or {}) != 1:
                raise FlowError(
                    E1015,
                    f"算子 {name!r} 是 {shape}，必须恰好声明 1 个参数域",
                    path=f"operators.{name}.param_domain",
                )


class _Builder:
    """把 JSON AST 递归转成 qweave `Expr`，同时完成白名单校验。

    校验与构造合在一条递归里：分两步做就要遍历两次 AST，两次之间没有任何
    「校验通过」的传递保证——迟早出现「校验走一支、构造走另一支」。
    """

    def __init__(self, reg, panel: Panel) -> None:
        self.reg = reg
        self.panel = panel
        self.nodes = 0
        self.depth = 0
        self.used: set[str] = set()
        self.fields: set[str] = set()
    def build(self, node: Any, path: str, depth: int = 1) -> Any:
        if depth > MAX_DEPTH:
            raise FlowError(
                E1003, f"表达式深度超过 {MAX_DEPTH}", path=path,
                hint="深层表达式是搜索失控的症状；先检查生成器",
            )
        self.nodes += 1
        self.depth = max(self.depth, depth)
        if not isinstance(node, Mapping):
            raise FlowError(
                E1003, f"AST 节点必须是映射，得到 {type(node).__name__}", path=path,
            )
        keys = set(node)
        if keys == {"field"}:
            return self._field(node["field"], path)
        if keys == {"const"}:
            return self._const(node["const"], path)
        if keys == {"op", "args"}:
            return self._op(node, path, depth)
        raise FlowError(
            E1003,
            f"AST 节点键 {sorted(keys)} 非法",
            path=path,
            hint='只允许 {"field": 名} / {"const": 数} / {"op": 名, "args": [...]}',
        )

    def _field(self, name: Any, path: str) -> Any:
        if not isinstance(name, str) or not name:
            raise FlowError(E1003, f"field 名必须是非空字符串，得到 {name!r}", path=path)
        if name not in self.panel.fields:
            raise FlowError(
                E1003,
                f"表达式引用了面板里没有的字段 {name!r}",
                path=path,
                hint=f"面板字段: {sorted(self.panel.fields)}",
            )
        self.fields.add(name)
        return qw.col(name)

    def _const(self, value: Any, path: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FlowError(
                E1003, f"const 必须是实数，得到 {value!r}", path=path,
                hint="布尔不是常数：True 会静默变成 1.0",
            )
        if not np.isfinite(float(value)):
            raise FlowError(E1003, f"const 必须是有限值，得到 {value!r}", path=path)
        return qw.lit(float(value))

    def _op(self, node: Mapping[str, Any], path: str, depth: int) -> Any:
        name = node["op"]
        if not isinstance(name, str):
            raise FlowError(E1003, f"op 名必须是字符串，得到 {name!r}", path=path)
        spec = self.reg.operators.get(name)
        if spec is None:
            raise FlowError(
                E1015, f"算子 {name!r} 不在白名单", path=path,
                hint=f"已登记: {sorted(self.reg.operators)}",
            )
        args = node["args"]
        if not isinstance(args, (list, tuple)) or len(args) != int(spec["arity"]):
            got = len(args) if isinstance(args, (list, tuple)) else type(args).__name__
            raise FlowError(
                E1003,
                f"算子 {name!r} 需要 {spec['arity']} 个参数，得到 {got}",
                path=path,
            )
        self.used.add(name)
        shape, _arity = _SHAPES[name]
        symbol = str(spec["impl_symbol"])

        if shape == "method_param":
            # 末位是整数参数（lag / window）：必须是 const 且落在 param_domain
            head = self.build(args[0], f"{path}.args[0]", depth + 1)
            n = self._int_param(name, args[-1], spec, path)
            return getattr(head, symbol)(n)

        if shape == "call_param":
            # 两表达式 + 窗口；窗口是参数，不构造成 Expr（构造了只能丢掉）
            left = self.build(args[0], f"{path}.args[0]", depth + 1)
            right = self.build(args[1], f"{path}.args[1]", depth + 1)
            n = self._int_param(name, args[-1], spec, path)
            return getattr(qw, symbol)(left, right, n)

        left = self.build(args[0], f"{path}.args[0]", depth + 1)
        if shape == "method":
            return getattr(left, symbol)()
        return getattr(left, symbol)(
            self.build(args[1], f"{path}.args[1]", depth + 1))

    def _int_param(self, name: str, node: Any, spec: Mapping[str, Any],
                   path: str) -> int:
        if not isinstance(node, Mapping) or set(node) != {"const"}:
            raise FlowError(
                E1004,
                f'算子 {name!r} 的末位参数必须是 {{"const": 整数}}，得到 {node!r}',
                path=f"{path}.args[-1]",
                hint="窗口/位移是参数，不是表达式",
            )
        # 窗口节点也是 AST 的一个节点：`nodes` 报的是「这棵树有多大」，
        # 少算一个参数节点会让两个规模不同的候选报出同样的节点数。
        self.nodes += 1
        value = node["const"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise FlowError(
                E1004, f"算子 {name!r} 的参数必须是整数，得到 {value!r}",
                path=f"{path}.args[-1]",
            )
        domains = spec.get("param_domain") or {}
        if len(domains) != 1:
            raise FlowError(
                E1015,
                f"算子 {name!r} 声明了 {len(domains)} 个参数域，绑定只支持 1 个",
                path=path,
            )
        (pname, domain), = domains.items()
        lo, hi = domain.get("min"), domain.get("max")
        if lo is not None and value < int(lo):
            raise FlowError(
                E1004, f"{name}.{pname}={value} < 下界 {lo}", path=f"{path}.args[-1]",
            )
        if hi is not None and value > int(hi):
            raise FlowError(
                E1004, f"{name}.{pname}={value} > 上界 {hi}", path=f"{path}.args[-1]",
            )
        return int(value)


def _to_long(panel: Panel, fields: set[str]):
    """面板 → 长表（t 主序，与 `arr.reshape(-1)` 一致）。

    `time` 用 int64 纳秒而不是 datetime：面板的时间轴本来就是整数纳秒，
    转一圈 datetime 只会多一次时区解释的机会。qweave 按 `(symbol, time)` 排序，
    与行的先后无关。
    """
    n_bars, n_sym = panel.shape
    idx = np.arange(n_bars * n_sym)

    clash = sorted(set(fields) & _RESERVED_COLUMNS)
    if clash:
        # 长表的分组列与时间列固定叫 `symbol` / `time`；同名字段会覆盖它们，
        # qweave 随即按一列因子值分组，结果看起来正常、含义已经变了。
        raise FlowError(
            E1003,
            f"面板字段名与长表保留列冲突: {clash}",
            path="evaluate.inputs.panel",
            hint="保留列是 symbol / time；字段注册表里不得出现这两个名字",
        )

    data: dict[str, Any] = {
        "symbol": pl.Series(
            "symbol", [panel.symbols[i % n_sym] for i in range(idx.size)],
            dtype=pl.String,
        ),
        "time": pl.Series("time", panel.times[idx // n_sym], dtype=pl.Int64),
    }
    for name in sorted(fields):
        data[name] = pl.Series(
            name,
            np.ascontiguousarray(panel.fields[name]).reshape(-1)[idx],
            dtype=pl.Float64,
        )
    return pl.DataFrame(data), idx


def build(call) -> dict:
    params = call.params
    payload = _payload(call)
    panel = _panel(call)
    reg = default_registry()
    _check_registry(reg)

    operator_set = str(params["operator_set"])
    if operator_set != reg.operators_version:
        # 白名单不是配置项：它变了就是求值口径变了。只记版本号而不校验，
        # 「冻结的算子集」就只是一句声明。
        raise FlowError(
            E1015,
            "operator_set 与本次加载的算子白名单不一致",
            path="evaluate.params.operator_set",
            hint="传 registry.operators_version（白名单内容摘要）",
            context={"expected": reg.operators_version, "got": operator_set},
        )

    policy = str(params["nan_policy"])
    if policy not in _NAN_POLICIES:
        raise FlowError(
            E1004, f"nan_policy={policy!r} 不在 {sorted(_NAN_POLICIES)}",
            path="evaluate.params.nan_policy",
        )
    if params["node_cache"]:
        raise FlowError(
            E1004,
            "node_cache=true 本阶段不受支持：节点级落盘缓存实测净亏 65 倍（s0/04 §3.10）",
            path="evaluate.params.node_cache",
            hint="当前只依赖 qweave 的同批 CSE；接缓存要等面板规模越过盈亏线",
        )

    builder = _Builder(reg, panel)
    expr = builder.build(payload, "evaluate.payload")

    n_bars, n_sym = panel.shape
    frame, rows = _to_long(panel, builder.fields)
    try:
        out = qw.with_alphas(frame, "symbol", "time", [expr.alias(VALUE_COL)])
    except FlowError:
        raise
    except Exception as exc:  # noqa: BLE001 - 内核异常不得当成空结果
        raise FlowError(
            E3003,
            f"qweave 求值失败: {type(exc).__name__}: {exc}",
            path="evaluate.outputs.values",
        ) from exc

    raw = out[VALUE_COL].to_numpy()
    if raw.shape[0] != rows.size:
        raise FlowError(
            E3003,
            f"求值结果行数 {raw.shape[0]} 与输入行数 {rows.size} 不符",
            path="evaluate.outputs.values",
            hint="with_alphas 必须保持原行序；行数变了说明对齐已失效",
        )

    values = np.full((n_bars, n_sym), np.nan, dtype="float64")
    values.reshape(-1)[rows] = np.asarray(raw, dtype="float64")

    # `degenerate_policy=nan`：除零/溢出产出 inf 属退化，按注册表声明转成 NaN。
    # 不转的话它会一路活到 as_field 或下游统计，报错点离现场已经很远。
    inf_cells = int(np.isinf(values).sum())
    if inf_cells:
        values[np.isinf(values)] = np.nan
    valid = np.isfinite(values)
    if not valid.any():
        raise FlowError(
            E3003,
            "求值结果没有任何有限值",
            path="evaluate.outputs.values",
            hint="全 NaN 的暴露不是信号；检查窗口是否长于面板、字段是否全缺",
        )

    # ---- 退化检查：常数暴露 -------------------------------------------
    # `sub(close, close)` 这类暴露在 `rank()` 里会变成一串并列名次、在 IC 里
    # 变成 NaN，最后仍以「一条真实候选」的身份进 `n_trials`。V4 的实测缺陷
    # 就是 3 个全零常数因子入库（11 §3.3 / I42）。
    finite = values[valid]
    spread = float(finite.max() - finite.min())
    if spread <= CONST_RTOL * max(1.0, float(np.abs(finite).max())):
        raise FlowError(
            E3003,
            f"求值结果是常数暴露（极差 {spread:g}）",
            path="evaluate.outputs.values",
            hint="常数因子没有截面区分度；它只能抬高 n_trials，不能产生信号",
        )

    # ---- 退化检查：空截面 ---------------------------------------------
    row_valid = valid.any(axis=1)
    first = int(np.argmax(row_valid))
    tail = row_valid[first:]
    empty_bars = int((~tail).sum())
    if tail.size and empty_bars / tail.size > MAX_EMPTY_RATIO:
        raise FlowError(
            E3003,
            f"首根有效 bar 之后有 {empty_bars}/{tail.size} 个时点整截面为空",
            path="evaluate.outputs.valid_mask",
            hint=f"空截面占比超 {MAX_EMPTY_RATIO:.0%} 时 IC 序列没有样本；"
                 "头部预热不计入，那是窗口未满而非无数据",
        )

    incomplete = 0
    if policy == "drop":
        # 完整截面：某时点只要有一格不可算，整行都标无效。`values` 不动——
        # 先去缺失行再算时序窗口，会让窗口跨过缺失去用更早的观测。
        complete = valid.all(axis=1)
        incomplete = int((~complete).sum())
        if not complete.any():
            raise FlowError(
                E3003,
                "nan_policy=drop 下没有任何完整截面",
                path="evaluate.outputs.valid_mask",
                hint="每个时点都有缺失成员；要么放宽策略，要么补数据",
            )
        valid = valid & complete[:, None]

    node_stats = {
        "nodes": int(builder.nodes),
        "depth": int(builder.depth),
        "operators": len(builder.used),
        "fields": sorted(builder.fields),
        "nan_policy": policy,
        "dropped_bars": int(incomplete),
        "empty_bars": empty_bars,
        "degenerate_cells": inf_cells,
        "valid_cells": int(valid.sum()),
        # 节点级缓存：本阶段恒为关闭。`hits`/`total`/`key_prefix` 一律给 `null`
        # 而不是 0——「没查缓存」与「查了、命中 0 次」是两件事，写成 0 会被读成
        # 命中率 0%，仿佛缓存跑了但没生效。
        "cache": {
            "enabled": False,
            "hits": None,
            "total": None,
            "key_prefix": None,
        },
    }

    return {
        "values_ref": array_digest(values),
        "valid_mask_ref": array_digest(valid),
        "node_stats": node_stats,
        "operators_used": sorted(builder.used),
        # 数据通道：值随调用单传递，能力层不落盘（与 label 的 score/valid_mask 同）
        "values": values,
        "valid_mask": valid,
    }
