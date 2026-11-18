"""CLI `--live` 组装（S3-M6 收尾 / S4 前置）。

命令行默认走 dummy 桩（S0/S1/S2 验收不变）；显式 `--live` 时本模块承担组合根
的三件组装工作，把「已冻结的计划 + 已发布的供给事实」接到真实能力层：

1. **参数展开**（`expand_station_inputs`）：计划把工位配置写成嵌套的
   ``params`` / ``time_axis``，把经济口径写在 ``caliber``；而能力实现读的是
   **扁平 params + policy_view**。本模块做唯一一次展开：
   - 工位 ``params`` 子块摊平，``time_axis`` 提升为同级键；
   - ``caliber.label`` / ``caliber.execution`` 的口径镜像键填进 Target 的 params
     （`label._check_caliber` 会逐项比对，值必须同源）；
   - ``caliber`` 四子块整体作为 ``policy_view``（bridge 按各能力的 policy_reads
     裁剪，多带的子块由 runtime 的 E2001 挡）。
   schema 之外的字段一个都不造：plan 缺的必填项在这里给出**确定性默认值**
   （与 capability 注册的 enum 对齐），没有「运行期取不到就猜」的口子。

2. **供给注入**（`build_live_supply`）：从数据根读 parquet → 构造
   `SupplyBatch` → 调 `read` 能力得到 `FieldStream`。`supply_fn(station, params)`
   按工位返回外部事实 kind：Boundary 要 `pin` + `field_stream`，Panel 要
   `field_stream`，Search 要 `market_view`。能力层自己不读盘（AC31），打开哪个
   文件这件事只发生在这里。

3. **可复现 as-of**：`market_view.available_from` 取供给版本的覆盖末端（发布
   事实），不取墙钟——搜索候选的 available_from 因此可复现（AC3）。

本模块属于应用层组合根：允许读文件、允许调能力；不写账本、不产结论。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping

import numpy as np

from capability import Call, InputRef, run as capability_run
from capability.impls import install_default_impls
from capability.params import schema_for
from capability.stream import FieldStream, Pin as CapPin, SupplyBatch
from contracts.errors import E1004, E4002, E1008, E3003, E3004, FlowError
from contracts.types import Digest, Ref
from runtime.store import resolve_data_root
from upstream import store
from capability.params import implementation_defaults

# 工位配置在 plan 里的键 → 能力消费的扁平键。缺失的必填项用确定性默认补齐。
# 默认值与 registry/params.json 的 enum 对齐；注释标明来源，便于审计。
_BOUNDARY_DEFAULTS = {"mask_policy": "pit_dynamic"}       # pit 动态掩码（S1 默认）
_PANEL_DEFAULTS = {"causal_probe": "none", "quality_gates": {}}
_EVIDENCE_DEFAULTS = {
    "metrics": ["ic"], "cost_attribution": True, "n_permutations": 20,
}
_SEARCH_DEFAULTS = {"max_candidates": 40}

# 工位语义名 → 能力名（与 capability_bridge.STATION_CAPABILITY 同源；这里只用来
# 确认该工位确实有真实能力，避免把 dummy 桩的工位也算进 live 展开）。
_LIVE_STATIONS = ("boundary", "panel", "target", "search", "evidence", "verdict",
                  "portfolio", "certify")

_CAPABILITY_OF = {
    "boundary": "boundary", "panel": "align", "target": "label",
    "search": "search", "evidence": "observe", "verdict": "verdict",
    "portfolio": "combine", "certify": "certify",
}

# S7 组合域的三个合成器。这里是**展平期的白名单**（与实现注册表同源），
# 用来在展开阶段就挡住拼写错误；真正的实现名解析仍由 bridge 的 closed_multi
# 规则负责（E1002）。
_COMBINERS = frozenset({"equal_weight", "score_weighted", "fixed_ridge"})


def _flatten_block(plan: Mapping[str, Any], name: str,
                   defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把一个工位块摊平成能力 params：``params`` 子块 + ``time_axis`` 同级键。

    不保留 ``impl``（实现名走 executor.station_impls，不进 params）——把 impl 混进
    params 会让「换实现」同时改 params 指纹，而 call_digest 的 impl 通道已有它。
    """
    block = plan.get(name)
    if not isinstance(block, Mapping):
        raise FlowError(E1004, f"live 展开缺工位块 {name!r}", path=f"plan.{name}")
    out: dict[str, Any] = {}
    raw_params = block.get("params", {})
    if isinstance(raw_params, Mapping):
        out.update({k: v for k, v in raw_params.items()})
    if isinstance(block.get("time_axis"), Mapping):
        out["time_axis"] = dict(block["time_axis"])
    if defaults:
        for key, value in defaults.items():
            out.setdefault(key, value)
    return out


