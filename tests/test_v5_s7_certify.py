"""S7：`certify` 认证能力的单元测试。

直接走 `Call.create + run`，因此 params / inputs / outputs 六步契约也在覆盖内。
四条判据对应设计里的硬约束：

1. **折覆盖整段 OOS 且折间留 embargo**（06 §7/§8）——折不是随机 K 折；
2. **认证段与组合 eval 段不得重叠**（I24 / AC25）——否则「样本内诊断」被当结论；
3. **判据不自算**：`verdict_refs` 只引用成员，且必须是 pass/watch（AC27）；
4. **可复现清单只含确定性事实**：同一输入两次认证，清单逐字节相同。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.panel import Panel, array_digest
from capability.runtime import reset_impls
from contracts.errors import E1003, E1021, E3003, FlowError

_CERT_IMPL = "walk_forward"
_BARS = 60
_SYMS = ("A", "B", "C", "D")
_NS_DAY = 86_400_000_000_000


def _code(fn) -> str | None:
    try:
        fn()
    except FlowError as exc:
        return exc.error.code
    return None


@pytest.fixture(autouse=True)
def _impls():
    reset_impls()
    install_default_impls()
    yield
    reset_impls()


def _times(n: int = _BARS) -> np.ndarray:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return np.array([int((start + timedelta(days=i)).timestamp()) * 1_000_000_000
                     for i in range(n)], dtype="int64")


def _panel(rng: np.random.Generator) -> Panel:
    n = len(_times())
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=(n, len(_SYMS))), axis=0))
    return Panel(times=_times(), symbols=_SYMS, fields={"close": px})


def _fixture(*, folds=2, embargo=1, oos=("2024-02-06", "2024-02-29"),
             members=("c1",), decisions=("pass",), grade="in_sample",
             eval_end="2024-02-05"):
    rng = np.random.default_rng(7)
    panel = _panel(rng)
    score = rng.normal(size=(panel.shape[0], len(_SYMS)))
    tradable = np.ones(panel.shape, dtype=bool)
    tiers = np.zeros(panel.shape, dtype=int)
    portfolio = {
        "score": score,
        "members": [{"candidate_id": cid, "weight_share": 1.0 / len(members)}
                    for cid in members],
        "member_weights": np.full(len(members), 1.0 / len(members)),
        "domain_digest": "sha256:" + "d" * 64,
        "windows_used": {"fit": "2024-01-05..2024-01-20", "embargo": 1,
                         "eval": f"2024-01-25..{eval_end}"},
        "evidence_grade": grade,
    }
    verdict_set = {"per_candidate": [
        {"candidate_id": cid, "decision": dec, "family": "fam", "n_trials": 5}
        for cid, dec in zip(members, decisions, strict=True)
    ]}
    params = {
        "folds": folds, "embargo": embargo,
        "oos_window": {"start": f"{oos[0]}T00:00:00Z", "end": f"{oos[1]}T00:00:00Z"},
    }
    return panel, portfolio, verdict_set, tradable, tiers, score, params


def _call(panel, portfolio, verdict_set, tradable, tiers, params, **over):
    call = Call.create(
        capability="certify", station="Certify", impl=_CERT_IMPL, impl_version="1",
        params=params,
        inputs=(
            InputRef("portfolio", array_digest(portfolio["score"]), data=portfolio),
            InputRef("panel", panel.digest(), data=panel),
            InputRef("tradable_mask", array_digest(tradable), data=tradable),
            InputRef("tiers", array_digest(tiers), data=tiers),
            InputRef("verdict_set", array_digest(np.zeros(1)), data=verdict_set),
        ),
        policy_view={"allocate": {"quantiles": 3}, "net": {"cost_tiers": [10]}},
        runtime_facts={"holdout_visibility": "reserved"},
        seed=11,
    )
    if over:
        call = Call.create(
            capability="certify", station="Certify", impl=_CERT_IMPL, impl_version="1",
            params={**params, **over}, inputs=call.inputs,
            policy_view=dict(call.policy_view),
            runtime_facts=dict(call.runtime_facts), seed=call.seed,
        )
    return call


# ---------------------------------------------------------------- 折语义
def test_folds_cover_oos_with_embargo_gaps():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(folds=2, embargo=1)
    out = run(_call(panel, portfolio, verdict, tradable, tiers, params))

    folds = out["folds_detail"]
    assert len(folds) == 2
    # 折间空出 1 根隔离 bar：第二折起点 = 第一折终点 + embargo
    assert folds[1]["start_index"] == folds[0]["end_index"] + 1
    # 折覆盖到窗口末端，没有向下取整缩短认证段
    assert folds[-1]["end_index"] == 60  # 2024-02-29 是最後一根 (索引 59) 的下一个
    assert folds[0]["start_index"] == 36  # 2024-02-06
    # 总 bar 数 = 窗口 24 根 − 1 根 embargo
    assert sum(f["n_bars"] for f in folds) == 24 - 1
    assert out["metrics"]["n_bars"] == 23
    assert out["metrics"]["embargo_bars"] == 1


def test_single_fold_has_no_gap():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(
        folds=1, embargo=1)
    out = run(_call(panel, portfolio, verdict, tradable, tiers, params))
    assert len(out["folds_detail"]) == 1
    assert out["folds_detail"][0]["n_bars"] == 24


def test_too_many_folds_for_window_fails_closed():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(
        folds=40, embargo=1)
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params))) \
        == E3003


# ---------------------------------------------------------------- 窗口合法
def test_oos_overlapping_portfolio_eval_is_rejected():
    # eval 末端 02-20 晚于 oos 起点 02-06 → 同一段数据既诊断又发结论
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(eval_end="2024-02-20")
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params))) \
        == E1021


def test_oos_outside_panel_coverage_is_rejected():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(
        oos=("2024-02-06", "2024-03-05"))
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params))) \
        == E3003


# ---------------------------------------------------------------- 判据引用
def test_member_without_verdict_record_is_rejected():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(
        members=("c1", "c2"), decisions=("pass", "pass"))
    verdict["per_candidate"] = verdict["per_candidate"][:1]
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params))) \
        == E3003


def test_rejected_member_cannot_be_certified():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(
        members=("c1",), decisions=("reject",))
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params))) \
        == E1021


def test_verdict_refs_only_cover_members():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(
        members=("c1", "c2"), decisions=("pass", "watch"))
    verdict["per_candidate"].append(
        {"candidate_id": "c3", "decision": "pass", "family": "fam", "n_trials": 5})
    out = run(_call(panel, portfolio, verdict, tradable, tiers, params))
    assert {ref["candidate_id"] for ref in out["verdict_refs"]} == {"c1", "c2"}


# ---------------------------------------------------------------- 等级与清单
def test_portfolio_must_declare_in_sample():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture(grade="out_of_sample")
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params))) \
        == E1021


def test_certificate_is_out_of_sample_and_manifest_is_deterministic():
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture()
    first = run(_call(panel, portfolio, verdict, tradable, tiers, params))
    second = run(_call(panel, portfolio, verdict, tradable, tiers, params))
    assert first["evidence_grade"] == "out_of_sample"
    assert first["reproducible_manifest"] == second["reproducible_manifest"]
    manifest = first["reproducible_manifest"]
    assert manifest["folds"] == 2 and manifest["embargo"] == 1
    # 能力层只能给口径**视图**摘要；`policy_digest` 本体需要 baseline_version
    # 与 constitutive_subset（Call 里没有通道），由编排层回填。
    assert manifest["policy_view_digest"].startswith("sha256:")
    assert "policy_digest" not in manifest
    assert manifest["panel_digest"] == panel.digest()
    assert manifest["oos_window"] == {"start": "2024-02-06", "end": "2024-02-29"}
    assert manifest["member_ids"] == ["c1"]
    # 净表现此刻仍是空壳：能力不给自己发一张没回测的合格证
    assert first["metrics"]["net_return"] is None
    assert first["metrics"]["net_sharpe"] is None


def test_holdout_in_params_is_rejected():
    """`certify.params.holdout` 不是「配了没用」，而是配了就错（C16）。"""
    panel, portfolio, verdict, tradable, tiers, _, params = _fixture()
    assert _code(lambda: run(_call(panel, portfolio, verdict, tradable, tiers, params,
                                   holdout="reserved"))) == E1003
