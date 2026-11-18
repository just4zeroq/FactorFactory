"""`boundary` · 边界构建：时间轴解析、两张掩码、流动性分层（s0/04 §3.2）。

三件容易做错的事，都在这里定死：

1. **两张掩码不是一张表的两个视图。** `observable_mask` 回答「这个格能不能参与
   统计」，`tradable_mask` 回答「这个格能不能下权重」。未收盘 bar 是最典型的
   缺口：它能被观测（价格正在形成），不能用来下单（成交价还不存在）。合成一张
   就只能二选一，两种做法都错。
2. **理由码一格一个，且有优先级。** 一格可能同时满足「未上市」与「数据缺失」，
   不先定序，同一份数据两次运行会得到不同码值，`Scope` 的指纹随之漂移。
3. **保留区标的是补集，不是区间。** `holdout=reserved` 是声明，Boundary 不需要
   看到保留区的值（`05-orchestration.md` §1）——所以它按 `mine ∪ oos` 的补集
   标 `reserved`，而不是先去问保留区在哪。
"""

from __future__ import annotations

import numpy as np

from capability.params import duration_bars, implementation_defaults
from capability.stats import trailing_coverage, trailing_mean
from capability.stream import OBSERVABLE_CODES, REASON_CODES, FieldStream, Pin, Scope
from capability.timeaxis import resolve_interval
from contracts.errors import E1004, E1008, E3003, E4002, FlowError

_MAX_TIER_LEVELS = 8


# ---- 输入 ---------------------------------------------------------------
def _input(call, kind: str):
    for item in call.inputs:
        if item is not None and item.kind == kind:
            return item.data
    raise FlowError(
        E3003, f"boundary 没有拿到 {kind} 输入", path=f"boundary.inputs.{kind}",
        hint="执行器负责解析；能力层不自己读盘",
    )


def _pin(call) -> Pin:
    data = _input(call, "pin")
    if not isinstance(data, Pin):
        raise FlowError(
            E3003, f"boundary 的 pin 输入不是 Pin，得到 {type(data).__name__}",
            path="boundary.inputs.pin",
        )
    return data


def _stream(call) -> FieldStream:
    data = _input(call, "field_stream")
    if not isinstance(data, FieldStream):
        raise FlowError(
            E3003,
            f"boundary 的 field_stream 输入不是 FieldStream，得到 {type(data).__name__}",
            path="boundary.inputs.field_stream",
        )
    return data


# ---- 时间轴 -------------------------------------------------------------
# 区间解析只有 timeaxis 一份实现（E7）。这里曾自带两个本地解析器：把止日写成
# 「次日 00:00，side=left」，于是**次日第一根 bar 被算进前一段**。1d 数据下
# mine 的末日与 oos 的首日相邻时，那根 bar 同时落在两段里，而 `mine_hi ==
# oos_lo` 的检查照样通过——重叠被静默吞掉。timeaxis 用「末日最后一纳秒，
# side=right」，并与 read.window 共享同一份边界；复用它同时拿回 1900–2100
# 的纳秒 sanity（毫秒/秒时间戳在解析时就 E4002）。


def _resolve_axis(axis: dict, times: np.ndarray) -> tuple[dict, np.ndarray]:
    mine_lo, mine_hi = resolve_interval(axis.get("mine"), times, path="boundary.time_axis.mine")
    oos_text = axis.get("oos")
    if oos_text:
        oos_lo, oos_hi = resolve_interval(oos_text, times, path="boundary.time_axis.oos")
    else:
        oos_lo = oos_hi = mine_hi

    if mine_lo < oos_hi and oos_lo < mine_hi:
        raise FlowError(
            E4002, "mine 与 oos 区间重叠", path="boundary.time_axis",
            hint="样本内与样本外交叠，「样本外」就不再是样本外",
        )
    if oos_hi > oos_lo and mine_hi != oos_lo:
        # 两段之间留洞时，`align` 会把洞两侧按索引拼成相邻两行，而
        # `forward_return` / `trailing_mean` / `rolling_beta` 全都按「相邻一根」
        # 解释 —— mine 最后一根的标签会直接用上 oos 的价格。要么声明为 holdout
        # （那样它就不该出现在面板里），要么补全区间，不存在「中间跳过几天」。
        raise FlowError(
            E4002,
            "mine 与 oos 之间不得有洞",
            path="boundary.time_axis",
            hint="跨洞的标签与窗口会按相邻 bar 计算；被跳过的区间必须声明为 holdout",
        )

    run = np.zeros(times.size, dtype=bool)
    run[mine_lo:mine_hi] = True
    run[oos_lo:oos_hi] = True

    holdout = axis.get("holdout")
    if holdout is None or str(holdout) == "reserved":
        resolved_holdout: object = "reserved"
    elif not str(holdout).strip():
        # 空串不是「保留」也不是区间：`if holdout` 会把它当成缺省，
        # 于是一个写坏的配置被静默解释成最宽松的语义。
        raise FlowError(
            E4002, "holdout 不得为空串", path="boundary.time_axis",
            hint='要么写 "reserved"，要么写 "<起>..<止>"',
        )
    else:
        h_lo, h_hi = resolve_interval(str(holdout), times, path="boundary.time_axis.holdout")
        if (h_lo < mine_hi and mine_lo < h_hi) or (h_lo < oos_hi and oos_lo < h_hi):
            raise FlowError(
                E4002, "保留区与 mine/oos 相交", path="boundary.time_axis",
                hint="保留区的取值不得进入面板；相交就只能挖洞，而挖洞会让跨洞标签算错",
            )
        resolved_holdout = {
            "start": int(times[h_lo]), "end": int(times[h_hi - 1]),
        }

    def _span(lo: int, hi: int) -> dict:
        return {"start": int(times[lo]), "end": int(times[hi - 1])}

    n = times.size
    if oos_hi > oos_lo:
        oos_span = _span(oos_lo, oos_hi)
    else:
        # 未声明 oos：给一个空区间（start == end），而不是伪造一个时点
        oos_span = {"start": int(times[min(mine_hi, n - 1)]),
                    "end": int(times[min(mine_hi, n - 1)])}

    resolved = {
        "mine": _span(mine_lo, mine_hi),
        "oos": oos_span,
        "holdout": resolved_holdout,
    }
    return resolved, run


