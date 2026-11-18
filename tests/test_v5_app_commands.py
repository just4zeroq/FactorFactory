"""应用层拆分验收（09 §2 / IMPLEMENTATION-PLAN §3.5）。

覆盖：命令面唯一清单与 mutation 元数据、P1 口径开关守卫、P3 --yes、
三格式渲染、信封 ok/exit_code 恒一致、错误对象不带 exit_code。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import contract as C
from app.cli import build_parser
from app.cli.run import cmd_run
from app.render import FORMATS, render
from contracts.errors import E1004, E1020, FlowError


# ---------------------------------------------------------------- 命令面
def test_19_commands_declared():
    # S6 新增 reconcile（17）；快照管理新增 snapshot（18）；复盘闭环 replay（19）
    assert len(C.CORE_COMMANDS) == 5
    assert len(C.BUSINESS_COMMANDS) == 14
    assert len(C.ALL_COMMANDS) == 19
    assert set(C.COMMANDS) == set(C.ALL_COMMANDS)


def test_each_command_has_mutation_ticket_exit():
    for name in C.ALL_COMMANDS:
        spec = C.get(name)
        assert spec.mutation and spec.mutation <= set(C.MUTATIONS)
        assert isinstance(spec.produces_ticket, bool)
        assert spec.max_exit_code in {2, 3, 4}


def test_mutation_levels_match_doc():
    assert C.get("run").mutation == frozenset({C.WRITES_FACTS})
    assert C.get("run").produces_ticket is True
    assert C.get("explain").mutation == frozenset({C.READ_ONLY})
    assert C.get("fetch").mutation == frozenset({C.WRITES_SUPPLY})
    assert C.get("holdout").mutation == frozenset({C.WRITES_LEDGER})
    # promote/certify 是复合等级：含 writes_ledger，故须 --yes（I31）
    assert C.get("promote").needs_confirmation() is True
    assert C.get("certify").needs_confirmation() is True
    assert C.get("run").needs_confirmation() is False


def test_alias_experiment_maps_to_run():
    assert C.get("experiment").name == "run"


def test_unknown_command_rejected():
    with pytest.raises(FlowError) as e:
        C.get("nope")
    assert e.value.error.code == E1004


# ---------------------------------------------------------------- P1 / P3
def test_p1_caliber_option_guard():
    with pytest.raises(FlowError) as e:
        C.assert_no_caliber_args(["--horizon"])
    assert e.value.error.code == E1020
    # 正常参数不误伤
    C.assert_no_caliber_args(["--plan", "--set", "--format"])


def test_p3_requires_yes_for_ledger():
    class Args:
        yes = False
    with pytest.raises(FlowError) as e:
        C.require_confirmation("holdout", Args())
    assert e.value.error.code == E1004
    Args.yes = True
    C.require_confirmation("holdout", Args())
    Args.yes = False  # 复位，避免污染其他用例


def test_build_parser_declares_all_formats():
    p = build_parser()
    for argv in (["run", "--plan", "x.json", "--format", "ndjson"],
                 ["stations", "--format", "table"],
                 ["inspect", "--run-id", "r1"]):
        args = p.parse_args(argv)
        assert args.format in FORMATS


# ---------------------------------------------------------------- 渲染
def _envelope(ok=True):
    if ok:
        return {"ok": True, "command": "stations", "run_id": None,
                "data": {"count": 8, "stations": [{"name": "Boundary"}]},
                "warnings": [], "exit_code": 0}
    return {"ok": False, "command": "run", "run_id": None, "data": None,
            "warnings": [], "exit_code": 2,
            "errors": [{"code": "E1001", "layer": "usage", "retryable": False,
                        "message": "bad"}]}


def test_json_render_roundtrip():
    out = render(_envelope(), "json")
    assert json.loads(out)["data"]["count"] == 8


def test_ndjson_render_expands_lists():
    out = render(_envelope(), "ndjson")
    lines = [json.loads(x) for x in out.splitlines()]
    assert any(x.get("_kind") == "stations" for x in lines)


def test_table_render_is_readable():
    out = render(_envelope(), "table")
    assert "status: OK" in out and "count: 8" in out


def test_error_envelope_ok_exit_consistency():
    env = _envelope(ok=False)
    assert env["ok"] is False and env["exit_code"] != 0
    assert env["data"] is None
    assert "exit_code" not in env["errors"][0]


def test_unknown_render_format_rejected():
    with pytest.raises(ValueError):
        render(_envelope(), "yaml")


# ---------------------------------------------------------------- P0 回归：CLI 落盘
def _symbol_set(tmp_path: Path, name: str, members: list[str]) -> None:
    """计划里的 `supply.symbols` 只是标的集名，成员清单在 schemas 下。"""
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({name: members}), encoding="utf-8"
    )


def test_cmd_run_persists_parcels_and_traces(tmp_path, monkeypatch, supply):
    """信封报 parcels=N / traces=N，磁盘上就必须真有 N 份——否则是假成功。"""
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    _symbol_set(tmp_path, "cs_top60_2026q3", ["BTC", "ETH"])
    # 编译期要现算 checksum 进 blueprint_digest，所以要写真的 parquet。
    # plan_valid 声明的是 binance/futures（不是 binanceusdm），照着写。
    supply(symbols=["BTC", "ETH"], exchange="binance")
    plan_path = Path(__file__).resolve().parent / "fixtures" / "plan_valid.json"

    class Args:
        plan = str(plan_path)
        set = None
        no_strict = False
        allow_constitutive_override = False
        format = "json"

    env = cmd_run(Args())
    assert env["ok"] is True and env["exit_code"] == 0

    run_dir = tmp_path / "runs" / env["run_id"]
    n_parcels = env["data"]["parcels"]
    n_traces = env["data"]["traces"]
    assert n_parcels == 8 and n_traces == 8

    parcel_dirs = [d for d in (run_dir / "parcels").iterdir()
                   if d.is_dir() and (d / "parcel.json").exists()]
    assert len(parcel_dirs) == n_parcels
    for d in parcel_dirs:
        assert (d / "payload.json").exists() and (d / "log.jsonl").exists()

    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trace_lines) == n_traces
    assert (run_dir / "logs" / "process.jsonl").exists()
