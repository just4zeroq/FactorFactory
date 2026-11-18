"""面板值对象：`(T, N)` 逐字段数组 + UTC 时间索引 + 标的清单。

三条硬约束（s0/04 §3.3 / 06 §4）：

1. **时间索引严格递增且无重复**（`E4007`）。重复时间戳会让「同一时点两次观测」
   变成两次独立样本，标准误被低估；这是最早出现也最难发现的一类伪显著。
2. **缺失用 NaN 表示，不做静默填充**。填充把「缺席」变成「某个值」，截面排序
   随即把它当中间值参与。
3. **`holdout` 区间的取值不得进入面板**。面板是「可看的数据」，保留区一旦混进来，
   后面所有「没看过保留区」的声明都失去依据。

面板是**内存对象**，不是文件引用：能力层不落盘（s0/04 §1 第 6 步），
执行器负责把它作为只读输入传入。`digest()` 是它作为输入时的身份。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from v5.contracts.errors import E1014, E4007, FlowError


def as_times(values: Any) -> np.ndarray:
    arr = np.asarray(values)
    if arr.ndim != 1:
        raise FlowError(E4007, "时间索引必须是一维", path="panel.times")
    if arr.size == 0:
        raise FlowError(E4007, "时间索引为空", path="panel.times")
    if arr.dtype.kind not in "iu":
        raise FlowError(
            E4007, f"时间索引必须是整数纳秒，得到 {arr.dtype}",
            path="panel.times", hint="UTC 纳秒；不要传 datetime 对象",
        )
    if arr.size > 1 and np.any(np.diff(arr) <= 0):
        dup = int(np.sum(np.diff(arr) == 0))
        raise FlowError(
            E4007,
            "时间索引必须严格递增且无重复" + (f"（重复 {dup} 处）" if dup else "（存在倒序）"),
            path="panel.times",
            hint="重复时间戳会让同一时点被当成两个独立观测",
        )
    out = arr.astype("int64", copy=True)
    # 只读：`times` 是面板三条硬约束之一（严格递增无重复），构造后还能改它，
    # 约束就只是构造那一刻的约束。`fields` 早就只读了，同一对象不能两套规则。
    out.setflags(write=False)
    return out


def as_field(name: str, values: Any, shape: tuple[int, int]) -> np.ndarray:
    arr = np.asarray(values)
    if arr.ndim != 2 or arr.shape != shape:
        raise FlowError(
            E4007,
            f"字段 {name!r} 形状 {arr.shape} 与面板 {shape} 不符",
            path=f"panel.fields.{name}",
        )
    if arr.dtype.kind not in "fiu":
        raise FlowError(
            E4007, f"字段 {name!r} 必须是数值型，得到 {arr.dtype}",
            path=f"panel.fields.{name}",
        )
    out = arr.astype("float64", copy=True)
    if np.isinf(out).any():
        # inf 与 NaN 语义不同：inf 一定是算错的（除零、溢出），不能当成缺失
        raise FlowError(
            E4007, f"字段 {name!r} 含 Infinity", path=f"panel.fields.{name}",
            hint="除零必须走 degenerate_policy=nan，不得产生 inf",
        )
    out.setflags(write=False)
    return out


@dataclass(frozen=True)
class Panel:
    """只读面板。构造即校验，任何形状/序错误都不得进入后续工位。"""

    times: np.ndarray
    symbols: tuple[str, ...]
    fields: Mapping[str, np.ndarray]
    holdout: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        times = as_times(self.times)
        symbols = tuple(str(s) for s in self.symbols)
        if not symbols:
            raise FlowError(E4007, "标的清单为空", path="panel.symbols")
        if len(set(symbols)) != len(symbols):
            raise FlowError(E4007, "标的清单有重复", path="panel.symbols")
        shape = (times.size, len(symbols))
        fields: dict[str, np.ndarray] = {}
        for name, values in self.fields.items():
            fields[str(name)] = as_field(str(name), values, shape)
        if not fields:
            raise FlowError(E4007, "面板没有任何字段", path="panel.fields")
        if self.holdout is not None:
            start, end = self.holdout
            if not (0 <= start <= end <= times.size):
                raise FlowError(
                    E4007, f"holdout 区间 {self.holdout} 越界", path="panel.holdout",
                )
        object.__setattr__(self, "times", times)
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "fields", fields)

    # ---- 访问 --------------------------------------------------------
    @property
    def shape(self) -> tuple[int, int]:
        return (self.times.size, len(self.symbols))

    def field(self, name: str) -> np.ndarray:
        if name not in self.fields:
            raise FlowError(
                E1014, f"面板里没有字段 {name!r}", path=f"panel.fields.{name}",
                hint=f"可用字段: {sorted(self.fields)}",
            )
        return self.fields[name]

    def slice_rows(self, start: int, end: int) -> "Panel":
        """左闭右开切片，保持字段与 holdout 的相对关系。

        切片与保留区**部分重叠**时，保留区按交集裁剪而不是整体丢弃：切掉的是
        保留区左侧的可看数据，剩下的重叠段仍然不可用。若直接返回 `None`，
        同一段数据在切片后会被当成可用——「没看过保留区」的声明随即失效。
        """
        if not (0 <= start < end <= self.times.size):
            raise FlowError(
                E4007, f"切片 [{start}, {end}) 越界（共 {self.times.size} 根）",
                path="panel.slice",
            )
        holdout = None
        if self.holdout is not None:
            hs, he = self.holdout
            lo, hi = max(hs, start), min(he, end)
            if lo < hi:
                holdout = (lo - start, hi - start)
        return Panel(
            times=self.times[start:end],
            symbols=self.symbols,
            fields={k: v[start:end] for k, v in self.fields.items()},
            holdout=holdout,
        )

    def holdout_mask(self) -> np.ndarray:
        """逐时点是否落在保留区（True 表示**不可用**）。"""
        mask = np.zeros(self.times.size, dtype=bool)
        if self.holdout is not None:
            mask[self.holdout[0]:self.holdout[1]] = True
        return mask

    # ---- 身份 --------------------------------------------------------
    def digest(self) -> str:
        """内容指纹：时间索引 + 标的序 + 逐字段字节。

        标的顺序进指纹：同一批数据换个列序，截面运算结果可能不同（并列、
        浮点累加顺序），不能视为同一面板。
        """
        h = hashlib.sha256()
        h.update(b"v5-panel\x00")
        h.update(np.ascontiguousarray(self.times).tobytes())
        for name in sorted(self.fields):
            h.update(b"\x00field\x00")
            h.update(name.encode("utf-8"))
            h.update(np.ascontiguousarray(self.fields[name]).tobytes())
        h.update(b"\x00symbols\x00")
        h.update("\x1f".join(self.symbols).encode("utf-8"))
        # holdout 决定 `valid_bar_mask` 与有效样本数，属于「面板能怎么用」的一部分。
        # 不进指纹，一份「未标保留区」的面板就能顶替「已标保留区」的那份。
        h.update(b"\x00holdout\x00")
        h.update(b"none" if self.holdout is None
                 else f"{self.holdout[0]}:{self.holdout[1]}".encode("ascii"))
        return "sha256:" + h.hexdigest()


def array_digest(values: np.ndarray) -> str:
    """数组身份：形状 + dtype + 连续字节。

    隐藏的一点：`-0.0` 与 `0.0` 字节不同，但数值相同。这里按字节算，因为
    指纹的用途是「同一次计算 vs 另一次计算」，不是「数值等价」——把 -0.0
    归一化会让两次不同的浮点路径得到同一个指纹，缓存随即掩盖差异。
    """
    arr = np.ascontiguousarray(np.asarray(values))
    h = hashlib.sha256()
    h.update(b"v5-array\x00")
    h.update(str(arr.shape).encode("ascii"))
    h.update(str(arr.dtype).encode("ascii"))
    h.update(arr.tobytes())
    return "sha256:" + h.hexdigest()
