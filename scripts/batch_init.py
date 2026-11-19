"""L0 数据基座初始化：全市场日 K 线 + OI 的批量、可续跑执行器。

三条初始动作（元数据已由 data_cli universe 单独完成）：
1. 日 K 线：um + spot 活跃非稳定币，近 5 年，data vision 直连。
2. OI：仅 um（现货无 OI），ccxt 经代理拉最近窗口。

设计：
- 标的清单来自 <root>/schemas/{um,spot}_active_nostable.txt（universe 筛出）。
- 按 BATCH 分批，每批一个子进程调 data_cli，避免单次过长、便于观察进度。
- 每批完成记 done 标记；重跑跳过已完成批次（append-only 幂等，只补缺口）。
- 全部日志追加到 <root>/base/batch_init.log，带时间戳。

用法：
    venv/Scripts/python.exe scripts/batch_init.py            # 全量
    venv/Scripts/python.exe scripts/batch_init.py --dry-run  # 只打印计划
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# 数据根：与 .env 的 FLOW_DATA_ROOT 一致（用绝对路径，避免 cwd 漂移）。
ROOT = Path("C:/Users/lansa/factorfactory")
SCHEMAS = ROOT / "schemas"
STATE = ROOT / "base" / "batch_state"
LOG = ROOT / "base" / "batch_init.log"
PROXY = "http://127.0.0.1:7890"  # 币安 API 仅此代理可通；data vision 直连

START = "2021-11-19"
END = "2026-11-19"
BATCH = 40  # 每批标的数；控制单批时长与失败影响面
OI_SINCE_DAYS = 30  # OI 交易所仅留约 30 天，拉满


def log(msg: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_symbols(path: Path) -> list[str]:
    return [s.strip() for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def done_key(kind: str, market: str, idx: int) -> str:
    return f"{kind}:{market}:{idx:04d}"


def is_done(key: str) -> bool:
    return (STATE / f"{key}.done").exists()


def mark_done(key: str) -> None:
    (STATE / f"{key}.done").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


def run_cli(argv: list[str]) -> tuple[int, str]:
    """子进程跑 data_cli，返回 (exit_code, stdout末段)。"""
    cmd = [sys.executable, "-m", "app.data_cli", *argv]
    proc = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parent.parent),
                          capture_output=True, text=True, timeout=6 * 3600)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        log(f"  STDERR: {(proc.stderr or '')[-800:]}")
    return proc.returncode, out[-1200:]


def summarize(stdout: str) -> str:
    """从 CLI 的 json 回执里挑关键计数。"""
    try:
        env = json.loads(stdout)
        data = env.get("data", {})
        if "datasets" in data:
            ds = data["datasets"][0]
            return (f"rows_added={ds.get('rows_added', ds.get('rows', '?'))} "
                    f"failed={ds.get('failed', 0)} errors={ds.get('errors', 0)} "
                    f"missing={ds.get('missing', 0)}")
        return f"ok={env.get('ok')} exit={env.get('exit_code')}"
    except Exception:
        return f"(unparsed) {stdout[-200:]}"


def batch_kline(market: str, symbols: list[str], dry: bool) -> None:
    kind = "kline"
    batches = [symbols[i:i + BATCH] for i in range(0, len(symbols), BATCH)]
    log(f"[{kind}:{market}] {len(symbols)} 标的 / {len(batches)} 批 / {START}..{END}")
    for idx, batch in enumerate(batches):
        key = done_key(kind, market, idx)
        if is_done(key):
            log(f"  batch {idx + 1}/{len(batches)} SKIP (done)")
            continue
        if dry:
            log(f"  batch {idx + 1}/{len(batches)} WOULD RUN {len(batch)} symbols")
            continue
        log(f"  batch {idx + 1}/{len(batches)} RUN {len(batch)} symbols")
        rc, out = run_cli([
            "market-init", "--datasets", "klines",
            "--symbols", ",".join(batch),
            "--start", START, "--end", END, "--timeframe", "1d",
            "--market", market, "--workers", "8", "--format", "json",
        ])
        log(f"    rc={rc} {summarize(out)}")
        if rc == 0:
            mark_done(key)
        else:
            log(f"    批次失败，稍后重跑会重试本批")


def batch_oi(market: str, symbols: list[str], dry: bool) -> None:
    kind = "oi"
    batches = [symbols[i:i + BATCH] for i in range(0, len(symbols), BATCH)]
    log(f"[{kind}:{market}] {len(symbols)} 标的 / {len(batches)} 批 / since={OI_SINCE_DAYS}d")
    for idx, batch in enumerate(batches):
        key = done_key(kind, market, idx)
        if is_done(key):
            log(f"  batch {idx + 1}/{len(batches)} SKIP (done)")
            continue
        if dry:
            log(f"  batch {idx + 1}/{len(batches)} WOULD RUN {len(batch)} symbols")
            continue
        log(f"  batch {idx + 1}/{len(batches)} RUN {len(batch)} symbols")
        rc, out = run_cli([
            "market-update", "--datasets", "open_interest",
            "--symbols", ",".join(batch),
            "--since-days", str(OI_SINCE_DAYS), "--market", market,
            "--proxy", PROXY, "--format", "json",
        ])
        log(f"    rc={rc} {summarize(out)}")
        if rc == 0:
            mark_done(key)
        else:
            log(f"    批次失败，稍后重跑会重试本批")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=("kline", "oi", "all"), default="all")
    args = ap.parse_args()

    um = load_symbols(SCHEMAS / "um_active_nostable.txt")
    spot = load_symbols(SCHEMAS / "spot_active_nostable.txt")
    log(f"=== 初始化开始 dry_run={args.dry_run} only={args.only} um={len(um)} spot={len(spot)} ===")

    if args.only in ("kline", "all"):
        batch_kline("futures", um, args.dry_run)  # um 日 K
        batch_kline("spot", spot, args.dry_run)    # spot 日 K
    if args.only in ("oi", "all"):
        batch_oi("futures", um, args.dry_run)      # um OI（现货无 OI）

    log("=== 初始化全部完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
