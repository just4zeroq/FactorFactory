"""第三来源：从币安公开数据备份站（data.binance.vision）下载月度 zip（19-supply.md §2）。

为什么需要这条来源：
- ccxt 逐个标的翻页拉历史，慢且受交易所 rate limit；备份站是官方按月的静态归档，
  一次一个 zip 就是一整个月，历史深度也更长（ohlcv/funding 可回溯到 2019 年）。
- 它和 ccxt、V4 落到**完全相同**的 parquet 路径与列契约，读取侧无需知道数据来自哪。

命名规则（已对 2020-01 的样本实测确认，见 docs）：
- K 线：`data/futures/um/monthly/klines/<SYMBOL>/<tf>/<SYMBOL>-<tf>-<YYYY-MM>.zip`
  zip 内单个 CSV 无表头，前 6 列 = open_time(ms)/open/high/low/close/volume。
- 资金费：`data/futures/um/monthly/fundingRate/<SYMBOL>/<SYMBOL>-fundingRate-<YYYY-MM>.zip`
  zip 内单个 CSV **有表头**：calc_time / funding_interval_hours / last_funding_rate。
- 完整性：同路径 `<file>.CHECKSUM`，内容为 `<sha256>  <basename>`，下载后必校验。

设计取舍：
- 只做 ohlcv / funding 两个数据集。open_interest 备份站没有月度历史（实测 404，
  交易所只留约 30 天），basis 需要 perp/spot 同日对齐，都不适合这条静态归档路径。
- 标的清单由调用方给（符号是 ccxt 形态 `BTC/USDT:USDT`），此处存数据根用的
  binance 大写字形 `BTCUSDT`（`_vision_symbol`）。反推符号有歧义，不可猜。
- 缺月（该标的尚未上市，或备份站尚未归档）正常跳过并计入 skipped，不视为失败——
  这正是 PIT 动态宇宙要的：晚上市标的在上市前的格子天然缺失。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from contracts.errors import E1004, E1014, E4002, FlowError

from . import store

BASE_URL = "https://data.binance.vision"
# 下载器可注入：测试传假实现，生产走 _http_get（带代理/重试）。
Downloader = Callable[[str], bytes]


def _http_get(url: str, *, timeout: float, retries: int, proxy: str | None) -> bytes:
    """带代理与重试的 GET。404 由调用方按缺月处理，其余异常重试后抛出。"""
    import time
    import urllib.error
    import urllib.request

    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "factorfactory/1.0"})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except Exception as exc:  # 代理 SSL EOF / 超时都可能
            last = exc
        if attempt < retries - 1:
            # 代理的 SSL EOF 常成串出现（连接被重置后短时间内反复失败），
            # 用带抖动的递增退避给对端恢复时间；上限 15s 不至于拖垮整批。
            time.sleep(min(1.5 * (2 ** min(attempt, 3)), 15.0))
    raise FlowError(
        E4002, f"下载失败（重试 {retries} 次）: {url}",
        path="source.binance_vision",
        context={"error": f"{type(last).__name__}: {last}" if last else "unknown"},
    )


def vision_symbol(ccxt_symbol: str) -> str:
    """`BTC/USDT:USDT` → `BTCUSDT`（币安备份站的文件名用 base+quote 大写）。

    ccxt 永续符号是 `BASE/QUOTE:SETTLE`，备份站归档只用 `BASEQUOTE`。
    引号后段 `:USDT` 是结算币种，不进文件名；现货 `BTC/USDT` 同样得到 `BTCUSDT`。
    """
    text = str(ccxt_symbol).strip()
    parts = text.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise FlowError(E1004, f"无法从符号推出币安字形: {ccxt_symbol!r}", path="symbols")
    base = parts[0]
    quote = parts[1].split(":", 1)[0]  # 去掉 :USDT 结算后缀
    if not quote:
        raise FlowError(E1004, f"符号缺报价币: {ccxt_symbol!r}", path="symbols")
    return (base + quote).upper()


def _month_keys(start: datetime, end: datetime) -> list[str]:
    """闭区间内的月份键 `YYYY-MM`。按 UTC 年/月推进，不依赖 day。"""
    if start.tzinfo is None or end.tzinfo is None:
        raise FlowError(E1014, "下载区间必须带时区", path="range")
    if start >= end:
        raise FlowError(E1004, "--start 必须早于 --end", path="range")
    keys: list[str] = []
    year, month = start.year, start.month
    # end 取最后一刻所属月：end=2026-03-01T00:00Z 不应把 03 月整月拉进来，
    # 但 end 落在月中某天则该月要有。用 (year,month) <= end 的 YYYY-MM 比较。
    last = (end.year, end.month)
    while (year, month) <= last:
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return keys


def kline_url(symbol: str, timeframe: str, month: str) -> str:
    stem = f"{symbol}-{timeframe}-{month}"
    return (
        f"{BASE_URL}/data/futures/um/monthly/klines/{symbol}/{timeframe}/{stem}.zip"
    )


def funding_url(symbol: str, month: str) -> str:
    stem = f"{symbol}-fundingRate-{month}"
    return f"{BASE_URL}/data/futures/um/monthly/fundingRate/{symbol}/{stem}.zip"


def _verify_checksum(blob: bytes, url: str, get: Downloader) -> None:
    """下载 `<url>.CHECKSUM` 并比对 sha256；不符即拒绝落盘。"""
    try:
        raw = get(url + ".CHECKSUM")
    except FlowError:
        raise
    except Exception as exc:
        raise FlowError(
            E4002, f"缺校验文件: {url}.CHECKSUM", path="source.binance_vision",
            context={"error": f"{type(exc).__name__}: {exc}"},
        ) from None
    first_line = raw.decode("utf-8", errors="replace").splitlines()[0] if raw else ""
    expected = first_line.strip().split()[0] if first_line.strip() else ""
    if not expected:
        raise FlowError(E4002, f"校验文件为空: {url}.CHECKSUM", path="source.binance_vision")
    actual = hashlib.sha256(blob).hexdigest()
    if actual.lower() != expected.lower():
        raise FlowError(
            E4002, f"sha256 不匹配: {url}", path="source.binance_vision",
            context={"expected": expected, "actual": actual},
        )


def parse_kline(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise FlowError(E4002, "K线 zip 内无 CSV", path="source.binance_vision")
        payload = archive.read(names[0])
    # 备份站格式在 2023 年前后变过：早期无表头，之后首行是
    # `open_time,open,high,...`。用第一字段能否解析成整数时间戳判断，
    # 而不是按年份猜——格式是文件的属性，不依赖我们记住哪年改版。
    first_field = payload.split(b"\n", 1)[0].split(b",", 1)[0].strip()
    has_header = not first_field.isdigit()
    raw = pd.read_csv(io.BytesIO(payload), header=0 if has_header else None)
    if raw.shape[1] < 6:
        raise FlowError(E4002, f"K线 CSV 列数不足: {raw.shape[1]}", path="source.binance_vision")
    out = raw.iloc[:, :6].copy()
    if has_header:
        # 按列名取，避免列序将来变动时错位。
        wanted = ["open_time", "open", "high", "low", "close", "volume"]
        if not all(c in raw.columns for c in wanted):
            raise FlowError(
                E4002, f"K线带表头但列名意外: {list(raw.columns)}", path="source.binance_vision",
            )
        out = raw[wanted].copy()
    out.columns = [store.TIMESTAMP_COL, "open", "high", "low", "close", "volume"]
    return out


def parse_funding(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise FlowError(E4002, "资金费 zip 内无 CSV", path="source.binance_vision")
        raw = pd.read_csv(io.BytesIO(archive.read(names[0])))
    column_map = {
        "calc_time": store.TIMESTAMP_COL,
        "fundingTime": store.TIMESTAMP_COL,
        "timestamp": store.TIMESTAMP_COL,
        "last_funding_rate": "funding_rate",
        "fundingRate": "funding_rate",
    }
    out = raw.rename(columns={k: v for k, v in column_map.items() if k in raw.columns})
    wanted = [store.TIMESTAMP_COL, "funding_rate"]
    missing = [c for c in wanted if c not in out.columns]
    if missing:
        raise FlowError(
            E4002, f"资金费 CSV 缺列 {missing}，源列 {list(raw.columns)}",
            path="source.binance_vision",
        )
    return out[wanted]


def _window(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    stamps = pd.to_datetime(frame[store.TIMESTAMP_COL], unit="ms", utc=True)
    mask = (stamps >= start) & (stamps <= end)
    out = frame.loc[mask].copy()
    out[store.TIMESTAMP_COL] = stamps.loc[mask]
    return out


def _is_missing(exc: Exception) -> bool:
    """404 或明确的「缺归档」信号 —— 该月未上市/未归档，跳过而非失败。"""
    if getattr(exc, "code", None) == 404:
        return True
    if isinstance(exc, FlowError) and exc.error.code == "E4002" and "404" in str(exc):
        return True
    return False


def _fetch_month(
    dataset: str, ccxt_symbol: str, month: str, timeframe: str,
    start: datetime, end: datetime, get: Downloader,
) -> tuple[pd.DataFrame | None, str]:
    """下载+校验+解析单个月。

    返回 `(frame, status)`，status ∈ {ok, missing, error}：
    - missing：404（未上市/未归档），正常跳过；
    - error：网络/SSL 失败，软跳过并记录，供整体汇报后重跑；
    - 校验不符（checksum）则直接抛错——数据损坏必须 fail-closed，不可跳过。
    """
    symbol = vision_symbol(ccxt_symbol)
    url = (
        kline_url(symbol, timeframe, month)
        if dataset == "ohlcv"
        else funding_url(symbol, month)
    )
    try:
        blob = get(url)
    except Exception as exc:
        if _is_missing(exc):
            return None, "missing"
        # 网络失败软跳过，不让一个月的代理抖动毁掉整批。
        return None, "error"
    try:
        _verify_checksum(blob, url, get)
    except FlowError as exc:
        # 校验文件缺失/不符是数据完整性问题，必须 fail-closed。
        if "校验" in str(exc) or "sha256" in str(exc) or "不匹配" in str(exc):
            raise
        return None, "error"  # 校验文件本身下载失败 → 软跳过
    parsed = parse_kline(blob) if dataset == "ohlcv" else parse_funding(blob)
    return _window(parsed, start, end), "ok"


def download_vision(
    root: str | Path,
    datasets: Sequence[str],
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
    symbols: Sequence[str],
    timeframe: str = "1d",
    start: datetime,
    end: datetime,
    downloader: Downloader | None = None,
    proxy: str | None = None,
    timeout: float = 60.0,
    retries: int = 6,
    revise: bool = False,
    workers: int = 8,
) -> list[Any]:
    """从备份站下载月度归档并写入与 ccxt 相同的数据根。

    单标的、单月的 404（尚未上市 / 尚未归档）记入 skipped 但继续同标的其它月份；
    只有整个数据集零成功才由上层（cmd_fetch）判失败。月份下载按 `workers` 并发——
    备份站每月是独立静态文件，并发安全；全市场下载靠它把耗时从小时级压到分钟级。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .fetch import FetchReport  # 避免模块级循环依赖

    supported = ("ohlcv", "funding")
    unknown = [d for d in datasets if d not in supported]
    if unknown:
        raise FlowError(
            E1004, f"binance-vision 不支持数据集 {unknown}", path="datasets",
            hint="该来源仅支持 ohlcv / funding；open_interest/basis 请用交易所来源",
        )
    if not symbols:
        raise FlowError(E1004, "下载必须给标的清单", path="symbols")
    store.require_market_dataset(market, "ohlcv")  # 两个来源都只在合约侧
    months = _month_keys(start, end)

    get: Downloader = downloader or (
        lambda url: _http_get(url, timeout=timeout, retries=retries, proxy=proxy)
    )
    reports: list[Any] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for dataset in datasets:
            report = FetchReport(dataset=dataset, source="binance-vision")
            for ccxt_symbol in symbols:
                # 按标的分批：一个标的的所有月份并发下载，收齐后立即落盘。
                # 逐标的提交而非一次性全提交，好处是中途就可见进度、进程被打断
                # 时已完成标的的数据仍在盘上（append-only 重跑只补缺口）。
                futures = [
                    pool.submit(_fetch_month, dataset, ccxt_symbol, m,
                                timeframe, start, end, get)
                    for m in months
                ]
                frames: list[pd.DataFrame] = []
                missing = errors = 0
                for future in as_completed(futures):
                    frame, status = future.result()
                    if status == "missing":
                        missing += 1
                    elif status == "error":
                        errors += 1  # 网络软失败：记录后可重跑补齐
                    elif frame is not None and not frame.empty:
                        frames.append(frame)
                if not frames:
                    report.skipped.append({
                        "symbol": ccxt_symbol, "reason": "no_months",
                        "detail": f"{len(months)} 个月无归档（未上市或区间外）",
                    })
                    continue
                frame = pd.concat(frames, ignore_index=True)
                frame = frame.drop_duplicates(subset=[store.TIMESTAMP_COL], keep="last")
                frame = frame.sort_values(store.TIMESTAMP_COL, kind="mergesort").reset_index(drop=True)
                if frame.empty:
                    report.skipped.append({"symbol": ccxt_symbol, "reason": "empty_window"})
                    continue
                target = store.dataset_file(
                    root, dataset, exchange, market, ccxt_symbol,
                    timeframe if dataset == store.BAR_DATASET else None,
                )
                stats = store.upsert(target, frame, dataset, symbol=ccxt_symbol, revise=revise)
                report.symbols += 1
                report.rows_added += stats["added"]
                report.revised += stats["revised"]
                if missing:
                    report.skipped.append({
                        "symbol": ccxt_symbol, "reason": "missing_months", "count": missing,
                    })
                if errors:
                    report.skipped.append({
                        "symbol": ccxt_symbol, "reason": "network_errors", "count": errors,
                        "hint": "网络抖动致部分月份未下载；重跑同一命令可补齐（append-only）",
                    })
            reports.append(report)
    return reports


__all__ = [
    "BASE_URL", "Downloader", "download_vision", "vision_symbol",
    "kline_url", "funding_url", "parse_kline", "parse_funding",
]
