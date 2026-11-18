"""`align` · 面板对齐：时间轴裁剪、未收盘丢弃、两份报告（s0/04 §3.3）。

四条约束（§3.3「`align` 的四条实现约束」）都在这一个函数里落地，但它们的失败
方式各不相同，值得分开看：

- **`field_lags` 与注册表不一致** 是最隐蔽的前视：注册表写 `available_lag=1`、
  实现按 0 对齐，两边都不报错，但因子已经用上了当根未收盘的价。
- **`tz` 非 UTC** 不会算错一次，而是让「同一时刻」出现两种表示，指纹随机器漂移。
- **`unclosed_dropped`** 是 V4 那根「末根未收盘 bar 进了面板」的可查证据；
  记了 0 条却丢了行，是同一类假成功。
- **`duplicate_index`** 是自证：它恒为 0，因为重复在 `Panel` 构造时已经 `E4007`。

`causal_probe` 的含义是**检查强度**，不是「跑一个探针算法」：

| 取值 | 执行 |
|---|---|
| `none` | 索引单调、保留区不进入面板 |
| `strict` | 以上 + 逐字段逐位比对（面板必须与流在保留行上的字节完全一致）+ 时间连续性（相邻行的时间间隔不得超出正常步长） |

原来的设计写的是「打乱未来值、看历史统计是否变化」。这个探测在纯数组上恒通过
——它测的是 `trailing_mean` 自己（一个已经实现为因果的算子），而不是数据。
一个永远不会失败的检查比没有检查更糟：它会在报告里以 `passed=true` 出现。
现在的三项都能真失败，其中「连续性」正是洞（`mine` 与 `oos` 之间被跳过一段）
的唯一可查证据。
"""

from __future__ import annotations

import numpy as np

from capability.panel import Panel, array_digest
from capability.registry import default_registry
from capability.stream import FieldStream, Scope
from contracts.errors import E3003, E3004, FlowError

_VISIBILITY = frozenset({"reserved", "revealed"})


def _input(call, kind: str):
    for item in call.inputs:
        if item is not None and item.kind == kind:
            return item.data
    raise FlowError(
        E3003, f"align 没有拿到 {kind} 输入", path=f"align.inputs.{kind}",
        hint="执行器负责解析；能力层不自己读盘",
    )


def _stream(call) -> FieldStream:
    data = _input(call, "field_stream")
    if not isinstance(data, FieldStream):
        raise FlowError(
            E3003,
            f"align 的 field_stream 输入不是 FieldStream，得到 {type(data).__name__}",
            path="align.inputs.field_stream",
        )
    return data


def _scope(call) -> Scope:
    data = _input(call, "scope")
    if not isinstance(data, Scope):
        raise FlowError(
            E3003, f"align 的 scope 输入不是 Scope，得到 {type(data).__name__}",
            path="align.inputs.scope",
        )
    return data


def _run_rows(axis: dict, times: np.ndarray) -> np.ndarray:
    run = np.zeros(times.size, dtype=bool)
    for key in ("mine", "oos"):
        span = axis.get(key) or {}
        lo, hi = span.get("start"), span.get("end")
        if lo is None or hi is None:
            continue
        if int(hi) <= int(lo):
            # 未声明 oos 时给的是空区间（start == end）；它不得命中任何时点，
            # 否则「没有样本外」会变成「样本外恰好一根」。
            continue
        run |= (times >= int(lo)) & (times <= int(hi))
    return run


