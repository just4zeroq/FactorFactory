"""供给侧值对象：`Pin` / `FieldStream` / `Scope`（s0/01 §3.3、s0/04 §3.1–3.3）。

三个对象分别对应「数据是哪个版本」「字段长什么样」「哪些格能用」：

- **`Pin`** 是版本引用，不是数据。它只回答「这批数据是哪一次采集、覆盖到哪、
  末端是否收盘」。`end_closed=false` 的版本可以存在于 Depot，但不得被别名指向。
- **`FieldStream`** 是 `read` 的产物：逐字段 `(T,N)` 数组 + 每字段的供给元信息。
  它没有「补齐」「最新」这类参数——版本一旦固定，同一个 `Pin` 只对应一份内容。
- **`Scope`** 是 `boundary` 的产物：两张**分开**的掩码 + 分层 + 逐格理由码。

`Scope` 的形状校验里有一条容易漏掉的不变量：`tradable_mask ⊆ observable_mask`。
「不可观测却能下单」是 V4 里那类最难查的错误——持仓按一张表算、成本按另一张表算，
两边都自洽，合起来多出一笔根本不存在的成交。

三个对象都不可变：`Scope` 的掩码构造后只读，下游想改必须造新对象，于是
「谁改过掩码」在类型层面就不可能发生。

另外还有 `SupplyBatch`：执行器把 `supply` Ref 解析成「版本事实 + 长表列」后
交给 `read`。它存在的理由是 AC31——能力层不得 import pyarrow/pathlib，所以
「打开哪个文件」这件事必须留在事实层，能力层只拿到一张已校验的只读表。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np

from v5.contracts.digest import compute_digest
from v5.contracts.errors import (
    E1014, E3003, E4001, E4002, E4004, E4005, E4007, FlowError,
)
from v5.contracts.types import Pin as ContractPin
from v5.contracts.types import Ref

from .panel import as_field, as_times

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_NEG_INF = -(2**63)

# 逐格理由码（s0/04 §3.2 的表）。`0` 始终表示纳入。
REASON_CODES: dict[str, int] = {
    "valid": 0,
    "reserved": 1,
    "not_listed": 2,
    "delisted": 3,
    "missing_data": 4,
    "unclosed": 5,
    "low_liquidity": 6,
    "stable_or_leveraged": 7,
}
# 每条理由允许出现在哪张掩码里：不可观测的码、只影响交易的码。
_UNOBSERVABLE = frozenset({1, 2, 3, 4})
_NOT_TRADABLE = frozenset({1, 2, 3, 4, 5, 6, 7})
# 可观测码从 legend 派生，不在调用方抄字面量（G9）：新增理由码时，只要它在
# `REASON_CODES` 里且不在 `_UNOBSERVABLE`，observable 掩码就自动跟上。
# 注意「可观测」≠「可交易」：unclosed / low_liquidity / stable_or_leveraged
# 都能进统计，只是不能下单。
OBSERVABLE_CODES: frozenset[int] = (
    frozenset(REASON_CODES.values()) - _UNOBSERVABLE
)

SYMBOL_CLASSES = frozenset({"spot", "perp", "stable", "leveraged"})


def interior_missing_runs(
    arrays: Mapping[str, np.ndarray], symbols: Sequence[str], times: np.ndarray,
) -> list[dict]:
    """逐标的的内部缺口段。

    「内部」的定义是**首末有效观测之间**：上市前与退市后的空段不属于缺口。
    把结构性空段也报成缺口，会让「缺数据」这个信号在每个新标的的首段触发
    一次，读者随即学会忽略它。

    判据是「所有给定字段在该格同时缺失」——只看单个字段会把「该字段本来
    就不适用于这个数据集」误报成缺口。
    """
    n_col = len(symbols)
    t_len = int(times.size)
    if t_len == 0 or n_col == 0 or not arrays:
        return []
    all_missing = np.ones((t_len, n_col), dtype=bool)
    for values in arrays.values():
        all_missing &= ~np.isfinite(values)

    out: list[dict] = []
    for j, sym in enumerate(symbols):
        idx = np.flatnonzero(~all_missing[:, j])
        if idx.size == 0:
            continue
        lo, hi = int(idx[0]), int(idx[-1])
        run_start = None
        for t in range(lo, hi + 1):
            if all_missing[t, j]:
                if run_start is None:
                    run_start = t
                continue
            if run_start is not None:
                out.append({
                    "symbol": str(sym),
                    "start": int(times[run_start]),
                    "end": int(times[t - 1]),
                    "bars": int(t - run_start),
                })
                run_start = None
        # `hi` 处必有值，所以循环结束时不会有悬挂的 run_start：尾段缺口
        # 不在「首末有效观测之间」，本就不算缺口。
    return out


def _dt_ns(value: datetime) -> int:
    """带时区的 datetime → UTC 纳秒（去掉 tzinfo 再交给 numpy，避免时区告警）。"""
    if value.tzinfo is None:
        raise FlowError(
            E4002, "coverage 的 datetime 没有时区", path="pin.coverage",
            hint="naive 时间会被误当本地时间",
        )
    naive = value.astimezone(timezone.utc).replace(tzinfo=None)
    return int(np.datetime64(naive, "ns").astype("int64"))


@dataclass(frozen=True)
class Pin:
    """供给版本引用（s0/01 §3.3）。十个字段一个都不能少。"""

    dataset: str
    version: str
    checksum: str
    coverage: tuple[int, int]
    symbols_digest: str
    end_closed: bool
    schema_version: str = "1"
    acquired_at: str = ""
    gate_a: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dataset or not self.version:
            raise FlowError(
                E4001, "pin 缺 dataset 或 version", path="pin",
                hint="版本缺失意味着「不知道读的是哪一批数据」",
            )
        # checksum 必须是**可解析的内容摘要**：空串会让 SupplyBatch 的
        # 「Ref 与版本指向同一份内容」这条检查被 `if checksum` 静默关掉。
        if not isinstance(self.checksum, str) or not _SHA256.match(self.checksum):
            raise FlowError(
                E4004,
                f"pin.checksum 必须是 'sha256:<64 位小写十六进制>'，得到 {self.checksum!r}",
                path="pin.checksum",
                hint="内容摘要的形态是契约的一部分；空串等于关掉完整性校验",
            )
        start, end = self.coverage
        if not isinstance(start, int) or not isinstance(end, int):
            raise FlowError(
                E4002, f"pin.coverage 必须是整数纳秒，得到 {self.coverage}",
                path="pin.coverage",
            )
        if start >= end:
            raise FlowError(
                E4002, f"pin.coverage 必须 start < end，得到 {self.coverage}",
                path="pin.coverage",
            )
        if not isinstance(self.end_closed, bool):
            raise FlowError(
                E4002, "pin.end_closed 必须是布尔", path="pin.end_closed",
                hint="用了真值判断，'false' 字符串会被当成 True",
            )
        gate = dict(self.gate_a)
        if gate and not gate.get("passed", False):
            # 与门禁 B 同一个码（`gate_b.py` 也判 E4005）：「门禁 A 未通过的版本
            # 被引用」只有一种分类，两个入口不许给出两个码。
            raise FlowError(
                E4005,
                f"pin.gate_a 未通过（{gate.get('reason_code')!r}）",
                path="pin.gate_a",
                hint="采集侧门禁没过的版本不得进入任何流水线",
            )

    @classmethod
    def from_contract(cls, pin: ContractPin) -> "Pin":
        """契约层 `Pin`（datetime 覆盖区间 / `Digest` 校验和）→ 能力层 `Pin`。

        两套结构并存是历史事实（契约层要可 JSON 化，能力层要 int 纳秒）；
        但它们之间**只能有一个转换点**，否则「同一个版本」会在不同入口被换成
        不同的覆盖区间——差一个时区或一个单位都不会报错。
        """
        if not isinstance(pin, ContractPin):
            raise FlowError(
                E3003, f"from_contract 需要契约层 Pin，得到 {type(pin).__name__}",
                path="pin",
            )
        if pin.gate_a.get("passed") is False:
            raise FlowError(
                E4005,
                f"pin.gate_a 未通过（{pin.gate_a.get('reason_code')!r}）",
                path="pin.gate_a",
            )
        return cls(
            dataset=pin.dataset,
            version=pin.version,
            checksum=str(pin.checksum),
            coverage=(_dt_ns(pin.coverage["start"]), _dt_ns(pin.coverage["end"])),
            symbols_digest=str(pin.symbols_digest),
            end_closed=bool(pin.end_closed),
            schema_version=str(pin.schema_version),
            acquired_at=pin.acquired_at.astimezone(timezone.utc).isoformat(),
            gate_a=dict(pin.gate_a),
        )

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(b"v5-pin\x00")
        h.update(self.dataset.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.version.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.checksum.encode("utf-8"))
        h.update(b"\x00")
        h.update(f"{self.coverage[0]}:{self.coverage[1]}".encode("ascii"))
        h.update(b"\x00")
        h.update(self.symbols_digest.encode("utf-8"))
        h.update(b"\x00")
        h.update(b"closed" if self.end_closed else b"open")
        return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class SupplyBatch:
    """`supply` Ref 解析后的只读载荷：版本事实 + 长表列（s0/04 §3.1）。

    这是**事实层与能力层之间唯一的表通道**。执行器读已发布 parquet、校验
    payload 指纹、把列转成 Python 序列后构造它；`read` 只做窗口裁剪与
    长表 → 面板的转置。这样能力层既不需要 pyarrow，也没有「顺手打开另一个
    版本」的位置——AC31 的结构保证靠的就是这个数据流向，不是靠自觉。

    长表（`symbol, timestamp, <fields...>`）是已发布 payload 的原始形态，
    这里不做任何重排：一旦在这里按 (symbol, time) 排好序，`read` 的窗口
    裁剪就变成「按行号切」，而版本内容变了以后行号会漂移。
    """

    pin: Pin
    symbol_set: str
    source_ref: Ref
    columns: Mapping[str, Sequence[Any]]
    symbol_column: str = "symbol"
    time_column: str = "timestamp"
    symbol_classes: Mapping[str, str] = field(default_factory=dict)
    # 派生字段：构造期一次算好，不让每次访问都重扫一遍长表。
    _symbols: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)
    _rows: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.pin, Pin):
            raise FlowError(
                E3003, f"SupplyBatch.pin 必须是 Pin，得到 {type(self.pin).__name__}",
                path="supply.pin",
            )
        if not isinstance(self.source_ref, Ref):
            raise FlowError(
                E3003,
                f"SupplyBatch.source_ref 必须是 Ref，得到 {type(self.source_ref).__name__}",
                path="supply.source_ref",
                hint="stream_refs 直接回传它；能力层不自己拼路径",
            )
        if self.source_ref.kind != "supply":
            raise FlowError(
                E3003,
                f"SupplyBatch.source_ref.kind 必须是 supply，得到 {self.source_ref.kind!r}",
                path="supply.source_ref",
            )
        if not isinstance(self.symbol_set, str) or not self.symbol_set.strip():
            raise FlowError(
                E3003, "SupplyBatch.symbol_set 必须是非空字符串", path="supply.symbol_set",
                hint="标的集名要与计划里 supply.symbols 一致；空值等于放弃这条校验",
            )

        raw = dict(self.columns or {})
        if not raw:
            raise FlowError(E3003, "SupplyBatch 没有任何列", path="supply.columns")
        lengths = {len(list(values)) for values in raw.values()}
        if len(lengths) != 1:
            raise FlowError(
                E3003, f"SupplyBatch 各列长度不一致: {sorted(lengths)}",
                path="supply.columns",
            )
        rows = lengths.pop()
        if rows == 0:
            raise FlowError(
                E3003, "SupplyBatch 为空表", path="supply.columns",
                hint="空 payload 不得当作「这批没有数据」——它没有版本可用",
            )
        for name in (self.symbol_column, self.time_column):
            if name not in raw:
                raise FlowError(
                    E3003, f"SupplyBatch 缺列 {name!r}", path=f"supply.columns.{name}",
                    hint="长表必须带标的名与时间列；两者缺一就无法定位观测",
                )
        if self.symbol_column == self.time_column:
            raise FlowError(
                E3003, "SupplyBatch 的标的名列与时间列不能同名", path="supply.columns",
            )

        columns = {str(k): tuple(v) for k, v in raw.items()}
        symbols = tuple(sorted({str(s) for s in columns[self.symbol_column]}))

        classes: dict[str, str] = {}
        for sym, cls in (self.symbol_classes or {}).items():
            key, value = str(sym), str(cls)
            if key not in symbols:
                raise FlowError(
                    E1014, f"symbol_classes 含载荷里没有的标的 {key!r}",
                    path="supply.symbol_classes",
                )
            classes[key] = value

        # 版本事实与载荷身份必须指向同一份内容：Ref 是执行器解析出来的路径，
        # pin 是编译期冻结的版本；两者不一致时读到的可能已是另一个版本。
        if self.pin.checksum and str(self.source_ref.digest) != str(self.pin.checksum):
            raise FlowError(
                E4004,
                "source_ref.digest 与 pin.checksum 不一致",
                path="supply.source_ref.digest",
                hint="路径与版本事实必须指向同一份内容",
            )

        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "symbol_classes", classes)
        object.__setattr__(self, "_symbols", symbols)
        object.__setattr__(self, "_rows", rows)

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def column(self, name: str) -> tuple[Any, ...]:
        if name not in self.columns:
            raise FlowError(
                E1014, f"载荷里没有列 {name!r}", path=f"supply.columns.{name}",
                hint=f"可用列: {sorted(self.columns)}",
            )
        return self.columns[name]

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(b"v5-supply\x00")
        h.update(self.pin.digest().encode("ascii"))
        h.update(b"\x00")
        h.update(self.source_ref.id.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.symbol_set.encode("utf-8"))
        h.update(b"\x00")
        h.update(f"{self.symbol_column}\x1f{self.time_column}".encode("utf-8"))
        # 逐列编码带长度前缀：`None` 与 `""` 必须可区分（`str(None)=="None"` 会
        # 与真实取值 "None" 撞车），值里含分隔符也不得能构造碰撞。
        for name in sorted(self.columns):
            h.update(b"\x00col\x00")
            h.update(name.encode("utf-8"))
            for value in self.columns[name]:
                token = "" if value is None else str(value)
                h.update(b"\x1f")
                h.update(str(len(token)).encode("ascii"))
                h.update(b":")
                h.update(token.encode("utf-8"))
        # `symbol_classes` 决定 boundary 的 stable_or_leveraged 理由码——它是
        # 会改变结果的字段，不进指纹就等于「换一套标分类、复用旧掩码」。
        for sym in sorted(self.symbol_classes):
            h.update(b"\x00cls\x00")
            h.update(f"{sym}={self.symbol_classes[sym]}".encode("utf-8"))
        return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class FieldStream:
    """`read` 的产物：字段数组 + 逐字段供给元信息。

    `manifest` 每项必须含 `dataset` / `version` / `unit` / `available_lag`。
    `available_lag` 是**注册表**字段（`11-field-operator-registry.md`），这里只是
    把它带过来，不允许在流里改写——改写等于把前视藏进数据层。
    """

    times: np.ndarray
    symbols: tuple[str, ...]
    fields: Mapping[str, np.ndarray]
    manifest: Mapping[str, Mapping[str, Any]]
    end_closed: bool = True
    symbol_classes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        times = as_times(self.times)
        symbols = tuple(str(s) for s in self.symbols)
        if not symbols:
            raise FlowError(E4007, "FieldStream 标的清单为空", path="stream.symbols")
        if len(set(symbols)) != len(symbols):
            raise FlowError(E4007, "FieldStream 标的清单有重复", path="stream.symbols")
        shape = (times.size, len(symbols))

        fields: dict[str, np.ndarray] = {}
        for name, values in self.fields.items():
            fields[str(name)] = as_field(str(name), values, shape)
        if not fields:
            raise FlowError(E4007, "FieldStream 没有任何字段", path="stream.fields")

        manifest: dict[str, dict[str, Any]] = {}
        for name in fields:
            if name not in self.manifest:
                raise FlowError(
                    E1014, f"字段 {name!r} 缺供给元信息", path=f"stream.manifest.{name}",
                    hint="没有 available_lag 的字段无法参与因果检查",
                )
            item = dict(self.manifest[name])
            lag = item.get("available_lag")
            if not isinstance(lag, int) or isinstance(lag, bool) or lag < 0:
                raise FlowError(
                    E1014,
                    f"字段 {name!r} 的 available_lag 必须是非负整数，得到 {lag!r}",
                    path=f"stream.manifest.{name}.available_lag",
                )
            if not item.get("unit"):
                raise FlowError(
                    E1014, f"字段 {name!r} 缺 unit", path=f"stream.manifest.{name}.unit",
                )
            manifest[name] = item

        classes: dict[str, str] = {}
        for sym, cls in (self.symbol_classes or {}).items():
            key, value = str(sym), str(cls)
            if key not in symbols:
                raise FlowError(
                    E1014, f"symbol_classes 含未在清单中的标的 {key!r}",
                    path="stream.symbol_classes",
                )
            if value not in SYMBOL_CLASSES:
                raise FlowError(
                    E1014,
                    f"标的 {key!r} 的类型 {value!r} 不在 {sorted(SYMBOL_CLASSES)} 内",
                    path=f"stream.symbol_classes.{key}",
                )
            classes[key] = value

        object.__setattr__(self, "times", times)
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "symbol_classes", classes)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.times.size, len(self.symbols))

    def field(self, name: str) -> np.ndarray:
        if name not in self.fields:
            raise FlowError(
                E1014, f"流里没有字段 {name!r}", path=f"stream.fields.{name}",
                hint=f"可用字段: {sorted(self.fields)}",
            )
        return self.fields[name]

    @property
    def coverage(self) -> dict[str, int]:
        """实际覆盖：首末时点 + 根数 + 标的数（闭区间，与 `Pin.coverage` 同语义）。"""
        return {
            "start": int(self.times[0]),
            "end": int(self.times[-1]),
            "bars": int(self.times.size),
            "symbols": int(len(self.symbols)),
        }

    @property
    def missing_ranges(self) -> list[dict]:
        """逐标的**内部**缺口：首末有效观测之间、所有字段同时缺失的连续行。

        上市前与退市后的空段不算缺口（那是标的不存在，`boundary` 用
        `not_listed` / `delisted` 表达）。派生成而非构造入参：两份内容各存一处
        时，改了数组忘了改报告是必然的，而「缺口少了」这种错误没有任何检查点。
        """
        return interior_missing_runs(self.fields, self.symbols, self.times)

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(b"v5-stream\x00")
        h.update(np.ascontiguousarray(self.times).tobytes())
        for name in sorted(self.fields):
            h.update(b"\x00field\x00")
            h.update(name.encode("utf-8"))
            h.update(np.ascontiguousarray(self.fields[name]).tobytes())
        h.update(b"\x00symbols\x00")
        h.update("\x1f".join(self.symbols).encode("utf-8"))
        h.update(b"\x00closed" if self.end_closed else b"\x00open")
        # manifest 是 `available_lag` 的载体，也是 align 的因果门禁的唯一来源。
        # 不进指纹，「换了一个 lag 版本」在调用单与缓存层面就不可见——这正是
        # 「把前视藏进数据层」最省事的那条路径。
        for name in sorted(self.manifest):
            item = self.manifest[name]
            h.update(b"\x00meta\x00")
            h.update(
                f"{name}|{item.get('dataset')}|{item.get('version')}|"
                f"{item.get('unit')}|{item.get('available_lag')}".encode("utf-8")
            )
        for sym in sorted(self.symbol_classes):
            h.update(b"\x00cls\x00")
            h.update(f"{sym}={self.symbol_classes[sym]}".encode("utf-8"))
        return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class Scope:
    """`boundary` 的产物：两张掩码、分层、逐格理由码。

    校验的重点不是形状，而是**两张掩码的关系**：纳入的格必须可观测，
    可交易的格必须可观测。反过来不成立——未收盘 bar 可观测但不可交易，
    这正是两张表分开的理由。
    """

    times: np.ndarray
    symbols: tuple[str, ...]
    observable_mask: np.ndarray
    tradable_mask: np.ndarray
    tiers: np.ndarray
    reason_codes: np.ndarray
    axis_resolved: Mapping[str, Any]
    reason_code_legend: Mapping[str, int] = field(
        default_factory=lambda: dict(REASON_CODES),
    )

    def __post_init__(self) -> None:
        times = as_times(self.times)
        symbols = tuple(str(s) for s in self.symbols)
        shape = (times.size, len(symbols))

        def _mask(name: str, values: Any) -> np.ndarray:
            arr = np.asarray(values)
            if arr.shape != shape:
                raise FlowError(
                    E3003, f"{name} 形状 {arr.shape} 与时间轴 {shape} 不符",
                    path=f"scope.{name}",
                )
            if arr.dtype != bool:
                raise FlowError(
                    E3003, f"{name} 必须是布尔数组，得到 {arr.dtype}", path=f"scope.{name}",
                )
            out = arr.copy()
            out.setflags(write=False)
            return out

        observable = _mask("observable_mask", self.observable_mask)
        tradable = _mask("tradable_mask", self.tradable_mask)

        raw_codes = np.asarray(self.reason_codes)
        if raw_codes.shape != shape or raw_codes.dtype.kind not in "iu":
            raise FlowError(
                E3003, "reason_codes 必须是同形状整数数组", path="scope.reason_codes",
            )
        legend = {str(k): int(v) for k, v in dict(self.reason_code_legend).items()}
        allowed = set(legend.values())
        # **先验值域再窄化**：`astype("int8")` 会把 256 变成 0，越界的码值摇身一变
        # 成了合法码，`tradable ⊆ observable` 当场失效且无人报错。
        unknown = sorted(set(np.unique(raw_codes).tolist()) - allowed)
        if unknown:
            raise FlowError(
                E3003, f"reason_codes 含未登记的码 {unknown}", path="scope.reason_codes",
                hint=f"已登记: {sorted(allowed)}",
            )
        codes = raw_codes.astype("int8", copy=True)

        raw_tiers = np.asarray(self.tiers)
        if raw_tiers.shape != shape or raw_tiers.dtype.kind not in "iu":
            raise FlowError(E3003, "tiers 必须是同形状整数数组", path="scope.tiers")
        if raw_tiers.size:
            lo, hi = int(raw_tiers.min()), int(raw_tiers.max())
            if lo < -1:
                raise FlowError(E3003, "tiers 只能是 -1 或非负档位", path="scope.tiers")
            if hi > 127:
                # 与 reason_codes 同理：先验再窄化，否则 256 静默变成 0 档
                raise FlowError(
                    E3003, f"tiers 档位 {hi} 超出 int8 表示范围", path="scope.tiers",
                )
        tiers = raw_tiers.astype("int8", copy=True)

        # 关系不变量：可交易 ⊆ 可观测；纳入的格必须可观测。
        if np.any(tradable & ~observable):
            raise FlowError(
                E3003, "tradable_mask 不是 observable_mask 的子集",
                path="scope.tradable_mask",
                hint="不可观测却能下单：持仓表与成本表会各算一套",
            )
        bad_obs = np.isin(codes, list(_UNOBSERVABLE)) & observable
        if np.any(bad_obs):
            raise FlowError(
                E3003,
                f"有 {int(bad_obs.sum())} 个格被标为不可观测的理由码却进了 observable_mask",
                path="scope.observable_mask",
            )
        bad_trd = np.isin(codes, list(_NOT_TRADABLE)) & tradable
        if np.any(bad_trd):
            raise FlowError(
                E3003,
                f"有 {int(bad_trd.sum())} 个格被标为不可交易的理由码却进了 tradable_mask",
                path="scope.tradable_mask",
            )

        object.__setattr__(self, "times", times)
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "observable_mask", observable)
        object.__setattr__(self, "tradable_mask", tradable)
        object.__setattr__(self, "tiers", tiers)
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "reason_code_legend", legend)
        object.__setattr__(self, "axis_resolved", dict(self.axis_resolved))

    @property
    def shape(self) -> tuple[int, int]:
        return (self.times.size, len(self.symbols))

    def digest(self) -> str:
        h = hashlib.sha256()
        h.update(b"v5-scope\x00")
        h.update(np.ascontiguousarray(self.times).tobytes())
        h.update(b"\x00obs\x00")
        h.update(np.ascontiguousarray(self.observable_mask).tobytes())
        h.update(b"\x00trd\x00")
        h.update(np.ascontiguousarray(self.tradable_mask).tobytes())
        h.update(b"\x00tiers\x00")
        h.update(np.ascontiguousarray(self.tiers).tobytes())
        h.update(b"\x00codes\x00")
        h.update(np.ascontiguousarray(self.reason_codes).tobytes())
        h.update(b"\x00symbols\x00")
        h.update("\x1f".join(self.symbols).encode("utf-8"))
        # axis_resolved 决定 `align` 切哪一段；两个指纹相同却切出不同面板，
        # 缓存会把 A 段的面板发给 B 段。legend 同理：码值语义变了而指纹不变。
        h.update(b"\x00axis\x00")
        h.update(compute_digest(dict(self.axis_resolved)).encode("utf-8"))
        h.update(b"\x00legend\x00")
        h.update("\x1f".join(f"{k}={v}" for k, v in sorted(self.reason_code_legend.items()))
                 .encode("utf-8"))
        return "sha256:" + h.hexdigest()
