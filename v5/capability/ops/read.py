"""`read` · 事实读取：窗口裁剪、字段清单解析、长表 → 面板（s0/04 §3.1）。

这个能力只做四件事，每一件都对应一条会静默出错的路径：

1. **只读已发布版本**。它没有「下载」「补齐」「最新」参数——不是遗漏，是设计
   （§3.1 末句）。`window` 超出 `pin.coverage` 即 `E4002`，不做「取交集继续跑」：
   交集会让一次「读一段不存在的历史」悄悄变成「读一段更短的历史」。
2. **字段清单解析**。请求的字段必须是「注册表登记 × 该数据集 × 已发布版本列」
   的交集，任何一处不满足即 `E1014`（算法声明的字段超出已固化供给版本的覆盖）。
   三者不一致时若只取并集，`align` 到 `evaluate` 才会炸，而那时错误已经离现场很远。
3. **长表 → `(T,N)` 面板**。时间轴取窗口内出现过的时点并集，标的取出现过的清单。
   `(symbol, time)` 重复即 `E4007`：同一时点两次观测会被当成两个独立样本。
4. **`missing_ranges` 只报内部缺口**。标的上市前 / 退市后的空段不是「缺数据」，
   那是它还不存在；报出来会让「缺数据」这个信号被结构性空段淹没。

能力层不 import pyarrow/pathlib（AC31）：载荷由执行器解析成 `SupplyBatch` 传入。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np

from v5.capability.registry import default_registry
from v5.capability.stream import FieldStream, SupplyBatch
from v5.contracts.errors import (
    E1004, E1014, E3001, E3003, E4002, E4007, FlowError,
)

# 声明频率 → 纳秒步长。缺失任何一项都是契约漂移，_FREQ_NS 只与被注册的
# enum 取值对齐（`registry/params.json` 的 read.frequency）。
_FREQ_NS = {
    "1m": 60_000_000_000,
    "5m": 300_000_000_000,
    "15m": 900_000_000_000,
    "1h": 3_600_000_000_000,
    "4h": 14_400_000_000_000,
    "1d": 86_400_000_000_000,
}
# 频率只对 bar 数据集有意义：事件流（funding / OI / basis）的间隔本身不是
# 常量（I34：实测 1h/4h/8h 混合且同一合约会变），用频率去卡它是无解的。
# 这条与 `gate_b.py` 的 BAR_DATASET 是同一个裁决，不能一层一套。
_BAR_DATASET = "ohlcv"
# 只允许整数纳秒。给一个 1990–2100 的区间做 sanity check：毫秒/微秒时间戳
# 落在区间外，会在「窗口到不了任何观测」之前就被认出来。
_NS_MIN = -2208988800000000000  # 1900-01-01T00:00:00Z
_NS_MAX = 4102444800000000000   # 2100-01-01T00:00:00Z


def _supply(call) -> SupplyBatch:
    for item in call.inputs:
        if item is not None and item.kind == "supply":
            data = item.data
            if isinstance(data, SupplyBatch):
                return data
            raise FlowError(
                E3003,
                f"read 的 supply 输入不是 SupplyBatch，得到 {type(data).__name__}",
                path="read.inputs.supply",
            )
    raise FlowError(
        E3003, "read 没有拿到供给载荷", path="read.inputs.supply",
        hint="执行器解析 supply Ref 后放进 inputs[i].data；能力层不自己读盘",
    )


def _to_ns(value: Any, path: str) -> int:
    """时间戳归一成 int 纳秒。

    只接受三种形态：整数纳秒、`np.datetime64`、ISO 文本。**不接受**裸数字
    字符串与浮点秒——`1.7e9` 与 `1700000000` 都能被 `float()` 解释成「某一刻」，
    而它们既不是纳秒也不是秒，差 9 个数量级也不会报错。

    `np.datetime64` 必须显式换成 `[ns]` 再取整数：`datetime64[us]` 的
    `astype("int64")` 给的是**微秒数**，直接当纳秒用会让整条时间轴落到
    1970 年附近，而错误表现为「窗口里没有观测」。
    """
    if isinstance(value, bool):
        raise FlowError(E4002, f"{path} 是布尔，不是时间戳", path=path)
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            raise FlowError(E4002, f"{path} 是 NaT", path=path)
        return _checked(int(value.astype("datetime64[ns]").astype("int64")), path)
    if isinstance(value, (int, np.integer)):
        return _checked(int(value), path)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise FlowError(
                E4002, f"{path} 的 datetime 没有时区", path=path,
                hint="naive 时间会被误当本地时间，整体偏移 8 小时",
            )
        value = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, str):
        text = value.strip()
        # numpy 只认无时区后缀的字面量；UTC 是构造契约（数据层已归一），
        # 所以先去掉 Z / +00:00 再解析，而不是接受任意时区偏移。
        for suffix in ("Z", "+00:00"):
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                break
        try:
            ts = np.datetime64(text, "ns")
        except (ValueError, TypeError, OverflowError):
            raise FlowError(
                E4002, f"{path} 无法解析为时间戳: {value!r}", path=path,
            ) from None
        if np.isnat(ts):
            raise FlowError(E4002, f"{path} 无法解析为时间戳: {value!r}", path=path)
        return _checked(int(ts.astype("int64")), path)
    raise FlowError(
        E4002, f"{path} 必须是 int 纳秒或 ISO 时间文本，得到 {type(value).__name__}",
        path=path,
    )


def _checked(ns: int, path: str) -> int:
    if ns < _NS_MIN or ns > _NS_MAX:
        raise FlowError(
            E4002,
            f"{path}={ns} 不在 1900–2100 的纳秒区间内",
            path=path,
            hint="时间戳必须是纳秒；毫秒/微秒/秒会落在这个区间之外",
        )
    return ns


def _numeric(name: str, value: Any) -> float:
    """列值 → float。`None` 是缺失，非数值是供给坏了——两者不能混。"""
    if value is None:
        return float("nan")
    if isinstance(value, bool):
        raise FlowError(
            E3003, f"字段 {name!r} 的取值是布尔 {value!r}", path=f"read.supply.{name}",
            hint="布尔字段不能当数值用；True 会静默变成 1.0",
        )
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    raise FlowError(
        E3003,
        f"字段 {name!r} 的取值不可转成数值: {value!r}（{type(value).__name__}）",
        path=f"read.supply.{name}",
    )


def _version_step(times: np.ndarray, path: str) -> int:
    """供给版本的基准步长 = **全版本**相邻时点的最小间隔。

    取窗口内的中位数是错的，两个方向都错：3 根 bar 的窗口只要有一个洞，
    中位数就变成 2 天、把合法的 1d 数据判死；而窗口只有 1 根时中位数不存在，
    任何频率都能通过。频率是**版本**的属性，不是窗口的属性。
    """
    if times.size < 2:
        raise FlowError(
            E4002, "供给版本只有一根 bar，无法校验 frequency", path=path,
            hint="单 bar 版本没有时间结构；不得以「跳过校验」代替失败",
        )
    return int(np.min(np.diff(times)))


def _check_frequency(freq: str, times: np.ndarray, dataset: str) -> None:
    """声明频率必须与供给版本的基准步长一致，且所有间隔都是它的整数倍。

    错配的后果不是算错一次，而是窗口长度整体错倍数——`tier_window=20` 在
    1d 数据上被当 1h 用，回看从 20 天变成 20 小时。用「最小间隔 × 整数倍」
    而不是「中位数相等」：前者允许缺口（真实数据必然有），后者不允许。

    **能拦什么、不能拦什么**：这一条拦得住「声明 1d 实际是 1h」（步长不符）
    与非整数倍间隔；**拦不住**「1h 数据里混了 4h 间隔」——4h 是 1h 的整数倍，
    而且它与「连续 3 根缺失」在数据层面完全同形。凡是能拦的都已拦，写清楚
    边界是为了不让人以为这条检查比它实际更强。
    """
    if dataset != _BAR_DATASET:
        return
    step = _version_step(times, "read.params.frequency")
    expected = _FREQ_NS[freq]
    if step != expected:
        raise FlowError(
            E4002,
            f"供给版本的基准步长 {step}ns 与声明 frequency={freq!r}（{expected}ns）不符",
            path="read.params.frequency",
            hint="频率是口径的一部分：把 1d 当 1h 会让所有窗口长度整体错 24 倍",
        )
    gaps = np.diff(times)
    if np.any(gaps % expected != 0):
        odd = int((gaps % expected != 0).sum())
        raise FlowError(
            E4002,
            f"供给版本有 {odd} 处间隔不是 {freq!r} 的整数倍",
            path="read.params.frequency",
            hint="混合频率的版本不能声明成单一频率；它没有可用的窗口单位",
        )


def build(call) -> dict:
    params = call.params
    batch = _supply(call)
    pin = batch.pin

    datasets = [str(d) for d in params["datasets"]]
    fields_by_ds: dict[str, list[str]] = {
        str(k): [str(f) for f in v] for k, v in params["fields"].items()
    }

    # ---- 数据集与版本一一对应 -----------------------------------------
    if len(datasets) != 1:
        # 一个 supply 输入只承载一个版本。多数据集要多个输入，而 inputs 是
        # 有序定长的——S3 供给链一次只给一个版本，这里不假装支持。
        raise FlowError(
            E3001,
            f"read 只拿到 1 个 supply 输入，但 datasets 声明了 {len(datasets)} 个: {datasets}",
            path="read.params.datasets",
            hint="每个数据集需要各自的版本引用；不得用同一个版本冒充多个数据集",
        )
    dataset = datasets[0]
    if pin.dataset != dataset:
        raise FlowError(
            E3001,
            f"请求的数据集 {dataset!r} 与供给版本的 {pin.dataset!r} 不符",
            path="read.inputs.supply",
            hint="读哪个数据集就绑哪个版本的 Pin；错绑不会报错，只会拿错数",
        )
    extra_ds = sorted(set(fields_by_ds) - set(datasets))
    if extra_ds:
        raise FlowError(
            E1004,
            f"fields 含 datasets 未声明的数据集 {extra_ds}",
            path="read.params.fields",
            hint="fields 的键必须 ⊆ datasets；否则那些字段永远不会被读到",
        )
    requested = fields_by_ds.get(dataset)
    if not requested:
        raise FlowError(
            E1004, f"fields 没有给出数据集 {dataset!r} 的字段清单", path="read.params.fields",
        )
    if len(set(requested)) != len(requested):
        dup = sorted({f for f in requested if requested.count(f) > 1})
        raise FlowError(
            E1004, f"fields.{dataset} 有重复字段 {dup}", path=f"read.params.fields.{dataset}",
        )

    # ---- 标的集：计划名必须与供给版本记录的名字一致 --------------------
    declared_set = str(params["symbols"])
    if declared_set != batch.symbol_set:
        raise FlowError(
            E4002,
            f"标的集 {declared_set!r} 与供给版本记录的 {batch.symbol_set!r} 不一致",
            path="read.params.symbols",
            hint="标的集是供给版本的一部分；不一致说明读到的是另一批标的",
        )

    # ---- 字段清单：注册表 × 数据集 × 已发布列，三者取交集 -------------
    reg = default_registry()
    manifest: dict[str, dict[str, Any]] = {}
    for name in requested:
        entry = reg.fields.get(name)
        if entry is None:
            raise FlowError(
                E1014, f"字段 {name!r} 未在字段注册表登记", path=f"read.params.fields.{name}",
                hint=f"已登记: {sorted(reg.fields)}",
            )
        if dataset not in (entry.get("datasets") or []):
            raise FlowError(
                E1014,
                f"字段 {name!r} 不属于数据集 {dataset!r}（注册表：{entry.get('datasets')}）",
                path=f"read.params.fields.{name}",
            )
        if name not in batch.columns:
            raise FlowError(
                E1014,
                f"字段 {name!r} 不在已发布版本 {dataset}/{pin.version} 的列里",
                path=f"read.params.fields.{name}",
                hint=f"该版本可用列: {sorted(batch.columns)}",
            )
        manifest[name] = {
            "dataset": dataset,
            "version": pin.version,
            "unit": str(entry["unit"]),
            # 供给元信息只从注册表取，不从载荷取：载荷能改写 lag 就等于
            # 把前视藏进数据层（align 的 P2 检查正是比这两个值）。
            "available_lag": int(entry["available_lag"]),
        }

    # ---- 窗口 ---------------------------------------------------------
    window = dict(params["window"])
    w_lo = _to_ns(window["start"], "read.params.window.start")
    w_hi = _to_ns(window["end"], "read.params.window.end")
    if w_lo > w_hi:
        raise FlowError(
            E4002, "window.start 晚于 window.end", path="read.params.window",
            hint="空窗口不是「没有信号」，是没有定义研究区间",
        )
    cov_lo, cov_hi = pin.coverage
    if w_lo < cov_lo or w_hi > cov_hi:
        raise FlowError(
            E4002,
            "window 超出供给版本 coverage",
            path="read.params.window",
            hint="超出覆盖的时点没有版本可追溯；不得截断成「读到哪算哪」",
            context={"window": [w_lo, w_hi], "coverage": [int(cov_lo), int(cov_hi)]},
        )

    # ---- 长表 → 面板 ---------------------------------------------------
    time_col = batch.column(batch.time_column)
    sym_col = batch.column(batch.symbol_column)
    parsed = [
        (_to_ns(time_col[i], f"read.supply.{batch.time_column}[{i}]"),
         str(sym_col[i]), i)
        for i in range(batch.rows)
    ]
    version_times = np.array(sorted({ts for ts, _, _ in parsed}), dtype="int64")
    _check_frequency(str(params["frequency"]), version_times, dataset)

    rows = [item for item in parsed if w_lo <= item[0] <= w_hi]
    if not rows:
        raise FlowError(
            E4002,
            "window 在已发布版本上没有落到任何观测",
            path="read.params.window",
            hint="空面板不得当作成功：下游会把「没有数据」读成「没有信号」",
        )

    times = np.array(sorted({ts for ts, _, _ in rows}), dtype="int64")
    # 标的清单取**供给版本**的标的集，不是「窗口内出现过谁」。
    # 用窗口行推标的集，会让 N（top-k、分位点、覆盖率的分母）随窗口漂移：
    # 整窗没有观测的标的会被静默摘掉，而 `missing_ranges` 按定义只报内部缺口、
    # 不会报「整窗没有行」。它的正确表达是 `boundary` 的 `not_listed`/`delisted`
    # ——那需要这一列存在。`symbol_classes` 同理，必须按宇宙校验。
    symbols = batch.symbols
    t_index = {int(ts): i for i, ts in enumerate(times)}
    s_index = {sym: j for j, sym in enumerate(symbols)}

    # 同一 (标的, 时点) 出现两次：时间索引重复会让「同一时点两次观测」变成两个
    # 独立样本，标准误被低估。整批失败，不保留「先到的那条」——两条值不同时
    # 选哪条都是随手决定。
    seen: set[tuple[int, str]] = set()
    for ts, sym, _ in rows:
        key = (int(ts), sym)
        if key in seen:
            raise FlowError(
                E4007,
                f"({sym}, {ts}) 在版本 {dataset}/{pin.version} 里出现两次",
                path=f"read.supply.{batch.time_column}",
            )
        seen.add(key)

    shape = (times.size, len(symbols))
    arrays: dict[str, np.ndarray] = {}
    for name in requested:
        col = batch.column(name)
        out = np.full(shape, np.nan, dtype="float64")
        for ts, sym, i in rows:
            out[t_index[ts], s_index[sym]] = _numeric(name, col[i])
        arrays[name] = out

    stream = FieldStream(
        times=times,
        symbols=symbols,
        fields=arrays,
        manifest=manifest,
        end_closed=bool(pin.end_closed),
        symbol_classes=dict(batch.symbol_classes),
    )

    return {
        # 供给版本引用（provenance），**不是**可解析的产物引用：产物身份是
        # `stream.digest()`。把 `stream_refs` 当产物引用去取数会拿到未经窗口
        # 裁剪的整份载荷——两个内容、同一个名字。
        "stream_refs": [batch.source_ref],
        "field_manifest": {k: dict(v) for k, v in manifest.items()},
        "coverage": stream.coverage,
        "missing_ranges": stream.missing_ranges,
        "stream": stream,
    }
