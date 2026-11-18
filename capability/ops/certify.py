"""能力实现 · certify：认证（walk-forward + 可复现清单）。

这是八步里**唯一**允许产出 `out_of_sample` 结论的工位（s0/04 §3.12 / 06 §8.1）。
四条不可动摇的边界：

1. **只认证「未参与搜索」的那段**。`oos_window` 必须严格晚于组合层的
   `windows_used.eval.end`（I24 / AC25）。组合层自己在样本内的净值是诊断量，
   拿它当结论就是把「构造是否可行」说成「能不能交易」。
2. **折是 walk-forward 的折，不是随机 K 折**。时间序列只有前向可用：第 i 折
   之前的数据可以训练，之后的不行。折间留 `embargo` 根 bar 的隔离段，宽度
   由参数给、不由实现猜（标签重叠会跨折泄漏，06 §7）。
3. **判据不自算**。返回的 `verdict_refs` 引用 Sentinel 的裁决结果；认证段只
   复算**净表现**，不另起一套显著性口径。
4. **可复现清单只放确定性事实**。策略、口径、面板、分数、域、折参数与种子
   都进清单；墙钟时间、路径、耗时一律不进（否则同一份结论两次认证指纹不同）。

本模块只做三件事：解析窗口、切折、装配清单。逐折的持仓与净值由**编排层**
驱动 `allocate` / `backtest` 子调用算出后回填（s0/04 §1.3：能力不自建子调用）。
因此 `metrics` 里的净表现字段此处恒为空壳——本模块不给自己发一张没有回测的
合格证。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Mapping

import numpy as np

from capability.panel import Panel, array_digest
from contracts.digest import compute_digest
from contracts.errors import E1003, E1004, E1021, E3003, FlowError

from .backtest import bars_per_year

_NS_PER_DAY = 86_400 * 1_000_000_000
_DECISIONS = frozenset({"pass", "watch"})


# ---------------------------------------------------------------- 输入
def _input(call, kind: str) -> Any:
    for item in call.inputs:
        if item is not None and item.kind == kind:
            return item.data
    raise FlowError(E3003, f"certify 缺输入 {kind!r}", path=f"certify.inputs.{kind}")


def _int_param(params: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = params.get(key)
    if value is None:
        raise FlowError(E1003, f"certify.params 缺 {key}", path=f"certify.params.{key}")
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise FlowError(
            E1004, f"certify.params.{key} 必须是整数，得到 {type(value).__name__}",
            path=f"certify.params.{key}",
        )
    out = int(value)
    if out < minimum:
        raise FlowError(
            E1004, f"certify.params.{key}={out} 必须 >= {minimum}",
            path=f"certify.params.{key}",
        )
    return out


def _day(value: Any, *, path: str) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise FlowError(E1004, f"{path} 不是 YYYY-MM-DD 日期: {value!r}", path=path) from exc


def _window(params: Mapping[str, Any]) -> tuple[date, date]:
    """`oos_window` 只接受 `{start, end}` 映射（s0/04 §3.12）。

    不接受 `"A..B"` 字符串：那是计划里 `time_axis` 的字面量语法，由 CLI 展开层
    解析成两个日期。能力层若也认两种形状，「参数到底传了什么」就要看两处代码。
    """
    raw = params.get("oos_window")
    if not isinstance(raw, Mapping):
        raise FlowError(
            E1003, "certify.params.oos_window 必须是 {start, end}",
            path="certify.params.oos_window",
        )
    start = _day(raw.get("start"), path="certify.params.oos_window.start")
    end = _day(raw.get("end"), path="certify.params.oos_window.end")
    if start > end:
        raise FlowError(
            E1021, f"certify.oos_window 起止倒置: {start} > {end}",
            path="certify.params.oos_window",
        )
    return start, end


def _dates(times: np.ndarray) -> np.ndarray:
    """纳秒时间轴 → UTC 日期数组（`numpy.datetime64[D]`，可直接比较）。"""
    return (times // _NS_PER_DAY).astype("datetime64[D]")


def _bounds(times: np.ndarray, start: date, end: date) -> tuple[int, int]:
    """把闭区间日期窗口解析成半开 bar 索引 `[lo, hi)`。"""
    days = _dates(times)
    lo_key = np.datetime64(start.isoformat(), "D")
    hi_key = np.datetime64(end.isoformat(), "D")
    lo = int(np.searchsorted(days, lo_key, side="left"))
    hi = int(np.searchsorted(days, hi_key, side="right"))
    if lo >= hi:
        raise FlowError(
            E3003,
            f"certify 的 oos_window {start}..{end} 在面板内没有可用 bar",
            path="certify.params.oos_window",
            hint="检查面板覆盖与 time_axis.oos；空窗口不能认证出结论",
        )
    # 越界 = 供给不足，不是「刚好截断」：面板之外的 bar 不存在，悄悄缩短窗口
    # 会让「认证段长度」与计划声明不一致。窗口内部缺一根 bar 不算越界——
    # 那是缺口，`bars_per_year` 用中位步长吸收它。
    if lo == 0 and days[0] > lo_key:
        raise FlowError(
            E3003,
            f"certify 的 oos_window 起点 {start} 早于面板覆盖（首根 {days[0]}）",
            path="certify.params.oos_window",
        )
    if hi == times.size and days[hi - 1] < hi_key:
        raise FlowError(
            E3003,
            f"certify 的 oos_window 末端 {end} 超出面板覆盖（末根 {days[hi - 1]}）",
            path="certify.params.oos_window",
        )
    return lo, hi


def _split(lo: int, hi: int, folds: int, embargo: int) -> list[tuple[int, int]]:
    """把 `[lo, hi)` 切成 `folds` 段，段间留 `embargo` 根隔离 bar。

    折宽尽量均匀（余数给前几折）；`hi` 恰好落在最后一折末端——窗口边界是
    计划给的，不由实现向下取整缩短。
    """
    usable = (hi - lo) - embargo * (folds - 1)
    if usable < folds * 2:
        raise FlowError(
            E3003,
            f"certify 的 OOS 窗口只有 {hi - lo} 根 bar，切不出 {folds} 折"
            f"（每折至少 2 根 + 间隔 {embargo}）",
            path="certify.params.folds",
            hint="减少 folds/embargo，或扩大 time_axis.oos",
        )
    base, rem = divmod(usable, folds)
    out: list[tuple[int, int]] = []
    cursor = lo
    for i in range(folds):
        width = base + (1 if i < rem else 0)
        out.append((cursor, cursor + width))
        cursor += width + embargo
    if out[-1][1] != hi:
        # 构造保证相等；不等说明切分公式被改坏了，不能带着错折继续认证。
        raise FlowError(E3003, "certify 折切分未覆盖到窗口末端", path="certify.params")
    return out


def _portfolio(call, panel: Panel) -> tuple[dict[str, Any], np.ndarray]:
    raw = _input(call, "portfolio")
    if not isinstance(raw, Mapping):
        raise FlowError(E3003, "certify.portfolio 必须是映射", path="certify.inputs.portfolio")
    score = raw.get("score")
    if not isinstance(score, np.ndarray) or score.ndim != 2:
        raise FlowError(
            E3003, "certify 的 portfolio.score 必须是二维暴露",
            path="certify.inputs.portfolio.score",
        )
    if score.shape != panel.shape:
        raise FlowError(
            E3003,
            f"certify 的 portfolio.score 形状 {score.shape} 与面板 {panel.shape} 不符",
            path="certify.inputs.portfolio.score",
        )
    grade = str(raw.get("evidence_grade") or "")
    if grade != "in_sample":
        # 组合层必须自报「样本内诊断」；缺失或已是 out_of_sample 都说明
        # 上游产物被改过或接错了链，认证不能据此发合格证。
        raise FlowError(
            E1021,
            f"certify 只认证样本内组合的复算，portfolio.evidence_grade={grade!r}",
            path="certify.inputs.portfolio.evidence_grade",
        )
    members = raw.get("members")
    if not isinstance(members, list) or not members:
        raise FlowError(
            E3003, "certify 的 portfolio.members 为空", path="certify.inputs.portfolio.members",
        )
    return dict(raw), np.asarray(score, dtype="float64")


def _windows_used(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    win = portfolio.get("windows_used")
    if not isinstance(win, Mapping):
        raise FlowError(
            E3003, "certify 的 portfolio.windows_used 缺失",
            path="certify.inputs.portfolio.windows_used",
        )
    return dict(win)


def _verdict_refs(verdict_set: Any, members: list[Any]) -> list[dict[str, Any]]:
    """只引用成员候选的裁决：证书说「这组人过了判据」，不是「全库都过了」。"""
    if not isinstance(verdict_set, Mapping):
        raise FlowError(
            E3003, "certify 的 verdict_set 必须是映射", path="certify.inputs.verdict_set",
        )
    rows = verdict_set.get("per_candidate")
    if not isinstance(rows, list) or not rows:
        raise FlowError(
            E3003, "certify 的 verdict_set.per_candidate 为空",
            path="certify.inputs.verdict_set.per_candidate",
        )
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("candidate_id"), str):
            by_id[str(row["candidate_id"])] = row
    refs: list[dict[str, Any]] = []
    for member in members:
        cid = str((member or {}).get("candidate_id", "")) if isinstance(member, Mapping) else ""
        row = by_id.get(cid)
        if row is None:
            raise FlowError(
                E3003, f"certify 的成员 {cid!r} 没有裁决记录",
                path="certify.inputs.verdict_set",
                hint="组合成员必须来自 Sentinel 的裁决集合",
            )
        decision = str(row.get("decision", ""))
        if decision not in _DECISIONS:
            raise FlowError(
                E1021, f"certify 的成员 {cid!r} 裁决为 {decision!r}，不可认证",
                path="certify.inputs.verdict_set",
            )
        refs.append({
            "candidate_id": cid,
            "decision": decision,
            "family": str(row.get("family", "")),
            "n_trials": row.get("n_trials"),
        })
    return refs


def _manifest(call, *, panel: Panel, score: np.ndarray, portfolio: Mapping[str, Any],
              folds: int, embargo: int, window: tuple[date, date]) -> dict[str, Any]:
    """可复现清单：只放确定性事实（时间/路径/耗时一律不进）。"""
    return {
        "schema": "factorfactory.certificate_manifest@1",
        # 能力层看得见的是**口径视图**，不是 policy_digest 本体：
        # `dg.policy_digest = H(baseline_version, constitutive_subset, caliber_digest,
        # domain_digest)`，前两项在 Call 里根本没有通道。把 `H(policy_view)` 冠名
        # `policy_digest` 会让证书永远无法与 ticket 对账（同名不同值，且不报错）。
        # 所以这里如实叫 `policy_view_digest`；真正的 `policy_digest` 由编排层
        # 在 `_run_certify_folds` 里回填（只有它有 ticket 口径）。
        "policy_view_digest": compute_digest({
            "kind": "certify_policy_view",
            "view": dict(call.policy_view),
        }),
        "panel_digest": panel.digest(),
        "score_digest": array_digest(score),
        "domain_digest": str(portfolio.get("domain_digest", "")),
        "member_ids": [str((m or {}).get("candidate_id", ""))
                       for m in (portfolio.get("members") or [])],
        "folds": folds,
        "embargo": embargo,
        "oos_window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "seed": int(call.seed),
        "impl": f"{call.impl}@{call.impl_version}",
    }


def build(call) -> dict:
    params = call.params
    folds = _int_param(params, "folds", minimum=1)
    # `embargo` 是 duration(unit=bar)：certify 的口径单位是 bar，不是日历日。
    embargo = _int_param(params, "embargo", minimum=0)
    window = _window(params)

    panel = _input(call, "panel")
    if not isinstance(panel, Panel):
        raise FlowError(E3003, "certify.panel 必须是 Panel", path="certify.inputs.panel")
    portfolio, score = _portfolio(call, panel)
    tradable = _input(call, "tradable_mask")
    tiers = _input(call, "tiers")
    for name, value in (("tradable_mask", tradable), ("tiers", tiers)):
        arr = np.asarray(value)
        if arr.shape != panel.shape:
            raise FlowError(
                E3003,
                f"certify 的 {name} 形状 {arr.shape} 与面板 {panel.shape} 不符",
                path=f"certify.inputs.{name}",
            )

    used = _windows_used(portfolio)
    eval_end = _day((used.get("eval") or "").split("..")[-1] if isinstance(used.get("eval"), str)
                    else used.get("eval"), path="certify.inputs.portfolio.windows_used.eval")
    if window[0] <= eval_end:
        raise FlowError(
            E1021,
            f"certify 的 oos_window 起点 {window[0]} 不晚于组合 eval 末端 {eval_end}",
            path="certify.params.oos_window",
            hint="认证段必须与组合评估段不重叠（I24）：同一段数据不能既诊断又发结论",
        )

    lo, hi = _bounds(panel.times, *window)
    spans = _split(lo, hi, folds, embargo)
    days = _dates(panel.times)
    bpy, bpy_source = bars_per_year(np.asarray(panel.times[lo:hi], dtype="int64"), hi - lo)

    folds_detail: list[dict[str, Any]] = []
    for idx, (b0, b1) in enumerate(spans, start=1):
        folds_detail.append({
            "fold": idx,
            "start_index": int(b0),
            "end_index": int(b1),
            "start": str(days[b0]),
            "end": str(days[b1 - 1]),
            "n_bars": int(b1 - b0),
        })

    refs = _verdict_refs(_input(call, "verdict_set"), list(portfolio.get("members") or []))
    # 结构事实先写全；净表现字段由编排层逐折回填（本模块无回测能力，
    # 也不该有——两套成交逻辑就是两套口径）。
    metrics: dict[str, Any] = {
        "fold_count": folds,
        "n_bars": int(sum(b1 - b0 for b0, b1 in spans)),
        "embargo_bars": embargo,
        "bars_per_year": float(bpy),
        "bars_per_year_source": bpy_source,
        "oos_window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "member_count": len(refs),
        "net_return": None,
        "net_sharpe": None,
        "turnover": None,
        "max_drawdown": None,
        "cost_breakdown": {},
    }
    if not math.isfinite(float(bpy)):
        raise FlowError(E3003, "certify 的 bars_per_year 非有限值", path="certify.params")
    return {
        "metrics": metrics,
        "folds_detail": folds_detail,
        "reproducible_manifest": _manifest(
            call, panel=panel, score=score, portfolio=portfolio,
            folds=folds, embargo=embargo, window=window,
        ),
        "verdict_refs": refs,
        "evidence_grade": "out_of_sample",
    }


__all__ = ["build"]