def build(call) -> dict:
    params = call.params
    stream = _stream(call)
    scope = _scope(call)
    times = stream.times

    if not np.array_equal(times, scope.times) or stream.symbols != scope.symbols:
        raise FlowError(
            E3003,
            "scope 与 field_stream 的时间轴或标的不一致",
            path="align.inputs",
            hint="边界与面板必须来自同一次供给版本；不一致时掩码会错位",
        )

    visibility = str(call.runtime_facts.get("holdout_visibility", "reserved"))
    if visibility not in _VISIBILITY:
        raise FlowError(
            E3003,
            f"runtime_facts.holdout_visibility={visibility!r} 不在 {sorted(_VISIBILITY)}",
            path="align.runtime_facts.holdout_visibility",
            hint="是否揭示保留区是执行期事实，不得由 params 传入",
        )

    run = _run_rows(dict(scope.axis_resolved), times)
    keep = run.copy()
    unclosed = {"count": 0, "last_ts": None}
    if params["drop_unclosed"] and stream.end_closed is False and bool(keep[-1]):
        # 未收盘的是**数据末端**那一根，不是运行窗口末端。
        keep[-1] = False
        unclosed = {"count": 1, "last_ts": int(times[-1])}
    if not keep.any():
        raise FlowError(
            E3003, "对齐后没有任何时点", path="align.outputs.panel",
            hint="空面板不得当作成功——下游会把「没有数据」读成「没有信号」",
        )

    panel = Panel(
        times=times[keep],
        symbols=stream.symbols,
        fields={name: values[keep] for name, values in stream.fields.items()},
    )
    mode = str(params["causal_probe"])

    # ---- causal_report -------------------------------------------------
    reg = default_registry()
    field_lags: dict[str, int] = {}
    offending: list[dict] = []
    checks: list[dict] = []

    checks.append({
        "id": "P1", "scope": "time_index",
        "passed": bool(panel.times.size < 2 or np.all(np.diff(panel.times) > 0)),
        "offending": [],
    })

    for name, item in stream.manifest.items():
        got = int(item["available_lag"])
        field_lags[name] = got
        entry = reg.fields.get(name)
        if entry is None:
            offending.append({"field": name, "reason": "not_registered"})
        elif got != int(entry.get("available_lag", -1)):
            offending.append({
                "field": name,
                "reason": f"lag={got} 与注册表 {entry.get('available_lag')} 不一致",
            })
    if offending:
        raise FlowError(
            E3003,
            f"{len(offending)} 个字段的 available_lag 与注册表不符",
            path="align.outputs.causal_report.field_lags",
            hint="这是最容易被忽略的前视：两边都不报错，因子已经用上了未收盘的价",
        )
    # `passed` 在这个列表里恒为 True：不通过的情况上面已经 `E3003` 抛掉了，
    # 走不到这里。所以恒真项的价值不在布尔值，在**证据**——记下这次真的比对了
    # 几个字段。只留一个 `passed: true` 等于什么都没记（复核 E9）。
    checks.append({
        "id": "P2", "scope": "field_lags", "passed": True, "offending": [],
        "fields_checked": len(field_lags),
    })

    holdout = dict(scope.axis_resolved).get("holdout")
    if isinstance(holdout, dict):
        holdout_boundary = {"start": int(holdout["start"]), "end": int(holdout["end"])}
        leaks = (panel.times >= holdout_boundary["start"]) & (
            panel.times <= holdout_boundary["end"])
        if leaks.any():
            raise FlowError(
                E3003,
                f"面板含 {int(leaks.sum())} 根保留区时点",
                path="align.outputs.panel",
                hint="保留区的取值不得进入面板；「没看过保留区」靠这一条成立",
            )
    else:
        # reserved：面板就是 mine ∪ oos，保留区不在其中，边界因此「未定」
        holdout_boundary = {"start": None, "end": None}

    checks.append({
        "id": "P3", "scope": "holdout", "passed": True, "offending": [],
        # 同上：恒真项要带证据。保留区边界是这次实际用过的取值，不是常量。
        "boundary": holdout_boundary,
    })

    violations: list[dict] = []
    if mode == "strict":
        # 逐位比对：面板必须与「流在保留行上的字节」完全一致。
        # 任何位移、错行、补值都会在这里露出来——这是 align 唯一能自证的因果声明。
        for name in sorted(panel.fields):
            if not np.array_equal(
                panel.field(name), stream.fields[name][keep], equal_nan=True,
            ):
                violations.append({
                    "field": name, "at": None,
                    "reason": "面板取值与流在保留行上的取值不一致",
                })
        checks.append({
            "id": "P4", "scope": "byte_identity",
            "passed": not violations,
            "offending": [v["field"] for v in violations],
        })

        # 时间连续性：洞两侧在数组里相邻，而 forward_return / trailing 窗口
        # 都按「相邻一根」解释。洞一存在，mine 最后一根的标签就会用上 oos 的价格。
        step = int(np.median(np.diff(times))) if times.size > 1 else 0
        gaps = np.diff(panel.times)
        continuous = bool(step == 0 or gaps.size == 0 or np.all(gaps <= step * 1.5))
        checks.append({
            "id": "P5", "scope": "continuity", "passed": continuous, "offending": [],
        })
        if not continuous:
            raise FlowError(
                E3004,
                "面板时间轴不连续：mine 与 oos 之间存在被跳过的区间",
                path="align.outputs.panel",
                hint="跨洞的标签与窗口会按相邻 bar 计算；被跳过的区间必须声明为 holdout",
            )

    if violations:
        raise FlowError(
            E3004,
            f"因果探针发现 {len(violations)} 个字段与流不一致",
            path="align.outputs.causal_report.violations",
            hint="strict 模式整步失败，不降级——降级等于让前视以「警告」身份通过",
        )

    causal_report = {
        "checks": checks,
        "probe": {
            "mode": mode,
            "fields": sorted(panel.fields),
            "method": "byte_identity_and_continuity",
            "max_shift_used": 0,
        },
        "violations": violations,
        "field_lags": field_lags,
        "holdout_boundary": holdout_boundary,
    }

    # ---- quality_report ------------------------------------------------
    n_bars, n_sym = panel.shape
    missing_ratio = {
        name: float(np.mean(~np.isfinite(values))) for name, values in panel.fields.items()
    }
    # 有效观测数的判据字段：优先 `close`（价格存在即该标的当日有交易），
    # 没有价格字段的面板退到字典序第一个字段——不硬绑一个可能不存在的列。
    basis = "close" if "close" in panel.fields else sorted(panel.fields)[0]
    basis_values = panel.field(basis)
    min_obs = {
        sym: int(np.isfinite(basis_values[:, j]).sum())
        for j, sym in enumerate(panel.symbols)
    }

    gates = dict(params.get("quality_gates") or {})
    limit = gates.get("max_missing_ratio")
    if limit is not None:
        over = {k: v for k, v in missing_ratio.items() if v > float(limit)}
        if over:
            raise FlowError(
                E3003,
                f"字段缺失比例超上限 {float(limit):.0%}: {over}",
                path="align.params.quality_gates.max_missing_ratio",
                hint="缺失用 NaN 表示但不得静默填充；超限说明供给覆盖不足",
            )
    need_obs = gates.get("min_obs")
    if need_obs is not None:
        short = {k: v for k, v in min_obs.items() if v < int(need_obs)}
        if short:
            raise FlowError(
                E3003,
                f"标的有效观测数低于 min_obs={int(need_obs)}: {short}",
                path="align.params.quality_gates.min_obs",
                hint="逐个列出而不是报一个总数——哪一列不能用是逐列的问题",
            )

    quality_report = {
        "bars": int(n_bars),
        "symbols": int(n_sym),
        "fields": int(len(panel.fields)),
        "missing_ratio": missing_ratio,
        "min_obs_per_symbol": min_obs,
        "min_obs_basis": basis,
        "unclosed_dropped": unclosed,
        "duplicate_index": 0,
        # 时间轴是 int64 纳秒，本身没有时区。「UTC」是构造契约（数据层必须已归一），
        # 不是这里算出来的检查结果——写清来源，免得读者以为校验过。
        "tz": "UTC",
        "tz_source": "int64_utc_index",
    }

    field_manifest = [
        {
            "name": name,
            "dtype": str(panel.fields[name].dtype),
            "unit": str(stream.manifest[name]["unit"]),
            "available_lag": int(stream.manifest[name]["available_lag"]),
            "supply_version": str(stream.manifest[name].get("version", "")),
        }
        for name in sorted(panel.fields)
    ]

    return {
        "panel_ref": panel.digest(),
        "shape": {"bars": int(n_bars), "symbols": int(n_sym),
                  "fields": int(len(panel.fields))},
        "time_index_ref": array_digest(panel.times),
        "field_manifest": field_manifest,
        "causal_report": causal_report,
        "quality_report": quality_report,
        "panel": panel,
        "holdout_visibility": visibility,
    }