def _label_params(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Target 的扁平 params = caliber.label/execution 的镜像快照。

    ``neutralise_window`` 只在 policy_view.label 里，**不进 params**——label schema
    没有这个键，塞进去会被 E1003 挡在 runtime 第 2 步。
    """
    caliber = plan.get("caliber") or {}
    execution = caliber.get("execution") or {}
    label = caliber.get("label") or {}
    out: dict[str, Any] = {}
    # 只写入「caliber 里真有值」的镜像键。缺的键不写成 None——那会让
    # _assert_required 的「键在不在」检查通过，却把 E1008 推迟到 label.build
    # 里变成一句难查的 enum 报错。
    for key in ("entry", "rebalance"):
        if key in execution and execution[key] is not None:
            out[key] = execution[key]
    for key in ("horizon", "transform"):
        if key in label and label[key] is not None:
            out[key] = label[key]
    if "neutralise" in label and isinstance(label["neutralise"], list):
        out["neutralise"] = list(label["neutralise"])
    for key in ("overlap_policy", "min_width", "width_policy"):
        if key in label:
            out[key] = label[key]
    return out


def _search_params(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Search 的 params：全部算法透传，不再只取第一个。

    两种形态并存是刻意的：`algorithms` 列表是**全部**要跑的算法（search.build
    逐项执行并共用同一份 fitness 计数），扁平的 `algorithm` + 同级参数只服务
    「单算法老计划」与直接调用 `search.build` 的场景。

    旧实现 `algorithms[0]` 会把计划里第二个算法静默丢掉——计划写着
    `[gp_symbolic, random_expression]`，实际只跑 GP，而 GP 与随机基线的对照
    是引入多算法支持的唯一理由。静默丢弃让这个对照永远不成立。
    """
    block = plan.get("search") or {}
    algorithms = block.get("algorithms") or []
    if not algorithms or not isinstance(algorithms[0], Mapping):
        raise FlowError(E1004, "live 模式 search.algorithms 至少要有一个算法",
                        path="search.algorithms")
    runs: list[dict[str, Any]] = []
    for i, entry in enumerate(algorithms):
        if not isinstance(entry, Mapping):
            raise FlowError(E1004, f"search.algorithms[{i}] 必须是映射",
                            path=f"search.algorithms[{i}]")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise FlowError(E1004, f"search.algorithms[{i}] 缺 name",
                            path=f"search.algorithms[{i}].name")
        runs.append({"name": name, "params": dict(entry.get("params") or {})})

    first = runs[0]
    out: dict[str, Any] = {
        **_SEARCH_DEFAULTS, **first["params"],
        "algorithm": first["name"],
        # 全量算法列表：build 按它逐项执行，扁平键只作单算法兼容
        "algorithms": runs,
    }
    if isinstance(block.get("prior_pool"), list):
        out["prior_pool"] = [str(x) for x in block["prior_pool"]]
    if block.get("trial_family"):
        out["family"] = str(block["trial_family"])
    return out


def _portfolio_params(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Portfolio 的扁平 params：合成器走实现通道，正交/相关上限走参数通道。

    `combiner` 在 `run.py` 里被塞进 `station_impls["Portfolio"]`（closed_multi
    的名字空间），这里**不再重复**——同一件事写两处必然分叉。本函数只负责
    `combine` 的三个参数与三段窗口。

    `orthogonalise` / `corr_max` 是组合构造选择，不是经济口径，所以放在
    `portfolio` 块而不是 `caliber`：改它们只改方案，不改「怎么记账」。
    """
    block = plan.get("portfolio") or {}
    combiner = str(block.get("combiner") or "")
    if not combiner:
        raise FlowError(E1008, "live 模式 portfolio.combiner 缺失", path="portfolio.combiner")
    if combiner not in _COMBINERS:
        raise FlowError(
            E1004, f"未知组合器 {combiner!r}", path="portfolio.combiner",
            hint=f"可用: {sorted(_COMBINERS)}",
        )
    window = block.get("window")
    if not isinstance(window, Mapping):
        raise FlowError(E1008, "live 模式 portfolio.window 缺失", path="portfolio.window")
    orth = str(block.get("orthogonalise") or "none")
    if orth not in ("none", "symmetric"):
        raise FlowError(
            E1004, f"portfolio.orthogonalise 只允许 none/symmetric，得到 {orth!r}",
            path="portfolio.orthogonalise",
        )
    corr_raw = block.get("corr_max", 1.0)
    try:
        corr_max = float(corr_raw)
    except (TypeError, ValueError) as exc:
        raise FlowError(E1004, "portfolio.corr_max 必须是数",
                        path="portfolio.corr_max") from exc
    if not (0.0 < corr_max <= 1.0):
        raise FlowError(E1004, "portfolio.corr_max 必须在 (0,1]",
                        path="portfolio.corr_max")
    # domain 用计划名固化，使域身份随计划指纹变化；不是自由文本口径。
    return {
        "domain": str(plan.get("plan") or "portfolio"),
        "orthogonalise": orth,
        "corr_max": corr_max,
        "window": dict(window),
    }


def _interval(text: Any, *, path: str) -> tuple[str, str]:
    """计划里的区间字面量 `<YYYY-MM-DD>..<YYYY-MM-DD>` → 两个 UTC RFC3339 时刻。

    `certify.params.oos_window` 是 `map{start,end}`（时间戳类型，必须是显式 `Z`），
    而计划里写的是 `time_axis` 的区间字面量。两套写法之间只允许有**这一个**
    转换点：能力层只认时间戳，计划只认区间。
    """
    if not isinstance(text, str) or ".." not in text:
        raise FlowError(
            E1008, f"{path} 必须是 'YYYY-MM-DD..YYYY-MM-DD'，得到 {text!r}", path=path,
        )
    left, right = (part.strip() for part in text.split("..", 1))
    for part in (left, right):
        try:
            date.fromisoformat(part)
        except ValueError as exc:
            raise FlowError(E1008, f"{path} 日期不合法: {part!r}", path=path) from exc
    return f"{left}T00:00:00Z", f"{right}T00:00:00Z"


def _certify_params(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Certify 的扁平 params：折数/隔离来自 certify 块，OOS 窗口来自 time_axis。

    认证段的窗口**不是**认证工位自己配的：它必须与搜索区间互补（未参与搜索的
    那段才是结论，06 §8.1）。所以 `oos_window` 只从 `boundary.time_axis.oos` 取，
    certify 块里再写一份就会被忽略——两处真相必然分叉。
    """
    block = plan.get("certify")
    if not isinstance(block, Mapping):
        raise FlowError(
            E1008, "live 模式缺 certify 工位块；八个工位缺一个就不是完整挖掘流程",
            path="plan.certify",
        )
    raw = block.get("params")
    out: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
    axis = ((plan.get("boundary") or {}).get("time_axis") or {})
    oos = axis.get("oos")
    if not oos or str(oos) == "reserved":
        raise FlowError(
            E1008, "认证需要 boundary.time_axis.oos 给出未参与搜索的区间",
            path="boundary.time_axis.oos",
            hint="holdout 保留区与 OOS 不是同一段；没有 OOS 就无法产生可发布结论",
        )
    start, end = _interval(oos, path="boundary.time_axis.oos")
    out["oos_window"] = {"start": start, "end": end}
    return out


def expand_station_inputs(plan: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]],
                                                              dict[str, Any]]:
    """返回 ``(station_params, policy_view)``，供 Executor 构造调用单。

    键用 plan 的小写工位名（Executor 内部用 ``_canon_station`` 归一）。policy_view
    是 caliber 四子块的深拷贝，交给 bridge 按 policy_reads 裁剪。
    """
    station_params: dict[str, dict[str, Any]] = {
        "boundary": _flatten_block(plan, "boundary", _BOUNDARY_DEFAULTS),
        "panel": _flatten_block(plan, "panel", _PANEL_DEFAULTS),
        "target": _label_params(plan),
        "search": _search_params(plan),
        "evidence": _flatten_block(plan, "evidence", _EVIDENCE_DEFAULTS),
        "verdict": _flatten_block(plan, "verdict"),
        "portfolio": _portfolio_params(plan),
        "certify": _certify_params(plan),
    }
    # verdict 要 family 参数（schema 必填），取自 search.trial_family；plan 没写就
    # 用确定性默认，不为空串。必须在下面的必填校验之前补。
    verdict = station_params["verdict"]
    verdict.setdefault("family", str((plan.get("search") or {}).get("trial_family")
                                     or implementation_defaults("cli")["family"]))

    # 校验扁平后的 params 至少通过 schema 的形状检查：缺必填在这里就 fail-closed，
    # 不要等跑到第 5 工位才 E1008（错误离现场越远越难查）。
    for station in _LIVE_STATIONS:
        cap = _CAPABILITY_OF[station]
        _assert_required(cap, station_params[station])

    caliber = plan.get("caliber") or {}
    policy_view = {
        block: dict(caliber.get(block) or {})
        for block in ("execution", "label", "allocate", "net")
    }
    return station_params, policy_view


