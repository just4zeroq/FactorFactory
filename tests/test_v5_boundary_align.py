"""S3-M1：`boundary` 边界构建与 `align` 面板对齐。"""

from __future__ import annotations

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.registry import default_registry
from capability.runtime import reset_impls
from capability.stream import (
    REASON_CODES, FieldStream, Pin, Scope,
)
from contracts.digest import compute_digest
from contracts.errors import E1004, E1008, E3003, E4002, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
AXIS = {
    "mine": "2024-01-05..2024-01-20",
    "oos": "2024-01-21..2024-01-25",
    "holdout": "reserved",
}
# 覆盖到数据末端：用来测「未收盘的是供给末端那一根」
AXIS_FULL = {
    "mine": "2024-01-05..2024-01-20",
    "oos": "2024-01-21..2024-01-30",
    "holdout": "reserved",
}
# 覆盖到数据末端：用来测「未收盘的是供给末端那一根」
AXIS_FULL = {
    "mine": "2024-01-05..2024-01-20",
    "oos": "2024-01-21..2024-01-30",
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


def _stream(t_len: int = 30, n: int = 6, end_closed: bool = True,
            classes: dict | None = None, close=None, volume=None,
            lag: int = 0) -> FieldStream:
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
        times=times,
        symbols=tuple(f"S{i}" for i in range(n)),
        fields={"close": close, "volume": volume},
        manifest=manifest,
        end_closed=end_closed,
        symbol_classes=classes or {},
    )


def _pin(stream: FieldStream, end_closed: bool = True) -> Pin:
    return Pin(
        dataset="ohlcv", version="v1", checksum=compute_digest({"v": "v1"}),
        coverage=(int(stream.times[0]), int(stream.times[-1]) + DAY_NS),
        symbols_digest="s0", end_closed=end_closed,
    )


def _boundary_call(stream: FieldStream, pin: Pin, **params):
    base = {
        "k": 3, "tier_window": 1, "min_coverage": 0.5,
        "time_axis": dict(AXIS), "mask_policy": "pit_dynamic",
    }
    base.update(params)
    return Call.create(
        capability="boundary", station="Boundary", impl="pit_dynamic_topk",
        impl_version="1", params=base,
        inputs=(
            InputRef("pin", pin.digest(), data=pin),
            InputRef("field_stream", stream.digest(), data=stream),
        ),
    )


def _scope(stream: FieldStream | None = None, pin: Pin | None = None,
           **params) -> tuple[Scope, dict]:
    stream = stream or _stream()
    out = run(_boundary_call(stream, pin or _pin(stream), **params))
    return out["scope"], out


def _align_call(stream: FieldStream, scope: Scope, **params):
    base = {"drop_unclosed": True, "causal_probe": "strict",
            "quality_gates": {}}
    base.update(params)
    return Call.create(
        capability="align", station="Panel", impl="point_in_time_align",
        impl_version="1", params=base,
        inputs=(
            InputRef("field_stream", stream.digest(), data=stream),
            InputRef("scope", scope.digest(), data=scope),
        ),
    )


# ---- boundary：时间轴 ---------------------------------------------------


def test_time_axis_uses_the_single_timeaxis_implementation():
    """E7：区间展开不能再有第二份实现。"""
    from capability.timeaxis import resolve_interval

    stream = _stream()
    _, out = _scope(stream)
    axis = out["axis_resolved"]
    lo, hi = resolve_interval(AXIS["mine"], stream.times, path="t")
    assert axis["mine"] == {"start": int(stream.times[lo]), "end": int(stream.times[hi - 1])}


def test_millisecond_time_axis_is_rejected_not_silently_misread():
    """旧解析器没有 1900–2100 sanity：毫秒时间戳会被读成 1970 年，
    区间落不到任何 bar，最终以「空结果」的面目出现而不是明确报错。"""
    stream = _stream()
    bad = {"mine": "1704067200000..1704662400000", "oos": "1704662400001..1705257600000"}
    assert _code(lambda: _scope(stream, time_axis=bad)) == E4002


