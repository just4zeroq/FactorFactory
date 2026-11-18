"""能力实现 · allocate：持仓构造：分位/约束两条路径（自研算法之三）。

这是全系统**唯一**的持仓构造函数（06-evaluation.md §5）。三条不可动摇的边界：

1. **口径只有一个来源**：`quantiles` / `long_short` / `weighting` / `max_weight` /
   `net_exposure` / `turnover_max` / `min_members` 全部从 `policy_view.allocate`
   读。`params` 里只允许构造选择 `top_k`——把口径字段写进 `params` 会在
   `run` 的第 2 步被 `E1003` 挡下，本模块不必再写第二份判断。
2. **权重 t 只依赖 t 及以前**：分位/排名用 `score[t]` 的截面；`inv_vol` 用
   价格收益 `[0..t]` 的滚动波动（Welford 增量，绝不使用未来 bar）；换手约束
   只看 `w[t-1]`。任何一行都不会读 `t+1`。
3. **宽度不足不"有多少算多少"**：可交易成员 `< min_members` 的时点整行置 0
   并记入 `dropped_bars`，而不是用少数标的拼出一个看起来很便宜的组合。

权重的约束投影顺序（逐时点）：先按目标净敞口拆成多/空两组并按 weighting
分配，再按 `max_weight` 做 capped water-filling（等价"renorm→clip→renorm"），
最后按 `turnover_max` 缩放相对上一时点的增量。三个约束只要真正触发就记入
`binding_constraints`，不做静默降级。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from capability.panel import array_digest
from capability.stats import rebalance_freeze
from contracts.errors import E1004, E1008, E1013, E3001, E3003, FlowError

_WEIGHTINGS = frozenset({"equal", "liquidity", "inv_vol"})
_EPS = 1e-12


# ---------------------------------------------------------------- 口径校验
def _caliber(call) -> dict:
    view = call.policy_view or {}
    block = view.get("allocate")
    if block is None:
        raise FlowError(
            E1008, "policy_view 缺 allocate 子块", path="policy_view.allocate",
            hint="构造口径来自 caliber.allocate；能力不自选口径",
        )
    if not isinstance(block, Mapping):
        raise FlowError(
            E1004, "policy_view.allocate 必须是映射", path="policy_view.allocate",
        )
    return dict(block)


def _require(cal: Mapping[str, Any], key: str) -> Any:
    if key not in cal:
        raise FlowError(
            E1008, f"caliber.allocate 缺必填口径 {key}",
            path=f"policy_view.allocate.{key}",
        )
    return cal[key]


def _int_field(cal: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = _require(cal, key)
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise FlowError(
            E1004, f"caliber.allocate.{key} 必须是整数，得到 {type(value).__name__}",
            path=f"policy_view.allocate.{key}",
        )
    out = int(value)
    if out < minimum:
        raise FlowError(
            E1004, f"caliber.allocate.{key}={out} < {minimum}",
            path=f"policy_view.allocate.{key}",
        )
    return out


def _float_field(cal: Mapping[str, Any], key: str, *, gt: float | None = None,
                 ge: float | None = None, le: float | None = None) -> float:
    value = _require(cal, key)
    if isinstance(value, (bool, np.bool_)) or not isinstance(
            value, (int, float, np.integer, np.floating)):
        raise FlowError(
            E1004, f"caliber.allocate.{key} 必须是实数，得到 {type(value).__name__}",
            path=f"policy_view.allocate.{key}",
        )
    out = float(value)
    if not math.isfinite(out):
        raise FlowError(
            E1004, f"caliber.allocate.{key} 必须是有限值，得到 {value!r}",
            path=f"policy_view.allocate.{key}",
        )
    if gt is not None and not out > gt:
        raise FlowError(E1004, f"caliber.allocate.{key}={out} 必须 > {gt}",
                        path=f"policy_view.allocate.{key}")
    if ge is not None and not out >= ge:
        raise FlowError(E1004, f"caliber.allocate.{key}={out} 必须 >= {ge}",
                        path=f"policy_view.allocate.{key}")
    if le is not None and not out <= le:
        raise FlowError(E1004, f"caliber.allocate.{key}={out} 必须 <= {le}",
                        path=f"policy_view.allocate.{key}")
    return out


def _bool_field(cal: Mapping[str, Any], key: str) -> bool:
    value = _require(cal, key)
    if not isinstance(value, (bool, np.bool_)):
        raise FlowError(
            E1004, f"caliber.allocate.{key} 必须是 bool，得到 {value!r}",
            path=f"policy_view.allocate.{key}",
        )
    return bool(value)


def _enum_field(cal: Mapping[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = _require(cal, key)
    if not isinstance(value, str) or value not in allowed:
        raise FlowError(
            E1004, f"caliber.allocate.{key}={value!r} 不在白名单 {sorted(allowed)}",
            path=f"policy_view.allocate.{key}",
        )
    return value


# ---------------------------------------------------------------- 输入
def _matrix(data: Any, name: str, kind: str) -> np.ndarray:
    if not isinstance(data, np.ndarray) or data.ndim != 2:
        raise FlowError(
            E3003, f"allocate 输入 {name} 必须是二维 ndarray",
            path=f"allocate.inputs.{name}",
        )
    if kind == "bool":
        if data.dtype.kind != "b":
            raise FlowError(
                E3003, f"allocate 输入 {name} 必须是 bool 数组，得到 {data.dtype}",
                path=f"allocate.inputs.{name}",
            )
        return np.asarray(data, dtype=bool)
    if kind == "int":
        if data.dtype.kind not in "iu":
            raise FlowError(
                E3003, f"allocate 输入 {name} 必须是整数数组，得到 {data.dtype}",
                path=f"allocate.inputs.{name}",
            )
        return np.asarray(data, dtype="int64")
    out = np.asarray(data, dtype="float64")
    if np.isinf(out).any():
        raise FlowError(
            E3003, f"allocate 输入 {name} 含 Infinity",
            path=f"allocate.inputs.{name}",
            hint="缺失用 NaN，不用 inf",
        )
    return out


def _inputs(call) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    got: dict[str, Any] = {}
    for item in call.inputs:
        if item is None:
            continue
        if item.kind in got:
            raise FlowError(
                E3001, f"allocate 重复输入 {item.kind!r}", path=f"allocate.inputs.{item.kind}",
            )
        got[item.kind] = item.data
    for kind in ("score", "tradable_mask", "tiers"):
        if kind not in got:
            raise FlowError(
                E3001, f"allocate 缺输入 {kind!r}", path=f"allocate.inputs.{kind}",
            )
    score = _matrix(got["score"], "score", "float")
    tradable = _matrix(got["tradable_mask"], "tradable_mask", "bool")
    tiers = _matrix(got["tiers"], "tiers", "int")
    if score.shape != tradable.shape or score.shape != tiers.shape:
        raise FlowError(
            E3001, "allocate 三路输入形状必须一致", path="allocate.inputs",
        )
    # 价格是**可选**输入：只有 `weighting=inv_vol` 需要它（按资产收益波动加权）。
    # 缺它时不在能力里猜（拿分数序列的波动冒充资产波动会产出含义完全不同的
    # 权重，且看起来一样"正常"），由 build 按口径要求 fail-closed。
    price = None
    if got.get("price") is not None:
        price = _matrix(got["price"], "price", "float")
        if price.shape != score.shape:
            raise FlowError(
                E3001, "allocate 的 price 与 score 形状必须一致", path="allocate.inputs",
            )
        if not np.isfinite(price).all() or np.any(price <= 0.0):
            raise FlowError(
                E3003, "allocate 的 price 必须是有限正数", path="allocate.inputs.price",
            )
    return score, tradable, tiers, price


# ---------------------------------------------------------------- 权重分配
def _waterfill(raw: np.ndarray, cap: float, budget: float) -> tuple[np.ndarray, bool]:
    """按 `raw` 比例把 `budget` 分给若干成员，每个不超过 `cap`。

    等价于「先归一化 → 再 clip → 把释放的预算在未触顶者间重新分配」的迭代。
    触顶即记 `capped=True`（`binding_constraints` 据此记 `max_weight`）。
    """
    n = int(raw.size)
    w = np.zeros(n, dtype="float64")
    active = np.ones(n, dtype=bool)
    capped = False
    for _ in range(n + 1):
        if budget <= _EPS or not active.any():
            break
        idx = np.flatnonzero(active)
        r = np.asarray(raw[idx], dtype="float64")
        r = np.where(np.isfinite(r) & (r > 0), r, 0.0)
        total = float(r.sum())
        if total > _EPS:
            cand = r / total * budget
        else:
            cand = np.full(idx.size, budget / idx.size, dtype="float64")
        hit = cand >= cap - _EPS
        if hit.any():
            w[idx[hit]] = cap
            budget -= cap * int(hit.sum())
            active[idx[hit]] = False
            capped = True
        else:
            w[idx] = cand
            break
    return w, capped


def _inv_vol_history(price: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """滚动**资产收益**波动倒数与历史计数：只用 `[0..t]`（含当根，不含未来）。

    波动必须是价格的波动，不是分数序列的波动：`weighting=inv_vol` 的经济含义
    是「压低高波动标的的支配权」，拿因子暴露的截面离散度当波动会得到一个名字
    相同、含义全异的权重。收益 `r_t = p_t / p_{t-1} - 1` 在 t 时刻已知，用它
    算出的权重作用于 t+1，仍然是 PIT。
    """
    t_len, n_sym = price.shape
    iv = np.zeros((t_len, n_sym), dtype="float64")
    count = np.zeros((t_len, n_sym), dtype="int64")
    for j in range(n_sym):
        mean = 0.0
        m2 = 0.0
        seen = 0
        prev = None
        for t in range(t_len):
            p = float(price[t, j])
            if prev is not None and prev > 0.0 and p > 0.0:
                x = p / prev - 1.0
                seen += 1
                delta = x - mean
                mean += delta / seen
                m2 += delta * (x - mean)
                if seen >= 2:
                    var = m2 / (seen - 1)
                    sd = math.sqrt(var) if var > 0 else 0.0
                    iv[t, j] = 1.0 / sd if sd > 0 else 1.0
            count[t, j] = seen
            prev = p
    return iv, count


def _l2_regularisation() -> float:
    """求解器去退化正则系数（G14）。缺省值唯一来源是实现缺省注册表。"""
    from capability.params import implementation_defaults

    value = float(implementation_defaults("allocate")["l2_regularisation"])
    if not np.isfinite(value) or value < 0:
        raise FlowError(E1004, "allocate 的 l2_regularisation 必须是非负有限值",
                        path="params.json.defaults.allocate.l2_regularisation")
    return value


def _build_constrained(call, cal: Mapping[str, Any]) -> dict:
    """约束投影路径：cvxpy 一次 canonicalize，逐 bar 只换 `cp.Parameter`。

    触发条件是 `caliber.allocate.mode == "constraint"`（或出现 `constraints`
    块）。当前 plan_schema 尚未暴露这两个键，因此 CLI 仍只走分位路径；这里先
    把实现与回归守卫落地，待契约补键即可启用。

    目标函数里的 `1e-3 * ||w||^2` 只是求解器去退化的数值正则，不是经济偏好：
    `mu` 已做截面去均值，净敞口与 gross 由硬约束给定。
    """
    import cvxpy as cp

    long_short = _bool_field(cal, "long_short")
    max_weight = _float_field(cal, "max_weight", gt=0.0, le=1.0)
    net_exposure = _float_field(cal, "net_exposure", ge=-1.0, le=1.0)
    turnover_max = _float_field(cal, "turnover_max", gt=0.0)
    min_members = _int_field(cal, "min_members", minimum=2)
    if max_weight * min_members > 1.0 + _EPS:
        raise FlowError(E1013, "max_weight × min_members > 1，约束不可同时满足",
                        path="policy_view.allocate")
    if not long_short:
        net_exposure = 1.0

    score, tradable, tiers, price = _inputs(call)
    t_len, n_sym = score.shape
    w = cp.Variable(n_sym)
    mu = cp.Parameter(n_sym)
    prev = cp.Parameter(n_sym)
    upper = cp.Parameter(n_sym, nonneg=True)
    lower = cp.Parameter(n_sym, nonpos=True)
    net = cp.Parameter()
    turn = cp.Parameter(nonneg=True)
    problem = cp.Problem(
        # G14：去退化正则的系数在 registry/params.json 的 defaults 里。
        cp.Maximize(mu @ w - _l2_regularisation() * cp.sum_squares(w)),
        [w <= upper, w >= lower, cp.sum(w) == net,
         cp.norm1(w) <= 1.0, cp.norm1(w - prev) <= 2.0 * turn],
    )

    weights = np.zeros((t_len, n_sym), dtype="float64")
    dropped_row = np.zeros(t_len, dtype=bool)
    binding: set[str] = set()
    prev_w = np.zeros(n_sym, dtype="float64")
    rebalance = _rebalance_period(call)
    for t in range(t_len):
        # 再平衡网格（06 §5.6）：非网格行不求解，持仓沿用（`prev_w`）。
        if rebalance > 1 and t % rebalance:
            weights[t] = prev_w
            continue
        eligible = tradable[t] & (tiers[t] >= 0) & np.isfinite(score[t])
        count = int(eligible.sum())
        if count < min_members:
            dropped_row[t] = True
            binding.add("min_members")
            prev_w = np.zeros(n_sym, dtype="float64")
            continue
        valid = np.where(eligible, score[t], np.nan)
        center = float(np.nanmean(valid))
        mu.value = np.nan_to_num(valid - center, nan=0.0)
        upper.value = np.where(eligible, max_weight, 0.0)
        lower.value = np.where(eligible & long_short, -max_weight, 0.0)
        prev.value = prev_w
        net.value = net_exposure
        turn.value = turnover_max
        problem.solve(solver="CLARABEL", warm_start=True, verbose=False)
        if w.value is None or problem.status not in {
                cp.OPTIMAL, cp.OPTIMAL_INACCURATE}:
            raise FlowError(E3003, f"allocate 约束投影失败: {problem.status}",
                            path="allocate.outputs.weights")
        solved = np.nan_to_num(np.asarray(w.value, dtype="float64"), nan=0.0)
        solved[np.abs(solved) < _EPS] = 0.0
        weights[t] = solved
        if np.any(np.abs(solved) >= max_weight - _EPS):
            binding.add("max_weight")
        if abs(float(solved.sum()) - net_exposure) < _EPS:
            binding.add("net_exposure")
        if abs(float(np.abs(solved).sum()) - 1.0) < _EPS:
            binding.add("gross")
        if abs(0.5 * float(np.abs(solved - prev_w).sum()) - turnover_max) < _EPS:
            binding.add("turnover_max")
        prev_w = solved

    prevs = np.vstack([np.zeros((1, n_sym), dtype="float64"), weights[:-1]])
    turnover_series = 0.5 * np.abs(weights - prevs).sum(axis=1)
    effective = min((int(np.count_nonzero(weights[t])) for t in range(t_len)), default=0)
    weights = np.ascontiguousarray(weights, dtype="float64")
    weights.setflags(write=False)
    turnover_series = np.ascontiguousarray(turnover_series, dtype="float64")
    turnover_series.setflags(write=False)
    n_drop = int(dropped_row.sum())
    return {
        "weights_ref": array_digest(weights),
        "weights": weights,
        "turnover_series": turnover_series,
        "effective_members": int(effective),
        "binding_constraints": sorted(binding),
        "dropped_bars": {
            # `by_reason` 三类互斥且**之和等于 count**（14-test-strategy 的验收线）。
            # 不能拿别名重复计数：`min_members` 与 `insufficient_members` 同值相加
            # 会让「丢弃了多少根」凭空翻倍。
            "count": n_drop,
            "by_reason": {
                "insufficient_members": n_drop,
                "no_tradable": 0,
                "width_below_min": 0,
            },
            # 回退行不是丢弃行（该时点仍有持仓），单独记，不混进 by_reason。
            "fallback": {"inv_vol_history_short": 0},
        },
    }


def _rebalance_period(call) -> int:
    """`caliber.execution.rebalance`：再平衡周期（06 §5.6），缺省 1 = 逐 bar 重算。

    它是 `execution` 子块的口径字段，allocate 通过 `policy_reads` 里的
    `execution` 读取；缺块不报错——直接调用的测试与旧调用方没有这个块，
    而计划编译层保证真实 run 必有。给了但非法必须停：静默当 1 会把
    「5 根 bar 持有」偷换成「每 bar 换仓」，成本差一个数量级。
    """
    block = (getattr(call, "policy_view", None) or {}).get("execution") or {}
    raw_value = block.get("rebalance")
    if raw_value is None:
        return 1
    try:
        period = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise FlowError(
            E1013, f"caliber.execution.rebalance 非法: {raw_value!r}",
            path="caliber.execution.rebalance",
        ) from exc
    if period < 1:
        raise FlowError(
            E1013, f"caliber.execution.rebalance={period} 必须 >=1",
            path="caliber.execution.rebalance",
        )
    return period


def build(call) -> dict:
    cal = _caliber(call)
    mode = str(cal.get("mode", "constraint" if "constraints" in cal else "quantile"))
    if mode == "constraint":
        return _build_constrained(call, cal)
    if mode != "quantile":
        raise FlowError(E1004, f"caliber.allocate.mode={mode!r} 不在白名单",
                        path="policy_view.allocate.mode")
    quantiles = _int_field(cal, "quantiles", minimum=2)
    long_short = _bool_field(cal, "long_short")
    weighting = _enum_field(cal, "weighting", _WEIGHTINGS)
    max_weight = _float_field(cal, "max_weight", gt=0.0, le=1.0)
    net_exposure = _float_field(cal, "net_exposure", ge=-1.0, le=1.0)
    turnover_max = _float_field(cal, "turnover_max", gt=0.0)
    min_members = _int_field(cal, "min_members", minimum=2)
    if max_weight * min_members > 1.0 + _EPS:
        # X3：上限 × 最小成员数 > 1 时约束互相矛盾（经济口径不自洽）。
        raise FlowError(
            E1013,
            f"max_weight={max_weight} × min_members={min_members} > 1，约束不可同时满足",
            path="policy_view.allocate",
        )
    # 纯多时净敞口即满仓；杠杆/保证金属于 net 子块，本能力不读、也不检查。
    if not long_short:
        net_exposure = 1.0

    top_k = call.params.get("top_k")  # 类型/范围已由 validate_params 保证
    if top_k is not None:
        top_k = int(top_k)

    score, tradable, tiers, price = _inputs(call)
    t_len, n_sym = score.shape

    if weighting == "inv_vol":
        if price is None:
            raise FlowError(
                E3003, "weighting=inv_vol 需要 price 输入（按资产收益波动加权）",
                path="allocate.inputs.price",
                hint="能力不拿分数序列的波动冒充资产波动；调用方须传 price",
            )
        iv, hist_count = _inv_vol_history(price)
    else:
        iv, hist_count = None, None

    raw_weights = np.zeros((t_len, n_sym), dtype="float64")
    dropped_row = np.zeros(t_len, dtype=bool)
    n_min_members = 0
    n_iv_fallback = 0
    binding: set[str] = set()

    long_target = (1.0 + net_exposure) / 2.0 if long_short else 1.0
    short_target = (1.0 - net_exposure) / 2.0 if long_short else 0.0

    rebalance = _rebalance_period(call)
    for t in range(t_len):
        # 再平衡网格（06 §5.6）：非网格行不重算目标，持仓沿用最近一个成功
        # 网格行（rebalance_freeze 统一回填）。`rebalance=1` 时每行都是网格行。
        if rebalance > 1 and t % rebalance:
            continue
        eligible = tradable[t] & (tiers[t] >= 0) & np.isfinite(score[t])
        idx = np.flatnonzero(eligible)
        if idx.size < min_members:
            dropped_row[t] = True
            n_min_members += 1
            binding.add("min_members")
            continue

        order = idx[np.argsort(score[t, idx], kind="stable")]
        if top_k is None:
            # 分位组数固定为 quantiles，不随 N(t) 浮动；空组不参与选边。
            parts = [p for p in np.array_split(order, quantiles) if p.size > 0]
            long_idx = parts[-1]
            short_idx = parts[0] if long_short and len(parts) >= 2 else np.empty(0, dtype=int)
        else:
            long_idx = order[-top_k:]
            bottom = order[:top_k]
            # 可交易成员不足以支撑 2k 个不重叠名额时，k 不变、只是空位更少：
            # 先给多头的名次让位，避免同一资产既多又空。
            short_idx = bottom[~np.isin(bottom, long_idx)] if long_short else np.empty(0, dtype=int)

        def _raw(members: np.ndarray) -> np.ndarray:
            if members.size == 0:
                return np.empty(0, dtype="float64")
            if weighting == "equal":
                return np.ones(members.size, dtype="float64")
            if weighting == "liquidity":
                # 档位越高权重越大；+1 保证 0 档也有正权重。
                return (tiers[t, members].astype("float64") + 1.0)
            v = np.asarray(iv[t, members], dtype="float64")
            v = np.where(np.isfinite(v) & (v > 0), v, 1.0)
            return v

        raw_long = _raw(long_idx)
        raw_short = _raw(short_idx)
        if weighting == "inv_vol" and (
                (long_idx.size and np.any(hist_count[t, long_idx] < 2)) or
                (short_idx.size and np.any(hist_count[t, short_idx] < 2))):
            # 历史不足两根：整行回退等权，不报错，但在 dropped_bars 留痕。
            n_iv_fallback += 1
            raw_long = np.ones(long_idx.size, dtype="float64")
            raw_short = np.ones(short_idx.size, dtype="float64")

        w_long, cap_long = _waterfill(raw_long, max_weight, long_target)
        w_short, cap_short = _waterfill(raw_short, max_weight, short_target)
        if cap_long or cap_short:
            binding.add("max_weight")
        raw_weights[t, long_idx] = w_long
        raw_weights[t, short_idx] = -w_short

    # 冻结到再平衡网格：非网格行（含成员不足的网格行的后续行）持仓不动。
    # 必须在换手约束**之前**——约束作用在真实持仓序列的逐 bar 增量上，
    # 冻结后再平滑才是「每 rebalance 根 bar 换一次仓」的换手。
    raw_weights = rebalance_freeze(raw_weights, dropped_row, rebalance)

    # 换手约束：相对上一时点（已受约束）的最终权重缩放增量。
    weights = np.zeros_like(raw_weights)
    prev = np.zeros(n_sym, dtype="float64")
    for t in range(t_len):
        if dropped_row[t]:
            if rebalance > 1:
                # 再平衡持有语义（06 §5.6）：网格行算不出 = 今天不调仓，持仓
                # 不动——数据缺口不触发斩仓。`dropped_bars` 仍如实记账。
                weights[t] = prev
                continue
            # 逐 bar（rebalance=1）保持旧语义：整行置 0 是硬要求，
            # 被丢弃的时点不做换手平滑。
            prev = np.zeros(n_sym, dtype="float64")
            continue
        w = raw_weights[t].copy()
        delta = w - prev
        turn = 0.5 * float(np.abs(delta).sum())
        if turn > turnover_max:
            w = prev + (turnover_max / turn) * delta
            binding.add("turnover_max")
        weights[t] = w
        prev = w

    prev_all = np.vstack([np.zeros((1, n_sym), dtype="float64"), weights[:-1]])
    turnover_series = 0.5 * np.abs(weights - prev_all).sum(axis=1)
    effective_members = min(
        (int(np.count_nonzero(weights[t])) for t in range(t_len)), default=0,
    )

    weights = np.ascontiguousarray(weights, dtype="float64")
    weights.setflags(write=False)
    turnover_series = np.ascontiguousarray(turnover_series, dtype="float64")
    turnover_series.setflags(write=False)

    return {
        "weights_ref": array_digest(weights),
        "weights": weights,
        "turnover_series": turnover_series,
        "effective_members": int(effective_members),
        "binding_constraints": sorted(binding),
        "dropped_bars": {
            "count": int(dropped_row.sum()),
            "by_reason": {
                # s0/04 §3.9 的三个标准原因码，互斥、之和 = count。
                "insufficient_members": int(n_min_members),
                "no_tradable": 0,
                "width_below_min": 0,
            },
            # 历史不足两根时整行回退等权：仍持仓，不算丢弃行。
            "fallback": {"inv_vol_history_short": int(n_iv_fallback)},
        },
    }
