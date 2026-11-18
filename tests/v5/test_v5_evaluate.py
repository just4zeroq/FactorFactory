"""S3-M2：`evaluate` 表达式求值（AST → qweave、白名单、values/valid_mask 分开）。"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

import numpy as np
import pytest

from v5.capability import Call, InputRef, run
from v5.capability.impls import install_default_impls
from v5.capability.panel import Panel, array_digest
from v5.capability.registry import default_registry
from v5.capability.runtime import reset_impls
from v5.contracts.errors import E1003, E1004, E1015, E3003, FlowError

DAY_NS = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))
KERNEL = pkg_version("qweave")


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


def _panel(t_len: int = 6, n: int = 3, *, close=None, volume=None,
           name: str = "close") -> Panel:
    times = START + np.arange(t_len, dtype="int64") * DAY_NS
    if close is None:
        close = np.array([[10.0 * (j + 1) + t for j in range(n)] for t in range(t_len)])
    if volume is None:
        volume = np.tile(np.array([1.0, 2.0, 3.0])[:n], (t_len, 1))
    return Panel(
        times=times,
        symbols=tuple(f"S{j}" for j in range(n)),
        fields={name: close, "volume": volume},
    )


def _f(name: str) -> dict:
    return {"field": name}


def _c(value) -> dict:
    return {"const": value}


def _op(name: str, *args) -> dict:
    return {"op": name, "args": list(args)}


def _call(payload, panel: Panel, **params):
    base = {
        "operator_set": default_registry().operators_version,
        "nan_policy": "propagate",
        "node_cache": False,
    }
    base.update(params)
    return Call.create(
        capability="evaluate", station="Evidence", impl="qweave",
        impl_version=KERNEL, params=base,
        inputs=(
            InputRef("payload", "sha256:" + "0" * 64, data=payload),
            InputRef("panel", panel.digest(), data=panel),
        ),
    )


def _eval(payload, panel: Panel | None = None, **params) -> dict:
    panel = panel or _panel()
    return run(_call(payload, panel, **params))


# ---------------------------------------------------------------- 基本求值
def test_evaluate_rolling_mean_matches_hand_calc():
    panel = _panel()
    out = _eval(_op("mean", _f("close"), _c(3)), panel)
    values = out["values"]
    assert values.shape == panel.shape
    # t<2 窗口不足 → NaN；t=2 起是最近三根的均值
    assert np.isnan(values[0]).all() and np.isnan(values[1]).all()
    for t in (2, 3, 4, 5):
        for j in range(3):
            expect = np.mean([panel.fields["close"][t - k, j] for k in range(3)])
            assert values[t, j] == pytest.approx(expect)


def test_evaluate_cross_sectional_rank_normalizes_by_valid_members():
    close = np.array([[1.0, 2.0, 4.0]] * 3)
    panel = _panel(3, 3, close=close)
    out = _eval(_op("rank", _f("close")), panel)
    # qweave 的 rank = 平均名次 / 有效成员数
    assert out["values"][0].tolist() == pytest.approx([1 / 3, 2 / 3, 1.0])


def test_evaluate_delay_shifts_within_symbol():
    panel = _panel()
    out = _eval(_op("ref", _f("close"), _c(1)), panel)
    assert np.isnan(out["values"][0]).all()
    np.testing.assert_allclose(out["values"][1:], panel.fields["close"][:-1])


def test_evaluate_binary_and_const_nodes():
    panel = _panel()
    out = _eval(_op("add", _f("close"), _c(1.5)), panel)
    np.testing.assert_allclose(out["values"], panel.fields["close"] + 1.5)


def test_evaluate_ternary_corr_runs():
    rng = np.random.default_rng(7)
    close = np.cumsum(rng.normal(size=(40, 3)), axis=0) + 100.0
    volume = np.cumsum(rng.normal(size=(40, 3)), axis=0) + 50.0
    panel = _panel(40, 3, close=close, volume=volume)
    out = _eval(_op("corr", _f("close"), _f("volume"), _c(5)), panel)
    assert np.isfinite(out["values"][5:]).any()
    assert out["operators_used"] == ["corr"]


# ---------------------------------------------------------------- 白名单
def test_evaluate_rejects_unknown_operator():
    assert _code(lambda: _eval(_op("ts_rank", _f("close"), _c(3)))) == E1015


def test_evaluate_rejects_wrong_arity():
    assert _code(lambda: _eval(_op("mean", _f("close")))) == E1003
    assert _code(lambda: _eval(_op("rank", _f("close"), _c(3)))) == E1003


def test_evaluate_rejects_param_out_of_domain():
    # std 的 param_domain 下界是 2
    assert _code(lambda: _eval(_op("std", _f("close"), _c(1)))) == E1004
    assert _code(lambda: _eval(_op("mean", _f("close"), _c(999)))) == E1004


def test_evaluate_rejects_non_const_param():
    assert _code(lambda: _eval(_op("mean", _f("close"), _f("volume")))) == E1004


def test_evaluate_rejects_float_param():
    assert _code(lambda: _eval(_op("mean", _f("close"), _c(3.0)))) == E1004


def test_evaluate_rejects_unknown_field():
    assert _code(lambda: _eval(_op("mean", _f("adj_close"), _c(3)))) == E1003


def test_evaluate_rejects_malformed_node():
    assert _code(lambda: _eval({"op": "mean", "args": [_f("close")], "extra": 1})) == E1003
    assert _code(lambda: _eval({"op": "mean", "args": [_f("close")], "field": "close"})) == E1003
    assert _code(lambda: _eval({"const": True})) == E1003
    assert _code(lambda: _eval([1, 2, 3])) == E1003


def test_evaluate_rejects_operator_set_mismatch():
    assert _code(lambda: _eval(_op("rank", _f("close")), operator_set="v1")) == E1015


def test_evaluate_rejects_node_cache_true():
    # 取值不受支持（不是实现抛异常）：E1004，别用 E3004
    assert _code(lambda: _eval(_op("rank", _f("close")), node_cache=True)) == E1004


def test_evaluate_rejects_model_ref_payload():
    assert _code(lambda: _eval({"model_ref": "parcels/x/model.pt"})) == E1003


# ---------------------------------------------------------------- nan_policy
def test_evaluate_drop_requires_complete_cross_section():
    # 第 0..1 天 S2 缺数据；第 2 天起完整
    close = np.array([[1.0, 2.0, np.nan]] * 2 + [[1.0, 2.0, 3.0]] * 2)
    panel = _panel(4, 3, close=close)
    out = _eval(_op("rank", _f("close")), panel, nan_policy="drop")
    valid = out["valid_mask"]
    # 值本身与 propagate 完全一致（drop 不动 values）
    np.testing.assert_allclose(out["values"][2:], [[1 / 3, 2 / 3, 1.0]] * 2)
    # 但不完整的时点整行无效
    assert not valid[0].any() and not valid[1].any()
    assert valid[2].all() and valid[3].all()
    assert out["node_stats"]["dropped_bars"] == 2


def test_evaluate_propagate_keeps_partial_cross_section():
    close = np.array([[1.0, 2.0, np.nan]] * 2 + [[1.0, 2.0, 3.0]] * 2)
    panel = _panel(4, 3, close=close)
    out = _eval(_op("rank", _f("close")), panel, nan_policy="propagate")
    valid = out["valid_mask"]
    assert valid[0].tolist() == [True, True, False]
    assert out["node_stats"]["dropped_bars"] == 0


def test_evaluate_drop_without_complete_cross_section_is_rejected():
    close = np.full((3, 3), np.nan)
    panel = _panel(3, 3, close=close)
    assert _code(lambda: _eval(_op("rank", _f("close")), panel,
                               nan_policy="drop")) == E3003


# ---------------------------------------------------------------- 退化与掩码
def test_evaluate_division_by_zero_becomes_nan_not_inf():
    close = np.array([[0.0, 2.0, -4.0]] * 3)
    panel = _panel(3, 3, close=close)
    out = _eval(_op("div", _c(1.0), _f("close")), panel)
    values = out["values"]
    # 除零在 qweave 里产出 inf，按 degenerate_policy=nan 转成缺失
    assert np.isnan(values[:, 0]).all()
    np.testing.assert_allclose(values[:, 1], 0.5)
    np.testing.assert_allclose(values[:, 2], -0.25)
    assert out["node_stats"]["degenerate_cells"] == 3


def test_evaluate_fully_degenerate_result_is_rejected():
    panel = _panel(3, 3, close=np.array([[1.0, 2.0, -4.0]] * 3))
    assert _code(lambda: _eval(_op("div", _f("close"), _c(0.0)), panel)) == E3003


def test_evaluate_log_of_negative_is_nan():
    close = np.array([[-1.0, 0.0, 2.0], [-1.0, 0.0, 4.0], [-1.0, 0.0, 8.0]])
    panel = _panel(3, 3, close=close)
    out = _eval(_op("log", _f("close")), panel)
    assert np.isnan(out["values"][:, 0]).all()
    assert np.isnan(out["values"][:, 1]).all()
    np.testing.assert_allclose(out["values"][:, 2], np.log([2.0, 4.0, 8.0]))


def test_evaluate_all_nan_result_is_rejected():
    close = np.full((3, 3), np.nan)
    panel = _panel(3, 3, close=close)
    assert _code(lambda: _eval(_op("rank", _f("close")), panel)) == E3003


def test_evaluate_valid_mask_is_separate_from_values():
    panel = _panel()
    out = _eval(_op("mean", _f("close"), _c(3)), panel)
    values, valid = out["values"], out["valid_mask"]
    assert valid.dtype == bool
    assert valid.shape == values.shape
    assert not valid[0].any() and valid[2:].all()
    # 无效不等于 0：值必须是 NaN，不能被写成 0
    assert np.isnan(values[0]).all()
    assert out["valid_mask_ref"] == array_digest(valid)
    assert out["values_ref"] == array_digest(values)


def test_evaluate_values_ref_tracks_content():
    panel = _panel()
    a = _eval(_op("rank", _f("close")), panel)
    b = _eval(_op("scale", _f("close")), panel)
    assert a["values_ref"] != b["values_ref"]


# ---------------------------------------------------------------- node_stats
def test_evaluate_node_stats_reports_cache_off():
    out = _eval(_op("add", _op("mean", _f("close"), _c(2)), _f("volume")))
    stats = out["node_stats"]
    assert stats["cache"]["enabled"] is False
    # 没查缓存 ≠ 命中率 0%：三个统计一律 null
    assert stats["cache"]["hits"] is None
    assert stats["cache"]["total"] is None
    assert stats["cache"]["key_prefix"] is None
    assert stats["nodes"] == 5
    assert stats["depth"] == 3
    assert stats["operators"] == 2
    assert stats["fields"] == ["close", "volume"]
    assert out["operators_used"] == ["add", "mean"]


def test_evaluate_depth_limit():
    node = _f("close")
    for _ in range(70):
        node = _op("abs", node)
    assert _code(lambda: _eval(node)) == E1003


# ---------------------------------------------------------------- 数据通道
def test_evaluate_panel_is_not_mutated():
    panel = _panel()
    before = panel.digest()
    _eval(_op("rank", _f("close")), panel)
    assert panel.digest() == before


def test_evaluate_outputs_declared_channels_only():
    """返回值必须与 outputs_schema 逐键一致（多一个就是未声明通道）。"""
    from v5.capability.params import validate_outputs

    out = _eval(_op("rank", _f("close")))
    schema = default_registry()  # 仅确保注册表可用
    assert schema is not None
    validate_outputs("evaluate", out, {"node_cache": False})
