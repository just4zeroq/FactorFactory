"""L0 数据基座全量初始化（研究口径，含已下架标的）。

四个阶段，全部可续跑：

1. ``kline``  — 合约 + 现货 1d K 线，data vision 月度归档，含已下架；
2. ``funding`` — 合约资金费率，data vision 月度归档，含已下架永续；
3. ``ccxt``   — 用 ccxt 补 11 月（归档未覆盖的最近段）K 线与资金费率；
4. ``oi``     — 合约未平仓量（仅 ccxt 有，交易所只保留约 30 天）。

历史段取 ``2021-11-19..2026-10-31``，整月归档；11 月 ``2026-11-01`` 起交给
ccxt 补，避免月度归档尚未生成时留缺口。ccxt 对已下架标的通常无历史，故补尾
阶段只跑 active 标的——它们本来就属于「当前月仍在交易」的集合。

设计：
- 标的清单直接读 ``<root>/base/universe/markets.jsonl``，按 ``market`` 隔离，
  不再依赖 schemas 里的 active 名单（那份名单会系统性剔除已下架币）；
- 按 ``BATCH`` 分批，每批一个子进程调 data_cli；批完成记 done 标记，重跑跳过；
- 日志追加到 ``<root>/base/batch_init_v2.log``，带时间戳。

用法：
    venv/Scripts/python.exe scripts/batch_init.py --dry-run
    venv/Scripts/python.exe scripts/batch_init.py --only kline
    venv/Scripts/python.exe scripts/batch_init.py --only all
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# 直接执行本脚本时 sys.path[0] 是 scripts/，补上项目根才能 import services。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 数据根：与 .env 的 FLOW_DATA_ROOT 一致（绝对路径，避免 cwd 漂移）。
ROOT = Path("C:/Users/lansa/factorfactory")
STATE = ROOT / "base" / "batch_state_v2"
LOG = ROOT / "base" / "batch_init_v2.log"
UNIVERSE = ROOT / "base" / "universe" / "markets.jsonl"
PROXY = "http://127.0.0.1:7890"  # 币安 API 仅此代理可通；data vision 直连

VISION_START = "2021-11-19"
VISION_END = "2026-10-31"       # 11 月留给 ccxt
CCXT_START = "2026-11-01"
BATCH = 40


def log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_universe(market: str) -> list[dict]:
    """读 universe 表，按市场过滤；保留全部状态（含 delisted / unknown）。"""
    from services import universe_service as us

    rows = [
        json.loads(line) for line in UNIVERSE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        r for r in rows
        if r.get("market") == market and us.is_storable(str(r.get("symbol") or ""))
    ]


def done_key(kind: str, market: str, idx: int) -> str:
    # Windows 文件名不允许冒号，旧格式 v2:kline:futures:0000 会让写标记崩溃。
    return f"v2_{kind}_{market}_{idx:04d}"


def is_done(key: str) -> bool:
    return (STATE / f"{key}.done").exists()


def mark_done(key: str) -> None:
    (STATE / f"{key}.done").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


def run_cli(argv: list[str]) -> tuple[int, str]:
    """子进程跑 data_cli，返回 (exit_code, stdout 末段)。"""
    cmd = [sys.executable, "-m", "app.data_cli", *argv]
    proc = subprocess.run(
        cmd, cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=6 * 3600,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        log(f"  STDERR: {(proc.stderr or '')[-1000:]}")
    return proc.returncode, out[-1500:]


def summarize(stdout: str) -> str:
    """从 CLI 的 json 回执里挑关键计数。"""
    try:
        env = json.loads(stdout)
        data = env.get("data") or {}
        if "datasets" in data:
            ds = data["datasets"][0]
            return (f"rows_added={ds.get('rows_added', ds.get('rows', '?'))} "
                    f"failed={ds.get('failed', 0)} errors={ds.get('errors', 0)} "
                    f"missing={ds.get('missing', 0)}")
        return f"ok={env.get('ok')} exit={env.get('exit_code')}"
    except Exception:
        return f"(unparsed) {stdout[-200:]}"


def batch_run(
    kind: str,
    market: str,
    symbols: list[str],
    base_argv: list[str],
    dry: bool,
    batch_size: int,
) -> None:
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    log(f"[{kind}:{market}] {len(symbols)} 标的 / {len(batches)} 批")
    for idx, batch in enumerate(batches):
        key = done_key(kind, market, idx)
        if is_done(key):
            log(f"  batch {idx + 1}/{len(batches)} SKIP (done)")
            continue
        if dry:
            log(f"  batch {idx + 1}/{len(batches)} WOULD RUN {len(batch)} symbols")
            continue
        log(f"  batch {idx + 1}/{len(batches)} RUN {len(batch)} symbols")
        rc, out = run_cli([*base_argv, "--symbols", ",".join(batch)])
        log(f"    rc={rc} {summarize(out)}")
        if rc == 0:
            mark_done(key)
        else:
            log("    批次失败，重跑会重试本批（已完成部分幂等）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=("kline", "funding", "ccxt", "oi", "all"),
                    default="all")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    futures = load_universe("futures")
    spot = load_universe("spot")
    log(f"=== 初始化开始 dry_run={args.dry_run} only={args.only} "
        f"futures={len(futures)} spot={len(spot)} ===")

    if args.only in ("kline", "all"):
        common = [
            "market-init", "--datasets", "klines",
            "--start", VISION_START, "--end", VISION_END, "--timeframe", "1d",
            "--include-delisted", "--workers", "24", "--format", "json",
        ]
        batch_run("kline", "futures", [str(r["symbol"]) for r in futures],
                  [*common, "--market", "futures"], args.dry_run, args.batch)
        batch_run("kline", "spot", [str(r["symbol"]) for r in spot],
                  [*common, "--market", "spot"], args.dry_run, args.batch)

    if args.only in ("funding", "all"):
        # fundingRate 归档只有永续合约有；非永续（242 个）跳过，省掉整批 404。
        perps = [
            str(r["symbol"]) for r in futures
            if r.get("contract_type") == "PERPETUAL"
        ]
        batch_run("funding", "futures", perps, [
            "market-init", "--datasets", "funding",
            "--start", VISION_START, "--end", VISION_END,
            "--market", "futures", "--include-delisted",
            "--workers", "24", "--format", "json",
        ], args.dry_run, args.batch)

    if args.only in ("ccxt", "all"):
        active_f = [str(r["symbol"]) for r in futures if r.get("status") == "active"]
        active_s = [str(r["symbol"]) for r in spot if r.get("status") == "active"]
        for market, symbols in (("futures", active_f), ("spot", active_s)):
            batch_run("ccxt_kline", market, symbols, [
                "market-update", "--datasets", "klines",
                "--start", CCXT_START, "--market", market,
                "--proxy", PROXY, "--format", "json",
            ], args.dry_run, args.batch)
        batch_run("ccxt_funding", "futures", active_f, [
            "market-update", "--datasets", "funding",
            "--start", CCXT_START, "--market", "futures",
            "--proxy", PROXY, "--format", "json",
        ], args.dry_run, args.batch)

    if args.only in ("oi", "all"):
        active_f = [str(r["symbol"]) for r in futures if r.get("status") == "active"]
        batch_run("oi", "futures", active_f, [
            "market-update", "--datasets", "open_interest",
            "--since-days", "30", "--market", "futures",
            "--proxy", PROXY, "--format", "json",
        ], args.dry_run, args.batch)

    log("=== 初始化全部完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
