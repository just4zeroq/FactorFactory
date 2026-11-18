"""flow vision —— 全市场日 K 发现 → 筛选 → 拉齐数据到挖掘空间。

三步工作流（详见 services/vision_service.py）：
```
flow vision list                                  # ccxt 列全市场 USDT-M 永续
flow vision scan --min-days 720                   # 探测每标的备份站最早月份并筛选
flow vision fetch --min-days 720 --datasets ohlcv,funding   # 下载筛中标的的完整历史
```

之所以独立成命令而非塞进 fetch：这条链要「先看全市场、再按历史可用性筛、最后才下载」，
是一个发现+决策过程，和 fetch「给定标的直接采」的语义不同。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.errors import E1004, E4002, FlowError
from runtime.store import resolve_data_root
from storage.layout import Layout
from upstream import binance_vision as bv
from upstream import store
from services import vision_service as vs

from .common import envelope_ok


def _schemas() -> Path:
    return Layout(resolve_data_root()).schemas


def _all_perps_path() -> Path:
    return _schemas() / "all_perps.json"


def _candidates_path() -> Path:
    return _schemas() / "vision_candidates.json"


def _ccxt_loader(proxy: str | None) -> vs.MarketLoader:
    def load() -> list[str]:
        import ccxt

        opts: dict[str, Any] = {"timeout": 25000, "enableRateLimit": True,
                                "options": {"defaultType": "future"}}
        if proxy:
            opts["proxies"] = {"http": proxy, "https": proxy}
            opts["aiohttp_trust_env"] = False
        ex = ccxt.binanceusdm(opts)
        markets = ex.load_markets()
        return [
            s for s, m in markets.items()
            if m.get("swap") and m.get("quote") == "USDT" and m.get("settle") == "USDT"
        ]
    return load


def _head_probe(proxy: str | None, timeout: float, retries: int = 3) -> vs.HeadProbe:
    import time

    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    def head(url: str) -> bool:
        # 用 GET 而非 HEAD：直连备份站时 HEAD 会被连接重置，GET 稳定且快（~0.3s）。
        # 探测只判断存在性，读 1 字节即可（Range 不被 S3 支持时也不过是一个 zip 首部）。
        req = urllib.request.Request(
            url, headers={"User-Agent": "factorfactory/1.0", "Range": "bytes=0-0"},
        )
        last: Exception | None = None
        for attempt in range(retries):
            try:
                with opener.open(req, timeout=timeout) as resp:
                    resp.read(1)
                    return True
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return False  # 确认无归档，不是网络问题
                last = exc
            except Exception as exc:
                last = exc  # 网络抖动：重试，不能当成「没有归档」
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
        raise FlowError(
            E4002, f"归档探测失败（重试 {retries} 次）: {url}", path="vision.head",
            context={"error": f"{type(last).__name__}: {last}" if last else "unknown"},
        )

    return head


def _url_for(symbol: str, month: str) -> str:
    return bv.kline_url(bv.vision_symbol(symbol), "1d", month)


def _load_or_discover(proxy: str | None, refresh: bool) -> list[str]:
    path = _all_perps_path()
    if refresh or not path.exists():
        symbols = vs.discover_symbols(_ccxt_loader(proxy))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(symbols, ensure_ascii=False, indent=1), encoding="utf-8")
        return symbols
    return [str(s) for s in json.loads(path.read_text(encoding="utf-8"))]


def cmd_vision(args: Any) -> dict[str, Any]:
    action = str(getattr(args, "vision_action", "") or "")
    proxy = getattr(args, "proxy", None)
    timeout = float(getattr(args, "timeout", 20) or 20)

    if action == "list":
        symbols = _load_or_discover(proxy, refresh=True)
        return envelope_ok("vision", None, {
            "action": "list", "count": len(symbols),
            "path": str(_all_perps_path()), "sample": symbols[:10],
        })

    if action == "scan":
        symbols = _load_or_discover(proxy, refresh=bool(getattr(args, "refresh", False)))
        start = str(getattr(args, "scan_start", "2019-01") or "2019-01")
        end = str(getattr(args, "scan_end", datetime.now(timezone.utc).strftime("%Y-%m")) or
                  datetime.now(timezone.utc).strftime("%Y-%m"))
        min_days = int(getattr(args, "min_days", 720) or 720)
        head = _head_probe(proxy, timeout)
        first_months = vs.probe_first_month(
            symbols, head, url_for=_url_for, start=start, end=end,
            workers=int(getattr(args, "workers", 8) or 8),
        )
        # 健康锚点：BTC 一定有 2019 后的归档。若连它都探不到，是代理/网络
        # 问题而非「市场无数据」——fail-closed，避免把空清单误当成筛选结论。
        anchor = "BTC/USDT:USDT"
        if anchor in first_months and first_months[anchor] is None:
            raise FlowError(
                E4002, "健康探测失败：连 BTC 都没有探到归档，代理/网络不可用",
                path="vision.head",
                hint="检查 --proxy 与网络；备份站请求被代理重置时扫描会静默漏标的",
            )
        found = sum(1 for v in first_months.values() if v)
        if found < max(5, len(symbols) // 10):
            raise FlowError(
                E4002, f"仅 {found}/{len(symbols)} 个标的探到归档，疑似网络问题",
                path="vision.head", hint="降低 --workers / 增大 --timeout 后重试",
            )
        candidates = vs.build_candidates(
            first_months, end=end, min_days=min_days, start=start,
        )
        path = _candidates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidates, ensure_ascii=False, indent=1), encoding="utf-8")
        set_name = getattr(args, "write_symbol_set", None)
        if set_name:
            vs.write_symbol_set(candidates, _schemas() / "symbol_sets.json", set_name)
        return envelope_ok("vision", None, {
            "action": "scan", "scanned": len(symbols),
            "eligible": len(candidates["eligible"]), "rejected": len(candidates["rejected"]),
            "min_days": min_days, "path": str(path),
            "set_name": set_name,
            "sample": candidates["eligible"][:10],
        })

    if action == "fetch":
        path = _candidates_path()
        if not path.exists():
            raise FlowError(E4002, "没有候选清单，请先 flow vision scan", path="vision")
        candidates = json.loads(path.read_text(encoding="utf-8"))
        min_days = int(getattr(args, "min_days", 720) or 720)
        symbols = [
            r["symbol"] for r in candidates["eligible"] if int(r.get("days", 0)) >= min_days
        ]
        if not symbols:
            raise FlowError(E4002, "没有满足 --min-days 的标的", path="vision.min_days")
        datasets = [s.strip() for s in str(getattr(args, "datasets", "ohlcv,funding") or "").split(",")
                    if s.strip()]
        # 下载起点取筛中标的里最早的 first_month，避免对 2019 空窗逐月发无效请求；
        # download_vision 对尚未归档的月份本来就跳过，所以晚上市标的仍安全。
        firsts = [
            r["first_month"] for r in candidates["eligible"]
            if int(r.get("days", 0)) >= min_days and r.get("first_month")
        ]
        start_month = min(firsts) if firsts else str(candidates["criteria"]["start"])
        start_date = datetime.strptime(start_month, "%Y-%m").replace(tzinfo=timezone.utc)
        end_date = datetime.now(timezone.utc)
        root = resolve_data_root()
        reports = bv.download_vision(
            root, datasets, symbols=symbols,
            exchange=str(getattr(args, "exchange", "binanceusdm")),
            market=str(getattr(args, "market", "futures")),
            timeframe=str(getattr(args, "timeframe", "1d")),
            start=start_date, end=end_date,
            proxy=proxy,
            revise=bool(getattr(args, "revise", False)),
            workers=int(getattr(args, "workers", 8) or 8),
        )
        return envelope_ok("vision", None, {
            "action": "fetch", "symbols": len(symbols),
            "data_root": str(root),
            "datasets": [r.to_dict() for r in reports],
        })

    raise FlowError(E1004, f"未知 vision 动作: {action!r}", path="vision_action",
                    hint="用 list / scan / fetch")


__all__ = ["cmd_vision"]
