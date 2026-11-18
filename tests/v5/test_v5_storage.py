"""V5 地基测试：存储层 + 数据根 + 环境快照 + 结构往返。

覆盖计划 §2 的全部验收点：
- 数据根四种配置源优先级
- 四区权限靠接口形状保证（执行器无 Depot 写方法）
- 写入即校验四条 + 原子写不产生半份
- 账本只追加 + 一次性事件去重
- duckdb 删掉可重建且幂等
- 环境快照凭据不泄漏 + E5002 硬失败
- 七类结构双向往返
- 九区路径字面量唯一定义（AC31）
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pyarrow as pa
import pytest

from v5.contracts import blob_digest
from v5.contracts.errors import E2002, E3003, E3006, E5002, E5004, FlowError
from v5.contracts import types as T
from v5.runtime.envelope import Envelope, build_envelope, require_credentials
from v5.runtime.store import (
    DEFAULT_DIRNAME, ENV_DATA_ROOT, ReadOnlyStore, StoreContext,
    TicketWriter, DepotWriter, resolve_data_root,
)
from v5.storage.index import IndexDB
from v5.storage.ledger import Ledger
from v5.storage.layout import Layout, ZONE_NAMES, all_zone_literals
from v5.storage.objects import ObjectStore

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 数据根
def test_root_priority_explicit_over_env_config_default(tmp_path):
    env = {ENV_DATA_ROOT: str(tmp_path / "env")}
    cfg = {"operations": {"data_root": str(tmp_path / "cfg")}}
    assert resolve_data_root(environ={}) == Path.home() / DEFAULT_DIRNAME
    assert resolve_data_root(config=cfg, environ={}) == tmp_path / "cfg"
    assert resolve_data_root(environ=env) == tmp_path / "env"
    assert resolve_data_root(tmp_path / "explicit", cfg, environ=env) == tmp_path / "explicit"


def test_root_rejects_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FlowError) as e:
        resolve_data_root("relative/path", environ={})
    assert e.value.error.code == E5004


def test_default_lands_in_user_home():
    root = resolve_data_root(environ={})
    assert root.is_absolute()
    assert root.name == DEFAULT_DIRNAME
    assert str(root).startswith(str(Path.home()))


# ---------------------------------------------------------------- 权限接口
def test_ticket_writer_has_no_depot_write_methods(tmp_path):
    ctx = StoreContext.from_root(tmp_path)
    tw = ctx.for_ticket("T1")
    dw = ctx.for_depot()
    assert isinstance(tw, ReadOnlyStore)
    assert not hasattr(tw, "publish_bytes")
    assert not hasattr(tw, "for_depot")
    assert hasattr(dw, "publish_bytes")
    assert not hasattr(dw, "write_bytes")


def test_ticket_writer_rejects_path_escape(tmp_path):
    tw = StoreContext.from_root(tmp_path).for_ticket("T1")
    with pytest.raises(FlowError) as e:
        tw.write_bytes("../etc/passwd", b"x")
    assert e.value.error.code == E5004


def test_ticket_writer_digest_mismatch_and_idempotence(tmp_path):
    tw = StoreContext.from_root(tmp_path).for_ticket("T1")
    with pytest.raises(FlowError) as e:
        tw.write_bytes("a.json", b"{}", expected_digest="sha256:" + "0" * 64)
    assert e.value.error.code == E5004
    p1 = tw.write_bytes("a.json", b'{"a": 1}')
    p2 = tw.write_bytes("a.json", b'{"a": 1}')  # 幂等
    assert p1 == p2
    with pytest.raises(FlowError) as e:
        tw.write_bytes("a.json", b'{"a": 2}')  # 异内容拒绝覆盖
    assert e.value.error.code == E5004


def test_read_only_store_rejects_escape(tmp_path):
    reader = StoreContext.from_root(tmp_path).reader
    with pytest.raises(FlowError) as e:
        reader.read_bytes(tmp_path.parent / "outside.bin")
    assert e.value.error.code == E5004


# ---------------------------------------------------------------- 对象存储
def test_object_store_content_addressing_and_schema_sidecar(tmp_path):
    L = Layout(tmp_path)
    store = ObjectStore(L)
    table = pa.table({"close": pa.array([1.0, 2.0, 3.0], type=pa.float64()),
                      "rank": pa.array([1, 2, 3], type=pa.float32())})
    ref = store.write_table("panels", table)
    assert ref.digest.startswith("sha256:")
    assert (L.fact_kind("panels") / (ref.digest.split(":")[1] + ".parquet")).exists()
    sidecar = L.sidecar(str(L.root / ref.relpath))
    assert json.loads(sidecar.read_text(encoding="utf-8"))["schema_version"] == ref.schema_version
    out = store.read_table(ref)
    assert out.column_names == ["close", "rank"]
    assert out.num_rows == 3
    # 同内容再写命中同一地址
    assert store.write_table("panels", table).digest == ref.digest


def test_object_store_detects_tampering(tmp_path):
    L = Layout(tmp_path)
    store = ObjectStore(L)
    ref = store.write_json("reports", {"n": 1})
    target = L.root / ref.relpath
    target.write_text('{"n": 2}', encoding="utf-8")
    with pytest.raises(FlowError) as e:
        store.read_json(ref)
    assert e.value.error.code == E5004


def test_object_store_rejects_bad_kind(tmp_path):
    store = ObjectStore(Layout(tmp_path))
    with pytest.raises(ValueError):
        store.write_json("unknown_kind", {})


# ---------------------------------------------------------------- 账本
def test_ledger_append_only_and_dedup(tmp_path):
    L = Layout(tmp_path)
    led = Ledger(L)
    led.append("trials", {"event_id": "t1", "kind": "trial"})
    led.append("trials", {"event_id": "t2", "kind": "trial"})
    assert led.count("trials") == 2
    with pytest.raises(FlowError) as e:
        led.append("trials", {"event_id": "t1", "kind": "trial"})
    assert e.value.error.code == E3006
    assert led.count("trials") == 2
    assert len(Ledger(L).records("trials")) == 2  # 新实例从盘上重建 seen


def test_ledger_rejects_unknown_register(tmp_path):
    with pytest.raises(FlowError) as e:
        Ledger(Layout(tmp_path)).append("bogus", {})
    assert e.value.error.code == E3006


def test_ledger_merges_locked_rescan_into_instance_view(tmp_path):
    """锁内重扫的结果必须回并 `_seen`，否则实例视图与磁盘长期漂移。

    场景：A 实例构造后，B 实例（另一个进程）写入 e1。A 下次 append 会重扫
    并看见 e1——但若只在重扫里判重、不回并 `_seen`，A 的视图里永远没有 e1，
    任何基于 `_seen` 的「我见过吗」判断都会给出与磁盘相反的答案。
    """
    L = Layout(tmp_path)
    a = Ledger(L)
    b = Ledger(L)
    b.append("events", {"event_id": "e1", "kind": "Reveal", "ticket_id": "T1"})
    assert "e1" not in a._seen["events"]  # 构造时看不见后写入的
    a.append("events", {"event_id": "e2", "kind": "Reveal", "ticket_id": "T2"})
    assert "e1" in a._seen["events"]  # 重扫后回并
    assert a._seen["events"] == b._seen["events"] | {"e2"}


def test_fact_kind_whitelist_has_single_source(tmp_path):
    """index / gc / fact_kind 必须引用同一份白名单（`layout.FACT_KINDS`）。

    曾经 index 手抄 5 类、漏掉 `parcel_data`，那类对象永不进索引、永不受
    一致性校验，而 GC 认为它是活对象——「谁算活对象」有两个答案且不报错。
    """
    from v5.storage import gc as gc_mod
    from v5.storage import index as index_mod
    from v5.storage.layout import FACT_KINDS

    assert gc_mod._KINDS is FACT_KINDS
    # fact_kind 只接受白名单内的类别
    L = Layout(tmp_path)
    assert [p.name for p in (L.fact_kind(k) for k in FACT_KINDS)] == list(FACT_KINDS)
    with pytest.raises(ValueError):
        L.fact_kind("not_a_kind")
    # 索引重建按同一份白名单枚举（源码层面的一致性守卫）
    import inspect
    src = inspect.getsource(index_mod.IndexDB._rebuild_object_rows)
    assert "FACT_KINDS" in src


# ---------------------------------------------------------------- 索引库
def test_index_rebuild_is_idempotent_and_truth_is_ledger(tmp_path):
    L = Layout(tmp_path)
    led = Ledger(L)
    store = ObjectStore(L)
    led.append("events", {"event_id": "e1", "kind": "Reveal", "ticket_id": "T1"})
    store.write_table("panels", pa.table({"x": pa.array([1.0])}))
    idx = IndexDB(L)
    first = idx.reconcile(led)
    assert first["events"] == 1 and first["objects"] == 1
    assert idx.assert_consistent(led)["events"]["ok"]
    assert idx.query("SELECT COUNT(*) AS n FROM reg_events")[0]["n"] == 1
    # 删掉索引 → 重建逐位一致（索引不是真相）
    idx.path.unlink()
    assert idx.reconcile(led) == first
    assert idx.assert_consistent(led)["events"]["ok"]


def test_index_detects_inconsistency(tmp_path):
    L = Layout(tmp_path)
    led = Ledger(L)
    idx = IndexDB(L)
    idx.reconcile(led)
    led.append("events", {"event_id": "late", "kind": "Publish"})
    with pytest.raises(FlowError) as e:
        idx.assert_consistent(led)
    assert e.value.error.code == E5004


# ---------------------------------------------------------------- Envelope
def test_envelope_has_no_secret_values(tmp_path):
    e = build_envelope(tmp_path, packages={"duckdb": "2.0.0"},
                       credential_env_names={"exchange": "BINANCE_API_KEY"})
    d = e.to_dict()
    assert "BINANCE_API_KEY" in d["credential_refs"].values()
    assert "BINANCE_API_KEY" not in d.get("package_versions", {})
    assert d["clock_basis"] == "utc"
    assert d["data_root_resolved"] == str(tmp_path)
    assert e.digest.startswith("sha256:")


def test_envelope_missing_credential_is_hard_failure():
    with pytest.raises(FlowError) as e:
        require_credentials({"exchange": "NO_SUCH_VAR_XYZ"}, environ={})
    assert e.value.error.code == E5002


def test_envelope_cross_env_comparison_never_fails(tmp_path):
    e1 = build_envelope(tmp_path, packages={"duckdb": "2.0.0"})
    e2 = build_envelope(tmp_path / "other", packages={"duckdb": "2.1.0"})
    cmp = e1.compare(e2, parcel_digest_same=False)
    assert cmp["env_mismatch"] is True
    assert cmp["reproduction_failed"] is True
    cmp2 = e1.compare(e1, parcel_digest_same=True)
    assert cmp2["env_mismatch"] is False


# ---------------------------------------------------------------- 结构往返
def _digest():
    return T.Digest.parse("sha256:" + "a" * 64)


def _ref():
    return T.Ref(kind="derived", id="R1", digest=_digest(), relpath="facts/panels/x.parquet", schema_version="1")


def test_parcel_roundtrip():
    from datetime import datetime, timezone
    parcel = T.Parcel(
        ticket_id="20261116T053000Z-1a2b3c4d", station="Evidence", station_revision="r1",
        impl="observe", impl_version="1.0", call_digest=_digest(), payload_ref=_ref(),
        payload_digest=_digest(), digest=_digest(), lineage=[("Panel", _digest())],
        evidence=[], origin="run", status="done", skip_reason=None, error=None,
        started_at=datetime(2026, 11, 16, tzinfo=timezone.utc), seconds=1.5, log_ref=_ref(),
    )
    assert T.Parcel.from_dict(parcel.to_dict()).to_dict() == parcel.to_dict()


def test_ticket_roundtrip():
    from datetime import datetime, timezone
    pin = T.Pin(
        dataset="ohlcv", version="a" * 16, checksum=_digest(),
        coverage={"start": datetime(2026, 1, 1, tzinfo=timezone.utc),
                  "end": datetime(2026, 11, 1, tzinfo=timezone.utc)},
        symbols_digest=_digest(), end_closed=True, schema_version="1",
        acquired_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
        gate_a={"passed": True, "reason_code": None, "checked_at": "2026-11-16T00:00:00Z"},
    )
    ticket = T.Ticket(
        ticket_id="20261116T053000Z-1a2b3c4d", plan_digest=_digest(),
        blueprint_digest=_digest(), policy_digest=_digest(), caliber_digest=_digest(),
        baseline_version="v1", family="family_v1",
        family_declared_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        master_seed=42, pins=[pin], domain_digest=_digest(), status="pending",
        created_at=datetime(2026, 11, 16, tzinfo=timezone.utc), environment_ref=_ref(),
        archived_at=None,
    )
    assert T.Ticket.from_dict(ticket.to_dict()).to_dict() == ticket.to_dict()


def test_blueprint_roundtrip():
    from datetime import datetime, timezone
    bp = T.Blueprint(
        values={"caliber": {"h": 5}}, origins={"caliber.h": "plan"},
        resolved={"caliber.h": 5}, caliber={"h": 5}, supply_pins=[], domain={},
        family_declaration={"family": "f1", "declared_at": "2026-11-01T00:00:00Z"},
        station_order=[("Boundary", "r1")], subcall_graph={"Evidence": []},
        digests={"plan": _digest(), "blueprint": _digest(), "policy": _digest(),
                 "domain": _digest(), "supply": _digest(), "caliber": _digest()},
        check_results=[], compiled_at=datetime(2026, 11, 16, tzinfo=timezone.utc),
    )
    assert T.Blueprint.from_dict(bp.to_dict()).to_dict() == bp.to_dict()


def test_from_dict_rejects_unknown_field():
    d = _ref().to_dict()
    d["unexpected"] = 1
    with pytest.raises(FlowError):
        T.Ref.from_dict(d)


# ---------------------------------------------------------------- 路径守卫
def test_zone_literals_defined_only_in_layout():
    """除 layout.py 外，v5 代码不得把九区名与 Path 拼接（AC31）。"""
    violations = []
    zones = set(all_zone_literals())
    for path in sorted((ROOT / "v5").rglob("*.py")):
        if path.name == "layout.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            # Path(...) / "facts" 形式：右侧是字符串常量
            right = node.right
            if isinstance(right, ast.Constant) and isinstance(right.value, str) and right.value in zones:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {right.value}")
    assert not violations, "九区路径字面量只能在 layout.py 出现：\n" + "\n".join(violations)


def test_layout_exposes_all_nine_zones(tmp_path):
    L = Layout(tmp_path)
    for name in ZONE_NAMES:
        assert getattr(L, name) == tmp_path / name
    assert L.ledger == tmp_path / "db" / "ledger.jsonl"
    assert L.index_db == tmp_path / "db" / "meta.duckdb"


# ---------------------------------------------------------------- review 回归
def test_ticket_writer_rejects_path_like_id(tmp_path):
    """ticket_id 是路径段：`../..` 会把校验根一起挪到数据根外（P0）。"""
    ctx = StoreContext.from_root(tmp_path)
    for bad in ("../../db", "a/b", "..", "", "T1/../T2"):
        with pytest.raises(FlowError) as e:
            ctx.for_ticket(bad)
        assert e.value.error.code == E5004


def test_append_jsonl_rejects_non_jsonl_target(tmp_path):
    """追加写不得绕过不可变产物的"异内容拒绝覆盖"。"""
    tw = StoreContext.from_root(tmp_path).for_ticket("T1")
    tw.write_bytes("parcel.json", b'{"a": 1}')
    with pytest.raises(FlowError) as e:
        tw.append_jsonl("parcel.json", {"x": 1})
    assert e.value.error.code == E5004
    assert tw.read_bytes(tw._target("parcel.json")) == b'{"a": 1}'


def test_depot_writer_confined_to_supply(tmp_path):
    """DepotWriter 只能写 supply/：否则可覆盖别 Ticket 产物或自建账本。"""
    dw = StoreContext.from_root(tmp_path).for_depot()
    with pytest.raises(FlowError) as e:
        dw.publish_bytes(tmp_path / "db" / "ledger.jsonl", b"{}")
    assert e.value.error.code == E5004
    ok = dw.publish_bytes(tmp_path / "supply" / "ohlcv" / "v1" / "x.json", b"{}")
    assert ok.exists()


def test_object_read_rejects_escaping_relpath(tmp_path):
    from v5.storage.objects import ObjectRef, ObjectStore

    store = ObjectStore(Layout(tmp_path))
    ref = ObjectRef(kind="panels", digest="sha256:" + "a" * 64,
                    relpath="../outside.parquet", schema_version=1)
    with pytest.raises(FlowError) as e:
        store.read_table(ref)
    # 路径校验先于读盘：`..` 在 assert_relative_path 就被拒（E2002）
    assert e.value.error.code in {E2002, E5004, E3003}


def test_index_objects_digest_uses_sha256_prefix(tmp_path):
    from v5.storage.index import IndexDB
    from v5.storage.ledger import Ledger

    L = Layout(tmp_path)
    store = ObjectStore(L)
    ref = store.write_table("panels", pa.table({"x": pa.array([1.0], type=pa.float64())}))
    idx = IndexDB(L)
    idx.reconcile(Ledger(L))
    rows = idx.query("SELECT digest, relpath FROM objects")
    assert rows and rows[0]["digest"] == ref.digest
    assert "\\" not in rows[0]["relpath"]


def test_envelope_rejects_secret_value(tmp_path):
    """引用名必须是环境变量名；把值塞进来会随快照落盘。"""
    for bad in ({"exchange": "sk_live_ABCDEF123456"}, {"exchange": "abc123"}):
        with pytest.raises(FlowError) as e:
            build_envelope(tmp_path, credential_env_names=bad)
        assert e.value.error.code == "E2004"


# 供给维护链的路径（lease / staging / seed / 维护日志）随 19-supply.md 方案 B
# 一并删除：没有暂存就没有 staging，没有版本目录就没有 seed_dir，没有别名表
# 就没有 published_aliases。数据落在 <root>/<dataset>/<exchange>/<market>/，
# 路径规则归 v5/upstream/store.py 独有，这里不再重复一遍。




def test_station_names_match_specs():
    """`catalog._STATION_NAMES` 必须是 `STATION_SPECS` 的投影。

    storage 不 import orchestration（层次方向），所以两份清单并存；这条守卫
    保证它们不会悄悄分叉——加一个工位却漏改目录名匹配，`read_ticket` 会把那个
    工位当成「目录不存在」，静默少读一个产物。
    """
    from v5.orchestration.station import STATION_SPECS
    from v5.storage.catalog import _STATION_NAMES

    assert tuple(spec.name for spec in STATION_SPECS) == _STATION_NAMES