def test_observable_codes_derive_from_the_legend():
    """G9：可观测码从 `REASON_CODES` 派生，不在调用方抄 [0,5,6,7]。"""
    from capability.stream import OBSERVABLE_CODES, REASON_CODES

    assert OBSERVABLE_CODES == frozenset({0, 5, 6, 7})
    assert OBSERVABLE_CODES == frozenset(REASON_CODES.values()) - frozenset(
        REASON_CODES[k] for k in ("reserved", "not_listed", "delisted", "missing_data")
    )
    assert REASON_CODES["low_liquidity"] in OBSERVABLE_CODES
    assert REASON_CODES["low_liquidity"] not in frozenset({0, 5, 7})

def test_boundary_resolves_time_axis_to_timestamps():
    stream = _stream()
    _, out = _scope(stream)
    axis = out["axis_resolved"]
    assert axis["mine"] == {"start": int(stream.times[4]), "end": int(stream.times[19])}
    assert axis["oos"] == {"start": int(stream.times[20]), "end": int(stream.times[24])}
    assert axis["holdout"] == "reserved"


def test_boundary_marks_reserved_as_the_complement_of_the_run_window():
    codes = _scope()[0].reason_codes
    reserved = REASON_CODES["reserved"]
    assert (codes[:4] == reserved).all(), "mine 之前的时点属于保留（不参与本次运行）"
    assert (codes[25:] == reserved).all(), "oos 之后的时点同样不参与"
    assert (codes[4:25] != reserved).all()


def test_boundary_rejects_overlapping_mine_and_oos():
    stream = _stream()
    bad = {"mine": "2024-01-05..2024-01-20", "oos": "2024-01-10..2024-01-25"}
    assert _code(lambda: _scope(stream, time_axis=bad)) == E4002


def test_boundary_rejects_span_without_any_bar():
    stream = _stream()
    bad = {"mine": "2023-01-01..2023-01-02", "oos": "2023-01-03..2023-01-04"}
    assert _code(lambda: _scope(stream, time_axis=bad)) == E4002


def test_boundary_rejects_holdout_overlapping_the_run_window():
    stream = _stream()
    bad = {"mine": "2024-01-05..2024-01-20", "oos": "2024-01-21..2024-01-25",
           "holdout": "2024-01-10..2024-01-12"}
    assert _code(lambda: _scope(stream, time_axis=bad)) == E4002


# ---- boundary：两张掩码 -------------------------------------------------
def test_unclosed_bar_is_observable_but_not_tradable():
    stream = _stream(end_closed=False)
    scope, out = _scope(stream, pin=_pin(stream, end_closed=False),
                        time_axis=AXIS_FULL)
    tail = 29
    assert out["observable_mask"][tail].all()
    assert not out["tradable_mask"][tail].any(), "未收盘 bar 不得下权重"
    assert (out["reason_codes"][tail] == REASON_CODES["unclosed"]).all()


def test_unclosed_bar_outside_the_run_window_is_not_marked():
    """未收盘的是数据末端；窗口没覆盖到它时不该把窗口末根当半成品。"""
    stream = _stream(end_closed=False)
    scope, out = _scope(stream, pin=_pin(stream, end_closed=False))
    assert not (out["reason_codes"] == REASON_CODES["unclosed"]).any()


def test_topk_marks_low_liquidity_without_hiding_observability():
    scope, out = _scope()
    row = 10
    assert int(out["tradable_mask"][row].sum()) == 3
    assert out["observable_mask"][row].all(), "流动性不足仍可被观测"
    assert int((out["reason_codes"][row] == REASON_CODES["low_liquidity"]).sum()) == 3


def test_stable_or_leveraged_is_tradable_never():
    stream = _stream(classes={"S0": "stable", "S1": "leveraged"})
    scope, out = _scope(stream)
    for j in (0, 1):
        assert not out["tradable_mask"][4:25, j].any()
        assert out["observable_mask"][4:25, j].all()
        assert (out["reason_codes"][4:25, j] == REASON_CODES["stable_or_leveraged"]).all()


