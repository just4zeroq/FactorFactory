"""`label` · 标签生成：跨度 / 变换 / 中性化（s0/04 §3.4，06 §4 / §7）。

这个工位产出的**不是因子**，是「未来会怎样」的度量。三条边界：

1. **未来值的算法只写在这里**。`forward_return` 用 `t+h` 的价格算标签，这是合法的；
   把同一段未来价格拿去算分数才是前视。所以本模块的输出永远只进 `observe`
   的相关性计算，不进 `evaluate` 的表达式。Evidence 的 IC 衰减曲线需要其他跨度
   的标签时，也调本模块的 `build_raw_by_h`（同一个 `forward_raw`），**不另写一份**
   ——两套实现必然分叉，而分叉的两边数值都"看起来合理"，没有检查点会报错。
2. **变换只用当根 bar 的截面**。`cs_zscore` 的均值与标准差来自该时点，不是全期。
   「全期标准化」是最隐蔽的一类前视，因为它的结果看起来更"稳"。
3. **宽度不足不是零信号**。`min_width` 不满足时该时点标记不可用（`valid_mask=False`），
   由下游统一消费；丢弃占比超 20%（I61）直接 `E3003`，不得「丢一点继续跑」。

口径只有一个来源：`caliber`。`params` 里的 `horizon` / `rebalance` / `entry` /
`transform` / `neutralise` / `overlap_policy` / `min_width` / `width_policy` 是编译期
从 `caliber` 展开的快照，运行期与 `policy_view` 逐项比对，任何一项不等即 `E1013`
（C9：判据口径与回测口径不一致）。`neutralise_window` 不在 `params` 里——它只从
`caliber.label` 读，`params` 出现同名键会被 `E1003` 挡在更前面。

输出形状与 `s0/04 §3.4` 的 `Label` 一致，另带数据通道（`score` / `valid_mask` /
`width_series`）：`ref` 是身份，数据随调用单传递，能力层不落盘。
"""

from __future__ import annotations

import numpy as np

from v5.capability.panel import Panel, array_digest
from v5.capability.params import duration_bars
from v5.capability.stats import (
    TRANSFORMS, cs_neutralise, forward_return, nanmean, rolling_beta,
)
from v5.contracts.errors import E1004, E1008, E1013, E1022, E3003, FlowError

MAX_DROP_RATIO = 0.20  # I61 / A27

# `params` 里的口径镜像键与 `caliber` 的对应关系（block, caliber_key, params_key）。
_CALIBER_MIRRORS = (
    ("execution", "entry", "entry"),
    ("execution", "rebalance", "rebalance"),
    ("label", "horizon", "horizon"),
    ("label", "transform", "transform"),
    ("label", "neutralise", "neutralise"),
    ("label", "overlap_policy", "overlap_policy"),
    ("label", "min_width", "min_width"),
    ("label", "width_policy", "width_policy"),
)


def _panel(call) -> Panel:
    for item in call.inputs:
        if item is not None and item.kind == "panel" and isinstance(item.data, Panel):
            return item.data
    raise FlowError(
        E3003, "label 没有拿到面板数组", path="label.inputs.panel",
        hint="执行器解析 Panel 后放进 inputs[i].data；能力层不自己读盘",
    )


def _policy_block(call, block: str) -> dict:
    view = call.policy_view or {}
    value = view.get(block) or {}
    if not isinstance(value, dict):
        raise FlowError(
            E1013, f"policy_view.{block} 必须是映射", path=f"policy_view.{block}",
        )
    return dict(value)


def _check_caliber(params: dict, policy_view: dict) -> None:
    """`params` 是 `caliber` 的镜像，镜像与本体不一致即失败。

    两边都保留是有意的：`params` 进 `call_digest`（一次调用的完整身份），
    `policy_view` 是口径的唯一权威。谁覆盖谁都会留下一个可以静默分叉的来源——
    所以两者不一致时不是「以后者为准」，而是停下来。
    """
    for block, key, pkey in _CALIBER_MIRRORS:
        authority = (policy_view.get(block) or {}).get(key)
        if authority is None or pkey not in params:
            continue
        got = params[pkey]
        if key == "neutralise":
            same = [str(x) for x in (got or [])] == [str(x) for x in (authority or [])]
        elif isinstance(authority, float) or isinstance(got, float):
            same = float(got) == float(authority)
        else:
            same = got == authority
        if not same:
            raise FlowError(
                E1013,
                f"{pkey}={got!r} 与 caliber.{block}.{key}={authority!r} 不一致",
                path=f"caliber.{block}.{key}",
                hint="判据口径与回测口径必须是同一个值；两个来源分叉时停下，不以后者为准",
            )