# ---- 分层与 top-k -------------------------------------------------------
def _tiers(adv: np.ndarray, edges: list[float], pit: bool, run: np.ndarray,
           pool: np.ndarray) -> np.ndarray:
    """分层量是 trailing ADV，分位点只在**入池成员**上取。

    在全体标的上取分位，会让「池子变小」把档位整体上移；而档位同时被分层回测
    与组合约束消费。`-1` 表示未入池，不是「第 0 档」。
    """
    t_len, n_col = adv.shape
    levels = len(edges) + 1
    tiers = np.full((t_len, n_col), -1, dtype="int8")

    if pit:
        for t in range(t_len):
            if not run[t]:
                continue
            ok = np.isfinite(adv[t]) & pool[t]
            if int(ok.sum()) < levels:
                continue
            cuts = np.quantile(adv[t][ok], edges)
            tiers[t, ok] = np.digitize(adv[t][ok], cuts, right=False).astype("int8")
        return tiers

    rows = np.flatnonzero(run)
    if rows.size == 0:
        return tiers
    sub = np.where(pool[rows], adv[rows], np.nan)
    count = np.isfinite(sub).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        pooled = np.where(count > 0, np.nansum(sub, axis=0) / np.maximum(count, 1), np.nan)
    ok = np.isfinite(pooled)
    if int(ok.sum()) < levels:
        return tiers
    cuts = np.quantile(pooled[ok], edges)
    flat = np.full(n_col, -1, dtype="int8")
    flat[ok] = np.digitize(pooled[ok], cuts, right=False).astype("int8")
    for t in rows:
        tiers[t] = np.where((flat >= 0) & pool[t], flat, -1)
    return tiers


def _topk(adv: np.ndarray, k: int) -> np.ndarray:
    """逐时点按报价成交额取 top-k（同额按列序稳定，保证可复现）。"""
    out = np.zeros(adv.shape, dtype=bool)
    for t in range(adv.shape[0]):
        row = adv[t]
        idx = np.flatnonzero(np.isfinite(row))
        if idx.size == 0:
            continue
        order = idx[np.argsort(-row[idx], kind="stable")]
        out[t, order[:k]] = True
    return out


