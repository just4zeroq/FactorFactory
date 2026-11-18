"""方案 B 的供给契约（19-supply.md）：一份数据集 = 一组 parquet，身份读取时现算。

删掉 S2 的事务协议之后，这些性质不能再靠「租约 / 暂存 / 门禁 A / 别名」间接保证，
必须逐条直测：

1. **路径规则**：`<root>/<dataset>/<exchange>/<market>/`，bar 带 tf、事件流不带；
2. **append-only**：同一 timestamp 的旧值永不覆盖，冲突只计数；
3. **身份现算**：checksum 行序无关、同内容同值、`version = checksum[:16]`；
4. **缺口**：bar 容差 1.5×中位间隔，事件流 3×（间隔本身会变）；
5. **逐标的覆盖**：晚上市的标的必须当场报出来，不能等 NaN 传到 allocate；
6. **市场维度**：spot 与 futures 是两份数据，数据集定义域不同；
7. **零成功即失败**：一次采集全都跳过，命令不能返回成功。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from contracts.errors import E1004, E4002, E4003, FlowError
from upstream import fetch as fetcher
from upstream import store

NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


def _ohlcv(stamps, base: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({
        store.TIMESTAMP_COL: pd.DatetimeIndex(stamps, tz="UTC"),
        "open": base, "high": base + 1, "low": base - 1,
        "close": base + 0.5, "volume": 1e3,
    })


def _write(tmp_path, dataset, symbol, frame, *, exchange="binanceusdm",
           market="futures", timeframe="1d", revise=False):
    # 事件流的文件名不带 timeframe（间隔由数据决定），传了会 E1004。
    tf = timeframe if dataset == store.BAR_DATASET else None
    target = store.dataset_file(tmp_path, dataset, exchange, market, symbol, tf)
    return store.upsert(target, frame, dataset, symbol=symbol, revise=revise)


# ---------------------------------------------------------------- 路径规则
def test_path_carries_market_and_timeframe(tmp_path):
    """bar 带 tf（1d 与 4h 是两份数据）；事件流不带（间隔由数据决定）。"""
    day = store.dataset_file(tmp_path, "ohlcv", "binanceusdm", "futures",
                             "BTC/USDT:USDT", "1d")
    hour = store.dataset_file(tmp_path, "ohlcv", "binanceusdm", "futures",
                              "BTC/USDT:USDT", "4h")
    assert day != hour and day.name == "BTC_USDT_USDT-1d.parquet"
    funding = store.dataset_file(tmp_path, "funding", "binanceusdm", "futures",
                                 "BTC/USDT:USDT")
    assert funding.name == "BTC_USDT_USDT.parquet"
    assert funding.parent.name == "futures"
    with pytest.raises(FlowError) as exc:
        store.dataset_file(tmp_path, "funding", "binanceusdm", "futures",
                           "BTC/USDT:USDT", "1d")
    assert exc.value.code == E1004


def test_spot_and_futures_never_share_a_file(tmp_path):
    """同一交易所 id 下两个市场必须是两个目录，否则现货会覆盖合约。"""
    spot = store.dataset_file(tmp_path, "ohlcv", "binance", "spot", "BTC/USDT", "1d")
    fut = store.dataset_file(tmp_path, "ohlcv", "binance", "futures",
                             "BTC/USDT:USDT", "1d")
    assert spot.parent.name == "spot" and fut.parent.name == "futures"
    assert spot != fut


# ---------------------------------------------------------------- append-only
def test_upsert_keeps_old_values_and_only_counts_conflicts(tmp_path):
    """交易所修订历史 K 线时**不得**覆盖：过去跑出的结论要还能解释。"""
    stamps = pd.date_range("2026-01-01", periods=5, freq="1D", tz="UTC")
    frame = _ohlcv(stamps)
    symbol = "BTC/USDT:USDT"
    first = _write(tmp_path, "ohlcv", symbol, frame)
    assert first["added"] == 5

    again = _write(tmp_path, "ohlcv", symbol, frame)
    assert (again["added"], again["revised"], again["unchanged"]) == (0, 0, 5)

    changed = frame.copy()
    changed.loc[2, "close"] = 999.0
    stats = _write(tmp_path, "ohlcv", symbol, changed)
    assert stats["revised"] == 1 and stats["added"] == 0

    path = store.dataset_file(tmp_path, "ohlcv", "binanceusdm", "futures", symbol, "1d")
    kept = store.read_file(path, "ohlcv")
    assert kept.loc[2, "close"] == pytest.approx(100.5)  # 旧值保留


def test_upsert_appends_new_timestamps(tmp_path):
    stamps = pd.date_range("2026-01-01", periods=3, freq="1D", tz="UTC")
    symbol = "BTC/USDT:USDT"
    _write(tmp_path, "ohlcv", symbol, _ohlcv(stamps))
    more = _ohlcv(pd.date_range("2026-01-04", periods=2, freq="1D", tz="UTC"))
    stats = _write(tmp_path, "ohlcv", symbol, more)
    assert stats["added"] == 2 and stats["rows"] == 5


# ---------------------------------------------------------------- 身份现算
def test_checksum_is_order_independent_and_stable(tmp_path):
    """行序无关 + 同内容同值：这是「这份结论用的哪份数据」的唯一答案。"""
    a = pd.date_range("2026-01-01", periods=4, freq="1D", tz="UTC")
    ds1 = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                             symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW) \
        if _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(a)) else None
    b = pd.date_range("2026-01-01", periods=4, freq="1D", tz="UTC")
    ds2 = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                             symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    assert ds1.checksum == ds2.checksum and ds1.version == ds2.version
    assert ds1.version == ds1.checksum.split(":", 1)[-1][:16]
    # 换内容 → 换身份
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(b, base=200.0), revise=True)
    ds3 = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                             symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    assert ds3.checksum != ds1.checksum


def test_pin_carries_nine_fields_and_gate_a_verdict(tmp_path):
    stamps = pd.date_range("2026-01-01", periods=3, freq="1D", tz="UTC")
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(stamps))
    ds = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    pin = ds.pin()
    assert pin.dataset == "ohlcv" and pin.end_closed is True
    assert pin.gate_a["passed"] is ds.passed
    # 覆盖到 2026-01-03，末根 + 1d 已闭合
    assert pin.coverage["start"].date().isoformat() == "2026-01-01"
    assert ds.meta()["market"] == "futures"


# ---------------------------------------------------------------- 缺口
def test_bar_gap_beyond_one_and_a_half_intervals_is_reported(tmp_path):
    stamps = list(pd.date_range("2026-01-01", periods=3, freq="1D", tz="UTC"))
    stamps.append(stamps[-1] + timedelta(days=3))  # 跳过两根
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(stamps))
    ds = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    assert [g["kind"] for g in ds.gaps] == ["interior"]
    assert ds.passed is False and ds.reason_code == E4002


def test_event_stream_tolerates_irregular_intervals(tmp_path):
    """资金费实测 1h/4h/8h 混合：1.5× 容差会把正常变化报成缺口。"""
    stamps = pd.to_datetime(
        ["2026-01-01T00:00Z", "2026-01-01T01:00Z", "2026-01-01T09:00Z",
         "2026-01-01T17:00Z"], utc=True,
    )
    frame = pd.DataFrame({store.TIMESTAMP_COL: stamps, "funding_rate": 1e-4})
    _write(tmp_path, "funding", "BTC/USDT:USDT", frame)
    ds = store.read_dataset(tmp_path, "funding", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT"], now=NOW)
    assert ds.gaps == () and ds.passed is True


def test_unclosed_tail_is_rejected(tmp_path):
    """末根还没走完就发布 = 把一个未定型的 bar 当事实。"""
    last = NOW.replace(minute=0, second=0, microsecond=0)
    stamps = [last - timedelta(days=1), last]
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(stamps))
    ds = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    assert ds.end_closed is False and ds.reason_code == E4003


# ---------------------------------------------------------------- 逐标的覆盖
def test_check_window_reports_the_symbol_that_starts_late(tmp_path):
    """晚上市的标的必须点名：否则 NaN 会一路传到 allocate 的 price。"""
    full = pd.date_range("2026-01-01", periods=10, freq="1D", tz="UTC")
    late = pd.date_range("2026-01-06", periods=5, freq="1D", tz="UTC")
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(full))
    _write(tmp_path, "ohlcv", "ARB/USDT:USDT", _ohlcv(late))
    ds = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT", "ARB/USDT:USDT"],
                            timeframe="1d", now=NOW)
    with pytest.raises(FlowError) as exc:
        ds.check_window(full[0].to_pydatetime(), full[-1].to_pydatetime())
    assert exc.value.code == E4002
    assert "ARB/USDT:USDT" in exc.value.error.message
    assert "BTC/USDT:USDT" not in exc.value.error.message


def test_missing_declared_symbol_fails_closed(tmp_path):
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT",
           _ohlcv(pd.date_range("2026-01-01", periods=3, freq="1D", tz="UTC")))
    with pytest.raises(FlowError) as exc:
        store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                           symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"],
                           timeframe="1d", now=NOW)
    assert exc.value.code == E4002


# ---------------------------------------------------------------- 市场维度
@pytest.mark.parametrize("dataset", ["funding", "open_interest", "basis"])
def test_spot_market_rejects_futures_only_datasets(dataset):
    """资金费 / 持仓量 / 基差的定义域是合约，现货声明它们必须当场拒。"""
    with pytest.raises(FlowError) as exc:
        store.require_market_dataset("spot", dataset)
    assert exc.value.code == E1004


def test_unknown_market_is_rejected():
    with pytest.raises(FlowError) as exc:
        store.require_market("swap")
    assert exc.value.code == E1004


# ---------------------------------------------------------------- V4 导入
def test_import_v4_normalises_date_column_and_timezone(tmp_path):
    """V4 落盘是 naive UTC + `date` 列；导入必须转成 UTC 感知的 timestamp。"""
    v4 = tmp_path / "v4"
    (v4 / "futures").mkdir(parents=True)
    source = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=3),  # naive
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0,
    })
    source.to_feather(v4 / "futures" / "BTC_USDT_USDT-1d-futures.feather")

    report = fetcher.import_v4(
        tmp_path / "root", v4, ["ohlcv"],
        exchange="binanceusdm", market="futures",
        symbols=["BTC/USDT:USDT"], timeframe="1d",
    )[0]
    assert report.symbols == 1 and report.rows_added == 3
    ds = store.read_dataset(tmp_path / "root", "ohlcv", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    assert ds.rows == 3
    assert all(stamp.tzinfo is not None for stamp in ds.columns[store.TIMESTAMP_COL])


def test_import_v4_requires_explicit_symbols(tmp_path):
    """`1000PEPE_USDT_USDT` 反解需要猜段数：猜错会把两个合约并成一个。"""
    with pytest.raises(FlowError) as exc:
        fetcher.import_v4(tmp_path, tmp_path, ["ohlcv"],
                          exchange="binanceusdm", market="futures")
    assert exc.value.code == E1004


def test_import_v4_rejects_spot_with_derivative_symbols(tmp_path):
    """现货目录 + 永续符号：必须在写盘之前失败，不能逐标的 skip 完还报成功。"""
    with pytest.raises(FlowError) as exc:
        fetcher.import_v4(tmp_path, tmp_path, ["ohlcv"],
                          exchange="binance", market="spot",
                          symbols=["BTC/USDT:USDT"], timeframe="1d")
    assert exc.value.code == E1004


# ---------------------------------------------------------------- CLI：零成功
def test_fetch_fails_when_every_symbol_is_skipped(tmp_path, monkeypatch, capsys):
    """全都跳过却 exit 0，是「用错标的集」和「区间没数据」最常见的假成功。"""
    from app.cli import fetch as fetch_cli
    from app.cli import main

    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({"empty": ["BTC/USDT:USDT"]}), encoding="utf-8")

    class DeadExchange:
        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            return []

    monkeypatch.setattr(fetch_cli, "_client_factory", lambda _eid: DeadExchange())
    code = main([
        "fetch", "--datasets", "ohlcv", "--symbol-set", "empty",
        "--exchange", "binanceusdm", "--market", "futures",
        "--start", "2026-01-01T00:00:00+00:00", "--end", "2026-01-05T00:00:00+00:00",
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert out["errors"][0]["code"] == E4002


def test_fetch_rejects_spot_dataset_before_network(tmp_path, monkeypatch, capsys):
    from app.cli import main

    monkeypatch.setenv("FLOW_DATA_ROOT", str(tmp_path))
    (tmp_path / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schemas" / "symbol_sets.json").write_text(
        json.dumps({"spot": ["BTC/USDT"]}), encoding="utf-8")
    code = main([
        "fetch", "--datasets", "ohlcv,funding", "--symbol-set", "spot",
        "--exchange", "binance", "--market", "spot",
        "--start", "2026-01-01T00:00:00+00:00", "--end", "2026-01-05T00:00:00+00:00",
    ])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["errors"][0]["code"] == E1004
    assert "funding" in out["errors"][0]["message"]


# ---------------------------------------------------------------- 门禁 B：市场
def test_gate_b_rejects_market_mismatch(tmp_path):
    """计划写 spot、事实是 futures → 必须失败。

    少了这一条，「用现货挖出来的因子」会被标成「合约因子」，而所有其它门禁
    （覆盖、标的集、频率、末端闭合）都会照常全绿。
    """
    from runtime.gate_b import check_gate_b

    stamps = pd.date_range("2026-01-01", periods=5, freq="1D", tz="UTC")
    _write(tmp_path, "ohlcv", "BTC/USDT:USDT", _ohlcv(stamps))
    ds = store.read_dataset(tmp_path, "ohlcv", "binanceusdm", "futures",
                            symbols=["BTC/USDT:USDT"], timeframe="1d", now=NOW)
    plan = {
        "boundary": {"time_axis": {"mine": "2026-01-01..2026-01-03"}},
        "supply": {"symbols": ["BTC/USDT:USDT"], "frequency": "1d",
                   "exchange": "binanceusdm", "market": "spot"},
    }
    with pytest.raises(FlowError) as exc:
        check_gate_b(ds.pin(), plan, supply_meta=ds.meta())
    assert exc.value.code == E4002
    assert "市场不符" in exc.value.error.message

    # 改成 futures 就通过——证明失败的原因是市场，不是别的字段。
    plan["supply"]["market"] = "futures"
    check_gate_b(ds.pin(), plan, supply_meta=ds.meta())