def test_min_coverage_drops_a_thin_symbol_from_the_pool():
    stream = _stream()
    close = np.full_like(stream.field("close"), 100.0)
    close[:, 5] = np.nan
    close[4:25:5, 5] = 100.0          # 每 5 根 1 根 → trailing 覆盖率 0.2 < 0.5
    thin = _stream(close=close)
    scope, out = _scope(thin, tier_window=5)
    assert (out["reason_codes"][4:25, 5] == REASON_CODES["missing_data"]).all()
    assert not out["tradable_mask"][:, 5].any()
    assert out["tiers"][14, 5] == -1, "不入池的标的不参与分层"


def test_low_liquidity_takes_the_smallest_dollar_volume():
    volume = np.tile(np.linspace(1.0, 6.0, 6) * 1e5, (30, 1))
    stream = _stream(volume=volume)
    scope, out = _scope(stream)
    row = 10
    # 成交额升序：S0 最小 → 只有 S3/S4/S5 进 top-3
    assert out["tradable_mask"][row].tolist() == [False, False, False, True, True, True]


def test_tiers_split_pool_into_ordered_buckets():
    """分位点只在入池成员（top-k）上取，未入池为 -1。"""
    scope, out = _scope()
    row = 10
    assert out["tiers"][row].tolist() == [-1, -1, -1, 0, 1, 2]


def test_tiers_static_uses_one_cut_for_all_bars():
    scope, out = _scope(mask_policy="static", tier_window=5)
    rows = [10, 15, 20]
    for row in rows[1:]:
        assert out["tiers"][row].tolist() == out["tiers"][rows[0]].tolist()


def test_boundary_rejects_k_larger_than_pool():
    stream = _stream(n=4)
    assert _code(lambda: _scope(stream, k=5)) == E1004


# ---- Scope 不变量 -------------------------------------------------------
def test_scope_rejects_tradable_not_subset_of_observable():
    scope, _ = _scope()
    tradable = np.ones(scope.shape, dtype=bool)
    observable = np.zeros(scope.shape, dtype=bool)
    with pytest.raises(FlowError) as exc:
        Scope(
            times=scope.times, symbols=scope.symbols,
            observable_mask=observable, tradable_mask=tradable,
            tiers=scope.tiers, reason_codes=scope.reason_codes,
            axis_resolved=scope.axis_resolved,
        )
    assert exc.value.error.code == E3003


def test_scope_rejects_unregistered_reason_code():
    scope, _ = _scope()
    codes = np.full(scope.shape, 99, dtype="int8")
    with pytest.raises(FlowError) as exc:
        Scope(
            times=scope.times, symbols=scope.symbols,
            observable_mask=scope.observable_mask,
            tradable_mask=scope.tradable_mask,
            tiers=scope.tiers, reason_codes=codes,
            axis_resolved=scope.axis_resolved,
        )
    assert exc.value.error.code == E3003


# ---- align -------------------------------------------------------------
def test_align_trims_to_run_window_and_reports_shape():
    stream = _stream()
    scope, _ = _scope(stream)
    out = run(_align_call(stream, scope))
    assert out["shape"] == {"bars": 21, "symbols": 6, "fields": 2}
    assert out["quality_report"]["bars"] == 21
    assert out["quality_report"]["tz"] == "UTC"
    assert out["quality_report"]["duplicate_index"] == 0
    assert out["panel"].shape == (21, 6)


def test_align_drops_unclosed_tail_and_records_it():
    stream = _stream(end_closed=False)
    scope, _ = _scope(stream, pin=_pin(stream, end_closed=False),
                      time_axis=AXIS_FULL)
    res = run(_align_call(stream, scope))
    rec = res["quality_report"]["unclosed_dropped"]
    assert rec["count"] == 1 and rec["last_ts"] == int(stream.times[29])
    assert res["shape"]["bars"] == 25, "run 26 根里丢掉未收盘的末根"

    kept = run(_align_call(stream, scope, drop_unclosed=False))
    assert kept["quality_report"]["unclosed_dropped"] == {"count": 0, "last_ts": None}
    assert kept["shape"]["bars"] == 26


