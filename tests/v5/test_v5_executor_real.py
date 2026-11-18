"""S1 执行器真实化验收：调用单六通道 + 指纹链 + 真实依赖 + 落盘往返。

与 test_v5_orchestration.py（S0 骨架接口）互补：这里断言 S1 新增的真实语义。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from v5.contracts import digest as dg
from v5.contracts.errors import E1004, E3001, E3002, E3003, E3004, FlowError
from v5.contracts.types import Parcel as ContractParcel
from v5.facilities import build_facilities
from v5.orchestration import Executor
from v5.orchestration.station import BY_NAME, STATION_SPECS, input_producers
from v5.runtime.store import StoreContext

FROZEN = datetime(2026, 11, 16, tzinfo=timezone.utc)


def _fx(seed=1):
    return build_facilities(scope="ticket", ticket_id="T1", master_seed=seed, frozen_at=FROZEN)


def _err(fn):
    with pytest.raises(FlowError) as e:
        fn()
    return e.value.error.code


# ---------------------------------------------------------------- 六通道
def test_call_digest_includes_policy_channel():
    a = Executor(_fx(), policy_view={"family": "f1"})
    b = Executor(_fx(), policy_view={"family": "f2"})
    da = a._call_digest("T", 1, BY_NAME["Boundary"], [])
    db = b._call_digest("T", 1, BY_NAME["Boundary"], [])
    assert da != db


def test_call_digest_includes_runtime_facts_channel():
    a = Executor(_fx(), runtime_facts={"n_trials": 10})
    b = Executor(_fx(), runtime_facts={"n_trials": 11})
    assert a._call_digest("T", 1, BY_NAME["Boundary"], []) != b._call_digest("T", 1, BY_NAME["Boundary"], [])


def test_call_digest_includes_params_channel():
    a = Executor(_fx(), station_params={"Boundary": {"k": 1}})
    b = Executor(_fx(), station_params={"Boundary": {"k": 2}})
    assert a._call_digest("T", 1, BY_NAME["Boundary"], []) != b._call_digest("T", 1, BY_NAME["Boundary"], [])


def test_call_digest_includes_seed_and_phase():
    ex = Executor(_fx(seed=1))
    assert ex._call_digest("T", 1, BY_NAME["Search"], []) != Executor(_fx(seed=2))._call_digest("T", 1, BY_NAME["Search"], [])
    assert ex._call_digest("T", 1, BY_NAME["Search"], [], phase="fit") != ex._call_digest("T", 1, BY_NAME["Search"], [], phase="generate")


def test_call_digest_includes_input_digests():
    ex = Executor(_fx())
    d0 = ex._call_digest("T", 2, BY_NAME["Panel"], [])
    d1 = ex._call_digest("T", 2, BY_NAME["Panel"], ["sha256:" + "a" * 64])
    assert d0 != d1


def test_plan_station_block_enters_call_digest():
    """工位块（params/time_axis）必须进指纹：否则不同方案会命中同一缓存。"""
    a = Executor(_fx(), station_params={"boundary": {"params": {"k": 60}}})
    b = Executor(_fx(), station_params={"boundary": {"params": {"k": 61}}})
    c = Executor(_fx(), station_params={"boundary": {"time_axis": {"mine": "2024..2026"}}})
    da = a._call_digest("T", 1, BY_NAME["Boundary"], [])
    assert da != b._call_digest("T", 1, BY_NAME["Boundary"], [])
    assert da != c._call_digest("T", 1, BY_NAME["Boundary"], [])


def test_lowercase_station_keys_normalize():
    """plan 用小写键，规格表用大写名；归一失败会静默丢参数。"""
    a = Executor(_fx(), station_params={"search": {"max": 1}})
    b = Executor(_fx(), station_params={"Search": {"max": 1}})
    assert a.station_params == b.station_params == {"Search": {"max": 1}}
    assert _err(lambda: Executor(_fx(), station_params={"nope": {}})) == E1004


def test_plan_impl_overrides_spec_default():
    """plan 声明 impl 时必须生效，且进 call_digest 与 Parcel.impl。"""
    a = Executor(_fx())
    b = Executor(_fx(), station_impls={"boundary": "pit_dynamic_topk"})
    assert a._call_digest("T", 1, BY_NAME["Boundary"], []) != \
        b._call_digest("T", 1, BY_NAME["Boundary"], [])
    parcel = b.execute("T").parcels["Boundary"]
    assert parcel.impl == "pit_dynamic_topk@1"
    assert parcel.to_contract().impl == "pit_dynamic_topk"


def test_trace_carries_real_channels():
    r = Executor(_fx()).execute("T")
    panel = next(t for t in r.traces if t["station"] == "Panel")
    assert panel["inputs"] and panel["inputs"][0][0] == "Boundary"
    assert panel["call_digest"].startswith("sha256:")
    assert all(t["params_digest"].startswith("sha256:") for t in r.traces)


# ---------------------------------------------------------------- 指纹链
def test_fingerprint_chain_upstream_change_propagates():
    """上游 payload 变 → 上游 parcel_digest 变 → 下游 call_digest/parcel 全变。"""
    base = Executor(_fx()).execute("T")
    changed = Executor(_fx(), dummy_fn=lambda s, i: {"station": s, "kind": "dummy", "payload": {"x": 1}} if s == "Boundary" else {"station": s, "kind": "dummy", "payload": {}}).execute("T")
    assert base.parcels["Boundary"].parcel_digest != changed.parcels["Boundary"].parcel_digest
    for name in ("Panel", "Target", "Search", "Evidence", "Verdict", "Portfolio", "Certify"):
        assert base.parcels[name].call_digest != changed.parcels[name].call_digest, name
        assert base.parcels[name].parcel_digest != changed.parcels[name].parcel_digest, name


def test_lineage_is_ordered_upstream_digests():
    r = Executor(_fx()).execute("T")
    search = r.parcels["Search"]
    assert [n for n, _ in search.lineage] == ["Target", "Panel"]
    for name, d in search.lineage:
        assert r.parcels[name].parcel_digest == d


def test_input_producers_all_upstream_expands():
    certify = next(s for s in STATION_SPECS if s.name == "Certify")
    assert len(input_producers(certify)) == 7


# ---------------------------------------------------------------- 依赖校验
def test_real_dependency_missing_upstream_e3001():
    ex = Executor(_fx())
    spec = BY_NAME["Panel"]  # 依赖 Boundary
    assert _err(lambda: ex._resolve_inputs(spec, {})) == E3001


def test_stale_upstream_e3002():
    boundary = Executor(_fx()).execute("T").parcels["Boundary"]
    ex = Executor(_fx(), stale=["Boundary"])
    assert _err(lambda: ex._resolve_inputs(BY_NAME["Panel"], {"Boundary": boundary})) == E3002


# ---------------------------------------------------------------- 落盘往返
def test_parcel_persists_and_roundtrips(tmp_path):
    ctx = StoreContext.from_root(tmp_path)
    writer = ctx.for_ticket("20261116T000000Z-abcdef12")
    ex = Executor(_fx(), writer=writer)
    r = ex.execute("20261116T000000Z-abcdef12")
    boundary = r.parcels["Boundary"]
    p = boundary.to_contract()
    # to_dict/from_dict 无损往返
    assert ContractParcel.from_dict(p.to_dict()) == p
    # 落盘文件存在且可解析
    files = list((tmp_path / "runs" / "20261116T000000Z-abcdef12").rglob("parcel.json"))
    assert len(files) == 8
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["call_digest"].startswith("sha256:")
    assert doc["lineage"] == [] or isinstance(doc["lineage"], list)


def test_payload_and_log_refs_point_to_real_files(tmp_path):
    """Ref 不能只是"声明"：relpath 必须存在，且 log_ref.digest = 文件字节摘要。"""
    from v5.contracts.digest import blob_digest

    tid = "20261116T000000Z-abcdef12"
    root = tmp_path / "runs" / tid
    ex = Executor(_fx(), writer=StoreContext.from_root(tmp_path).for_ticket(tid))
    parcel = ex.execute(tid).parcels["Panel"]
    record = parcel.to_contract()

    payload_file = root / record.payload_ref.relpath
    log_file = root / record.log_ref.relpath
    assert payload_file.is_file() and log_file.is_file()
    assert blob_digest(log_file.read_bytes()) == str(record.log_ref.digest)
    # payload 的指纹是结构指纹：从文件读回重算必须一致
    assert dg.compute_digest(json.loads(payload_file.read_text(encoding="utf-8"))) \
        == str(record.payload_digest)


def test_trace_records_seconds_and_appends_jsonl(tmp_path):
    """trace 必须带真实 seconds，且逐工位追加到 trace.jsonl（失败也要有）。"""
    tid = "20261116T000000Z-abcdef12"
    ex = Executor(_fx(), writer=StoreContext.from_root(tmp_path).for_ticket(tid))
    r = ex.execute(tid)
    for tr in r.traces:
        assert tr["seconds"] >= 0.0
    # 每个工位 seconds 与 Parcel 一致，不再恒为 0.0 的占位（冻结时钟下为 0 也合法）
    for tr in r.traces:
        assert tr["seconds"] == r.parcels[tr["station"]].seconds
    lines = (tmp_path / "runs" / tid / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    assert [json.loads(x)["seq"] for x in lines] == list(range(1, 9))


def test_failed_station_writes_failed_trace_and_no_parcel(tmp_path):
    """第 8 步后失败：写 status=failed 的 trace，且不留下半成品 parcel.json。"""
    tid = "20261116T000000Z-abcdef12"
    root = tmp_path / "runs" / tid

    def boom(station, inputs):
        if station == "Target":
            raise FlowError(E3003, "empty")
        return {"station": station, "kind": "dummy", "payload": {}}

    ex = Executor(_fx(), writer=StoreContext.from_root(tmp_path).for_ticket(tid),
                  dummy_fn=boom)
    assert _err(lambda: ex.execute(tid)) == E3003

    traces = [json.loads(x) for x in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert traces[-1]["station"] == "Target"
    assert traces[-1]["status"] == "failed"
    assert traces[-1]["error_code"] == E3003
    assert traces[-1]["parcel_digest"] is None
    # 失败工位没有 parcel.json，已完成的上游各有一份
    parcels = sorted(p.parent.name for p in root.rglob("parcel.json"))
    assert len(parcels) == 2 and all(n.startswith(("01-", "02-")) for n in parcels)


def test_unexpected_exception_writes_failed_trace_as_e3004(tmp_path):
    """P1-10：能力抛 NotImplementedError 时也要留 failed trace（E3004）。"""
    tid = "20261116T000000Z-abcdef12"
    root = tmp_path / "runs" / tid

    def boom(station, inputs):
        if station == "Target":
            raise NotImplementedError("capability not implemented")
        return {"station": station, "kind": "dummy", "payload": {}}

    ex = Executor(_fx(), writer=StoreContext.from_root(tmp_path).for_ticket(tid),
                  dummy_fn=boom)
    assert _err(lambda: ex.execute(tid)) == E3004

    traces = [json.loads(x) for x in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert traces[-1]["station"] == "Target"
    assert traces[-1]["status"] == "failed"
    assert traces[-1]["error_code"] == E3004
    assert traces[-1]["parcel_digest"] is None


# ---------------------------------------------------------------- 边界
def test_commit_mode_runs_the_chain_but_never_fires_events():
    """S5：`mode=commit` 是合法枚举，且**不触发一次性事件**。

    整体提交仍然是「重跑一遍」，差别只在写入范围与状态推进；事件只由
    `mode=run` 触发，否则单步/提交就是绕开整体执行的计数后门（I7）。
    """
    ex = Executor(_fx())
    result = ex.execute("T", mode="commit")
    assert len(result.parcels) == 8
    assert all(p.origin == "commit" for p in result.parcels.values())
    assert result.events == []
    assert all(p.base_relpath().startswith("parcels/") for p in result.parcels.values())


def test_step_mode_writes_isolated_scope_dir():
    """`mode=step` 的产物落 `steps/`，与正式产物分目录。"""
    result = Executor(_fx()).execute("T", mode="step")
    assert all(p.scope_dir() == "steps" for p in result.parcels.values())
    assert all(p.base_relpath().startswith("steps/") for p in result.parcels.values())


def test_unknown_mode_is_param_error():
    assert _err(lambda: Executor(_fx()).execute("T", mode="bogus")) == E1004


# ---------------------------------------------------------------- Budget 计数接线
def test_budget_event_uses_search_n_trials_from_payload():
    """Budget 事件值来自 Search 工位产物的实测去重候选数，不是写死的 0。

    S1 的 dummy 桩没有 n_trials → 仍是 0；真实 search 能力把 n_trials 放进
    payload 后，执行器自动采用（计数只有能力层一份真相，执行器不另算）。
    """
    def with_count(station, inputs):
        payload = {"station": station, "kind": "real", "payload": {}}
        if station == "Search":
            payload["n_trials"] = 42
        return payload

    ex = Executor(_fx(), dummy_fn=with_count)
    r = ex.execute("TB")
    budget = [e for e in r.events if e["kind"] == "Budget"]
    assert len(budget) == 1
    assert budget[0]["value"] == 42


def test_budget_event_defaults_zero_without_n_trials():
    r = Executor(_fx()).execute("TC")
    budget = [e for e in r.events if e["kind"] == "Budget"]
    assert budget[0]["value"] == 0


def test_budget_count_bool_and_negative_ignored():
    """bool/负数不得冒充计数（bool 是 int 子类）。"""
    def bogus(station, inputs):
        payload = {"station": station, "kind": "real", "payload": {}}
        if station == "Search":
            payload["n_trials"] = True
        return payload
    r = Executor(_fx(), dummy_fn=bogus).execute("TD")
    assert [e for e in r.events if e["kind"] == "Budget"][0]["value"] == 0


def test_parcel_is_constructed_in_exactly_one_place():
    """`execute` 与 `execute_station` 曾各抄一遍 12 字段的 parcel 构造。

    抄两份的代价不在行数：判据只改一处时，另一条路径会继续接受空产物、继续跳过
    `verify()`，而两条路径的产物长得一模一样，谁都不会报错。这条断言把
    "再抄一遍"挡在 CI 里。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "v5" / "orchestration"
           / "executor.py").read_text(encoding="utf-8")
    # 两条执行路径都必须走同一处构造（`load_committed_parcel` 是从契约记录复原，
    # 不是新建产物，不受这条约束）。
    assert src.count("self._parcel_for(") == 2, "有执行路径绕开了 _parcel_for"
    assert src.count("def _parcel_for(") == 1

