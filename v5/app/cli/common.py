"""CLI 公共：信封构造 + 参数转换（09 §2.4）。

信封字段固定 {ok, command, run_id, data, warnings, exit_code, errors?}。
`ok` 与 `exit_code` 恒一致；错误对象不带 exit_code，进程码取第一条 error 推导。
日志失败等非结论性失败只进 warnings，不改变退出码。
"""

from __future__ import annotations

import json
from typing import Any

from v5.contracts.errors import exit_code_for


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
    from v5.runtime.charter import load_plan as _load
    return _load(args.plan)


def load_json_dict(path: Any, *, code: str, label: str) -> dict[str, Any] | None:
    """读一个「顶层必须是对象」的 JSON 文件。

    不存在 → None（由调用方决定是「缺供给」还是「缺登记」）；损坏 → FlowError，
    并**用调用方给的码**：同一份坏文件在编译路径是 E1004（Charter 输入），
    在供给路径是 E4004（供给事实）。让 JSONDecodeError 冒到 main 兜底会变成
    E3004/退出码 1，把「用户可修」误报成「代码写错了」（S2-M5 review P1-3）。
    """
    import json

    from v5.contracts.errors import FlowError

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


def build_supply_context(
    plan: dict[str, Any],
    symbol_sets: dict[str, Any] | None,
    data_root: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """按路径读文件、现算供给身份（19-supply.md §2.4）。

    返回 `(pins, symbol_sets, supply_meta, datasets)`：

    - `pins`：`数据集名 → Pin.to_dict()`，供编译器进 `blueprint_digest`；
    - `supply_meta`：门禁 B 核对 frequency/exchange/market 用的最小元信息；
    - `datasets`：内存里的 `store.Dataset`，供 live 直接转 `SupplyBatch`。

    这里**没有别名表**：数据在哪由约定路径决定，身份由内容决定——这正是
    「存储极简 + 身份现算」的全部含义。
    """
    from v5.contracts.errors import E1004, FlowError
    from v5.facilities.clock import Clock
    from v5.runtime.gate_b import resolve_symbols
    from v5.upstream import store

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
        # 只有面板基座严格要求计划标的集；辅助数据集缺标的属常态（交易所不给历史）。
        wanted = symbols if dataset == store.BAR_DATASET else None
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
    from v5.runtime.gate_b import _parse_day

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
    from v5.contracts.errors import E1004, FlowError

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
    from v5.facilities import Clock

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
