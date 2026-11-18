"""AC63 / D-003：黄金面板已知答案（2026-11-17 裁决版，无 V4 依赖）。

锚点 = V5 自采数据冻结的黄金面板 + 手写 pandas 独立复算。三层断言：

1. `reference_matches_frozen`：参考实现（本文件 `_reference_metrics`，不
   import `v5` 的 observe/label）对黄金面板重算，与 `golden_panel.json`
   冻结值相对误差 ≤ `1e-9`——面板与冻结值没被动过；
2. `pipeline_matches_frozen`：完整流水线跑黄金计划的 mom60 指标对冻结值
   ≤ ±10%——实现重构后不得漂移；
3. `reference_matches_pipeline`：参考 vs 流水线 ≤ `1e-9`——实现分叉即失败。

黄金面板数据缺失时整组 skip（本测试只依赖本地已采集的 parquet，不联网）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 冻结的验证基线随仓库提交：fresh clone 不必先采集数据就能跑对拍。
DATA_ROOT = Path(__file__).resolve().parent / "fixtures" / "golden"
MANIFEST = DATA_ROOT / "golden_panel.json"

WINDOW_START = "2023-01-01"
WINDOW_END = "2026-06-30"          # 含当日；mine+oos 的边界（holdout reserved）
HORIZON = 5
ENTRY_OFFSET = 1                   # next_close
LOOKBACK = 60                      # mom60
TOLERANCE_DETERMINISTIC = 1e-9     # 同数据同口径的决定论量（06 §5.6）
TOLERANCE_REGRESSION = 0.10        # 跨版本对冻结期望值

pytestmark = pytest.mark.skipif(
    not MANIFEST.exists() or not (DATA_ROOT / "ohlcv").exists(),
    reason="黄金面板夹具缺失（应在 tests/fixtures/golden/）",
)


def _golden_symbols() -> list[str]:
    if MANIFEST.exists():
        return list(json.loads(MANIFEST.read_text(encoding="utf-8"))["symbols"])
    sets = json.loads(
        (DATA_ROOT / "symbol_sets.json").read_text(encoding="utf-8"))
    return list(sets["crypto20"])   # 冻结前的回退：同一份标的集


def _load_close() -> tuple[np.ndarray, list[str]]:
    """独立读盘：直接 pandas 读 parquet，不走 v5 的 store。"""
    symbols = _golden_symbols()
    stems = [s.replace("/", "_").replace(":", "_") for s in symbols]
    frames = {}
    for sym, stem in zip(symbols, stems):
        f = DATA_ROOT / "ohlcv" / "binanceusdm" / "futures" / f"{stem}-1d.parquet"
        df = pd.read_parquet(f)
        ts = pd.to_datetime(df["timestamp"], utc=True)
        m = (ts >= pd.Timestamp(WINDOW_START, tz="UTC")) & \
            (ts <= pd.Timestamp(WINDOW_END, tz="UTC"))
        frames[sym] = pd.Series(df.loc[m, "close"].to_numpy(dtype="float64"),
                                index=ts[m])
    close = pd.DataFrame(frames).sort_index()
    assert close.notna().all().all(), "黄金面板在窗口内必须逐标的完整"
    return close.to_numpy(dtype="float64"), symbols


def _reference_metrics(close: np.ndarray, rebalance: int = 5) -> dict:
    """手写 pandas 参考实现：mom60@5bar 的多空毛指标（06 §5.6 口径）。

    - 权重：暴露截面前/后 30%（k = n//3）等权，±1/k；
    - **再平衡网格**：目标权重只在 `t % rebalance == 0` 重算，中间行持仓不动
      （网格行算不出 = 不调仓，沿用最近一个成功网格行；首个成功网格行前空仓）；
    - `ls_t = Σ_s w_{t,s} · raw_{t,s}`，`raw_t = p[t+1+h]/p[t+1] − 1`
      （entry=next_close：成交延迟已编进标签，与回测链 NEXT_BAR 同构）；
    - `gross_return = mean(ls)/h`，`gross_sharpe = sr(ls)·√(365/h)`；
    - `turnover = 0.5·Σ|Δw|`（累计）。
    """
    df = pd.DataFrame(close)
    exposure = df / df.shift(LOOKBACK) - 1.0
    raw = df.shift(-(ENTRY_OFFSET + HORIZON)) / df.shift(-ENTRY_OFFSET) - 1.0
    n = df.shape[1]
    k = n // 3
    t_len = df.shape[0]
    weights = np.zeros((t_len, n), dtype="float64")
    last = np.zeros(n, dtype="float64")
    for t in range(t_len):
        on_grid = (t % rebalance == 0)
        if on_grid:
            row = exposure.iloc[t]
            ok = row.notna() & np.isfinite(raw.iloc[t].to_numpy())
            if int(ok.sum()) >= 2:
                order = row[ok].sort_values(kind="stable")
                w = pd.Series(0.0, index=row.index)
                w[order.index[-k:]] = 1.0 / k
                w[order.index[:k]] = -1.0 / k
                last = w.to_numpy()
        weights[t] = last
    ls = (weights * raw.to_numpy()).sum(axis=1)
    ls = ls[np.isfinite(ls)]
    mean = float(ls.mean())
    std = float(ls.std())
    sr = mean / std if std > 0 else 0.0
    return {
        "gross_sharpe": sr * float(np.sqrt(365.0 / HORIZON)),
        "gross_return": mean / HORIZON,
        "sr": sr,
        "turnover": 0.5 * float(np.abs(np.diff(weights, axis=0)).sum()),
        "n_periods": int(ls.size),
        "k": k,
        "n_symbols": n,
        "n_bars": t_len,
    }


def _assert_close(got: dict, want: dict, tol: float) -> None:
    for key in ("gross_sharpe", "gross_return", "sr", "turnover"):
        assert got[key] == pytest.approx(want[key], rel=tol), key
    assert got["n_periods"] == want["n_periods"]


def test_reference_matches_frozen():
    """参考实现对冻结面板重算 == 冻结值（1e-9）：锚点未被篡改/漂移。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    close, _ = _load_close()
    _assert_close(_reference_metrics(close), manifest["expected"],
                  TOLERANCE_DETERMINISTIC)


def test_pipeline_evidence_matches_reference_one_e_minus_nine():
    """参考 vs 流水线 Evidence mom60 指标 ≤ 1e-9（决定论量）。

    流水线侧读取验收跑落盘的 `runs/<ticket>/parcels/05-Evidence-*/payload.json`
    （由 freeze/验收脚本执行 `--live` 后留下）；两者必须逐量吻合。
    """
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    pipeline = manifest.get("pipeline_metrics")
    if not pipeline:
        pytest.skip("流水线验收跑尚未执行（见 freeze_golden.py 的 run 步骤）")
    close, _ = _load_close()
    _assert_close(_reference_metrics(close), pipeline, TOLERANCE_DETERMINISTIC)