def test_align_reports_field_lags_from_registry():
    stream = _stream()
    scope, _ = _scope(stream)
    res = run(_align_call(stream, scope))
    report = res["causal_report"]
    assert report["field_lags"] == {"close": 0, "volume": 0}
    reg = default_registry()
    for name, lag in report["field_lags"].items():
        assert reg.fields[name]["available_lag"] == lag
    assert report["probe"]["max_shift_used"] == 0
    assert report["probe"]["method"] == "byte_identity_and_continuity"
    assert report["holdout_boundary"] == {"start": None, "end": None}
    assert all(c["passed"] for c in report["checks"])
    assert [c["id"] for c in report["checks"]] == ["P1", "P2", "P3", "P4", "P5"]


def test_align_rejects_lag_that_disagrees_with_registry():
    # 注册表里全部是 0；manifest 写成 1 就是 read 改写了 lag（最隐蔽的前视）
    stream = _stream(lag=1)
    scope, _ = _scope(stream)
    assert _code(lambda: run(_align_call(stream, scope))) == E3003


def test_align_rejects_scope_from_another_symbol_set():
    stream = _stream()
    scope, _ = _scope(stream)
    other = _stream(n=4)
    assert _code(lambda: run(_align_call(other, scope))) == E3003


def test_align_enforces_missing_ratio_gate():
    stream = _stream()
    close = np.full_like(stream.field("close"), 100.0)
    close[4:16] = np.nan
    sparse = _stream(close=close)
    scope, _ = _scope(sparse)
    assert _code(lambda: run(_align_call(
        sparse, scope, quality_gates={"max_missing_ratio": 0.2}))) == E3003
    ok = run(_align_call(sparse, scope, quality_gates={"max_missing_ratio": 0.9}))
    assert ok["shape"]["bars"] == 21


def test_align_enforces_min_obs_per_symbol():
    stream = _stream()
    close = np.full_like(stream.field("close"), 100.0)
    close[:, 2] = np.nan
    close[0:3, 2] = 100.0
    thin = _stream(close=close)
    scope, _ = _scope(thin)
    assert _code(lambda: run(_align_call(
        thin, scope, quality_gates={"min_obs": 10}))) == E3003


def test_align_rejects_empty_panel():
    stream = _stream()
    scope, _ = _scope(stream)
    axis = dict(scope.axis_resolved)
    axis["mine"] = {"start": int(stream.times[-1]) + DAY_NS * 5,
                    "end": int(stream.times[-1]) + DAY_NS * 6}
    axis["oos"] = {"start": int(stream.times[-1]) + DAY_NS * 7,
                   "end": int(stream.times[-1]) + DAY_NS * 8}
    far = Scope(
        times=scope.times, symbols=scope.symbols,
        observable_mask=scope.observable_mask, tradable_mask=scope.tradable_mask,
        tiers=scope.tiers, reason_codes=scope.reason_codes, axis_resolved=axis,
    )
    assert _code(lambda: run(_align_call(stream, far))) == E3003


def test_align_panel_ref_is_content_digest_and_stable():
    stream = _stream()
    scope, _ = _scope(stream)
    a = run(_align_call(stream, scope))
    b = run(_align_call(stream, scope))
    assert a["panel_ref"] == b["panel_ref"] == a["panel"].digest()


def test_align_always_true_checks_carry_evidence():
    """P2/P3 的 `passed` 恒 True（不通过就抛异常了），所以它们必须带证据。

    只印一个 `passed: true` 等于什么都没记：读产物的人无法区分"比对了 4 个字段"
    与"根本没比"。复核 E9。
    """
    stream = _stream()
    scope, _ = _scope(stream)
    report = run(_align_call(stream, scope))["causal_report"]
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["P2"]["fields_checked"] == len(report["field_lags"]) > 0
    assert "boundary" in by_id["P3"]
    assert by_id["P3"]["boundary"] == report["holdout_boundary"]
