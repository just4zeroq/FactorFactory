"""编译器十步（s0/03-runtime.md §5.1）。

纯函数 compile(baseline, plan, overrides, aliases) -> CompiledPlan。
不读数据、不写文件、不取时钟（compiled_at 由调用方注入）。

步骤 1–4 复用 contracts.plan_schema；步骤 5–10 在 S1 接真公式：
深合并 + 来源表 + 四锚点指纹。退出码分界：1–4 为 E1xxx（码 2），
5+ 才有 E2xxx（码 3）与 E4xxx（码 4）。

四指纹公式（s0/01 §5）：
  plan_digest       = H(plan_raw)                    用户改了计划
  domain_digest     = H(members, fit/embargo/eval)   组合域或窗口变了
  caliber_digest    = H(caliber 四子块)               口径块本身
  policy_digest     = H(baseline_version, constitutive_subset,
                        caliber_digest, domain_digest)  口径变了 → 全链失效
  blueprint_digest  = H(baseline_version, values, caliber,
                        supply_pins, domain_digest)     展开后的方案变了

关键边界：policy_digest 不含 supply_pins（数据刷新是事实演进，不新建试验族）；
blueprint_digest 含。operations（workers/cache/log_level）不进任何指纹。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from contracts import digest as dg
from contracts.errors import (
    E1001, E1002, E1003, E1004, E1005, E1006, E1007, E1008, E1011,
    E1020, E2002, E2003, E4001, E4002, E4004, FlowError,
)
from contracts.plan_schema import (
    Violation, caliber_field_names, validate_cross_field, validate_plan,
)
from contracts.types import leaf_paths

from .gate_b import as_pin, check_gate_b

# 工位块的顶层键（八个工位）
STATIONS = (
    "boundary", "panel", "target", "search", "evidence",
    "verdict", "portfolio", "certify",
)
# caliber 四子块的字段名——工位 params 里出现这些即 C20 / E1020。
# **从 schema 派生，不手抄**：曾经这里是 24 个字面量，schema 加口径键时没人
# 同步，那个键就能合法地同时出现在两处（口径有两份且不报错）。
CALIBER_FIELDS = caliber_field_names()
# 构成性参数：出现在 overrides 里且未放行 → E1005
CONSTITUTIVE_TOPS = frozenset({
    "supply", "caliber", "boundary", "panel", "target", "search",
    "evidence", "verdict", "portfolio", "certify",
})
# 不进任何指纹的操作性字段
OPERATION_KEYS = frozenset({"operations", "compiled_at"})
# 基线文件的元字段：只用于定位版本，不是方案内容。
# 若原样并入 merged，`baseline_version` 会成为 plan 的非法顶层键（E1001）。
BASELINE_META_KEYS = frozenset({
    "baseline_version", "released_at", "published_at", "schema_version",
    "inherits", "extends", "preset",
})
# 参与来源表与指纹的顶层块（顺序固定，保证规范化后逐位一致）
FINGERPRINT_TOPS = (
    "plan", "baseline", "supply", "caliber", "boundary", "panel",
    "target", "search", "evidence", "verdict", "portfolio", "certify",
)


@dataclass
class CompiledPlan:
    """冻结后的方案：展开值 + 逐叶子来源表 + 四锚点指纹。"""

    plan: dict[str, Any]
    origins: dict[str, str]
    fingerprints: dict[str, str] = field(default_factory=dict)
    compiled_at: datetime | None = None

    def explain(self) -> str:
        lines = []
        for path, (value, origin) in sorted(self.origins_items()):
            lines.append(f"{path:45s} {str(value)[:30]:30s} {origin}")
        return "\n".join(lines)

    def origins_items(self):
        for path, origin in self.origins.items():
            yield path, (_get_path(self.plan, path), origin)


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _first_violation_error(violations: list[Violation]) -> None:
    if violations:
        v = violations[0]
        raise FlowError(v.code, v.message, path=v.path, hint=v.hint or None)


def compile_plan(
    *,
    baseline: Mapping[str, Any] | None,
    plan: Mapping[str, Any],
    overrides: Mapping[str, Any] | None = None,
    aliases: Mapping[str, Any] | None = None,
    symbol_sets: Mapping[str, Any] | None = None,
    supply_meta: Mapping[str, Any] | None = None,
    require_supply: bool = False,
    strict: bool = True,
    allow_constitutive_override: bool = False,
    compiled_at: datetime | None = None,
) -> CompiledPlan:
    """十步编译。任何一步失败都抛 FlowError，带正确错误码。"""
    # plan_digest = H(用户写下的计划原文)。必须最先深拷贝：
    # ① `_deep_merge_with_origins`/`_apply_override` 会就地改嵌套对象，
    #    浅拷贝会让 --set 的覆盖值污染"原文"指纹；
    # ② compile 是纯函数，不能就地改调用方传进来的 plan。
    plan_raw = deepcopy(dict(plan))
    baseline = deepcopy(baseline or {})

    declared_baseline = plan.get("baseline")
    if declared_baseline is not None and not isinstance(declared_baseline, str):
        raise FlowError(
            E1004, "plan.baseline 必须是版本字符串（如 v2）",
            path="baseline", hint="基线版本是标识符，不是配置块",
        )
    # 基线版本：载入的基线文件为准；plan 的 baseline 只是声明（无基线文件时采用）。
    bv = str(baseline.get("baseline_version") or declared_baseline or "v1")

    # 步骤 1–2：载入基线 + 按路径叠加计划（深合并，C1/C4）。
    # 基线元字段（baseline_version/released_at/released_at…）不是方案内容；
    # 原样并入会让 `baseline_version` 成为 plan 的非法顶层键（E1001）。
    baseline_values = {k: v for k, v in baseline.items() if k not in BASELINE_META_KEYS}
    merged, origins = _deep_merge_with_origins(
        baseline_values, deepcopy(plan), baseline_version=bv,
    )

    # 步骤 1-4：结构层校验（C1-C4 / C20 部分）
    violations = validate_plan(merged, strict_required=strict)
    _first_violation_error(violations)
    if not strict:
        # `--no-strict` 只放行"必填字段缺失"，不放行"字段之间自相矛盾"：
        # 后者（X1–X7 / C21）是数值错误的前置条件，不是草稿的正常状态。
        _first_violation_error(validate_cross_field(merged))

    # 步骤 3：overrides 叠加；构成性参数须显式放行（C5 / E1005）
    if overrides:
        for key, _value in overrides.items():
            top = key.split(".", 1)[0]
            if top in CONSTITUTIVE_TOPS and not allow_constitutive_override:
                raise FlowError(
                    E1005,
                    f"构成性参数 '{top}' 出现在覆盖层且未放行",
                    path=key,
                    hint="改构成性参数须新建计划并声明试验族",
                )
        for key, value in overrides.items():
            _apply_override(merged, key, deepcopy(value), origins)
        # 覆盖层同样是"用户写下的东西"：必须重新过一遍结构校验，
        # 否则 `--set foo=1` / `--set caliber=oops` 会绕过 C1/C4 进指纹。
        _first_violation_error(validate_plan(merged, strict_required=strict))
        if not strict:
            # 与步骤 1-4 同样：--set 注入的矛盾必须照样拦。
            _first_violation_error(validate_cross_field(merged))

    # 步骤 4：工位 params 不得自带 caliber 字段（C20 / E1020）
    for station in STATIONS:
        block = merged.get(station)
        if not isinstance(block, Mapping):
            continue
        params = block.get("params", {})
        if isinstance(params, Mapping):
            for field_name in params:
                if field_name in CALIBER_FIELDS:
                    raise FlowError(
                        E1020,
                        f"工位参数里出现口径字段 '{field_name}'",
                        path=f"{station}.params.{field_name}",
                        hint="口径只写在 caliber，工位自带一份就会有第二份",
                    )

    # 步骤 5：解析实现名（C2 / E1002，C8 / E1011）
    for station in STATIONS:
        block = merged.get(station)
        if not isinstance(block, Mapping):
            continue
        impl = block.get("impl")
        if impl is None and station in {"target", "evidence"}:
            continue
        if impl is not None:
            if not isinstance(impl, str) or not impl:
                raise FlowError(E1002, f"{station}.impl 必须是非空字符串", path=f"{station}.impl")
            if impl.startswith(("dummy_", "demo_", "placeholder")):
                raise FlowError(
                    E1011,
                    f"默认实现指向占位 '{impl}'",
                    path=f"{station}.impl",
                    hint="占位实现不得作为默认",
                )

    # 步骤 6/7：param_schema 逐项校验、composes 子调用图——
    # 依赖能力层注册表（② 实现），S1 编译期只留 C3/E1003、E2008 的接入口。
    # 注册表就绪后由 registry.validate_calls() 填充；此处不放宽为「忽略继续」。

    # 步骤 9：供给别名解析 + 门禁 B（只读；C11 / E4001，C12 / E4002/E4003/E4005）
    # aliases=None 是 S1 的「尚未接供给」形态，保持原样；一旦给了别名表，
    # 每一项都必须是完整 Pin 且过门禁 B——不能用字符串桩冒充已解析版本。
    supply = merged.get("supply")
    supply_pins: list[Any] = []
    if require_supply and (
        not aliases or not isinstance(supply, Mapping)
        or not list(supply.get("datasets") or [])
    ):
        # 应用层（run/explain）必须传 require_supply=True：缺别名表、空别名表、
        # 空 datasets 都是「没有可检查的供给」，不是「跳过门禁 B」。
        # 默认 False 只服务 S1 纯编译单测。
        raise FlowError(
            E4001, "计划未声明 supply.datasets，无法定位任何供给",
            path="supply.datasets",
            hint="先 flow fetch 采集数据；编译只读已落盘事实，不补采",
        )
    if aliases is not None and isinstance(supply, Mapping):
        streams = list(supply.get("datasets", []) or [])
        for stream in streams:
            if stream not in aliases:
                raise FlowError(
                    E4001,
                    f"别名指向不存在的版本: {stream}",
                    path=f"supply.datasets.{stream}",
                )
        # 集合语义：datasets 的书写顺序不应改变蓝图指纹（与 supply_digest 一致）。
        for stream in sorted(streams):
            raw = aliases[stream]
            pin = as_pin(raw)
            if pin is None:
                raise FlowError(
                    E4002,
                    f"别名 {stream} 不是完整 Pin，无法解析为供给版本",
                    path=f"supply.datasets.{stream}",
                    hint="由 supply publish 写入别名表；字符串桩只用于 S1 指纹单测",
                )
            if pin.dataset != stream:
                # 「ohlcv 指向 funding 的版本」会让四流共用同一 Pin，指纹看起来
                # 正常而实际数据全错（review P2-2）。
                raise FlowError(
                    E4001,
                    f"别名 {stream} 指向 {pin.dataset} 的版本",
                    path=f"supply.datasets.{stream}",
                    hint="别名键必须等于 Pin.dataset；重新发布该数据集",
                )
            meta = (supply_meta or {}).get(stream)
            if not isinstance(meta, Mapping):
                # 别名存在但版本对象读不到：这正是 E4001「指向不存在的版本」。
                # 只读别名表会让「seed 被删」的研究继续编译，错误推迟到执行期。
                raise FlowError(
                    E4001,
                    f"别名 {stream} 指向的版本没有可读 manifest",
                    path=f"supply.datasets.{stream}",
                    hint="重新 refresh 发布该版本；编译不补采也不猜",
                )
            # manifest 必须就是这一流、这一版本的事实：否则门禁 B 会拿别人的
            # timeframe/exchange 去比对，结论看起来正常而依据是错的（P2-2）。
            if meta.get("dataset") != stream or str(meta.get("version")) != pin.version:
                raise FlowError(
                    E4004,
                    f"{stream} 的 manifest 与别名指向的版本不一致: "
                    f"manifest={meta.get('dataset')!r}/{meta.get('version')!r}, "
                    f"pin={stream!r}/{pin.version!r}",
                    path=f"supply.datasets.{stream}",
                    hint="供给事实损坏；重新发布该版本",
                )
            check_gate_b(pin, merged, symbol_sets=symbol_sets, supply_meta=meta)
            # 用 Pin.digest_view 的规范化投影进指纹：直接透传 JSON Mapping
            # 会让 "Z" 与 "+00:00" 两种等价时间写法产生不同 blueprint_digest（D1）。
            supply_pins.append(pin)

    # 步骤 10：四锚点真公式（先 domain → caliber → policy → blueprint）
    caliber = merged.get("caliber") or {}
    if not isinstance(caliber, Mapping):
        raise FlowError(E1004, "caliber 必须是映射", path="caliber")
    missing_caliber = sorted({"execution", "label", "allocate", "net"} - set(caliber))
    if missing_caliber:
        if strict:
            raise FlowError(
                E1008, f"caliber 缺子块 {missing_caliber}",
                path="caliber",
                hint="caliber 是唯一口径源，四个子块缺一不可",
            )
        caliber_input = {k: caliber.get(k) for k in ("execution", "label", "allocate", "net")}
    else:
        caliber_input = {
            "execution": caliber["execution"], "label": caliber["label"],
            "allocate": caliber["allocate"], "net": caliber["net"],
        }
    domain = dg.domain_digest(
        members=_domain_members(merged),
        fit_window=_window(merged).get("fit"),
        embargo=_window(merged).get("embargo"),
        eval_window=_window(merged).get("eval"),
    )
    caliber_digest = dg.caliber_digest(**caliber_input)
    policy_digest = dg.policy_digest(
        baseline_version=bv,
        constitutive_subset=_constitutive_subset(merged),
        caliber=caliber_digest,
        domain=domain,
    )
    values = {k: merged[k] for k in FINGERPRINT_TOPS if k in merged}
    blueprint_digest = dg.blueprint_digest(
        baseline_version=bv, values=values, caliber=caliber,
        supply_pins=supply_pins, domain=domain,
    )
    fingerprints = {
        "plan_digest": dg.plan_digest(plan_raw),
        "blueprint_digest": blueprint_digest,
        "policy_digest": policy_digest,
        "caliber_digest": caliber_digest,
        "domain_digest": domain,
    }

    # 来源表必须与 merged 的叶子一一对应：多出来的条目会让 explain 显示 None，
    # 缺条目则出现"这个值从哪来"的盲区（E1007）。
    leaves = leaf_paths(merged)
    origins = {k: v for k, v in origins.items() if k in leaves}
    missing_origins = sorted(leaves - set(origins))
    if missing_origins:
        raise FlowError(
            E1007,
            f"origins 未覆盖 {len(missing_origins)} 个叶子",
            hint="每个叶子都必须标出来源 baseline@vN / plan / override",
            context={"missing": missing_origins[:20], "missing_count": len(missing_origins)},
        )
    return CompiledPlan(plan=merged, origins=origins, fingerprints=fingerprints,
                        compiled_at=compiled_at)


def _domain_members(merged: Mapping[str, Any]) -> list[dict[str, Any]]:
    """组合域成员清单。样例里成员由 `supply.symbols` 给出（S2 起解析为真实成员集）。

    每个成员必须带 `id / version / available_from` 三个字段（contracts 会校验）；
    S1 只能拿到标的集名，故 version/available_from 为 None——这是**显式缺失**，
    由 S2 的供给链填真值，不是"默认值兜底"。
    """
    members: list[dict[str, Any]] = []
    supply = merged.get("supply") or {}
    symbols = supply.get("symbols") if isinstance(supply, Mapping) else None
    if isinstance(symbols, (list, tuple)):
        for m in symbols:
            if isinstance(m, Mapping):
                members.append({
                    "id": m.get("id"), "version": m.get("version"),
                    "available_from": m.get("available_from"),
                })
            else:
                members.append({"id": str(m), "version": None, "available_from": None})
    elif isinstance(symbols, str):
        members.append({"id": symbols, "version": None, "available_from": None})
    return members


def _window(merged: Mapping[str, Any]) -> dict[str, Any]:
    """组合三段窗口。portfolio 存在时 window 必须存在（C21 的第一道闸）。"""
    portfolio = merged.get("portfolio")
    if not isinstance(portfolio, Mapping):
        return {}
    window = portfolio.get("window")
    if not isinstance(window, Mapping):
        raise FlowError(
            E1008, "portfolio.window 缺失：fit/embargo/eval 三段窗口是口径的一部分",
            path="portfolio.window",
            hint="缺窗口时 domain_digest 会退化为空映射，等于没有域约束",
        )
    return dict(window)


def _constitutive_subset(merged: Mapping[str, Any]) -> dict[str, Any]:
    """进 policy 的构成性子集。

    s0/01 §2.3：**实现名与版本、种子都不进 policy**（它们进 blueprint/parcel）。
    整块塞进去会让"换个实现名"或"改个种子"被当成口径变化，全部产物无谓失效，
    并且新建一个试验族。
    """
    out: dict[str, Any] = {}
    for key in CONSTITUTIVE_TOPS:
        if key not in merged or key in ("caliber", "supply"):
            continue
        block = merged[key]
        if isinstance(block, Mapping):
            block = {k: v for k, v in block.items()
                     if k not in ("impl", "impl_version", "seed")}
        out[key] = block
    return out


def _apply_override(
    merged: dict[str, Any], path: str, value: Any, origins: dict[str, str],
) -> None:
    """`--set a.b.c=v` 必须按路径写入，而不是造一个字面量键 `"a.b.c"`。

    覆盖后清掉该路径下所有旧来源并重标 `override`——否则同一份值会同时标成
    `plan` 与 `override`，`explain` 自相矛盾。
    """
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise FlowError(E1001, "覆盖路径为空", path=path)
    cur: Any = merged
    for i, part in enumerate(parts[:-1]):
        nxt = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(nxt, dict):
            raise FlowError(
                E1001, f"覆盖路径 {path!r} 的中间节点 {part!r} 不是映射",
                path=".".join(parts[: i + 1]),
                hint="只能覆盖已存在的块，或用完整路径新建叶子",
            )
        cur = nxt
    cur[parts[-1]] = value
    full = ".".join(parts)
    prefix = full + "."
    for key in [k for k in origins if k == full or k.startswith(prefix)]:
        origins.pop(key, None)
    _mark_origin(origins, full, value, "override")


def _deep_merge_with_origins(
    baseline: Mapping[str, Any], plan: Mapping[str, Any], *, baseline_version: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """深合并两层，并为每个叶子记录来源。

    来源取值：``baseline@vN`` / ``plan``（overrides 后续单独覆盖）。
    合并规则：Mapping 递归；其余（含 list）整体替换——list 不做逐项合并，
    避免搜索算法数组被悄悄拼成两份。
    """
    origins: dict[str, str] = {}

    def walk(base: Any, over: Any, prefix: str) -> Any:
        if isinstance(base, Mapping) and isinstance(over, Mapping):
            out: dict[str, Any] = {}
            for k in base:
                path = f"{prefix}.{k}" if prefix else str(k)
                if k in over:
                    out[k] = walk(base[k], over[k], path)
                else:
                    out[k] = base[k]
                    _mark_origin(origins, path, out[k], f"baseline@{baseline_version}")
            for k in over:
                if k not in base:
                    path = f"{prefix}.{k}" if prefix else str(k)
                    out[k] = over[k]
                    _mark_origin(origins, path, out[k], "plan")
            return out
        # 显式 null 是一个值，不是"未提供"：回落基线会让 `horizon: null`
        # 静默变成 5，正是 §3.1 约束 1 要禁止的那种"补默认"。
        _mark_origin(origins, prefix, over, "plan")
        return over

    # baseline_version 只用于来源标记与指纹公式，绝不注入 plan——
    # plan_schema 的合法键是 plan 里的 'baseline'，注入 'baseline_version'
    # 会被 E1001 拒绝（它不在 schema 的顶层键里）。
    return walk(baseline, plan, ""), origins


def _mark_origin(origins: dict[str, str], path: str, value: Any, source: str) -> None:
    """叶子记来源；容器只递归，不记（explain 按叶子展示）。"""
    if isinstance(value, Mapping) and value:
        for k, v in value.items():
            _mark_origin(origins, f"{path}.{k}" if path else str(k), v, source)
    else:
        origins[path] = source
