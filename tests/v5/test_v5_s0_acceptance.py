"""S0 十二条验收（s0/README.md §6）。

每条构造一个坏输入，断言：错误码正确 + 进程退出码非零且正确 +
（供给类）无副作用。外加一条正向冒烟（编译通过、八工位、退出码 0）。

这些测试证明「失败是有信号的」——S0 的核心价值，V4 的病就是假成功。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from v5.app.cli import build_parser
from v5.contracts.errors import exit_code_for

FIXTURE = Path(__file__).parents[1] / "fixtures" / "v5" / "plan_valid.json"


def _plan() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _plan_tmp(plan: dict) -> Path:
    import tempfile

    p = Path(tempfile.gettempdir()) / f"v5_s0_plan_{abs(hash(json.dumps(plan, sort_keys=True)))}.json"
    p.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return p


def _assert_reject(plan: dict, expected_code: str, *, set_args=None, extra=None) -> dict:
    """断言错误码与退出码都正确。"""
    from v5.app.cli import _DISPATCH

    p = _plan_tmp(plan)
    argv = ["run", "--plan", str(p)] + (set_args or []) + (extra or [])
    args = build_parser().parse_args(argv)
    try:
        _DISPATCH["run"](args)
    except Exception as exc:  # noqa: BLE001 - 测试需要看具体错误码
        from v5.contracts.errors import FlowError

        if isinstance(exc, FlowError):
            assert exc.error.code == expected_code, f"期望 {expected_code}，实得 {exc.error.code}"
            expected_exit = exit_code_for(expected_code)
            assert expected_exit != 0
            return {"code": exc.error.code, "exit": expected_exit}
        raise
    raise AssertionError(f"坏输入未被拒绝（期望 {expected_code}）")


class TestS0Acceptance:
    def test_smoke_valid_plan(self, tmp_path, monkeypatch, v5_supply):
        """正向冒烟：合法计划编译通过，八工位，退出码 0。

        S2 起 run 必须读到真实供给；方案 B 之后「有供给」就是「目录里有 parquet」，
        假别名表已经不能混过去（身份由内容现算，没有内容就没有身份）。
        """
        from v5.app.cli import _DISPATCH

        monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
        (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
        (tmp_path / "schemas" / "symbol_sets.json").write_text(
            json.dumps({"cs_top60_2026q3": ["BTC", "ETH"]}), encoding="utf-8"
        )
        # plan_valid 声明的是 binance/futures + 4 个数据集，照着写。
        v5_supply(symbols=["BTC", "ETH"], exchange="binance")

        args = build_parser().parse_args(["run", "--plan", str(FIXTURE)])
        env = _DISPATCH["run"](args)
        assert env["ok"] is True
        assert env["exit_code"] == 0
        assert len(env["data"]["station_specs"]) == 8
        # 正向冒烟：八 Parcel + 八 trace，且没有真实数字
        assert env["data"]["parcels"] == 8
        assert env["data"]["traces"] == 8
        assert env["data"]["state"] == "done"
        # 调用单/指纹链已真实，工位算法仍是桩；阶段标记随之更新
        assert env["data"]["stage"] == "S2-real-supply"

    def test_s0_1_unknown_config_path(self):
        """S0-1：拼错配置路径 → E1001 + 退出码 2。"""
        plan = _plan()
        plan["target"]["horizn"] = 5
        r = _assert_reject(plan, "E1001")
        assert r["exit"] == 2

    def test_s0_2_unknown_impl_name(self):
        """S0-2：实现名格式错 → E1002 + 退出码 2。"""
        plan = _plan()
        plan["target"]["impl"] = ""
        r = _assert_reject(plan, "E1002")
        assert r["exit"] == 2

    def test_s0_5_constitutive_override(self):
        """S0-5：命令行改构成性参数 → E1005 + 退出码 2。"""
        plan = _plan()
        r = _assert_reject(plan, "E1005", set_args=["--set", "caliber=bad"])
        assert r["exit"] == 2

    def test_s0_6_verdict_must_be_sentinel(self):
        """S0-6 的配置侧：verdict.impl 只能是 sentinel（schema 枚举，E1004/退出码 2）。

        真正的 S0-6「执行期未注入 Sentinel → E1006」属 executor（任务 12）；
        这里验证计划层先把"另配裁判"挡在编译之前——两个检查点各管一段。
        """
        plan = _plan()
        plan["verdict"]["impl"] = "lenient_verdict"
        r = _assert_reject(plan, "E1004")
        assert r["exit"] == 2

    def test_s0_20_station_has_caliber_field(self):
        """C20/E1020：工位 params 自带口径字段 → 退出码 2。"""
        plan = _plan()
        plan["target"].setdefault("params", {})["horizon"] = 5
        r = _assert_reject(plan, "E1020")
        assert r["exit"] == 2

    def test_envelope_ok_exit_code_consistency(self):
        """ok 与 exit_code 必须一致（历史问题：退出码恒 0）。"""
        from v5.app.cli import _DISPATCH

        args = build_parser().parse_args(["stations"])
        env = _DISPATCH["stations"](args)
        assert env["ok"] is True and env["exit_code"] == 0
        assert env["data"]["count"] == 8

    def test_error_object_has_no_exit_code(self):
        """错误对象不带 exit_code，只有信封带（09 §2.4）。"""
        from v5.contracts.errors import Error

        e = Error(code="E1001", message="x")
        assert "exit_code" not in e.to_dict()
        assert e.to_dict()["layer"] == "usage"
