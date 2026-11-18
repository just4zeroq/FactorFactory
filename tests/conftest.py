"""测试夹具：在 ``tmp_path`` 里造真实可读的 V5 供给。

方案 B 之后「有没有数据」就等于「目录里有没有 parquet 文件」：编译期要现算
checksum 才能得到 ``blueprint_digest``，所以任何走 CLI 的测试都必须有真数据，
不能用一个假别名表混过去。

写入的是最小的真数据：等距 bar + 可选的三个事件流，末根落在 ``end`` 上
（保证 ``end_closed``）。
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

# `tests/reference/` 里冻结着 V4 的统计实现，只作数值对拍 oracle 使用。
# 把它挂上 sys.path，测试里就能写 `from reference.v4 import significance`。
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


@pytest.fixture()
def v5_supply(tmp_path):
    """返回 ``write(**kwargs)``，参数缺省即 ``tests/fixtures/v5/plan_valid.json``
    的声明（4 个数据集、binance/futures、1d）。"""

    def write(
        *,
        symbols=("BTC/USDT:USDT", "ETH/USDT:USDT"),
        datasets=("ohlcv", "funding", "open_interest", "basis"),
        start="2024-01-01",
        end="2026-09-13",
        timeframe="1d",
        exchange="binanceusdm",
        market="futures",
    ) -> None:
        from v5.upstream import store

        stamps = pd.date_range(start, end, freq="1D", tz="UTC")
        for dataset in datasets:
            factory = {
                "ohlcv": lambda i: {
                    "open": 100.0 + i,
                    "high": 101.0 + i,
                    "low": 99.0 + i,
                    "close": 100.5 + i,
                    "volume": 1e3,
                },
                "funding": lambda i: {"funding_rate": 1e-4},
                "open_interest": lambda i: {"open_interest": 1e6},
                "basis": lambda i: {"basis": 1e-3},
            }[dataset]
            for index, symbol in enumerate(symbols):
                # 事件流每 8 根 bar 才有一个观测（真实资金费就是这样，间隔会变）
                step = 1 if dataset == "ohlcv" else 8
                picks = list(range(0, len(stamps), step))
                sample = factory(index)
                frame = pd.DataFrame(
                    {
                        store.TIMESTAMP_COL: stamps[picks],
                        **{name: [value for _ in picks] for name, value in sample.items()},
                    }
                )
                target = store.dataset_file(
                    tmp_path,
                    dataset,
                    exchange,
                    market,
                    symbol,
                    timeframe if dataset == store.BAR_DATASET else None,
                )
                store.upsert(target, frame, dataset, symbol=symbol)

    return write
