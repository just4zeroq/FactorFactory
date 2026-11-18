"""因子服务 / 复盘服务 / 自动化 测试（docs/21 §4）。

全部使用临时数据根，不读不写真实 ~/factorfactory。compute 注入确定性结果，
覆盖 diff / 决策 / 幂等 / 失败不改证书，不依赖真实行情。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from contracts.errors import FlowError
from services.factor_service import FactorService, derive_factor_id
from services.replay_service import (
    ReplayService, decide, diff_metrics,
)
from storage.catalog import Catalog
from storage.layout import Layout
from storage.sqlite_store import resolve_db_path


@pytest.fixture()
def tmp_root():
    td = tempfile.mkdtemp(prefix="ff-replay-")
    root = Path(td)
    (root / "ohlcv" / "binanceusdm" / "futures").mkdir(parents=True)
    for symbol, close in (("BTC", 100.0), ("ETH", 10.0)):
        pd.DataFrame({
            "symbol": [f"{symbol}/USDT:USDT"] * 3,
            "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
            "open": [close] * 3, "high": [close * 1.1] * 3,
            "low": [close * 0.9] * 3, "close": [close] * 3,
            "volume": [1000.0] * 3,
        }).to_parquet(
            root / "ohlcv" / "binanceusdm" / "futures" / f"{symbol}_USDT_USDT-1d.parquet",
            index=False,
        )
    yield root
    shutil.rmtree(td, ignore_errors=True)


_EXPR = {"op": "div", "args": [{"field": "close"}, {"const": 2}]}


def _entry(fid: str | None = None, *, expr: Any = None) -> dict[str, Any]:
    expression = expr if expr is not None else _EXPR
    fields = ["close"]
    return {
        "factor_id": fid or derive_factor_id(expression, fields),
        "name": "test:factor",
        "expression": expression,
        "required_fields": fields,
        "operators": ["div"],
        "origin": {"ticket_id": "20260101T000000Z-aaaaaaaa", "plan": "p", "family": "f"},
        "evidence_grade": "out_of_sample",
        "statistics": {"dsr": 0.98},
        "metrics": {"ic": 0.05, "cost_adj_return": 0.01},
        "promoted_at": "2026-01-01T00:00:00Z",
    }


def _compute(metrics: dict[str, Any], **overrides: Any):
    def fn(factor, snapshot_id, manifest):
        return {
            "metrics": metrics, "evidence_grade": "out_of_sample",
            "oos_passed": True, "cost_gate_passed": True, "run_id": "20260102T000000Z-bbbbbbbb",
            **overrides,
        }
    return fn


# ---------------------------------------------------------------- P3 因子服务
def test_factor_id_is_content_identity(tmp_root):
    a = derive_factor_id(_EXPR, ["close"])
    b = derive_factor_id({"op": "div", "args": [{"field": "close"}, {"const": 2.0}]}, ["close"])
    # 规范化后 2 与 2.0 同身份
    assert a == b
    with FactorService(tmp_root) as svc:
        svc.upsert(_entry(a))
        svc.upsert(_entry(a))  # 幂等
        assert len(svc.list_factors()) == 1
        with pytest.raises(FlowError):
            svc.upsert(_entry(a, expr={"op": "mul", "args": [{"field": "close"}, {"const": 2}]}))


def test_certificates_are_versioned_by_snapshot(tmp_root):
    """两张证书挂不同快照身份：同一 factor_id 各有各的证书。"""
    from services.snapshot_service import SnapshotService

    with SnapshotService(tmp_root) as snaps:
        s1 = snaps.create(datasets=["ohlcv"], exchange="binanceusdm",
                          stamp="2026-01-01T00:00:00Z")["snapshot_id"]
    # 第二张证书挂 legacy 哨兵（不同身份，避免与 s1 产生同 manifest_digest 的重复快照）
    s2 = "legacy"
    fid = derive_factor_id(_EXPR, ["close"])
    with FactorService(tmp_root) as svc:
        svc.upsert(_entry(fid))
        svc.certify(snapshot_id=s1, factor_id=fid, caliber_digest="c",
                    evidence_grade="out_of_sample", verdict="active", metrics={"ic": 0.05},
                    at="2026-01-01T01:00:00Z")
        svc.certify(snapshot_id=s2, factor_id=fid, caliber_digest="c",
                    evidence_grade="out_of_sample", verdict="degraded", metrics={"ic": 0.02},
                    at="2026-01-02T01:00:00Z")
        assert len(svc.certificates(factor_id=fid)) == 2
        assert svc.get_cert(s1, fid)["verdict"] == "active"
        assert svc.get_cert(s2, fid)["verdict"] == "degraded"
        assert [f["factor_id"] for f in svc.list_active()] == [fid]
        # 同 cert_key 内容不同 → E2005
        with pytest.raises(FlowError):
            svc.certify(snapshot_id=s1, factor_id=fid, caliber_digest="c",
                        evidence_grade="out_of_sample", verdict="retired", metrics={"ic": 0.05})


def test_retire_materializes_to_legacy_catalog_view(tmp_root):
    from services.snapshot_service import SnapshotService

    with SnapshotService(tmp_root) as snaps:
        s1 = snaps.create(datasets=["ohlcv"], exchange="binanceusdm",
                          stamp="2026-01-01T00:00:00Z")["snapshot_id"]
    s2 = "legacy"
    fid = derive_factor_id(_EXPR, ["close"])
    with FactorService(tmp_root) as svc:
        svc.upsert(_entry(fid))
        svc.certify(snapshot_id=s1, factor_id=fid, caliber_digest="c",
                    evidence_grade="out_of_sample", verdict="active",
                    metrics={"metrics": {"ic": 0.05}}, at="2026-01-01T01:00:00Z")
        svc.materialize()
        cat = Catalog(Layout(tmp_root))
        assert cat.count() == 1
        assert cat.get(fid)["status"] == "active"
        svc.certify(snapshot_id=s2, factor_id=fid, caliber_digest="c",
                    evidence_grade="out_of_sample", verdict="retired", metrics={},
                    at="2026-01-02T01:00:00Z")
        svc.materialize()
        assert Catalog(Layout(tmp_root)).get(fid)["status"] == "retired"


def test_import_legacy_marks_pending(tmp_root):
    fid = derive_factor_id(_EXPR, ["close"])
    Catalog(Layout(tmp_root)).add(_entry(fid))
    with FactorService(tmp_root) as svc:
        imported = svc.import_legacy()
        assert imported == [fid]
        cert = svc.get_cert("legacy", fid)
        assert cert["caliber_digest"] == "unknown"
        assert json.loads(cert["metrics_json"])["pending_revalidation"] is True


# ---------------------------------------------------------------- diff / 决策
def test_diff_metrics_only_compares_present():
    d = diff_metrics({"ic": 0.05, "dsr": 0.99}, {"ic": 0.03, "dsr": 0.99, "net_return": 0.1})
    rows = {r["metric"]: r for r in d["rows"]}
    assert rows["ic"]["delta"] == pytest.approx(-0.02)
    assert rows["dsr"]["delta"] == pytest.approx(0.0)
    assert rows["net_return"]["delta"] is None
    assert d["compared"] == 2


def test_decide_rules():
    diff = diff_metrics({"ic": 0.05}, {"ic": 0.05})
    base = dict(diff=diff, current={}, evidence_grade="out_of_sample",
                oos_passed=True, cost_gate_passed=True, thresholds={})
    assert decide(**base)[0] == "active"
    assert decide(**{**base, "oos_passed": False})[0] == "retired"
    assert decide(**{**base, "cost_gate_passed": False})[0] == "retired"
    degraded = diff_metrics({"ic": 0.05}, {"ic": 0.01})
    v, reasons = decide(**{**base, "diff": degraded, "thresholds": {"ic_drop": 0.02}})
    assert v == "degraded" and "degraded:ic" in reasons


# ---------------------------------------------------------------- P4 复盘服务
def _seed_active(tmp_root) -> str:
    """建一个 active 因子（旧证书挂 legacy 哨兵）+ 一张 2026-01-02 的快照。"""
    fid = derive_factor_id(_EXPR, ["close"])
    with FactorService(tmp_root) as svc:
        svc.upsert(_entry(fid))
        svc.certify(snapshot_id="legacy", factor_id=fid, caliber_digest="c",
                    evidence_grade="out_of_sample", verdict="active",
                    metrics={"metrics": {"ic": 0.05, "cost_adj_return": 0.01}},
                    at="2026-01-01T00:00:00Z")
    from services.snapshot_service import SnapshotService
    with SnapshotService(tmp_root) as snaps:
        snaps.create(datasets=["ohlcv"], exchange="binanceusdm", stamp="2026-01-02T00:00:00Z")
    return fid


def test_enqueue_only_active_and_idempotent(tmp_root):
    fid = _seed_active(tmp_root)
    with ReplayService(tmp_root, compute=_compute({"ic": 0.05})) as svc:
        first = svc.enqueue(all_active=True, now="2026-01-03T00:00:00Z")
        assert len(first) == 1
        assert first[0]["factor_id"] == fid
        second = svc.enqueue(all_active=True, now="2026-01-03T00:00:00Z")
        assert second == []  # 重复 enqueue 幂等
        # 非 active 因子被拒绝
        with pytest.raises(FlowError):
            svc.enqueue(all_active=True, factor_ids=["sha256:" + "f" * 64])


def test_replay_run_writes_cert_and_report(tmp_root):
    fid = _seed_active(tmp_root)
    with ReplayService(
        tmp_root,
        compute=_compute({"ic": 0.02, "cost_adj_return": 0.01}, run_id="r-new"),
        thresholds={"ic_drop": 0.02},
    ) as svc:
        row = svc.enqueue(all_active=True, now="2026-01-03T00:00:00Z")[0]
        rid = row["replay_id"]
        result = svc.run(rid, now="2026-01-03T01:00:00Z")
        # replay_runs.status 描述执行成败；因子健康度在 report.decision / 证书 verdict
        assert result["status"] == "passed"
        report = result["report"]
        assert report["decision"] == "degraded"
        assert report["run_id"] == "r-new"
        assert report["thresholds"]["ic_drop"] == 0.02
        assert any(r["metric"] == "ic" and r["delta"] is not None
                   for r in report["diff"]["rows"])
        assert svc.report_path(rid).exists()
        # 证书落在目标快照下
        with FactorService(tmp_root) as factors:
            certs = factors.certificates(factor_id=fid)
            assert len(certs) == 2
            assert any(c["verdict"] == "degraded" for c in certs)


def test_replay_failure_does_not_change_certificate(tmp_root):
    fid = _seed_active(tmp_root)

    def boom(factor, snapshot_id, manifest):
        raise FlowError("E3004", "compute 失败", path="replay.compute")

    with ReplayService(tmp_root, compute=boom) as svc:
        row = svc.enqueue(all_active=True, now="2026-01-03T00:00:00Z")[0]
        rid = row["replay_id"]
        result = svc.run(rid, now="2026-01-03T01:00:00Z")
        assert result["status"] == "error"
        assert result["report"]["decision"] == "error"
        with FactorService(tmp_root) as factors:
            certs = factors.certificates(factor_id=fid)
            assert len(certs) == 1  # 没有新证书
            assert certs[0]["verdict"] == "active"
        # error 允许重新入队重试
        assert svc.enqueue(all_active=True, now="2026-01-03T02:00:00Z")


def test_snapshot_create_auto_enqueues_active(tmp_root, monkeypatch):
    from app.cli.common import envelope_ok
    from app.cli.snapshot import cmd_snapshot
    from services.factor_service import FactorService

    fid = derive_factor_id(_EXPR, ["close"])
    with FactorService(tmp_root) as svc:
        svc.upsert(_entry(fid))
        svc.certify(snapshot_id="legacy", factor_id=fid, caliber_digest="unknown",
                    evidence_grade="out_of_sample", verdict="active", metrics={},
                    at="2026-01-01T00:00:00Z")
    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_root))
    args = type("A", (), {
        "snapshot_action": "create", "datasets": "ohlcv", "exchange": "binanceusdm",
        "market": "futures", "symbols": None, "timeframe": "1d", "parent": None,
        "note": "", "no_enqueue": False,
    })()
    out = cmd_snapshot(args)
    assert len(out["data"]["auto_enqueued"]) == 1
    with ReplayService(tmp_root, compute=_compute({"ic": 0.05})) as replay:
        assert len(replay.list()) == 1


def test_materialize_preserves_legacy_catalog_rows(tmp_root):
    """物化不得抹掉尚未导入 sqlite 的 legacy 因子（promote 曾因此清空旧库）。"""
    from services.factor_service import FactorService, derive_factor_id

    legacy_expr = {"op": "add", "args": [{"field": "close"}, {"const": 1}]}
    legacy = {
        "catalog_version": "v1",
        "factor_id": "sha256:legacy_not_in_sqlite",
        "name": "legacy_factor",
        "expression": legacy_expr,
        "required_fields": ["close"],
        "operators": ["add"],
        "evidence_grade": "out_of_sample",
        "status": "active",
        "promoted_at": "2026-01-01",
    }
    Catalog(Layout(tmp_root)).add(legacy)  # 只写 jsonl，不进 sqlite

    with FactorService(tmp_root) as svc:
        svc.materialize()

    rows = {str(r["factor_id"]): r for r in Catalog(Layout(tmp_root)).entries()}
    assert "sha256:legacy_not_in_sqlite" in rows
    assert rows["sha256:legacy_not_in_sqlite"]["expression"] == legacy_expr