def _entry_offset(entry: str) -> int:
    if entry == "close":
        return 0
    if entry in ("next_close", "next_open"):
        return 1
    raise FlowError(E1004, f"未知 entry={entry!r}", path="label.params.entry")


def _regressors(panel: Panel, neutralise: list[str],
                label_block: dict) -> list[np.ndarray]:
    """中性化回归量（`beta` / `log_dollar_volume`）。

    单独抽出来的理由只有一个：IC 衰减（`build_raw_by_h`）必须与主标签用**同
    一套**回归量。抄一份到别处，改了一处另一处还停在旧语义，而两边数值都
    "看起来合理"，没有任何检查点会报错。
    """
    out: list[np.ndarray] = []
    if "beta" in neutralise:
        close = panel.field("close")
        with np.errstate(invalid="ignore", divide="ignore"):
            returns = np.full(close.shape, np.nan)
            returns[1:] = close[1:] / close[:-1] - 1.0
        returns[~np.isfinite(returns)] = np.nan
        market = np.array([nanmean(returns[t]) for t in range(returns.shape[0])])
        window = int(label_block.get("neutralise_window") or 0)
        if window < 2:
            raise FlowError(
                E1008,
                "neutralise 含 beta 但 caliber.label.neutralise_window 未给出",
                path="caliber.label.neutralise_window",
                hint="回归窗口是口径字段（06 §4），不属于能力参数；X5 编译期已挡一次",
            )
        out.append(rolling_beta(returns, market, window))
    if "log_dollar_volume" in neutralise:
        close = panel.field("close")
        volume = panel.field("volume")
        with np.errstate(invalid="ignore", divide="ignore"):
            dv = close * volume
            dv = np.where(dv > 0, dv, np.nan)
            ldv = np.log(dv)
        ldv[~np.isfinite(ldv)] = np.nan
        out.append(ldv)
    return out


def forward_raw(panel: Panel, *, entry: str, horizon: int,
                neutralise: list[str], label_block: dict) -> np.ndarray:
    """单跨度的标签收益：`forward_return` + 中性化残差，不做抽样/宽度处理。

    **这是「未来值怎么算」的唯一实现**。`build` 用它产主标签，`build_raw_by_h`
    用它产 IC 衰减曲线——两处调用同一个函数、同一份 `caliber` 镜像，所以
    "衰减曲线在口径跨度那一档"与主标签逐位相同（有测试钉住）。
    """
    offset = _entry_offset(entry)
    price = panel.field("open" if entry == "next_open" else "close")
    raw = forward_return(price, horizon, entry_offset=offset)
    if neutralise:
        raw = cs_neutralise(raw, _regressors(panel, neutralise, label_block))
    return raw


def _holdout_touch(holdout: np.ndarray, reach: int) -> np.ndarray:
    """窗口 `[t+1, t+reach]` 是否触及保留区。

    这不是"多排几行"的洁癖：`t` 的标签用 `t + offset + horizon` 的价格，那一根
    落在保留区里时，标签就读过了保留区的数据。只把 holdout 那几行排除，等于
    允许「贴着保留区边缘」的标签把保留区的价带进来。
    """
    n_rows = holdout.size
    cum = np.concatenate([[0], np.cumsum(holdout.astype("int64"))])
    idx = np.arange(n_rows)
    lo = np.minimum(idx + 1, n_rows)
    hi = np.minimum(idx + reach + 1, n_rows)
    return (cum[hi] - cum[lo]) > 0


