"""统计推断原语（observe / verdict 共用的唯一实现）。

这一层只回答「一个数怎么算」，不回答「算不算显著」：没有阈值、没有结论、
没有账本。阈值与唯一裁判在 `ops/verdict.py`，零分布的组装在 `ops/observe.py`。
把原语单独收口，是为了让 V4 → V5 的逐位对拍只有一个落点——否则同一条
Newey-West 公式在 observe 与 verdict 各写一遍，改了一处另一处还停在旧语义，
而两边数值都"看起来合理"，没有任何检查点会报错。

与 V4 (`core/evaluation/significance.py`) 的关系：这些函数是那批实现的移植，
保持公式逐位一致，并固化 V4 审计报告确认的三处修复：

- 置换零分布用**行内跨资产**置换（`cross_section_permute`），不用整行时间轴
  置换——后者保留资产漂移，零分布中位 |t|=4.74 > 观测 3.46，empirical_p=0.95
  把强因子判死（`production_gap_review_20261115.md` §4）。
- `empirical_p` 用 Phipson-Smyth 的 +1 校正，非有限观测值返回 1.0，绝不给
  一个 NaN t  statistic 配上 0.0 的假显著。
- DSR 的 `var_term <= 1e-9` fail-closed（返回 0.0）——V4 的 E-01 bug 就是把
  clamp 放进根号里，让病态收益率的 DSR 虚假逼近 1.0。
- `sigma_sr` 的**默认口径是单 SR 的采样误差** `1/√(n_obs-1)`，不是试验间横截面
  离散度。后者测的是「候选之间差异有多大」，挪一批候选门槛就从 0.08/期跳到
  0.40/期，与因子质量无关；它只作诊断字段保留（`verdict` 的 `sigma_mode`）。
  这一段曾被写反过一次，导致「文档说 A、默认跑 B」，最容易被下一个读者
  当 bug 改回去——两处必须同时改，且要先改文档。
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
from scipy import stats as _sstats

_EPS = 1e-12


def spearman_ic_row(a: np.ndarray, b: np.ndarray) -> float:
    """单个截面的 Spearman IC，NaN 成对剔除。

    少于 3 个有效对给 0.0（与 V4 `_spearman_row` 一致：没有有效相关就是无信息，
    不是 NaN——IC 序列里塞 NaN 会被 HAC 整段丢掉，反而掩盖样本不足）。
    """
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 3:
        return 0.0
    ar = np.argsort(np.argsort(a)).astype("float64")
    br = np.argsort(np.argsort(b)).astype("float64")
    da, db = ar - ar.mean(), br - br.mean()
    denom = math.sqrt(float((da ** 2).sum() * (db ** 2).sum()))
    return float((da * db).sum() / denom) if denom > 0 else 0.0


def pearson_ic_row(a: np.ndarray, b: np.ndarray) -> float:
    """单个截面的 Pearson IC，NaN 成对剔除。

    与 `spearman_ic_row` 的分工：秩相关测「排序是否一致」，线性相关测「是否
    同向同幅」。两者在单调非线性因子上会显著分叉——`y = x³` 的 Spearman≈1
    而 Pearson 明显低，这正是判断「能不能用线性权重合成」的依据。
    少于 3 个有效对给 0.0，与 Spearman 同一约定（没有有效相关就是无信息）。
    """
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 3:
        return 0.0
    da, db = a - a.mean(), b - b.mean()
    denom = math.sqrt(float((da ** 2).sum() * (db ** 2).sum()))
    return float((da * db).sum() / denom) if denom > 0 else 0.0


def ic_series(factor: np.ndarray, returns: np.ndarray,
              *, method: str = "spearman") -> np.ndarray:
    """逐时点横截面 IC，返回长度 T 的向量。

    `method="spearman"`（缺省）是 rank IC；`"pearson"` 是线性 IC。两者都保留：
    V4 的 `ic` 一直只有 Spearman，名字却叫 `ic`——补 Pearson 时不改旧字段语义，
    否则所有历史对比都失去基准。
    """
    factor = np.asarray(factor, dtype="float64")
    returns = np.asarray(returns, dtype="float64")
    if factor.shape != returns.shape:
        raise ValueError("factor 与 returns 必须同形状")
    if method == "spearman":
        row_fn = spearman_ic_row
    elif method == "pearson":
        row_fn = pearson_ic_row
    else:
        raise ValueError(f"未知 IC 口径: {method!r}")
    return np.array(
        [row_fn(factor[t], returns[t]) for t in range(factor.shape[0])],
        dtype="float64",
    )


def sample_shape(values: np.ndarray) -> tuple[float, float]:
    """收益序列的偏度与峰度（**非超额**，正态 = 3.0）。

    DSR 的方差项 `1 − skew·SR + (kurt−1)/4·SR²` 需要这两个数。V5 首版没有生产方，
    DSR 恒按正态 `(0, 3)` 算——加密日频的肥尾修正名义存在、实际未生效。
    样本不足（<4 个有限值）时返回正态默认值：不猜形状，也不让 DSR 因缺值崩。
    """
    x = np.asarray(values, dtype="float64").ravel()
    x = x[np.isfinite(x)]
    if x.size < 4:
        return 0.0, 3.0
    # 零方差序列没有「形状」：偏度/峰度的分母是标准差，算出来是 0/0。
    # scipy 会给出 nan 并抛 Precision loss 警告——那不是数据问题，是定义域外。
    # 直接返回正态默认值，比让噪声进日志更有用。
    if float(np.std(x)) == 0.0:
        return 0.0, 3.0
    import warnings

    from scipy import stats as _st

    with warnings.catch_warnings():
        # 近似常数序列会触发 catastrophic cancellation 警告；结果本身仍有限
        # 时照用，非有限时下面兜底成正态默认值。
        warnings.simplefilter("ignore", RuntimeWarning)
        skew = float(_st.skew(x, bias=False))
        kurt = float(_st.kurtosis(x, fisher=False, bias=False))
    if not np.isfinite(skew):
        skew = 0.0
    if not np.isfinite(kurt):
        kurt = 3.0
    return skew, kurt


def _acov(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, dtype="float64")
    x = x - x.mean()
    n = x.size
    if lag >= n:
        return 0.0
    return float(np.sum(x[lag:] * x[: n - lag]) / n)


def newey_west_tstat(series: Sequence[float], max_lags: int | None = None) -> float:
    """IC 序列均值的 HAC t（Newey-West 1987/1994 带宽），逐位对拍 V4。

    样本不足 4、或方差估计非正时返回 0.0（"没有可靠证据"不是"显著为正"）。
    """
    x = np.asarray(series, dtype="float64")
    x = x[np.isfinite(x)]
    t_len = x.size
    if t_len < 4:
        return 0.0
    mean = float(x.mean())
    if max_lags is None:
        max_lags = int(math.floor(4 * (t_len / 100.0) ** (2.0 / 9.0)))
    max_lags = max(1, min(max_lags, t_len - 1))
    s = _acov(x, 0)
    for lag in range(1, max_lags + 1):
        s += 2.0 * (1.0 - lag / (max_lags + 1.0)) * _acov(x, lag)
    var_mean = s / t_len
    if var_mean <= 0:
        return 0.0
    return float(mean / math.sqrt(var_mean))


def cross_section_permute(rng: np.random.Generator, panel: np.ndarray) -> np.ndarray:
    """行内跨资产置换：只打乱每行内部的资产，缺失格原位不动。

    零分布必须与观测统计在**同一批样本**上计算，所以每行资产的 multiset 不变、
    缺失位置不变，只破坏 factor→asset 的对应关系。`mode="time"` 的整行置换
    保留每个资产的无条件漂移，会把 drift 混进零分布（V4 的头号缺陷）。
    """
    panel = np.asarray(panel, dtype="float64")
    finite = np.isfinite(panel)
    u = np.where(finite, rng.random(panel.shape), np.inf)
    order = np.argsort(u, axis=1, kind="stable")
    vals = np.take_along_axis(panel, order, axis=1)
    out = np.full(panel.shape, np.nan, dtype="float64")
    rows, cols = np.nonzero(finite)
    out[rows, cols] = vals[np.isfinite(vals)]
    return out


def empirical_p(value: float, null_samples: Sequence[float]) -> float:
    """单尾经验 p = (1 + #{null >= value}) / (1 + n)，Phipson-Smyth 校正。

    非有限的观测值返回 1.0：一个 NaN 的 HAC t 不能拒绝零假设，更不能在配上
    0.0 之后被误读成"极端显著"。空零分布同样返回 1.0（没有证据不是显著）。
    """
    if not np.isfinite(value):
        return 1.0
    null = np.asarray(null_samples, dtype="float64")
    null = null[np.isfinite(null)]
    n = int(null.size)
    if n == 0:
        return 1.0
    count = int(np.count_nonzero(null >= value))
    return (count + 1.0) / (n + 1.0)


def bh_fdr(pvals: Sequence[float]) -> np.ndarray:
    """Benjamini-Hochberg 校正 q 值，单调化，逐位对拍 V4。"""
    p = np.clip(np.asarray(pvals, dtype="float64"), 0.0, 1.0)
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order]
    adj = ranked * n / np.arange(1, n + 1)
    adj = np.minimum.accumulate(adj[::-1])[::-1]
    out = np.empty(n, dtype="float64")
    out[order] = np.clip(adj, 0.0, 1.0)
    return out


def holm(pvals: Sequence[float]) -> np.ndarray:
    """Holm step-down 校正（FWER），8 行，逐位对拍 V4。"""
    p = np.clip(np.asarray(pvals, dtype="float64"), 0.0, 1.0)
    n = p.size
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order]
    adj = ranked * (n - np.arange(n))
    adj = np.maximum.accumulate(adj)
    out = np.empty(n, dtype="float64")
    out[order] = np.clip(adj, 0.0, 1.0)
    return out


def by_fdr(pvals: Sequence[float]) -> np.ndarray:
    """Benjamini-Yekutieli 校正（依赖条件下的 FDR）。

    用 scipy 的实现（>=1.11），与 statsmodels 逐位一致；statsmodels 只作 dev
    对拍依赖，生产不引入。
    """
    p = np.clip(np.asarray(pvals, dtype="float64"), 0.0, 1.0)
    if p.size == 0:
        return p
    return np.asarray(_sstats.false_discovery_control(p, method="by"), dtype="float64")


def expected_max_sr(n_trials: int, sr_0: float = 0.0, sigma: float = 1.0) -> float:
    """n_trials 次独立随机策略的最大 SR 期望 ≈ sr_0 + sigma·√(2 ln n)。

    n_trials<=1 时无搜索惩罚，直接返回 sr_0（DSR 门槛随试验数上升，这是
    诚实的必然结果，不是可以调小的旋钮）。
    """
    if n_trials <= 1:
        return float(sr_0)
    return float(sr_0 + sigma * math.sqrt(2.0 * math.log(n_trials)))


def deflated_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    n_trials: int = 1,
    *,
    sr_benchmark: float | None = None,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    sigma_sr: float | None = None,
) -> float:
    """Deflated Sharpe Ratio（Bailey & Lopez de Prado 2014），逐位对拍 V4。

    - `sigma_sr` 是**单策略 SR 的采样误差** `1/√(n_obs−1)`，这是缺省口径
      （`verdict.sigma_mode = sampling`）。传试验间横截面离散度只在
      `sigma_mode = cross_section` 的诊断通道里发生——它测「候选彼此有多像」，
      不是「这个 SR 有多少来自运气」，换一批候选门槛就会跳。
    - `var_term <= 1e-9` 直接返回 0.0（fail-closed）——不能把 clamp 放进根号，
      那会让病态厚尾收益的 DSR 虚假逼近 1.0（V4 E-01）。
    - 年化只影响展示：这里比较的是每期 SR，调用方不得先把 SR 年化再传进来。
    """
    if n_obs < 2:
        return 0.0
    if sr_benchmark is None:
        sr_sigma = (
            float(sigma_sr)
            if sigma_sr is not None and np.isfinite(sigma_sr) and float(sigma_sr) > 0
            else 1.0 / math.sqrt(max(n_obs - 1, 1))
        )
        sr_benchmark = expected_max_sr(n_trials, sigma=sr_sigma)
    var_term = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe ** 2
    if not np.isfinite(var_term) or var_term <= 1e-9:
        return 0.0
    denom = math.sqrt(var_term)
    z = (sharpe - sr_benchmark) * math.sqrt(n_obs - 1) / denom
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def pvalue_from_t(t: float, df: int | None = None) -> float:
    """HAC t 的双尾 p：给了 df 用 Student-t 尾，否则用正态尾。

    HAC t 只是渐近 N(0,1)；加密因子 IC 序列的实际样本量（几十到几百）下，
    正态尾系统性低估 p。新代码必须传 `df = n_obs - 1`（V4 E-03）。
    """
    t = float(t)
    if not np.isfinite(t):
        return 1.0
    if df is not None and int(df) >= 2:
        return float(min(1.0, 2.0 * float(_sstats.t.sf(abs(t), float(df)))))
    return float(2.0 * float(_sstats.norm.sf(abs(t))))


__all__ = [
    "spearman_ic_row", "ic_series", "newey_west_tstat", "cross_section_permute",
    "empirical_p", "bh_fdr", "holm", "by_fdr", "expected_max_sr",
    "deflated_sharpe_ratio", "pvalue_from_t",
]
