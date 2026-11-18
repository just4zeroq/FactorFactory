"""计划 schema 的加载与校验（C1–C4 / C20 的结构层实现）。

这一层只管**结构**：键是否存在、类型是否对、枚举与范围是否合法、跨字段是否自洽。
实现参数（各个 `params` 内部）由能力注册表的 `param_schema` 校验，不在这里。

设计约束：
- 只返回违规清单，不抛错给调用方决定；`assert_valid` 才抛 FlowError。
- 违规必须带 `path` 与 `hint`：只说"不合法"的错误，等于让人去读源码。
- 未知键一律拒绝（E1001），不做"警告后忽略"：忽略的效果与"这个键不存在"相同，
  而人会以为它生效了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import E1001, E1004, E1008, E1020, E1021, FlowError

SCHEMA_PATH = Path(__file__).with_name("plan_schema.json")

_INTERVAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")

@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    message: str
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"code": self.code, "path": self.path, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class _Ctx:
    violations: list[Violation] = field(default_factory=list)

    def add(self, code: str, path: str, message: str, hint: str = "") -> None:
        self.violations.append(Violation(code, path, message, hint))


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类；`population: true` 绝不能当成 1 通过。
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def _check_scalar(spec: Mapping[str, Any], value: Any, path: str, ctx: _Ctx) -> None:
    kind = spec["type"]
    if kind == "str":
        if not isinstance(value, str):
            ctx.add(E1004, path, f"应为字符串，实际是 {type(value).__name__}")
    elif kind == "int":
        if not _is_int(value):
            ctx.add(E1004, path, f"应为整数，实际是 {value!r}")
            return
        _check_bounds(spec, value, path, ctx)
    elif kind == "float":
        if not _is_number(value):
            ctx.add(E1004, path, f"应为数值，实际是 {value!r}")
            return
        _check_bounds(spec, value, path, ctx)
    elif kind == "bool":
        if not isinstance(value, bool):
            ctx.add(E1004, path, f"应为布尔，实际是 {value!r}")
    elif kind == "enum":
        allowed = spec.get("values", [])
        if value not in allowed:
            ctx.add(
                E1004,
                path,
                f"取值 {value!r} 不在允许集内",
                "允许：" + " / ".join(map(str, allowed)),
            )
    elif kind == "interval":
        if not isinstance(value, str):
            ctx.add(E1004, path, f"应为区间字符串，实际是 {value!r}")
            return
        if value == "reserved":
            return
        m = _INTERVAL_RE.match(value)
        if not m:
            ctx.add(E1004, path, f"区间格式应为 YYYY-MM-DD..YYYY-MM-DD，实际 {value!r}")
        elif m.group(1) > m.group(2):
            ctx.add(E1004, path, f"区间起点晚于终点：{value}")
    else:  # pragma: no cover - schema 自身错误
        raise AssertionError(f"schema 里有未知类型 {kind!r}（{path}）")


def _check_bounds(spec: Mapping[str, Any], value: float, path: str, ctx: _Ctx) -> None:
    if "min" in spec and value < spec["min"]:
        ctx.add(E1004, path, f"{value} 小于下限 {spec['min']}")
    if "gt" in spec and not value > spec["gt"]:
        ctx.add(E1004, path, f"{value} 必须大于 {spec['gt']}")
    if "ge" in spec and not value >= spec["ge"]:
        ctx.add(E1004, path, f"{value} 必须大于等于 {spec['ge']}")
    if "le" in spec and not value <= spec["le"]:
        ctx.add(E1004, path, f"{value} 必须小于等于 {spec['le']}")


def _check_block(
    spec: Mapping[str, Any],
    value: Any,
    path: str,
    ctx: _Ctx,
    *,
    strict_required: bool,
) -> None:
    if not isinstance(value, Mapping):
        ctx.add(E1004, path, f"应为对象，实际是 {type(value).__name__}")
        return

    keys: Mapping[str, Any] = spec.get("keys", {})
    for name in value:
        if name not in keys:
            full = f"{path}.{name}" if path else name
            if _emit_forbidden(full, ctx):
                continue
            guess = _closest(name, list(keys) + list(_caliber_field_names()))
            if guess in _caliber_field_names():
                hint = f"最近似合法键：{guess}；它是口径字段，只能写在 {_caliber_owner(guess)}"
            elif guess:
                hint = f"最近似合法键：{guess}"
            else:
                hint = "该路径不存在于 schema"
            ctx.add(E1001, full, f"未知键 {name!r}", hint)

    for name, child in keys.items():
        child_path = f"{path}.{name}" if path else name
        if name not in value:
            if child.get("required") and strict_required:
                ctx.add(
                    E1008,
                    child_path,
                    "必填键缺失",
                    "没有默认值就是没有默认值；不要在实现里补兜底",
                )
            continue
        _check_node(child, value[name], child_path, ctx, strict_required=strict_required)


def _check_node(
    spec: Mapping[str, Any],
    value: Any,
    path: str,
    ctx: _Ctx,
    *,
    strict_required: bool,
) -> None:
    kind = spec["type"]
    if kind == "block":
        _check_block(spec, value, path, ctx, strict_required=strict_required)
    elif kind == "list":
        if not isinstance(value, list):
            ctx.add(E1004, path, f"应为列表，实际是 {type(value).__name__}")
            return
        item_spec = spec.get("item", {})
        for i, item in enumerate(value):
            _check_node(item_spec, item, f"{path}[{i}]", ctx, strict_required=strict_required)
    elif kind == "map":
        if not isinstance(value, Mapping):
            ctx.add(E1004, path, f"应为对象，实际是 {type(value).__name__}")
    else:
        _check_scalar(spec, value, path, ctx)


@lru_cache(maxsize=1)
def _forbidden_rules() -> tuple[tuple[str, str, str], ...]:
    out = []
    for rule in load_schema().get("forbidden_paths", []):
        out.append((rule["path"], rule["code"], rule.get("hint", "")))
    return tuple(out)


def _emit_forbidden(path: str, ctx: _Ctx) -> bool:
    """路径命中 schema 的 forbidden_paths 时，用schema 里的码与提示，返回是否命中。

    命中后不再走「未知键」的通用提示：`funding_period` 的问题不是"键名拼错"，
    而是"这件事不属于口径"，两者的修复动作不同。
    """
    for pattern, code, hint in _forbidden_rules():
        if pattern.startswith("*."):
            if path.endswith(pattern[1:]):
                ctx.add(code, path, f"{path} 不是合法位置", hint)
                return True
        elif pattern == path:
            ctx.add(code, path, f"{path} 不是合法位置", hint)
            return True
    return False


def _closest(name: str, candidates: Iterable[str]) -> str:
    """给拼错的键一个最近似提示。不做编辑距离库依赖，规则够用即可。"""
    name = name.lower()
    best, best_score = "", -1
    for cand in candidates:
        common = len(set(name) & set(cand.lower()))
        score = common - abs(len(name) - len(cand))
        if score > best_score:
            best, best_score = cand, score
    return best


def _collect_params(plan: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """所有「带 params 块」的工位，供口径泄漏检查（E1020）遍历。

    覆盖面必须是**全部**有 params 的工位。这里曾漏 `portfolio`，于是这一站写
    `cost_tiers` / `embargo` 这类口径字段时 E1020 看不见——口径合法地存在两份，
    且不报任何错。`search` 的参数挂在 `algorithms[*]` 下，单独展开。
    `allocate` 不是 plan 的顶层块（它是 Portfolio 的子能力，口径在
    `caliber.allocate`），因此不在此列。
    """
    out: list[tuple[str, Mapping[str, Any]]] = []
    for station in ("boundary", "panel", "target", "evidence", "verdict",
                    "portfolio", "certify"):
        node = plan.get(station)
        if isinstance(node, Mapping) and isinstance(node.get("params"), Mapping):
            out.append((f"{station}.params", node["params"]))
    search = plan.get("search")
    if isinstance(search, Mapping):
        for i, algo in enumerate(search.get("algorithms") or []):
            if isinstance(algo, Mapping) and isinstance(algo.get("params"), Mapping):
                out.append((f"search.algorithms[{i}].params", algo["params"]))
    return out


@lru_cache(maxsize=1)
def _caliber_field_names() -> frozenset[str]:
    schema = load_schema()
    names: set[str] = set()
    for sub in schema["root"]["caliber"]["keys"].values():
        names.update(sub.get("keys", {}))
    return frozenset(names)


def caliber_field_names() -> frozenset[str]:
    """`caliber` 四子块里出现过的全部键名，**唯一来源**。

    编译器曾手抄一份 24 个字段的清单用于 E1020（口径泄漏），与 schema 是两份
    真相：schema 加一个口径键、清单没跟，那个键就能同时出现在 `caliber` 和某个
    工位 `params` 里，且不报错。公开这个函数，让调用方派生而不是抄。
    """
    return _caliber_field_names()


def _check_caliber_leak(plan: Mapping[str, Any], ctx: _Ctx) -> None:
    names = _caliber_field_names()
    reported = {v.path for v in ctx.violations}
    for path, params in _collect_params(plan):
        for key in params:
            if key in names and f"{path}.{key}" not in reported:
                ctx.add(
                    E1020,
                    f"{path}.{key}",
                    f"工位参数里出现口径字段 {key!r}",
                    f"口径只写在 caliber 里（{_caliber_owner(key)}），工位自带一份就会有第二份",
                )


def _caliber_owner(key: str) -> str:
    schema = load_schema()
    for name, sub in schema["root"]["caliber"]["keys"].items():
        if key in sub.get("keys", {}):
            return f"caliber.{name}"
    return "caliber"


def _parse_interval(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    m = _INTERVAL_RE.match(value)
    return (m.group(1), m.group(2)) if m else None


def _check_cross_field(plan: Mapping[str, Any], ctx: _Ctx) -> None:
    cal = plan.get("caliber")
    if isinstance(cal, Mapping):
        net = cal.get("net") or {}
        label = cal.get("label") or {}
        alloc = cal.get("allocate") or {}
        exe = cal.get("execution") or {}

        im, mm = net.get("initial_margin"), net.get("maintenance_margin")
        if _is_number(im) and _is_number(mm) and not mm < im:
            ctx.add(E1004, "caliber.net.maintenance_margin", "维持保证金必须小于开仓保证金")

        tiers = net.get("cost_tiers")
        if isinstance(tiers, list) and all(_is_int(x) for x in tiers):
            if tiers != sorted(set(tiers)):
                ctx.add(E1004, "caliber.net.cost_tiers", "成本档必须严格升序且不重复")

        mw, mmem = alloc.get("max_weight"), alloc.get("min_members")
        if _is_number(mw) and _is_int(mmem) and mw * mmem > 1:
            ctx.add(
                E1004,
                "caliber.allocate",
                f"max_weight × min_members = {mw * mmem:.3f} > 1，约束不可行",
                "要么放宽单成员上限，要么减少最少成员数",
            )

        reb, hor = exe.get("rebalance"), label.get("horizon")
        if _is_int(reb) and _is_int(hor) and reb < hor and not label.get("overlap_policy"):
            ctx.add(
                E1008,
                "caliber.label.overlap_policy",
                f"rebalance({reb}) < horizon({hor}) 时标签重叠，必须声明处理方式",
            )

        neu = label.get("neutralise")
        if isinstance(neu, list) and neu and not _is_int(label.get("neutralise_window")):
            ctx.add(
                E1008,
                "caliber.label.neutralise_window",
                "neutralise 非空时必须给出回归窗口",
                "窗口未定义时，「逐时点回归」四个字没有确定含义",
            )

    port = plan.get("portfolio")
    if isinstance(port, Mapping) and isinstance(port.get("window"), Mapping):
        win = port["window"]
        fit, ev = _parse_interval(win.get("fit")), _parse_interval(win.get("eval"))
        emb = win.get("embargo")
        if fit and ev and _is_int(emb):
            if _within(fit[1], emb) > ev[0]:
                # 错误码与 `combine._check_window` 必须同一个：同一约束在编译期
                # 报 E1008、在运行期报 E1021，调用方按码分支就会漏掉一半。
                # 三段窗口语义归 E1021（18-plan-schema X6 / s0/03 C21）。
                ctx.add(
                    E1021,
                    "portfolio.window",
                    "fit 终点加 embargo 晚于 eval 起点",
                    "这等于用评估区数据拟合组合权重",
                )

    supply = plan.get("supply")
    if isinstance(supply, Mapping):
        market = supply.get("market") or "futures"
        datasets = supply.get("datasets")
        if market == "spot" and isinstance(datasets, list):
            # 现货没有资金费、持仓量，也没有「相对现货的溢价」——这三个数据集
            # 的定义域就是合约。允许它们出现在 spot 下，只会让采集器逐个标的
            # 失败再把失败记成 skipped，命令仍然退出 0，是标准的假成功。
            illegal = sorted({d for d in datasets if d != "ohlcv"})
            if illegal:
                ctx.add(
                    E1004,
                    "supply.datasets",
                    f"现货市场不支持数据集 {illegal}",
                    "funding / open_interest / basis 都定义在合约上；"
                    "现货研究只能声明 ohlcv，需要这三项就改用 market=futures",
                )

    boundary = plan.get("boundary")
    if isinstance(boundary, Mapping) and isinstance(boundary.get("time_axis"), Mapping):
        axis = boundary["time_axis"]
        mine, oos, hold = (
            _parse_interval(axis.get("mine")),
            _parse_interval(axis.get("oos")),
            _parse_interval(axis.get("holdout")),
        )
        if mine and oos and mine[1] >= oos[0]:
            ctx.add(E1008, "boundary.time_axis", "mine 必须整体早于 oos")
        if oos and hold and oos[0] <= hold[1] and hold[0] <= oos[1]:
            ctx.add(E1008, "boundary.time_axis", "oos 与 holdout 区间重叠")


def _within(end: str, offset: int) -> str:
    """把日期字符串按天推进，只用于比较先后，不追求日历精度。"""
    from datetime import date, timedelta

    y, m, d = (int(x) for x in end.split("-"))
    return (date(y, m, d) + timedelta(days=offset)).isoformat()


def validate_plan(plan: Any, *, strict_required: bool = True) -> list[Violation]:
    """校验一份已加载的计划。返回违规清单（空 = 通过）。

    `strict_required=False` 用于校验**文档里的片段**：片段可以只写几个键，
    但出现的键必须合法。
    """
    ctx = _Ctx()
    if not isinstance(plan, Mapping):
        ctx.add(E1004, "<root>", f"计划应为对象，实际是 {type(plan).__name__}")
        return ctx.violations

    schema = load_schema()
    _check_block({"keys": schema["root"]}, plan, "", ctx, strict_required=strict_required)
    _check_caliber_leak(plan, ctx)
    if strict_required:
        _check_cross_field(plan, ctx)
    return ctx.violations


def validate_cross_field(plan: Any) -> list[Violation]:
    """只跑跨字段检查（X1–X7 / C21 等）。

    `--no-strict` 只应放行"必填字段缺失"，不该放行"字段之间自相矛盾"：
    后者是数值错误的前置条件，不是草稿阶段的正常状态。
    """
    ctx = _Ctx()
    if isinstance(plan, Mapping):
        _check_cross_field(plan, ctx)
    return ctx.violations


def assert_valid_plan(plan: Any) -> None:
    violations = validate_plan(plan)
    if not violations:
        return
    first = violations[0]
    raise FlowError(
        first.code,
        first.message,
        hint=first.hint or None,
        path=first.path,
        context={"violations": [v.as_dict() for v in violations]},
    )
