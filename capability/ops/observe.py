"""能力实现 · observe：两阶段漏斗 + 零分布证据采集（s0/04 §3.6 / §1.3）。

这是 V4 头号缺陷（置换零分布错）的修复落点。它只做四件事：

1. **Stage A 廉价粗筛**：覆盖率、换手、|IC| 三项 prefilter 先筛掉明显候选，
   并逐条记拒绝理由。Stage A 不是"放宽的闸门"——被筛掉的候选不进零分布、
   **不得报告为"已检验"**（`core/evaluation/funnel.py` 的设计要点）。
2. **Stage B 零分布精算**：只对幸存者算置换零分布与 `empirical_p`。零分布用
   `inference.cross_section_permute`（行内跨资产置换），不用整行时间轴置换——
   后者保留资产漂移，实测中位 |t|=4.74 > 观测 3.46，`empirical_p=0.95`
   把强因子判死（`production_gap_review_20261115.md` §4）。
3. **指标带 method_id**：每个指标声明算法+版本（`method_ids`），同名指标不同算法
   不是同一个量（`s0/04 §3.6`）。
4. **只产观测，不产结论**：没有 pass/significant 等词（E2004），裁决归 verdict。

暴露的来源（`values_ref` 唯一来源契约）：执行器把 `evaluate` 产出的 `(T,N)` 数组
注入 `candidate_set["exposures"][candidate_id]`，observe 只读、绝不重算。候选对象
本身仍不带任何暴露/指标（02-mining-interface §6 禁止字段不变）。

S3 边界与裁决（需在 S3-M6 正式化）：
- params.json 的 observe.inputs 只声明 candidate_set/panel/label，没有独立的
  exposure kind；暴露以 candidate_set 内并列通道传入，避免改 schema 破坏
  现有 3 输入桩（test_v5_capability_runtime._observe_call）。
- composes 声明 allocate/backtest 但**本阶段不执行子调用**：两者是 S4 空壳。
  成本用标注了 method_id 的简化多空换手估计（方法版本见 `spec.METHOD_STAGES`），净成本
  归 S4 的同源 allocate+backtest。这是诚实的阶段边界，不冒充净成本。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from capability.inference import (
    cross_section_permute, empirical_p, ic_series, newey_west_tstat,
    pvalue_from_t, sample_shape, spearman_ic_row,
)
from capability.panel import Panel, array_digest
from capability.ops.label import build_raw_by_h
from capability.stats import nanmean, row_corr
from contracts.errors import E1004, E1008, E3001, E3003, FlowError
from capability.timeaxis import CALENDAR_DAYS_PER_YEAR
from capability.spec import method_id

# 方法版本：指标名 → 算法@版本。换算法就是新口径，不得静默复用旧值。
_METHODS = {
    "ic": "spearman@1",
    "rank_ic": "spearman@1",
    "pearson_ic": "pearson@1",
    "icir": "mean_std@1",
    "hac_t": "newey_west@1",
    "turnover": method_id("simple_longshort"),
    "gross_sharpe": method_id("longshort"),
    "hit_rate": "frac_positive@1",
    "coverage": "valid_fraction@1",
    "skew": "sample_skew@1",
    "kurtosis": "sample_kurtosis@1",
    "group_returns": "score_quantile_ew@1",
    "monotonicity": "group_order_spearman@1",
    "ic_decay": "ic_horizon_curve@1",
    "dedup": "score_corr_greedy@1",
    "null": {
        "within_bar_cross_asset": "cross_section_permute@1",
        "block_bootstrap": "moving_block_bootstrap@1",
        "time_shift": "whole_row_time_permutation@deprecated",
    },
}
_NULL_MODELS = frozenset(_METHODS["null"])
# 被 V4 实证有偏的整行时间轴置换：保留在枚举里供诊断/对拍，但 method_id 标
# deprecated，并在产物里给出 biased 警告——不得被误当成有效裁决证据。
_BIASED_NULL = "time_shift"
_BLOCK_MIN = 4
# 年化天数统一从 timeaxis 读（G8）：365 是定义性常量，不是口径旋钮。
# 判重的相关性开关默认关闭：`corr=1.0` 表示永不判为同一因子，既有计划
# 行为逐位不变。开启是口径变更（新试验族），必须显式写进计划。
_DEDUP_OFF = 1.0


def _inputs(call) -> tuple[Mapping[str, Any], Panel, Mapping[str, Any]]:
    got: dict[str, Any] = {}
    for item in call.inputs:
        if item is None:
            continue
        if item.kind in got:
            raise FlowError(
                E3001, f"observe 重复输入 {item.kind!r}", path=f"observe.inputs.{item.kind}",
            )
        got[item.kind] = item.data
    for kind in ("candidate_set", "panel", "label"):
        if kind not in got:
            raise FlowError(
                E3001, f"observe 缺输入 {kind!r}", path=f"observe.inputs.{kind}",
            )
    cs, panel, label = got["candidate_set"], got["panel"], got["label"]
    if not isinstance(cs, Mapping):
        raise FlowError(E3003, "observe 的 candidate_set 必须是映射",
                        path="observe.inputs.candidate_set")
    if not isinstance(panel, Panel):
        raise FlowError(E3003, "observe 的 panel 必须是 Panel",
                        path="observe.inputs.panel")
    if not isinstance(label, Mapping):
        raise FlowError(E3003, "observe 的 label 必须是映射",
                        path="observe.inputs.label")
    return cs, panel, label


def _label_arrays(label: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, int]:
    score = label.get("score")
    valid = label.get("valid_mask")
    if not isinstance(score, np.ndarray) or not isinstance(valid, np.ndarray):
        raise FlowError(
            E3003, "label 必须带 score/valid_mask 数组", path="observe.inputs.label",
            hint="标签由 Target 工位产出；能力层不自己算未来收益",
        )
    if score.shape != valid.shape or score.ndim != 2:
        raise FlowError(E3003, "label.score 与 valid_mask 必须是同形二维数组",
                        path="observe.inputs.label")
    eff = label.get("effective_n")
    if not isinstance(eff, (int, np.integer)) or isinstance(eff, bool) or int(eff) < 2:
        raise FlowError(E3003, "label.effective_n 必须是 >=2 的整数",
                        path="observe.inputs.label.effective_n")
    return np.asarray(score, dtype="float64"), np.asarray(valid, dtype=bool), int(eff)


def _candidates_and_exposures(
    cs: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    cands = cs.get("candidates")
    exposures = cs.get("exposures")
    if not isinstance(cands, list) or not cands:
        raise FlowError(E3003, "candidate_set.candidates 为空",
                        path="observe.inputs.candidate_set.candidates")
    if not isinstance(exposures, Mapping):
        raise FlowError(
            E3003, "candidate_set.exposures 缺失或非映射",
            path="observe.inputs.candidate_set.exposures",
            hint="暴露由 evaluate 产出、执行器注入；observe 只读不重算",
        )
    for i, cand in enumerate(cands):
        if not isinstance(cand, Mapping) or not isinstance(cand.get("candidate_id"), str):
            raise FlowError(E3003, f"candidates[{i}] 缺候选身份",
                            path=f"observe.inputs.candidate_set.candidates[{i}]")
    return cands, exposures


def _exposure(exposures: Mapping[str, Any], cid: str, shape: tuple[int, int],
              panel: Panel) -> tuple[np.ndarray, np.ndarray, str]:
    if cid not in exposures:
        raise FlowError(E3003, f"候选 {cid} 没有暴露",
                        path=f"observe.inputs.candidate_set.exposures.{cid}")
    item = exposures[cid]
    if not isinstance(item, Mapping):
        raise FlowError(E3003, f"候选 {cid} 的暴露必须是映射",
                        path=f"observe.inputs.candidate_set.exposures.{cid}")
    values = item.get("values")
    valid = item.get("valid_mask")
    ref = item.get("values_ref")
    if not isinstance(values, np.ndarray) or values.shape != shape:
        raise FlowError(E3003, f"候选 {cid} 的暴露 values 形状应为 {shape}",
                        path=f"observe.inputs.candidate_set.exposures.{cid}.values")
    if not isinstance(valid, np.ndarray) or valid.shape != shape or valid.dtype != bool:
        raise FlowError(E3003, f"候选 {cid} 的 valid_mask 必须是同形布尔数组",
                        path=f"observe.inputs.candidate_set.exposures.{cid}.valid_mask")
    if not isinstance(ref, str) or not ref:
        raise FlowError(E3003, f"候选 {cid} 缺 values_ref",
                        path=f"observe.inputs.candidate_set.exposures.{cid}.values_ref")
    if array_digest(values) != ref:
        raise FlowError(
            E3003, f"候选 {cid} 的 values_ref 与 values 不符",
            path=f"observe.inputs.candidate_set.exposures.{cid}.values_ref",
            hint="暴露身份必须和内容一致；observe 只读，不改暴露",
        )
    return (np.asarray(values, dtype="float64"),
            np.asarray(valid, dtype=bool), ref)


def _longshort(values: np.ndarray, valid: np.ndarray, label_raw: np.ndarray,
               label_valid: np.ndarray, horizon_bars: int = 1,
               rebalance: int = 1,
               quantile: float | None = None) -> tuple[float, float, float, float, int, np.ndarray]:
    """简化的分位多空：每再平衡期取暴露截面前/后 q 分位（缺省 1/3）等权，算换手、毛收益与毛 Sharpe。

    S3 只用它给 Stage A 一个可比的换手/毛收益口径；净成本（含费用/滑点/资金费）
    归 S4 的同源 allocate+backtest。返回
    `(turnover, gross_sharpe_annualised, gross_return, sr_per_period, n_periods)`：

    - `gross_return` 是**该候选的多空组合每 bar 等效毛收益**——成本闸必须作用在
      候选自己的组合收益上，而不是全市场标签均值（后者对所有候选相同，闸形同虚设,
      review P0）。
    - `sr_per_period` / `n_periods` 是**未年化**的每期 Sharpe 与真实观测期数，
      专供 DSR（`inference.deflated_sharpe_ratio` 明确要求每期 SR；喂年化值、
      或用面板格子数当 n_obs 会把 DSR 系统性抬高）。年化只用于展示字段。

    **量纲（06 §5.6）**：`ls_t = Σ_s w_{t,s} · raw_{t,s}` 的第二操作数必须是
    **变换前**的标签收益 `raw`（收益单位）。`cs_rank`/`cs_zscore` 变换后的
    `score` 单位是秩/标准差——拿它当组合收益，`gross_return − 成本` 就是
    量纲混拼；变换后的 `score` 只进 IC（rank IC）。权重来自暴露自身的
    截面排序，与标签变换无关。

    **对齐（06 §5.6）**：`entry=next_close` 的成交延迟已编在标签里
    （`raw_t = p[t+1+h]/p[t+1] − 1`）：权重行 `t` 在 bar `t+1` 成交（回测链
    `NEXT_BAR`），恰好吃满 `raw_t` 的窗口。所以 `ls_t = Σ w_t · raw_t`，
    **不带**额外的 +1 移位——那会把延迟算两次，且与回测链错开一根 bar。

    **再平衡（06 §5.6）**：目标权重只在 `t % rebalance == 0` 重算，中间行
    冻结沿用（与 allocate 同一网格、同一持仓过程）；`rebalance=1` 即逐 bar
    重算的旧行为。

    **时间单位**：标签是 `h` 根 bar 的跨度收益，每个 bar 采一次样（重叠窗口），
    所以 `ls` 序列的一「期」= h 根 bar。两处换算：

    - `gross_return = mean(ls) / h`：与「每 bar 成本」同量纲，可直接相减；
    - `gross_sharpe` 年化乘 `sqrt(365 / h)` 而不是 `sqrt(365)`：一年只有
      `365/h` 个独立持有期，按 365 年化会把 SR 多报 `sqrt(h)` 倍
      （h=5 时日频从 1.81 虚报成 4.05）。

    `sr_per_period` 不随 h 变（除以 h 只缩放均值与标准差，比值不变），所以
    DSR 侧的样本量口径要单独处理，不在这里混同。

    第 6 个返回值是**多空收益序列本身**（已剔除非有限值）。DSR 的偏度/峰度
    修正需要它：只给均值与标准差，任何肥尾修正都无从计算（首版就是这样
    ——`net_skew`/`net_kurtosis` 全仓没有生产方，DSR 恒按正态算）。
    """
    t_len, n_sym = values.shape
    h = max(int(horizon_bars), 1)
    period = max(int(rebalance), 1)
    mask = valid & label_valid
    weights = np.zeros((t_len, n_sym), dtype="float64")
    # G7：分位出口化为 `prefilter.quantile`；缺省仍是 1/3，和旧的
    # `n_sym // 3` 逐位一致（用 +1e-12 消掉 6*(1/3)=1.999… 这类浮点下取整）。
    q = 1.0 / 3.0 if quantile is None else float(quantile)
    if not 0.0 < q <= 0.5:
        raise FlowError(E1004, f"prefilter.quantile 必须在 (0, 0.5]，得到 {q}",
                        path="observe.params.prefilter.quantile")
    k = max(1, int(n_sym * q + 1e-12))
    for t in range(t_len):
        # 再平衡网格：非网格行持仓不动（与 allocate.rebalance_freeze 同一语义）。
        if period > 1 and t % period:
            weights[t] = weights[t - 1] if t else 0.0
            continue
        row = values[t]
        idx = np.flatnonzero(mask[t] & np.isfinite(row))
        if idx.size < 2:
            if period > 1 and t:
                # 网格行算不出 = 今天不调仓，持仓不动（数据缺口不触发斩仓）；
                # `period == 1` 保持旧行为：该行清仓。
                weights[t] = weights[t - 1]
            continue
        order = idx[np.argsort(row[idx], kind="stable")]
        longs = order[-k:]
        shorts = order[:k]
        weights[t, longs] = 1.0 / k
        weights[t, shorts] = -1.0 / k
    turnover = 0.5 * float(np.abs(np.diff(weights, axis=0)).sum())
    # 多空组合每期收益：t 期持仓乘 t 期标签收益（成交延迟已由标签的 entry
    # offset 表达，同索引 PIT；见 docstring「对齐」）。
    ls = np.sum(weights * label_raw, axis=1)
    ls = ls[np.isfinite(ls)]
    if ls.size < 2:
        return turnover, 0.0, 0.0, 0.0, 0, ls
    std = float(np.std(ls))
    mean = float(np.mean(ls))
    sr_per_period = mean / std if std > 0 else 0.0
    annualised = sr_per_period * np.sqrt(CALENDAR_DAYS_PER_YEAR / h)
    return turnover, annualised, mean / h, sr_per_period, int(ls.size), ls


def _group_stats(values: np.ndarray, valid: np.ndarray, label_raw: np.ndarray,
                 label_valid: np.ndarray, n_groups: int,
                 horizon_bars: int) -> tuple[list[float], float, int]:
    """分层单调性：按 score 截面等分 `n_groups` 组，逐期组内等权收益、跨期平均。

    **为什么 IC 不够**：IC 是一个标量相关系数，测的是「整条截面的排序一致
    程度」；它对收益的**单调变换完全不敏感**——把最高分组的收益放大十倍，
    rank IC 一位不变。而持仓只吃两头、等权买入组内每一只，所以决定净收益的
    是**各组收益的水平差**，不是相关系数的大小。「上尾组 +5%、下尾组 −0.5%」
    与「上尾组 +0.5%、下尾组 −5%」可以是同一个 IC，但前者做多有效、后者做空
    有效，成本与容量也完全不同。`group_returns` 把这个形状摊开来看，
    `monotonicity` 只是它的一个方向性摘要。

    `monotonicity` = 组序（0..G-1）与组均收益序的 Spearman。它只是**诊断量**
    （06 §9），不进任何判据：拿它当通过条件等于凭空造了第二套门槛，而门槛
    必须唯一。

    口径与 `_longshort` 对齐的三处：同一个掩码 `valid & label_valid`、同一个
    **变换前**的 `raw`（变换后 score 的单位是秩/标准差，当收益用就是量纲混拼）、
    同样除以 `h` 换成「每 bar」口径（否则 5 组收益和 `gross_return` 差 h 倍，
    摆在一起比会被误读成因子变强了）。

    **不冻结再平衡网格**：这里测的是「分数 → 收益」的截面映射，不是一条持仓
    路径。冻结持仓属于 `allocate` 的执行口径，混进来会让单调性随 `rebalance`
    漂移，同一个因子在 `rebalance=5` 下"更单调"，那是持仓惯性不是因子性质。

    某期有效标的少于 `n_groups` 时**跳过该期**（不猜、不降组数）：把 4 个标的
    硬分成 5 组会让空组冒充「收益 0」，把单调性稀释向 0。
    """
    t_len, _ = values.shape
    h = max(int(horizon_bars), 1)
    mask = valid & label_valid
    acc = np.zeros(n_groups, dtype="float64")
    cnt = np.zeros(n_groups, dtype="int64")
    periods = 0
    for t in range(t_len):
        row = values[t]
        ret = label_raw[t]
        ok = mask[t] & np.isfinite(row) & np.isfinite(ret)
        idx = np.flatnonzero(ok)
        if idx.size < n_groups:
            continue
        order = idx[np.argsort(row[idx], kind="stable")]
        periods += 1
        # `array_split` 而非 `np.quantile` 切点：同分（并列名次）在后者会落到
        # 同一侧，导致组内样本数悬殊；按名次切保证每组至多差 1 个。
        for g, chunk in enumerate(np.array_split(order, n_groups)):
            if chunk.size:
                acc[g] += float(np.mean(ret[chunk]))
                cnt[g] += 1
    if periods == 0 or int(cnt.min()) == 0:
        # 没有任何一期能分出完整的 G 组：返回空而不是 0 单调性——0 会被读成
        # 「测过了，不单调」，而真相是「测不了」。
        return [], 0.0, 0
    means = acc / cnt / h
    mono = spearman_ic_row(np.arange(n_groups, dtype="float64"), means)
    return [float(x) for x in means], float(mono), int(periods)


def _null_tstats(model: str, values: np.ndarray, score: np.ndarray,
                 ic: np.ndarray, observed_t: float, n_perm: int,
                 seed: int) -> tuple[list[float], float]:
    """返回 (零分布 |t| 列表, empirical_p)。"""
    rng = np.random.default_rng(seed)
    out = np.empty(n_perm, dtype="float64")
    for i in range(n_perm):
        if model == "within_bar_cross_asset":
            permuted = cross_section_permute(rng, score)
            out[i] = abs(newey_west_tstat(ic_series(values, permuted)))
        elif model == "time_shift":
            # 有偏：整行置换保留资产漂移，仅作诊断/对拍，method_id 标 deprecated
            perm = rng.permutation(score.shape[0])
            out[i] = abs(newey_west_tstat(ic_series(values, score[perm])))
        else:  # block_bootstrap：对 IC 序列做移动块自助，保留局部自相关结构
            # 必须先中心化：HAC t 检验的原假设是「IC 均值为 0」。若直接对带
            # 正漂移的 IC 重采样，零分布自带同样的 alpha，强因子 empirical_p
            # 会逼近 0.5（review P1-2）。中心化后重采样的均值才是零假设下的值。
            ic_finite = ic[np.isfinite(ic)]
            if ic_finite.size < _BLOCK_MIN:
                out[i] = 0.0
                continue
            centered = ic_finite - float(np.mean(ic_finite))
            n = centered.size
            block = max(_BLOCK_MIN, int(np.sqrt(n)))
            starts = rng.integers(0, n, size=int(np.ceil(n / block)))
            resampled = np.concatenate([
                centered[s:s + block] for s in starts
            ])[:n]
            out[i] = abs(newey_west_tstat(resampled))
    samples = [float(x) for x in out if np.isfinite(x)]
    return samples, empirical_p(abs(observed_t), samples)


def _dedup_config(params: Mapping[str, Any]) -> dict[str, Any]:
    """读判重参数。缺省关闭（`corr=1.0`），既有计划行为逐位不变。"""
    block = params.get("dedup") or {}
    if not isinstance(block, Mapping):
        raise FlowError(
            E1004, f"observe.params.dedup 必须是映射，得到 {type(block).__name__}",
            path="observe.params.dedup",
        )
    corr = block.get("corr", _DEDUP_OFF)
    if isinstance(corr, bool) or not isinstance(corr, (int, float)):
        raise FlowError(
            E1004, f"dedup.corr 必须是数，得到 {corr!r}", path="observe.params.dedup.corr",
        )
    corr = float(corr)
    if not (0.0 <= corr <= 1.0):
        raise FlowError(
            E1004, f"dedup.corr={corr} 必须落在 [0, 1]", path="observe.params.dedup.corr",
        )
    method = str(block.get("method", "spearman"))
    if method not in ("spearman", "pearson"):
        raise FlowError(
            E1004, f"dedup.method={method!r} 不在 ['pearson', 'spearman']",
            path="observe.params.dedup.method",
        )
    rep_by = str(block.get("rep_by", "abs_ic"))
    if rep_by not in ("abs_ic", "hac_t", "abs_gross_sharpe"):
        raise FlowError(
            E1004, f"dedup.rep_by={rep_by!r} 不在 ['abs_gross_sharpe', 'abs_ic', 'hac_t']",
            path="observe.params.dedup.rep_by",
        )
    return {"corr": corr, "method": method, "rep_by": rep_by,
            "enabled": corr < _DEDUP_OFF}


def _rep_score(row: Mapping[str, Any], rep_by: str) -> float:
    """簇内代表的排序依据（越大越优先当代表）。缺失一律当 0。"""
    key = {"abs_ic": "ic", "hac_t": "hac_t",
           "abs_gross_sharpe": "gross_sharpe"}[rep_by]
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return abs(out) if np.isfinite(out) else 0.0


def _redundancy(
    items: list[tuple[str, np.ndarray, Mapping[str, Any]]],
    *,
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """经济等价判重：按 score 相关聚类，标出「谁是谁的重复」。

    为什么必须在 Evidence 做：内容身份（`candidate_id = H(规范化 AST)`）只能
    拦住写法与身份都相同的候选。`mean(close,5)` 与 `mul(mean(close,5),1.0)`
    是两个 id，却指向同一个信号——它们各计一次试验，`n_trials` 因此虚高，
    而 `expected_max_sr` 随 `√(2·ln n_trials)` 上升，**门槛被自己的记账方式
    收紧了**。这是记账错误，不是因子质量问题；但它不能靠「清 n_trials」修
    （那是下调判据），只能另记一个诊断量 `n_effective`。

    只在 `stage_a_pass` 的候选之间算：未过粗筛的本就不进判据，让它们参与
    聚类只会把「未检验」混进「已检验」的独立性视图。

    贪心顺序按 `rep_by` 降序：质量高的先立为簇代表，后来者相关超限则挂到
    代表名下。这与 Portfolio 的 `corr_max` 贪心同构，但**问的问题不同**：
    这里问「是不是同一个信号」，那里问「能不能同时持仓」。
    """
    corr = float(cfg["corr"])
    method = str(cfg["method"])
    rep_by = str(cfg["rep_by"])
    out: dict[str, Any] = {
        "enabled": bool(cfg["enabled"]),
        "threshold": corr,
        "method": method,
        "rep_by": rep_by,
        "method_id": _METHODS["dedup"],
        "n_candidates": len(items),
        "n_effective": len(items),
        "clusters": [],
        "max_pair_corr": None,
        "redundant_of": {},
    }
    if not cfg["enabled"] or len(items) < 2:
        return out

    # 一次算完全部两两相关：逐行 `row_corr` 再取时点均值，缺失行自动跳过。
    corr_map: dict[tuple[int, int], float] = {}
    max_pair: float | None = None
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i][1], items[j][1]
            if a.shape != b.shape:
                continue
            series = row_corr(a, b, method=method)
            finite = series[np.isfinite(series)]
            if finite.size == 0:
                continue
            value = float(np.mean(finite))
            corr_map[(i, j)] = corr_map[(j, i)] = value
            if max_pair is None or abs(value) > max_pair:
                max_pair = abs(value)
    out["max_pair_corr"] = max_pair

    order = sorted(
        range(len(items)),
        key=lambda i: (_rep_score(items[i][2], rep_by), items[i][0]),
        reverse=True,
    )
    reps: list[int] = []
    assign: dict[str, str | None] = {items[i][0]: None for i in range(len(items))}
    members: dict[int, list[str]] = {}
    # 每个非代表成员与**自己簇代表**的相关强度，用于 `min_abs_corr`
    # （簇内最弱的一环有多强，决定这个簇是不是被勉强拴在一起的）。
    link: dict[int, list[float]] = {}
    for i in order:
        cid = items[i][0]
        best_rep, best_corr = None, 0.0
        for rep in reps:
            rho = corr_map.get((i, rep))
            if rho is None:
                continue
            if abs(rho) >= corr and abs(rho) > best_corr:
                best_rep, best_corr = rep, abs(rho)
        if best_rep is None:
            reps.append(i)
            members[i] = [cid]
            link[i] = []
        else:
            assign[cid] = items[best_rep][0]
            members[best_rep].append(cid)
            link[best_rep].append(best_corr)

    out["clusters"] = [
        {
            "rep": items[rep][0],
            "members": members[rep],
            "min_abs_corr": (min(link[rep]) if link[rep] else None),
        }
        for rep in reps
    ]
    out["n_effective"] = len(reps)
    out["redundant_of"] = assign
    return out


def _group_config(
    params: Mapping[str, Any], metrics_req: list[str],
) -> tuple[int | None, bool]:
    """读分层单调性的组数，并检查「要了指标却没给组数」。

    组数**没有默认值**：3 组、5 组、10 组算出来的单调性不是同一个量，默认一个
    数就等于让同一份计划在两种口径下都"合法"。所以缺组数时报 `E1008`，
    而不是猜 5。
    """
    wanted = bool({"group_returns", "monotonicity"} & set(metrics_req))
    raw = params.get("groups")
    if raw is None:
        if wanted:
            raise FlowError(
                E1008,
                "请求了 group_returns / monotonicity 但未给 observe.params.groups",
                path="observe.params.groups",
                hint="分组数没有默认值：3/5/10 组算出的单调性不是同一个量",
            )
        return None, False
    if isinstance(raw, bool) or not isinstance(raw, (int, np.integer)):
        raise FlowError(
            E1004, f"observe.params.groups 必须是整数，得到 {raw!r}",
            path="observe.params.groups",
        )
    n_groups = int(raw)
    if n_groups < 2:
        raise FlowError(
            E1004, f"observe.params.groups={n_groups} 必须 >=2",
            path="observe.params.groups",
        )
    return n_groups, wanted


def _decay_config(
    params: Mapping[str, Any], metrics_req: list[str],
) -> list[int] | None:
    """读 IC 衰减曲线的跨度列表（单位：bar）。缺省不算是；要了却未给则失败。

    跨度是 **bar 数**而不是 `duration`：衰减曲线的横轴就是"多持有几根 bar"，
    换成 `5d`/`4h` 会让同一份曲线随标的频率变形，读曲线的人反而算不出横轴。
    """
    wanted = "ic_decay" in set(metrics_req)
    raw = params.get("ic_decay")
    if raw is None:
        if wanted:
            raise FlowError(
                E1008,
                "请求了 ic_decay 但未给 observe.params.ic_decay",
                path="observe.params.ic_decay",
                hint="衰减曲线的跨度必须显式列出（bar 数），没有默认集合",
            )
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise FlowError(
            E1004, f"observe.params.ic_decay 必须是整数列表，得到 {type(raw).__name__}",
            path="observe.params.ic_decay",
        )
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, np.integer)) \
                or int(item) < 1:
            raise FlowError(
                E1004, f"ic_decay 的跨度必须是 >=1 的整数（bar），得到 {item!r}",
                path="observe.params.ic_decay",
            )
        value = int(item)
        if value not in out:  # 去重但保序：重复跨度会算两遍同一件事
            out.append(value)
    if not out:
        raise FlowError(
            E1004, "observe.params.ic_decay 不能为空列表", path="observe.params.ic_decay",
        )
    return out


def build(call) -> dict:
    params = call.params
    null_model = str(params["null_model"])
    if null_model not in _NULL_MODELS:
        raise FlowError(
            E1004, f"null_model={null_model!r} 不在 {sorted(_NULL_MODELS)}",
            path="observe.params.null_model",
        )
    metrics_req = [str(m) for m in params["metrics"]]
    n_perm = int(params["n_permutations"])
    if n_perm < 1:
        raise FlowError(E1004, "n_permutations 必须 >=1", path="observe.params.null_model")
    prefilter = dict(params["prefilter"])
    min_cov = float(prefilter.get("min_coverage", 0.0))
    max_turn = float(prefilter.get("max_turnover", 0.0))  # 0 = 不设限
    min_abs_ic = float(prefilter.get("min_abs_ic", 0.0))
    quantile = float(prefilter.get("quantile", 1.0 / 3.0))
    dedup_cfg = _dedup_config(params)
    groups, wants_groups = _group_config(params, metrics_req)
    decay_h = _decay_config(params, metrics_req)
    # 给了跨度但没请求指标 = 配了不用：不算、也不在产物里留一个 None 字段
    # （None 和「没算」在下游是两件事，多留一个口径模糊的键就是隐患）。
    wants_decay = bool(decay_h) and "ic_decay" in set(metrics_req)
    seed = int(call.seed or 0)

    cs, panel, label = _inputs(call)
    score, label_valid, _ = _label_arrays(label)
    if groups is not None and groups > score.shape[1]:
        raise FlowError(
            E1004,
            f"groups={groups} 超过标的数 {score.shape[1]}",
            path="observe.params.groups",
            hint="组数不得多于标的数：空组会被当成「收益 0」把单调性稀释掉",
        )
    # IC 衰减的标签由 label 工位的能力函数产出：未来收益只有一处实现，
    # observe 不另写一遍（否则 entry/中性化/保留区掩码都会悄悄分叉）。
    decay_raw = (build_raw_by_h(panel, dict(call.policy_view or {}), decay_h)
                 if wants_decay else {})
    # 多空收益序列必须是**变换前**的标签收益（06 §5.6）：变换后 score 的单位
    # 是秩/标准差，当组合收益去减成本是量纲混拼。缺 `raw` 是 fail-closed——
    # 静默退回 score 会把量纲 bug 重新藏起来。
    raw = label.get("raw")
    if not isinstance(raw, np.ndarray) or raw.shape != score.shape:
        raise FlowError(
            E3003, "label 缺同形 raw（变换前标签收益）",
            path="observe.inputs.label.raw",
            hint="label 输出 raw = 中性化后、变换前的 forward return；observe 的"
                 "多空序列只能用它，变换后的 score 只进 IC",
        )
    raw = np.asarray(raw, dtype="float64")
    # 再平衡周期：execution 子块的口径字段（06 §5.6），与 allocate 同一网格。
    exec_block = (call.policy_view or {}).get("execution") or {}
    rebalance = exec_block.get("rebalance")
    if rebalance is not None:
        try:
            rebalance = int(rebalance)
        except (TypeError, ValueError) as exc:
            raise FlowError(
                E1013, f"caliber.execution.rebalance 非法: {rebalance!r}",
                path="caliber.execution.rebalance",
            ) from exc
        if rebalance < 1:
            raise FlowError(
                E1013, f"caliber.execution.rebalance={rebalance} 必须 >=1",
                path="caliber.execution.rebalance",
            )
    else:
        rebalance = 1
    cands, exposures = _candidates_and_exposures(cs)
    # 标签跨度（bar 数）由 Target 产出：缺了它就只能假设 1 根，而 h=5 时
    # 会让年化 SR 多报 √5 倍、每 bar 毛收益多报 5 倍（成本闸随之失去意义）。
    # **缺了必须停**：静默按 1 年化正是这类量纲错位的形状（B4——能力模式
    # 下桥曾漏传 horizon_bars，observe 默认 1，不报错，√h 虚高无人知）。
    horizon_bars = label.get("horizon_bars") if isinstance(label, Mapping) else None
    if not isinstance(horizon_bars, (int, np.integer)) or isinstance(horizon_bars, bool) \
            or int(horizon_bars) < 1:
        raise FlowError(
            E3003, f"label.horizon_bars 缺失或非法: {horizon_bars!r}",
            path="observe.inputs.label.horizon_bars",
            hint="标签跨度由 Target 产出并随 label 对象传递；编排层不得丢字段，"
                 "observe 不得猜 1",
        )
    horizon_bars = int(horizon_bars)

    per: list[dict[str, Any]] = []
    # 判重需要每个候选的暴露矩阵，而它算完就丢。只保留一份引用（不复制）
    # 供循环后的两两相关使用。
    dedup_input: list[tuple[str, np.ndarray, dict[str, Any]]] = []
    for i, cand in enumerate(cands):
        cid = str(cand["candidate_id"])
        values, valid, ref = _exposure(exposures, cid, score.shape, panel)
        mask = valid & label_valid
        coverage = float(mask.sum()) / max(1, mask.size)

        ic = ic_series(values, score)
        n_ic = int(np.count_nonzero(np.isfinite(ic)))
        hac_t = newey_west_tstat(ic)
        ic_mean = nanmean(ic)
        ic_std = float(np.std(ic[np.isfinite(ic)])) if np.isfinite(ic).any() else 0.0
        icir = float(ic_mean / ic_std) if ic_std > 0 else 0.0
        rank_ic = nanmean(ic)  # 逐行 spearman 已是 rank IC；与 ic 同源，method_id 相同
        hit_rate = float(np.mean(ic[np.isfinite(ic)] > 0)) if np.isfinite(ic).any() else 0.0
        turnover, gross_sharpe, gross_return, sr_per_period, n_periods, ls = _longshort(
            values, valid, raw, label_valid, horizon_bars, rebalance, quantile)
        # DSR 的偏度/峰度必须来自**同一条**多空收益序列（与 `sr` 同源）：
        # 换个口径填这两个数，等于在方差修正项里混用两个分布。
        net_skew, net_kurt = sample_shape(ls)
        # 分层单调性（06 §9 诊断量）：IC 是一个标量，对收益的单调变换完全不敏感；
        # 持仓吃的是各组收益的水平差。只在被请求时算（每次要重扫全样本）。
        if groups is not None and wants_groups:
            group_returns, monotonicity, group_periods = _group_stats(
                values, valid, raw, label_valid, groups, horizon_bars)
        else:
            group_returns, monotonicity, group_periods = None, None, 0
        # IC 衰减（D4）：同一批时点、同一份中性化标签，只换收益跨度。
        # 曲线形状决定 h 的取舍——短跨度 IC 高但换手贵、长跨度省成本但信号钝，
        # 这个取舍只能靠曲线定，靠单个 h 的 IC 定不了。
        decay_map: dict[str, float | None] | None = None
        if decay_raw:
            decay_map = {}
            for h_key, arr in decay_raw.items():
                series = ic_series(values, arr)
                finite = series[np.isfinite(series)]
                decay_map[h_key] = float(finite.mean()) if finite.size else None
        # Pearson IC 只在被请求时算：它是另一次全样本逐行相关，代价不低，
        # 而绝大多数计划只需要 rank IC。
        pearson_ic = (nanmean(ic_series(values, score, method="pearson"))
                      if "pearson_ic" in set(metrics_req) else None)
        # `_longshort` 返回的是**累计**换手（0.5·Σ|Δw|）：跨 1200 根 bar 累加成
        # 7~8 的量级。拿它和「每期换手上限」比，任何真实候选都会被判 turnover
        # 超限；口径必须按「每期」表达（06 §9：换手是成本可行性的先行指标）。
        turnover_per_period = turnover / max(int(n_periods), 1)
        # HAC t 是 IC 序列均值的 t：自由度取 IC 的**时点数 T-1**，不是面板格子数
        # effective_n（T×N）。用格子数会系统性高估 df、压低 p（review P1-3）。
        p_value = pvalue_from_t(hac_t, df=(n_ic - 1) if n_ic >= 2 else None)

        # ---- Stage A：廉价粗筛，不通过的候选不进零分布精算 ----
        stage_a_reasons: list[str] = []
        if coverage < min_cov:
            stage_a_reasons.append("coverage_below_floor")
        if max_turn > 0 and turnover_per_period > max_turn:
            stage_a_reasons.append("turnover_above_cap")
        if abs(ic_mean) < min_abs_ic:
            stage_a_reasons.append("abs_ic_below_floor")

        metrics: dict[str, Any] = {
            "coverage": coverage,
            "ic": float(ic_mean) if np.isfinite(ic_mean) else 0.0,
            "rank_ic": float(rank_ic) if np.isfinite(rank_ic) else 0.0,
            "pearson_ic": (float(pearson_ic) if pearson_ic is not None
                           and np.isfinite(pearson_ic) else None),
            "icir": icir,
            # 偏度 / 峰度（非超额，正态=3）：DSR 方差项 `1 − skew·SR +
            # (kurt−1)/4·SR²` 的唯一输入。首版没有生产方，修正项恒为空转。
            "skew": float(net_skew),
            "kurtosis": float(net_kurt),
            # Stage A 未过的候选没有走完整检验链：p_value 置 None（证据缺失），
            # verdict 会据此给 watch + insufficient_n，绝不判 reject 冒充"已检验"
            # 的坏因子（review P1-1；s0/04 §3.6）。
            "hac_t": float(hac_t) if not stage_a_reasons else None,
            "p_value": float(p_value) if not stage_a_reasons else None,
            "turnover": turnover,
            "turnover_per_period": turnover_per_period,
            "gross_sharpe": gross_sharpe,
            # `gross_return` 不是可选展示指标，而是 `cost_adj_return` 的基座：
            # 编排层拿到真实 `estimated_cost` 后要算 `gross - cost`。若此处不输出，
            # 编排层只能 `metrics.get("gross_return")` 拿到 None 而静默跳过替换，
            # `cost_adj_return` 就永远停在 S3 的 est_cost=0 占位——成本闸名义存在、
            # 实际按毛收益放行（V5 首跑实测踩中，见 review）。
            "gross_return": float(gross_return),
            "hit_rate": hit_rate,
            # 分层单调性：`group_returns` 是各组每 bar 毛收益（低分 → 高分），
            # `monotonicity` 是组序与收益序的 Spearman。两者同源同掩码，只在
            # 被请求时出现（见 `_group_config`：要指标就必须显式给组数）。
            "group_returns": group_returns,
            "monotonicity": monotonicity,
            "group_periods": group_periods,
            # IC 衰减：{跨度(bar): 平均 IC}。跨度取"口径跨度"那一档时，与 `ic`
            # 的差别只在抽样掩码（衰减不做重叠抽样），不是另一个量纲。
            "ic_decay": decay_map,
            # DSR 口径：用**未年化**的每期 Sharpe 与真实观测期数（多空组合的
            # T-1 期），不用年化 gross_sharpe、也不用面板格子数 effective_n——
            # 后两者会把 DSR 系统性抬高（复核 3；inference 明确要求每期 SR）。
            "sr": sr_per_period if not stage_a_reasons else None,
            "sr_n_obs": n_periods,
            "values_ref": ref,
            "stage_a_pass": not stage_a_reasons,
        }
        # 只返回请求的指标 + 裁决必需统计量；避免多余字段成为未声明通道。
        # `gross_return` 是 `cost_adj_return` 的基座，属「必需统计量」而非可选指标，
        # 与 `hac_t`/`p_value` 同类：不请求也必须给，否则成本闸无法换成净口径。
        requested = set(metrics_req) | {
            "hac_t", "p_value", "coverage", "turnover", "turnover_per_period",
            "gross_sharpe", "gross_return",
            "sr", "sr_n_obs", "values_ref", "stage_a_pass",
            # DSR 的肥尾修正输入：与 `sr` 同源，属裁决必需统计量，不请求也要给。
            "skew", "kurtosis",
            # 判重结论是裁决的必需输入（`redundant` 闸），不是可选展示项：
            # 不请求也要给，否则 verdict 只能靠「字段缺失」猜，而缺失有两种含义。
            "redundant_of",
        }
        if wants_groups:
            # 三个字段是一个整体：只给单调性而不给分组收益，读产物的人无法
            # 判断这个数是被哪一组拖下去的。`group_periods` 一并给出，是为了
            # 区分「全期可分」与「只有 3 期能分出 5 组」——后者的单调性不可信。
            requested |= {"group_returns", "monotonicity", "group_periods"}
        if wants_decay:
            requested.add("ic_decay")
        # 判重算的是「信号是否同一」，与「用户要哪几个指标」无关：过滤前留一份
        # 完整 metrics 供 `_rep_score` 取 ic/hac_t/gross_sharpe，否则默认计划
        # 里不请求 ic 时代表排序依据全为 0，簇代表退化成按 id 字典序。
        full_metrics = dict(metrics)
        metrics = {k: v for k, v in metrics.items() if k in requested}
        dedup_input.append((cid, values, full_metrics))

        null_dist: list[float] = []
        emp: float | None = None
        if not stage_a_reasons:
            null_dist, emp = _null_tstats(
                null_model, values, score, ic, hac_t, n_perm,
                seed + i,  # 每候选独立子种子，同 seed 可复现
            )
            metrics["null_distribution"] = null_dist
            metrics["empirical_p"] = float(emp)

        # S3 成本口径：只有换手可估，费用/滑点/资金费待 S4 同源回测。
        # 成本闸必须作用在候选自己的**多空组合毛收益**上（gross_return），
        # 不能用全市场标签均值——后者对所有候选相同，成本闸就失去区分度
        # （review P0）。S3 估计成本为 0，method_id 标注 S3，绝不冒充净成本。
        est_cost = 0.0  # S4 接 allocate.turnover_series × caliber.net 后替换
        metrics["cost_adj_return"] = float(gross_return - turnover * est_cost)
        cost_attr: dict[str, Any] | None = None
        if bool(params["cost_attribution"]):
            cost_attr = {
                "turnover": turnover,
                "estimated_cost": est_cost,
                "breakdown": {"fee": None, "slippage": None, "funding": None},
                "method_id": method_id("simple_longshort"),
                "note": "S3 只估换手；净成本待 S4 allocate+backtest 同源回测",
            }

        per.append({
            "candidate_id": cid,
            "metrics": metrics,
            "null_distribution": null_dist,
            "cost_attribution": cost_attr,
            "stage_a_reasons": stage_a_reasons,
            "generator": cand.get("generator", ""),
        })

    if not per:
        raise FlowError(E3003, "observe 没有任何候选观测", path="observe.outputs.per_candidate")

    # ---- 经济等价判重（s0/04 §3.6）-------------------------------------
    # 只在走完 Stage A 的候选之间判：未过粗筛的本就不进判据，把它算进
    # `n_effective` 会让「未观测」冒充「已判定为独立」。
    passing = [(cid, values, m) for cid, values, m in dedup_input
               if bool(m.get("stage_a_pass"))]
    redundancy = _redundancy(passing, cfg=dedup_cfg)
    assign = redundancy.pop("redundant_of")
    for row in per:
        row["metrics"]["redundant_of"] = assign.get(row["candidate_id"])
    # 未观测的候选数与「关掉判重」不是一回事：前者是没算，后者是算过但没有
    # 任何一对超过阈值。两个都落盘，读产物的人才知道 `n_effective == n_candidates`
    # 到底是哪一种。
    redundancy["n_unobserved"] = len(dedup_input) - len(passing)

    method_ids = {k: v for k, v in _METHODS.items() if k != "null"}
    method_ids["null"] = _METHODS["null"][null_model]
    method_ids["null_biased"] = null_model == _BIASED_NULL
    return {
        "per_candidate": per,
        "method_ids": method_ids,
        "redundancy": redundancy,
    }