def _assert_required(capability: str, params: Mapping[str, Any]) -> None:
    """只检查「键在不在」，不做完整类型校验（那是 runtime 第 2 步的职责）。"""
    spec = schema_for(capability)["params"]
    for key, field in spec.items():
        if field.get("required") and key not in params:
            raise FlowError(E1008, f"live 展开后 {capability}.params 缺必填 {key!r}",
                            path=f"{capability}.params.{key}",
                            hint="在计划里补齐；默认值只覆盖能力注册的固定枚举")


def _as_of_iso(value: Any) -> str:
    """把契约 Pin.coverage.end（datetime）规范成 read.window 要的 UTC `Z` 文本。"""
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


# 要求「按事件计资金费」的费用模型。其它模型下不读 funding 数据集——
# 计划口径说了不计，供给链就不该多付这份对齐成本，也不该留一个
# 「数组在手、口径没开」的歧义状态。
_FUNDING_FEE_MODELS = frozenset({"taker_with_funding"})

# 事件数据集 → 面板字段映射（注册表 fields.json 的 datasets 声明同源）。
_EVENT_FIELD_BY_DATASET: dict[str, str] = {
    "funding": "funding_rate",
    "open_interest": "open_interest",
    "basis": "basis",
}


def _event_field_grid(
    event_stream: "FieldStream", bar_stream: "FieldStream", field_name: str,
) -> "tuple[np.ndarray, int]":
    """事件流字段 → 面板网格，**as-of 前向填充**（PIT：t 行只拿 ≤ t 的最后观测）。

    事件序列（结算/快照时点）与 bar 网格天然不同轴：用
    `searchsorted(事件时刻, bar时刻, side="right") - 1` 找每根 bar 之前
    最近的一条事件。早于首根 bar 的事件对首行就有效（它们是「当时已知」
    的事实，不是泄漏）；面板外标的忽略并计数。
    """
    times = np.asarray(bar_stream.times, dtype="int64")
    n_sym = len(bar_stream.symbols)
    grid = np.full((times.size, n_sym), np.nan, dtype="float64")
    sym_index = {s: i for i, s in enumerate(bar_stream.symbols)}
    values = np.asarray(event_stream.fields[field_name], dtype="float64")
    ev_times = np.asarray(event_stream.times, dtype="int64")
    outside = 0
    for j, sym in enumerate(event_stream.symbols):
        col = sym_index.get(sym)
        if col is None:
            outside += 1
            continue
        col_vals = values[:, j] if values.ndim == 2 else values
        # 只用「本标的自己有观测」的事件时刻做 as-of。event_stream 的 times 是
        # 全标的事件时间并集，col_vals 在别的标的的时刻上是 NaN；若拿全局并集
        # searchsorted，bar 时刻常落在「属于别的标的」的事件上 → 取到 NaN，
        # 事件流字段会随标的事件相位的偶然重合而大面积缺失（OOS 段尤甚）。
        own = np.isfinite(col_vals)
        ev_t = ev_times[own]
        ev_v = col_vals[own]
        order = np.argsort(ev_t, kind="stable")
        ev_t = ev_t[order]
        ev_v = ev_v[order]
        if ev_t.size == 0:
            continue
        idx = np.searchsorted(ev_t, times, side="right") - 1
        known = idx >= 0
        grid[known, col] = ev_v[idx[known]]
    return grid, outside


