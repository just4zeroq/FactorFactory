"""内置因子池：先验因子的唯一定义处（18-plan-schema.md `search.prior_pool`）。

计划里的 `prior_pool` 只写名字（`schema type=ref`：只接受引用标识，不接受内联
表达式）。名字到 AST 的解析必须发生在**编排侧**，并且只有这一处：

- 能力层（`ops/search.py`）只消费 `market_view.prior_candidates`，不读盘、不内联
  —— 否则「口径只有一个来源」在搜索工位就破了；
- 计划层不持有表达式 —— 否则同一因子在两个计划里能写成两份不同的内容身份。

三条校验在加载时全做，任何一条不满足就整体失败（`E1015`），不做「跳过这一条」：

1. 每个 `op` 必须在 `registry/operators.json` 里，且 `arity` 与 `args` 长度一致
   （带参算子的末位必须是 `{"const": int}`，与 `normalise_ast` 同一规则）；
2. 每个 `field` 必须在 `registry/fields.json` 里；
3. 窗口/滞后必须落在该算子的 `param_domain` 内 —— 越界会让 execute 阶段才报
   `E1003`，而那时已经烧掉了前三个工位的时间。

**为什么要有这张表**：没有先验池时，一批 GP 候选全被裁决拒绝，无法区分
「数据没有信号」和「搜索没找到」——两者在产物里长得一模一样。先验因子是
唯一的锚点：`mom60` 在 ACCEPTANCE 里就是已知答案（年化 SR≈0.675）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from capability.mining import normalise_ast
from capability.registry import default_registry
from contracts.errors import E1015, FlowError


def _op(name: str, *args: Any) -> dict[str, Any]:
    return {"op": name, "args": list(args)}


def _field(name: str) -> dict[str, Any]:
    return {"field": name}


def _const(value: int | float) -> dict[str, Any]:
    return {"const": value}


def _ret(name: str, window: int) -> dict[str, Any]:
    """`price / delay(price, window) - 1`，即 window 根 bar 的简单收益率。"""
    d = _op("ref", _field(name), _const(window))
    return _op("div", _op("sub", _field(name), d), d)


# 名字 → 表达式。键是**唯一身份**：同一名字在库存里不允许有两种写法。
# 全部只使用已注册的 9 个字段与 19 个算子，不引入新原语。
BUILTIN_FACTORS: dict[str, dict[str, Any]] = {
    # --- 价格动量 / 反转 -------------------------------------------------
    "mom5": _ret("close", 5),
    "mom20": _ret("close", 20),
    "mom60": _ret("close", 60),
    # 反转＝动量的相反数（负号在值位置，不占窗口参数位）
    "rev5": _op("mul", _const(-1), _ret("close", 5)),
    "rev1": _op("mul", _const(-1), _ret("close", 1)),
    # --- 波动与形态 -------------------------------------------------------
    # 20 根日收益的时序标准差；收益用 delay(close,1) 构造，不引入 pct_change
    "rv20": _op("std", _ret("close", 1), _const(20)),
    # 日内振幅占收盘价
    "hl_range": _op("div", _op("sub", _field("high"), _field("low")), _field("close")),
    # 收盘价相对 VWAP 的偏离
    "vwap_gap": _op("div", _op("sub", _field("close"), _field("vwap")), _field("vwap")),
    # --- 量能 -------------------------------------------------------------
    "vol_ratio": _op("div", _field("volume"),
                     _op("mean", _field("volume"), _const(20))),
    # --- 衍生品截面（只在 futures 数据集可用）-----------------------------
    # 资金费是持有成本：多头在正资金费下净收益为负，故取相反数
    "funding_carry": _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7))),
    # 更长窗口的资金费carry：7 个 funding 事件约 2.3 天，2026Q2 的短窗口 IC
    # 衰减到 0；14/21 个事件覆盖 5~7 天，与 5 日前瞻收益的持有期更匹配，
    # 且在 2024H2/2025H1/2025H2/2026Q1/Q2/Q3Q4 分段 IC 上保持同号。
    "funding_carry14": _op("mul", _const(-1),
                           _op("mean", _field("funding_rate"), _const(14))),
    "funding_carry21": _op("mul", _const(-1),
                           _op("mean", _field("funding_rate"), _const(21))),
    # 低波动异象：截面 IC 为负说明高波动标的后续收益更差；取负号作为因子。
    # 它不依赖资金费数据，可作为独立的经济学锚点，也是 funding 失效时的对照。
    "low_vol20": _op("mul", _const(-1),
                     _op("std", _ret("close", 1), _const(20))),
    "low_vol60": _op("mul", _const(-1),
                     _op("std", _ret("close", 1), _const(60))),
    # 资金费carry + 低波动：两项各自截面标准化后等权，降低单一信号失效风险。
    "funding_lowvol14": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(14)))),
        _op("scale", _op("mul", _const(-1),
                         _op("std", _ret("close", 1), _const(20)))),
    ),
    # 资金费的截面秩版本：抑制极端 funding 事件对分位组合的影响。
    "funding_carry_rank7": _op(
        "rank",
        _op("mul", _const(-1), _op("mean", _field("funding_rate"), _const(7))),
    ),
    # 组合锚点：7 事件资金费carry 是主收益来源；量能比在 2026Q2/Q3Q4 的截面 IC
    # 为正，基差水平在 2026Q1/Q2 为正。两项各自截面标准化后与资金费等权，
    # 用不同经济学来源对冲单一 carry 的时段失效。
    "funding_volratio": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    # 长窗口版本：Q3/Q4 原始截面 IC 上 14/21 事件资金费强于 7 事件。
    "funding_volratio14": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(14)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    "funding_volratio21": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(21)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    # 交互项：负资金费只有在成交活跃时才更容易被纠正。原始截面 IC 在
    # 2024H2/2025H1/2025H2/2026Q1/Q2/Q3Q4 六段全部同号，优于加法组合。
    "funding_vol_interaction": _op(
        "mul",
        _op("mul", _const(-1), _op("mean", _field("funding_rate"), _const(7))),
        _op("div", _field("volume"), _op("mean", _field("volume"), _const(20))),
    ),
    "funding14_vol_interaction": _op(
        "mul",
        _op("mul", _const(-1), _op("mean", _field("funding_rate"), _const(14))),
        _op("div", _field("volume"), _op("mean", _field("volume"), _const(20))),
    ),
    "funding14_vol_interaction_scaled": _op(
        "mul",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(14)))),
        _op("scale", _op("div", _field("volume"),
                         _op("mean", _field("volume"), _const(20)))),
    ),
    "funding_basis7": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7)))),
        _op("scale", _op("mean", _field("basis"), _const(7))),
    ),
    "funding_volratio_basis": _op(
        "add",
        _op("scale", _op("mul", _const(-1),
                         _op("mean", _field("funding_rate"), _const(7)))),
        _op("add",
            _op("scale", _op("div", _field("volume"),
                             _op("mean", _field("volume"), _const(20)))),
            _op("scale", _op("mean", _field("basis"), _const(7)))),
    ),
    # 持仓量变化
    "oi_change20": _ret("open_interest", 20),
    # 基差（perp 相对现货溢价）的近期均值
    "basis_carry": _op("mean", _field("basis"), _const(7)),
    # --- S8 新增锚点（top200 挖掘）-------------------------------------
    # carry 的变化率比水平更贴近 5 日前瞻：基差/资金费的收敛速度反映套利压力。
    "funding_delta": _op("delta", _op("mean", _field("funding_rate"), _const(14)), _const(3)),
    "basis_delta": _op("delta", _op("mean", _field("basis"), _const(14)), _const(3)),
    # Amihud 非流动性：|收益|/成交额，低流动性溢价；取负号作因子。
    "illiquidity": _op(
        "mul", _const(-1),
        _op("div",
            _op("abs", _ret("close", 1)),
            _op("mean", _field("volume"), _const(20))),
    ),
    # 区间效率：(high-low)/close 与已实现波动的比值——信息含量高的波动更易延续。
    "range_efficiency": _op(
        "div",
        _op("div", _op("sub", _field("high"), _field("low")), _field("close")),
        _op("std", _ret("close", 1), _const(20)),
    ),
    # Kaufman 效率比率：|10日净位移| / 10日路径总长。取值 [0,1]，越高说明
    # 价格沿直线推进（趋势纯净），越低说明来回震荡。纯 OHLCV、跨标的池稳健：
    # 2020-2026 逐年截面 IC 均值 0.06 / IR 1.28 / 正率 86%（old18 与 38 标的池同号）。
    "ts_efficiency10": _op(
        "div",
        _op("abs", _op("delta", _field("close"), _const(10))),
        _op("sum", _op("abs", _ret("close", 1)), _const(10)),
    ),
    # 时序动量（跳过最近一周，12-1 经典构造），规避短期反转污染。
    "tsmom_60_5": _op(
        "div",
        _op("sub", _op("ref", _field("close"), _const(5)),
            _op("ref", _field("close"), _const(60))),
        _op("ref", _field("close"), _const(60)),
    ),
    # 量价背离：上涨缩量（价涨量缩）常预示反转；取 volume 与 ret 的负相关。
    "vol_price_diverge": _op(
        "mul", _const(-1),
        _op("corr", _field("volume"), _ret("close", 1), _const(20)),
    ),
}

# 每个因子在语义上的最低数据集要求：只有 futures 才提供这两类数据。
# `require_market_dataset` 已经在采集侧拦一次，这里再拦一次是因为**先验池可以
# 跨计划复用**：同一份 prior_pool 在现货计划里会引用 funding_rate。
_FUTURES_ONLY_FIELDS = frozenset({"funding_rate", "open_interest", "basis"})


def available_names() -> tuple[str, ...]:
    return tuple(sorted(BUILTIN_FACTORS))


def _check_ast(node: Any, reg, path: str) -> set[str]:
    """递归校验 AST，返回其引用到的字段集。"""
    if not isinstance(node, Mapping):
        raise FlowError(E1015, f"{path} 节点必须是映射", path=path)
    keys = set(node)
    if keys == {"field"}:
        name = node["field"]
        if name not in reg.fields:
            raise FlowError(
                E1015, f"{path} 引用了未注册字段 {name!r}", path=path,
                hint=f"已注册: {sorted(reg.fields)}",
            )
        return {name}
    if keys == {"const"}:
        return set()
    if keys != {"op", "args"}:
        raise FlowError(E1015, f"{path} 键集合必须是 field/const/(op,args)", path=path)
    op_name = node["op"]
    spec = reg.operators.get(op_name)
    if spec is None:
        raise FlowError(
            E1015, f"{path} 引用了未注册算子 {op_name!r}", path=path,
            hint=f"已注册: {sorted(reg.operators)}",
        )
    args = node["args"]
    if not isinstance(args, list) or len(args) != int(spec["arity"]):
        raise FlowError(
            E1015,
            f"{path} 算子 {op_name} 需要 {spec['arity']} 个参数，得到 "
            f"{len(args) if isinstance(args, list) else '非列表'}",
            path=path,
        )
    fields: set[str] = set()
    for i, arg in enumerate(args):
        fields |= _check_ast(arg, reg, f"{path}.args[{i}]")
    # 带参算子的末位是计数，必须是 int const 且落在 param_domain 内
    domains = spec.get("param_domain") or {}
    if domains:
        last = args[-1]
        if not (isinstance(last, Mapping) and set(last) == {"const"}
                and isinstance(last["const"], int)):
            raise FlowError(
                E1015, f"{path} 算子 {op_name} 的末位参数必须是整数常量", path=path,
            )
        value = int(last["const"])
        for pname, domain in domains.items():
            lo, hi = domain.get("min"), domain.get("max")
            if lo is not None and value < int(lo):
                raise FlowError(E1015, f"{path} {pname}={value} < {lo}", path=path)
            if hi is not None and value > int(hi):
                raise FlowError(E1015, f"{path} {pname}={value} > {hi}", path=path)
    return fields


def factor_ast(name: str) -> dict[str, Any]:
    """名字 → 已规范化的 AST。未知名字 → E1015，不做模糊匹配。"""
    if name not in BUILTIN_FACTORS:
        raise FlowError(
            E1015, f"先验因子未登记: {name!r}", path="search.prior_pool",
            hint=f"可用: {list(available_names())}",
        )
    return normalise_ast(BUILTIN_FACTORS[name])


def resolve_prior_pool(
    names: Sequence[str] | None,
    *,
    available_fields: Iterable[str] | None = None,
    catalog: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把计划里的名字列表解析成 `prior_candidates`，并显式报告被排除的条目。

    三种写法，都只接受**引用标识**（不内联表达式）：

    - `mom60` —— 内置因子池（本模块的 `BUILTIN_FACTORS`）；
    - `db:<name>` / `db:<factor_id>` —— 因子库里已晋升的因子；
    - `db:*` —— 库里全部 active 因子（跨 run 复用的主要入口）。

    返回 `(resolved, skipped)`。`skipped` 不是静默丢弃：每条都带 `name` 与
    `reason`，由 Search 产物原样带出（进 `fitted_state`），所以「先验池里有 5 个、
    实际进池 2 个」在产物里是可查的。

    两种排除原因：
    - `unknown`：名字既不在内置池也不在库里 → 这是**配置错误**，抛 E1015；
    - `field_unavailable`：因子引用了当前数据集没有的字段（例如现货集引用
      `funding_rate`）。这不是配置错误 —— 同一份 prior_pool 跨市场复用是正常需求。
    """
    if not names:
        return [], []
    reg = default_registry()
    have = None if available_fields is None else {str(f) for f in available_fields}
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()

    def _emit(name: str, ast: Any, label: str) -> None:
        ast = normalise_ast(ast)
        fields = _check_ast(ast, reg, f"factors.{label}")
        if have is not None:
            missing = sorted(fields - have)
            if missing:
                skipped.append({"name": name, "reason": "field_unavailable",
                                "detail": ",".join(missing)})
                return
        resolved.append({"name": name, "expression": ast,
                         "required_fields": sorted(fields)})

    for raw in names:
        name = str(raw)
        if name in seen:  # 同名重复：只进池一次，不重复计数
            continue
        seen.add(name)
        if name.startswith("db:"):
            if catalog is None:
                raise FlowError(
                    E1015, f"{name!r} 需要因子库，但调用方没有提供",
                    path="search.prior_pool",
                    hint="把 DataRoot/catalog 传进 resolve_prior_pool",
                )
            key = name[3:]
            rows = catalog.expressions(status="active")
            if key != "*":
                rows = [r for r in rows
                        if key in (r.get("name"), r.get("factor_id"))]
            if not rows:
                raise FlowError(
                    E1015, f"因子库里没有 {key!r}", path="search.prior_pool",
                    hint="先 flow promote 把通过裁决的候选入库",
                )
            for row in rows:
                _emit(str(row["name"]), row["expression"], str(row["name"]))
            continue
        _emit(name, factor_ast(name), name)
    return resolved, skipped


def validate_all() -> dict[str, Any]:
    """加载即自检：整张表一起过，任何一条不合法就整体失败（E1015）。"""
    reg = default_registry()
    for name in available_names():
        ast = normalise_ast(BUILTIN_FACTORS[name])
        _check_ast(ast, reg, f"factors.{name}")
    return {"count": len(BUILTIN_FACTORS), "names": list(available_names())}


__all__ = [
    "BUILTIN_FACTORS", "available_names", "factor_ast",
    "resolve_prior_pool", "validate_all",
]
