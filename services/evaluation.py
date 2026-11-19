"""预注册评价档位与多指标 scorecard（不止看一个 rank IC）。

这里刻意**不是**第八工位的替代品：`verdict` 仍是唯一裁判，`certify` 仍是唯一
OOS 结论来源。本模块只在 run 产物之上做两件事：

1. 把 run 前冻结在 `plan.evaluation` 的评价档位读出来，形成一份透明 scorecard；
2. 把六层证据（预测力 / 稳定性 / 区分度 / 显著性校正 / 稳健性 / 可实现性）摊开，
   让「为什么这个因子不能用」有具体位置，而不是事后换一个指标碰运气。

防指标购物的规则写进结构，不靠自觉：

- 档位与阈值必须写在 plan 里，run 后从冻结 ticket 读取；
- 任何阈值覆盖都会把 `profile_locked` 置为 false，并取消 scorecard 的晋升资格；
- 档位名不在白名单里直接 fail-closed；
- diagnostic 档即使指标全绿也不得晋升。

WFE / OOS 衰减比的口径：evidence 的 `sr` 是**每持有期** Sharpe，certify 的
`net_sharpe` 是**年化** Sharpe。先把 OOS 年化 Sharpe 除以 sqrt(bars_per_year /
horizon) 换回每持有期，再与 IS 的每期 SR 相除。这样比较的是同一时间单位；
分子是净、分母是毛，所以这个比值是保守的稳健性诊断，不是收益预测。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from contracts.digest import compute_digest
from contracts.errors import E1004, FlowError

SCHEMA = "factorfactory.scorecard@1"
DEFAULT_PROFILE = "strict_institutional_v1"

# 阈值都是**每持有期**或比率口径；年化 Sharpe 只在 certify 产物里出现，
# 进入 scorecard 前统一换算。
_PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "strict_institutional_v1": {
        "description": "机构级七闸：HLZ t>3 + 多重检验 + DSR + OOS + 成本净值，可作为晋升候选",
        "promotable": True,
        "thresholds": {
            "min_abs_rank_ic": 0.02,
            "min_hac_t": 3.0,
            "min_icir": 0.25,
            "min_monotonicity": 0.60,
            "min_dsr": 0.95,
            "max_p_adj": 0.05,
            "min_positive_fold_ratio": 0.60,
            "min_wfe": 0.30,
            "min_net_sharpe": 0.50,
            "max_drawdown": -0.25,
            "max_turnover": None,
            "min_oos_breadth": 5,
        },
    },
    "exploratory_v1": {
        "description": "探索档：允许 t≈2、弱 OOS；只用于生成假设，不得晋升",
        "promotable": False,
        "thresholds": {
            "min_abs_rank_ic": 0.01,
            "min_hac_t": 2.0,
            "min_icir": 0.10,
            "min_monotonicity": 0.30,
            "min_dsr": 0.50,
            "max_p_adj": 0.10,
            "min_positive_fold_ratio": 0.50,
            "min_wfe": 0.0,
            "min_net_sharpe": 0.0,
            "max_drawdown": -0.40,
            "max_turnover": None,
            "min_oos_breadth": 2,
        },
    },
    "diagnostic_v1": {
        "description": "纯诊断档：阈值全零，只展示证据形状，不得晋升",
        "promotable": False,
        "thresholds": {
            "min_abs_rank_ic": 0.0,
            "min_hac_t": 0.0,
            "min_icir": 0.0,
            "min_monotonicity": 0.0,
            "min_dsr": 0.0,
            "max_p_adj": 1.0,
            "min_positive_fold_ratio": 0.0,
            "min_wfe": 0.0,
            "min_net_sharpe": 0.0,
            "max_drawdown": -1.0,
            "max_turnover": None,
            "min_oos_breadth": 1,
        },
    },
}

_LAYERS = (
    "predictive", "stability", "discrimination",
    "significance", "robustness", "realizability",
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def available_profiles() -> dict[str, str]:
    """给 `flow explain` / 文档用：档位名 → 一句话说明。"""
    return {name: str(spec["description"]) for name, spec in _PROFILE_SPECS.items()}


def resolve_profile(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """从计划读预注册档位；任何覆盖都会取消晋升资格。"""
    block = (plan or {}).get("evaluation") or {}
    if block and not isinstance(block, Mapping):
        raise FlowError(E1004, "evaluation 必须是映射", path="plan.evaluation")
    name = str(block.get("profile") or DEFAULT_PROFILE)
    spec = _PROFILE_SPECS.get(name)
    if spec is None:
        raise FlowError(
            E1004, f"未知 evaluation.profile {name!r}", path="plan.evaluation.profile",
            hint=f"可用: {sorted(_PROFILE_SPECS)}",
        )
    raw_overrides = block.get("thresholds") or {}
    if not isinstance(raw_overrides, Mapping):
        raise FlowError(E1004, "evaluation.thresholds 必须是映射",
                        path="plan.evaluation.thresholds")
    allowed = set(spec["thresholds"])
    unknown = sorted(set(raw_overrides) - allowed)
    if unknown:
        raise FlowError(E1004, f"evaluation.thresholds 含未知键 {unknown}",
                        path="plan.evaluation.thresholds")
    thresholds = dict(spec["thresholds"])
    for key, value in raw_overrides.items():
        if value is None:
            thresholds[key] = None
            continue
        number = _number(value)
        if number is None:
            raise FlowError(E1004, f"evaluation.thresholds.{key} 必须是有限数",
                            path=f"plan.evaluation.thresholds.{key}")
        thresholds[key] = number

    # 判据阈值已在 verdict.params 里冻结，scorecard 不另立第二个真相。
    vparams = ((plan or {}).get("verdict") or {}).get("params") or {}
    if isinstance(vparams, Mapping):
        if (value := _number(vparams.get("dsr_min"))) is not None:
            thresholds["min_dsr"] = value
        if (value := _number(vparams.get("fdr_max"))) is not None:
            thresholds["max_p_adj"] = value

    declared = bool(block)
    locked = declared and not raw_overrides
    return {
        "name": name,
        "description": str(spec["description"]),
        "source": "plan.evaluation" if declared else "implicit_default",
        "locked": locked,
        "promotable": bool(spec["promotable"]) and locked,
        "thresholds": thresholds,
        "digest": compute_digest({
            "kind": "evaluation_profile", "name": name,
            "thresholds": thresholds, "locked": locked,
        }),
    }


def _check(layer: str, name: str, value: float | None, op: str,
           threshold: float | None) -> dict[str, Any]:
    if threshold is None:
        return {"layer": layer, "name": name, "op": op, "value": value,
                "threshold": None, "pass": True, "status": "not_applicable"}
    if value is None:
        return {"layer": layer, "name": name, "op": op, "value": None,
                "threshold": threshold, "pass": False, "status": "missing"}
    passed = {"ge": value >= threshold, "le": value <= threshold,
              "gt": value > threshold}.get(op, False)
    return {"layer": layer, "name": name, "op": op, "value": value,
            "threshold": threshold, "pass": bool(passed),
            "status": "pass" if passed else "fail"}


def _by_id(rows: Any) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str):
            out[str(row["candidate_id"])] = row
    return out


def _horizon(plan: Mapping[str, Any] | None) -> float:
    value = (((plan or {}).get("caliber") or {}).get("label") or {}).get("horizon")
    number = _number(value)
    return number if number is not None and number > 0 else 1.0


def _regime_summary(folds: Any) -> dict[str, Any]:
    """按市场趋势/波动标签汇总 OOS 折；标签是描述量，不参与任何拟合。"""
    buckets: dict[str, dict[str, Any]] = {}
    if not isinstance(folds, list):
        return {"by_trend": {}, "by_vol": {}, "by_year": {}}
    for field, key in (("trend", "by_trend"), ("vol", "by_vol"),
                       ("year", "by_year")):
        for fold in folds:
            if not isinstance(fold, Mapping):
                continue
            regime = fold.get("regime")
            label = regime.get(field) if isinstance(regime, Mapping) else None
            if field == "year" and not isinstance(label, str):
                label = str(fold.get("start", ""))[:4] or None
            if not label:
                continue
            row = buckets.setdefault(key, {}).setdefault(str(label), {
                "folds": 0, "positive_folds": 0, "net_return_sum": 0.0,
            })
            row["folds"] += 1
            net = _number(fold.get("net_return"))
            if net is not None:
                row["net_return_sum"] += net
                if net > 0:
                    row["positive_folds"] += 1
    for key, rows in buckets.items():
        for label, row in rows.items():
            count = int(row["folds"])
            row["positive_fold_ratio"] = round(row["positive_folds"] / count, 6)
            row["mean_net_return"] = round(row["net_return_sum"] / count, 6)
            row.pop("net_return_sum")
    return {"by_trend": buckets.get("by_trend", {}),
            "by_vol": buckets.get("by_vol", {}),
            "by_year": buckets.get("by_year", {})}


def build_scorecard(info: Mapping[str, Any],
                    plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """从 ticket 产物生成 scorecard；只读，不改任何账本。"""
    frozen_plan = (((info.get("frozen") or {}).get("plan") or {}).get("plan") or {})
    effective_plan = plan if plan is not None else frozen_plan
    profile = resolve_profile(effective_plan)
    payloads = info.get("payloads") or {}
    evidence = _by_id((payloads.get("Evidence") or {}).get("per_candidate"))
    verdict = _by_id((payloads.get("Verdict") or {}).get("per_candidate"))
    certify = payloads.get("Certify") or {}
    cert_metrics = certify.get("metrics") or {}
    folds = certify.get("folds_detail") or []
    regimes = _regime_summary(folds)
    thresholds = profile["thresholds"]
    bpy = _number(cert_metrics.get("bars_per_year")) or 365.0
    fold_count = len(folds) if isinstance(folds, list) else 0
    positive_folds = _number(cert_metrics.get("positive_folds"))
    fold_ratio = (positive_folds / fold_count
                  if positive_folds is not None and fold_count > 0 else None)
    oos_net_sharpe = _number(cert_metrics.get("net_sharpe"))
    oos_net_return = _number(cert_metrics.get("net_return"))
    oos_drawdown = _number(cert_metrics.get("max_drawdown"))
    oos_turnover = _number(cert_metrics.get("turnover"))
    # `member_count` 是组合里的因子数；`min_oos_breadth` 才是每期实际持仓的
    # 标的数（allocate.effective_members 的逐折最小值）。用前者判断「单名
    # 押注」会把所有单因子策略误杀，同时放过真正塌成一只票的暴露。
    oos_breadth = _number(cert_metrics.get("min_oos_breadth"))
    oos_sharpe_per_period = (
        oos_net_sharpe / math.sqrt(max(bpy / _horizon(effective_plan), 1.0))
        if oos_net_sharpe is not None else None
    )
    warnings: list[str] = []
    if not profile["locked"]:
        warnings.append("评价阈值被覆盖或计划未显式预注册；scorecard 不具备晋升资格")
    if not isinstance(cert_metrics, Mapping) or not cert_metrics:
        warnings.append("没有 Certify OOS 产物；稳健性与可实现性层不可用")
    if not any(isinstance(fold, Mapping) and isinstance(fold.get("regime"), Mapping)
               for fold in folds if isinstance(fold, Mapping)):
        warnings.append("OOS 折没有 regime 标签；regime 子样本仅按年份汇总")

    ids = sorted(set(evidence) | set(verdict))
    rows: list[dict[str, Any]] = []
    for cid in ids:
        ev = evidence.get(cid, {})
        vd = verdict.get(cid, {})
        metrics = ev.get("metrics") or {}
        stats = vd.get("statistics") or {}
        rank_ic = _number(metrics.get("rank_ic"))
        icir = _number(metrics.get("icir"))
        hac_t = _number(metrics.get("hac_t"))
        mono = _number(metrics.get("monotonicity"))
        is_sr = _number(metrics.get("sr"))
        wfe = (max(0.0, oos_sharpe_per_period / is_sr)
               if is_sr is not None and is_sr > 0 and oos_sharpe_per_period is not None
               else None)
        p_adj = _number(stats.get("p_adj"))
        dsr = _number(stats.get("dsr"))
        decision = str(vd.get("decision") or "")
        checks = [
            _check("predictive", "abs_rank_ic", abs(rank_ic) if rank_ic is not None else None,
                   "ge", thresholds["min_abs_rank_ic"]),
            _check("predictive", "hlz_hac_t", abs(hac_t) if hac_t is not None else None,
                   "ge", thresholds["min_hac_t"]),
            _check("stability", "abs_icir", abs(icir) if icir is not None else None,
                   "ge", thresholds["min_icir"]),
            _check("discrimination", "abs_monotonicity",
                   abs(mono) if mono is not None else None,
                   "ge", thresholds["min_monotonicity"]),
            _check("significance", "p_adj", p_adj, "le", thresholds["max_p_adj"]),
            _check("significance", "dsr", dsr, "ge", thresholds["min_dsr"]),
            _check("robustness", "positive_fold_ratio", fold_ratio, "ge",
                   thresholds["min_positive_fold_ratio"]),
            _check("robustness", "wfe", wfe, "ge", thresholds["min_wfe"]),
            _check("realizability", "oos_net_return", oos_net_return, "gt", 0.0),
            _check("realizability", "oos_net_sharpe", oos_net_sharpe, "ge",
                   thresholds["min_net_sharpe"]),
            _check("realizability", "max_drawdown", oos_drawdown, "ge",
                   thresholds["max_drawdown"]),
            _check("realizability", "turnover", oos_turnover, "le",
                   thresholds["max_turnover"]),
            _check("realizability", "oos_breadth", oos_breadth, "ge",
                   thresholds["min_oos_breadth"]),
        ]
        signs: list[dict[str, Any]] = []
        if rank_ic is not None and icir is not None:
            signs.append({"layer": "stability", "name": "icir_sign_agrees",
                          "pass": rank_ic * icir >= 0})
        if rank_ic is not None and mono is not None:
            signs.append({"layer": "discrimination", "name": "monotonicity_sign_agrees",
                          "pass": rank_ic * mono >= 0})
        checks.extend(signs)
        row_checks = checks + signs
        eligible = (
            profile["promotable"] and decision == "pass"
            and all(item.get("pass") is True for item in row_checks)
        )
        rows.append({
            "candidate_id": cid,
            "decision": decision or "missing",
            "eligible_for_promotion": eligible,
            "rank_ic": rank_ic,
            "hlz_hac_t": hac_t,
            "icir": icir,
            "monotonicity": mono,
            "wfe": wfe,
            "oos_decay": (1.0 - wfe) if wfe is not None else None,
            "is_sharpe_per_period": is_sr,
            "oos_sharpe_per_period": oos_sharpe_per_period,
            "checks": checks,
        })

    layers = []
    for layer in _LAYERS:
        layer_rows = [row for row in rows
                      if any(c.get("layer") == layer for c in row["checks"])]
        layer_pass = [row for row in layer_rows
                      if all(c.get("pass") is True for c in row["checks"]
                             if c.get("layer") == layer)]
        layers.append({"name": layer, "candidate_count": len(layer_rows),
                       "pass_count": len(layer_pass),
                       "status": "pass" if layer_rows and len(layer_pass) == len(layer_rows)
                       else ("unavailable" if not layer_rows else "fail")})

    eligible_ids = [row["candidate_id"] for row in rows if row["eligible_for_promotion"]]
    if eligible_ids:
        overall = "pass"
    elif rows:
        overall = "fail"
    else:
        overall = "unavailable"
    return {
        "schema": SCHEMA,
        "profile": profile,
        "status": overall,
        "eligible_candidates": eligible_ids,
        "layers": layers,
        "candidates": rows,
        "regime_subsamples": regimes,
        "warnings": warnings,
        "no_shopping_contract": {
            "rule": "评价档位在 run 前由 plan.evaluation 冻结；run 后不得因结果改阈值",
            "profile_digest": profile["digest"],
            "post_hoc_threshold_edit_disqualifies": True,
        },
    }


__all__ = ["SCHEMA", "DEFAULT_PROFILE", "available_profiles",
           "resolve_profile", "build_scorecard"]
