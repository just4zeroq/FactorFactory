"""sqlite 基础设施 + 快照服务 + 计划快照解析测试（docs/21）。

全部使用临时数据根，不读不写真实 ~/factorfactory。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from contracts.errors import FlowError
from services.snapshot_service import SnapshotService
from storage.sqlite_store import SqliteStore, ddl_checksum, resolve_db_path


@pytest.fixture()
def tmp_root():
    td = tempfile.mkdtemp(prefix="ff-snap-")
    root = Path(td)
    (root / "ohlcv" / "binanceusdm" / "futures").mkdir(parents=True)
    (root / "funding" / "binanceusdm" / "futures").mkdir(parents=True)
    for symbol, close in (("BTC", 100.0), ("ETH", 10.0)):
        pd.DataFrame({
            "symbol": [f"{symbol}/USDT:USDT"] * 3,
            "timestamp": pd.to_datetime([
                "2024-01-01", "2024-01-02", "2024-01-03",
            ], utc=True),
            "open": [close] * 3, "high": [close * 1.1] * 3,
            "low": [close * 0.9] * 3, "close": [close] * 3,
            "volume": [1000.0] * 3,
        }).to_parquet(
            root / "ohlcv" / "binanceusdm" / "futures" / f"{symbol}_USDT_USDT-1d.parquet",
            index=False,
        )
        pd.DataFrame({
            "symbol": [f"{symbol}/USDT:USDT"] * 3,
            "timestamp": pd.to_datetime([
                "2024-01-01", "2024-01-02", "2024-01-03",
            ], utc=True),
            "funding_rate": [0.0001, -0.0002, 0.0003],
        }).to_parquet(
            root / "funding" / "binanceusdm" / "futures" / f"{symbol}_USDT_USDT.parquet",
            index=False,
        )
    yield root
    shutil.rmtree(td, ignore_errors=True)


def test_migration_idempotent_and_checksum_guard(tmp_root):
    path = resolve_db_path(tmp_root)
    store = SqliteStore(path)
    assert store.migrate(now="2026-01-01T00:00:00Z") == 1
    assert store.migrate(now="2026-01-02T00:00:00Z") == 1
    rows = store.query("SELECT * FROM schema_migrations")
    assert len(rows) == 1
    from storage.sqlite_store import _migrations
    assert rows[0]["checksum"] == ddl_checksum(_migrations()[0][1])
    store.close()

    store = SqliteStore(path)
    store.execute("UPDATE schema_migrations SET checksum='sha256:bad'")
    with pytest.raises(FlowError):
        store.migrate(now="2026-01-03T00:00:00Z")
    store.close()


def test_readonly_connection_cannot_write(tmp_root):
    path = resolve_db_path(tmp_root)
    writable = SqliteStore(path)
    writable.migrate(now="t")
    writable.execute(
        "INSERT INTO snapshots(snapshot_id,manifest_digest,created_at,status,span_start,span_end)"
        " VALUES('s','d','t','active','a','b')")
    writable.close()
    ro = SqliteStore(path, readonly=True)
    assert ro.one("SELECT count(*) n FROM snapshots")["n"] == 1
    with pytest.raises(FlowError):
        ro.execute("DELETE FROM snapshots")
    ro.close()


def test_snapshot_entries_come_from_files(tmp_root):
    with SnapshotService(tmp_root) as svc:
        m = svc.create(datasets=["ohlcv", "funding"], exchange="binanceusdm")
        assert len(m["entries"]) == 4
        assert m["span"]["start"].startswith("2024-01-01")
        assert m["span"]["end"].startswith("2024-01-03")
        for e in m["entries"]:
            assert e["row_count"] == 3
            assert e["blob_digest"].startswith("sha256:")
            assert (tmp_root / e["relpath"]).exists()


def test_create_idempotent_and_resolve_latest(tmp_root):
    with SnapshotService(tmp_root) as svc:
        a = svc.create(datasets=["ohlcv", "funding"], exchange="binanceusdm")
        b = svc.create(datasets=["ohlcv", "funding"], exchange="binanceusdm")
        assert a["snapshot_id"] == b["snapshot_id"]
        assert svc.resolve("latest") == a["snapshot_id"]
        assert svc.resolve(None) == a["snapshot_id"]
        assert svc.resolve(a["snapshot_id"]) == a["snapshot_id"]


def test_archive_blocks_new_plan_reference(tmp_root):
    with SnapshotService(tmp_root) as svc:
        m = svc.create(datasets=["ohlcv"], exchange="binanceusdm")
        svc.archive(m["snapshot_id"])
        assert svc.list(status="archived")[0]["snapshot_id"] == m["snapshot_id"]
        with pytest.raises(FlowError):
            svc.resolve(m["snapshot_id"])


def test_plan_resolver_rejects_mutated_supply(tmp_root):
    from app.cli.common import resolve_snapshot_for_plan

    with SnapshotService(tmp_root) as svc:
        m = svc.create(datasets=["ohlcv"], exchange="binanceusdm")
        sid = m["snapshot_id"]
    plan = {
        "supply": {
            "datasets": ["ohlcv"], "exchange": "binanceusdm",
            "market": "futures", "snapshot": sid,
        },
    }
    got_sid, _manifest = resolve_snapshot_for_plan(plan, tmp_root)
    assert got_sid == sid
    target = tmp_root / "ohlcv" / "binanceusdm" / "futures" / "BTC_USDT_USDT-1d.parquet"
    target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(FlowError):
        resolve_snapshot_for_plan(plan, tmp_root)


def test_plan_resolver_auto_creates_for_legacy(tmp_root):
    from app.cli.common import resolve_snapshot_for_plan

    plan = {
        "supply": {
            "datasets": ["ohlcv"], "exchange": "binanceusdm",
            "market": "futures",
        },
    }
    with pytest.raises(FlowError):
        resolve_snapshot_for_plan(plan, tmp_root, auto_create=False)
    sid, _manifest = resolve_snapshot_for_plan(plan, tmp_root, auto_create=True)
    assert sid.startswith("snap-")