# ---- 主流程 -------------------------------------------------------------
def build(call) -> dict:
    params = call.params
    pin = _pin(call)
    stream = _stream(call)
    times = stream.times
    t_len, n_col = stream.shape

    if bool(stream.end_closed) != bool(pin.end_closed):
        # 两个来源可以并存且相反：boundary 说未收盘（不可交易），align 说已收盘
        # （不丢弃）—— 结果是那根半成品 bar 留在面板里，而报告记 0 条。
        raise FlowError(
            E3003,
            "stream.end_closed 与 pin.end_closed 不一致",
            path="boundary.inputs",
            hint="末端闭合是供给版本的事实，只能有一个来源",
        )
    if times[0] < pin.coverage[0] or times[-1] > pin.coverage[1]:
        raise FlowError(
            E4002,
            "流的时间轴超出 pin.coverage",
            path="boundary.inputs.field_stream",
            hint="超出覆盖的时点没有供给版本可追溯，不得参与计算",
        )

    k = int(params["k"])
    if k > n_col:
        raise FlowError(
            E1004, f"k={k} 超过可选标的数 {n_col}", path="boundary.params.k",
            hint="top-k 选不出比池子更大的集合；这不是「少选几个」",
        )

    step = int(np.median(np.diff(times))) if t_len > 1 else 0
    tier_window = duration_bars(params["tier_window"], step_ns=step)
    edges = [float(e) for e in (params.get("tier_edges") or [])]
    if len(edges) > _MAX_TIER_LEVELS - 1:
        raise FlowError(
            E1004, f"tier_edges 最多 {_MAX_TIER_LEVELS - 1} 个分位点",
            path="boundary.params.tier_edges",
        )
    if edges and (any(not (0.0 < e < 1.0) for e in edges)
                  or edges != sorted(set(edges))):
        raise FlowError(
            E1004, f"tier_edges 必须是严格升序且落在 (0,1) 内，得到 {edges}",
            path="boundary.params.tier_edges",
        )
    if not edges:
        # G10：分位边界的缺省值在 registry/params.json 的 defaults 里，
        # 实现里不再抄第二份。缺省值不变，所以现有计划的边界逐位不变。
        edges = [float(e) for e in implementation_defaults("boundary")["tier_edges"]]

    min_coverage = float(params["min_coverage"])
    axis_resolved, run = _resolve_axis(dict(params["time_axis"]), times)

    close = stream.field("close")
    try:
        volume = stream.field("volume")
    except FlowError as exc:
        if exc.error.code != E1014:
            raise
        raise FlowError(
            E1004, "boundary 需要 volume 字段来构建流动性边界",
            path="boundary.inputs.field_stream",
            hint="没有成交额就算不出分层与 top-k；不得用等权代替",
        ) from None

    with np.errstate(invalid="ignore", divide="ignore"):
        dollar = np.where((close > 0) & (volume > 0), close * volume, np.nan)
    adv = trailing_mean(dollar, tier_window)

    alive = np.isfinite(close)
    has_any = alive.any(axis=0)
    first = np.where(has_any, np.argmax(alive, axis=0), t_len)
    last = np.where(has_any, t_len - 1 - np.argmax(alive[::-1], axis=0), -1)

    # 覆盖率门禁必须是**回看**的：用整个运行区间的覆盖率决定逐格可用性，
    # 等于让一根 bar 能不能用由它之后的缺口决定——一个只在后半段出现过的标的，
    # 它前半段本有效的格会被整列抹掉。这里改成 trailing 覆盖率，逐格判。
    thin = np.zeros((t_len, n_col), dtype=bool)
    if min_coverage > 0:
        cover = trailing_coverage(alive, tier_window)
        with np.errstate(invalid="ignore"):
            thin = run[:, None] & ~(cover >= min_coverage)
        if thin.any():
            adv = np.where(thin, np.nan, adv)

    liquid = _topk(adv, k)

    rows = np.arange(t_len)[:, None]
    codes = np.zeros((t_len, n_col), dtype="int8")

    def _mark(mask: np.ndarray, code: int) -> None:
        # 先命中先记：同一格不因为判据求值顺序不同而换码值
        codes[mask & (codes == 0)] = code

    _mark(np.broadcast_to((~run)[:, None], (t_len, n_col)), REASON_CODES["reserved"])
    _mark(np.broadcast_to(rows < first[None, :], (t_len, n_col)), REASON_CODES["not_listed"])
    _mark(np.broadcast_to(rows > last[None, :], (t_len, n_col)), REASON_CODES["delisted"])

    # 标的结构性原因排在数据质量之前：一个稳定币被标成「流动性不足」，
    # 读者会去查成交量；被标成「稳定币」才会去看标的集。
    special = np.array(
        [stream.symbol_classes.get(s) in ("stable", "leveraged") for s in stream.symbols],
        dtype=bool,
    )
    if special.any():
        _mark(np.broadcast_to(special[None, :], (t_len, n_col)),
              REASON_CODES["stable_or_leveraged"])

    inside = (rows >= first[None, :]) & (rows <= last[None, :])
    _mark(inside & ~alive, REASON_CODES["missing_data"])
    if thin.any():
        _mark(thin, REASON_CODES["missing_data"])

    # 未收盘 bar：可观测、不可交易。它永远是**供给版本末端**那一根，
    # 不是研究窗口末端——取后者会把一根已收盘的有效 bar 当成半成品丢掉，
    # 而真正的半成品落在窗口外被标成 reserved。
    if pin.end_closed is False and run[-1]:
        tail_mask = np.zeros((t_len, n_col), dtype=bool)
        tail_mask[-1, :] = True
        _mark(tail_mask, REASON_CODES["unclosed"])

    _mark(np.broadcast_to(run[:, None], (t_len, n_col)) & ~liquid,
          REASON_CODES["low_liquidity"])

    observable = np.isin(codes, list(OBSERVABLE_CODES))
    tradable = codes == 0
    tiers = _tiers(adv, edges, params["mask_policy"] == "pit_dynamic", run, liquid)

    scope = Scope(
        times=times,
        symbols=stream.symbols,
        observable_mask=observable,
        tradable_mask=tradable,
        tiers=tiers,
        reason_codes=codes,
        axis_resolved=axis_resolved,
        reason_code_legend=dict(REASON_CODES),
    )
    return {
        "observable_mask": scope.observable_mask,
        "tradable_mask": scope.tradable_mask,
        "tiers": scope.tiers,
        "reason_codes": scope.reason_codes,
        "reason_code_legend": dict(REASON_CODES),
        "axis_resolved": axis_resolved,
        "scope": scope,
    }
