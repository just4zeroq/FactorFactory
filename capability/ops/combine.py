"""能力实现 · combine：组合域合成（S7 完整版）。

四件必须先说清的事：

1. **权重只在 `fit` 段拟合一次，`eval` 段内保持不变**（s0/04 §3.8）。这里不存在
   「按时点重拟合」的参数，也不存在「先看 eval 表现再调权重」的入口——拟合用的
   行索引由 `window.fit` 唯一决定。
2. **三种合成器**，实现名即 `portfolio.combiner`（closed_multi 名字空间）：
   - `equal_weight`：逐候选一票（暴露先逐 bar 截面标准化，否则量纲大的独占结果）；
   - `score_weighted`：权重 ∝ `fit` 段 IC（**带符号**，负 IC 的成员自动反向加权）；
   - `fixed_ridge`：`fit` 段对标签做岭回归，系数即权重（闭式解，无迭代、无随机）。
3. **正交化与相关上限都是「拟合期」动作**：
   - `corr_max < 1`：按 `|fit IC|` 降序贪心保留，与已保留成员的 `fit` 段相关性
     超过上限即剔除，并记入 `dropped_members`（不静默丢）；
   - `orthogonalise = symmetric`：对保留成员的 `fit` 段相关阵 A 做对称正交化
     （Löwdin，`A^{-1/2}`），`w_eff = A^{-1/2} w`；`eval` 段沿用同一变换。
4. **暴露只有一份真相**：逐候选暴露读 `candidate_set.exposures`（来自 `evaluate`
   的 `values_ref`）；本模块绝不重算表达式，也不从标签派生分数。

`combine` 只交分数、不交结论：净表现由 `allocate` + `backtest` 在同一 `caliber`
下算出，`evidence_grade=in_sample` 由编排层写入。

**已知的契约不一致（不在本模块内悄悄修）**：s0/04 §3.8 要求
`fit.start >= max(available_from)`，但编排层把候选的 `available_from` 置成供给
覆盖末端（`live.build_live_supply` 的 `as_of`），该条件在任何真实计划上都不可能
成立。这里的取舍是：**不实现这条检查**，而不是实现一条会把所有计划判死的检查；
正确修法是让 `available_from` 回到「候选首次可计算日」的语义（需编排层改）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import numpy as np

from capability.panel import array_digest
from capability.spec import method_id
from contracts.digest import compute_digest
from contracts.errors import E1004, E3003, E3004, E1021, FlowError

_DECISIONS = frozenset({"pass", "watch"})
_EPS = 1e-12
_SCHEMES = frozenset({"equal_weight", "score_weighted", "fixed_ridge"})
# 岭回归的相对正则强度：λ = rel × trace(XᵀX)/K。写成相对量是为了让权重不随
# 候选暴露的整体尺度漂移（绝对 λ 在 z-score 暴露上等于没正则，在原始价格
# 暴露上等于全零解）。
_RIDGE_REL = 1e-3
# 相关阵求逆平方根时的特征值下界：相关阵理论上半正定，数值上会出现极小负值。
_EIG_FLOOR = 1e-10


def _input(call, kind: str) -> Any:
    for item in call.inputs:
        if item is not None and item.kind == kind:
            return item.data
    raise FlowError(E3003, f"combine 缺输入 {kind!r}", path=f"combine.inputs.{kind}")


def _interval(text: Any, name: str) -> tuple[date, date]:
    if not isinstance(text, str) or ".." not in text:
        raise FlowError(E1004, f"combine.window.{name} 必须是 'YYYY-MM-DD..YYYY-MM-DD'",
                        path=f"combine.params.window.{name}")
    left, right = text.split("..", 1)
    try:
        return date.fromisoformat(left), date.fromisoformat(right)
    except ValueError as exc:
        raise FlowError(E1004, f"combine.window.{name} 日期不合法: {text!r}",
                        path=f"combine.params.window.{name}") from exc


def _check_window(params: Mapping[str, Any], panel) -> dict[str, Any]:
    window = params.get("window")
    if not isinstance(window, Mapping):
        raise FlowError(E3003, "combine.params.window 缺失", path="combine.params.window")
    fit_start, fit_end = _interval(window.get("fit"), "fit")
    eval_start, eval_end = _interval(window.get("eval"), "eval")
    embargo = window.get("embargo")
    if isinstance(embargo, bool) or not isinstance(embargo, (int, np.integer)) \
            or int(embargo) < 0:
        raise FlowError(E1004, "combine.window.embargo 必须是 >=0 的整数",
                        path="combine.params.window.embargo")
    # X6：fit.end + embargo <= eval.start。三个字段单独看都合法，连起来才是
    # 「没有用评估区数据拟合权重」。旧实现只比 fit_end 与 eval_start， embargo
    # 读了却没用——等于把隔离期当装饰。
    if fit_end + timedelta(days=int(embargo)) > eval_start:
        raise FlowError(
            E1021,
            f"combine 的 fit 段与 eval 段未隔离：{fit_end}+{int(embargo)}d > {eval_start}",
            path="combine.params.window",
        )
    times = np.asarray(panel.times)
    if times.size == 0:
        raise FlowError(E3003, "combine 的面板时间轴为空", path="combine.inputs.panel.times")
    # Panel.times 是 UTC 纳秒（panel.as_times 的契约），不是毫秒。
    days = [datetime.fromtimestamp(float(t) / 1_000_000_000.0, tz=timezone.utc).date()
            for t in times]
    last = days[-1]
    if eval_end > last:
        raise FlowError(E1021, f"combine 的 eval 末端 {eval_end} 超出面板覆盖 {last}",
                        path="combine.params.window.eval")
    if fit_start > fit_end:
        raise FlowError(E1004, "combine 的 fit 段起止颠倒", path="combine.params.window.fit")
    fit_rows = np.asarray([fit_start <= d <= fit_end for d in days], dtype=bool)
    if not fit_rows.any():
        raise FlowError(E1021, "combine 的 fit 段与面板时间轴没有交集",
                        path="combine.params.window.fit")
    return {
        "fit": str(window.get("fit")),
        "embargo": int(embargo),
        "eval": str(window.get("eval")),
        "fit_rows": int(fit_rows.sum()),
        "_fit_mask": fit_rows,
    }


def _selected(
    candidate_set: Mapping[str, Any], verdict_set: Mapping[str, Any],
) -> list[dict[str, Any]]:
    cands = candidate_set.get("candidates")
    exposures = candidate_set.get("exposures")
    if not isinstance(cands, list) or not cands:
        raise FlowError(E3003, "combine 的 candidate_set.candidates 为空",
                        path="combine.inputs.candidate_set.candidates")
    if not isinstance(exposures, Mapping):
        raise FlowError(E3003, "combine 的 candidate_set.exposures 缺失",
                        path="combine.inputs.candidate_set.exposures")
    rows = verdict_set.get("per_candidate")
    if not isinstance(rows, list) or not rows:
        raise FlowError(E3003, "combine 的 verdict_set.per_candidate 为空",
                        path="combine.inputs.verdict_set.per_candidate")
    decisions: dict[str, str] = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str):
            decisions[str(row["candidate_id"])] = str(row.get("decision", ""))
    by_id: dict[str, Mapping[str, Any]] = {}
    for cand in cands:
        if isinstance(cand, Mapping) and isinstance(cand.get("candidate_id"), str):
            by_id[str(cand["candidate_id"])] = cand
    picked: list[dict[str, Any]] = []
    for cid in (c for c in by_id if decisions.get(c) in _DECISIONS and c in exposures):
        item = exposures[cid]
        if not isinstance(item, Mapping) or item.get("values") is None \
                or item.get("valid_mask") is None:
            raise FlowError(E3003, f"combine 的候选 {cid} 暴露不完整",
                            path=f"combine.inputs.candidate_set.exposures.{cid}")
        picked.append({
            "candidate_id": cid,
            "available_from": str(by_id[cid].get("available_from", "")),
            "evidence_ref": str(item.get("values_ref", "")),
            "values": np.asarray(item["values"], dtype="float64"),
            "valid_mask": np.asarray(item["valid_mask"], dtype=bool),
        })
    if not picked:
        raise FlowError(E3003, "combine 没有 pass/watch 且带暴露的候选",
                        path="combine.inputs.verdict_set")
    # 成员顺序必须只由内容决定：dict 迭代顺序来自上游产物，换一次上游序列化
    # 顺序就会让权重顺序漂移、同一计划产出不同 digest（AC39）。
    picked.sort(key=lambda item: item["candidate_id"])
    return picked


def _cs_zscore(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """逐 bar 做**标的维**截面标准化：`(x - mean_N) / std_N`，无效格保持 NaN。

    标准化必须在标的维（截面）上做，不是在候选维上做：同一时点的两个候选
    只有在「同一批标的上的相对排序」可比，量纲才被消掉。
    常数行（std=0）记为 0——它没有截面区分度，不能当成极端值。
    """
    count = valid.sum(axis=1, keepdims=True)
    safe = np.where(valid, values, 0.0)
    mean = safe.sum(axis=1, keepdims=True) / np.maximum(count, 1)
    dev = np.where(valid, values - mean, 0.0)
    var = (dev ** 2).sum(axis=1, keepdims=True) / np.maximum(count, 1)
    sd = np.sqrt(var)
    positive = sd > _EPS
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(positive, dev / np.where(positive, sd, 1.0), 0.0)
    return np.where(valid, z, np.nan)


# ---------------------------------------------------------------- 拟合期工具
def _fit_ic(z: np.ndarray, y: np.ndarray, fit_mask: np.ndarray) -> float:
    """`fit` 段逐格 Pearson 相关（z 已逐 bar 截面标准化，故量纲已消）。

    注意这是**池化**相关，不是「逐 bar IC 再取均值」：两者在有缺格时不等价。
    权重只需要一个稳健的方向与强度，池化对缺失更宽容；而判据侧的 IC 仍由
    `observe` 按逐 bar 口径计算——两处不能互相替代，所以这里不冒充 IC。

    只读 `fit_mask` 为真的行：这是「权重只在 fit 段拟合」的唯一实现点。
    """
    a = z[fit_mask].ravel()
    b = y[fit_mask].ravel()
    ok = np.isfinite(a) & np.isfinite(b)
    if np.count_nonzero(ok) < 3:
        return 0.0
    a, b = a[ok], b[ok]
    sa, sb = a.std(), b.std()
    if sa <= _EPS or sb <= _EPS:
        return 0.0
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def _corr_matrix(zs: list[np.ndarray], fit_mask: np.ndarray) -> np.ndarray:
    """成员在 `fit` 段的相关阵；逐对用「双方都有效」的格，不做插补。"""
    k = len(zs)
    flat = [z[fit_mask].ravel() for z in zs]
    out = np.eye(k, dtype="float64")
    for i in range(k):
        for j in range(i + 1, k):
            a, b = flat[i], flat[j]
            ok = np.isfinite(a) & np.isfinite(b)
            if np.count_nonzero(ok) < 3:
                rho = 0.0
            else:
                aa, bb = a[ok], b[ok]
                sa, sb = aa.std(), bb.std()
                rho = 0.0 if sa <= _EPS or sb <= _EPS else float(
                    ((aa - aa.mean()) * (bb - bb.mean())).mean() / (sa * sb))
            out[i, j] = out[j, i] = rho
    return out


def _prune(
    ids: list[str], ic: np.ndarray, corr: np.ndarray, corr_max: float,
) -> tuple[list[int], list[dict[str, Any]]]:
    """按 |fit IC| 降序贪心保留；与任一已保留成员 |ρ|>corr_max 即剔除。

    排序键含 candidate_id（同一 |IC| 时按内容定序），否则同分成员的先后
    依赖上游顺序，同一计划会产出不同成员集。被剔除的成员带原因返回，绝不静默。
    """
    order = sorted(range(len(ids)), key=lambda i: (-abs(float(ic[i])), ids[i]))
    keep: list[int] = []
    dropped: list[dict[str, Any]] = []
    for i in order:
        worst = 0.0
        for j in keep:
            worst = max(worst, abs(float(corr[i, j])))
        if keep and worst > corr_max + _EPS:
            dropped.append({
                "candidate_id": ids[i],
                "reason": "corr_above_max",
                "max_abs_corr": round(worst, 10),
            })
            continue
        keep.append(i)
    return keep, dropped


def _ridge(zs: list[np.ndarray], y: np.ndarray, fit_mask: np.ndarray) -> np.ndarray:
    """`fit` 段岭回归的闭式解；只用「全部成员与标签都有效」的格（完备案例）。

    完备案例而不是插补：插补会凭空造出相关性，而权重是要写进谱系的东西。
    """
    cols = [z[fit_mask].ravel() for z in zs]
    target = y[fit_mask].ravel()
    ok = np.isfinite(target)
    for col in cols:
        ok &= np.isfinite(col)
    m = int(np.count_nonzero(ok))
    k = len(zs)
    if m < k + 2:
        raise FlowError(
            E3003,
            f"combine 的 fit 段完备格只有 {m} 个，不足以拟合 {k} 个权重",
            path="combine.params.window.fit",
            hint="加长 fit 段、减少成员，或改用 equal_weight",
        )
    X = np.stack([col[ok] for col in cols], axis=1)
    t = target[ok]
    gram = X.T @ X
    lam = _RIDGE_REL * max(float(np.trace(gram)) / k, _EPS)
    try:
        return np.linalg.solve(gram + lam * np.eye(k), X.T @ t)
    except np.linalg.LinAlgError:  # 数值奇异：退到最小二乘，仍不引入随机性
        return np.linalg.lstsq(gram + lam * np.eye(k), X.T @ t, rcond=None)[0]


def _sym_inv_sqrt(matrix: np.ndarray) -> np.ndarray:
    """相关阵的对称逆平方根（Löwdin 正交化）：`A^{-1/2}`。

    用 eigh 而不是 inv：相关阵半正定，eigh 的特征值可以显式下限，奇异时
    给出「把共线成员压平」的极限行为，而不是抛一个不可解释的 LinAlgError。
    """
    vals, vecs = np.linalg.eigh(matrix)
    vals = np.clip(vals, _EIG_FLOOR, None)
    return (vecs * (1.0 / np.sqrt(vals))) @ vecs.T


def _combine_score(zs: list[np.ndarray], weights: np.ndarray,
                   shape: tuple[int, int]) -> np.ndarray:
    """加权合成：`Σ w_k z_k / Σ |w_k|`（按逐格可用成员归一）。

    分母用「可用成员的 |w| 之和」而不是常数：某成员在该 bar 缺数据时，
    不能让它的权重凭空变成「默认 0 暴露」——那等于把一个缺失信号当成中性信号。
    """
    k = len(zs)
    num = np.zeros(shape, dtype="float64")
    den = np.zeros(shape, dtype="float64")
    for idx in range(k):
        z = zs[idx]
        ok = np.isfinite(z)
        num += np.where(ok, weights[idx] * np.where(ok, z, 0.0), 0.0)
        den += np.where(ok, abs(float(weights[idx])), 0.0)
    usable = den > _EPS
    if not np.any(usable):
        raise FlowError(E3003, "combine 的全部候选暴露均为空", path="combine.outputs.score")
    with np.errstate(invalid="ignore", divide="ignore"):
        score = np.where(usable, num / np.where(usable, den, 1.0), np.nan)
    return score


def build(call) -> dict:
    params = call.params
    scheme = str(getattr(call, "impl", "") or "")
    if scheme not in _SCHEMES:
        raise FlowError(
            E3004, f"combine 没有实现 {scheme!r}", path="combine.impl",
            hint=f"已实现: {sorted(_SCHEMES)}",
        )
    orth = str(params.get("orthogonalise", "none"))
    if orth not in ("none", "symmetric"):
        raise FlowError(E1004, "combine.params.orthogonalise 只允许 none/symmetric",
                        path="combine.params.orthogonalise")
    try:
        corr_max = float(params.get("corr_max", 1.0))
    except (TypeError, ValueError) as exc:
        raise FlowError(E1004, "combine.params.corr_max 必须是数",
                        path="combine.params.corr_max") from exc
    if not (0.0 < corr_max <= 1.0):
        raise FlowError(E1004, "combine.params.corr_max 必须在 (0,1]",
                        path="combine.params.corr_max")
    domain = str(params.get("domain") or "")
    if not domain:
        raise FlowError(E1004, "combine.params.domain 不得为空", path="combine.params.domain")

    candidate_set = _input(call, "candidate_set")
    verdict_set = _input(call, "verdict_set")
    panel = _input(call, "panel")
    label = _input(call, "label")
    if not all(isinstance(x, Mapping) for x in (candidate_set, verdict_set, label)):
        raise FlowError(E3003, "combine 的 candidate_set/verdict_set/label 必须是映射",
                        path="combine.inputs")
    score0 = label.get("score")
    if not isinstance(score0, np.ndarray) or score0.ndim != 2:
        raise FlowError(E3003, "combine 的 label.score 必须是二维暴露",
                        path="combine.inputs.label.score")
    shape = score0.shape
    window_text = _check_window(params, panel)
    fit_mask = window_text.pop("_fit_mask")
    picked = _selected(candidate_set, verdict_set)

    ids = [item["candidate_id"] for item in picked]
    zs: list[np.ndarray] = []
    for item in picked:
        values, valid = item["values"], item["valid_mask"]
        if values.shape != shape or valid.shape != shape:
            raise FlowError(E3003, f"combine 候选 {item['candidate_id']} 暴露形状与标签不符",
                            path=f"combine.inputs.candidate_set.exposures.{item['candidate_id']}")
        # 先截面标准化再合成：暴露来自任意量纲的表达式，直接相加＝按量纲加权。
        zs.append(_cs_zscore(np.where(valid, values, np.nan), valid))

    # 旧路径逐位保留：等权 + 无正交 + 无相关约束时，历史产物与指纹不变。
    legacy = scheme == "equal_weight" and orth == "none" and corr_max >= 1.0

    dropped: list[dict[str, Any]] = []
    if legacy:
        stacked = np.stack(zs, axis=0)
        count = np.count_nonzero(np.isfinite(stacked), axis=0)
        if not np.any(count > 0):
            raise FlowError(E3003, "combine 的全部候选暴露均为空",
                            path="combine.outputs.score")
        with np.errstate(invalid="ignore"):
            total = np.nansum(stacked, axis=0)
        score = np.where(count > 0, total / np.maximum(count, 1), np.nan)
        weights = np.full(len(ids), 1.0 / len(ids), dtype="float64")
        # legacy 路径不拟合任何东西：`fit_ic` 恒 0 是**占位**，不是「拟合结果恰好
        # 为 0」。两者在报告里必须可区分，否则归因表看起来像算过。
        fit_ic = np.zeros(len(ids), dtype="float64")
        fit_ic_known = False
    else:
        target = np.asarray(score0, dtype="float64")
        fit_ic = np.asarray([_fit_ic(z, target, fit_mask) for z in zs], dtype="float64")
        fit_ic_known = True
        if corr_max < 1.0:
            corr = _corr_matrix(zs, fit_mask)
            keep, dropped = _prune(ids, fit_ic, corr, corr_max)
            ids = [ids[i] for i in keep]
            zs = [zs[i] for i in keep]
            fit_ic = fit_ic[keep]
            picked = [picked[i] for i in keep]
        if scheme == "equal_weight":
            base = np.full(len(ids), 1.0 / len(ids), dtype="float64")
        elif scheme == "score_weighted":
            total_abs = float(np.abs(fit_ic).sum())
            base = (fit_ic / total_abs if total_abs > _EPS
                    else np.full(len(ids), 1.0 / len(ids), dtype="float64"))
        else:
            base = _ridge(zs, target, fit_mask)
        weights = base
        if orth == "symmetric":
            if len(ids) == 1:
                weights = base
            else:
                corr = _corr_matrix(zs, fit_mask)
                weights = _sym_inv_sqrt(corr) @ base
        # 归一化到 |w| 之和 = 1，使合成分数与 z 同尺度（下游 allocate 只吃排序，
        # 但尺度一致让「换合成器」的对比不是「换量纲」）。
        scale = float(np.abs(weights).sum())
        if not np.isfinite(scale) or scale <= _EPS:
            raise FlowError(E3003, "combine 拟合出的权重全为 0",
                            path="combine.outputs.member_weights_ref")
        weights = weights / scale
        score = _combine_score(zs, weights, shape)

    score = np.ascontiguousarray(score, dtype="float64")
    score.setflags(write=False)

    members = [{
        "candidate_id": item["candidate_id"],
        "available_from": item["available_from"],
        "evidence_ref": item["evidence_ref"],
        "weight_share": float(weights[idx]),
        # `None` = 这条路径没有拟合（legacy 直通），不是「拟合得 0」。
        "fit_ic": float(fit_ic[idx]) if fit_ic_known else None,
        "decision": decisions_of(verdict_set, item["candidate_id"]),
    } for idx, item in enumerate(picked)]
    shares = np.asarray(weights, dtype="float64")
    shares.setflags(write=False)
    domain_digest = compute_digest({
        "kind": method_id("combine_domain"),
        "domain": domain,
        "members": [m["candidate_id"] for m in members],
    })
    # windows_used 的键名是**下游契约**：certify 读 `windows_used.eval`
    # （s0/04 §3.10 / AC25），所以 `fit`/`embargo`/`eval` 三个原始字符串必须
    # 原样保留，诊断字段只能追加、不能改名。
    windows_used = {
        "fit": window_text["fit"], "embargo": window_text["embargo"],
        "eval": window_text["eval"], "fit_rows": window_text["fit_rows"],
        "combiner": scheme, "orthogonalise": orth, "corr_max": corr_max,
    }
    return {
        "score_ref": array_digest(score),
        "member_weights_ref": array_digest(shares),
        "members": members,
        "dropped_members": dropped,
        "domain_digest": domain_digest,
        "windows_used": windows_used,
        "score": score,
    }


def decisions_of(verdict_set: Mapping[str, Any], cid: str) -> str:
    for row in verdict_set.get("per_candidate") or []:
        if isinstance(row, Mapping) and row.get("candidate_id") == cid:
            return str(row.get("decision", ""))
    return ""