def _inject_event_fields(
    datasets: Mapping[str, Any], stream: "FieldStream", symbol_set: str,
) -> "tuple[FieldStream, dict[str, dict[str, Any]]]":
    """把已采集的事件数据集聚合成面板字段并入 FieldStream（D-006）。

    每个字段走各自的 read 子调用（复用同一套 PIT/窗口/标的校验），再
    as-of 对齐到 bar 网格。注册表是 unit/available_lag 的唯一真相；字段
    未注册则跳过（不硬造元信息）。返回 (新流, 逐字段注入摘要)。
    """
    from capability.registry import default_registry

    reg = default_registry()
    added_fields: dict[str, "np.ndarray"] = {}
    added_manifest: dict[str, dict[str, Any]] = {}
    notes: dict[str, dict[str, Any]] = {}
    for dataset, field_name in _EVENT_FIELD_BY_DATASET.items():
        if dataset not in datasets:
            continue
        if field_name in stream.fields:
            continue  # 基座数据集已带该字段，不覆盖
        meta = reg.fields.get(field_name)
        if not isinstance(meta, dict):
            continue  # 未注册字段不硬造 available_lag/unit
        ds = datasets[dataset]
        if field_name not in ds.columns:
            notes[field_name] = {"dataset": dataset, "status": "column_missing"}
            continue
        batch = SupplyBatch(
            pin=CapPin.from_contract(ds.pin()),
            symbol_set=str(symbol_set),
            source_ref=Ref(
                kind="supply", id=f"{ds.dataset}/{ds.version}",
                digest=Digest.parse(ds.checksum),
                relpath=f"{ds.dataset}/{ds.exchange}/{ds.market}",
                schema_version="1",
            ),
            columns=dict(ds.columns), symbol_classes={},
        )
        ds_coverage = ds.coverage
        read_call = Call.create(
            capability="read", station="Panel", impl="published_only",
            impl_version="1",
            params={
                "datasets": [dataset], "fields": {dataset: [field_name]},
                "frequency": "1d",
                "window": {"start": _as_of_iso(ds_coverage["start"]),
                           "end": _as_of_iso(ds_coverage["end"])},
                "symbols": str(symbol_set),
            },
            inputs=(InputRef("supply", batch.digest(), data=batch),),
        )
        read_out = capability_run(read_call)
        ev_stream = read_out.get("stream")
        if not isinstance(ev_stream, FieldStream):
            notes[field_name] = {"dataset": dataset, "status": "read_failed"}
            continue
        grid, outside = _event_field_grid(ev_stream, stream, field_name)
        known = int(np.isfinite(grid).sum())
        added_fields[field_name] = grid
        added_manifest[field_name] = {
            "dataset": dataset, "version": str(ds.version),
            "unit": str(meta.get("unit") or ""),
            "available_lag": int(meta.get("available_lag") or 0),
        }
        notes[field_name] = {
            "dataset": dataset, "status": "injected",
            "events": int(np.asarray(ev_stream.times).size),
            "outside_symbols": outside,
            "known_cells": known,
            "total_cells": int(grid.size),
        }
    if not added_fields:
        return stream, notes
    merged = FieldStream(
        times=stream.times, symbols=stream.symbols,
        fields={**stream.fields, **added_fields},
        manifest={**stream.manifest, **added_manifest},
        end_closed=stream.end_closed,
        symbol_classes=dict(stream.symbol_classes),
    )
    return merged, notes


