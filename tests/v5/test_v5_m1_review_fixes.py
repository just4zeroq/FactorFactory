"""S3-M1 review 的回归守卫。

每条测试对应 review 里一条发现：**修完之后必须有一条测试会因它重新出现而变红**。
没有测试的修复只是这一次改对了。
"""

from __future__ import annotations

import numpy as np
import pytest

from v5.capability import Call, InputRef, run
from v5.capability.impls import install_default_impls
from v5.capability.panel import Panel, array_digest
from v5.capability.runtime import reset_impls
from v5.capability.stats import forward_return, trailing_coverage
from v5.capability.stream import REASON_CODES, FieldStream, Pin, Scope
from v5.contracts.digest import compute_digest
from v5.contracts.errors import E1004, E1013, E3003, E4002, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
AXIS = {
    "mine": "2024-01-05..2024-01-20",
    "oos": "2024-01-21..2024-01-25",
    "holdout": "reserved",
}


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


# ---- 构造 ---------------------------------------------------------------
def _panel(t_len: int = 40, n: int = 6, seed: int = 0, holdout=None) -> Panel:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (t_len, n)), axis=0))
    open_ = close * (1 + rng.normal(0, 0.002, (t_len, n)))
    volume = rng.uniform(1e3, 1e4, (t_len, n))
    return Panel(
        times=START + np.arange(t_len, dtype="int64") * DAY_NS,
        symbols=tuple(f"S{i}" for i in range(n)),
        fields={"open": open_, "high": np.maximum(open_, close) * 1.001,
                "low": np.minimum(open_, close) * 0.999, "close": close,
                "volume": volume},
        holdout=holdout,
    )


def _label_call(panel: Panel, policy=None, execution=None, **params):
    base = {"horizon": 3, "rebalance": 3, "entry": "next_close",
            "transform": "none", "neutralise": []}
    base.update(params)
    view: dict = {"label": dict(policy or {})}
    if execution is not None:
        view["execution"] = dict(execution)
    return Call.create(
        capability="label", station="Target", impl="forward_return_cs",
        impl_version="1", params=base,
        inputs=(InputRef("panel", panel.digest(), data=panel),),
        policy_view=view,
    )


def _stream(t_len: int = 30, n: int = 6, end_closed: bool = True,
            close=None, volume=None, lag: int = 0) -> FieldStream:
    times = START + np.arange(t_len, dtype="int64") * DAY_NS
    if close is None:
        close = np.full((t_len, n), 100.0)
    if volume is None:
        volume = np.tile(np.linspace(1e3, 1e4, n), (t_len, 1))
    manifest = {
        "close": {"dataset": "ohlcv", "version": "v1", "unit": "quote",
                  "available_lag": lag},
        "volume": {"dataset": "ohlcv", "version": "v1", "unit": "base",
                   "available_lag": lag},
    }
    return FieldStream(
        times=times, symbols=tuple(f"S{i}" for i in range(n)),
        fields={"close": close, "volume": volume}, manifest=manifest,
        end_closed=end_closed,
    )


def _pin(stream: FieldStream, end_closed: bool = True) -> Pin:
    return Pin(
        dataset="ohlcv", version="v1", checksum=compute_digest({"v": "v1"}),
        coverage=(int(stream.times[0]), int(stream.times[-1]) + DAY_NS),
        symbols_digest="s0", end_closed=end_closed,
    )


def _boundary_call(stream: FieldStream, pin: Pin, **params):
    base = {"k": 3, "tier_window": 1, "min_coverage": 0.5,
            "time_axis": dict(AXIS), "mask_policy": "pit_dynamic"}
    base.update(params)
    return Call.create(
        capability="boundary", station="Boundary", impl="pit_dynamic_topk",
        impl_version="1", params=base,
        inputs=(InputRef("pin", pin.digest(), data=pin),
                InputRef("field_stream", stream.digest(), data=stream)),
    )


def _align_call(stream, scope, runtime_facts=None, **params):
    base = {"drop_unclosed": True, "causal_probe": "strict", "quality_gates": {}}
    base.update(params)
    return Call.create(
        capability="align", station="Panel", impl="point_in_time_align",
        impl_version="1", params=base,
        inputs=(InputRef("field_stream", stream.digest(), data=stream),
                InputRef("scope", scope.digest(), data=scope)),
        runtime_facts=runtime_facts or {},
    )


