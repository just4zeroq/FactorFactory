"""L0 三层：交易所元数据 / 日模板初始化 / ccxt 增量 —— 离线单元测试。

所有网络与磁盘都注入假实现，测试不依赖 data.binance.vision / fapi 可达性。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
import zipfile

from contracts.errors import FlowError
from services import exchange_metadata as em
from services import l0_incremental as inc
from services import l0_store as ls
from services import universe_service as us
from upstream import vision_daily as vd


def _zip_csv(text: str) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.csv", text)
    return buf.getvalue()


# ---------------------------------------------------------------- 日模板 URL 与解析
def test_daily_url_all_datasets():
    day = date(2026, 10, 10)
    url = vd.daily_url("klines", "BTC/USDT:USDT", day)
    assert url == (
        "https://data.binance.vision/data/futures/um/daily/klines/"
        "BTCUSDT/1d/BTCUSDT-1d-2026-10-10.zip"
    )
    assert vd.daily_url("aggTrades", "BTCUSDT", day).endswith(
        "/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-10-10.zip"
    )
    assert "/spot/daily/" in vd.daily_url("klines", "BTC/USDT", day, market="spot")
    assert vd._vision_symbol("BTCUSDT") == "BTCUSDT"  # 幂等


def test_dataset_market_compat():
    with pytest.raises(FlowError) as exc:
        vd.validate_datasets(["metrics"], market="spot")
    assert exc.value.error.code == "E1004"
    assert vd.validate_datasets(["klines", "klines"], "futures") == ["klines"]


def test_parse_kline_and_epoch_unit_detection():
    # 无表头、毫秒
    csv = "1790812800000,1,2,0.5,1.5,10,1790812800001,20,3,4,5,0\n"
    frame = vd.parse_daily("klines", _zip_csv(csv))
    assert list(frame.columns)[:5] == ["open", "high", "low", "close", "volume"]
    assert str(frame["timestamp"].iloc[0]).startswith("2026-10-01")
    assert frame["timestamp"].dt.tz is not None
    # 微秒（16 位）不能被当成 ms 落到 1970
    csv_us = "1790812800000000,1,2,0.5,1.5,10,1790812800000001,20,3,4,5,0\n"
    frame_us = vd.parse_daily("klines", _zip_csv(csv_us))
    assert frame_us["timestamp"].iloc[0].year == 2026


def test_parse_book_depth_keeps_all_columns():
    csv = (
        "timestamp,percentage,depth,notional\n"
        "2026-10-10 00:00:03,-1.00,2216.29,18211004\n"
        "2026-10-10 00:00:03,1.00,2200.00,18000000\n"
    )
    frame = vd.parse_daily("bookDepth", _zip_csv(csv))
    assert len(frame) == 2
    assert "notional" in frame.columns
    assert frame["timestamp"].dt.tz is not None


def test_bookticker_verified_with_real_columns():
    """bookTicker 日归档已按用户给的 2024-03-30 样本实测确认。"""
    spec = vd.DAILY_DATASETS["bookTicker"]
    assert spec.verified is True
    assert spec.time_col == "event_time"
    assert "best_bid_price" in spec.value_cols
    assert "transaction_time" in spec.value_cols
    # 解析真实列结构的样本：event_time → timestamp，其余原样保留
    csv = (
        "update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,transaction_time,event_time\n"
        "1,100.5,2.0,101.0,3.0,1711756800080,1711756800087\n"
    )
    frame = vd.parse_daily("bookTicker", _zip_csv(csv))
    assert len(frame) == 1
    assert "timestamp" in frame.columns and "event_time" not in frame.columns
    assert "best_bid_price" in frame.columns and "transaction_time" in frame.columns
    assert frame["timestamp"].dt.tz is not None


def test_monthly_funding_rate():
    """fundingRate 月度归档：URL 模板、月份范围、解析。"""
    assert vd.monthly_url("fundingRate", "BTC/USDT:USDT", "2026-10") == (
        "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
        "BTCUSDT/BTCUSDT-fundingRate-2026-10.zip"
    )
    months = vd.month_range(date(2026, 9, 15), date(2026, 11, 3))
    assert months == ["2026-09", "2026-10", "2026-11"]
    csv = "calc_time,funding_interval_hours,last_funding_rate\n1790812800001,8,0.00007981\n"
    frame = vd.parse_monthly("fundingRate", _zip_csv(csv))
    assert len(frame) == 1
    assert "timestamp" in frame.columns and "calc_time" not in frame.columns
    assert "last_funding_rate" in frame.columns
    assert frame["timestamp"].dt.tz is not None


def test_day_range_inclusive():
    days = vd.day_range(date(2026, 10, 8), date(2026, 10, 10))
    assert len(days) == 3 and days[-1] == date(2026, 10, 10)


# ---------------------------------------------------------------- L0 原样存储
def test_l0_store_append_only_and_compound_dedup(tmp_path):
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-10-10"] * 4, utc=True),
        "percentage": [-2.0, -1.0, 1.0, 2.0],
        "depth": [1.0, 2.0, 3.0, 4.0],
    })
    assert ls.append_frame(tmp_path, "bookDepth", "BTC/USDT:USDT", df)["added"] == 4
    assert ls.append_frame(tmp_path, "bookDepth", "BTC/USDT:USDT", df)["added"] == 0
    assert len(ls.read_raw(tmp_path, "bookDepth", "BTC/USDT:USDT")) == 4


def test_l0_store_revise_replaces_same_key(tmp_path):
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-10-10"], utc=True),
        "open": [1.0], "quote_volume": [10.0],
    })
    ls.append_frame(tmp_path, "klines", "BTC/USDT:USDT", df)
    new = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-10-10"], utc=True),
        "open": [9.0], "quote_volume": [99.0],
    })
    out = ls.append_frame(tmp_path, "klines", "BTC/USDT:USDT", new, revise=True)
    assert out["replaced"] == 1
    got = ls.read_raw(tmp_path, "klines", "BTC/USDT:USDT")
    assert float(got["open"].iloc[0]) == 9.0


# ---------------------------------------------------------------- 交易所元数据
def test_normalize_marks_delisted_and_asset_class():
    sample = {"symbols": [
        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
         "contractType": "PERPETUAL", "contractStatus": "TRADING", "status": "TRADING",
         "underlyingType": "COIN", "onboardDate": "2019-09-08"},
        {"symbol": "FTTUSDT", "baseAsset": "FTT", "quoteAsset": "USDT",
         "contractType": "PERPETUAL", "contractStatus": "CLOSE", "status": "BREAK"},
        {"symbol": "CRWDUSDT", "baseAsset": "CRWD", "quoteAsset": "USDT",
         "contractType": "PERPETUAL", "contractStatus": "TRADING", "status": "TRADING",
         "underlyingType": "EQUITY"},
        {"symbol": "BTCUSD_230929", "baseAsset": "BTC", "quoteAsset": "USD",
         "contractType": "CURRENT_QUARTER", "status": "TRADING"},
    ]}
    recs, stats = em.normalize(sample, "um")
    assert stats["closed"] == 1 and stats["skipped_non_perp"] == 1
    assert recs["FTT/USDT:USDT"]["exchange_state"] == "closed"
    assert recs["CRWD/USDT:USDT"]["asset_class"] == "equity"
    assert recs["BTC/USDT:USDT"]["onboard_date"] == "2019-09-08"


def test_merge_universe_trusts_exchange_state_not_zombie_bar():
    """退市币备份站仍给零量僵尸 bar（末根是昨天），必须按交易所状态判退市。"""
    rec = us.make_record(
        "FTT/USDT:USDT", exchange_state="closed", contract_status="CLOSE",
        delivery_date="2022-11-11",
    )
    rec = us.attach_bars(rec, first_bar="2020-01-01", last_bar="2026-11-18", bars=2000)
    merged = us.merge_universe({}, {rec["symbol"]: rec}, observed_at="2026-11-19T00:00:00+00:00")
    out = merged["FTT/USDT:USDT"]
    assert out["status"] == us.STATUS_DELISTED
    assert out["delisted_at"] == "2022-11-11"


def test_collect_falls_back_to_cache(tmp_path, monkeypatch):
    cached = {"serverTime": 1, "symbols": [
        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
         "contractType": "PERPETUAL", "contractStatus": "TRADING", "status": "TRADING"},
    ]}
    em.write_snapshot(tmp_path, "um", cached)
    def offline_fetcher():
        raise FlowError("E4002", "offline")
    recs, stats, source = em.collect(tmp_path, "um", fetcher=offline_fetcher)
    assert source == "cache" and "warning" in stats
    assert "BTC/USDT:USDT" in recs


# ---------------------------------------------------------------- ccxt 增量
def test_incremental_uses_kline_methods(tmp_path):
    class Fake:
        has = {m: True for m in inc.KLINE_METHODS.values()}

        def _rows(self, *a, **k):
            return [[1790812800000, 1, 2, 0.5, 1.5, 10]]

        fetchOHLCV = fetchIndexOHLCV = fetchMarkOHLCV = fetchPremiumIndexOHLCV = _rows

    written = []

    def writer(root, dataset, symbol, frame, **kwargs):
        written.append((dataset, symbol, len(frame)))
        return {"added": len(frame)}

    out = inc.incremental_update(
        tmp_path, ["klines", "markPriceKlines"],
        symbols=["BTC/USDT:USDT"], client=Fake(), writer=writer,
    )
    assert [r["rows_added"] for r in out] == [1, 1]
    assert len(written) == 2


def test_incremental_rejects_unknown(tmp_path):
    with pytest.raises(FlowError):
        inc.incremental_update(tmp_path, ["not_a_dataset"], symbols=["BTC/USDT:USDT"], client=object())