def build_raw_by_h(panel: Panel, policy_view: dict,
                   horizons: list[int]) -> dict[str, np.ndarray]:
    """多跨度标签收益，供 Evidence 的 IC 衰减曲线（`ic_decay`）使用。

    三处刻意的取舍：

    1. **口径全部来自 `policy_view`**，不从 observe 自己的参数推。IC 衰减是
       "同一个标签在不同持有期上的排序能力"，换一套 entry/中性化就不是同一条
       曲线了。
    2. **不做重叠抽样**（`independent_sample` 的等间隔抽取）。衰减要在**同一批
       时点**上比较不同跨度，抽样步长随 h 变会让每条曲线的样本都不一样，
       曲线之间的落差就分不清是持有期效应还是抽样噪声。
    3. **不做宽度掩码**（`min_width` / `width_policy`）。那是"哪几根 bar 值得
       拿去交易"的口径规则；衰减问的是信号自身的衰减速度。代价是：口径里开了
       `width_policy=drop` 时，"h = 口径跨度"那一档与本工位主标签的 `raw`
       不再逐位相同（主标签多一层宽度丢弃）——这一点写在测试的 precondition 里。

    每个跨度按**自己的** `reach = offset + h` 重算保留区触达掩码：h=20 的标签比
    h=1 的标签多读 19 根 bar，用 h=1 的掩码会放过一批伸进保留区的标签。
    """
    label_block = dict((policy_view or {}).get("label") or {})
    exec_block = dict((policy_view or {}).get("execution") or {})
    entry = str(exec_block.get("entry") or "next_close")
    neutralise = [str(x) for x in (label_block.get("neutralise") or [])]
    holdout = panel.holdout_mask()
    offset = _entry_offset(entry)
    out: dict[str, np.ndarray] = {}
    for value in horizons:
        horizon = int(value)
        raw = forward_raw(panel, entry=entry, horizon=horizon,
                          neutralise=neutralise, label_block=label_block)
        touch = _holdout_touch(holdout, offset + horizon)
        # holdout 是逐时点（T,）掩码，raw 是 (T,N)：按列广播，别拿 (T,) 去与
        # (T,N) 逐元素相与（形状对不上时会抛错，但对不上的一半情况下 numpy
        # 会把 (T,) 当成行广播，静默错位——这里显式补轴，不留歧义）。
        usable = np.isfinite(raw) & ~holdout[:, None] & ~touch[:, None]
        arr = np.where(usable, raw, np.nan)
        arr.setflags(write=False)
        out[str(horizon)] = arr
    return out


