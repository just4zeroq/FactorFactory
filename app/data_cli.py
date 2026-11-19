"""L0 数据基座工具（独立 CLI，不属于 flow 主流程）。

这个工具是可选的：用户可以用它批量建全市场数据基座、生成币池；也可以完全
不用，自己维护 `schemas/symbol_sets.json`。主流程 `flow fetch` / `flow run`
只读现成标的集，不感知本工具的存在。

六个动作（取数统一走 `services.market_data` 三原语，全部支持 `--proxy`）：

```
python -m app.data_cli universe [--probe]      # 1. 交易对元数据（ccxt，含已下架）
python -m app.data_cli market-init --start ... --end ...  # 2. data vision 日归档初始化 L0
python -m app.data_cli market-update           # 3. ccxt 每日增量补数
python -m app.data_cli sync --datasets ohlcv,funding   # 4. 备份站全市场历史落到 L0
python -m app.data_cli pool --name crypto_all  # 5. 由 K 线算出挖掘用币池
python -m app.data_cli l1 --set crypto_all     # 6. 把币池数据装配到 L1
```

取数门面 `market_data` 提供三原语（只取数不落盘，本 CLI 负责落 L0）：

- `fetch_metadata` — 元数据走 ccxt `load_markets`；
- `fetch_history` — 全历史走 data vision 日归档，K 线类支持 `timeframe`；
- `fetch_range` — 指定区间，历史走归档、最近几天用 ccxt 补。

多交易所通过适配器注册表扩展，当前仅注册 binance。L0 落在
`<data_root>/base/`，L1 仍是数据根本身。`upstream.store.dataset_file`
接受任意根，所以两侧共用同一套读写与现算 checksum，不需要新存储层。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.render import FORMATS, render
from contracts.errors import E1004, E4002, FlowError, exit_code_for
from runtime.dotenv import load_dotenv
from runtime.store import resolve_data_root
from services import l0_store as ls
from services import pool_service as ps
from services import universe_service as us
from services import vision_service as vs
from storage.layout import Layout
from upstream import binance_vision as bv
from upstream import store

_DEFAULT_EXCHANGE = "binanceusdm"
_DEFAULT_MARKET = "futures"


# 信封与归档探测在这里内联。不从 app.cli 导入：导入任何 app.cli 子模块都会先
# 执行 app/cli/__init__.py，把 capability → ml4t 整条依赖链拖进来。这个工具
# 的定位就是「用户不装挖掘依赖也能单独用」，所以这条边必须断开。
def _envelope_ok(
    command: str, run_id: str | None, data: dict[str, Any] | None,
    warnings: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True, "command": command, "run_id": run_id,
        "data": data, "warnings": warnings or [], "exit_code": 0,
    }


def _envelope_error(
    command: str, run_id: str | None, errors: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": False, "command": command, "run_id": run_id,
        "data": None, "warnings": [],
        "exit_code": int(exit_code_for(errors[0]["code"])),
        "errors": errors,
    }


def _head_probe(proxy: str | None, timeout: float, retries: int = 3):
    """归档存在性探测：用 GET 读 1 字节，404 视为不存在，网络失败重试。"""
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    def head(url: str) -> bool:
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
                    return False
                last = exc
            except Exception as exc:
                last = exc
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
        raise FlowError(
            E4002, f"归档探测失败（重试 {retries} 次）: {url}", path="l0.head",
            context={"error": f"{type(last).__name__}: {last}" if last else "unknown"},
        )

    return head


# ---------------------------------------------------------------- 发现
# 元数据统一走 services.market_data.fetch_metadata → ccxt load_markets，
# 读 market['info'] 原始 exchangeInfo 字段（含 contractStatus/underlyingType/
# onboardDate），不是 ccxt 顶层归一字段，也不是备份站目录（列不出）。


def _l0_bars(root: Path, symbol: str, timeframe: str) -> tuple[str | None, str | None, int]:
    """L0 里该标的的首末 bar 与行数。读不出来就当没采过。"""
    import pandas as pd

    path = store.dataset_file(
        us.base_root(root), store.BAR_DATASET, _DEFAULT_EXCHANGE, _DEFAULT_MARKET,
        symbol, timeframe,
    )
    if not path.exists():
        return None, None, 0
    try:
        frame = pd.read_parquet(path, columns=[store.TIMESTAMP_COL])
    except Exception:
        return None, None, 0
    if frame.empty:
        return None, None, 0
    stamps = pd.to_datetime(frame[store.TIMESTAMP_COL], utc=True, errors="coerce").dropna()
    if stamps.empty:
        return None, None, 0
    return (
        stamps.min().date().isoformat(),
        stamps.max().date().isoformat(),
        int(len(stamps)),
    )


def _url_for(symbol: str, month: str) -> str:
    return bv.kline_url(bv.vision_symbol(symbol), "1d", month)


def _symbols_for(args: Any, root: Path) -> list[str]:
    explicit = getattr(args, "symbols", None)
    if explicit:
        return [s.strip() for s in str(explicit).split(",")
                if s.strip() and us.is_storable(s.strip())]
    name = getattr(args, "set", None)
    if name:
        path = Layout(root).schemas / "symbol_sets.json"
        if not path.exists():
            raise FlowError(E4002, "symbol_sets.json 不存在", path=str(path),
                            hint="先 python -m app.data_cli pool 生成币池")
        table = json.loads(path.read_text(encoding="utf-8"))
        if name not in table:
            raise FlowError(
                E4002, f"标的集 {name} 不存在", path="set",
                hint=f"已有: {', '.join(sorted(table))}",
            )
        return [str(s) for s in table[name] if us.is_storable(str(s))]
    records = us.load_universe(root)
    if not records:
        raise FlowError(E4002, "L0 元数据表为空", path="l0.universe",
                        hint="先 python -m app.data_cli universe")
    return us.active_symbols(records)


# ---------------------------------------------------------------- 命令
def _drop_stable(
    records: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    """剔除稳定币交易对（``asset_class == stable``），返回 (保留表, 剔除数)。

    稳定币 base 价格锚定，在动量/波动类因子上是噪声源；与 ``pool`` 命令的
    ``--allow-stable`` 用同一份 ``asset_class`` 判定，口径一致。
    """
    kept = {s: r for s, r in records.items() if r.get("asset_class") != "stable"}
    return kept, len(records) - len(kept)


def _cmd_universe(args: Any, root: Path) -> dict[str, Any]:
    """通过 ccxt 刷新交易对元数据（含已下架），合成 universe 表。"""
    from services import market_data as md

    proxy = getattr(args, "proxy", None)
    timeframe = str(getattr(args, "timeframe", "1d") or "1d")
    market_key = str(getattr(args, "exchange_market", "um") or "um")
    refresh = bool(getattr(args, "refresh", False))

    # exchange_market 的 um/cm/spot → market_data 的 futures/spot。
    # cm（币本位）当前不在适配器注册表，明确报错而不是静默当 um。
    if market_key == "cm":
        raise FlowError(
            E1004, "币本位 cm 暂未接入", path="exchange_market",
            hint="当前支持 um（U 本位永续）与 spot",
        )
    market = "spot" if market_key == "spot" else "futures"
    result = md.fetch_metadata(
        "binance", market, root=root, proxy=proxy,
        allow_cache=not refresh,
    )
    discovered, stats, source = result.records, dict(result.stats), result.source
    warnings: list[str] = []
    if stats.get("warning"):
        warnings.append(str(stats["warning"]))
    if stats.get("unsafe_name"):
        warnings.append(f"{stats['unsafe_name']} 个标的含非法文件名字符，已跳过")

    observed_at = datetime.now(timezone.utc).isoformat()
    symbols = sorted(discovered)

    # 归档月探测：全市场约 38 分钟（8 并发），默认关闭，显式 --probe 才做。
    probed = 0
    if bool(getattr(args, "probe", False)):
        start = str(getattr(args, "scan_start", "2019-01") or "2019-01")
        end = str(getattr(args, "scan_end", "") or "") \
            or datetime.now(timezone.utc).strftime("%Y-%m")
        head = _head_probe(proxy, float(getattr(args, "timeout", 20) or 20))
        first_months = vs.probe_first_month(
            symbols, head, url_for=_url_for, start=start, end=end,
            workers=int(getattr(args, "workers", 8) or 8),
        )
        probed = sum(1 for v in first_months.values() if v)
        for symbol, month in first_months.items():
            if month:
                discovered[symbol]["first_archive_month"] = month

    # 数据侧观测：L0 已采到的首末 bar 补进记录。
    for symbol, rec in discovered.items():
        first, last, bars = _l0_bars(root, symbol, timeframe)
        if bars:
            discovered[symbol] = us.attach_bars(rec, first_bar=first, last_bar=last, bars=bars)

    previous = us.load_universe(root)
    merged = us.merge_universe(
        previous, discovered, observed_at=observed_at,
        delist_gap_days=int(getattr(args, "delist_gap_days", 7) or 7),
    )

    # 可选：过滤稳定币交易对。默认保留全量元数据；显式 --exclude-stable 才剔除。
    # 放在 merge 之后：稳定币判定用归一化后的 asset_class，与 pool 口径一致。
    excluded_stable = 0
    if bool(getattr(args, "exclude_stable", False)):
        merged, excluded_stable = _drop_stable(merged)

    path = us.write_universe(root, merged)

    newly_delisted = sorted(
        s for s, r in merged.items()
        if r.get("status") == us.STATUS_DELISTED
        and previous.get(s, {}).get("status") != us.STATUS_DELISTED
    )
    return _envelope_ok("l0.universe", None, {
        "path": str(path),
        "source": source,
        "exchange_market": market_key,
        "api_stats": {k: v for k, v in stats.items() if k != "warning"},
        "discovered": len(discovered),
        "probed": probed,
        "total": len(merged),
        "excluded_stable": excluded_stable,
        "summary": us.summarize(merged),
        "newly_delisted": newly_delisted[:20],
        "newly_delisted_count": len(newly_delisted),
        "base_root": str(us.base_root(root)),
    }, warnings=warnings)


# ---------------------------------------------------------------- 行情：初始化 / 增量
def _cmd_market_init(args: Any, root: Path) -> dict[str, Any]:
    """用 data.binance.vision 日归档初始化/补齐 L0 历史行情。

    日归档路径 ``data/<market>/daily/<dataset>/...``，逐日下载、sha256 校验、
    原样落 parquet（保留 count / taker_buy / notional 等源列）。
    """
    from datetime import date as _date

    from upstream import vision_daily as vd

    market = str(getattr(args, "market", "futures") or "futures")
    datasets = vd.validate_datasets(
        str(getattr(args, "datasets", "klines") or "klines").split(","), market,
    )
    symbols = _symbols_for(args, root)
    limit = int(getattr(args, "limit", 0) or 0)
    if limit > 0:
        symbols = symbols[:limit]

    start_raw = getattr(args, "start", None)
    end_raw = getattr(args, "end", None)
    if not start_raw or not end_raw:
        raise FlowError(
            E1004, "日归档必须给 --start 和 --end", path="range",
            hint="用 YYYY-MM-DD；日归档按天定位，不接受缺省区间",
        )
    try:
        start = _date.fromisoformat(str(start_raw)[:10])
        end = _date.fromisoformat(str(end_raw)[:10])
    except ValueError as exc:
        raise FlowError(E1004, f"日期格式错误: {start_raw} / {end_raw}", path="range") from exc
    days = vd.day_range(start, end)
    timeframe = str(getattr(args, "timeframe", "1d") or "1d")
    proxy = getattr(args, "proxy", None)

    from services import market_data as md

    def sink(dataset: str, symbol: str, day: str, frame: Any) -> None:
        """下载器回调：把单日帧并入 L0 parquet。"""
        stats = ls.append_frame(
            root, dataset, symbol, frame,
            exchange=_DEFAULT_EXCHANGE, market=market,
            revise=bool(getattr(args, "revise", False)),
        )
        downloaded.append((dataset, symbol, day, stats["added"]))

    downloaded: list[tuple[str, str, str, int]] = []
    fetched: list[Any] = []
    for dataset in datasets:
        for symbol in symbols:
            res = md.fetch_history(
                symbol, dataset, start, end,
                exchange="binance", market=market, timeframe=timeframe,
                proxy=proxy,
                timeout=float(getattr(args, "timeout", 60) or 60),
                retries=int(getattr(args, "retries", 6) or 6),
                workers=int(getattr(args, "workers", 8) or 8),
                sink=sink,
            )
            fetched.append(res)
    warnings: list[str] = []
    # 聚合逐标的告警，避免全市场时刷屏：同类问题只报一次并带标的数。
    unverified = {r.dataset for r in fetched if not vd.DAILY_DATASETS[r.dataset].verified}
    for dataset in sorted(unverified):
        warnings.append(
            f"{dataset}: 归档路径未经实测确认（{vd.DAILY_DATASETS[dataset].note}）"
        )
    missing_syms = sum(1 for r in fetched if r.missing)
    error_syms = sum(1 for r in fetched if r.errors)
    if missing_syms:
        warnings.append(f"{missing_syms} 个标的存在无归档日（未上市/未发布）")
    if error_syms:
        warnings.append(f"{error_syms} 个标的存在网络失败日，重跑同命令可补齐")

    # 兼容旧回执的 datasets 汇总形状：按数据集聚合行数/天数/缺失/错误。
    by_ds: dict[str, dict[str, int]] = {}
    for r in fetched:
        agg = by_ds.setdefault(r.dataset, {"days": 0, "rows": 0, "missing": 0, "errors": 0})
        agg["days"] += r.days
        agg["rows"] += r.rows
        agg["missing"] += r.missing
        agg["errors"] += r.errors
    return _envelope_ok("l0.market.init", None, {
        "source": "data.binance.vision",
        "layout": "daily",
        "gate": "market_data.fetch_history",
        "base_root": str(us.base_root(root)),
        "datasets": [
            {"dataset": d, **v} for d, v in by_ds.items()
        ],
        "symbols": len(symbols),
        "days": len(days),
        "coverage": [
            ls.coverage(root, d, symbols, market=market) for d in datasets
        ],
    }, warnings=warnings)


def _cmd_market_update(args: Any, root: Path) -> dict[str, Any]:
    """用 ccxt 做每日增量补数，写入与初始化同一份 L0 parquet。"""
    from datetime import date as _date, timedelta as _td

    from services import market_data as md

    datasets = [
        d.strip() for d in str(getattr(args, "datasets", "klines") or "klines").split(",")
        if d.strip()
    ]
    symbols = _symbols_for(args, root)
    limit_n = int(getattr(args, "limit", 0) or 0)
    if limit_n > 0:
        symbols = symbols[:limit_n]
    market = str(getattr(args, "market", "futures") or "futures")
    timeframe = str(getattr(args, "timeframe", "1d") or "1d")
    proxy = getattr(args, "proxy", None)
    api_limit = int(getattr(args, "api_limit", 500) or 500)
    since_days = int(getattr(args, "since_days", 3) or 3)
    today = datetime.now(timezone.utc).date()
    start = today - _td(days=since_days)

    client = md.get_adapter("binance").make_client(market, proxy=proxy)
    results: list[dict[str, Any]] = []
    for dataset in datasets:
        added = rows = 0
        failed: list[str] = []
        for symbol in symbols:
            try:
                res = md.fetch_range(
                    symbol, dataset, start, today,
                    exchange="binance", market=market, timeframe=timeframe,
                    proxy=proxy, limit=api_limit,
                    use_vision=False, client=client,
                )
                if res.frame.empty:
                    continue
                stats = ls.append_frame(
                    root, dataset, symbol, res.frame,
                    exchange=_DEFAULT_EXCHANGE, market=market, revise=True,
                )
                rows += res.rows
                added += int(stats.get("added", 0))
            except Exception as exc:
                failed.append(f"{symbol}: {type(exc).__name__}: {exc}")
        results.append({
            "dataset": dataset,
            "symbols": len(symbols) - len(failed),
            "rows_fetched": rows,
            "rows_added": added,
            "failed": len(failed),
            "errors": failed[:10],
        })
    return _envelope_ok("l0.market.update", None, {
        "source": "ccxt",
        "gate": "market_data.fetch_range",
        "base_root": str(us.base_root(root)),
        "symbols": len(symbols),
        "datasets": results,
    })


def _cmd_sync(args: Any, root: Path) -> dict[str, Any]:
    datasets = [s.strip() for s in str(getattr(args, "datasets", "ohlcv,funding")).split(",")
                if s.strip()]
    unsupported = [d for d in datasets if d not in ("ohlcv", "funding")]
    if unsupported:
        raise FlowError(
            E1004, f"备份站不支持 {unsupported}", path="datasets",
            hint="备份站只有 ohlcv/funding 归档；open_interest/basis 只能用 ccxt 增量补",
        )

    symbols = _symbols_for(args, root)
    limit = int(getattr(args, "limit", 0) or 0)
    if limit > 0:
        symbols = symbols[:limit]

    timeframe = str(getattr(args, "timeframe", "1d") or "1d")
    start = getattr(args, "start", None)
    start_dt = _parse_range(start) if start else _earliest_start(us.load_universe(root), symbols)
    end_dt = _parse_range(getattr(args, "end")) if getattr(args, "end") \
        else datetime.now(timezone.utc)

    reports = bv.download_vision(
        us.base_root(root), datasets, symbols=symbols,
        exchange=_DEFAULT_EXCHANGE, market=_DEFAULT_MARKET,
        timeframe=timeframe, start=start_dt, end=end_dt,
        proxy=getattr(args, "proxy", None),
        workers=int(getattr(args, "workers", 8) or 8),
        revise=bool(getattr(args, "revise", False)),
    )

    # 采完立刻回填元数据：否则下次 pool 用的是采数前的 last_bar。
    observed_at = datetime.now(timezone.utc).isoformat()
    records = us.load_universe(root)
    touched = 0
    for symbol in symbols:
        if symbol not in records:
            continue
        first, last, bars = _l0_bars(root, symbol, timeframe)
        if bars:
            records[symbol] = us.attach_bars(
                records[symbol], first_bar=first, last_bar=last, bars=bars,
            )
            touched += 1
    if touched:
        records = us.merge_universe(
            records, {s: records[s] for s in symbols if s in records},
            observed_at=observed_at,
        )
        us.write_universe(root, records)

    payload = [
        {
            "dataset": r.dataset, "symbols": r.symbols,
            "rows_added": r.rows_added, "revised": r.revised,
            "skipped": len(r.skipped),
        }
        for r in reports
    ]
    zero = [r for r in reports if r.symbols == 0]
    warnings: list[str] = []
    if zero and len(zero) == len(reports):
        raise FlowError(
            E4002, "所有数据集零成功", path="l0.sync",
            hint="检查区间是否早于标的归档月，或 --proxy 是否可用",
        )
    if zero:
        warnings.append(f"{len(zero)} 个数据集零成功：{[r.dataset for r in zero]}")
    return _envelope_ok("l0.sync", None, {
        "base_root": str(us.base_root(root)),
        "timeframe": timeframe,
        "symbols": len(symbols),
        "datasets": payload,
        "universe_touched": touched,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
    }, warnings=warnings)


def _cmd_pool(args: Any, root: Path) -> dict[str, Any]:
    records = us.load_universe(root)
    if not records:
        raise FlowError(E4002, "L0 元数据表为空", path="l0.universe",
                        hint="先 python -m app.data_cli universe")

    asset_classes = getattr(args, "asset_classes", None)
    classes = tuple(
        c.strip() for c in str(asset_classes).split(",") if c.strip()
    ) if asset_classes else ("crypto",)

    criteria = ps.PoolCriteria(
        min_days=int(getattr(args, "min_days", 365) or 0),
        min_adv=float(getattr(args, "min_adv", 1_000_000.0) or 0.0),
        adv_window=int(getattr(args, "adv_window", 90) or 90),
        max_last_bar_age_days=int(getattr(args, "max_last_bar_age_days", 7) or 7),
        min_coverage=float(getattr(args, "min_coverage", 0.90) or 0.0),
        allow_asset_classes=classes,
        exclude_stable=not bool(getattr(args, "allow_stable", False)),
    )

    timeframe = str(getattr(args, "timeframe", "1d") or "1d")
    base = us.base_root(root)
    metrics: dict[str, dict[str, Any]] = {}
    for symbol in records:
        metrics[symbol] = ps.read_metrics(
            base, symbol, exchange=_DEFAULT_EXCHANGE, market=_DEFAULT_MARKET,
            timeframe=timeframe, adv_window=criteria.adv_window,
        )

    pool = ps.build_pool(records, metrics, criteria)
    name = str(getattr(args, "name", "crypto_all") or "crypto_all")
    paths = ps.write_pool(root, pool, name)

    return _envelope_ok("l0.pool", None, {
        "name": name,
        "audit": str(paths["audit"]),
        "symbol_sets": str(paths["symbol_sets"]),
        "criteria": pool["criteria"],
        "summary": pool["summary"],
        "sample": [r["symbol"] for r in pool["eligible"][:15]],
    })


def _cmd_l1(args: Any, root: Path) -> dict[str, Any]:
    symbols = _symbols_for(args, root)
    datasets = [s.strip() for s in str(getattr(args, "datasets", "ohlcv,funding")).split(",")
                if s.strip()]
    timeframe = str(getattr(args, "timeframe", "1d") or "1d")
    use_link = bool(getattr(args, "link", False))
    revise = bool(getattr(args, "revise", False))

    base = us.base_root(root)
    copied = linked = skipped = 0
    per_dataset: dict[str, int] = {}
    for dataset in datasets:
        tf = timeframe if dataset == store.BAR_DATASET else None
        for symbol in symbols:
            src = store.dataset_file(base, dataset, _DEFAULT_EXCHANGE, _DEFAULT_MARKET, symbol, tf)
            if not src.exists():
                skipped += 1
                continue
            dst = store.dataset_file(root, dataset, _DEFAULT_EXCHANGE, _DEFAULT_MARKET, symbol, tf)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and not revise:
                skipped += 1
                continue
            if dst.exists():
                dst.unlink()
            if use_link:
                try:
                    dst.hardlink_to(src)
                    linked += 1
                except OSError:
                    shutil.copy2(src, dst)
                    copied += 1
            else:
                shutil.copy2(src, dst)
                copied += 1
            per_dataset[dataset] = per_dataset.get(dataset, 0) + 1

    if copied + linked == 0:
        raise FlowError(
            E4002, "L1 装配零成功", path="l0.l1",
            hint=f"L0 里没有这些标的的数据，先 python -m app.data_cli sync（base={base}）",
        )
    return _envelope_ok("l0.l1", None, {
        "symbols": len(symbols),
        "copied": copied,
        "linked": linked,
        "skipped": skipped,
        "per_dataset": per_dataset,
        "mode": "hardlink" if use_link else "copy",
        "target_root": str(root),
    })


def _parse_range(text: Any) -> datetime:
    raw = str(text).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FlowError(
            E1004, f"无法解析时间: {raw!r}", path="range",
            hint="用 RFC3339 或 YYYY-MM-DD（按 UTC 解释）",
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _earliest_start(records: Mapping[str, Mapping[str, Any]], symbols: Sequence[str]) -> datetime:
    """下载起点取选中标的里最早的归档月，避免对空窗逐月发请求。"""
    months = [
        str(records[s].get("first_archive_month") or "")
        for s in symbols if s in records
    ]
    months = [m for m in months if len(m) >= 7]
    if not months:
        return datetime(2019, 1, 1, tzinfo=timezone.utc)
    first = min(months)[:7]
    try:
        return datetime.strptime(first, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(2019, 1, 1, tzinfo=timezone.utc)


def cmd_l0(args: Any) -> dict[str, Any]:
    action = str(getattr(args, "l0_action", "") or "").replace("-", "_")
    root = resolve_data_root()
    handlers = {
        "universe": _cmd_universe, "sync": _cmd_sync,
        "pool": _cmd_pool, "l1": _cmd_l1,
        "market_init": _cmd_market_init, "market_update": _cmd_market_update,
    }
    handler = handlers.get(action)
    if handler is None:
        raise FlowError(
            E1004, f"未知动作: {action!r}", path="l0",
            hint="可用: universe / market-init / market-update / sync / pool / l1",
        )
    return handler(args, root)


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="data_cli",
        description="L0 数据基座工具（独立可选，不属于 flow 主流程）",
    )
    sub = p.add_subparsers(dest="l0_action", required=True)

    u = sub.add_parser("universe", help="从交易所接口刷新交易对元数据（含已下架）")
    u.add_argument("--exchange-market", default="um", choices=("um", "cm", "spot"),
                   help="交易所市场：um=U本位永续(默认) / cm=币本位 / spot=现货")
    u.add_argument("--endpoint", help=argparse.SUPPRESS)  # 元数据走 ccxt，端点由适配器决定；保留兼容旧脚本
    u.add_argument("--refresh", action="store_true",
                   help="接口失败时不回退缓存，直接报错（默认回退上次快照并告警）")
    u.add_argument("--exclude-stable", action="store_true",
                   help="过滤稳定币交易对（asset_class=stable，含 um 的 USDC/USTC 永续），默认保留全量")
    u.add_argument("--probe", action="store_true",
                   help="探测每个标的的备份站最早归档月（全市场约 38 分钟）")
    u.add_argument("--scan-start", default="2019-01", help="起始探测月 YYYY-MM")
    u.add_argument("--scan-end", help="结束探测月 YYYY-MM（缺省当前月）")
    u.add_argument("--timeout", type=float, default=20, help="归档探测超时秒数")
    u.add_argument("--workers", type=int, default=8, help="并发探测线程数（默认 8）")
    u.add_argument("--proxy", help="代理（如 http://127.0.0.1:7890）")
    u.add_argument("--timeframe", default="1d", help="回填首末 bar 用的周期")
    u.add_argument("--delist-gap-days", type=int, default=7,
                   help="末根 bar 距今超过此天数判为下架（默认 7）")
    u.add_argument("--format", choices=FORMATS, default="json")

    mi = sub.add_parser("market-init", help="用 data.binance.vision 日归档初始化/补齐 L0 行情")
    mg = mi.add_mutually_exclusive_group()
    mg.add_argument("--symbols", help="逗号分隔的标的清单")
    mg.add_argument("--set", help="L0 universe 中的标的集名（schemas/symbol_sets.json）")
    mi.add_argument("--datasets", default="klines",
                    help="逗号分隔：klines/indexPriceKlines/markPriceKlines/"
                         "premiumIndexKlines/aggTrades/trades/bookDepth/metrics/bookTicker")
    mi.add_argument("--market", default="futures", choices=("futures", "spot"))
    mi.add_argument("--timeframe", default="1d")
    mi.add_argument("--start", required=True, help="起始日 YYYY-MM-DD（含）")
    mi.add_argument("--end", required=True, help="结束日 YYYY-MM-DD（含）")
    mi.add_argument("--limit", type=int, default=0, help="只处理前 N 个标的（分批用）")
    mi.add_argument("--proxy", help="代理（如 http://127.0.0.1:7890）")
    mi.add_argument("--timeout", type=float, default=60)
    mi.add_argument("--retries", type=int, default=6)
    mi.add_argument("--workers", type=int, default=8)
    mi.add_argument("--revise", action="store_true", help="覆盖同时间戳已有行（默认 append-only）")
    mi.add_argument("--format", choices=FORMATS, default="json")

    mu = sub.add_parser("market-update", help="用 ccxt 做每日增量补数（写入同一份 L0）")
    ug = mu.add_mutually_exclusive_group()
    ug.add_argument("--symbols", help="逗号分隔的标的清单")
    ug.add_argument("--set", help="标的集名")
    mu.add_argument("--datasets", default="klines",
                    help="逗号分隔：K线四类/trades/funding/open_interest/"
                         "bookTicker/bookDepth/metrics")
    mu.add_argument("--market", default="futures", choices=("futures", "spot"))
    mu.add_argument("--timeframe", default="1d")
    mu.add_argument("--api-limit", type=int, default=500, help="单次 API 返回条数上限")
    mu.add_argument("--since-days", type=int, default=3, help="从 L0 末端回看天数（默认 3）")
    mu.add_argument("--proxy", help="代理")
    mu.add_argument("--limit", type=int, default=0, help="只处理前 N 个标的")
    mu.add_argument("--format", choices=FORMATS, default="json")

    s = sub.add_parser("sync", help="从备份站把全市场历史落到 L0")
    s.add_argument("--datasets", default="ohlcv,funding",
                   help="逗号分隔（备份站仅 ohlcv/funding）")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--symbols", help="逗号分隔的标的清单")
    g.add_argument("--set", help="symbol_sets.json 中的标的集名")
    s.add_argument("--timeframe", default="1d")
    s.add_argument("--start", help="RFC3339 或 YYYY-MM-DD；缺省取最早的归档月")
    s.add_argument("--end", help="RFC3339 或 YYYY-MM-DD；缺省当前")
    s.add_argument("--limit", type=int, default=0, help="只采前 N 个标的（分批用）")
    s.add_argument("--proxy", help="代理（如 http://127.0.0.1:7890）")
    s.add_argument("--workers", type=int, default=8, help="并发下载线程数（默认 8）")
    s.add_argument("--revise", action="store_true", help="覆盖已写历史")
    s.add_argument("--format", choices=FORMATS, default="json")

    pl = sub.add_parser("pool", help="基于 L0 日 K 算出挖掘用币池")
    pl.add_argument("--name", default="crypto_all", help="写入 symbol_sets.json 的集合名")
    pl.add_argument("--min-days", type=int, default=365, help="上市最少天数（默认 365）")
    pl.add_argument("--min-adv", type=float, default=1_000_000.0,
                    help="成交额中位数下限（默认 1e6）")
    pl.add_argument("--adv-window", type=int, default=90, help="成交额回看期数（默认 90）")
    pl.add_argument("--max-last-bar-age-days", type=int, default=7,
                    help="末根 bar 距今上限（默认 7）")
    pl.add_argument("--min-coverage", type=float, default=0.90,
                    help="覆盖率下限（默认 0.9）")
    pl.add_argument("--asset-classes", default="crypto",
                    help="允许的资产类别，逗号分隔（默认 crypto）")
    pl.add_argument("--allow-stable", action="store_true", help="允许稳定币入池")
    pl.add_argument("--timeframe", default="1d")
    pl.add_argument("--format", choices=FORMATS, default="json")

    l1 = sub.add_parser("l1", help="把币池的数据从 L0 装配到 L1")
    lg = l1.add_mutually_exclusive_group()
    lg.add_argument("--symbols", help="逗号分隔的标的清单")
    lg.add_argument("--set", help="symbol_sets.json 中的标的集名")
    l1.add_argument("--datasets", default="ohlcv,funding")
    l1.add_argument("--timeframe", default="1d")
    l1.add_argument("--link", action="store_true", help="用硬链接代替复制")
    l1.add_argument("--revise", action="store_true", help="覆盖 L1 已存在的文件")
    l1.add_argument("--format", choices=FORMATS, default="json")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    fmt = getattr(args, "format", "json")
    try:
        load_dotenv()
        envelope = cmd_l0(args)
    except FlowError as exc:
        envelope = _envelope_error("l0", None, [exc.error.to_dict()])
    except Exception as exc:
        envelope = _envelope_error("l0", None, [{
            "code": "E3004", "layer": "execution", "retryable": False,
            "message": f"{type(exc).__name__}: {exc}",
        }])
    print(render(envelope, fmt))
    return int(envelope["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "cmd_l0"]
