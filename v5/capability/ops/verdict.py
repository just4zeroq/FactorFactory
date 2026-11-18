"""能力实现 · verdict：唯一裁判（s0/04 §3.7 / §5）。

这是全系统**唯一**允许产出「pass / reject / watch」的地方（名字空间 `fixed`，
不得配置第二实现，见 `spec.py` / `runtime.register`）。它只做四件事：

1. 对每个候选执行四道判据：DSR、BH/BY-FDR 调整后 p、成本闸、零分布经验 p。
2. 把结果收敛成三值 `decision` 与可枚举 `reason_codes`（不允许自由文本当唯一原因）。
3. 从 `runtime_facts.n_trials` 读试验计数——**不**从 params 读：写进参数就等于
   把「把计数填小」变成一个新自由度（`s0/04 §3.7` / `C16`）。返回值回写
   `n_trials_used`，让「这次裁决用了多少次试验」在产物里可查。
4. 用 `runtime_facts.family_cumulative`（历史 + 本轮全部 raw p）做 BH 族校正：
   只校正本轮短名单几乎不起校正作用，那正是 V4 让 OOS 亏损因子混进来的原因之一。

能力层只返回，不写账本、不产「找到 alpha」之类的结论词（E2004）。落盘与事件由
执行器代写。所有统计原语来自 `capability.inference`，与 observe 共用同一实现。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from v5.capability.inference import (
    bh_fdr, deflated_sharpe_ratio, empirical_p, expected_max_sr,
)
from v5.contracts.errors import E1004, E2003, E3003, FlowError
from v5.capability.timeaxis import CALENDAR_DAYS_PER_YEAR

# 年化只用于把门槛表达成可读数字（与 observe/backtest 的展示口径一致）。

# decision 三值与可枚举 reason_codes——`s0/04 §3.7` 冻结，不得扩张。
_DECISIONS = frozenset({"pass", "reject", "watch"})
_REASON_ORDER = ("dsr_fail", "fdr_fail", "cost_fail", "null_fail",
                 "insufficient_n", "redundant")
_REASONS = frozenset(_REASON_ORDER)
# 两类「不下结论」的原因：证据不足（没检验）与重复（检验过但不独立）。
# 它们与硬失败的区别是「这个候选到底行不行」还没有答案，不能记成 reject——
# reject 是结论，会进族失败计数，也会让同一信号换个名字重跑永远无法通过。
_SOFT_REASONS = frozenset({"insufficient_n", "redundant"})
# 候选 metrics 里 verdict 必须读到的字段。缺任一项说明 observe 没产出完整证据，
# 该候选给 `insufficient_n` + watch，而不是「缺字段当作通过」（V4 的旧失败模式）。
_REQUIRED_METRICS = ("hac_t", "p_value", "cost_adj_return")


def _is_int(value: Any) -> bool:
    return isinstance(value, (int, np.integer)) and not isinstance(value, bool)


def _observation(call) -> Mapping[str, Any]:
    for item in call.inputs:
        if item is not None and item.kind == "observation_set":
            data = item.data
            if not isinstance(data, Mapping):
                raise FlowError(
                    E3003,
                    f"verdict 的 observation_set 输入不是映射，得到 {type(data).__name__}",
                    path="verdict.inputs.observation_set",
                )
            return data
    raise FlowError(
        E3003, "verdict 没有拿到 observation_set 输入",
        path="verdict.inputs.observation_set",
        hint="执行器解析上游 Evidence 产物后放进 inputs[i].data；能力层不自己读盘",
    )


def _n_trials(call) -> int:
    facts = call.runtime_facts
    if "n_trials" not in facts:
        raise FlowError(
            E2003, "verdict 缺 runtime_facts.n_trials",
            path="verdict.runtime_facts.n_trials",
            hint="试验计数由执行器从账本快照注入；不得写进 params 当作可调旋钮",
        )
    value = facts["n_trials"]
    if not _is_int(value) or int(value) < 1:
        raise FlowError(
            E2003, f"runtime_facts.n_trials 必须是正整数，得到 {value!r}",
            path="verdict.runtime_facts.n_trials",
        )
    return int(value)


def _family_pvalues(call) -> list[float]:
    """历史 + 本轮的 raw p（BH 族），可选；只接受有限值。"""
    value = call.runtime_facts.get("family_cumulative")
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise FlowError(
            E2003, "runtime_facts.family_cumulative 必须是列表",
            path="verdict.runtime_facts.family_cumulative",
        )
    out: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise FlowError(
                E2003, "family_cumulative 不能含布尔",
                path="verdict.runtime_facts.family_cumulative",
            )
        try:
            p = float(item)
        except (TypeError, ValueError):
            raise FlowError(
                E2003, f"family_cumulative 含不可解析的 p: {item!r}",
                path="verdict.runtime_facts.family_cumulative",
            ) from None
        if np.isfinite(p):
            out.append(min(1.0, max(0.0, p)))
    return out


def _adjusted_p(raw: list[float], family: list[float], n_trials: int) -> np.ndarray:
    """对整个 BH 族做 FDR 校正，本批每个候选读自己 raw p 的调整值。

    主判据固定 BH（`s0/05`：多种统计变体只能走只读诊断通道，不得成为裁判旋钮；
    BY/Holm 作为对拍原语保留在 `inference.py`）。`family` 为空且
    `n_trials > 本批数量` 时，用 p=1.0 补齐未观测的历史试验（A4 安全网）：
    保守，只会把调整后 p 变大，不会制造假显著。
    """
    pool = list(family)
    if not pool and n_trials > len(raw):
        pool = list(raw) + [1.0] * (n_trials - len(raw))
    if not pool:
        return bh_fdr(raw)
    sorted_pool = np.sort(np.asarray(pool, dtype="float64"))
    adj_sorted = bh_fdr(sorted_pool)
    out = np.empty(len(raw), dtype="float64")
    for i, p in enumerate(raw):
        idx = min(int(np.searchsorted(sorted_pool, p, side="left")),
                  sorted_pool.size - 1)
        out[i] = float(adj_sorted[idx])
    return out


def _numeric(m: Mapping[str, Any], key: str) -> float | None:
    value = m.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _decide(reasons: list[str]) -> str:
    """三值收敛：硬失败 > 软原因（证据不足 / 重复）。

    旧写法里第二段 `return "watch"` 不可达（`insufficient_n` 单条已在第一段
    返回，剩下来必然非空），而新增 `redundant` 后那个位置才重新有意义。
    改成「先剔软原因、看还剩什么」：一步覆盖两种软原因，且不会出现
    「重复 + DSR 失败」被判成 watch。
    """
    if not reasons:
        return "pass"
    hard = [r for r in reasons if r not in _SOFT_REASONS]
    return "reject" if hard else "watch"


def build(call) -> dict:
    params = call.params
    dsr_min = float(params["dsr_min"])
    fdr_max = float(params["fdr_max"])
    cost_adj_min = float(params["cost_adj_min"])
    # family 是本次裁决归属的试验族名（进产物可查），不是判据旋钮。
    family = str(params["family"])

    n_trials = _n_trials(call)
    family_pvalues = _family_pvalues(call)
    obs = _observation(call)
    # 独立性诊断：`redundancy` 由 observe 产出，缺省（旧产物/关闭判重）时 None。
    # 它**不进判据**——用 n_effective 会降低 expected_max_sr，等于放宽门槛。
    # 落盘的意义是让「这批候选有多雷同」可查，且两个数一起出现才不会误读。
    redundancy = obs.get("redundancy")
    n_effective = None
    if isinstance(redundancy, Mapping):
        raw_eff = redundancy.get("n_effective")
        if isinstance(raw_eff, int) and not isinstance(raw_eff, bool) \
                and raw_eff >= 1:
            n_effective = int(raw_eff)
    per = obs.get("per_candidate")
    if not isinstance(per, list) or not per:
        raise FlowError(
            E3003, "verdict 的 observation_set.per_candidate 为空",
            path="verdict.inputs.observation_set.per_candidate",
            hint="没有候选就没有裁决；空集不是「全部通过」",
        )

    # 逐条校验候选结构：candidate_id 与 metrics 缺失都是上游产物坏了。
    rows: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for i, row in enumerate(per):
        if not isinstance(row, Mapping):
            raise FlowError(
                E3003, f"per_candidate[{i}] 不是映射",
                path=f"verdict.inputs.observation_set.per_candidate[{i}]",
            )
        cid = row.get("candidate_id")
        if not isinstance(cid, str) or not cid:
            raise FlowError(
                E3003, f"per_candidate[{i}].candidate_id 必须是非空字符串",
                path=f"verdict.inputs.observation_set.per_candidate[{i}].candidate_id",
            )
        if cid in seen:
            raise FlowError(
                E3003, f"candidate_id 重复: {cid}",
                path=f"verdict.inputs.observation_set.per_candidate[{i}].candidate_id",
                hint="重复候选会让一次试验被判两次",
            )
        seen.add(cid)
        metrics = row.get("metrics")
        if not isinstance(metrics, Mapping):
            raise FlowError(
                E3003, f"候选 {cid} 缺 metrics 映射",
                path=f"verdict.inputs.observation_set.per_candidate[{i}].metrics",
            )
        rows.append((cid, metrics))

    # ---- DSR 的 sigma_sr -------------------------------------------------
    # 默认用**单策略 SR 的采样误差** `1/√(n_obs−1)`（`inference` 的缺省）。
    # 这是 ACCEPTANCE 里「n_trials=255 → 年化门槛 1.53」唯一自洽的口径：
    # 3.330 × 0.0241 ≈ 0.080/期 ≈ 1.53 年化（日频 √365≈19.1）。
    #
    # 「试验间横截面离散度」不再作默认，原因有三条，都是实测得出的：
    # 1. 它测的是**候选之间差异有多大**，不是「这个 SR 有多少来自运气」。
    #    同一个因子模板换一批候选集，门槛会从 0.08/期跳到 0.40/期；
    # 2. 它让 DSR 只剩 0/1 两态：V5 实测同一时间轴上「合约 32 候选全 reject、
    #    现货 15 候选全 pass」，差别只来自候选集构成，与因子质量无关；
    # 3. 它与文档的已知答案差 7 倍（0.131×3.01=0.395/期 vs 0.080/期）。
    #
    # 横截面离散度保留为**诊断字段**，因为它在「同批候选彼此是否雷同」上仍有用；
    # 只是在判据里不再承担门槛。
    sigma_mode = str(params.get("sigma_mode") or "sampling")
    if sigma_mode not in ("sampling", "cross_section"):
        raise FlowError(
            E1004, f"未知 sigma_mode {sigma_mode!r}", path="verdict.params.sigma_mode",
            hint="可用: sampling（默认）/ cross_section（诊断性旧口径）",
        )
    sr_pop = [s for _, m in rows if (s := _numeric(m, "sr")) is not None]
    sigma_cross: float | None = None
    if len(sr_pop) >= 3:
        std = float(np.std(sr_pop, ddof=1))
        if std > 0 and np.isfinite(std):
            sigma_cross = std
    sigma_sr: float | None = sigma_cross if sigma_mode == "cross_section" else None

    # p_value=0.0 是真实的极端显著值，不能用 `or 1.0` 兜底——那会把最显著的
    # 候选换成最不显著。只有 None/非有限（观测层没算出来）才保守地当 1.0。
    # Stage A 粗筛未过的候选**根本没被检验**，它的 p=None 不得作为 1.0 塞进
    # BH 族（那会抬高其他候选的调整后 p、压低发现率；复核 2）。族校正只纳入
    # 已走完检验链的候选。
    raw_p: list[float] = []
    in_family: list[bool] = []
    for _, m in rows:
        tested = m.get("stage_a_pass") is not False
        in_family.append(tested)
        if not tested:
            raw_p.append(1.0)  # 占位，不进族；stats 也不会暴露它
            continue
        p = _numeric(m, "p_value")
        raw_p.append(1.0 if p is None else min(1.0, max(0.0, p)))
    family_p = [p for p, tested in zip(raw_p, in_family) if tested]
    adjusted_tested = _adjusted_p(family_p, family_pvalues, n_trials)
    adjusted: list[float | None] = []
    it = iter(adjusted_tested)
    for tested in in_family:
        adjusted.append(float(next(it)) if tested else None)

    per_out: list[dict[str, Any]] = []
    for i, (cid, m) in enumerate(rows):
        reasons: list[str] = []

        # Stage A 粗筛未过的候选：observe 没有对它做完整检验（无零分布、p 值
        # 被置 None）。它既不能算通过，也不能算"被拒绝的坏因子"——那会把一个
        # 只被廉价筛子扫掉的候选变成已裁决结论。强制 watch + insufficient_n，
        # 且 stats 里不给 p 值（没检验就不该有 p 值；review P1-1 / 复核 2）。
        if m.get("stage_a_pass") is False:
            per_out.append({
                "candidate_id": cid,
                "decision": "watch",
                "reason_codes": ["insufficient_n"],
                "statistics": {
                    "n_trials": n_trials, "family": family,
                    "fdr_correction": "bh", "tested": False,
                    "n_effective_trials": n_effective,
                },
            })
            continue

        stats: dict[str, Any] = {
            "p_value": raw_p[i],
            "p_adj": float(adjusted[i]) if adjusted[i] is not None else 1.0,
            "n_trials": n_trials,
            "family": family,
            "fdr_correction": "bh",
            "tested": True,
            # 诊断量，与 `n_trials` 并列落盘：两个数一起出现才不会把
            # 「本批候选很雷同」误读成「判据放宽了」。判据仍用 `n_trials`。
            "n_effective_trials": n_effective,
        }

        # 独立性：observe 判出「与某个独立发现是同一信号」→ 软原因 redundant。
        # 判 `watch` 而不是 `reject`（见 `_decide`）：它是真信号，只是不独立；
        # `reject` 会把它记成坏因子、并让同一信号换个名字重跑永远过不了。
        redundant_of = m.get("redundant_of")
        if isinstance(redundant_of, str) and redundant_of:
            reasons.append("redundant")
            stats["redundant_of"] = redundant_of

        # 证据完整性：缺任一必需统计量 → insufficient_n（watch）。
        # 注意这里不因为缺字段就拒绝整批：一个坏候选不应该让其余候选失去裁决。
        if any(_numeric(m, key) is None for key in _REQUIRED_METRICS):
            reasons.append("insufficient_n")
            decision = _decide(reasons)
            ordered = [r for r in _REASON_ORDER if r in set(reasons)]
            per_out.append({
                "candidate_id": cid,
                "decision": decision,
                "reason_codes": ordered,
                "statistics": stats,
            })
            continue

        hac_t = float(_numeric(m, "hac_t") or 0.0)
        cost_adj = float(_numeric(m, "cost_adj_return") or 0.0)
        stats["hac_t"] = hac_t
        stats["cost_adj_return"] = cost_adj

        # --- FDR 闸 ---
        if float(adjusted[i]) > fdr_max:
            reasons.append("fdr_fail")

        # --- DSR 闸：每期 SR + 真实观测期数 ---
        # 不再回落 `ir`/`n_obs`：那是 V4 的 IC 信息比口径，V5 全仓没有任何模块
        # 产出这两个字段。留着一条永不命中的分支，只会让下一个读者以为 DSR
        # 有两套口径，并照它去「修」另一头。sr 缺失时 DSR 保持 0（保守）。
        sr = _numeric(m, "sr")
        n_obs = _numeric(m, "sr_n_obs")
        dsr = float(_numeric(m, "dsr") or 0.0)
        if sr is not None and n_obs is not None and int(n_obs) >= 2:
            sigma_used = (sigma_sr if sigma_sr is not None
                          else 1.0 / math.sqrt(max(int(n_obs) - 1, 1)))
            benchmark = expected_max_sr(int(n_trials), sigma=sigma_used)
            # 偏度/峰度：`skew`/`kurtosis` 是 observe 与本轮新增的口径（毛多空
            # 收益序列，与 `sr` 同源）。`net_skew`/`net_kurtosis` 是旧名，保留
            # 回落以兼容既有产物——但首位改为非 net 命名的字段，是因为
            # observe 手里只有毛序列，叫 net_ 会撒谎。
            skewness = _numeric(m, "skew")
            if skewness is None:
                skewness = _numeric(m, "net_skew")
            kurtosis = _numeric(m, "kurtosis")
            if kurtosis is None:
                kurtosis = _numeric(m, "net_kurtosis")
            dsr = deflated_sharpe_ratio(
                sr, int(n_obs), n_trials,
                skewness=0.0 if skewness is None else skewness,
                kurtosis=3.0 if kurtosis is None else kurtosis,
                sigma_sr=sigma_sr,
            )
            # 门槛直接落盘：不用反推「DSR 0.03 到底对应多少 SR」，也不会在
            # 换了 sigma 口径之后让人误以为门槛没变。
            stats["dsr_benchmark_sr"] = float(benchmark)
            stats["dsr_benchmark_sr_annual"] = float(
                benchmark * math.sqrt(CALENDAR_DAYS_PER_YEAR))
            stats["sigma_sr_used"] = float(sigma_used)
            stats["sigma_mode"] = sigma_mode
            if sigma_cross is not None:
                stats["sigma_sr_cross"] = float(sigma_cross)
        stats["dsr"] = dsr
        if dsr < dsr_min:
            reasons.append("dsr_fail")

        # --- 成本闸 ---
        if cost_adj <= cost_adj_min:
            reasons.append("cost_fail")

        # --- 零分布经验 p 闸 ---
        null_samples = m.get("null_distribution")
        emp_p = _numeric(m, "empirical_p")
        if null_samples is not None:
            if not isinstance(null_samples, (list, tuple)):
                raise FlowError(
                    E3003, f"候选 {cid} 的 null_distribution 必须是列表",
                    path=f"verdict.inputs.observation_set.per_candidate[{i}].metrics.null_distribution",
                )
            emp_p = empirical_p(abs(hac_t), list(null_samples))
        if emp_p is None:
            # 没有零分布证据：证据不足，watch（不算通过）。
            reasons.append("insufficient_n")
        else:
            stats["empirical_p"] = float(emp_p)
            if emp_p > fdr_max:
                reasons.append("null_fail")

        ordered = [r for r in _REASON_ORDER if r in set(reasons)]
        decision = _decide(list(reasons))
        if decision not in _DECISIONS:  # pragma: no cover - 防御
            raise FlowError(E3003, f"非法 decision: {decision}", path="verdict.decision")
        per_out.append({
            "candidate_id": cid,
            "decision": decision,
            "reason_codes": ordered,
            "statistics": stats,
        })

    return {
        "per_candidate": per_out,
        "n_trials_used": n_trials,
    }
