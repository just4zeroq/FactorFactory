"""时间区间的**唯一**展开实现：`<YYYY-MM-DD>..<YYYY-MM-DD>` → 纳秒边界 / 行区间。

为什么必须有这么一个模块：同一个区间字面量在两处被解释——

- `boundary.time_axis`（`mine` / `oos` / `holdout`）：解析成**半开行区间** `[lo, hi)`；
- `read.window`（`{start, end}`，闭区间纳秒）：执行器从 `time_axis` 派生它，决定读哪几行。

两处各写一份，「闭区间的日」到底含不含末尾那一天、时区怎么归一、早于首根/晚于
末根怎么截断，就有 2×3 种组合，而其中只有一种是自洽的。更糟的是不一致时**没有
任何检查点会报错**：`boundary` 按 03-31 截断、`read` 按 03-30 截断，结果只是
「样本外少了最后一天」，谁也看不出来。

所以本模块提供两件事，两处都必须用它：

| 函数 | 返回 | 谁用 |
|---|---|---|
| `day_bounds(text)` | `(lo_ns, hi_ns)`，`hi_ns` 是**末日最后一纳秒**（闭区间） | `read.window` 的构造方 |
| `resolve_interval(text, times)` | `(lo_row, hi_row)`，半开行区间 | `boundary` / `label` / `align` |

`resolve_interval` 内部就是 `day_bounds` + 两次 `searchsorted`，不存在第二种算法。

本模块只依赖 numpy 与错误类型：`v5/capability/ops/` 的 AC31 静态扫描会拒绝
`pyarrow` / `pathlib` / `os` / `io` / `duckdb`，这里也不引入，以免有人拿
「反正 timeaxis 不在 ops 下」当缺口。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from v5.contracts.errors import E4002, FlowError

DAY_NS = 86_400_000_000_000
# 加密市场 7×24 无休市，日历年长度是**定义性常量**（G8）：它进 `bars_per_year`
# 与年化公式，开放成参数就等于允许同一份 Sharpe 有两个不同的年化口径。
# 所以集中在这里一处定义，四处 import，**不**做成可配置旋钮。
# 每年多少根 bar 由数据频率决定（1d=365、1h=8760），不是这个常量。
CALENDAR_DAYS_PER_YEAR = 365
# 可接受的纳秒范围（1900–2100）：毫秒/微秒/秒级时间戳会落在区间之外，
# 从而在「窗口到不了任何观测」这种间接症状之前就被认出来。
NS_MIN = -2_208_988_800_000_000_000
NS_MAX = 4_102_444_800_000_000_000

Interval = tuple[int, int]


def check_ns(ns: int, path: str) -> int:
    """纳秒时间戳的 sanity check。"""
    if ns < NS_MIN or ns > NS_MAX:
        raise FlowError(
            E4002,
            f"{path}={ns} 不在 1900–2100 的纳秒区间内",
            path=path,
            hint="时间戳必须是纳秒；毫秒/微秒/秒会落在这个区间之外",
        )
    return ns


def parse_day(text: Any, *, path: str) -> int:
    """`YYYY-MM-DD` → 当日 00:00:00Z 的纳秒数。"""
    if not isinstance(text, str):
        raise FlowError(
            E4002, f"{path} 必须是 'YYYY-MM-DD' 文本，得到 {type(text).__name__}",
            path=path,
        )
    try:
        ts = np.datetime64(text.strip(), "ns")
    except (ValueError, TypeError):
        raise FlowError(
            E4002, f"{path} 的日期 {text!r} 不是 YYYY-MM-DD", path=path,
        ) from None
    if np.isnat(ts):
        raise FlowError(E4002, f"{path} 的日期 {text!r} 无法解析", path=path)
    return check_ns(int(ts.astype("int64")), path)


def day_bounds(text: Any, *, path: str) -> Interval:
    """`<起>..<止>` → 闭区间纳秒边界 `(起日 00:00:00Z, 止日 23:59:59.999999999Z)`。

    **止日整天都被包含**。这是最容易写错的一处：`searchsorted(times, stop)`
    不含止日当天的任何 bar，于是 `mine` 的最后一根被悄悄丢给 `oos`，
    而两边都不会报错。
    """
    if not isinstance(text, str) or ".." not in text:
        raise FlowError(
            E4002, f"区间必须是 '<起>..<止>'，得到 {text!r}", path=path,
            hint="日期早于数据首根则取首根、晚于末端则取末端；空区间即失败",
        )
    left, right = text.split("..", 1)
    lo = parse_day(left, path=path)
    hi_day = parse_day(right, path=path)
    hi = hi_day + DAY_NS - 1
    if lo > hi_day:
        raise FlowError(
            E4002, f"区间 {text!r} 的起始日晚于终止日", path=path,
        )
    return lo, hi


def rows_for_bounds(lo: int, hi: int, times: np.ndarray, *, path: str) -> Interval:
    """闭区间纳秒边界 → 半开行区间 `[lo_row, hi_row)`。

    边界外的部分向数据方向截断（早于首根取 0、晚于末根取 `size`），
    因为「研究窗口比数据宽」是正常声明；但**整段都落在数据之外**是 `E4002`，
    不是「跑出空结果」——那说明窗口声明错了，空数组后续会一路静默传播。
    """
    size = int(times.size)
    lo_row = int(np.searchsorted(times, lo, side="left"))
    hi_row = int(np.searchsorted(times, hi, side="right"))
    lo_row = min(max(lo_row, 0), size)
    hi_row = min(max(hi_row, 0), size)
    if lo_row >= hi_row:
        raise FlowError(
            E4002,
            f"区间 [{lo}, {hi}] 在数据上没有落到任何时点",
            path=path,
            hint="空区间不得当作「跑出空结果」——它没有定义研究窗口",
        )
    return lo_row, hi_row


def resolve_interval(text: Any, times: np.ndarray, *, path: str) -> Interval:
    """`<起>..<止>` → 半开行区间。`boundary` / `label` / `align` 的唯一入口。"""
    lo, hi = day_bounds(text, path=path)
    return rows_for_bounds(lo, hi, times, path=path)


def window_for_axis(axis: Mapping[str, Any], *, path: str = "boundary.time_axis",
                    oos_optional: bool = False) -> dict[str, int]:
    """`time_axis` → `read.window`（`{start, end}`，闭区间纳秒）。

    执行器在解析 `read` 的 `supply` 输入时用它派生窗口，使「读几行」与
    「`boundary` 认为哪几行属于研究对象」由同一个函数决定。

    规则：

    - `start` = `mine` 起日 00:00:00Z；`end` = `oos` 止日 23:59:59.999999999Z
      （未声明 `oos` 或为空串时取 `mine` 止日）。两段**必须连续**：`boundary`
      对「mine/oos 之间留洞」是 `E4002`，所以并集永远是一个区间，一次读得完。
    - **绝不为了补尾部标签而越界读到 `holdout`**。末根 bar 的 `forward_return`
      需要下一根价格，若下一根在保留区，那一格的标签就是 `NaN`——缺口应当由
      组合层的 `embargo` 显式声明，不能靠「多读一天」来掩盖。多读一天就是
      泄漏，而且它以「标签更完整」的形态出现，看起来像好事。
    """
    mine = axis.get("mine")
    lo, mine_hi = day_bounds(mine, path=f"{path}.mine")
    oos = axis.get("oos")
    if oos:
        _, hi = day_bounds(oos, path=f"{path}.oos")
        oos_lo, _ = day_bounds(oos, path=f"{path}.oos")
        if oos_lo > mine_hi + 1:
            raise FlowError(
                E4002, "mine 与 oos 之间不得有洞", path=f"{path}.oos",
                hint="跨洞的标签与窗口会按相邻 bar 计算；被跳过的区间必须声明为 holdout",
            )
    else:
        if not oos_optional:
            raise FlowError(
                E4002, f"{path}.oos 未声明", path=f"{path}.oos",
                hint="样本外区间必须显式声明；缺省等于「没有样本外」",
            )
        hi = mine_hi
    return {"start": lo, "end": hi}
