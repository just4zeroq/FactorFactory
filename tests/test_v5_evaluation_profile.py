"""预注册 scorecard 的单点守卫：OOS 持仓塌成单名不得晋升。

campaign_r1/r2 的 verdict=pass 候选在 certify 里 `member_count=1` —— 但那个字段
数的是**组合里的因子数**，单因子策略天然就是 1，不能用来判断单名押注。真正的
宽度来自 allocate 的 `effective_members`（逐 bar 非零点数的最小值），经编排层
回填为 `min_oos_breadth`。本测试锁住这个口径，防止再拿因子数冒充持仓宽度。
"""
from __future__ import annotations

from services.evaluation import build_scorecard, resolve_profile

PLAN = {
    "evaluation": {"profile": "strict_institutional_v1"},
    "caliber": {"label": {"horizon": 5}},
}


def _info(breadth: int, *, members: int = 1, with_breadth: bool = True) -> dict:
    cid = "sha256:" + "1" * 64
    cert = {
        "member_count": members,
        "net_return": 0.1,
        "net_sharpe": 1.0,
        "max_drawdown": -0.05,
        "turnover": 0.01,
        "bars_per_year": 365.0,
        "positive_folds": 3,
    }
    if with_breadth:
        cert["min_oos_breadth"] = breadth
    return {
        "frozen": {"plan": {"plan": PLAN}},
        "payloads": {
            "Evidence": {"per_candidate": [{
                "candidate_id": cid,
                "metrics": {
                    "rank_ic": 0.04, "icir": 0.4, "monotonicity": 0.8,
                    "hac_t": 4.0, "sr": 0.2,
                },
            }]},
            "Verdict": {"per_candidate": [{
                "candidate_id": cid, "decision": "pass",
                "statistics": {"p_adj": 0.001, "dsr": 0.99},
            }]},
            "Certify": {
                "metrics": cert,
                "folds_detail": [{"start": "2026-04"} for _ in range(3)],
            },
        },
    }


def test_strict_profile_declares_min_oos_breadth():
    profile = resolve_profile(PLAN)
    assert profile["locked"] is True
    assert profile["promotable"] is True
    assert profile["thresholds"]["min_oos_breadth"] == 5


def test_single_name_oos_is_not_eligible():
    sc = build_scorecard(_info(1, members=8), PLAN)
    assert sc["eligible_candidates"] == []
    row = sc["candidates"][0]
    check = next(c for c in row["checks"] if c["name"] == "oos_breadth")
    assert check["pass"] is False
    assert row["eligible_for_promotion"] is False


def test_eight_names_oos_can_be_eligible_even_with_one_factor():
    """单因子组合不该因为 member_count=1 被误杀。"""
    sc = build_scorecard(_info(8, members=1), PLAN)
    assert sc["status"] == "pass"
    assert sc["eligible_candidates"] == ["sha256:" + "1" * 64]


def test_missing_breadth_evidence_is_not_eligible():
    """老产物没有宽度字段时必须判证据缺失，不能当作通过。"""
    sc = build_scorecard(_info(8, with_breadth=False), PLAN)
    row = sc["candidates"][0]
    check = next(c for c in row["checks"] if c["name"] == "oos_breadth")
    assert check["status"] == "missing"
    assert sc["eligible_candidates"] == []


def test_flat_ticket_shape_is_read_without_manual_wrapping():
    """真实 ticket 的 frozen.plan 是完整 plan dict，"plan" 键只是名字字符串。

    研究期临时脚本手工包成 {frozen:{plan:{plan:<完整 plan>}}} 才让本模块能跑——
    零生产引用直接掩盖了这个结构错误。CLI 用真实 read_ticket 结果时必须能读。
    """
    info = _info(8)
    info["frozen"]["plan"] = {**PLAN, "plan": "confirm_run"}
    sc = build_scorecard(info)  # 不传 plan，走 frozen 读取
    assert sc["profile"]["name"] == "strict_institutional_v1"
    assert sc["profile"]["locked"] is True
    assert len(sc["eligible_candidates"]) == 1
