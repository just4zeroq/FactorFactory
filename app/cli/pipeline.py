"""flow pipeline —— 体检通过后再跑（doctor → run）。

存在的理由只有一个：**别用坏数据跑一次完整执行**。供给有缺口或末端未闭合时，
run 不会失败——它会照常产出候选、照常裁决，只是结论建立在一段有洞的时间轴上。
把体检放在前面，让「数据有问题」在烧掉一次执行之前暴露。

两段之间的判定只看 `passed`（末端闭合 + 无缺口），不看行数偏少的告警：
薄标的会让分位分母偏移，但不该拦住整条流水线——那是研究选择，不是数据故障。

`--force` 跳过体检。它的唯一正当用途是「已知缺口且明确接受」，因此结果里
永远带 `doctor_skipped: true`，不假装体检过。

S6 告警（15-operations §3）：run 结束后按阈值表评估告警，经 Notifier
（F7 去重/限流）投递并写进本命令的信封——`pipeline_<ts>.json` 是唯一
可信的「跑过没有」证据，告警也必须落在同一份证据里，而不是只在终端喊。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.store import resolve_data_root
from storage.layout import Layout

from .common import envelope_ok
from .doctor import cmd_doctor
from .run import cmd_run


def _history_n_trials(runs_dir: Path) -> list[int]:
    """历次 run 的 Search n_trials（升序，含本次之前）——供突降对比。"""
    out: list[int] = []
    if not runs_dir.exists():
        return out
    for ticket in sorted(runs_dir.iterdir()):
        for parcel in ticket.glob("parcels/04-Search-*/payload.json"):
            try:
                val = json.loads(parcel.read_text(encoding="utf-8")).get("n_trials")
            except (ValueError, OSError):
                continue
            if isinstance(val, int) and val > 0:
                out.append(val)
    return out


def collect_alerts(run_data: dict[str, Any], layout: Layout,
                   *, n_trials_drop_ratio: float | None = None) -> list[dict[str, Any]]:
    """按 15-operations §3 阈值表评估本轮告警。只评估不裁决：告警不许静音
    （I52），处置归人。

    G11：腰斩阈值不再写死在 `0.5` 字面量里。缺省值在
    `registry/params.json` 的 `defaults.pipeline`——和 GP 种群、分位边界、
    求解器正则共用同一套「实现缺省」机制。调用方可以传 `n_trials_drop_ratio`
    覆盖（例如文档阈值改了还没同步注册表时），但实现里不再有第二份真相。
    """
    from capability.params import implementation_defaults

    ratio = (float(n_trials_drop_ratio) if n_trials_drop_ratio is not None
             else float(implementation_defaults("pipeline")["n_trials_drop_ratio"]))
    if not 0.0 < ratio < 1.0:
        raise ValueError(f"n_trials_drop_ratio 必须在 (0,1)，得到 {ratio}")
    alerts: list[dict[str, Any]] = []
    state = str(run_data.get("state") or "")
    skip = run_data.get("skip_reason")

    # 零候选 / 零晋升（§3：一轮 0 候选；连续 3 轮 0 晋升见 factors/promote）
    if skip == "no_candidate_passed" or state == "skipped":
        alerts.append({
            "kind": "zero_candidates", "severity": "warn",
            "message": f"本轮无候选过闸（state={state}, skip={skip}）；"
                       "先查信号量级再谈门槛（15-operations §4-6 红线）",
        })
    if state == "done" and skip is None:
        pass  # 正常完成：§3 的晋升数阈值归 promote/factors 链，不在 run 评估

    # 试验计数突降（§3：>50%）——_history_n_trials 已含本轮（run 刚落盘），
    # 取末两位对比：current=history[-1]，prev=history[-2]。
    history = _history_n_trials(layout.runs)
    if len(history) >= 2:
        current, prev = history[-1], history[-2]
        if prev > 0 and current < prev * ratio:
            alerts.append({
                "kind": "n_trials_drop", "severity": "warn",
                "message": f"n_trials {prev} -> {current}（降幅 "
                           f"{1 - current / prev:.0%} > {1 - ratio:.0%}）；"
                           "计数链可能断了（§4-7）",
            })

    # DSR 全失败（§3 标注「常见且正常」）：信息级，提醒量级差距而不是故障
    if run_data.get("dsr_all_rejected"):
        alerts.append({
            "kind": "dsr_all_fail", "severity": "info",
            "message": "全部候选 DSR 失败：门槛与信号量级不匹配（常见且正常）；"
                       "提高信号量级，不下调门槛",
        })
    return alerts


def _emit_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """经 F7 设施投递（同 key 去重 + 窗口限流），返回带投递状态的通知清单。

    `ticket` 作用域合法（station/probe 禁止通知，F10）；无外部 sink 时
    Notifier 视为已送达——信封里的 alerts 本身就是持久证据。
    """
    # F1：告警设施从唯一工厂取，不再在 CLI 里另造一个不同配置的 Notifier。
    from facilities.notify import build_notifier

    notifier = build_notifier("ticket")
    delivered: list[dict[str, Any]] = []
    for alert in alerts:
        result = notifier.emit(
            f"alert.{alert['kind']}",
            {"status": alert["severity"], "error_code": None, "seconds": 0.0},
            dedup_key=alert["kind"],
        )
        delivered.append({**alert, "notified": result.delivered,
                          "deduped": result.deduped,
                          "notes": list(notifier.trace_notes)})
        notifier.trace_notes.clear()
    return delivered


def cmd_pipeline(args: Any) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    force = bool(getattr(args, "force", False))

    if force:
        steps.append({"step": "doctor", "skipped": True,
                      "reason": "--force", "ok": True})
    else:
        doctor = cmd_doctor(args)
        data = doctor.get("data") or {}
        checks = data.get("checks") or []
        blocking = [
            {"dataset": c.get("dataset"), "market": c.get("market"),
             "timeframe": c.get("timeframe"), "reason": c.get("reason_code"),
             "gaps": c.get("gap_count"), "end_closed": c.get("end_closed")}
            for c in checks if not c.get("passed")
        ]
        steps.append({
            "step": "doctor", "ok": not blocking,
            "groups": len(checks), "blocking": blocking,
            "warnings": len(data.get("warnings") or []),
        })
        if blocking:
            return envelope_ok("pipeline", None, {
                "steps": steps,
                "state": "stopped",
                "reason": "supply_not_ready",
            })

    run = cmd_run(args)
    run_data = run.get("data") or {}
    steps.append({
        "step": "run", "ok": bool(run.get("ok")),
        "run_id": run.get("run_id"),
        "state": run_data.get("state"),
        "skip_reason": run_data.get("skip_reason"),
        "parcels": run_data.get("parcels"),
        "live": run_data.get("live"),
    })
    if not run.get("ok"):
        # run 的失败已经带着自己的错误对象；pipeline 只补上"停在哪一步"，
        # 不吞掉原始错误（吞掉会让 exit_code 与原因对不上）。
        return {
            **run,
            "command": "pipeline",
            "data": {"steps": steps, **(run_data or {})},
        }
    layout = Layout(resolve_data_root())
    alerts = _emit_alerts(collect_alerts(run_data, layout))
    return envelope_ok("pipeline", run.get("run_id"), {
        "steps": steps,
        "state": run_data.get("state"),
        "skip_reason": run_data.get("skip_reason"),
        "parcels": run_data.get("parcels"),
        "doctor_skipped": force,
        "alerts": alerts,
    })