def _funding_supply(
    datasets: Mapping[str, Any],
    plan: Mapping[str, Any],
    stream: "FieldStream",
) -> "np.ndarray | None":
    """fee_model 要求计资金费时构造事件网格；否则返回 None（不读盘）。

    缺数据集 / 缺面板标的都 fail-closed：口径开了却拿全零网格冒充
    「已按事件计费」，是成本口径的静默失真，比报错更糟。
    """
    net_caliber = (plan.get("caliber") or {}).get("net") or {}
    fee_model = str(net_caliber.get("fee_model") or "")
    if fee_model not in _FUNDING_FEE_MODELS:
        return None
    if "funding" not in datasets:
        raise FlowError(
            E4002,
            "fee_model 要求按事件计资金费，但供给里没有 funding 数据集",
            path="caliber.net.fee_model",
            hint="flow fetch --datasets funding --exchange <同 ohlcv> 先采集",
        )
    funding_ds = datasets["funding"]
    covered = {str(s) for s in funding_ds.columns["symbol"]}
    missing = [s for s in stream.symbols if s not in covered]
    if missing:
        raise FlowError(
            E4002, f"funding 事件表缺面板标的: {missing[:5]}",
            path="supply.datasets.funding",
            hint="资金费按事件逐标的求和；缺事件的标的不得按零费率冒充",
        )
    grid, _dropped = _funding_grid(funding_ds, stream)
    return grid


