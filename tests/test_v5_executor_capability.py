"""S3-M6：executor 真实接入 capability 的验收。

覆盖：
- CapabilityBridge 调真实能力（boundary→align→label→search→evaluate→observe→verdict 六工位链）
- 落盘视图剥离对象（opaque/array 不进 payload，json.dumps/compute_digest 不炸）
- 内存对象表在工位间传递（panel/scope/label/exposures）
- 八工位全部真实能力串通（含 S7 的 Certify）；失败路径仍写 failed trace
- executor 默认 dummy 路径不受影响（S0/S1/S2 兼容）
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from capability.stream import FieldStream, Pin
from contracts.digest import compute_digest
from contracts.errors import E3004, FlowError
from facilities import build_facilities
from orchestration import Executor
from orchestration.capability_bridge import CapabilityBridge

DAY = 86_400_000_000_000
START = int(np.datetime64("2024-01-01T00:00:00", "ns").astype("int64"))


def _stream(t_len: int = 90, n: int = 8, seed: int = 1) -> FieldStream:
    times = START + np.arange(t_len, dtype="int64") * DAY
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (t_len, n)), axis=0))
    volume = np.tile(np.linspace(1e3, 1e4, n), (t_len, 1))
    return FieldStream(
        times=times,
        symbols=tuple(f"S{i}" for i in range(n)),
        fields={"close": close, "volume": volume,
                "open": close, "high": close * 1.01, "low": close * 0.99},
        manifest={k: {"dataset": "ohlcv", "version": "v1", "unit": "q",
                      "available_lag": 0}
                  for k in ("close", "volume", "open", "high", "low")},
        end_closed=True,
        symbol_classes={},
    )


def _pin(stream: FieldStream) -> Pin:
    return Pin(
        dataset="ohlcv", version="v1", checksum=compute_digest({"v": "v1"}),
        coverage=(int(stream.times[0]), int(stream.times[-1]) + DAY),
        symbols_digest="s0", end_closed=True,
    )


def _supply_fn(stream, pin):
    def fn(station, params):
        if station == "Boundary":
            return {"pin": pin, "field_stream": stream}
        if station == "Panel":
            return {"field_stream": stream}
        if station == "Search":
            return {"market_view": {"available_from": "2024-03-30"}}
        return {}
    return fn


def _params():
    return {
        "Boundary": {"k": 5, "tier_window": 1, "min_coverage": 0.5,
                     "time_axis": {"mine": "2024-01-05..2024-03-01",
                                   "oos": "2024-03-02..2024-03-30",
                                   "holdout": "reserved"},
                     "mask_policy": "pit_dynamic"},
        "Panel": {"drop_unclosed": True, "causal_probe": "none", "quality_gates": {}},
        "Target": {"horizon": 3, "rebalance": 3, "entry": "next_close",
                   "transform": "none", "neutralise": []},
        "Search": {"max_candidates": 4, "population": 5, "generations": 1,
                   "max_depth": 3},
        "Evidence": {"prefilter": {"min_coverage": 0.5},
                     "null_model": "within_bar_cross_asset",
                     "metrics": ["ic"],
                     # 本文件验证编排接线：关掉成本归因，避免「真实成本把随机
                     # 候选全部否掉 → Portfolio 无成员 E3003」把接线验证变成
                     # 统计抽签。S4 成本链本身由 test_evidence_cost_chain_*
                     # 单独覆盖。
                     "cost_attribution": False,
                     "n_permutations": 3},
        # 本测试只验证编排接线，不追求统计显著性；让随机候选至少有一个 watch，
        # 才能把 Portfolio 的 S4 尾链真正跑到 Certify 的 fail-closed。
        "Verdict": {"dsr_min": 0.0, "fdr_max": 1.0, "cost_adj_min": 0.0,
                    "family": "f1"},
        "Portfolio": {
            "domain": "cs_test",
            "orthogonalise": "none",
            "corr_max": 1.0,
            "window": {"fit": "2024-01-10..2024-02-20", "embargo": 1,
                       "eval": "2024-02-25..2024-03-01"},
        },
        # 认证段严格晚于组合 eval 末端（02-25..03-01），与 boundary 的 oos 一致。
        "Certify": {"folds": 2, "embargo": 1,
                    "oos_window": {"start": "2024-03-02T00:00:00Z",
                                   "end": "2024-03-30T00:00:00Z"}},
    }


def _fx():
    return build_facilities(scope="ticket", ticket_id="T", master_seed=1)


def _run_six(**overrides):
    """八工位全链（Boundary→…→Certify）真实能力配置。"""
    stream = _stream()
    pin = _pin(stream)
    kwargs = dict(
        use_capabilities=True, supply_fn=_supply_fn(stream, pin),
        station_params=_params(),
        station_impls={"Portfolio": "equal_weight"},
        policy_view={
            "execution": {}, "label": {},
            "allocate": {"quantiles": 3, "long_short": True, "weighting": "equal",
                         "max_weight": 0.2, "net_exposure": 0.0,
                         "turnover_max": 0.35, "min_members": 2},
            "net": {"cost_tiers": [10, 20], "slippage": "none",
                    "fee_model": "taker", "max_leverage": 1.0,
                    "initial_margin": 0.1, "maintenance_margin": 0.005,
                    "liquidation_buffer": 0.01},
        },
        runtime_facts={"n_trials": 4},
    )
    kwargs.update(overrides)
    return Executor(_fx(), **kwargs)


# ---------------------------------------------------------------- bridge 直接测
def test_bridge_boundary_splits_disk_and_memory():
    """boundary 返回对象/数组走内存通道；无 store 时落盘视图剔除这些字段。"""
    stream, pin = _stream(), None
    pin = _pin(stream)
    bridge = CapabilityBridge(supply_fn=_supply_fn(stream, pin))
    result = bridge.invoke(
        station="Boundary", capability="boundary", impl="pit_dynamic_topk",
        impl_version="1", params=_params()["Boundary"],
        policy_view={"execution": {}}, runtime_facts={}, phase="none", seed=1,
        station_revision="r1", upstream={}, shared={},
    )
    assert "scope" in result.objects           # 对象通道
    assert "observable_mask" in result.objects  # 数组通道
    assert "scope" not in result.payload
    assert "observable_mask" not in result.payload
    json.dumps(result.payload)                  # 必须可序列化
    compute_digest(result.payload)              # 必须可算指纹
    assert result.payload["axis_resolved"]


def test_bridge_with_store_writes_data_refs(tmp_path):
    """有 ObjectStore 时，对象/数组落盘为 <field>_ref，parcel.json 可独立重建。"""
    from storage.layout import Layout
    from storage.objects import ObjectStore

    stream = _stream()
    pin = _pin(stream)
    store = ObjectStore(Layout(tmp_path))
    bridge = CapabilityBridge(supply_fn=_supply_fn(stream, pin), object_store=store)
    result = bridge.invoke(
        station="Boundary", capability="boundary", impl="pit_dynamic_topk",
        impl_version="1", params=_params()["Boundary"],
        policy_view={"execution": {}}, runtime_facts={}, phase="none", seed=1,
        station_revision="r1", upstream={}, shared={},
    )
    # 数据通道落盘为 *_ref（s0/05 §3.1），而不是被静默丢弃
    for field in ("observable_mask", "tradable_mask", "tiers", "reason_codes", "scope"):
        ref = result.payload.get(f"{field}_ref")
        assert isinstance(ref, str) and ref.startswith("sha256:"), field
    # ref 指向的对象真实存在
    from pathlib import Path
    digests = [v.split(":")[1] for k, v in result.payload.items() if k.endswith("_ref")]
    files = list((tmp_path / "facts" / "parcel_data").glob("*"))
    assert files and all(any(d in f.name for f in files) for d in digests)
    json.dumps(result.payload)
    compute_digest(result.payload)


def test_bridge_preserves_native_ref_identity(tmp_path):
    """复核 bug：能力原生 panel_ref/score_ref 不得被派生 <field>_ref 覆盖。

    覆盖会让同一对象有两个 digest，下游按落盘 ref 取回的内容与能力声明身份
    不符。规则：能力自己给了同名原生 ref 就保留，跳过派生。
    """
    from storage.layout import Layout
    from storage.objects import ObjectStore

    stream = _stream()
    pin = _pin(stream)
    store = ObjectStore(Layout(tmp_path))
    bridge = CapabilityBridge(supply_fn=_supply_fn(stream, pin), object_store=store)

    # 先跑 Boundary→Panel，检查 Panel 的原生 panel_ref 是否保留
    b = bridge.invoke(
        station="Boundary", capability="boundary", impl="pit_dynamic_topk",
        impl_version="1", params=_params()["Boundary"],
        policy_view={"execution": {}}, runtime_facts={}, phase="none", seed=1,
        station_revision="r1", upstream={}, shared={},
    )
    p = bridge.invoke(
        station="Panel", capability="align", impl="point_in_time_align",
        impl_version="1", params=_params()["Panel"],
        policy_view={"execution": {}}, runtime_facts={}, phase="none", seed=1,
        station_revision="r1", upstream={"Boundary": b.objects}, shared={},
    )
    # align 原生返回 panel_ref = panel.digest()；落盘必须原样保留，不被
    # object_store 对 panel 对象的写入覆盖（两者 digest 不同）。
    assert p.payload["panel_ref"] == p.objects["panel"].digest()


def test_bridge_resolves_impl_name_not_station_name():
    """单实现能力：工位语义名（boundary@1）不得原样传给 resolve。"""
    stream, pin = _stream(), None
    pin = _pin(stream)
    bridge = CapabilityBridge(supply_fn=_supply_fn(stream, pin))
    # 传工位名 "boundary" 作为 impl，bridge 应自动解析成注册名 pit_dynamic_topk
    result = bridge.invoke(
        station="Boundary", capability="boundary", impl="boundary", impl_version="1",
        params=_params()["Boundary"], policy_view={"execution": {}},
        runtime_facts={}, phase="none", seed=1, station_revision="r1",
        upstream={}, shared={},
    )
    assert result.objects.get("scope") is not None


def test_unimplemented_capability_is_rejected_before_input_parsing(monkeypatch):
    """未实现能力报 E3004，而不是「缺输入 E3003」。

    顺序很重要：先把「S4/S7 没做」说清楚，再谈数据准不准备。反过来会让
    「能力没实现」被读成「数据没准备好」，排障方向整体错位。
    """
    import orchestration.capability_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "installed_impls", lambda: {})
    bridge = CapabilityBridge()
    with pytest.raises(FlowError) as e:
        bridge.invoke(
            station="Certify", capability="certify", impl="walk_forward",
            impl_version="1", params={}, policy_view={}, runtime_facts={},
            phase="none", seed=1, station_revision="r1", upstream={}, shared={},
        )
    assert e.value.error.code == E3004


def test_certify_without_upstream_objects_fails_closed():
    """能力存在但上游对象缺失 → E3003，不返回空产物冒充成功。"""
    bridge = CapabilityBridge()
    with pytest.raises(FlowError) as e:
        bridge.invoke(
            station="Certify", capability="certify", impl="walk_forward",
            impl_version="1", params={}, policy_view={}, runtime_facts={},
            phase="none", seed=1, station_revision="r1", upstream={}, shared={},
        )
    assert e.value.error.code == "E3003"


# ---------------------------------------------------------------- executor 接入
def test_executor_capability_mode_runs_all_eight_stations_real():
    """executor 开启能力后，八个工位依次跑真实能力并全部成功。"""
    ex = _run_six()
    seen = []

    def spy(**kwargs):
        seen.append((kwargs["station"], kwargs["capability"]))
        bridge = CapabilityBridge(supply_fn=ex.bridge.supply_fn)
        return bridge.invoke(**kwargs)

    ex.capability_fn = spy
    result = ex.execute("T")
    stations = [s for s, _ in seen]
    assert stations == ["Boundary", "Panel", "Target", "Search",
                        "Evidence", "Verdict", "Portfolio", "Certify"]
    assert len(result.parcels) == 8
    assert result.parcels["Certify"].payload["evidence_grade"] == "out_of_sample"


def test_zero_promotion_is_skipped_not_failed():
    """零晋升是**结论**，不是故障（s0/05 §4.2 / AC52）。

    判据全部卡死时，Portfolio 必须记 `skipped(no_candidate_passed)`，Certify
    随 `upstream_skipped`，整链仍正常结束（state=skipped），绝不抛 E3003。
    第一次真实数据跑通时，这里抛的是 `combine 没有 pass/watch 且带暴露的候选`
    ——把结论当故障，正是 AC52 要挡的那类误判。
    """
    hard = dict(_params()["Verdict"])
    hard.update({"dsr_min": 1.0, "cost_adj_min": 1.0, "fdr_max": 1e-12})
    ex = _run_six()
    ex.station_params["Verdict"] = hard
    result = ex.execute("T")

    assert result.state == "skipped"
    assert result.skip_reason == "no_candidate_passed"
    assert result.skipped_at == "Portfolio"
    # 前六个工位有产物；Portfolio/Certify 不落 parcel（没有结论可落）
    assert set(result.parcels) == {"Boundary", "Panel", "Target", "Search",
                                   "Evidence", "Verdict"}
    statuses = {t["station"]: (t["status"], t.get("skip_reason"))
                for t in result.traces}
    assert statuses["Portfolio"] == ("skipped", "no_candidate_passed")
    assert statuses["Certify"] == ("skipped", "upstream_skipped")
    # 零晋升不得触发 Publish（没有可发布的结论）
    assert not [e for e in result.events if e["kind"] == "Publish"]


def test_s4_backtest_receives_the_panel_time_axis(monkeypatch):
    """S4 的 backtest 子调用必须带 Panel 的真实时间轴。

    成交时点与「每年多少根 bar」都由它决定。桥一旦不传，backtest 会静默退回
    日频假设，接 1h 供给时年报 Sharpe 就差 sqrt(24) 倍——那正是 V4 那类
    「能跑、数不对」的假成功。这里只钉搬运，不重复测 backtest 的数值。
    """
    import orchestration.capability_bridge as bridge_mod

    seen_times: list[np.ndarray] = []
    seen_out: list[dict] = []
    real = bridge_mod.capability_run

    def spy(call):
        result = real(call)
        if call.capability == "backtest":
            bound = {i.kind: i.data for i in call.inputs if i is not None}
            seen_times.append(bound["time"])
            seen_out.append(result)
        return result

    monkeypatch.setattr(bridge_mod, "capability_run", spy)
    ex = _run_six()
    ex.execute("T")

    assert seen_times, "Portfolio 的 S4 尾链没有真正调 backtest"
    for time in seen_times:
        assert np.asarray(time).dtype.kind in "iu", "时间轴必须是整数纳秒"
        assert np.all(np.diff(time) > 0), "时间轴必须严格递增"
        assert np.all(np.diff(time) == DAY), "夹具是日频供给，步长应恒为 1 天"
    for out in seen_out:
        # 关键：来源必须是真实时间轴，不是缺省假设。
        assert out["bars_per_year_source"] == "time_axis_median"
        assert out["bars_per_year"] == pytest.approx(365.0)
        assert out["annualisation_used"] == "calendar_365"


def test_executor_default_dummy_unchanged():
    """默认不开启能力时仍走 dummy，八工位全过（S0/S1/S2 兼容）。"""
    ex = Executor(_fx())
    result = ex.execute("T")
    assert len(result.parcels) == 8
    assert all(p.payload.get("kind") == "dummy" for p in result.parcels.values())


def test_failed_station_writes_failed_trace_without_half_parcel(tmp_path):
    """工位失败时写 status=failed trace 且不留下半成品 parcel。

    用「折数比 OOS 窗口还多」触发真实的 E3003：这不是未实现，而是参数与窗口
    不自洽——正是认证段最容易被「多切几折显得更稳」搞错的地方。
    """
    from runtime.store import StoreContext
    tid = "20261116T000000Z-abcdef12"
    root = tmp_path / "runs" / tid
    writer = StoreContext.from_root(tmp_path).for_ticket(tid)
    params = _params()
    params["Certify"] = {**params["Certify"], "folds": 99}
    ex = _run_six(writer=writer, station_params=params)
    with pytest.raises(FlowError) as e:
        ex.execute(tid)
    assert e.value.error.code == "E3003"
    lines = (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    assert last["station"] == "Certify"
    assert last["status"] == "failed"
    assert last["error_code"] == "E3003"
    parcels = sorted(p.parent.name for p in root.rglob("parcel.json"))
    assert len(parcels) == 7  # 前七工位各一份，Certify 无


def test_budget_event_uses_real_search_n_trials():
    """真实 search 产出的 n_trials 通道被 Budget 事件采用，不是写死 0。

    用 capability_fn 混合：前六工位走真实能力，后两工位（未实现）返回最小
    合法产物，让 execute 走完整流程并返回 events；然后断言 Budget 值 == 真实
    search 产物的 n_trials，而不是只看 payload 里有数字。
    """
    from orchestration.capability_bridge import BridgeResult

    ex = _run_six()
    real_bridge = CapabilityBridge(
        supply_fn=ex.bridge.supply_fn, object_store=ex.bridge.object_store,
    )
    real_search_n = {}

    def mixed(**kwargs):
        if kwargs["station"] in ("Portfolio", "Certify"):
            # 仅测试用：给这两个工位一个可序列化的最小产物，让八步走完。
            # Certify 必须带 out_of_sample 等级，否则 Publish 会按 AC27 拒绝。
            return BridgeResult(payload={"station": kwargs["station"], "test": True,
                                         "evidence_grade": "out_of_sample"},
                                objects={}, capability=kwargs["capability"])
        r = real_bridge.invoke(**kwargs)
        if kwargs["station"] == "Search":
            real_search_n["value"] = r.payload.get("n_trials")
        return r

    ex.capability_fn = mixed
    result = ex.execute("T")
    assert len(result.parcels) == 8
    budget = [e for e in result.events if e["kind"] == "Budget"]
    assert len(budget) == 1
    assert isinstance(real_search_n.get("value"), int) and real_search_n["value"] > 0
    # 关键断言：事件值必须来自真实 search 产物，不是写死 0
    assert budget[0]["value"] == real_search_n["value"]






def test_unobservable_candidates_are_named_not_silently_dropped():
    """求不出暴露的候选必须逐条进 `unobservable`，不能只体现为数量变少。

    V5 首跑实测 Search `count=65`、Evidence `per_candidate=57`，差额 8 个只
    活在注释里。数量差可以被发现，但「哪 8 个、因为什么」不能——那正是
    V4「假成功」的同款形状：流水线自己知道丢了东西，报告里看不出来。
    """
    from capability.panel import Panel
    from orchestration.capability_bridge import _digest_of

    times = START + np.arange(30, dtype="int64") * DAY
    close = 100.0 + np.arange(30, dtype="float64")[:, None] * np.ones((1, 3))
    panel = Panel(times=times, symbols=("A", "B", "C"),
                  fields={"close": close, "volume": np.ones((30, 3))})

    good = {"candidate_id": "sha256:" + "a" * 64,
            "payload": {"expression": {"field": "close"}}}
    bad = {"candidate_id": "sha256:" + "b" * 64,
           "payload": {"expression": {"op": "no_such_op",
                                      "args": [{"field": "close"}]}}}
    noexpr = {"candidate_id": "sha256:" + "c" * 64, "payload": {}}

    bridge = CapabilityBridge()
    exposures, unobservable = bridge._build_exposures(
        [good, bad, noexpr], {"Panel": {"panel": panel}}, {},
    )

    assert list(exposures) == [good["candidate_id"]]
    reasons = {row["candidate_id"]: row["reason"] for row in unobservable}
    assert set(reasons) == {bad["candidate_id"], noexpr["candidate_id"]}
    assert reasons[noexpr["candidate_id"]] == "missing_expression"
    # 求值失败的原因码必须是 FlowError 的错误码，不是裸异常类名
    assert reasons[bad["candidate_id"]].startswith("E")
    assert all(row["detail"] for row in unobservable)
    # 落盘视图里可 JSON 序列化 + 可算指纹
    json.dumps(unobservable)
    compute_digest({"unobservable": unobservable})
    assert _digest_of(panel).startswith("sha256:")


def test_search_payload_declares_the_unobservable_channel():
    """`unobservable` 是 Search 的声明输出：能力层默认空，编排层回填。

    声明在 `registry/params.json` 而不是靠「直接塞进 payload」——否则
    outputs 校验（`outputs_reject_extra_keys`）与 payload 内容会各说一套。
    """
    from capability.params import schema_for

    assert schema_for("search")["outputs"]["unobservable"] == "list"
