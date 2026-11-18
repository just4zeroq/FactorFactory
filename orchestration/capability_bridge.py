"""工位 ↔ 能力桥接（S3-M6）：executor 唯一调用能力的适配层。

职责边界（s0/05 §2.1 第 5/8 步、s0/04 §1.3）：

* **编排层有权**：本模块由 executor 调用，负责构造 ``Call`` 六通道、解析上游
  ``InputRef.data``、执行 ``run(call)``、把能力返回拆成「内存对象（喂下游）」
  与「落盘视图（写 Parcel）」两部分。
* **能力层无权**：能力只返回，不读盘、不写账本、不产结论。本模块不替能力做
  任何判断，只做机械的数据搬运与契约校验。

三个不可省的动作：

1. **落盘投影**：能力返回的 ``opaque`` / ``array`` 字段（Panel/Scope/ndarray）
   只在内存传递，不进 ``Parcel.payload``（后者要 ``json.dumps`` +
   ``compute_digest``，会拒绝这些类型，见 contracts/digest.py:24/210）。
   落盘视图只保留标量 / ref / map / list 字段。
2. **内存对象表**：上游能力返回的 data 通道对象按 ``kind`` 存入对象表，下游
   按输入声明的 ``kind`` 取用，放进 ``InputRef.data``。data 不进指纹
   （runtime.py:49 compare=False），指纹链仍由 digest 驱动。
3. **fail-closed**：未实现能力（allocate/backtest/combine/certify，S4/S7）由
   ``run(call)`` 抛 E3004；缺上游对象/类型不符抛 E3003 —— 绝不返回空产物。

工位到能力的映射是**数据表**。Panel 工位在 read 已把供给转成 FieldStream 后
由 align 产出 panel；未实现能力不降级、不跳过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import numpy as np

from capability import Call, InputRef, run as capability_run
from capability.impls import install_default_impls
from capability.panel import array_digest
from capability.params import schema_for
from capability.runtime import impls as installed_impls
from capability.spec import describe as describe_capability
from contracts.digest import compute_digest
from contracts.errors import E1004, E3002, E3003, E3004, FlowError
from storage.objects import ObjectStore
from capability.timeaxis import CALENDAR_DAYS_PER_YEAR
from capability.spec import method_id

# 工位 → 主能力（数据表；能力名也是实现名，见 impls.py）。
STATION_CAPABILITY: dict[str, str] = {
    "Boundary": "boundary",
    "Panel": "align",       # read→align 的 field_stream 由 supply 预置
    "Target": "label",
    "Search": "search",
    "Evidence": "observe",
    "Verdict": "verdict",
    # S4 诊断链：combine 只做等权合成，allocate/backtest 仍由桥在同一工位
    # 内接续调用（S7 的 ridge/相关约束未实现，CLI 显式拒绝）。
    "Portfolio": "combine",
    "Certify": "certify",    # S7 未实现 → run() E3004
}

# 上游能力产物里的对象通道 key → 下游输入 kind 的默认别名。
# 大多数 kind 同名直取；少数能力间的叫法不同，在这里显式列出（数据表）。
# 注意：这里只做「按 kind 取同名字段」；跨工位语义映射（如 label 工位产出的
# score 要作为 search 的 label 输入）由 _KIND_ALIAS 表达。
_KIND_ALIAS: dict[str, tuple[str, ...]] = {
    "label": ("label",),                  # Target 派生的 label 通道（见下）
    "candidate_set": ("candidate_set",),  # Search 派生的 candidate_set 通道
    "observation_set": ("observation_set",),  # Evidence 派生的 observation_set 通道
    "market_view": ("market_view",),
}

# 这些 kind 不来自任何上游 Parcel，必须由外部 supply_fn / shared 注入。
_EXTERNAL_KINDS = frozenset({"pin", "field_stream", "supply", "market_view"})

# outputs 的类型标记里，这两类只在内存传，不落盘。
_MEMORY_MARKERS = frozenset({"opaque", "array"})


def _output_markers(capability: str) -> dict[str, str]:
    """取能力 outputs 的类型标记表（opaque/array 会被投影掉）。"""
    spec = schema_for(capability).get("outputs", {})
    if not isinstance(spec, Mapping):
        return {}
    return {str(k): str(v) for k, v in spec.items()}


def _input_decls(capability: str) -> list[Mapping[str, Any]]:
    """输入声明（含 required 标记）。可选输入缺失时必须落 `None` 占位。"""
    return list(schema_for(capability).get("inputs") or [])


def _first_object(upstream: Mapping[str, Mapping[str, Any]], key: str) -> Any:
    for produced in upstream.values():
        if key in produced and produced[key] is not None:
            return produced[key]
    return None


def _align_to_panel(
    value: Any,
    *,
    shape: tuple[int, ...],
    upstream: Mapping[str, Mapping[str, Any]],
    station: str,
    name: str,
) -> np.ndarray:
    """把 Boundary 的全量逐格数组裁到 Panel/分数的时间轴。

    Boundary 覆盖整段供给，Panel 会按对齐规则裁掉不可用区间，行数天然不同。
    组合链和单因子成本链都必须取同一批 bar，不能拿全量掩码去裁已裁过的分数。
    """
    arr = np.asarray(value)
    if arr.shape == tuple(shape):
        return arr
    scope = _first_object(upstream, "scope")
    panel = _first_object(upstream, "panel")
    src_times = getattr(scope, "times", None)
    panel_times = getattr(panel, "times", None)
    if (src_times is None or panel_times is None
            or tuple(getattr(panel, "symbols", ()) or ())
            != tuple(getattr(scope, "symbols", ()) or ())):
        raise FlowError(
            E3003, f"{station} 无法把 {name} 对齐到面板时间轴",
            path=f"station.{station}.inputs.{name}",
        )
    source = np.asarray(src_times, dtype="int64")
    target = np.asarray(panel_times, dtype="int64")
    idx = np.searchsorted(source, target)
    if idx.size == 0 or int(idx.max()) >= source.size \
            or not np.array_equal(source[idx], target):
        raise FlowError(
            E3003, f"{station} 的 {name} 时间轴不是面板时间轴的子集",
            path=f"station.{station}.inputs.{name}",
        )
    return arr[idx]


def _net_series(backtest_out: Mapping[str, Any]) -> np.ndarray:
    """取 `backtest` 的逐期净收益（内存通道）。

    `net_return_ref` 是身份，不是数值：拿它算不出 Sharpe。缺数组说明实现把
    「落盘引用」当成了「产物本身」，这时 fail-closed 而不是退回按成本估算。
    """
    value = backtest_out.get("net_return")
    if not isinstance(value, np.ndarray) or value.ndim != 1:
        raise FlowError(
            E3003, "backtest 未返回逐期净收益数组（certify 无法复算净表现）",
            path="backtest.outputs.net_return",
        )
    return np.asarray(value, dtype="float64")


def _as_series(value: Any, size: int) -> np.ndarray:
    arr = np.asarray(value, dtype="float64") if value is not None else np.zeros(size)
    if arr.ndim != 1 or arr.size != size:
        raise FlowError(E3003, f"backtest.turnover 形状 {arr.shape} 与 {size} 期不符",
                        path="backtest.outputs.turnover")
    return arr


def _compound(periods: np.ndarray) -> float:
    """逐期收益 → 累计净值（乘法，不是求和：求和会把复利当成单利）。"""
    if periods.size == 0:
        return 0.0
    return float(np.prod(1.0 + periods) - 1.0)


def _sharpe(periods: np.ndarray, bars_per_year: float) -> float:
    """`mean/std × sqrt(bars_per_year)`；样本不足或零波动返回 0（不返回 NaN）。"""
    if periods.size < 2:
        return 0.0
    std = float(np.std(periods))
    if not np.isfinite(std) or std <= 1e-12:
        return 0.0
    mean = float(np.mean(periods))
    if not np.isfinite(mean):
        return 0.0
    return mean / std * float(np.sqrt(bars_per_year))


def _max_drawdown(net: np.ndarray) -> float:
    """最大回撤（负数或 0）。净值曲线由同一批逐期净收益累乘得到。"""
    if net.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + net)
    peak = np.maximum.accumulate(equity)
    # peak 为 0 只可能出现在「净收益 -100%」之后，此时回撤定义为 -1。
    with np.errstate(invalid="ignore", divide="ignore"):
        drawdown = np.where(peak > 0, equity / np.where(peak > 0, peak, 1.0) - 1.0, -1.0)
    return float(np.min(drawdown))


def _panel_times(panel: Any, *, station: str) -> np.ndarray:
    """Panel 时间轴 → `backtest` 的 `time` 输入（UTC 纳秒，只读副本）。

    时间轴是事实，不是配置：桥只做搬运，不排序也不补点。缺时间轴说明 Panel
    不是能力层构造的对象（可能是测试桩），这时 fail-closed——让 backtest 静默
    退回日频假设，等于把「不知道频率」伪装成「日频」。
    """
    times = getattr(panel, "times", None)
    if times is None:
        raise FlowError(
            E3003, "backtest 的 S4 链缺 Panel 时间轴",
            path=f"station.{station}.inputs.time",
        )
    return np.asarray(times, dtype="int64")


def _portfolio_shared(
    shared: Mapping[str, Any], upstream: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """把非直接上游的候选/裁决对象放进 combine 的可用输入池。

    Portfolio 在 StationSpec 里只依赖 Verdict/Panel/Boundary；但 combine 的
    等权合成还要读 Search 的候选暴露。executor 把全部已产出工位放进
    ``shared["prior_objects"]``，这里只做**按 kind 的机械搬运**，不替能力
    判断成员、不读盘、不产结论。
    """
    out = dict(shared)
    prior = shared.get("prior_objects") or {}
    cand = (prior.get("Search") or {}).get("candidate_set")
    if cand is not None:
        out.setdefault("candidate_set", cand)
    label = (prior.get("Target") or {}).get("label")
    if label is not None:
        out.setdefault("label", label)
    verdict = _first_object(upstream, "verdict_set")
    if verdict is None:
        verdict = (prior.get("Verdict") or {}).get("verdict_set")
    if verdict is not None:
        out.setdefault("verdict_set", verdict)
    return out


def _eligible_members(shared: Mapping[str, Any]) -> int:
    """数一遍「裁决为 pass/watch 且有暴露」的候选数，只数不改。

    与 `combine._selected` 同一判据（裁决集合 ∩ 候选集合 ∩ 暴露表），但这里
    只做**是否为空**的判定：真正的成员筛选仍归 combine，编排层不替它挑人。
    """
    cand = shared.get("candidate_set")
    verdict = shared.get("verdict_set")
    if not isinstance(cand, Mapping) or not isinstance(verdict, Mapping):
        return 0
    exposures = cand.get("exposures")
    if not isinstance(exposures, Mapping):
        return 0
    rows = verdict.get("per_candidate")
    if not isinstance(rows, list):
        return 0
    passed = {
        str(row.get("candidate_id"))
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("decision", "")) in {"pass", "watch"}
    }
    members = {
        str(c.get("candidate_id"))
        for c in (cand.get("candidates") or [])
        if isinstance(c, Mapping)
    }
    return len((passed & members) & set(exposures))


@dataclass
class BridgeResult:
    """一个工位能力调用的结果，拆成落盘视图与内存对象两部分。"""

    payload: dict[str, Any]
    objects: dict[str, Any] = field(default_factory=dict)
    capability: str = ""
    # 「结果性跳过」的唯一信号（s0/05 §4.2）：null 表示正常产出；非空时 payload
    # 为空、不落 parcel，执行器把工位记 skipped(reason) 并让下游随 upstream_skipped
    # 跳过。"无候选通过裁决"是结果而不是故障（AC52），不能写成 E3003。
    skip_reason: str | None = None


class CapabilityBridge:
    """executor 内唯一的能力调用点。无状态；对象表由 executor 持有。

    ``supply_fn(station, params) -> {kind: object}``：外部事实注入点。Boundary
    需要 ``pin`` + ``field_stream``，Panel 需要 ``field_stream``。没有注入点而
    这些 kind 缺失时抛 E3003 —— 能力层不自己读盘。
    """

    def __init__(
        self,
        supply_fn: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        object_store: ObjectStore | None = None,
        policy_digest: str | None = None,
    ) -> None:
        install_default_impls()
        self.supply_fn = supply_fn
        self.object_store = object_store
        # 编译期冻结的 policy_digest。能力层拿不到（缺 baseline_version /
        # constitutive_subset 两个通道），而证书清单必须能与 ticket 对账，
        # 所以真值由编排层持有、在认证尾链回填。
        self.policy_digest = policy_digest

    def _resolve_input(
        self,
        kind: str,
        *,
        station: str,
        upstream: Mapping[str, Mapping[str, Any]],
        shared: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> Any:
        """按 kind 取输入对象：优先级 shared > 上游对象 > 派生 > supply 注入。"""
        if kind in shared:
            return shared[kind]
        aliases = _KIND_ALIAS.get(kind, (kind,))
        for produced in upstream.values():
            for key in aliases:
                if key in produced:
                    return produced[key]
        if kind == "price":
            # 价格不是某个工位单独产出的对象，它是 Panel 的 `close` 字段。
            # `allocate` 的 inv_vol 口径需要它；没有这条派生就只能让调用方
            # 手工塞 shared，多一个可以塞错的口子。
            panel = _first_object(upstream, "panel")
            field = getattr(panel, "field", None)
            if callable(field) and "close" in getattr(panel, "fields", {}):
                return field("close")
        if self.supply_fn is not None:
            injected = self.supply_fn(station, params)
            if kind in injected:
                return injected[kind]
        hint = ("这是外部事实 kind，需 supply_fn 注入" if kind in _EXTERNAL_KINDS
                else "上游能力未产出该对象")
        raise FlowError(
            E3003, f"{station} 缺输入对象 {kind!r}（{hint}）",
            path=f"station.{station}.inputs.{kind}",
            hint="供给/上游对象必须由执行器注入；能力层不自己读盘，也不接受空值降级",
        )

    def _resolve_impl(self, capability: str, impl: str, version: str,
                      *, declared: bool = False) -> tuple[str, str]:
        """工位声明的实现名 → 能力注册表里的真实实现名 + 版本。

        `declared=True` 表示 `impl` 来自计划的显式声明（不是规格表默认值），
        此时必须真在注册表里 —— 否则「换实现」这件事是假的：

        - 单实现时旧逻辑直接返回唯一实现，**忽略声明名**。计划写
          `impl: pit_dynamic_topk` 与写 `impl: typo_here` 跑出完全相同的产物，
          而 `call_digest` 记的是注册名，于是两个不同的计划指纹相同、缓存互通。
        - `impl` 为空或等于能力名本身（`boundary`）不算错：前者是未声明，
          后者是「用这个能力的默认实现」的写法。
        """
        bucket = installed_impls().get(capability) or {}
        if not bucket:
            # 未实现能力：返回一个不会命中任何注册项的名字，让 run() 抛 E3004。
            # 不在这里提前抛，是为了让拒绝路径统一走 runtime.run 的契约检查。
            return impl or capability, version
        if len(bucket) == 1:
            only = next(iter(bucket.values()))
            if declared and impl and impl not in (only.name, capability):
                raise FlowError(
                    E1002,
                    f"{capability} 只登记了实现 {only.name!r}，计划声明 {impl!r}",
                    path=f"station.{capability}.impl",
                    hint=f"改成 {only.name!r} 或删掉 impl 用默认",
                )
            return only.name, only.version
        # 多实现：plan 声明名必须命中；不存在就是写错了，不再往下传一个
        # 注定在 resolve 阶段变成「版本不符」的名字（错误码会误导排查方向）。
        if impl in bucket:
            return impl, version
        if declared and impl and impl != capability:
            raise FlowError(
                E1002,
                f"{capability} 没有登记实现 {impl!r}",
                path=f"station.{capability}.impl",
                hint=f"已登记: {sorted(bucket)}",
            )
        return impl or capability, version

    def _write_ref(self, capability: str, field: str, value: Any) -> str:
        """把数据通道对象写进对象存储，返回可落盘的内容寻址 digest 字符串。

        三种对象各有规范落法（s0/01 数据通道 + 09 存储）：
        - ndarray → parquet 表（一列 value）；
        - 值对象（Panel/Scope/FieldStream，带 digest()）→ 其 digest 即身份，
          同时写一份 json 摘要供独立重建；
        - list/dict（候选集、逐候选结果）→ json 对象。
        无 object_store 时不应调用本方法（由 invoke 守卫）。
        """
        store = self.object_store
        if store is None:  # pragma: no cover - 由调用方保证
            raise FlowError(E3003, "落盘 Ref 需要 ObjectStore", path="object_store")
        # 全部落进 parcel_data 一类；field 身份进 digest 的内容，不靠目录名区分
        # （目录名是类别，不是命名空间；用 capability_field 当类别会撞白名单）。
        kind = "parcel_data"

        if hasattr(value, "shape") and hasattr(value, "dtype"):
            import pyarrow as pa
            arr = np.asarray(value)
            # 只接受 1D/2D：当前能力 data 通道（掩码/分数/暴露/权重）都是二维
            # 面板或一维序列。>2D 若静默 reshape，读侧无从还原原形状（sidecar
            # 只记列名/dtype/行数），会变成"ref 存在但内容语义已变"——fail-closed。
            if arr.ndim > 2:
                raise FlowError(
                    E3003,
                    f"数据通道 {kind} 是 {arr.ndim}D，落盘只支持 1D/2D",
                    path=f"parcel_data.{kind}",
                    hint="高维对象须先在能力层降维并显式返回 shape 字段",
                )
            table = pa.table({"value": arr.tolist()}) if arr.ndim == 1 \
                else pa.table({f"c{i}": arr[:, i] for i in range(arr.shape[1])})
            return str(store.write_table(kind, table).digest)

        digest_fn = getattr(value, "digest", None)
        if callable(digest_fn) and not isinstance(value, (list, tuple, dict)):
            # 值对象：digest 是身份；写 json 摘要让 parcel.json 可独立重建形状等元信息
            summary = {
                "digest": str(digest_fn()),
                "type": type(value).__name__,
                "shape": list(getattr(value, "shape", ()) or ()),
            }
            return str(store.write_json(kind, summary).digest)

        return str(store.write_json(kind, _jsonable(value)).digest)

    def _build_exposures(
        self,
        candidates: list[Mapping[str, Any]],
        upstream: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """逐候选调 evaluate 生成因子暴露（observe 的 exposures 通道）。

        这是编排层驱动的能力子调用：evaluate 是独立能力，走自己完整的六步校验，
        算法无法替换求值口径。候选表达式非法 / evaluate 失败时跳过该候选（它
        在 observe 的 Stage A 会因无暴露被拒），不让一个坏 AST 中断整批。
        没有 upstream panel 时返回空（observe 会 fail-closed，不假成功）。

        **跳过必须留痕**：返回的第二个值是「求不出暴露的候选」清单
        （`candidate_id` + 原因码 + 报错摘要），由调用方写进 Search 产物的
        `unobservable` 通道。Search 的 `count` 是 65、Evidence 的
        `per_candidate` 是 57 时，差额必须能指名道姓地回答「哪 8 个、为什么」——
        否则一个 evaluate 回归就能让好因子凭空消失而无人在意（V5 首跑实测
        65→57，差额此前只活在注释里）。
        """
        from importlib.metadata import version as pkg_version

        from capability.registry import default_registry

        panel = None
        for produced in upstream.values():
            for key in ("panel",):
                if isinstance(produced.get(key), object) and produced.get(key) is not None:
                    panel = produced[key]
                    break
            if panel is not None:
                break
        if panel is None:
            return {}, []

        reg = default_registry()
        out: dict[str, Any] = {}
        unobservable: list[dict[str, Any]] = []
        for cand in candidates:
            ast = (cand.get("payload") or {}).get("expression")
            cid = cand.get("candidate_id")
            if ast is None or cid is None:
                unobservable.append({
                    "candidate_id": str(cid or ""),
                    "reason": "missing_expression",
                    "detail": "候选 payload 里没有 expression",
                })
                continue
            try:
                # station 用 "Search"：这次 evaluate 是 Search 工位内部的求值
                # 子调用（与 search.py fitness 的 evaluate 同构），不是独立工位。
                # spec.py 声明 evaluate 服务 Evidence/Certify 指的是工位主能力，
                # 不排除被搜索工位当作子调用——子调用 station 必须与父一致
                # （runtime._check_composition 的 E2001 要求）。
                eval_call = Call.create(
                    capability="evaluate", station="Search", impl="qweave",
                    impl_version=pkg_version("qweave"),
                    params={"operator_set": reg.operators_version,
                            "nan_policy": "propagate", "node_cache": False},
                    inputs=(InputRef("payload", _digest_of(ast), data=ast),
                            InputRef("panel", _digest_of(panel), data=panel)),
                )
                result = capability_run(eval_call)
                values = result["values"]
                valid = result["valid_mask"]
                # observe 要求 exposure 是 {values, valid_mask, values_ref}，且
                # values_ref 必须是数组内容摘要（array_digest）——由 evaluate 的
                # values_ref 通道原样带过来，不在这里重算（身份只有一份真相）。
                out[cid] = {
                    "values": values,
                    "valid_mask": valid,
                    "values_ref": result["values_ref"],
                }
            except (FlowError, KeyError, TypeError) as exc:
                unobservable.append({
                    "candidate_id": str(cid),
                    "reason": getattr(exc, "code", "") or type(exc).__name__,
                    "detail": str(getattr(exc, "message", "") or exc)[:200],
                })
                continue
        return out, unobservable

    def _funding_input(
        self,
        *,
        station: str,
        upstream: Mapping[str, Mapping[str, Any]],
        shape: tuple[int, ...],
        params: Mapping[str, Any] | None = None,
    ) -> np.ndarray | None:
        """从 supply 注入读资金费事件网格并对齐到面板；没注入就是 None。

        网格在 `app/cli/live.py` 按 `(t_{k-1}, t_k]` 逐 bar 对事件**求和**
        聚合好（I34：结算间隔 1h/4h/8h 混合，按周期常数乘必错）；本方法只做
        与 tradable/tiers 相同的机械对齐，不重算、不补点、不插值。
        """
        if self.supply_fn is None:
            return None
        injected = self.supply_fn(station, params or {})
        funding = injected.get("funding") if isinstance(injected, Mapping) else None
        if funding is None:
            return None
        aligned = _align_to_panel(
            funding, shape=tuple(shape), upstream=upstream,
            station=station, name="funding",
        )
        return np.asarray(aligned, dtype="float64")

    def _run_single_factor_costs(
        self,
        *,
        returned: dict[str, Any],
        upstream: Mapping[str, Mapping[str, Any]],
        policy_view: Mapping[str, Any],
        runtime_facts: Mapping[str, Any],
        phase: str,
        seed: int,
        station_revision: str,
        station: str,
    ) -> None:
        """S4：单因子净成本必须与组合层读同一个 allocate/backtest。

        S3 的 observe 只给 `est_cost=0` 的诚实占位；这里在编排层按候选暴露
        逐条调用真实 allocate + backtest，把 `cost_adj_return` 换成净成本口径。
        能力层仍不自己调子能力，也不读配置——`composes` 已在 spec 声明，
        本方法只做机械接续。
        """
        rows = returned.get("per_candidate")
        if not isinstance(rows, list) or not rows:
            return
        candidate_set = _first_object(upstream, "candidate_set")
        exposures = candidate_set.get("exposures") if isinstance(
            candidate_set, Mapping) else None
        if not isinstance(exposures, Mapping) or not exposures:
            return  # 没有暴露时 observe 自身已拒绝，不做假成本
        panel = _first_object(upstream, "panel")
        price = None
        if panel is not None:
            get_field = getattr(panel, "field", None)
            if callable(get_field) and "close" in getattr(panel, "fields", {}):
                price = get_field("close")
        tradable = _first_object(upstream, "tradable_mask")
        tiers = _first_object(upstream, "tiers")
        if tradable is None:
            scope = _first_object(upstream, "scope")
            tradable = getattr(scope, "tradable_mask", None)
        if tiers is None:
            scope = _first_object(upstream, "scope")
            tiers = getattr(scope, "tiers", None)
        if price is None or tradable is None or tiers is None:
            raise FlowError(E3003, "Evidence 的 S4 成本链缺价格/掩码/分层",
                            path=f"station.{station}.inputs")
        # 时间轴必须与价格同源：backtest 用真实 bar 时间喂 ml4t，年化也要按它
        # 推导每年 bar 数。缺时间轴时 backtest 会退回日频假设，接小时线就错 sqrt(24)。
        times = _panel_times(panel, station=station)

        updated: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw) if isinstance(raw, Mapping) else {}
            cid = str(row.get("candidate_id", ""))
            item = exposures.get(cid)
            if not isinstance(item, Mapping):
                updated.append(row)
                continue
            values = np.asarray(item.get("values"), dtype="float64")
            valid = np.asarray(item.get("valid_mask"), dtype=bool)
            if values.ndim != 2 or valid.shape != values.shape:
                raise FlowError(E3003, f"候选 {cid} 暴露不是同形二维矩阵",
                                path="observe.inputs.candidate_set.exposures")
            score = np.where(valid, values, np.nan)
            aligned_tradable = _align_to_panel(
                tradable, shape=score.shape, upstream=upstream,
                station=station, name="tradable_mask",
            ).astype(bool)
            aligned_tiers = _align_to_panel(
                tiers, shape=score.shape, upstream=upstream,
                station=station, name="tiers",
            )
            aligned_price = _align_to_panel(
                price, shape=score.shape, upstream=upstream,
                station=station, name="price",
            )
            if times.size != aligned_price.shape[0]:
                raise FlowError(
                    E3003, "Evidence 的 S4 链时间轴与价格行数不一致",
                    path=f"station.{station}.inputs.time",
                )
            # 资金费与价格同形后再进回测：口径开了（fee_model）且供给注入了
            # 事件网格才计费；`include_funding` 只描述「有没有拿到数据」，
            # 真正的门仍然是 caliber.net.fee_model——口径不承认的成本进不来。
            aligned_funding = self._funding_input(
                station=station, upstream=upstream, shape=aligned_price.shape,
            )

            a_impl, a_ver = self._resolve_impl("allocate", "", "")
            alloc_call = Call.create(
                capability="allocate", station=station,
                station_revision=station_revision, impl=a_impl, impl_version=a_ver,
                phase=phase, params={},
                inputs=(
                    InputRef("score", array_digest(score), data=score),
                    InputRef("tradable_mask", array_digest(aligned_tradable),
                             data=aligned_tradable),
                    InputRef("tiers", array_digest(aligned_tiers), data=aligned_tiers),
                    # price 只在 weighting=inv_vol 时被读；无论如何都传，避免
                    # 同一口径下「有时能算有时算不出」的隐藏分叉。
                    InputRef("price", array_digest(aligned_price), data=aligned_price),
                ),
                policy_view=_scoped_view("allocate", policy_view), runtime_facts=runtime_facts, seed=seed,
            )
            alloc_out = capability_run(alloc_call)
            position = alloc_out.get("weights")
            if not isinstance(position, np.ndarray):
                raise FlowError(E3003, "allocate 未返回持仓数组",
                                path="allocate.outputs.weights")

            b_impl, b_ver = self._resolve_impl("backtest", "", "")
            bt_call = Call.create(
                capability="backtest", station=station,
                station_revision=station_revision, impl=b_impl, impl_version=b_ver,
                phase=phase,
                params={"detail": "summary",
                        "include_funding": aligned_funding is not None},
                inputs=(
                    InputRef("position", array_digest(position), data=position),
                    InputRef("price", array_digest(aligned_price), data=aligned_price),
                    InputRef("funding", array_digest(aligned_funding),
                             data=aligned_funding)
                    if aligned_funding is not None else None,
                    InputRef("tiers", array_digest(aligned_tiers), data=aligned_tiers),
                    InputRef("time", array_digest(times), data=times),
                ),
                policy_view=_scoped_view("backtest", policy_view), runtime_facts=runtime_facts, seed=seed,
            )
            bt_out = capability_run(bt_call)

            periods = max(score.shape[0] - 1, 1)
            breakdown = dict(bt_out.get("cost_breakdown") or {})
            total = float(breakdown.get("total", 0.0))
            per_period = total / periods
            metrics = dict(row.get("metrics") or {})
            # 只有毛收益已知时才换算出净收益：毛收益缺失是「证据不足」，
            # 不能拿 0 当毛收益再减成本——那会凭空造出一个负的 cost_adj_return，
            # 把「算不出来」伪装成「算出来是负的」。
            # 同理也不能「缺失就跳过」：跳过会让 `cost_adj_return` 停在上游
            # observe 的 est_cost=0 占位，成本闸按毛收益放行却仍叫 cost_adj。
            # V5 首跑实测就踩中这条（observe 未输出 gross_return → 静默跳过）。
            gross = metrics.get("gross_return")
            if gross is None:
                raise FlowError(
                    E3003,
                    f"候选 {cid} 缺 gross_return，无法把 S3 零成本占位换成净成本",
                    path="observe.outputs.per_candidate.metrics",
                )
            net = float(gross) - per_period
            metrics["cost_adj_return"] = net
            metrics["net_return"] = net
            row["metrics"] = metrics
            # turnover 是逐 bar 序列（backtest 的 turnover_series），取均值时
            # 不能写 `x or 0.0`：数组的真值判断会抛 ValueError。
            turnover_series = bt_out.get("turnover")
            turnover_mean = (float(np.mean(np.asarray(turnover_series)))
                             if turnover_series is not None else 0.0)
            row["cost_attribution"] = {
                "turnover": turnover_mean,
                "turnover_per_bar": turnover_mean,
                "estimated_cost": per_period,
                "gross_return": float(gross),
                "net_return": net,
                "breakdown": {k: float(v) / periods for k, v in breakdown.items()
                              if k in {"fee", "slippage", "funding", "total"}},
                "method_id": method_id("allocate+backtest"),
                "note": "同一 caliber 的真实净成本，按每期摊还；毛收益取候选自身多空组合",
            }
            row["net_ref"] = str(bt_out.get("net_return_ref", ""))
            updated.append(row)
        returned["per_candidate"] = updated

    def _run_portfolio_tail(
        self,
        *,
        station: str,
        policy_view: Mapping[str, Any],
        runtime_facts: Mapping[str, Any],
        phase: str,
        seed: int,
        station_revision: str,
        upstream: Mapping[str, Mapping[str, Any]],
        score: np.ndarray,
    ) -> dict[str, Any]:
        """S4：combine 的分数 → allocate → backtest（同一工位内的能力子调用）。

        这是编排层的机械接续，不是第四个工位，也不产结论：`allocate` 的口径
        只从 `policy_view.allocate` 读，`backtest` 只从 `policy_view.net` 读。
        两个子调用各自走 `runtime.run` 的六步校验；任何失败都 fail-closed。
        """
        tradable = _first_object(upstream, "tradable_mask")
        tiers = _first_object(upstream, "tiers")
        if tradable is None:
            scope = _first_object(upstream, "scope")
            tradable = getattr(scope, "tradable_mask", None)
        if tiers is None:
            scope = _first_object(upstream, "scope")
            tiers = getattr(scope, "tiers", None)
        panel = _first_object(upstream, "panel")
        price = None
        if panel is not None:
            get_field = getattr(panel, "field", None)
            if callable(get_field) and "close" in getattr(panel, "fields", {}):
                price = get_field("close")
        if tradable is None or tiers is None or price is None:
            raise FlowError(
                E3003, "Portfolio 的 S4 链缺 tradable_mask/tiers/close 价格",
                path=f"station.{station}.inputs",
            )
        times = _panel_times(panel, station=station)
        if times.size != score.shape[0]:
            raise FlowError(
                E3003, "Portfolio 的 S4 链时间轴与合成分数行数不一致",
                path=f"station.{station}.inputs.time",
            )

        tradable = _align_to_panel(
            tradable, shape=score.shape, upstream=upstream,
            station=station, name="tradable_mask",
        ).astype(bool)
        tiers = _align_to_panel(
            tiers, shape=score.shape, upstream=upstream,
            station=station, name="tiers",
        )
        price = np.asarray(price, dtype="float64")
        if score.shape != tradable.shape or score.shape != tiers.shape \
                or score.shape != price.shape:
            raise FlowError(
                E3003, "Portfolio 的合成分数与掩码/分层/价格形状不一致",
                path=f"station.{station}.inputs",
            )

        a_impl, a_ver = self._resolve_impl("allocate", "", "")
        alloc_call = Call.create(
            capability="allocate", station=station, station_revision=station_revision,
            impl=a_impl, impl_version=a_ver, phase=phase, params={},
            inputs=(
                InputRef("score", array_digest(score), data=score),
                InputRef("tradable_mask", array_digest(tradable), data=tradable),
                InputRef("tiers", array_digest(tiers), data=tiers),
                InputRef("price", array_digest(price), data=price),
            ),
            policy_view=_scoped_view("allocate", policy_view), runtime_facts=runtime_facts, seed=seed,
        )
        alloc_out = capability_run(alloc_call)
        position = alloc_out.get("weights")
        if not isinstance(position, np.ndarray):
            raise FlowError(E3003, "allocate 未返回持仓数组", path="allocate.outputs.weights")

        b_impl, b_ver = self._resolve_impl("backtest", "", "")
        funding = self._funding_input(
            station=station, upstream=upstream, shape=score.shape,
        )
        bt_call = Call.create(
            capability="backtest", station=station, station_revision=station_revision,
            impl=b_impl, impl_version=b_ver, phase=phase,
            params={"detail": "summary", "include_funding": funding is not None},
            inputs=(
                InputRef("position", array_digest(position), data=position),
                InputRef("price", array_digest(price), data=price),
                InputRef("funding", array_digest(funding), data=funding)
                if funding is not None else None,
                InputRef("tiers", array_digest(tiers), data=tiers),
                InputRef("time", array_digest(times), data=times),
            ),
            policy_view=_scoped_view("backtest", policy_view), runtime_facts=runtime_facts, seed=seed,
        )
        bt_out = capability_run(bt_call)
        return {
            "alloc_call": alloc_call, "alloc_out": alloc_out,
            "bt_call": bt_call, "bt_out": bt_out,
        }

    def _certify_arrays(
        self, *, upstream: Mapping[str, Mapping[str, Any]], station: str,
    ) -> dict[str, Any]:
        """把 Certify 的三个逐格输入对齐到面板时间轴。

        只做形状对齐（同一批 bar），不改值、不补点：掩码/分层是 Boundary 在
        整段供给上的判定，面板是它的子集，取子集元素即可；不是子集就
        `E3003`（`_align_to_panel` 会拒绝）。
        """
        panel = _first_object(upstream, "panel")
        if panel is None:
            raise FlowError(E3003, "Certify 缺 panel 对象", path=f"station.{station}.inputs")
        shape = tuple(getattr(panel, "shape", ()) or ())
        if len(shape) != 2:
            raise FlowError(E3003, "Certify 的 panel 形状非法", path=f"station.{station}.inputs")
        tradable = _first_object(upstream, "tradable_mask")
        tiers = _first_object(upstream, "tiers")
        if tradable is None or tiers is None:
            scope = _first_object(upstream, "scope")
            tradable = getattr(scope, "tradable_mask", None) if tradable is None else tradable
            tiers = getattr(scope, "tiers", None) if tiers is None else tiers
        if tradable is None or tiers is None:
            raise FlowError(E3003, "Certify 缺 tradable_mask/tiers",
                            path=f"station.{station}.inputs")
        get_field = getattr(panel, "field", None)
        price = None
        if callable(get_field) and "close" in getattr(panel, "fields", {}):
            price = get_field("close")
        if price is None:
            raise FlowError(E3003, "Certify 缺面板 close 价格",
                            path=f"station.{station}.inputs.price")
        return {
            "tradable_mask": _align_to_panel(
                tradable, shape=shape, upstream=upstream,
                station=station, name="tradable_mask",
            ).astype(bool),
            "tiers": _align_to_panel(
                tiers, shape=shape, upstream=upstream, station=station, name="tiers",
            ),
            "price": np.asarray(price, dtype="float64"),
        }

    def _run_certify_folds(
        self,
        *,
        station: str,
        policy_view: Mapping[str, Any],
        runtime_facts: Mapping[str, Any],
        phase: str,
        seed: int,
        station_revision: str,
        upstream: Mapping[str, Mapping[str, Any]],
        returned: dict[str, Any],
    ) -> None:
        """S7：逐折 allocate → backtest，把净表现回填进认证产物。

        为什么由编排层做而不是能力自己做：能力不得自建子调用（s0/04 §1.3），
        而「同一口径下算净表现」必须走同一个 `allocate` / `backtest`。折的
        **边界**是 certify 的产物（窗口与 embargo 语义归它），**净值**是这两个
        能力在同一 `policy_view` 下的产物——两段都不在桥里重算。

        四件事一件都不能省：
        1. 分数/价格/掩码/分层/时间轴先对齐到面板，再按折切片；
        2. 每折独立走完整 `runtime.run` 六步校验（不是桥内自己算）；
        3. 折间 `net_return[0]` 是结构性 0（t=0 无前序持仓），聚合时剔除；
        4. 任何一折失败即整站失败——「其余折还行」不是合格证。
        """
        folds_detail = returned.get("folds_detail")
        metrics = returned.get("metrics")
        if not isinstance(folds_detail, list) or not folds_detail:
            raise FlowError(E3003, "certify 未返回折明细", path="certify.outputs.folds_detail")
        if not isinstance(metrics, dict):
            raise FlowError(E3003, "certify.metrics 必须是映射", path="certify.outputs.metrics")

        portfolio = _first_object(upstream, "portfolio")
        panel = _first_object(upstream, "panel")
        if not isinstance(portfolio, Mapping) or panel is None:
            raise FlowError(E3003, "Certify 缺 portfolio/panel 对象",
                            path=f"station.{station}.inputs")
        score = portfolio.get("score")
        if not isinstance(score, np.ndarray) or score.ndim != 2:
            raise FlowError(E3003, "Certify 的 portfolio.score 不是二维数组",
                            path=f"station.{station}.inputs.portfolio.score")

        tradable = _first_object(upstream, "tradable_mask")
        tiers = _first_object(upstream, "tiers")
        if tradable is None or tiers is None:
            scope = _first_object(upstream, "scope")
            tradable = getattr(scope, "tradable_mask", None) if tradable is None else tradable
            tiers = getattr(scope, "tiers", None) if tiers is None else tiers
        if tradable is None or tiers is None:
            raise FlowError(E3003, "Certify 缺 tradable_mask/tiers",
                            path=f"station.{station}.inputs")
        price = None
        get_field = getattr(panel, "field", None)
        if callable(get_field) and "close" in getattr(panel, "fields", {}):
            price = get_field("close")
        if price is None:
            raise FlowError(E3003, "Certify 缺面板 close 价格",
                            path=f"station.{station}.inputs.price")
        times = _panel_times(panel, station=station)
        if times.size != score.shape[0]:
            raise FlowError(E3003, "Certify 时间轴与合成分数行数不一致",
                            path=f"station.{station}.inputs.time")

        tradable = _align_to_panel(tradable, shape=score.shape, upstream=upstream,
                                   station=station, name="tradable_mask").astype(bool)
        tiers = _align_to_panel(tiers, shape=score.shape, upstream=upstream,
                                station=station, name="tiers")
        price = np.asarray(price, dtype="float64")
        if score.shape != tradable.shape or score.shape != tiers.shape \
                or score.shape != price.shape:
            raise FlowError(E3003, "Certify 的分数与掩码/分层/价格形状不一致",
                            path=f"station.{station}.inputs")
        # 逐折切片前的同形资金费：认证与组合读同一份事件网格（AC27 的
        # 「同一 caliber_digest、只允许数据不同」也覆盖资金费来源）。
        funding = self._funding_input(
            station=station, upstream=upstream, shape=score.shape,
        )

        bpy = float(metrics.get("bars_per_year") or CALENDAR_DAYS_PER_YEAR)
        periods_all: list[np.ndarray] = []
        turnovers: list[np.ndarray] = []
        cost_total = {"fee": 0.0, "slippage": 0.0, "funding": 0.0, "total": 0.0}
        positive = 0

        for entry in folds_detail:
            if not isinstance(entry, dict):
                raise FlowError(E3003, "certify 的折明细结构非法",
                                path="certify.outputs.folds_detail")
            b0 = int(entry.get("start_index", -1))
            b1 = int(entry.get("end_index", -1))
            if not (0 <= b0 < b1 <= score.shape[0]):
                raise FlowError(E3003, f"certify 折区间 [{b0},{b1}) 越界",
                                path="certify.outputs.folds_detail")

            fold_score = np.ascontiguousarray(score[b0:b1])
            fold_price = np.ascontiguousarray(price[b0:b1])
            fold_tiers = np.ascontiguousarray(tiers[b0:b1])
            fold_tradable = np.ascontiguousarray(tradable[b0:b1])
            fold_time = np.ascontiguousarray(times[b0:b1])
            fold_funding = (np.ascontiguousarray(funding[b0:b1])
                            if funding is not None else None)

            a_impl, a_ver = self._resolve_impl("allocate", "", "")
            alloc_call = Call.create(
                capability="allocate", station=station, station_revision=station_revision,
                impl=a_impl, impl_version=a_ver, phase=phase, params={},
                inputs=(
                    InputRef("score", array_digest(fold_score), data=fold_score),
                    InputRef("tradable_mask", array_digest(fold_tradable),
                             data=fold_tradable),
                    InputRef("tiers", array_digest(fold_tiers), data=fold_tiers),
                    InputRef("price", array_digest(fold_price), data=fold_price),
                ),
                policy_view=_scoped_view("allocate", policy_view), runtime_facts=runtime_facts, seed=seed,
            )
            alloc_out = capability_run(alloc_call)
            position = alloc_out.get("weights")
            if not isinstance(position, np.ndarray):
                raise FlowError(E3003, "allocate 未返回持仓数组",
                                path="allocate.outputs.weights")

            b_impl, b_ver = self._resolve_impl("backtest", "", "")
            bt_call = Call.create(
                capability="backtest", station=station, station_revision=station_revision,
                impl=b_impl, impl_version=b_ver, phase=phase,
                params={"detail": "summary",
                        "include_funding": fold_funding is not None},
                inputs=(
                    InputRef("position", array_digest(position), data=position),
                    InputRef("price", array_digest(fold_price), data=fold_price),
                    InputRef("funding", array_digest(fold_funding), data=fold_funding)
                    if fold_funding is not None else None,
                    InputRef("tiers", array_digest(fold_tiers), data=fold_tiers),
                    InputRef("time", array_digest(fold_time), data=fold_time),
                ),
                policy_view=_scoped_view("backtest", policy_view), runtime_facts=runtime_facts, seed=seed,
            )
            bt_out = capability_run(bt_call)
            net = _net_series(bt_out)
            # t=0 是结构性 0（无前序持仓可吃收益），不是「这一期刚好不赚」。
            # 把它算进样本会人为压低均值、抬高期数，Sharpe 因此偏乐观。
            sample = net[1:] if net.size > 1 else net[:0]
            periods_all.append(sample)
            turnovers.append(_as_series(bt_out.get("turnover"), net.size))

            breakdown = dict(bt_out.get("cost_breakdown") or {})
            fold_cost = {k: float(breakdown.get(k, 0.0))
                         for k in ("fee", "slippage", "funding", "total")}
            for k in cost_total:
                cost_total[k] += fold_cost[k]

            fold_ret = _compound(sample)
            entry["net_return"] = fold_ret
            entry["sharpe"] = _sharpe(sample, bpy)
            entry["turnover"] = float(np.mean(turnovers[-1])) if net.size else 0.0
            entry["max_drawdown"] = _max_drawdown(net)
            entry["cost_total"] = fold_cost["total"]
            entry["cost_breakdown"] = fold_cost
            entry["net_ref"] = str(bt_out.get("net_return_ref", ""))
            if fold_ret > 0.0:
                positive += 1

        combined = np.concatenate(periods_all) if periods_all else np.zeros(0)
        total_periods = int(combined.size)
        metrics["net_return"] = float(_compound(combined))
        metrics["net_sharpe"] = _sharpe(combined, bpy)
        metrics["turnover"] = (
            float(np.sum([t.sum() for t in turnovers]) / max(total_periods, 1))
            if total_periods else 0.0
        )
        metrics["max_drawdown"] = _max_drawdown(combined)
        metrics["period_count"] = total_periods
        metrics["positive_folds"] = int(positive)
        metrics["cost_breakdown"] = cost_total
        metrics["cost_per_period"] = float(cost_total["total"] / max(total_periods, 1))

        # 证书清单的 `policy_digest` 只能由编排层填：它是 `H(baseline_version,
        # constitutive_subset, caliber_digest, domain_digest)`，前两项在能力
        # 调用单里没有通道。能力层写的是 `policy_view_digest`（如实描述），
        # 两个键并存才能既对账又不撒谎。
        manifest = returned.get("reproducible_manifest")
        if isinstance(manifest, dict) and self.policy_digest:
            manifest["policy_digest"] = str(self.policy_digest)

    def invoke(
        self,
        *,
        station: str,
        capability: str | None,
        impl: str,
        impl_version: str,
        params: Mapping[str, Any],
        policy_view: Mapping[str, Any],
        runtime_facts: Mapping[str, Any],
        phase: str,
        seed: int,
        station_revision: str,
        upstream: Mapping[str, Mapping[str, Any]],
        shared: Mapping[str, Any] | None = None,
    ) -> BridgeResult:
        """构造 Call → run() → 拆落盘视图 / 内存对象。"""
        cap = capability or STATION_CAPABILITY.get(station)
        if not cap:
            raise FlowError(
                E1004, f"工位 {station} 没有能力映射", path=f"station.{station}.capability",
            )
        # 未实现能力先拒绝（E3004），再谈输入解析。否则对一个根本没有实现的
        # 能力先报「缺输入 E3003」会把「S4/S7 未做」误导成「数据没准备好」。
        if not installed_impls().get(cap):
            raise FlowError(
                E3004, f"工位 {station} 的能力 {cap} 尚无真实实现（S4/S7 未接入）",
                path=f"capability.{cap}",
                hint="按阶段填实现；不得回落到桩成功",
            )
        shared = dict(shared or {})
        if cap == "combine" and station == "Portfolio":
            shared = _portfolio_shared(shared, upstream)
            # 零合格成员是**结果**，不是故障（s0/05 §4.2 / AC52）。combine 自己
            # 必须拒绝空集合（那是它的前置条件），所以「跳过」只能由编排层判：
            # 这里先数一遍合格成员，空则短路成 skipped(no_candidate_passed)，
            # 不构建调用单、不落 parcel、不让下游拿空组合去认证。
            if _eligible_members(shared) == 0:
                return BridgeResult(payload={}, objects={}, capability=cap,
                                    skip_reason="no_candidate_passed")
        if cap == "certify":
            # 认证读的是面板时间轴上的逐格事实；Boundary 的掩码覆盖整段供给，
            # 行数天然不同。对齐是桥的职责（与另两条 S4 链同一个 helper），
            # 不是能力的：能力层拿不到 Scope 的全量时间轴。
            shared.update(self._certify_arrays(upstream=upstream, station=station))
        inputs: list[InputRef | None] = []
        for decl in _input_decls(cap):
            kind = str(decl["kind"])
            if kind == "all_upstream":
                # certify 的特殊通道：展开全部上游对象（能力未实现时不会到这一步）
                for produced in upstream.values():
                    for key, value in produced.items():
                        inputs.append(InputRef(key, _digest_of(value), data=value))
                continue
            try:
                obj = self._resolve_input(
                    kind, station=station, upstream=upstream, shared=shared,
                    params=params,
                )
            except FlowError:
                # 可选输入（如 allocate 的 price）取不到就落 None：能力自己按口径
                # 判断要不要它。必填输入仍然照原样抛，不做任何降级。
                if decl.get("required", True):
                    raise
                inputs.append(None)
                continue
            inputs.append(InputRef(kind, _digest_of(obj), data=obj))

        # 实现名解析：工位 default_impl 是**工位语义名**（boundary@1/panel@1），
        # 能力注册名是**算法名**（pit_dynamic_topk/point_in_time_align）。二者不同，
        # 不能把工位名原样传给 resolve（single/fixed 传 name 会 E1002）。
        # 规则：单一实现直接取注册表唯一条目；多实现（open/closed_multi）按 plan
        # 声明的 impl 名匹配，匹配不到由 resolve 报 E3004（不降级到任一实现）。
        real_impl, real_version = self._resolve_impl(cap, impl, impl_version)

        # policy_view 按能力的 policy_reads 裁剪：runtime._check_policy_scope 要求
        # 调用单只含本能力声明过的口径子块，多带一个即 E2001（"读到了即越权"）。
        declared = set(describe_capability(cap).policy_reads)
        scoped_policy = {k: v for k, v in policy_view.items() if k in declared}

        call = Call.create(
            capability=cap, station=station, station_revision=station_revision,
            impl=real_impl, impl_version=real_version, phase=phase, params=params,
            inputs=inputs, policy_view=scoped_policy, runtime_facts=runtime_facts,
            seed=seed,
        )
        returned = capability_run(call)
        # S4 单因子成本链：把 observe 的 S3 零成本占位替换成同一 caliber 下的
        # allocate + backtest 真实净成本；失败即整站 fail-closed。
        # 只在计划显式要求成本归因时跑（`evidence.params.cost_attribution`）：
        # 逐候选回测是本链最贵的一步，关掉归因就不该付这份代价；关掉时 payload
        # 里保留 observe 的 `estimated_cost=0` 占位，语义诚实、不冒充净成本。
        if cap == "observe" and station == "Evidence" \
                and bool(params.get("cost_attribution", True)):
            # 净成本链要读 Boundary 的 tradable_mask/tiers，而它们不在 Evidence
            # 的直接上游里（规格只声明 Candidate/Panel/Label）。把 prior_objects
            # 并入可用池：只做按 kind 的机械查找，不改变调用单的输入指纹。
            pool: dict[str, dict[str, Any]] = {
                name: dict(objs) for name, objs in
                (shared.get("prior_objects") or {}).items()
            }
            for name, objs in upstream.items():
                pool.setdefault(name, {}).update(objs)
            self._run_single_factor_costs(
                returned=returned, upstream=pool, policy_view=policy_view,
                runtime_facts=runtime_facts, phase=phase, seed=seed,
                station_revision=station_revision, station=station,
            )
        # S4 Portfolio 的组合诊断尾链：combine 只交分数，仓位与净表现必须由
        # 同一 caliber 下的 allocate/backtest 算出。子调用失败即整站失败。
        tail: dict[str, Any] | None = None
        if cap == "combine" and station == "Portfolio":
            score = returned.get("score")
            if not isinstance(score, np.ndarray):
                raise FlowError(E3003, "combine 未返回分数数组", path="combine.outputs.score")
            tail = self._run_portfolio_tail(
                station=station, policy_view=policy_view, runtime_facts=runtime_facts,
                phase=phase, seed=seed, station_revision=station_revision,
                upstream=upstream, score=score,
            )

        # S7 Certify：certify 只切折并装配清单，逐折净表现必须由同一
        # `allocate` + `backtest` 在同一个 policy_view 下算出（I28/AC27）。
        if cap == "certify":
            self._run_certify_folds(
                station=station, policy_view=policy_view, runtime_facts=runtime_facts,
                phase=phase, seed=seed, station_revision=station_revision,
                upstream=upstream, returned=returned,
            )

        markers = _output_markers(cap)
        disk: dict[str, Any] = {}
        objects: dict[str, Any] = {}
        # 能力可能自己返回原生 <field>_ref（内容 digest），如 align.panel_ref、
        # label.score_ref。这些是能力层算出的身份，必须保留：派生 ref 若覆盖它们，
        # 同一个对象就有两个 digest，下游按 ref 取回的内容与能力声明的身份不符。
        # 所以先收集全部原生字段，派生时只填**没有原生 ref** 的通道。
        native_keys = set(returned)
        for key, value in returned.items():
            if markers.get(key) in _MEMORY_MARKERS:
                objects[key] = value
                # S3-M6 落盘接线（s0/05 §3.1）：数据通道写进对象存储得到
                # <field>_ref，才能落到 Parcel.payload、让下游按 Ref 重建。
                # 能力已给出同名原生 ref 时跳过派生（身份只有一份真相）。
                ref_key = f"{key}_ref"
                if self.object_store is not None and ref_key not in native_keys:
                    disk[ref_key] = self._write_ref(cap, key, value)
            else:
                disk[key] = _jsonable(value)

        if tail is not None:
            alloc_out = tail["alloc_out"]
            bt_out = tail["bt_out"]
            # combine 的 `member_weights_ref` 是成员谱系权重；这里的 weights_ref
            # 是 (T,N) 目标持仓，名字空间不同，不能被对方覆盖。
            disk["weights_ref"] = str(alloc_out["weights_ref"])
            disk["binding_constraints"] = _jsonable(alloc_out["binding_constraints"])
            disk["net_ref"] = str(bt_out["net_return_ref"])
            cost = dict(bt_out["cost_breakdown"])
            cost["fees"] = cost.get("fee", 0.0)  # 兼容 s0/05 的 `{fees,slippage,funding}`
            disk["cost_breakdown"] = _jsonable(cost)
            disk["evidence_grade"] = "in_sample"
            disk["portfolio_stage"] = "S4_diagnostic"
            # 下游 Certify（S7）按 kind 取对象；本阶段只保证对象表不断链。
            objects["position"] = alloc_out["weights"]
            objects["net_return"] = bt_out.get("net_return_ref")
            # 单命令 `flow backtest` 要在不落盘的前提下拿到「这次回测到底跑了什么」：
            # payload 里只有 ref，净值数组与年度化口径都在 bt_out 里。放进内存对象
            # 表（不进 payload、不进指纹），让 CLI 复算结果可直接阅读。
            net_series = np.asarray(bt_out["net_return"], dtype="float64")
            objects["net"] = net_series
            total = float(_compound(net_series))
            bpy = float(bt_out.get("bars_per_year", 0.0) or 0.0)
            curve = np.cumprod(1.0 + net_series)
            peak = np.maximum.accumulate(curve)
            max_dd = float(np.min(curve / np.maximum(peak, 1e-12) - 1.0)) \
                if curve.size else 0.0
            objects["tail_summary"] = {
                # 规范键（09 §4 的输出契约）与精确键并存：前者给报表，
                # 后者给复核——只留一套必然有一边要猜。
                "sharpe": float(bt_out.get("sharpe", 0.0)),
                "cagr_pct": float(((1.0 + total) ** (bpy / max(net_series.size, 1))
                                   - 1.0) * 100.0) if bpy > 0 else 0.0,
                "max_dd_pct": max_dd * 100.0,
                "turnover": float(np.mean(np.asarray(bt_out.get("turnover", [])))),
                "net_return": total,
                "net_sharpe": float(bt_out.get("sharpe", 0.0)),
                "bars_per_year": bpy,
                "annualisation_used": str(bt_out.get("annualisation_used", "")),
                "turnover_mean": float(np.mean(np.asarray(bt_out.get("turnover", [])))),
                "funding_total": float(bt_out.get("funding_total", 0.0)),
                "cost_breakdown": {k: float(v) for k, v in
                                   (bt_out.get("cost_breakdown") or {}).items()},
                "periods": int(net_series.size),
            }
            # 认证需要的不是「持仓」也不是「净值」，而是**这次组合的全部构成**：
            # 合成分数、成员、三段窗口、域身份、样本内标记。按分散的 kind 传会
            # 让 certify 有能力「自己拼一个组合」，那它就能认证一个没跑过的组合。
            objects["portfolio"] = {
                "score": np.asarray(returned.get("score")),
                "members": list(returned.get("members") or []),
                "member_weights": returned.get("member_weights"),
                "domain_digest": str(returned.get("domain_digest", "")),
                "windows_used": dict(returned.get("windows_used") or {}),
                "evidence_grade": "in_sample",
                "position": alloc_out["weights"],
            }

        # 派生通道：下游能力按**逻辑 kind** 取整个产物，而能力 outputs 是拆开的
        # 字段。这里把拆开的字段重组为下游期望的单一对象（数据表驱动）。
        # - label 工位产出 score/valid_mask → 下游 search/observe 的 `label` 通道
        #   （一个带 score 数组的 Mapping，见 search._label / observe）。
        # - evidence 工位产出 per_candidate → 下游 verdict 的 `observation_set`。
        if cap == "label":
            score = objects.get("score")
            valid = objects.get("valid_mask")
            # label 产出的 valid_mask 是 (T,) 逐时点掩码；observe/evaluate 要求
            # score 与 valid_mask 同形二维。这里按 score 形状广播（机械变换，
            # 不是替能力判断——有效时点覆盖全部标的，见 label 的 sample 语义）。
            if score is not None and getattr(valid, "ndim", None) == 1 \
                    and getattr(score, "ndim", None) == 2:
                valid = np.broadcast_to(valid[:, None], score.shape)
            # `raw`（变换前标签收益，06 §5.6）与 `horizon_bars` 必须随对象走：
            # 之前漏掉 horizon_bars，observe 只能默认 1，年化按 √365 而非
            # √(365/h)——√h 倍虚高，且不报错（B4，AC63 对拍撞出来的）。
            objects["label"] = {
                "score": score,
                "raw": objects.get("raw"),
                "valid_mask": valid,
                "width_series": returned.get("width_series"),
                "effective_n": returned.get("effective_n"),
                "horizon_bars": returned.get("horizon_bars"),
                "overlap_policy_used": returned.get("overlap_policy_used"),
            }
        elif cap == "search":
            # observe 期望 {candidates, exposures}。exposures 需逐候选 evaluate
            # 得到——这是编排层驱动的能力子调用，observe 只读不重算（04 文档）。
            # 若 external_fn 已注入 exposures，直接用；否则用 evaluate 能力生成。
            cands = list(returned.get("candidates") or [])
            injected = bool(shared.get("exposures"))
            if injected:
                exposures = dict(shared.get("exposures") or {})
                unobservable: list[dict[str, Any]] = []
            else:
                exposures, unobservable = self._build_exposures(
                    cands, upstream, params,
                )
            # evaluate 失败的表达式不进观察链：它们没有 `values_ref`，observe
            # 只能拒绝，不能替它造一份暴露。Search 的 count/n_trials 仍按搜索
            # 实测记账；被排除的候选逐条写进 Search 产物的 `unobservable`
            # 通道（count 与 Evidence.per_candidate 的差额必须可指名核对），
            # 不让一个坏 AST 静默吃掉候选、也不让它中断整批。
            cands = [c for c in cands
                     if isinstance(c, Mapping)
                     and str(c.get("candidate_id")) in exposures]
            if not cands:
                raise FlowError(
                    E3003, "Search 产出的候选全部无法求值", path="search.outputs.candidates",
                )
            if unobservable or "unobservable" in disk:
                disk["unobservable"] = _jsonable(unobservable)
            objects["candidate_set"] = {
                "candidates": cands,
                "exposures": exposures,
            }
        elif cap == "observe":
            objects["observation_set"] = dict(returned)
        elif cap == "verdict":
            # Verdict 的 payload 只落盘；Portfolio 的 combine 需要按裁决筛选
            # 成员，因此内存对象表保留一份只读裁决集合。
            objects["verdict_set"] = dict(returned)
        elif cap == "certify":
            # `flow certify` 需要直接读证书本体；逐折明细与可复现清单都在这份
            # 返回值里，重算一次就能拿全，不需要去对象存储里按 ref 拼。
            objects["certificate"] = dict(returned)
        return BridgeResult(payload=disk, objects=objects, capability=cap)

    # ---- S5-M2：单步跨进程的对象重建 ----------------------------------
    def rebuild_upstream(
        self,
        parcels: Mapping[str, Any],
        impls: Mapping[str, str] | None = None,
        policy_view: Mapping[str, Any] | None = None,
        runtime_facts: Mapping[str, Any] | None = None,
        station_params: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """从冻结的正式产物重建上游内存对象表（单步没有 run 进程内的对象）。

        信任模型与 `_load_committed` 相同：**重推导 + 身份对拍**，不做「信
        parcel.json 就够了」的降级。三类对象三种重建法：

        - payload JSON 能完整表达的（Evidence/Verdict 的结论集合、Search 的
          候选清单）直接取自 payload——它们本来就是 JSON 值；
        - JSON 表达不了的（Panel/Scope 值对象、数组、组合对象）用**冻结的
          params/policy + supply 事实**重推导能力，并把重算 payload 的
          digest 与冻结 parcel 的 `payload_digest` 逐字节对拍，不一致即
          E3002（重算结果与当时落盘的不同源）；
        - Search 不重跑（append_only，GP 重放会改变候选身份）：候选取自
          payload，暴露走同一 `_build_exposures` 通道重算。

        `parcels` 按工位顺序给出（含 payload/payload_digest 属性）；`impls`
        是执行器按冻结声明解析好的 `station -> impl` 名——重推导必须用与
        当时相同的实现，否则对拍必然失败。
        """
        impls = dict(impls or {})
        station_params = {
            str(k): dict(v or {}) for k, v in (station_params or {}).items()
        }
        rebuilt: dict[str, dict[str, Any]] = {}
        for name, parcel in parcels.items():
            cap = STATION_CAPABILITY.get(name)
            if not cap:
                continue
            payload = getattr(parcel, "payload", None)
            if not isinstance(payload, Mapping) or not payload:
                continue
            params = dict(station_params.get(name, {}))
            if cap == "search":
                rebuilt[name] = self._rebuild_search_objects(payload, rebuilt, params)
                continue
            if cap in ("observe", "verdict"):
                # per_candidate / method_ids / n_trials_used 是纯 JSON 值：
                # payload 就是结论本身，重算反而会把净成本链白跑一遍。
                key = "observation_set" if cap == "observe" else "verdict_set"
                rebuilt[name] = {key: dict(payload)}
                continue
            # boundary / align / label / combine：重推导。
            # 注意 invoke 内部会跑 S4 尾链（combine）——重建成本与当时执行
            # 相当，这是「单步重放」诚实的代价；对拍不过就 fail-closed。
            impl_name, _, impl_version = str(impls.get(name, "")).partition("@")
            # 与 run 模式同形：Portfolio/Evidence 依赖非直接上游的对象
            # （combine 的成员池读 prior_objects），重建时同样要传。
            result = self.invoke(
                station=name, capability=None,
                impl=impl_name, impl_version=impl_version or "1",
                params=params,
                policy_view=dict(policy_view or {}),
                runtime_facts=dict(runtime_facts or {}),
                phase="none", seed=0, station_revision="rebuild",
                upstream=rebuilt,
                shared={"prior_objects": {
                    n: dict(objs) for n, objs in rebuilt.items() if n != name
                }},
            )
            if result.skip_reason is not None:
                raise FlowError(
                    E3002,
                    f"重建 {name} 得到 skip({result.skip_reason})，与冻结产物不同源",
                    path=f"parcel.{name}.rebuild",
                )
            frozen_digest = str(getattr(parcel, "payload_digest", "") or "")
            rebuilt_digest = compute_digest(result.payload)
            if not frozen_digest:
                raise FlowError(
                    E3002, f"{name} 的冻结产物缺少 payload_digest，无法对拍",
                    path=f"parcel.{name}.payload_digest",
                )
            if rebuilt_digest != frozen_digest:
                raise FlowError(
                    E3002,
                    f"重建 {name} 的 payload 与冻结产物不一致 "
                    f"(frozen={frozen_digest[:16]} rebuilt={rebuilt_digest[:16]})",
                    path=f"parcel.{name}.rebuild",
                    hint="冻结 params/policy 与当前实现的重算结果不同源；"
                         "确认实现版本未变、供给未被改写",
                )
            rebuilt[name] = dict(result.objects)
        return rebuilt

    def _rebuild_search_objects(
        self,
        payload: Mapping[str, Any],
        upstream: Mapping[str, Mapping[str, Any]],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Search 的对象重建：候选取自 payload（append_only，不重跑 GP），
        暴露走同一 evaluate 通道重算——evaluate 对同一表达式是决定论的。"""
        cands = list(payload.get("candidates") or [])
        exposures, _unobservable = self._build_exposures(cands, upstream, params)
        keep = [c for c in cands
                if isinstance(c, Mapping)
                and str(c.get("candidate_id")) in exposures]
        return {"candidate_set": {"candidates": keep, "exposures": exposures}}