def _funding_grid(funding: Any, stream: Any) -> tuple["np.ndarray", int]:
    """资金费事件表 → 面板同形的逐 bar 费率网格（I34/AC34）。

    结算间隔实测 1h/4h/8h 混合且同一合约会变，所以**只能按事件求和**：
    把 `(t_{k-1}, t_k]` 区间内的全部事件费率加到第 k 根 bar 上。同一个
    「区间内事件总额」与结算网格无关——1h 网格 8 条、4h 网格 2 条，只要
    总额相同，聚合到同一 bar 后金额就相等（这正是 AC34 的对拍判据）。

    事件早于面板首根 bar 的无法归属到任何持仓期，直接丢弃并计数；
    面板之外的标的同样丢弃。返回 `(网格, 丢弃的事件数)`。
    """
    import pandas as pd

    times = np.asarray(stream.times, dtype="int64")
    symbols = tuple(stream.symbols)
    index = {s: i for i, s in enumerate(symbols)}
    cols = funding.columns
    sym_arr = np.asarray(
        [index.get(str(s), -1) for s in cols["symbol"]], dtype="int64")
    ts_arr = np.asarray(
        [pd.Timestamp(t).value for t in cols["timestamp"]], dtype="int64")
    rate_arr = np.asarray(cols["funding_rate"], dtype="float64")
    grid = np.zeros((times.size, len(symbols)), dtype="float64")
    known = sym_arr >= 0
    dropped = int(np.count_nonzero(~known))
    sym_arr, ts_arr, rate_arr = sym_arr[known], ts_arr[known], rate_arr[known]
    if ts_arr.size:
        # side="right"：恰好在 t_k 的结算落进第 k 根 bar（(t_{k-1}, t_k]）。
        pos = np.searchsorted(times, ts_arr, side="right") - 1
        inside = pos >= 0
        dropped += int(np.count_nonzero(~inside))
        np.add.at(grid, (pos[inside], sym_arr[inside]), rate_arr[inside])
    return grid, dropped


