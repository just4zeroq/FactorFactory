"""CLI 公共：信封构造 + 参数转换（09 §2.4）。

信封字段固定 {ok, command, run_id, data, warnings, exit_code, errors?}。
`ok` 与 `exit_code` 恒一致；错误对象不带 exit_code，进程码取第一条 error 推导。
日志失败等非结论性失败只进 warnings，不改变退出码。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from contracts.errors import exit_code_for


def envelope_ok(command: str, run_id: str | None, data: dict[str, Any] | None,
                warnings: list[Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True, "command": command, "run_id": run_id,
        "data": data, "warnings": warnings or [], "exit_code": 0,
    }


def envelope_error(command: str, run_id: str | None,
                   errors: list[dict[str, Any]]) -> dict[str, Any]:
    code = errors[0]["code"]
    return {
        "ok": False, "command": command, "run_id": run_id,
        "data": None, "warnings": [], "exit_code": int(exit_code_for(code)),
        "errors": errors,
    }


def load_plan(args: Any) -> dict[str, Any]:
    from runtime.charter import load_plan as _load
    return _load(args.plan)


def load_json_dict(path: Any, *, code: str, label: str) -> dict[str, Any] | None:
    """读一个「顶层必须是对象」的 JSON 文件。

    不存在 → None（由调用方决定是「缺供给」还是「缺登记」）；损坏 → FlowError，
    并**用调用方给的码**：同一份坏文件在编译路径是 E1004（Charter 输入），
    在供给路径是 E4004（供给事实）。让 JSONDecodeError 冒到 main 兜底会变成
    E3004/退出码 1，把「用户可修」误报成「代码写错了」（S2-M5 review P1-3）。
    """
    import json

    from contracts.errors import FlowError

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlowError(
            code, f"{label} 无法解析: {exc}", path=str(path),
            hint="修好该文件后重试；不得按空表继续",
        ) from exc
    if not isinstance(data, dict):
        raise FlowError(code, f"{label} 顶层必须是对象", path=str(path))
    return data


def resolve_snapshot_for_plan(
    plan: Mapping[str, Any], data_root: Any, *,
    auto_create: bool = False, optional: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """解析计划声明的快照，并校验当前供给与快照一致。

    返回 `(snapshot_id, manifest)`。未声明时取最新 active 快照；若库里没有
    快照且 `auto_create=True`，先从当前供给建一份（兼容存量计划与迁移期）。
    校验失败抛 E4002/E2005：数据变了就必须让 run 停下，不能悄悄用新数据。

    `optional=True` 供只读命令（如 explain）使用：库里一份快照都没有且计划
    也没声明时退回 legacy 视图，而不是让只读命令凭空造快照或拒绝解释。有
    快照存在时仍然按清单解析，保证 explain 与 run 的指纹逐位一致。
    """
    from contracts.digest import blob_digest
    from contracts.errors import E2005, FlowError
    from services.snapshot_service import SnapshotService

    supply = plan.get("supply") or {}
    ref = supply.get("snapshot") if isinstance(supply, Mapping) else None
    with SnapshotService(Path(data_root)) as svc:
        if ref is None or str(ref).strip().lower() in {"", "latest"}:
            if not svc.list(status="active", limit=1):
                if auto_create:
                    datasets = [str(x) for x in (supply.get("datasets") or [])]
                    svc.create(
                        datasets=datasets,
                        exchange=str(supply.get("exchange") or ""),
                        market=str(supply.get("market") or "futures"),
                        note="auto-created for legacy plan",
                    )
                elif optional:
                    return None, None
        snapshot_id = svc.resolve(ref)
        # verify 而不是 manifest：校验交给快照服务，避免两处各写一套 digest 比较。
        manifest = svc.verify(snapshot_id)
        return snapshot_id, manifest


def load_snapshot_for_ticket(
    ctx: Mapping[str, Any], data_root: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    """从冻结上下文取 run 当时绑定的快照并核对字节。

    返回 `(None, None)` 表示 legacy ticket（本机制之前生成的 run），调用方
    退回原来的「扫描当前目录」行为，不假装它也绑定了快照。归档快照允许读——
    旧 run 的复盘不能因为快照被归档就失去可复现性。
    """
    from services.snapshot_service import SnapshotService

    snapshot_id = ctx.get("resolved_snapshot_id")
    if not snapshot_id:
        return None, None
    with SnapshotService(Path(data_root)) as svc:
        manifest = svc.verify(str(snapshot_id))
    return str(snapshot_id), manifest


def _snapshot_stems(
    manifest: Mapping[str, Any] | None, *, dataset: str,
    exchange: str, market: str, timeframe: str,
) -> tuple[str, ...] | None:
    """把清单条目翻译成 `store.read_dataset` 认得的文件主干。

    清单决定**可见集合**：补采产生的新文件不在旧清单里，因此对旧 run 不可见。
    这里从 `relpath` 的文件名反推主干，与 `store.dataset_file` 的命名规则互逆；
    真实符号仍由 `read_dataset` 从文件内容读取，不用文件名当事实。
    `None` 表示没有快照（legacy），退回按目录扫描。
    """
    from contracts.errors import E2005, E4002, FlowError
    from upstream import store

    if manifest is None:
        return None
    suffix = f"-{timeframe}.parquet" if dataset == store.BAR_DATASET else ".parquet"
    stems: list[str] = []
    for entry in manifest.get("entries") or []:
        if (entry.get("dataset"), entry.get("exchange"), entry.get("market")) != (
                dataset, exchange, market):
            continue
        if dataset == store.BAR_DATASET and entry.get("timeframe") != timeframe:
            continue
        name = Path(str(entry["relpath"])).name
        if not name.endswith(suffix):
            raise FlowError(
                E2005, f"快照清单的文件名不符合口径: {entry['relpath']}",
                path="supply.snapshot",
            )
        stems.append(name[: -len(suffix)])
    if not stems:
        raise FlowError(
            E4002, f"快照 {manifest.get('snapshot_id', '?')} 没有 "
                   f"{dataset} 在 {exchange}/{market} 的数据文件",
            path="supply.snapshot", hint="该 run 的计划声明了快照未覆盖的数据集",
        )
    return tuple(dict.fromkeys(stems))


def build_supply_context(
    plan: dict[str, Any],
    symbol_sets: dict[str, Any] | None,
    data_root: Any,
    manifest: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """按路径读文件、现算供给身份（19-supply.md §2.4）。

    返回 `(pins, symbol_sets, supply_meta, datasets)`：

    - `pins`：`数据集名 → Pin.to_dict()`，供编译器进 `blueprint_digest`；
    - `supply_meta`：门禁 B 核对 frequency/exchange/market 用的最小元信息；
    - `datasets`：内存里的 `store.Dataset`，供 live 直接转 `SupplyBatch`。

    `manifest` 非空时，可见文件集合被严格限制在清单内——补采新文件不会悄悄
    进入旧 run。辅助数据集仍按清单全量读（交易所本来就不给所有标的历史）；
    面板标的集是清单的子集筛选，不是扩大。字节一致性由调用方在
    `resolve_snapshot_for_plan` / `load_snapshot_for_ticket` 里先验过。

    这里**没有别名表**：数据在哪由约定路径决定，身份由内容决定——这正是
    「存储极简 + 身份现算」的全部含义。
    """
    from contracts.errors import E1004, E4002, FlowError
    from facilities.clock import Clock
    from runtime.gate_b import resolve_symbols
    from upstream import store

    supply = plan.get("supply") or {}
    datasets = [str(x) for x in (supply.get("datasets") or [])]
    if not datasets:
        raise FlowError(E1004, "计划 supply.datasets 为空", path="supply.datasets")
    exchange = str(supply.get("exchange") or "").strip()
    if not exchange:
        raise FlowError(E1004, "计划未声明 supply.exchange", path="supply.exchange")
    market = str(supply.get("market") or "futures")
    frequency = str(supply.get("frequency") or "1d")
    symbols = resolve_symbols(plan, symbol_sets)
    now = Clock().wall()

    pins: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    loaded: dict[str, Any] = {}
    # 面板基座要覆盖研究时间轴**加**认证窗口：mine 里挖，oos 里认证，两者都在
    # 面板上。只看整体 coverage（min/max）不够——晚上市的标的会在前半段留 NaN，
    # 一路传到 allocate 的 price 才炸，报出来的是「价格必须有限正数」，
    # 与真实原因（某只标的 2023-03 才上市）相差十万八千里。
    window = _panel_window(plan)
    for dataset in datasets:
        # 市场 × 数据集相容在这里再查一次：schema 已经拦了计划层，但
        # `build_supply_context` 是程序化入口，不该依赖「调用方一定跑过 schema」。
        store.require_market_dataset(market, dataset)
        visible = _snapshot_stems(
            manifest, dataset=dataset, exchange=exchange,
            market=market, timeframe=frequency,
        )
        if dataset == store.BAR_DATASET:
            # 面板基座严格要求计划标的集；辅助数据集缺标的属常态（交易所不给历史）。
            planned = tuple(store.file_stem(s) for s in symbols) if symbols else None
            if visible is None:
                wanted = planned
            elif planned is None:
                wanted = visible
            else:
                allowed = set(visible)
                absent = [s for s, stem in zip(symbols, planned) if stem not in allowed]
                if absent:
                    raise FlowError(
                        E4002,
                        f"快照 {manifest.get('snapshot_id', '?')} 未包含计划标的: {absent[:5]}",
                        path="supply.symbols",
                        hint="该 run 声明的标的超出快照可见范围；换快照或改标的集重跑",
                    )
                wanted = planned
        else:
            wanted = visible
        item = store.read_dataset(
            data_root, dataset, exchange, market,
            symbols=wanted,
            timeframe=frequency if dataset == store.BAR_DATASET else None,
            now=now,
        )
        if dataset == store.BAR_DATASET and window is not None:
            item.check_window(*window)
        pins[dataset] = item.pin().to_dict()
        meta[dataset] = item.meta()
        loaded[dataset] = item
    return pins, symbol_sets, meta, loaded


def _panel_window(plan: dict[str, Any]):
    """面板必须覆盖的区间：`mine` 起点 → `oos` 终点（缺 oos 则用 mine 终点）。

    解析不出来就返回 None，让 schema 层的 `E1008` 去报——这里不重复校验，
    也不猜一个区间出来。
    """
    from runtime.gate_b import _parse_day

    axis = (plan.get("boundary") or {}).get("time_axis") or {}
    mine = str(axis.get("mine") or "").split("..")
    oos = str(axis.get("oos") or "").split("..")
    start = _parse_day(mine[0]) if mine and mine[0] else None
    end_text = (oos[1] if len(oos) > 1 and oos[1] else None) or (
        mine[1] if len(mine) > 1 and mine[1] else None
    )
    end = _parse_day(end_text)
    if start is None or end is None:
        return None
    return start, end


def parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    """`--set key=value` → dict。格式错即 E1004（P2：不猜、不忽略）。"""
    from contracts.errors import E1004, FlowError

    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise FlowError(E1004, f"--set 格式错误: {pair}", hint="用 key=value")
        key, value = pair.split("=", 1)
        if not key:
            raise FlowError(E1004, f"--set 缺键名: {pair}", hint="用 key=value")
        out[key] = coerce(value)
    return out


def new_ticket_id(plan_digest: str) -> str:
    """按 ``<UTC 紧凑时间>-<plan_digest 前 8 位>`` 生成合法 ticket_id。

    时间取设施时钟（CLI 是组合根，可用 Clock()），不用 datetime.now。
    """
    from facilities import Clock

    stamp = Clock().wall().strftime("%Y%m%dT%H%M%SZ")
    short = plan_digest.split(":", 1)[-1][:8]
    return f"{stamp}-{short}"


def coerce(value: str) -> Any:
    """--set 的值转换：数字/布尔/JSON，否则字符串。"""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
