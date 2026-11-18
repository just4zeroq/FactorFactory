"""S0 契约层测试：`s0/01-data-contracts.md` §6 的 D1-D12。

D13（`target.params` 写 `horizon` → `E1020`）与 D14（`composes` 深度 3 →
`E2008`）需要编译器与编排层，属 S1，此处不覆盖。

这些测试的第三类（"不该变的没变"）比前两类更重要：只测"变了会变"，不测
"不该变的没变"，就会把时间戳或路径悄悄钉进口径锚点里，而所有测试仍然全绿。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from v5.contracts import (
    ALL_ERROR_CODES,
    Blueprint,
    Digest,
    Error,
    ErrorLayer,
    Evidence,
    ExitCode,
    FlowError,
    Parcel,
    Pin,
    Ref,
    Ticket,
    blueprint_digest,
    call_digest,
    canonical_text,
    compute_digest,
    domain_digest,
    environment_digest,
    exit_code_for,
    layer_for,
    leaf_paths,
    parcel_digest,
    plan_digest,
    policy_digest,
    runtime_facts_digest,
    supply_digest,
)

UTC = timezone.utc


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[arg-type]


def dg(seed: str) -> Digest:
    return Digest.parse(compute_digest({"seed": seed}))


def make_pin(
    version: str = "0123456789abcdef",
    *,
    acquired_at: datetime | None = None,
    end_closed: bool = True,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Pin:
    return Pin(
        dataset="ohlcv",
        version=version,
        checksum=dg(f"checksum:{version}"),
        coverage={"start": start or dt(2024, 1, 1), "end": end or dt(2024, 6, 1)},
        symbols_digest=dg(f"symbols:{version}"),
        end_closed=end_closed,
        schema_version="1",
        acquired_at=acquired_at or dt(2026, 11, 16),
        gate_a={"passed": True, "reason_code": None},
    )


def make_ref(seed: str = "panel") -> Ref:
    return Ref(
        kind="parcel",
        id=seed,
        digest=dg(seed),
        relpath=f"parcels/{seed}/{dg(seed).hex[:16]}",
        schema_version="1",
    )


def make_call_kwargs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "station": "Evidence",
        "station_revision": "1",
        "impl": "gate/permutation",
        "impl_version": "v1",
        "phase": "none",
        "params_digest": compute_digest({"params": 1}),
        "input_digests": [compute_digest({"in": 1})],
        "policy": compute_digest({"policy": 1}),
        "runtime_facts": compute_digest({"facts": 1}),
        "composed_digests": [],
        "seed": 7,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# D1 / D5 —— 规范化
# --------------------------------------------------------------------------


def test_key_order_does_not_change_digest() -> None:
    a = compute_digest({"z": 1, "a": 2, "m": {"y": 1, "b": 2}})
    b = compute_digest({"m": {"b": 2, "y": 1}, "a": 2, "z": 1})
    assert a == b


def test_integer_and_float_forms_are_one_digest() -> None:
    assert compute_digest({"n": 1}) == compute_digest({"n": 1.0})
    assert compute_digest({"n": -0.0}) == compute_digest({"n": 0})
    assert compute_digest({"n": 2.5}) != compute_digest({"n": 2})


def test_timezone_offset_normalised_to_utc() -> None:
    utc = datetime(2026, 11, 16, 5, 0, 0, tzinfo=UTC)
    shifted = utc.astimezone(timezone(timedelta(hours=8)))
    assert compute_digest({"t": utc}) == compute_digest({"t": shifted})


def test_nan_and_infinity_rejected() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(FlowError) as exc:
            compute_digest({"x": bad})
        assert exc.value.code == "E2002"
        assert exc.value.exit_code is ExitCode.CONTRACT


def test_naive_datetime_rejected() -> None:
    with pytest.raises(FlowError) as exc:
        compute_digest({"t": datetime(2026, 11, 16, 5, 0, 0)})
    assert exc.value.code == "E2002"
    assert "时区" in exc.value.error.message


def test_unicode_nfc_normalised() -> None:
    composed = "e\u0301"  # e + combining acute
    precomposed = "\u00e9"
    assert compute_digest({"s": composed}) == compute_digest({"s": precomposed})


def test_bytes_rejected_from_fingerprint() -> None:
    with pytest.raises(FlowError) as exc:
        compute_digest({"blob": b"\x00\x01"})
    assert exc.value.code == "E2002"


def test_oversized_inline_sequence_rejected() -> None:
    with pytest.raises(FlowError) as exc:
        compute_digest({"arr": [0.5] * 9000})
    assert exc.value.code == "E2002"
    assert "Ref" in (exc.value.error.hint or "")


def test_set_encoding_is_order_independent() -> None:
    assert canonical_text({"s": {"b", "a", "c"}}) == canonical_text({"s": {"c", "a", "b"}})


# --------------------------------------------------------------------------
# D3 / D11 / D12 —— call 与 parcel
# --------------------------------------------------------------------------


def test_changing_semantic_field_changes_call_and_parcel() -> None:
    base = make_call_kwargs()
    call_a = call_digest(**base)  # type: ignore[arg-type]
    call_b = call_digest(**{**base, "params_digest": compute_digest({"params": 2})})  # type: ignore[arg-type]
    assert call_a != call_b
    payload = compute_digest({"payload": 1})
    assert parcel_digest(call_a, payload) != parcel_digest(call_b, payload)


def test_seed_only_change_changes_call_digest() -> None:
    base = make_call_kwargs()
    a = call_digest(**{**base, "seed": 7})  # type: ignore[arg-type]
    b = call_digest(**{**base, "seed": 8})  # type: ignore[arg-type]
    assert a != b, "seed 改变结果却不改变参数；漏掉它会错误命中缓存"


def test_phase_change_changes_call_digest() -> None:
    base = make_call_kwargs()
    a = call_digest(**{**base, "phase": "fit"})  # type: ignore[arg-type]
    b = call_digest(**{**base, "phase": "generate"})  # type: ignore[arg-type]
    assert a != b, "fit 与 generate 是两条计算路径，进不了指纹就会互相命中"


def test_runtime_facts_change_call_but_not_policy() -> None:
    base = make_call_kwargs()
    a = call_digest(**{**base, "runtime_facts": runtime_facts_digest({"trials": 10})})  # type: ignore[arg-type]
    b = call_digest(**{**base, "runtime_facts": runtime_facts_digest({"trials": 20})})  # type: ignore[arg-type]
    assert a != b

    # 账本里的滚动数字本身不进口径锚点：policy_digest 的签名里没有计数入口。
    params = inspect.signature(policy_digest).parameters
    assert "trials" not in params
    assert "n_trials" not in params


# --------------------------------------------------------------------------
# D4 —— 数据版本只影响 blueprint，不影响 policy
# --------------------------------------------------------------------------


def test_supply_pins_change_blueprint_digest_only() -> None:
    kwargs = {
        "baseline_version": "v1",
        "values": {"target": {"horizon": 5}},
        "caliber": {"label": {"horizon": 5}},
        "domain": compute_digest({"domain": 1}),
    }
    bp_a = blueprint_digest(supply_pins=[make_pin("1111111111111111")], **kwargs)  # type: ignore[arg-type]
    bp_b = blueprint_digest(supply_pins=[make_pin("2222222222222222")], **kwargs)  # type: ignore[arg-type]
    assert bp_a != bp_b, "蓝图描述展开后的方案，事实版本变了就是另一个蓝图"

    # policy_digest 的签名里根本没有 supply_pins：数据刷新不新建试验族。
    assert "supply_pins" not in inspect.signature(policy_digest).parameters


def test_supply_digest_ignores_acquired_at() -> None:
    fresh = supply_digest([make_pin(acquired_at=dt(2026, 11, 16))])
    refetched = supply_digest([make_pin(acquired_at=dt(2026, 11, 20))])
    assert fresh == refetched, "重采逐字节相同的数据不该让全部下游产物失效"


def test_supply_digest_tracks_end_closed() -> None:
    closed = supply_digest([make_pin(end_closed=True)])
    open_end = supply_digest([make_pin(end_closed=False)])
    assert closed != open_end


def test_domain_digest_is_member_order_independent() -> None:
    members = [
        {"id": "BTC/USDT:USDT", "version": "v1", "available_from": dt(2021, 1, 1)},
        {"id": "ETH/USDT:USDT", "version": "v1", "available_from": dt(2021, 1, 1)},
    ]
    common = {"fit_window": [dt(2024, 1, 1), dt(2024, 6, 1)], "embargo": 5, "eval_window": [dt(2024, 6, 6), dt(2024, 9, 1)]}
    a = domain_digest(members=list(reversed(members)), **common)  # type: ignore[arg-type]
    b = domain_digest(members=members, **common)  # type: ignore[arg-type]
    assert a == b


def test_environment_digest_not_in_parcel() -> None:
    assert "environment" not in inspect.signature(parcel_digest).parameters
    assert "environment_digest" not in inspect.signature(parcel_digest).parameters
    # 但函数本身要能算，供单独记录与重放排查。
    assert environment_digest(
        version_subset={"python": "3.12.1"},
        clock_basis="utc",
        rng_impl="pcg64",
        resource_limits={"cpu": 8},
    ).startswith("sha256:")


# --------------------------------------------------------------------------
# D6 / D7 / D8 —— 结构校验
# --------------------------------------------------------------------------


def test_missing_required_field_is_e2003() -> None:
    with pytest.raises(FlowError) as exc:
        Ref.from_dict({"kind": "parcel", "digest": str(dg("x")), "relpath": "p/x", "schema_version": "1"})
    assert exc.value.code == "E2003"
    assert "id" in exc.value.error.context["missing"]

    with pytest.raises(FlowError) as exc2:
        Pin.from_dict({"dataset": "ohlcv"})
    assert exc2.value.code == "E2003"


def test_unknown_field_in_structure_is_e1004() -> None:
    with pytest.raises(FlowError) as exc:
        Ref.from_dict(
            {
                "kind": "parcel",
                "id": "panel",
                "digest": str(dg("x")),
                "relpath": "p/x",
                "schema_version": "1",
                "note": "extra",
            }
        )
    assert exc.value.code == "E1004"


def test_evidence_rejects_conclusion_field() -> None:
    with pytest.raises(FlowError) as exc:
        Evidence.from_dict(
            {
                "kind": "observation",
                "name": "rank_ic",
                "value": 0.03,
                "source": "evaluate",
                "method": "spearman@1",
                "pass": True,
            }
        )
    assert exc.value.code == "E2004"
    assert "pass" in str(exc.value.error.context)


def test_evidence_rejects_conclusion_in_name() -> None:
    with pytest.raises(FlowError) as exc:
        Evidence(kind="observation", name="gate_pass", value=1, unit=None, source="evaluate", method="m@1")
    assert exc.value.code == "E2004"


def test_evidence_allows_statistic_names() -> None:
    # rank_ic / raw_score 是观察量而不是结论，按子串拒绝会误伤真正的事实。
    for name in ("rank_ic", "raw_score", "turnover"):
        ev = Evidence(kind="observation", name=name, value=0.1, unit=None, source="evaluate", method="m@1")
        assert ev.name == name


def test_evidence_kind_enum() -> None:
    with pytest.raises(FlowError) as exc:
        Evidence(kind="verdict", name="x", value=1, unit=None, source="s", method="m")
    assert exc.value.code == "E1004"


def test_ticket_immutable_except_status_and_archived_at() -> None:
    ticket = Ticket(
        ticket_id="20261116T053000Z-1a2b3c4d",
        plan_digest=dg("plan"),
        blueprint_digest=dg("bp"),
        policy_digest=dg("policy"),
        caliber_digest=dg("caliber"),
        baseline_version="v1",
        family="fam_a",
        family_declared_at=dt(2026, 11, 1),
        master_seed=42,
        pins=[make_pin()],
        domain_digest=dg("domain"),
        status="pending",
        created_at=dt(2026, 11, 16),
        environment_ref=make_ref("env"),
    )
    ticket.status = "running"
    assert ticket.status == "running"

    with pytest.raises(FlowError) as exc:
        ticket.plan_digest = dg("other")
    assert exc.value.code == "E2005"

    with pytest.raises(FlowError) as exc2:
        ticket.pins = []
    assert exc2.value.code == "E2005"


def test_ticket_id_format_enforced() -> None:
    with pytest.raises(FlowError) as exc:
        Ticket(
            ticket_id="ticket-1",
            plan_digest=dg("plan"),
            blueprint_digest=dg("bp"),
            policy_digest=dg("policy"),
            caliber_digest=dg("caliber"),
            baseline_version="v1",
            family="f",
            family_declared_at=dt(2026, 11, 1),
            master_seed=1,
            pins=[],
            domain_digest=dg("domain"),
            status="pending",
            created_at=dt(2026, 11, 16),
            environment_ref=make_ref("env"),
        )
    assert exc.value.code == "E1004"


def test_parcel_skipped_requires_reason() -> None:
    kwargs = {
        "ticket_id": "20261116T053000Z-1a2b3c4d",
        "station": "Panel",
        "station_revision": "1",
        "impl": "panel/default",
        "impl_version": "v1",
        "call_digest": dg("call"),
        "payload_ref": make_ref("panel"),
        "payload_digest": dg("payload"),
        "digest": dg("parcel"),
        "lineage": [],
        "evidence": [],
        "origin": "run",
        "started_at": dt(2026, 11, 16),
        "seconds": 0.5,
        "log_ref": make_ref("log"),
        "skip_reason": None,
        "error": None,
    }
    with pytest.raises(FlowError) as exc:
        Parcel(**kwargs, status="skipped")  # type: ignore[arg-type]
    assert exc.value.code == "E2003"
    with pytest.raises(FlowError) as exc2:
        Parcel(**kwargs, status="failed")  # type: ignore[arg-type]
    assert exc2.value.code == "E2003"


# --------------------------------------------------------------------------
# Blueprint origins 覆盖
# --------------------------------------------------------------------------


def test_leaf_paths_treats_empty_mapping_as_leaf() -> None:
    tree = {"a": {"b": 1, "c": {}}, "d": 2}
    assert leaf_paths(tree) == {"a.b", "a.c", "d"}


def test_blueprint_requires_full_origin_coverage() -> None:
    with pytest.raises(FlowError) as exc:
        Blueprint(
            values={"target": {"horizon": 5}, "search": {"pop": 100}},
            origins={"target.horizon": "plan"},
            resolved={},
            caliber={},
            supply_pins=[make_pin()],
            domain={},
            family_declaration={"family": "f", "declared_at": dt(2026, 11, 1)},
            station_order=[("Panel", "1")],
            subcall_graph={},
            digests={
                "plan": dg("p"),
                "blueprint": dg("b"),
                "policy": dg("pol"),
                "domain": dg("d"),
                "supply": dg("s"),
                "caliber": dg("c"),
            },
            check_results=[],
            compiled_at=dt(2026, 11, 16),
        )
    assert exc.value.code == "E1007"
    assert "search.pop" in exc.value.error.context["missing"]


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["/abs/path", "C:/data/facts", "../escape", "a/../../b"],
)
def test_absolute_and_dotdot_paths_rejected(bad: str) -> None:
    with pytest.raises(FlowError) as exc:
        Ref(kind="parcel", id="x", digest=dg("x"), relpath=bad, schema_version="1")
    assert exc.value.code == "E2002"


def test_backslash_normalised_not_rejected() -> None:
    ref = Ref(kind="parcel", id="x", digest=dg("x"), relpath="parcels\\panel\\abc", schema_version="1")
    assert ref.relpath == "parcels/panel/abc"
    assert compute_digest({"p": ref.relpath}) == compute_digest({"p": "parcels/panel/abc"})


# --------------------------------------------------------------------------
# D10 —— 错误码全表
# --------------------------------------------------------------------------


def test_every_error_code_has_layer_and_exit() -> None:
    assert len(ALL_ERROR_CODES) == 49
    expected_layer = {"E1": ErrorLayer.USAGE, "E2": ErrorLayer.CONTRACT, "E3": ErrorLayer.EXECUTION, "E4": ErrorLayer.SUPPLY, "E5": ErrorLayer.STORAGE}
    expected_exit = {
        ErrorLayer.USAGE: ExitCode.USAGE,
        ErrorLayer.CONTRACT: ExitCode.CONTRACT,
        ErrorLayer.EXECUTION: ExitCode.EXECUTION,
        ErrorLayer.SUPPLY: ExitCode.SUPPLY,
        ErrorLayer.STORAGE: ExitCode.EXECUTION,
    }
    for code in ALL_ERROR_CODES:
        layer = layer_for(code)
        assert layer is expected_layer[code[:2]], code
        assert exit_code_for(code) is expected_exit[layer], code


def test_unknown_error_code_rejected() -> None:
    with pytest.raises(ValueError):
        layer_for("E1023")
    with pytest.raises(ValueError):
        Error(code="X0001", message="x")


def test_error_serialisation_omits_absent_optionals() -> None:
    payload = Error(code="E4002", message="覆盖区间不含本次时间轴").to_dict()
    assert payload["layer"] == "supply"
    assert "path" not in payload
    assert "hint" not in payload
    assert "context" not in payload


def test_flow_error_carries_exit_code() -> None:
    err = FlowError("E3001", "上游产物缺失", hint="先跑 Panel")
    assert err.code == "E3001"
    assert err.exit_code is ExitCode.EXECUTION
    assert "上游产物缺失" in str(err)


# --------------------------------------------------------------------------
# 确定性（十次一致）
# --------------------------------------------------------------------------


def test_repeated_digest_is_bit_identical() -> None:
    value = {"b": [1, 2.5, {"c": None}], "a": "x", "t": dt(2026, 11, 16)}
    digests = {compute_digest(value) for _ in range(10)}
    assert len(digests) == 1


def test_plan_and_caliber_are_domain_separated() -> None:
    # 同一个 dict 作为 plan 与作为 caliber 不应撞指纹（kind 前缀保证）。
    raw = {"horizon": 5}
    assert plan_digest(raw) != compute_digest({"kind": "caliber", "raw": raw})