def build(call) -> dict:
    params = call.params
    panel = _panel(call)
    policy_view = {"execution": _policy_block(call, "execution"),
                   "label": _policy_block(call, "label")}
    _check_caliber(dict(params), policy_view)

    step = int(np.median(np.diff(panel.times))) if panel.shape[0] > 1 else 0
    horizon = duration_bars(params["horizon"], step_ns=step)
    rebalance = duration_bars(params["rebalance"], step_ns=step)
    entry = str(params["entry"])
    transform = str(params["transform"])
    neutralise = list(params.get("neutralise") or [])
    min_width = params.get("min_width")
    width_policy = params.get("width_policy", "keep")
    overlap_policy = params.get("overlap_policy")

    # 重叠标签不能当独立观测（06 §7）：`rebalance < horizon` 必须显式声明处理方式。
    # 编译期 X4 已挡一次；这里再挡一次，因为 `--set` 与直调是两个入口。
    if rebalance < horizon and not overlap_policy:
        raise FlowError(
            E1022,
            f"rebalance={rebalance} < horizon={horizon} 且未声明 overlap_policy",
            path="label.params.overlap_policy",
            hint="相邻标签共享未来价格，朴素标准误会系统性低估",
        )

    offset = _entry_offset(entry)
    raw = forward_raw(panel, entry=entry, horizon=horizon,
                      neutralise=neutralise, label_block=policy_view["label"])

    label_digest = array_digest(raw)
    score = TRANSFORMS[transform](raw)
    score[~np.isfinite(score)] = np.nan

    n_rows = panel.shape[0]
    width = np.isfinite(score).sum(axis=1).astype("int64")

    # 宽度掩码：只由 `min_width` / `width_policy` 决定（06 §4.1）。
    width_ok = np.ones(n_rows, dtype=bool)
    if min_width is not None and width_policy == "drop":
        width_ok = width >= int(min_width)

    # I61 的 20% 只对「本可以算出标签、却因宽度不足被丢」的时点生效。
    # 尾部 horizon+offset 行与保留区都属于结构性不可用：把它们算进分母，
    # 一条 20 根、horizon=6 的正常面板会无端 E3003。
    holdout = panel.holdout_mask()
    window_rows = max(0, n_rows - offset - horizon)
    window_ok = np.zeros(n_rows, dtype=bool)
    window_ok[:window_rows] = True

    # 窗口**触及**保留区同样是泄漏：`t` 的标签用 `t + offset + horizon` 的价格，
    # 那一根落在保留区里时，标签就读过了保留区的数据。只把 holdout 那几行排除，
    # 等于允许「贴着保留区边缘」的标签把保留区的价带进来。
    touch = _holdout_touch(holdout, offset + horizon)

    judgeable = window_ok & ~holdout & ~touch
    width_dropped = int(np.sum(judgeable & ~width_ok))
    if int(judgeable.sum()) and width_dropped / int(judgeable.sum()) > MAX_DROP_RATIO:
        raise FlowError(
            E3003,
            f"宽度不足丢弃时点 {width_dropped}/{int(judgeable.sum())} "
            f"超过 {MAX_DROP_RATIO:.0%}（I61）",
            path="label.valid_mask",
            hint="丢弃过多说明宽度口径与数据不匹配；不得降级为成功",
        )

    # 可消费掩码 = 宽度达标 ∩ 标签有限 ∩ 非保留区 ∩ 重叠抽样选中。
    usable = width_ok & np.isfinite(score).any(axis=1) & ~holdout & ~touch
    if overlap_policy == "independent_sample":
        # 只用不重叠的时点：按 horizon 等间隔抽样（有效样本降到约 1/h）
        sample = np.zeros(n_rows, dtype=bool)
        picked = np.flatnonzero(usable)
        sample[picked[::horizon]] = True
    else:
        # `nw_adjusted` 不改样本：重叠的标签全部保留，调整责任在 Evidence
        # （截断阶数属于判据口径，标签层无权替它决定）。这里只把用的策略回写，
        # 让下游知道该按哪种方式算标准误——否则「33 个独立观测」是凭空来的。
        sample = usable
    effective_n = int(sample.sum())

    if effective_n < 2:
        raise FlowError(
            E3003, f"标签有效样本 {effective_n} < 2，无法评估", path="label.valid_mask",
        )

    score = np.where(sample[:, None], score, np.nan)
    score.setflags(write=False)
    # `raw`：中性化后、**变换前**的标签收益（与 score 同形、同 sample 掩码）。
    # observe 的多空序列必须用它——`cs_rank`/`cs_zscore` 之后单位是秩/标准差，
    # 拿它当组合收益去减成本是量纲混拼（06 §5.6）。变换后的 score 只进 IC。
    raw_out = np.where(sample[:, None], raw, np.nan)
    raw_out = np.ascontiguousarray(raw_out, dtype="float64")
    raw_out.setflags(write=False)
    valid_mask = sample.copy()
    valid_mask.setflags(write=False)

    # 四类互斥：窗口越界 / 落在保留区 / 窗口触及保留区 / 宽度不足。
    not_estimable = int(np.sum(~window_ok & ~holdout))
    holdout_bars = int(np.sum(holdout))
    holdout_leak = int(np.sum(touch & ~holdout & window_ok))
    dropped_bars = {
        "count": width_dropped + not_estimable + holdout_bars + holdout_leak,
        "by_reason": {
            "width_below_min": width_dropped,
            "not_estimable": not_estimable,
            "holdout": holdout_bars,
            "holdout_leak": holdout_leak,
        },
    }

    return {
        "label_ref": label_digest,
        "score_ref": array_digest(score),
        "score": score,
        "raw": raw_out,
        "valid_mask": valid_mask,
        "width_series": width,
        "dropped_bars": dropped_bars,
        "overlap_policy_used": str(overlap_policy or "none"),
        "effective_n": effective_n,
        # 跨度是标签自身的属性，不是口径参数：下游要把「每 h bar 收益」与
        # 「每 bar 成本」换算到同一时间单位，缺了它只能猜（猜 1 就是乐观 h 倍）。
        "horizon_bars": int(horizon),
    }