def build_live_supply(
    *,
    datasets: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """从文件式供给构造 ``supply_fn``（19-supply.md §2.4）；只读，不改盘。

    流程：`store.Dataset` → 内存 Pin（version = checksum[:16]）→ `SupplyBatch`
    → 调 `read` 能力转 `FieldStream`。没有别名表、没有 manifest、没有装载器中间层：
    身份在读取时现算，所以「读的是哪份」由 checksum 回答，而不是由某个文件记录。

    本轮只把 `ohlcv` 接成面板。其它数据集（funding/OI/basis）已落在同一套存储里；
    其中 funding 在 `caliber.net.fee_model` 要求计费时按事件聚合进成本链
    （见 `_funding_grid`），OI/basis 仍未被任何成本模型消费——这一点必须
    显式说出，不能让「已采集」被误读成「已使用」。
    """
    supply = plan.get("supply") or {}
    if store.BAR_DATASET not in datasets:
        raise FlowError(
            E1004, "live 需要 ohlcv 作为面板基座", path="supply.datasets",
            hint="flow fetch --datasets ohlcv 先采面板数据",
        )
    primary = datasets[store.BAR_DATASET]
    contract_pin = primary.pin()
    cap_pin = CapPin.from_contract(contract_pin)
    source_ref = Ref(
        kind="supply", id=f"{primary.dataset}/{primary.version}",
        digest=Digest.parse(primary.checksum),
        relpath=f"{primary.dataset}/{primary.exchange}/{primary.market}",
        schema_version="1",
    )
    batch = SupplyBatch(
        pin=cap_pin, symbol_set=str(supply.get("symbols") or ""),
        source_ref=source_ref, columns=dict(primary.columns), symbol_classes={},
    )

    from capability.registry import default_registry
    reg = default_registry()
    fields = [f for f in primary.columns
              if f not in (store.SYMBOL_COL, store.TIMESTAMP_COL) and f in reg.fields]
    if not fields:
        raise FlowError(E3003, f"{primary.dataset} 没有可注册字段", path="supply.fields")
    coverage = primary.coverage
    read_call = Call.create(
        capability="read", station="Panel", impl="published_only", impl_version="1",
        params={
            "datasets": [primary.dataset], "fields": {primary.dataset: fields},
            "frequency": str(supply.get("frequency") or "1d"),
            "window": {"start": _as_of_iso(coverage["start"]),
                       "end": _as_of_iso(coverage["end"])},
            "symbols": str(supply.get("symbols") or ""),
        },
        inputs=(InputRef("supply", batch.digest(), data=batch),),
    )
    # 组合根先于 Executor 调 read，不能等 CapabilityBridge 构造时才装配实现；
    # 否则测试重置过实现表后，这里会误报「read 尚无真实实现」。
    install_default_impls()
    read_out = capability_run(read_call)
    stream = read_out.get("stream")
    if not isinstance(stream, FieldStream):
        raise FlowError(E3003, "read 能力未返回 FieldStream", path="read.outputs.stream")

    as_of = _as_of_iso(coverage["end"])[:10]  # YYYY-MM-DD，来自供给事实不是墙钟

    # ---- 事件字段注入（D-006）------------------------------------------
    # funding/OI/basis 已采集且注册表声明了字段映射：按 as-of 前向填充聚
    # 合成面板字段并入 FieldStream，funding_carry / oi_change20 / basis_carry
    # 三个内置因子由此真正可算——「已采集未消费」到此闭合。数据没采集时
    # 不注入、不造零值：NaN 语义诚实（没数据 ≠ 零信号），因子评估的覆盖
    # 率预筛会给出明确拒绝原因。
    stream, event_field_notes = _inject_event_fields(
        datasets, stream, str(supply.get("symbols") or ""))

    # prior_pool：计划里只有名字（schema type=ref），解析在这里做且只做一次。
    # 已解析的表达式经 market_view 进 search；被排除的条目（例如现货集引用
    # funding_rate）带原因一起注入，由 Search 产物原样带出——不是静默丢弃。
    # `db:*` / `db:<name>` 走因子库，这是「上一轮挖到的因子当下一轮先验」的唯一通路。
    from capability.factors import resolve_prior_pool
    from storage.catalog import Catalog
    from storage.layout import Layout
    prior_pool = (plan.get("search") or {}).get("prior_pool") or []
    catalog = None
    if any(str(x).startswith("db:") for x in prior_pool):
        catalog = Catalog(Layout(resolve_data_root()))
    available_fields = set(fields) | set(event_field_notes)
    prior_resolved, prior_skipped = resolve_prior_pool(
        [str(x) for x in prior_pool], available_fields=available_fields, catalog=catalog,
    )
    market_view = {
        "available_from": as_of,
        "prior_candidates": prior_resolved,
        "prior_skipped": prior_skipped,
        "event_fields": event_field_notes,
    }

    # ---- 资金费（I34：按事件、不按周期常数乘）---------------------------
    # 口径开了（fee_model）才构造；构造失败 fail-closed，见 `_funding_supply`。
    funding_array = _funding_supply(datasets, plan, stream)

    def supply_fn(station: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if station == "Boundary":
            return {"pin": cap_pin, "field_stream": stream}
        if station == "Panel":
            return {"field_stream": stream}
        if station == "Search":
            return {"market_view": dict(market_view)}
        if funding_array is not None:
            return {"funding": funding_array}
        return {}

    return supply_fn


__all__ = ["expand_station_inputs", "build_live_supply"]
