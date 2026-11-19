"""统一取数门面 market_data 的离线单元测试。

ccxt client / vision downloader 全部注入假实现，不依赖任何网络。
时间戳用 pandas 按日期动态生成，不硬编码。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from contracts.errors import FlowError
from services import market_data as md


def _ms(day: str, hour: int = 0) -> int:
    ts = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int((ts + pd.Timedelta(hours=hour)).timestamp() * 1000)


def _kline_csv(rows: list[tuple[int, float]]) -> bytes:
    """rows = [(timestamp_ms, close), ...] → 12 列无表头 K 线 zip。"""
    lines = []
    for ts, close in rows:
        lines.append(
            f"{ts},{close},{close + 1},{close - 1},{close + 0.5},10,"
            f"{ts + 1},20,3,4,5,0\n"
        )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.csv", "".join(lines))
    return buf.getvalue()


def _funding_csv(rows: list[tuple[int, float]]) -> bytes:
    """rows = [(calc_time_ms, rate), ...] → 带表头 fundingRate zip（真实列结构）。"""
    lines = ["calc_time,funding_interval_hours,last_funding_rate\n"]
    for ts, rate in rows:
        lines.append(f"{ts},8,{rate}\n")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.csv", "".join(lines))
    return buf.getvalue()


# ---------------------------------------------------------------- 注册表 / 周期
def test_adapters_and_market_normalization():
    assert "binance" in md.ADAPTERS
    assert md.get_adapter("BINANCE").key == "binance"
    assert md.normalize_market("um") == "futures"
    assert md.normalize_market("spot") == "spot"
    with pytest.raises(FlowError) as exc:
        md.get_adapter("okx")
    assert exc.value.error.code == "E1004"
    with pytest.raises(FlowError):
        md.normalize_market("cm")


def test_timeframe_only_for_kline():
    assert md.needs_timeframe("klines") is True
    assert md.needs_timeframe("markPriceKlines") is True
    assert md.needs_timeframe("trades") is False
    assert md._resolve_timeframe("klines", "4h") == "4h"
    assert md._resolve_timeframe("trades", "4h") is None  # 非 K 线忽略


def test_make_client_passes_proxy(monkeypatch):
    """代理必须透传到 ccxt 的 proxies 配置，并关掉环境代理继承。"""
    captured: dict = {}

    class FakeExchange:
        def __init__(self, opts):
            captured.update(opts)

    import sys
    import types
    fake_ccxt = types.ModuleType("ccxt")
    fake_ccxt.binanceusdm = FakeExchange
    fake_ccxt.binance = FakeExchange
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    md.get_adapter("binance").make_client("futures", proxy="http://127.0.0.1:7890")
    assert captured["proxies"] == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    assert captured["aiohttp_trust_env"] is False
    assert captured["options"]["defaultType"] == "future"

    # 无代理时不应写入 proxies。
    captured.clear()
    md.get_adapter("binance").make_client("spot")
    assert "proxies" not in captured
    assert captured["options"]["defaultType"] == "spot"


# ---------------------------------------------------------------- 元数据原语
def test_fetch_metadata_via_injected_client(tmp_path):
    class Fake:
        def load_markets(self):
            return {
                "BTC/USDT:USDT": {"info": {
                    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
                    "contractType": "PERPETUAL", "contractStatus": "TRADING",
                    "status": "TRADING", "underlyingType": "COIN",
                    "onboardDate": "2019-09-08",
                }},
                "FTT/USDT:USDT": {"info": {
                    "symbol": "FTTUSDT", "baseAsset": "FTT", "quoteAsset": "USDT",
                    "contractType": "PERPETUAL", "contractStatus": "CLOSE",
                    "status": "BREAK",
                }},
            }

    out = md.fetch_metadata("binance", "futures", root=tmp_path, client=Fake())
    assert out.source == "api"
    assert out.records["BTC/USDT:USDT"]["asset_class"] == "crypto"
    assert out.records["FTT/USDT:USDT"]["exchange_state"] == "closed"
    assert out.records["BTC/USDT:USDT"]["onboard_date"] == "2019-09-08"


# ---------------------------------------------------------------- 全历史原语
def test_fetch_history_concat_and_timeframe_url():
    seen_urls: list[str] = []
    # 两天各一行，时间戳不同 → 合并后 2 行。
    def downloader(url: str) -> bytes:
        seen_urls.append(url)
        day = "2026-01-01" if "2026-01-01" in url else "2026-01-02"
        return _kline_csv([(_ms(day), 1.0)])

    out = md.fetch_history(
        "BTC/USDT:USDT", "klines", date(2026, 1, 1), date(2026, 1, 2),
        timeframe="1d", downloader=downloader, verify=False,
    )
    assert out.rows == 2
    assert out.timeframe == "1d"
    assert "/1d/" in seen_urls[0]
    assert len(seen_urls) == 2
    assert set(out.frame.columns) >= {"timestamp", "open", "close", "volume"}


def test_fetch_history_respects_timeframe():
    seen: list[str] = []
    md.fetch_history(
        "BTC/USDT:USDT", "markPriceKlines", date(2026, 1, 1), date(2026, 1, 1),
        timeframe="4h", verify=False,
        downloader=lambda u: (seen.append(u), _kline_csv([(_ms("2026-01-01"), 1.0)]))[1],
    )
    assert "markPriceKlines" in seen[0] and "/4h/" in seen[0]


def test_fetch_history_funding_uses_monthly_archive():
    """funding 的 fetch_history 走月度归档：URL 是 monthly/fundingRate，用 vname。"""
    seen: list[str] = []

    def downloader(url: str) -> bytes:
        seen.append(url)
        month = "2026-01" if "2026-01" in url else "2026-02"
        day = f"{month}-01"
        return _funding_csv([(_ms(day), 0.0001), (_ms(day, 8), 0.0002)])

    out = md.fetch_history(
        "BTC/USDT:USDT", "funding", date(2026, 1, 1), date(2026, 2, 28),
        verify=False, downloader=downloader,
    )
    assert out.rows == 4  # 两个月各 2 行
    assert "/monthly/fundingRate/" in seen[0]
    assert "BTCUSDT-fundingRate-2026-01.zip" in seen[0]
    assert "last_funding_rate" in out.frame.columns
    assert "timestamp" in out.frame.columns


# ---------------------------------------------------------------- 区间原语
def test_fetch_range_routes_vision_and_ccxt():
    """归档段走 vision，最近段走注入的 ccxt client，两段合并。"""
    class Fake:
        has = {"fetchOHLCV": True}

        def fetchOHLCV(self, symbol, timeframe, since=None, limit=None):
            # 最近段两行：01-02 与 01-03。
            return [
                [_ms("2026-01-02"), 9, 9, 9, 9, 99],
                [_ms("2026-01-03"), 8, 8, 8, 8, 88],
            ]

    def downloader(url: str) -> bytes:
        # ccxt_lag_days=2，hi=01-03，归档段只有 [01-01, 01-01]。
        return _kline_csv([(_ms("2026-01-01"), 1.0)])

    out = md.fetch_range(
        "BTC/USDT:USDT", "klines", date(2026, 1, 1), date(2026, 1, 3),
        timeframe="1d", ccxt_lag_days=2, client=Fake(),
        downloader=downloader, verify=False,
    )
    assert "vision" in out.sources and "ccxt" in out.sources
    assert out.rows == 3  # 归档 1 行 + ccxt 2 行
    assert out.frame["timestamp"].min() == pd.Timestamp("2026-01-01", tz="UTC")
    assert out.frame["timestamp"].max() == pd.Timestamp("2026-01-03", tz="UTC")


def test_fetch_range_rejects_bad_dataset():
    with pytest.raises(FlowError):
        md.fetch_range("BTC/USDT:USDT", "not_a_dataset", date(2026, 1, 1), date(2026, 1, 2))


# ---------------------------------------------------------------- 双源能力配置
def test_vision_eight_datasets_mapped():
    """用户确认的 8 类 data vision 归档都在配置表里，且 vision 侧声明为历史。"""
    vision_eight = {
        "bookDepth", "bookTicker", "indexPriceKlines", "klines",
        "markPriceKlines", "metrics", "premiumIndexKlines", "trades",
    }
    for name in vision_eight:
        spec = md.get_spec(name)
        assert spec.vision.supported, f"{name} 应在 vision 侧可用"
        assert spec.vision.kind is md.SourceKind.HISTORY


def test_capability_mapping_two_sources():
    """两个源的能力映射符合实测。"""
    # 双源历史：K 线四类 + trades
    for name in ("klines", "indexPriceKlines", "markPriceKlines", "premiumIndexKlines", "trades"):
        assert md.get_spec(name).from_both is True, name
    # 归档历史 + ccxt 快照
    assert md.get_spec("bookDepth").ccxt.kind is md.SourceKind.SNAPSHOT
    assert md.get_spec("bookTicker").ccxt.kind is md.SourceKind.SNAPSHOT
    # metrics 归档历史 + 原生 REST
    assert md.get_spec("metrics").ccxt.kind is md.SourceKind.NATIVE_REST
    # funding：月度归档 + ccxt 历史（实测 2026-10 月包可用）
    assert md.get_spec("funding").vision.kind is md.SourceKind.HISTORY
    assert md.get_spec("funding").vision.monthly is True
    assert md.get_spec("funding").vision.name == "fundingRate"
    assert md.get_spec("funding").has_ccxt_history is True
    assert md.get_spec("funding").from_both is True
    # bookTicker 日归档已实测（稀疏，非 404）
    assert md.get_spec("bookTicker").vision.kind is md.SourceKind.HISTORY
    assert md.get_spec("bookTicker").vision.monthly is False
    # 仅 ccxt 历史（归档无）
    assert md.get_spec("open_interest").vision.kind is md.SourceKind.NONE


def test_plan_sources_auto():
    # 双源互补
    p = md.plan_sources("klines", "auto", True)
    assert p.both is True
    # funding 现在是月度归档 + ccxt，auto 双源互补
    p = md.plan_sources("funding", "auto", True)
    assert p.both is True
    # 仅 ccxt（open_interest 归档无历史）
    p = md.plan_sources("open_interest", "auto", True)
    assert p.use_ccxt and not p.use_vision
    # metrics：归档 + 原生 REST 也是双源互补
    p = md.plan_sources("metrics", "auto", True)
    assert p.both is True
    # 快照：ccxt 一定参与
    p = md.plan_sources("bookDepth", "auto", True)
    assert p.use_ccxt


def test_plan_sources_explicit_and_legacy():
    assert md.plan_sources("klines", "vision", True).use_vision is True
    assert md.plan_sources("klines", "ccxt", True).use_ccxt is True
    # use_vision=False 兼容旧开关 → 只走 ccxt
    p = md.plan_sources("klines", "auto", False)
    assert p.use_ccxt and not p.use_vision
    # 强制不支持的源 → 报错（open_interest 没有归档）
    with pytest.raises(FlowError) as exc:
        md.plan_sources("open_interest", "vision", True)
    assert exc.value.error.code == "E1004"
    # funding 现在可以强制走月度归档
    assert md.plan_sources("funding", "vision", True).use_vision is True
    with pytest.raises(FlowError):
        md.plan_sources("klines", "bogus", True)


def test_fetch_range_explicit_vision_skips_ccxt():
    """显式 vision 源时不创建 ccxt client 段。"""
    seen_urls: list[str] = []

    def downloader(url: str) -> bytes:
        seen_urls.append(url)
        return _kline_csv([(_ms("2026-01-01"), 1.0)])

    out = md.fetch_range(
        "BTC/USDT:USDT", "klines", date(2026, 1, 1), date(2026, 1, 1),
        source="vision", downloader=downloader, verify=False,
    )
    assert out.sources == ["vision"]
    assert out.rows == 1
    # 若误走 ccxt 会尝试 build client 而无网络 → 这里没报错即证明跳过了。


def test_fetch_range_funding_explicit_ccxt():
    """显式 source=ccxt 时 funding 只走 ccxt（跳过月度归档段）。"""
    class Fake:
        has = {"fetchFundingRateHistory": True}

        def fetchFundingRateHistory(self, symbol, since=None, limit=None):
            return [{"symbol": symbol, "fundingRate": 0.0001,
                     "timestamp": _ms("2026-01-01")}]

    out = md.fetch_range(
        "BTC/USDT:USDT", "funding", date(2026, 1, 1), date(2026, 1, 2),
        source="ccxt", client=Fake(),
    )
    assert out.sources == ["ccxt"]
    assert out.rows == 1
