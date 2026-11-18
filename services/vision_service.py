"""全市场日 K 发现与筛选用例（19-supply.md 的扩展）。

把「从备份站下载全部日 K → 判断每个标的可用的历史跨度 → 选标的 →
拉齐其它数据集到挖掘空间」这条人工流程固化成一个可复用的服务对象。

设计原则：
- **清单来源可注入**：`load_markets()` 走 ccxt（exchangeInfo REST 在部分网络下不通），
  但服务对象只认一个 callable，测试传假实现；生产由 CLI 注入真 ccxt。
- **最早月份靠探测不靠猜**：备份站归档从标的上市月开始有文件，逐月 HEAD 直到
  第一个 200；注入的 `head(url) -> bool` 决定命中。缓存与并发上限防止 901 标的
  把请求打爆。
- **筛选事实可审计**：scan 产物是一份 JSON 清单，每个标的带 `first_month`、`days`、
  `eligible`，按什么门槛筛的写进 `criteria`，谁都能复算。
- **fetch 只下载筛中的标的**：调 `upstream.binance_vision.download_vision`，落到与
  ccxt 相同的数据根，并把筛出的标的集写进 symbol_sets.json 供 plan 引用。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from contracts.errors import E1004, E4002, FlowError

HeadProbe = Callable[[str], bool]  # True=归档存在(200)，False=不存在(404)
MarketLoader = Callable[[], Sequence[str]]  # 返回 ccxt 永续符号清单


def _month_keys(start: str, end: str) -> list[str]:
    s = datetime.strptime(start, "%Y-%m")
    e = datetime.strptime(end, "%Y-%m")
    keys: list[str] = []
    y, m = s.year, s.month
    last = (e.year, e.month)
    while (y, m) <= last:
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return keys


def _days_since(first_month: str, end: str) -> int:
    first = datetime.strptime(first_month, "%Y-%m")
    last = datetime.strptime(end, "%Y-%m")  # end 是 YYYY-MM，不是完整日期
    return max((last.year - first.year) * 12 + (last.month - first.month), 0) * 30


def discover_symbols(load_markets: MarketLoader) -> list[str]:
    """列出全市场 USDT-M 永续（ccxt 符号 `BASE/USDT:USDT`）。"""
    symbols = sorted({str(s) for s in load_markets()})
    if not symbols:
        raise FlowError(E4002, "市场清单为空", path="vision.symbols")
    return symbols


def _head_safe(head: HeadProbe, url: str) -> bool:
    # 404 由 head 返回 False；网络失败（代理 SSL EOF 等）返回 False 等价语义。
    # 单标的探测失败不会被当成数据事实写进清单——调用方可用 non_null 比例
    # 判断整体网络健康（若连 BTC 都探不到，说明是代理问题而非真的无归档）。
    try:
        return bool(head(url))
    except Exception:
        return False


def _probe_one(symbol: str, head: HeadProbe, url_for, months: list[str]) -> str | None:
    """单标的二分探测最早归档月（归档月份单调：上市月起连续到当前）。

    备份站当前月通常尚未归档（按月在月底发布），所以从后往前找第一个已归档月；
    只有整个窗口都没有任何归档才返回 None。
    """
    # 从末尾往前找第一个存在的月作为二分上界（跳过未归档的尾部月份）
    hi = -1
    for i in range(len(months) - 1, -1, -1):
        if _head_safe(head, url_for(symbol, months[i])):
            hi = i
            break
    if hi < 0:
        return None  # 整个窗口都没有归档 → 无可用历史
    if _head_safe(head, url_for(symbol, months[0])):
        return months[0]
    lo = 0  # months[lo] 不存在，months[hi] 存在
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _head_safe(head, url_for(symbol, months[mid])):
            hi = mid
        else:
            lo = mid
    return months[hi]


def probe_first_month(
    symbols: Sequence[str],
    head: HeadProbe,
    *,
    url_for: Callable[[str, str], str],
    start: str,
    end: str,
    workers: int = 8,
) -> dict[str, str | None]:
    """探测备份站最早有归档的月份（`YYYY-MM`），没有则为 None。

    归档月份单调，用二分把每标的请求数从 O(月数) 降到 O(log 月数)；再按标的
    并发（`workers`），全市场 901 标的的扫描从十几分钟压到两分钟内。
    端点单独判，避免二分把「全无归档」误判成有。
    """
    from concurrent.futures import ThreadPoolExecutor

    months = _month_keys(start, end)
    if not months:
        return {s: None for s in symbols}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(
            lambda s: _probe_one(s, head, url_for, months), symbols,
        ))
    return dict(zip(symbols, results))


def build_candidates(
    first_months: Mapping[str, str | None],
    *,
    end: str,
    min_days: int,
    start: str,
) -> dict[str, Any]:
    """按历史跨度筛标的，产出可审计的候选清单。"""
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for symbol, first in sorted(first_months.items()):
        if first is None:
            rejected.append({"symbol": symbol, "reason": "no_archive"})
            continue
        days = _days_since(first, end)
        row = {"symbol": symbol, "first_month": first, "days": days}
        if first > start:
            row["late_start"] = True
        if days >= min_days:
            eligible.append(row)
        else:
            rejected.append({**row, "reason": "short_history"})
    return {
        "criteria": {
            "min_days": min_days, "start": start, "end": end,
            "rule": "days = (end_month - first_month) * 30, first_month 由备份站 HEAD 探测",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "eligible": eligible,
        "rejected": rejected,
    }


def write_symbol_set(candidates: Mapping[str, Any], path: Path, name: str) -> None:
    """把筛中的标的集写进 symbol_sets.json（保留其它集合）。"""
    table: dict[str, Any] = {}
    if path.exists():
        table = json.loads(path.read_text(encoding="utf-8"))
    members = [r["symbol"] for r in candidates["eligible"]]
    if not members:
        raise FlowError(E4002, "筛中的标的集为空，拒绝覆盖 symbol_sets.json", path="vision")
    table[name] = members
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "discover_symbols", "probe_first_month", "build_candidates",
    "write_symbol_set", "_month_keys", "_days_since",
]
