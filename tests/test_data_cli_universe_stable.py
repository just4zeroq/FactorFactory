"""universe 命令的 --exclude-stable 过滤开关 —— 离线测试。

打桩 ccxt 元数据抓取与 L0 bar 回填，只验证「过滤分支」的行为：
默认保留全量、显式开关剔除稳定币、um 稳定币永续被命中、回执计数正确。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import data_cli as cli
from services import universe_service as us


def _fake_discovered() -> dict[str, dict]:
    """造两条 crypto + 一条 stable（um 永续字形）。"""
    return {
        "BTC/USDT:USDT": us.make_record(
            "BTC/USDT:USDT", asset_class="crypto",
            contract_type="PERPETUAL", underlying_type="COIN",
        ),
        "ETH/USDT:USDT": us.make_record(
            "ETH/USDT:USDT", asset_class="crypto",
            contract_type="PERPETUAL", underlying_type="COIN",
        ),
        "USDC/USDT:USDT": us.make_record(
            "USDC/USDT:USDT", asset_class="stable",
            contract_type="PERPETUAL", underlying_type="COIN",
        ),
    }


@pytest.fixture
def stub(monkeypatch):
    """打桩所有网络/发现依赖，返回记录抓取调用次数的容器。"""
    calls = {"metadata": 0}

    def fake_fetch_metadata(*a, **k):
        calls["metadata"] += 1
        return SimpleNamespace(
            records=_fake_discovered(),
            stats={"raw": 3, "kept": 3},
            source="api",
        )

    monkeypatch.setattr("services.market_data.fetch_metadata", fake_fetch_metadata)
    monkeypatch.setattr(cli, "_l0_bars", lambda root, symbol, tf: (None, None, 0))
    return calls


def _run(tmp_path: Path, *, exclude: bool) -> dict:
    args = SimpleNamespace(
        proxy=None, timeframe="1d", exchange_market="um", refresh=False,
        probe=False, delist_gap_days=7, exclude_stable=exclude,
    )
    return cli._cmd_universe(args, tmp_path)


def test_universe_default_keeps_stable(tmp_path, stub):
    """不给开关：全量保留，回执 excluded_stable=0。"""
    env = _run(tmp_path, exclude=False)
    assert env["ok"] is True
    assert env["data"]["excluded_stable"] == 0
    assert "USDC/USDT:USDT" in us.load_universe(tmp_path)
    assert env["data"]["total"] == 3


def test_universe_exclude_stable_drops_only_stable(tmp_path, stub):
    """显式开关：只剔除 asset_class=stable，crypto 保留，回执计数正确。"""
    env = _run(tmp_path, exclude=True)
    assert env["ok"] is True
    assert env["data"]["excluded_stable"] == 1
    records = us.load_universe(tmp_path)
    assert "USDC/USDT:USDT" not in records
    assert "BTC/USDT:USDT" in records
    assert "ETH/USDT:USDT" in records
    assert len(records) == 2


def test_drop_stable_uses_asset_class_not_symbol():
    """过滤口径是 asset_class，不是符号字形（避免按 USDC 字样误删）。"""
    crypto = us.make_record("USDC/USDT:USDT", asset_class="crypto")
    stable = us.make_record("BTC/USDT:USDT", asset_class="stable")
    kept, n = cli._drop_stable({"a": crypto, "b": stable})
    assert n == 1
    assert kept == {"a": crypto}
