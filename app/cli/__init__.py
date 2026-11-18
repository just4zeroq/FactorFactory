"""V5 CLI 组装（09-app-storage.md §2）。

命令实现逐条拆到 `cli/<name>.py`，元数据在 `app/contract.py`，渲染在 `app/render/`。
本模块只做三件事：声明参数 schema、分派、渲染信封 + 推导进程码。

对外符号（build_parser / main / _DISPATCH / cmd_* / _envelope_* / _coerce /
CORE_COMMANDS）保持与 S0 单文件版本一致，迁移期调用方无需改动。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Callable

from app.contract import (
    ALIASES, ALL_COMMANDS, CORE_COMMANDS, assert_no_caliber_args, get as get_command,
)
from app.render import FORMATS, render
from contracts.errors import FlowError
from runtime.dotenv import load_dotenv

from .common import coerce as _coerce
from .common import envelope_error as _envelope_error
from .common import envelope_ok as _envelope_ok
from .common import load_plan as _load_plan
from .doctor import cmd_doctor
from .estimate import cmd_estimate
from .explain import cmd_explain
from .factor_db import cmd_factor_db
from .factors import cmd_factors
from .fetch import cmd_fetch
from .holdout import cmd_holdout
from .inspect import cmd_inspect
from .pipeline import cmd_pipeline
from .promote import cmd_promote
from .reconcile import cmd_reconcile
from .replay import cmd_backtest, cmd_certify, cmd_evaluate
from .replay_ops import cmd_replay
from .run import cmd_run
from .snapshot import cmd_snapshot
from .station import cmd_station
from .stations import cmd_stations
from .vision import cmd_vision

_DISPATCH: dict[str, Callable[[Any], dict[str, Any]]] = {
    "run": cmd_run, "explain": cmd_explain,
    "stations": cmd_stations, "station": cmd_station, "inspect": cmd_inspect,
    "fetch": cmd_fetch, "promote": cmd_promote, "factors": cmd_factors,
    "doctor": cmd_doctor, "estimate": cmd_estimate, "pipeline": cmd_pipeline,
    "holdout": cmd_holdout, "factor_db": cmd_factor_db,
    "reconcile": cmd_reconcile,
    "evaluate": cmd_evaluate, "backtest": cmd_backtest, "certify": cmd_certify,
    "snapshot": cmd_snapshot, "replay": cmd_replay,
    "vision": cmd_vision,
}

# 已声明但还没有入口的命令：给出一条能指路的拒绝，而不是让 KeyError 变成
# 一句 "KeyError: 'backtest'"。S7 起三条重算命令（evaluate/backtest/certify）
# 已接通（`replay.py`，基于 rebuild_upstream 的对象重建），这里只留兜底结构：
# 将来新增命令但没接分派时，仍然给出可读原因而不是裸异常。
_UNIMPLEMENTED_HINT: dict[str, str] = {}


def _add_plan_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--plan", required=True)
    sp.add_argument("--set", action="append", default=[], help="key=value 覆盖")
    sp.add_argument("--no-strict", action="store_true",
                    help="跳过必填字段检查（跨字段一致性仍校验）")
    sp.add_argument("--allow-constitutive-override", action="store_true")
    sp.add_argument("--live", action="store_true",
                    help="调真实能力层（需已 fetch 采集供给；默认走 dummy 桩）")
    sp.add_argument("--format", choices=FORMATS, default="json", help="输出格式")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flow", description="V5 CLI（S1 真实调用单）")
    sub = p.add_subparsers(dest="command", required=True)

    for name in ("run", "explain", "pipeline"):
        _add_plan_args(sub.add_parser(name))
    # pipeline 额外两件：体检范围与「已知问题照跑」的显式开关
    sub.choices["pipeline"].add_argument("--dataset", help="只体检指定数据集")
    sub.choices["pipeline"].add_argument("--force", action="store_true",
                                         help="跳过体检（结果里会标记 doctor_skipped）")

    sp = sub.add_parser("doctor", help="供给体检：覆盖 / 缺口 / 末端闭合")
    sp.add_argument("--plan", help="同时核对计划声明的数据集与标的集")
    sp.add_argument("--dataset", help="只看这些数据集（逗号分隔）")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("estimate", help="按 caliber.net 估算每期/年化成本")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--turnover", type=float, default=1.0,
                    help="每期单边换手（默认 1.0 = 全部换掉）")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("station")
    sp.add_argument("name")
    sp.add_argument("--on", required=True, help="ticket id")
    sp.add_argument("--probe", action="store_true")
    sp.add_argument("--commit", action="store_true")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("inspect")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--station", help="只看某一工位（Boundary/Panel/.../Certify）")
    sp.add_argument("--full", action="store_true", help="带完整 payload")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("promote", help="把通过裁决的候选写进因子库")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--candidate", action="append", default=[],
                    help="只晋升指定 candidate_id（可重复）；默认全部 pass/watch")
    sp.add_argument("--min-grade", default="any",
                    choices=("any", "out_of_sample", "in_sample"))
    sp.add_argument("--yes", action="store_true", help="写账本必须显式确认")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("factors", help="查因子库")
    sp.add_argument("--status", default="active",
                    choices=("active", "retired", "all"))
    sp.add_argument("--limit", type=int)
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("holdout", help="揭示保留区（只揭示一次）")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--set", action="append", default=[])
    sp.add_argument("--no-strict", action="store_true")
    sp.add_argument("--allow-constitutive-override", action="store_true")
    sp.add_argument("--yes", action="store_true", help="写账本必须显式确认")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("factor_db", help="因子库管理（下架 / 统计）")
    sp.add_argument("--retire", help="要下架的 factor_id")
    sp.add_argument("--note", default="", help="下架原因（写进库）")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--format", choices=FORMATS, default="json")

    # 三条"重算单点结论"的命令：只认冻结 ticket，不接 --plan（与 station 同一
    # 取舍——否则"重算"就成了"换参数再算一遍"）。evaluate 可收窄到单个候选。
    for name, helptext in (
        ("evaluate", "重算候选表达式暴露与 IC/ICIR"),
        ("backtest", "重算组合净表现（combine → allocate → backtest）"),
        ("certify", "重算 walk-forward 认证（写账本需 --yes）"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--on", required=True,
                        help="ticket id（只认已落盘的正式产物，不读 steps/）")
        if name == "evaluate":
            sp.add_argument("--candidate", help="只算指定 candidate_id")
        if name == "certify":
            sp.add_argument("--yes", action="store_true", help="写账本必须显式确认")
        sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("reconcile", help="登记库/产物链/对象存储三方一致性核对")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("snapshot", help="数据快照管理（create/list/show/archive）")
    subs = sp.add_subparsers(dest="snapshot_action", required=True)
    c = subs.add_parser("create", help="从当前供给冻结一个快照")
    c.add_argument("--datasets", required=True, help="逗号分隔，如 ohlcv,funding")
    c.add_argument("--exchange", required=True)
    c.add_argument("--market", default="futures", choices=("futures", "spot"))
    c.add_argument("--symbols", help="逗号分隔；缺省扫描目录全部标的")
    c.add_argument("--timeframe", default="1d")
    c.add_argument("--parent", help="父快照 id")
    c.add_argument("--note", default="")
    c.add_argument("--no-enqueue", action="store_true",
                   help="只建清单，不自动把 active 因子排入复盘队列")
    c.add_argument("--format", choices=FORMATS, default="json")
    l = subs.add_parser("list", help="列出快照")
    l.add_argument("--status", choices=("active", "archived"))
    l.add_argument("--limit", type=int)
    l.add_argument("--format", choices=FORMATS, default="json")
    sh = subs.add_parser("show", help="查看完整清单")
    sh.add_argument("snapshot_id")
    sh.add_argument("--format", choices=FORMATS, default="json")
    a = subs.add_parser("archive", help="归档快照（新计划不可再引用）")
    a.add_argument("snapshot_id")
    a.add_argument("--yes", action="store_true")
    a.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("replay", help="因子在新快照上的复盘闭环")
    subs = sp.add_subparsers(dest="replay_action", required=True)
    e = subs.add_parser("enqueue", help="把待复验因子入队（幂等）")
    e.add_argument("--snapshot", help="目标快照 id；缺省最新 active")
    e.add_argument("--all-active", action="store_true",
                   help="全部 active 因子，而不只是新快照之后未认证的")
    e.add_argument("--factor", action="append", default=[],
                   help="收窄到指定 factor_id（可重复）")
    e.add_argument("--format", choices=FORMATS, default="json")
    r = subs.add_parser("run", help="执行一条复盘（写证书 + 报告）")
    r.add_argument("replay_id")
    r.add_argument("--format", choices=FORMATS, default="json")
    sh = subs.add_parser("show", help="查看复盘状态与报告")
    sh.add_argument("replay_id")
    sh.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("vision", help="全市场日 K 发现 → 筛选 → 拉齐到挖掘空间")
    vsub = sp.add_subparsers(dest="vision_action", required=True)
    vl = vsub.add_parser("list", help="ccxt 列全市场 USDT-M 永续清单")
    vl.add_argument("--proxy", help="代理（如 http://127.0.0.1:7890）")
    vl.add_argument("--format", choices=FORMATS, default="json")
    vs = vsub.add_parser("scan", help="探测每标的备份站最早月份并按历史长度筛选")
    vs.add_argument("--scan-start", default="2019-01", help="起始探测月 YYYY-MM")
    vs.add_argument("--scan-end", help="结束探测月 YYYY-MM（缺省当前月）")
    vs.add_argument("--min-days", type=int, default=720, help="最少历史天数（默认 720）")
    vs.add_argument("--write-symbol-set", help="把筛中标的写入 symbol_sets.json 的集合名")
    vs.add_argument("--refresh", action="store_true", help="强制重新拉全市场清单")
    vs.add_argument("--proxy", help="代理（如 http://127.0.0.1:7890）")
    vs.add_argument("--timeout", type=float, default=20, help="HEAD 探测超时秒数")
    vs.add_argument("--workers", type=int, default=8, help="并发探测线程数（默认 8）")
    vs.add_argument("--format", choices=FORMATS, default="json")
    vf = vsub.add_parser("fetch", help="按筛中清单从备份站下载 ohlcv/funding")
    vf.add_argument("--min-days", type=int, default=720, help="只下载历史≥此天数的标的")
    vf.add_argument("--datasets", default="ohlcv,funding", help="逗号分隔（仅 ohlcv/funding）")
    vf.add_argument("--exchange", default="binanceusdm")
    vf.add_argument("--market", default="futures", choices=("futures", "spot"))
    vf.add_argument("--timeframe", default="1d")
    vf.add_argument("--proxy", help="代理（如 http://127.0.0.1:7890）")
    vf.add_argument("--workers", type=int, default=8, help="并发下载线程数（默认 8）")
    vf.add_argument("--revise", action="store_true", help="覆盖已写历史")
    vf.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("stations", help="八工位清单（顺序与规格自省）")
    sp.add_argument("--format", choices=FORMATS, default="json")

    sp = sub.add_parser("fetch", help="四数据集采集（文件即事实）")
    sp.add_argument("--datasets", required=True,
                    help="逗号分隔：ohlcv,funding,open_interest,basis"
                         "（现货只允许 ohlcv）")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--symbols", help="逗号分隔的标的清单")
    grp.add_argument("--symbol-set", help="schemas/symbol_sets.json 中的标的集名")
    sp.add_argument("--exchange", required=True,
                    help="ccxt 交易所 id（不设默认，如 binance / binanceusdm）")
    sp.add_argument("--market", default="futures", choices=("futures", "spot"),
                    help="市场维度：futures（默认，四数据集）/ spot（仅 ohlcv）")
    sp.add_argument("--timeframe", default="1d", help="只对 ohlcv 生效")
    sp.add_argument("--start", help="RFC3339；从交易所采集时必填")
    sp.add_argument("--end", help="RFC3339；从交易所采集时必填")
    src = sp.add_mutually_exclusive_group()
    src.add_argument("--from-v4",
                     help="从 V4 feather 快照导入（如 data/binance）")
    src.add_argument("--from-binance-vision", action="store_true",
                     help="从币安公开数据备份站 data.binance.vision 下载月度归档"
                          "（仅 ohlcv/funding；带 sha256 校验）")
    sp.add_argument("--proxy", help="下载代理（如 http://127.0.0.1:7890）；"
                                    "binance-vision 来源用，缺省读环境 https_proxy")
    sp.add_argument("--revise", action="store_true",
                    help="覆盖已写历史（默认 append-only，冲突只计数）")
    sp.add_argument("--limit", type=int, default=1000, help="每页根数")
    sp.add_argument("--spot-exchange", help="basis 用的现货交易所 id（默认 binance）")
    sp.add_argument("--format", choices=FORMATS, default="json")

    # P1 / I30：启动期静态扫描，任何命令出现口径开关立即 E1020。
    declared: list[str] = []
    for action in p._actions:  # noqa: SLF001 - argparse 无公开 API 枚举全部开关
        declared.extend(getattr(action, "option_strings", []) or [])
    for sp_cmd in sub.choices.values():
        for action in sp_cmd._actions:  # noqa: SLF001
            declared.extend(getattr(action, "option_strings", []) or [])
    assert_no_caliber_args(declared)

    # 元数据存在性：命令面清单与分派表必须一致（未实现的业务命令声明在 contract）。
    for name in ALL_COMMANDS:
        get_command(name)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # 旧名迁移（09 §2.2）：experiment → run。别名在分派前归一，
    # 否则 contract.get 认得它、argparse 却不认，别名等于死代码。
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ALIASES:
        raw[0] = ALIASES[raw[0]]
    args = parser.parse_args(raw)
    fmt = getattr(args, "format", "json")
    try:
        # 组合根：把 .env 注入进程环境。数据根、凭据校验、BLAS 线程快照等
        # 所有读 os.environ 的地方共用同一份本地配置；已存在的进程变量优先。
        # 放在 try 内，配置写错会走信封报错（E1004 / 退出码 2），不是裸栈回溯。
        load_dotenv()
        handler = _DISPATCH.get(args.command)
        if handler is None:
            raise FlowError(
                "E3004", f"{args.command} 尚未接通执行入口",
                path=f"{args.command}",
                hint=_UNIMPLEMENTED_HINT.get(
                    args.command, "该命令已声明元数据，实现随阶段补齐"),
            )
        envelope = handler(args)
    except FlowError as exc:
        envelope = _envelope_error(args.command, None, [exc.error.to_dict()])
    except Exception as exc:  # 未预期异常必须显式失败，不得静默成功
        envelope = _envelope_error(args.command, None, [{
            "code": "E3004", "layer": "execution", "retryable": False,
            "message": f"{type(exc).__name__}: {exc}",
        }])
    print(render(envelope, fmt))
    return int(envelope["exit_code"])


__all__ = [
    "build_parser", "main", "_DISPATCH", "CORE_COMMANDS", "ALL_COMMANDS",
    "cmd_run", "cmd_explain", "cmd_stations", "cmd_station", "cmd_inspect",
    "_envelope_ok", "_envelope_error", "_coerce", "_load_plan",
]
