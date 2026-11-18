"""S3-M2：`read` 事实读取（窗口裁剪、字段清单、长表 → 面板）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from capability import Call, InputRef, run
from capability.impls import install_default_impls
from capability.runtime import reset_impls
from capability.stream import FieldStream, Pin, SupplyBatch
from contracts.digest import compute_digest
from contracts.errors import (
    E1004, E1014, E3001, E3003, E4004, E4007, E4002, FlowError,
)
from contracts.types import Digest, Ref

DAY_NS = 86_400_000_000_000
BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
CHECKSUM = compute_digest({"payload": "ohlcv-v1"})
VERSION = "0123456789abcdef"


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


def _ts(day: int) -> str:
    """第 `day` 天的规范时间文本（与 acquire 写出的形态一致）。"""
    return (BASE + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pin(start: int = 0, end: int = 30, end_closed: bool = True) -> Pin:
    return Pin(
        dataset="ohlcv", version=VERSION, checksum=CHECKSUM,
        coverage=(START + start * DAY_NS, START + end * DAY_NS),
        symbols_digest="s0", end_closed=end_closed,
    )


def _source_ref(pin: Pin) -> Ref:
    return Ref(
        kind="supply", id=f"ohlcv@{pin.version}", digest=Digest.parse(pin.checksum),
        relpath=f"supply/ohlcv/{pin.version}/payload", schema_version="1",
    )


def _columns(rows: list[tuple[str, int, float, float]], *, gap=None) -> dict:
    sym = [r[0] for r in rows]
    time = [_ts(r[1]) for r in rows]
    close = [r[2] for r in rows]
    volume = [r[3] for r in rows]
    return {"symbol": sym, "timestamp": time, "close": close, "volume": volume}


def _rows(t_len: int = 30, n: int = 3, day0: int = 0) -> list[tuple[str, int, float, float]]:
    out = []
    for t in range(t_len):
        for j in range(n):
            out.append((f"S{j}", day0 + t, 100.0 + t + j, 1e3 + j))
    return out


def _batch(rows=None, *, pin=None, symbol_set="top3", columns=None,
           classes=None) -> SupplyBatch:
    pin = pin or _pin()
    return SupplyBatch(
        pin=pin, symbol_set=symbol_set, source_ref=_source_ref(pin),
        columns=columns if columns is not None else _columns(rows or _rows()),
        symbol_classes=classes or {},
    )


def _call(batch: SupplyBatch, **params):
    base = {
        "datasets": ["ohlcv"],
        "fields": {"ohlcv": ["close", "volume"]},
        "frequency": "1d",
        "window": {"start": _ts(0), "end": _ts(29)},
        "symbols": "top3",
    }
    base.update(params)
    return Call.create(
        capability="read", station="Panel", impl="published_only", impl_version="1",
        params=base, inputs=(InputRef("supply", batch.digest(), data=batch),),
    )


def _read(batch: SupplyBatch | None = None, **params) -> dict:
    return run(_call(batch or _batch(), **params))


# ---------------------------------------------------------------- 基本形状
def test_read_builds_panel_from_long_table():
    out = _read()
    stream = out["stream"]
    assert isinstance(stream, FieldStream)
    assert stream.symbols == ("S0", "S1", "S2")
    assert stream.shape == (30, 3)
    assert stream.times[0] == START and stream.times[-1] == START + 29 * DAY_NS
    assert stream.field("close")[0, 0] == 100.0
    assert stream.field("close")[29, 2] == 131.0
    assert stream.field("volume")[5, 1] == 1001.0
    assert stream.end_closed is True


def test_read_field_manifest_comes_from_registry():
    out = _read()
    manifest = out["field_manifest"]
    assert set(manifest) == {"close", "volume"}
    assert manifest["close"] == {
        "dataset": "ohlcv", "version": VERSION, "unit": "quote", "available_lag": 0,
    }
    assert manifest["volume"]["unit"] == "base"
    # 流的 manifest 与返回的 field_manifest 必须是同一份（align 读前者做 P2 检查）
    assert out["stream"].manifest["close"] == manifest["close"]


def test_read_stream_refs_passes_through_source_ref():
    batch = _batch()
    out = _read(batch)
    assert out["stream_refs"] == [batch.source_ref]


def test_read_coverage_reports_selected_window():
    out = _read(window={"start": _ts(5), "end": _ts(9)})
    cov = out["coverage"]
    assert cov["start"] == START + 5 * DAY_NS
    assert cov["end"] == START + 9 * DAY_NS
    assert cov["bars"] == 5
    assert cov["symbols"] == 3


# ---------------------------------------------------------------- 窗口
def test_read_window_is_inclusive_on_both_ends():
    out = _read(window={"start": _ts(10), "end": _ts(10)})
    assert out["stream"].shape == (1, 3)
    assert out["stream"].times[0] == START + 10 * DAY_NS


def test_read_window_beyond_coverage_is_rejected():
    batch = _batch()
    assert _code(lambda: _read(batch, window={"start": _ts(0), "end": _ts(40)})) == E4002
    # 覆盖区间是闭区间：`coverage[1]` 是最后一根 bar 的时戳，不是次日零点
    assert _code(lambda: _read(batch, window={"start": _ts(-1), "end": _ts(5)})) == E4002


def test_read_window_with_no_observations_is_rejected():
    # 覆盖声称到第 30 天，但数据只到第 9 天
    batch = _batch(_rows(t_len=10), pin=_pin(end=30))
    assert _code(lambda: _read(batch, window={"start": _ts(20), "end": _ts(25)})) == E4002


def test_read_rejects_reversed_window():
    assert _code(lambda: _read(window={"start": _ts(9), "end": _ts(5)})) == E4002


# ---------------------------------------------------------------- 字段清单
def test_read_rejects_unregistered_field():
    assert _code(lambda: _read(fields={"ohlcv": ["adj_close"]})) == E1014


def test_read_rejects_field_of_another_dataset():
    # funding_rate 已注册，但属于 funding 数据集
    assert _code(lambda: _read(fields={"ohlcv": ["funding_rate"]})) == E1014


def test_read_rejects_field_missing_from_published_columns():
    cols = _columns(_rows())
    cols.pop("volume")
    batch = _batch(columns=cols)
    assert _code(lambda: _read(batch)) == E1014


def test_read_rejects_duplicated_field_request():
    assert _code(lambda: _read(fields={"ohlcv": ["close", "close"]})) == E1004


def test_read_rejects_fields_for_undeclared_dataset():
    assert _code(lambda: _read(fields={"ohlcv": ["close"], "funding": ["funding_rate"]})) in {
        E1004, E3001,
    }


# ---------------------------------------------------------------- 数据集/标的集
def test_read_rejects_multiple_datasets_with_single_version():
    assert _code(lambda: _read(datasets=["ohlcv", "funding"])) == E3001


def test_read_rejects_dataset_mismatch_with_pin():
    assert _code(lambda: _read(datasets=["funding"],
                               fields={"funding": ["funding_rate"]})) == E3001


def test_read_rejects_symbol_set_mismatch():
    batch = _batch(symbol_set="top20")
    assert _code(lambda: _read(batch)) == E4002


# ---------------------------------------------------------------- 结构与数据
def test_read_rejects_duplicate_symbol_time_pair():
    rows = _rows()
    rows.append(("S0", 0, 1.0, 2.0))
    assert _code(lambda: _read(_batch(rows))) == E4007


def test_read_rejects_non_numeric_value():
    cols = _columns(_rows())
    cols["close"][0] = "n/a"
    assert _code(lambda: _read(_batch(columns=cols))) == E3003


def test_read_rejects_bool_value():
    cols = _columns(_rows())
    cols["close"][0] = True
    assert _code(lambda: _read(_batch(columns=cols))) == E3003


def test_read_keeps_nan_for_missing_cell():
    rows = [r for r in _rows() if not (r[0] == "S1" and r[1] == 4)]
    out = _read(_batch(rows))
    close = out["stream"].field("close")
    assert np.isnan(close[4, 1])
    assert np.isfinite(close[4, 0]) and np.isfinite(close[4, 2])


def test_read_rejects_frequency_mismatch():
    # 数据是 1d，声明成 4h
    assert _code(lambda: _read(frequency="4h")) == E4002


def test_read_end_closed_follows_pin():
    out = _read(_batch(pin=_pin(end_closed=False)))
    assert out["stream"].end_closed is False


# ---------------------------------------------------------------- missing_ranges
def test_read_missing_ranges_only_reports_interior_gaps():
    # S1 在第 4、5 天完全没有数据 → 一段内部缺口
    rows = [r for r in _rows() if not (r[0] == "S1" and r[1] in (4, 5))]
    out = _read(_batch(rows))
    ranges = out["missing_ranges"]
    assert len(ranges) == 1
    assert ranges[0]["symbol"] == "S1"
    assert ranges[0]["bars"] == 2
    assert ranges[0]["start"] == START + 4 * DAY_NS
    assert ranges[0]["end"] == START + 5 * DAY_NS


def test_read_missing_ranges_ignores_listing_gap():
    # S2 从第 10 天才开始：上市前那段不是「缺数据」
    rows = [r for r in _rows() if not (r[0] == "S2" and r[1] < 10)]
    out = _read(_batch(rows))
    assert out["missing_ranges"] == []
    assert np.isnan(out["stream"].field("close")[0, 2])


# ---------------------------------------------------------------- SupplyBatch
def test_supply_batch_normalizes_columns_to_tuples():
    batch = _batch()
    assert isinstance(batch.columns["close"], tuple)
    assert batch.rows == 90
    assert batch.symbols == ("S0", "S1", "S2")


def test_supply_batch_rejects_ragged_columns():
    cols = _columns(_rows())
    cols["close"] = cols["close"][:-1]
    assert _code(lambda: _batch(columns=cols)) == E3003


def test_supply_batch_rejects_empty_table():
    cols = {"symbol": [], "timestamp": [], "close": []}
    assert _code(lambda: _batch(columns=cols)) == E3003


def test_supply_batch_rejects_missing_time_column():
    cols = _columns(_rows())
    cols.pop("timestamp")
    assert _code(lambda: _batch(columns=cols)) == E3003


def test_supply_batch_rejects_checksum_mismatch():
    pin = _pin()
    bad = Ref(kind="supply", id="ohlcv@x", digest=Digest.parse(compute_digest({"x": 1})),
              relpath="supply/ohlcv/x/payload", schema_version="1")
    assert _code(lambda: SupplyBatch(
        pin=pin, symbol_set="top3", source_ref=bad, columns=_columns(_rows()),
    )) == E4004


def test_supply_batch_rejects_blank_symbol_set():
    pin = _pin()
    assert _code(lambda: SupplyBatch(
        pin=pin, symbol_set="  ", source_ref=_source_ref(pin), columns=_columns(_rows()),
    )) == E3003


def test_supply_batch_rejects_unknown_symbol_class():
    assert _code(lambda: _batch(classes={"S9": "spot"})) == E1014


# ---------------------------------------------------------------- 端到端
def test_read_outputs_feed_boundary_and_align():
    """read 的真实产物必须能被 boundary → align 直接消费。"""
    from capability.stream import Scope

    pin = _pin()
    out = _read(_batch(pin=pin))
    stream = out["stream"]

    scope_out = run(Call.create(
        capability="boundary", station="Boundary", impl="pit_dynamic_topk",
        impl_version="1",
        params={
            "k": 2, "tier_window": 1, "min_coverage": 0.5,
            "time_axis": {"mine": "2023-12-25..2024-01-20",
                          "oos": "2024-01-21..2024-01-30",
                          "holdout": "reserved"},
            "mask_policy": "pit_dynamic",
        },
        inputs=(
            InputRef("pin", pin.digest(), data=pin),
            InputRef("field_stream", stream.digest(), data=stream),
        ),
    ))
    scope = scope_out["scope"]
    assert isinstance(scope, Scope)
    assert scope.tradable_mask.sum() > 0

    panel_out = run(Call.create(
        capability="align", station="Panel", impl="point_in_time_align",
        impl_version="1",
        params={"drop_unclosed": True, "causal_probe": "strict",
                "quality_gates": {"max_missing_ratio": 0.9}},
        inputs=(
            InputRef("field_stream", stream.digest(), data=stream),
            InputRef("scope", scope.digest(), data=scope),
        ),
        runtime_facts={"holdout_visibility": "reserved"},
    ))
    panel = panel_out["panel"]
    assert panel.shape == (stream.shape[0], stream.shape[1])
    assert panel_out["field_manifest"][0]["available_lag"] == 0
    assert panel_out["quality_report"]["bars"] == 30
