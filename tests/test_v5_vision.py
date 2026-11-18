"""vision 发现/筛选服务的单元契约（不依赖网络）。

- 全市场清单来自注入的 loader，去重排序；
- 最早月份靠注入的 head 探测，返回第一个 200 的月份；
- 筛选按 days 门槛，产出带 criteria 的可审计清单；
- 写 symbol_sets.json 保留其它集合、空集拒绝。
"""

from __future__ import annotations

import json

import pytest

from contracts.errors import FlowError
from services import vision_service as vs

START, END = "2019-01", "2026-11"


def test_month_keys_inclusive():
    keys = vs._month_keys("2019-11", "2020-02")
    assert keys == ["2019-11", "2019-12", "2020-01", "2020-02"]


def test_days_since():
    assert vs._days_since("2020-01", "2026-11") == 82 * 30
    assert vs._days_since("2026-11", "2026-11") == 0


def test_discover_symbols_dedup_sorted():
    got = vs.discover_symbols(lambda: ["B/USDT:USDT", "A/USDT:USDT", "B/USDT:USDT"])
    assert got == ["A/USDT:USDT", "B/USDT:USDT"]


def test_discover_symbols_empty_rejected():
    with pytest.raises(FlowError) as exc:
        vs.discover_symbols(lambda: [])
    assert exc.value.error.code == "E4002"


def test_probe_first_month():
    # 归档单调：A 从 2020-01 起每月都有；B 一直没有；C 除尾部未归档月外都有。
    def head(url: str) -> bool:
        symbol, month = url.split("/")
        if "B_USDT" in symbol:
            return False
        if "C_USDT" in symbol:
            return month != "2020-03"  # 尾部当月未归档
        return "A_USDT" in symbol and month >= "2020-01"

    got = vs.probe_first_month(
        ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"], head,
        url_for=lambda s, m: f"{s.replace('/', '_')}/{m}",
        start="2019-11", end="2020-03",
    )
    assert got["A/USDT:USDT"] == "2020-01"
    assert got["B/USDT:USDT"] is None
    assert got["C/USDT:USDT"] == "2019-11"  # 尾部未归档不影响探测


def test_build_candidates_filters_and_auditable():
    first = {
        "OLD/USDT:USDT": "2020-01",
        "NEW/USDT:USDT": "2025-06",
        "NONE/USDT:USDT": None,
    }
    out = vs.build_candidates(first, end=END, min_days=720, start=START)
    assert [r["symbol"] for r in out["eligible"]] == ["OLD/USDT:USDT"]
    assert out["criteria"]["min_days"] == 720
    assert {r["reason"] for r in out["rejected"]} == {"no_archive", "short_history"}
    assert any(r["symbol"] == "NEW/USDT:USDT" and "days" in r for r in out["rejected"])


def test_write_symbol_set_preserves_others(tmp_path):
    path = tmp_path / "symbol_sets.json"
    path.write_text(json.dumps({"crypto20": ["BTC/USDT:USDT"]}), encoding="utf-8")
    candidates = {"eligible": [{"symbol": "A/USDT:USDT"}, {"symbol": "B/USDT:USDT"}]}
    vs.write_symbol_set(candidates, path, "pit_all")
    table = json.loads(path.read_text(encoding="utf-8"))
    assert table["crypto20"] == ["BTC/USDT:USDT"]
    assert table["pit_all"] == ["A/USDT:USDT", "B/USDT:USDT"]


def test_write_symbol_set_empty_rejected(tmp_path):
    with pytest.raises(FlowError) as exc:
        vs.write_symbol_set({"eligible": []}, tmp_path / "symbol_sets.json", "x")
    assert exc.value.error.code == "E4002"
