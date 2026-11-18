"""币安备份站来源（data.binance.vision）的供给契约。

不依赖网络：下载器注入假实现，生成内存 zip 与 CHECKSUM；验证四件事——
1. 符号 `BTC/USDT:USDT` → `BTCUSDT`（base+quote，去掉结算后缀）；
2. 月度按 YYYY-MM 推进，缺月（未上市）跳过不失败；
3. sha256 不符必须拒绝落盘（fail-closed）；
4. 端到端落盘后能被 store.read_dataset 按原 ccxt 符号读回。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timezone

import pandas as pd
import pytest

from contracts.errors import E4002, FlowError
from upstream import binance_vision as bv
from upstream import store

SYM = "BTC/USDT:USDT"
START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = datetime(2020, 3, 1, tzinfo=timezone.utc)


def _zip_bytes(csv_name: str, frame: pd.DataFrame, *, header: bool) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(csv_name, frame.to_csv(index=False, header=header))
    return buf.getvalue()


def _kline_zip(month: str, header: bool = False) -> bytes:
    # 早期无表头、2023 起带表头，两种格式备份站都实际存在。
    ts = int(datetime(2020, int(month[-2:]), 1, tzinfo=timezone.utc).timestamp() * 1000)
    if header:
        rows = pd.DataFrame([{
            "open_time": ts, "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.5, "volume": 1e3,
        }])
    else:
        rows = pd.DataFrame([[ts, 100.0, 101.0, 99.0, 100.5, 1e3]])
    return _zip_bytes(f"BTCUSDT-1d-{month}.csv", rows, header=header)


def _funding_zip(month: str) -> bytes:
    rows = pd.DataFrame([{
        "calc_time": int(datetime(2020, int(month[-2:]), 1, tzinfo=timezone.utc).timestamp() * 1000),
        "funding_interval_hours": 8,
        "last_funding_rate": 0.0001,
    }])
    return _zip_bytes(f"BTCUSDT-fundingRate-{month}.csv", rows, header=True)


def _fake_downloader(kind: str, *, corrupt_month: str | None = None):
    """按 URL 返回 zip / CHECKSUM；某月可注入错误的 CHECKSUM 触发校验失败。"""
    cache: dict[str, bytes] = {}

    def get(url: str) -> bytes:
        if url in cache:
            return cache[url]
        month = url.split("-")[-1].replace(".zip", "").replace(".CHECKSUM", "")
        if url.endswith(".CHECKSUM"):
            target = url[:-len(".CHECKSUM")]
            blob = get(target)
            digest = (
                "0" * 64 if month == corrupt_month
                else hashlib.sha256(blob).hexdigest()
            )
            text = f"{digest}  {target.rsplit('/', 1)[-1]}\n"
            return text.encode()
        if "-1d-" in url:
            blob = _kline_zip(month)
        elif "fundingRate-" in url:
            blob = _funding_zip(month)
        else:
            raise AssertionError(f"unexpected url {url}")
        cache[url] = blob
        return blob

    return get


# ---------------------------------------------------------------- 符号转换
def test_vision_symbol_strips_settle_suffix():
    assert bv.vision_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert bv.vision_symbol("1000PEPE/USDT:USDT") == "1000PEPEUSDT"
    assert bv.vision_symbol("ETH/USDT") == "ETHUSDT"


def test_vision_symbol_rejects_malformed():
    with pytest.raises(FlowError) as exc:
        bv.vision_symbol("BTC")
    assert exc.value.error.code == "E1004"


# ---------------------------------------------------------------- 月度推进
def test_month_keys_are_inclusive_and_utc():
    keys = bv._month_keys(START, END)
    assert keys == ["2020-01", "2020-02", "2020-03"]


def test_month_keys_require_timezone():
    with pytest.raises(FlowError) as exc:
        bv._month_keys(datetime(2020, 1, 1), END)
    assert exc.value.error.code == "E1014"


# ---------------------------------------------------------------- 表头兼容
def test_parse_kline_with_and_without_header():
    no_header = _kline_zip("2020-01", header=False)
    with_header = _kline_zip("2024-01", header=True)
    a = bv.parse_kline(no_header)
    b = bv.parse_kline(with_header)
    assert list(a.columns) == [store.TIMESTAMP_COL, "open", "high", "low", "close", "volume"]
    assert list(b.columns) == list(a.columns)
    assert b.iloc[0]["close"] == 100.5


# ---------------------------------------------------------------- 端到端落盘
def test_download_writes_parquet_readable_by_store(tmp_path):
    reports = bv.download_vision(
        tmp_path, ["ohlcv", "funding"], symbols=[SYM],
        start=START, end=END, downloader=_fake_downloader("ohlcv"),
    )
    assert [r.dataset for r in reports] == ["ohlcv", "funding"]
    assert all(r.symbols == 1 for r in reports)
    assert all(r.rows_added == 3 for r in reports)

    ohlcv = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                               symbols=[SYM], timeframe="1d")
    assert SYM in ohlcv.symbols
    assert ohlcv.rows == 3


# ---------------------------------------------------------------- 缺月跳过
def test_missing_month_is_skipped_not_failed(tmp_path):
    def get(url: str) -> bytes:
        # 只给 2020-01 有归档，02/03 视为未上市
        if url.endswith(".CHECKSUM"):
            return get(url[:-len(".CHECKSUM")]) and (
                f"{hashlib.sha256(get(url[:-len('.CHECKSUM')])).hexdigest()}  x\n".encode()
            )
        month = url.split("-")[-1].replace(".zip", "")
        if month != "01":
            raise FlowError(E4002, f"404 for {url}")
        return _kline_zip(month)

    reports = bv.download_vision(
        tmp_path, ["ohlcv"], symbols=[SYM], start=START, end=END, downloader=get,
    )
    assert reports[0].symbols == 1  # 01 月成功 → 不算零成功
    assert reports[0].skipped[0]["reason"] == "missing_months"
    assert reports[0].skipped[0]["count"] == 2


# ---------------------------------------------------------------- 校验失败
def test_checksum_mismatch_is_rejected(tmp_path):
    get = _fake_downloader("ohlcv", corrupt_month="01")
    with pytest.raises(FlowError) as exc:
        bv.download_vision(
            tmp_path, ["ohlcv"], symbols=[SYM], start=START, end=END, downloader=get,
        )
    assert exc.value.error.code == "E4002"


def test_unsupported_dataset_rejected(tmp_path):
    with pytest.raises(FlowError) as exc:
        bv.download_vision(
            tmp_path, ["basis"], symbols=[SYM], start=START, end=END,
            downloader=_fake_downloader("ohlcv"),
        )
    assert exc.value.error.code == "E1004"
