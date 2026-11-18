"""横截面 / 时序基础算子（06-evaluation.md §4 / §7）。

这一层只做「一个数怎么算」，不做任何判定：没有阈值、没有 p 值、没有结论。
所有函数都是纯函数，输入 `(T, N)` 浮点数组，NaN 一律表示「缺席」，
**不填充、不补零、不替换成常数**（§4 三条硬约束之二）。

三个 `transform` 在这里给出唯一定义，避免「同一名字两种算法」在事后对拍时才暴露：

| 取值 | 定义 |
|---|---|
| `cs_rank` | 逐时点 `rank(x, pct=True, method="average")`，**升序**（x 越大 u 越大） |
| `cs_zscore` | `(x - mean_cs) / std_cs(ddof=1)`；`std == 0` → 该时点整行 NaN |
| `cs_rank_zscore` | 先 `cs_rank` 再 `cs_zscore` |
"""

from __future__ import annotations

import numpy as np
from scipy import stats as _sstats

_EPS = 1e-12


def _row_valid(row: np.ndarray) -> np.ndarray:
    return np.isfinite(row)


def cs_rank(x: np.ndarray) -> np.ndarray:
    """逐时点百分位秩（升序，并列取平均秩）。少于 2 个有效成员 → 整行 NaN。"""
    out = np.full(x.shape, np.nan)
    for t in range(x.shape[0]):
        row = x[t]
        valid = _row_valid(row)
        n = int(valid.sum())
        if n < 2:
            continue
        ranks = _sstats.rankdata(row[valid], method="average")
        out[t, valid] = ranks / n
    return out


def cs_zscore(x: np.ndarray, *, ddof: int = 1) -> np.ndarray:
    """逐时点标准化。`std == 0`（含只有 1 个有效成员）→ 整行 NaN，绝不除、不补 0。"""
    out = np.full(x.shape, np.nan)
    for t in range(x.shape[0]):
        row = x[t]
        valid = _row_valid(row)
        n = int(valid.sum())
        if n < 2:
            continue
        vals = row[valid]
        std = float(np.std(vals, ddof=ddof))
        if not np.isfinite(std) or std <= _EPS:
            continue
        out[t, valid] = (vals - float(np.mean(vals))) / std
    return out


def cs_rank_zscore(x: np.ndarray) -> np.ndarray:
    return cs_zscore(cs_rank(x))


TRANSFORMS = {
    "cs_rank": cs_rank,
    "cs_zscore": cs_zscore,
    "cs_rank_zscore": cs_rank_zscore,
    "none": lambda x: np.array(x, dtype="float64", copy=True),
}