# ---- label：ref 与策略 --------------------------------------------------
def test_label_ref_is_the_raw_label_score_ref_is_the_transformed_one():
    """两个 ref 指向两个对象：毛标签与变换后的分数。"""
    panel = _panel(t_len=20, n=4)
    out = run(_label_call(panel, transform="cs_rank"))
    raw = forward_return(panel.field("close"), 3, entry_offset=1)
    assert out["label_ref"] == array_digest(raw)
    assert out["score_ref"] != out["label_ref"]


def test_overlap_policy_is_written_back_and_nw_keeps_all_samples():
    panel = _panel(t_len=30, n=4)
    ind = run(_label_call(panel, horizon=6, rebalance=2,
                          overlap_policy="independent_sample"))
    nw = run(_label_call(panel, horizon=6, rebalance=2,
                         overlap_policy="nw_adjusted"))
    assert ind["overlap_policy_used"] == "independent_sample"
    assert nw["overlap_policy_used"] == "nw_adjusted"
    assert nw["effective_n"] > ind["effective_n"], "nw 不改样本，只把调整责任交给下游"


def test_dropped_bars_reasons_sum_to_count():
    panel = _panel(t_len=20, n=4, holdout=(16, 18))
    out = run(_label_call(panel))
    dropped = out["dropped_bars"]
    assert dropped["count"] == sum(dropped["by_reason"].values())
    # horizon=3 + offset=1 → 尾部 4 行没有未来价，其中 2 行同时落在保留区
    assert dropped["by_reason"]["not_estimable"] == 2
    assert dropped["by_reason"]["holdout"] == 2


def test_i61_denominator_excludes_structural_tail():
    """20 根、horizon=6 的正常面板不得被 20% 上限误杀。"""
    panel = _panel(t_len=20, n=6)
    out = run(_label_call(panel, horizon=6, rebalance=6,
                          min_width=6, width_policy="drop"))
    assert out["dropped_bars"]["by_reason"]["width_below_min"] == 0
    assert out["effective_n"] == 13


def test_label_accepts_duration_strings_and_rejects_sub_bar_units():
    panel = _panel(t_len=40, n=4)
    out = run(_label_call(panel, horizon="3bar", rebalance="3bar"))
    assert out["effective_n"] == run(_label_call(panel))["effective_n"]
    assert _code(lambda: run(_label_call(panel, horizon="1h"))) == E1004


def test_label_rejects_entry_that_disagrees_with_caliber():
    panel = _panel()
    assert _code(lambda: run(_label_call(
        panel, entry="next_close", execution={"entry": "close"}))) == E1013
    ok = run(_label_call(panel, entry="next_close",
                         execution={"entry": "next_close"}))
    assert ok["effective_n"] > 0


# ---- boundary：时间轴 ---------------------------------------------------
def test_boundary_rejects_gap_between_mine_and_oos():
    """洞会让 mine 最后一根的标签用上 oos 的价格。"""
    stream = _stream()
    bad = {"mine": "2024-01-05..2024-01-10", "oos": "2024-01-15..2024-01-20"}
    assert _code(lambda: run(_boundary_call(stream, _pin(stream), time_axis=bad))) == E4002


def test_boundary_rejects_end_closed_disagreement():
    stream = _stream(end_closed=True)
    pin = _pin(stream, end_closed=False)
    assert _code(lambda: run(_boundary_call(stream, pin))) == E3003


def test_boundary_rejects_duplicate_tier_edges():
    stream = _stream()
    assert _code(lambda: run(_boundary_call(
        stream, _pin(stream), tier_edges=[0.5, 0.5]))) == E1004


def test_min_coverage_is_causal_not_a_full_sample_statistic():
    """后半段才消失的标的，前半段的有效格不得被整列抹掉。"""
    close = np.full((30, 6), 100.0)
    close[15:, 5] = np.nan
    stream = _stream(close=close)
    out = run(_boundary_call(stream, _pin(stream), tier_window=5,
                             min_coverage=0.5))
    codes = out["reason_codes"]
    assert not (codes[4:15, 5] == REASON_CODES["missing_data"]).any()
    assert (codes[15:25, 5] == REASON_CODES["delisted"]).all()
    assert (codes[25:, 5] == REASON_CODES["reserved"]).all()


# ---- align：报告与门禁 --------------------------------------------------
def test_causal_probe_none_reports_only_what_ran():
    stream = _stream()
    scope = run(_boundary_call(stream, _pin(stream)))["scope"]
    out = run(_align_call(stream, scope, causal_probe="none"))
    report = out["causal_report"]
    assert [c["id"] for c in report["checks"]] == ["P1", "P2", "P3"]
    assert report["probe"]["mode"] == "none"
    assert report["violations"] == []