def test_trace_log_is_fed_through_facility(tmp_path):
    """F1：trace 不再绕过设施直接写 jsonl——落盘前先登记 `fx.trace`。"""
    tid = "20261116T000001Z-abcdef12"
    fx = _fx()
    ex = Executor(fx, writer=StoreContext.from_root(tmp_path).for_ticket(tid))
    result = ex.execute(tid)
    assert [t.seq for t in fx.trace.records()] == list(range(1, 9))
    assert [t.to_dict() for t in fx.trace.records()] == result.traces


def test_digest_goes_through_the_injected_facility():
    """F1：payload / params 指纹经 `fx.digest`，STL 里只有 `compute_digest` 一份算法。

    这里不改算法，只钉住路径——设施被替换时指纹必须跟着换实现。
    """
    from v5.contracts.digest import compute_digest

    fx = _fx()
    assert fx.digest.of({"a": 1, "b": [2, 3]}) == compute_digest({"a": 1, "b": [2, 3]})


def test_cache_lookup_is_opt_in_by_station_whitelist(tmp_path):
    """F1：缓存不能默认开——命中时没有内存对象表，下游会拿到空对象。

    白名单为空时连键都不查；列入白名单后仍要重新验 payload 指纹。
    """
    from v5.contracts.digest import compute_digest

    fx = build_facilities(scope="ticket", ticket_id="T1", master_seed=1,
                          frozen_at=FROZEN, root=tmp_path)
    digest = compute_digest({"x": 1})
    payload_file = tmp_path / "payload.json"
    payload_file.write_text('{"x":1}', encoding="utf-8")
    fx.cache.put(digest, "payload.json", digest)

    off = Executor(fx, writer=StoreContext.from_root(tmp_path).for_ticket("T"))
    assert off.cache_stations == frozenset()
    assert off._cache_get(digest, "Boundary") is None

    on = Executor(fx, writer=StoreContext.from_root(tmp_path).for_ticket("T"),
                  cache_stations=("Boundary",))
    assert on._cache_get(digest, "Boundary") == {"x": 1}
    assert on._cache_get(digest, "Panel") is None  # 白名单外的工位不查

    payload_file.write_text('{"x":2}', encoding="utf-8")
    assert on._cache_get(digest, "Boundary") is None  # 内容变了即未命中




def test_cache_hit_is_marked_as_cache_origin_in_trace():
    """s0/02 §7：命中必须由执行器把 trace.origin 标记为 `cache`，
    不能让「复用产物」看起来像刚刚算过。"""
    from v5.contracts.digest import compute_digest

    fx = _fx()
    ex = Executor(fx)
    ex._cache_hit_last = True
    rec = ex._trace(1, BY_NAME["Boundary"], "sha256:" + "a" * 64, None,
                    "done", "run", inputs=[])
    assert rec.origin == "cache"
    ex._cache_hit_last = False
    assert ex._trace(2, BY_NAME["Panel"], "sha256:" + "a" * 64, None,
                     "done", "run", inputs=[]).origin == "run"