def trailing_mean(x: np.ndarray, window: int) -> np.ndarray:
    """逐标的 trailing 均值，只回看过去 `window` 根（含当前）。

    用累积和实现，避免 `rolling` 的边界语义（min_periods / center）被默认值带偏。
    全是 NaN 的窗口给 NaN，不补 0。
    """
    if window < 1:
        raise ValueError("window 必须 >= 1")
    t_len = x.shape[0]
    valid = np.isfinite(x)
    filled = np.where(valid, x, 0.0)
    csum = np.cumsum(filled, axis=0)
    cnt = np.cumsum(valid.astype("float64"), axis=0)
    out = np.full(x.shape, np.nan)
    for t in range(t_len):
        lo = max(0, t - window + 1)
        s = csum[t] - (csum[lo - 1] if lo > 0 else 0.0)
        c = cnt[t] - (cnt[lo - 1] if lo > 0 else 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[t] = np.where(c > 0, s / np.where(c > 0, c, 1.0), np.nan)
    return out


def trailing_coverage(mask: np.ndarray, window: int) -> np.ndarray:
    """逐格「最近 `window` 根内有效值的比例」，分母是实际回看的根数。

    不能用 `trailing_mean(x != nan)` 代替：它的分母是窗口内**有效计数**，
    于是稀疏数据里一个孤立样本的覆盖率恒为 1.0，`min_coverage` 永远不会触发。
    覆盖率的分母必须是「本该有几根」，否则它测的是「已有数据里有没有缺」，
    而缺的正是那些本该存在、却根本不在数组里的根。
    """
    if window < 1:
        raise ValueError("window 必须 >= 1")
    t_len = mask.shape[0]
    # 入参是「有没有」的布尔掩码，不是取值数组：把数组传进来再判 isfinite，
    # 0 也会被当成「有」，覆盖率恒为 1，门禁永远不触发。
    valid = np.asarray(mask).astype(bool).astype("float64")
    csum = np.cumsum(valid, axis=0)
    out = np.full(mask.shape, np.nan)
    for t in range(t_len):
        lo = max(0, t - window + 1)
        span = t - lo + 1
        s = csum[t] - (csum[lo - 1] if lo > 0 else 0.0)
        out[t] = s / span
    return out


def trailing_rank_pct(x: np.ndarray) -> np.ndarray:
    """逐时点横截面秩，用于分层与 top-k 选择。"""
    return cs_rank(x)


def forward_return(price: np.ndarray, horizon: int, *, entry_offset: int = 0) -> np.ndarray:
    """标签：`p[t + entry_offset + h] / p[t + entry_offset] - 1`。

    - `entry_offset = 0`（`entry=close`）：当根收盘进场，`h` 根后收盘出场；
    - `entry_offset = 1`（`next_close` / `next_open`）：下一根进场。

    未来值只出现在**标签**里，且标签不参与任何分数计算——这正是「同索引不构成
    前视」的边界（`11-field-operator-registry.md` §2.3）。
    """
    if horizon < 1:
        raise ValueError("horizon 必须 >= 1")
    if entry_offset < 0:
        raise ValueError("entry_offset 不得为负")
    t_len = price.shape[0]
    out = np.full(price.shape, np.nan)
    start = entry_offset
    end = entry_offset + horizon
    if t_len <= end:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        # 分子是**未来**的出场价 `p[t+offset+h]`，分母是进场价 `p[t+offset]`。
        # 写反会得到一个数值上"合理"、方向上相反的信号：IC 的符号随之整体翻转，
        # Verdict 的 t 值与组合层持仓方向相反，而两边都不会报错。
        out[: t_len - end] = price[end:] / price[start: t_len - horizon] - 1.0
    # 分母为 0 / 非有限 → NaN（不得产生 inf）
    out[~np.isfinite(out)] = np.nan
    return out


def rolling_beta(
    returns: np.ndarray, market: np.ndarray, window: int,
) -> np.ndarray:
    """逐标的对等权市场收益的 trailing beta，只用 `[t-window+1, t]`。

    回归窗口是**口径字段**（`caliber.label.neutralise_window`）；不足两个共同时点
    时给 NaN，不回落 1.0——回落会让「没有估计」变成「beta=1 的估计」。
    """
    t_len, n_col = returns.shape
    out = np.full((t_len, n_col), np.nan)
    m = np.asarray(market, dtype="float64")
    for j in range(n_col):
        x = returns[:, j]
        both = np.isfinite(x) & np.isfinite(m)
        xs = np.where(both, x, 0.0)
        ms = np.where(both, m, 0.0)
        prod = xs * ms
        c_x = np.cumsum(xs)
        c_m = np.cumsum(ms)
        c_p = np.cumsum(prod)
        c_mm = np.cumsum(ms * ms)
        c_n = np.cumsum(both.astype("float64"))
        for t in range(t_len):
            lo = max(0, t - window + 1)
            n = c_n[t] - (c_n[lo - 1] if lo > 0 else 0.0)
            if n < 2:
                continue
            sx = c_x[t] - (c_x[lo - 1] if lo > 0 else 0.0)
            sm = c_m[t] - (c_m[lo - 1] if lo > 0 else 0.0)
            sp = c_p[t] - (c_p[lo - 1] if lo > 0 else 0.0)
            smm = c_mm[t] - (c_mm[lo - 1] if lo > 0 else 0.0)
            cov = (sp - sx * sm / n) / (n - 1)
            var = (smm - sm * sm / n) / (n - 1)
            if var > _EPS:
                out[t, j] = cov / var
    return out


def cs_neutralise(y: np.ndarray, regressors: list[np.ndarray]) -> np.ndarray:
    """逐时点截面回归取残差：`y ~ 1 + Σ regressor`。

    只用**该时点**的截面，不得全期拟一次系数（§4 最后一段）。有效成员少于
    `1 + 回归元个数 + 1` 时整行 NaN——欠定回归的残差没有意义。
    """
    out = np.full(y.shape, np.nan)
    need = 2 + len(regressors)
    for t in range(y.shape[0]):
        cols = [np.asarray(r, dtype="float64")[t] for r in regressors]
        valid = np.isfinite(y[t])
        for c in cols:
            valid &= np.isfinite(c)
        if int(valid.sum()) < need:
            continue
        design = np.column_stack([np.ones(int(valid.sum()))] + [c[valid] for c in cols])
        target = y[t][valid]
        try:
            beta, *_ = np.linalg.lstsq(design, target, rcond=None)
        except np.linalg.LinAlgError:
            continue
        out[t, valid] = target - design @ beta
    return out


def row_corr(a: np.ndarray, b: np.ndarray, *, method: str = "pearson") -> np.ndarray:
    """逐时点截面相关（NaN 成对剔除）。有效对数 < 3 → NaN。"""
    out = np.full(a.shape[0], np.nan)
    for t in range(a.shape[0]):
        x, y = a[t], b[t]
        valid = np.isfinite(x) & np.isfinite(y)
        if int(valid.sum()) < 3:
            continue
        if method == "spearman":
            xv = _sstats.rankdata(x[valid], method="average")
            yv = _sstats.rankdata(y[valid], method="average")
        else:
            xv, yv = x[valid], y[valid]
        sx, sy = float(np.std(xv)), float(np.std(yv))
        if sx <= _EPS or sy <= _EPS:
            continue
        out[t] = float(np.corrcoef(xv, yv)[0, 1])
    return out


def nanmean(values: np.ndarray) -> float:
    arr = np.asarray(values, dtype="float64")
    if not np.isfinite(arr).any():
        return float("nan")
    return float(np.nanmean(arr))


def rebalance_freeze(weights: np.ndarray, dropped: np.ndarray,
                     rebalance: int) -> np.ndarray:
    """把逐时点目标权重冻结到再平衡网格上（06 §5.6 / caliber.execution.rebalance）。

    `rebalance > 1` 时：目标权重只在 `t % rebalance == 0` 的网格时点重算；
    其余行（含「网格行算不出」的行）**持仓不动**，沿用最近一个成功网格行——
    两次再平衡之间不调仓是 `rebalance` 的经济含义，数据缺口不触发斩仓。
    首个成功网格行之前全部空仓。`rebalance <= 1` 原样返回（逐 bar 重算的
    旧行为逐位不变，含「丢弃行整行清仓」的硬要求）。
    """
    period = int(rebalance)
    if period <= 1:
        return weights
    t_len, n_sym = weights.shape
    out = np.zeros_like(weights)
    last = np.zeros(n_sym, dtype="float64")
    for t in range(t_len):
        on_grid = (t % period == 0)
        if on_grid and not bool(dropped[t]):
            last = weights[t].copy()
        out[t] = last
    return out