def test_align_rejects_holdout_values_inside_the_panel():
    stream = _stream()
    base = run(_boundary_call(stream, _pin(stream)))["scope"]
    axis = dict(base.axis_resolved)
    axis["holdout"] = {"start": int(stream.times[10]), "end": int(stream.times[15])}
    leaky = Scope(
        times=base.times, symbols=base.symbols,
        observable_mask=base.observable_mask, tradable_mask=base.tradable_mask,
        tiers=base.tiers, reason_codes=base.reason_codes, axis_resolved=axis,
    )
    assert _code(lambda: run(_align_call(stream, leaky))) == E3003


def test_align_reads_holdout_visibility_from_runtime_facts():
    stream = _stream()
    scope = run(_boundary_call(stream, _pin(stream)))["scope"]
    ok = run(_align_call(stream, scope,
                         runtime_facts={"holdout_visibility": "revealed"}))
    assert ok["holdout_visibility"] == "revealed"
    assert _code(lambda: run(_align_call(
        stream, scope, runtime_facts={"holdout_visibility": "yes"}))) == E3003


# ---- 值对象不变量 -------------------------------------------------------
def test_panel_digest_covers_holdout():
    panel = _panel(t_len=10, n=2)
    same = Panel(times=panel.times, symbols=panel.symbols,
                 fields=panel.fields, holdout=(3, 6))
    assert same.digest() != panel.digest()


def test_stream_digest_covers_manifest():
    base = _stream(t_len=10, n=2)
    other = _stream(t_len=10, n=2, lag=1)
    assert base.digest() != other.digest()


def test_scope_digest_covers_axis_resolved():
    stream = _stream()
    base = run(_boundary_call(stream, _pin(stream)))["scope"]
    moved = dict(base.axis_resolved)
    moved["mine"] = {"start": int(stream.times[0]), "end": int(stream.times[3])}
    other = Scope(
        times=base.times, symbols=base.symbols,
        observable_mask=base.observable_mask, tradable_mask=base.tradable_mask,
        tiers=base.tiers, reason_codes=base.reason_codes, axis_resolved=moved,
    )
    assert base.digest() != other.digest()


def test_panel_times_are_readonly():
    panel = _panel(t_len=5, n=2)
    with pytest.raises(ValueError):
        panel.times[0] = 0


def test_reason_codes_out_of_range_are_rejected_before_narrowing():
    times = START + np.arange(5, dtype="int64") * DAY_NS
    shape = (5, 2)
    codes = np.zeros(shape, dtype=np.int64)
    codes[0, 0] = 256          # 窄化后会变成 0（valid）
    with pytest.raises(FlowError) as exc:
        Scope(
            times=times, symbols=("a", "b"),
            observable_mask=np.ones(shape, bool),
            tradable_mask=np.ones(shape, bool),
            tiers=np.zeros(shape, np.int64), reason_codes=codes,
            axis_resolved={"mine": {}},
        )
    assert exc.value.error.code == E3003


def test_trailing_coverage_divides_by_elapsed_bars_not_valid_count():
    mask = np.zeros((10, 1), dtype=bool)
    mask[4, 0] = True
    cover = trailing_coverage(mask, 5)
    assert cover[4, 0] == pytest.approx(1 / 5), "分母是本该有的根数，不是有效计数"
    assert cover[0, 0] == pytest.approx(0.0)


# ---- M1 端到端（不含 read） ---------------------------------------------


# ---- M1 端到端（不含 read） ---------------------------------------------
def test_boundary_align_label_chain_runs_end_to_end():
    """三个能力串起来：边界 → 面板 → 标签，掩码与行数必须自洽。"""
    stream = _stream(t_len=40, n=6)
    pin = _pin(stream)
    scope_out = run(_boundary_call(stream, pin, time_axis={
        "mine": "2024-01-05..2024-01-25",
        "oos": "2024-01-26..2024-02-05",
        "holdout": "reserved",
    }))
    scope = scope_out["scope"]
    aligned = run(_align_call(stream, scope))
    panel = aligned["panel"]

    assert panel.shape[0] == aligned["shape"]["bars"]
    assert panel.shape[1] == 6
    # 面板行上的掩码与 Scope 的裁剪必须一致：面板不含 reserved 时点
    reserved = scope.reason_codes == REASON_CODES["reserved"]
    kept_rows = np.isin(scope.times, panel.times)
    assert not (reserved.any(axis=1) & kept_rows).any()

    label = run(_label_call(panel, horizon=3, rebalance=3))
    assert label["effective_n"] > 0
    assert int(label["valid_mask"].sum()) == label["effective_n"]
    assert panel.shape[0] == label["score"].shape[0]