def _content_view(value: Any) -> Any:
    """指纹用的内容视图：数组按**内容**入摘要，不按 shape/dtype。

    `_jsonable` 的 ndarray → `{shape,dtype}` 是**落盘投影**的规则（数组不该内联
    进 payload）。拿同一套规则算指纹会让「两个候选的组合分数」共享同一个输入
    摘要——形状一样就撞键，缓存随即把 A 的结果发给 B。
    """
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return array_digest(np.asarray(value))
    if isinstance(value, Mapping):
        return {str(k): _content_view(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_content_view(v) for v in value]
    return _jsonable(value)


def _digest_of(obj: Any) -> str:
    """输入对象 digest：优先用对象自己的 digest()，否则结构化摘要。"""
    fn = getattr(obj, "digest", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:  # noqa: BLE001 - digest 失败退回结构摘要，不静默空串
            pass
    if hasattr(obj, "shape") and hasattr(obj, "dtype"):
        # 数组内容必须进指纹：只写 shape/dtype 会让两份不同分数共享同一
        # call_digest，下游缓存直接串（S4 的 score/weights 都走这里）。
        return array_digest(np.asarray(obj))
    return compute_digest(_content_view(obj))


def _scoped_view(capability: str, policy_view: Mapping[str, Any]) -> dict[str, Any]:
    """按能力声明的 `policy_reads` 裁剪口径视图。

    桥里曾有三处一模一样的 `def scoped(capability)` 局部函数（单因子成本 /
    组合尾链 / 逐折认证）。裁剪规则只有一条：**只给声明过的子块**——多给一个
    键就是给能力层开了一条未声明的口径入口，虽然能力侧 `E2001` 会兜，但那是
    "越权之后被发现"，不是"没越权"。三份并存的真实风险是规则会分叉：给某一条
    链多放一个子块，只有那条链会静默拿到更宽的口径。
    """
    declared = set(describe_capability(capability).policy_reads)
    return {k: v for k, v in policy_view.items() if k in declared}


def _jsonable(value: Any) -> Any:
    """转 Value 为可 compute_digest / json.dumps 的结构；对象用 digest 代替。"""
    if isinstance(value, np.generic):
        # np.float64/int64/bool_ 不是 JSON 往返安全的标量：json.dumps 虽能渲染，
        # 但 reload 后变 Python 标量，内存 digest 与磁盘 digest 就分家了
        # （np.float64 的 repr 带 `np.float64(...)` 前缀，见 digest._encode_float）。
        return value.item()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    fn = getattr(value, "digest", None)
    if callable(fn) and not isinstance(value, (list, tuple, dict)):
        try:
            return str(fn())
        except Exception:  # noqa: BLE001
            return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "shape"):  # ndarray：不内联（digest.py:24）
        return {"shape": list(value.shape), "dtype": str(getattr(value, "dtype", ""))}
    return str(value)


__all__ = ["STATION_CAPABILITY", "BridgeResult", "CapabilityBridge"]
