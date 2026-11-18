"""回测复盘服务层（docs/21-snapshot-replay-catalog.md §2）。

复盘对象严格限定为「当前 active 因子 × 上次认证后产生的新快照」（§2.1）：
历史 run、被拒候选、watch、已 retired 因子都不进队列。

`run()` 的真实重算通过 `compute` 可调用对象注入：默认实现 `default_compute`
复用现有 `flow run` 能力链——把因子的来源计划改写为「目标快照 + 该因子作为
唯一先验」，跑出一张新 ticket，再从 Evidence/Verdict/Certify 产物里取该因子
的指标。测试注入确定性的 compute 即可覆盖 diff / 决策 / 失败不改证书，而不
依赖真实行情数据。

决策阈值来自配置（§2.3「写进配置，不写死在代码里」）：`DEFAULT_THRESHOLDS`
是出厂值，调用方可用 `thresholds=` 覆盖；diff 与阈值都落进报告。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts.errors import E1004, E3005, E4002, FlowError
from storage.catalog import read_ticket
from storage.layout import Layout
from storage.sqlite_store import DB_FILENAME, SqliteStore, snapshot_json

from .factor_service import FactorService
from .snapshot_service import SnapshotService


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_digest(text: str) -> str:
    """稳定短 id：用 sha256（contracts.digest 已有），不引入新摘要算法。"""
    from contracts.digest import blob_digest
    return blob_digest(text.encode("utf-8")).split(":", 1)[-1][:10]


#: 出厂阈值：可被调用方整体替换。退化判据只在「显著」时才降权，避免噪音告警。
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "ic_drop": 0.02,            # IC 绝对值下降超过此值 → degraded
    "dsr_drop": 0.05,
    "net_return_drop": 0.05,   # 净收益相对/绝对下降
    "cost_adj_return_drop": 0.02,
    "cost_gate_min": None,     # None=用计划里 verdict.cost_adj_min；可显式覆盖
}

#: 逐指标 diff 时比较的指标名（缺失记 None，不参与判据）。
DIFF_METRICS = (
    "ic", "rank_ic", "dsr", "hac_t", "p_adj", "cost_adj_return",
    "net_return", "net_sharpe", "max_drawdown", "turnover", "fold_count",
)

#: compute 的返回结构：{metrics, evidence_grade, oos_passed, cost_gate_passed, run_id}
ComputeFn = Callable[[Mapping[str, Any], str, Mapping[str, Any]], dict[str, Any]]


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def diff_metrics(previous: Mapping[str, Any] | None,
                 current: Mapping[str, Any]) -> dict[str, Any]:
    """逐指标 diff：delta = 新 - 旧；旧值缺失记 None，不参与判据。"""
    prev = previous or {}
    rows: list[dict[str, Any]] = []
    for key in DIFF_METRICS:
        old = _as_float(prev.get(key))
        new = _as_float(current.get(key))
        rows.append({
            "metric": key,
            "previous": old,
            "current": new,
            "delta": (new - old) if (old is not None and new is not None) else None,
        })
    return {"rows": rows, "compared": sum(1 for r in rows if r["delta"] is not None)}


def decide(
    *, diff: Mapping[str, Any], current: Mapping[str, Any],
    evidence_grade: str, oos_passed: bool, cost_gate_passed: bool,
    thresholds: Mapping[str, Any],
) -> tuple[str, list[str]]:
    """按 §2.3 决策：active / degraded / retired。返回 (verdict, reason_codes)。"""
    reasons: list[str] = []
    # OOS 失败或成本闸失败优先：直接下线。
    if not oos_passed:
        reasons.append("oos_failed")
    if not cost_gate_passed:
        reasons.append("cost_gate_failed")
    if reasons:
        return "retired", reasons

    # 通过但显著退化：降权保留 + 告警。阈值来自配置，不在代码里写死。
    deltas = {r["metric"]: r["delta"] for r in diff.get("rows", [])}
    for metric in ("ic", "dsr", "net_return", "cost_adj_return"):
        floor = thresholds.get(f"{metric}_drop")
        delta = deltas.get(metric)
        if floor is None or delta is None:
            continue
        if delta < -abs(float(floor)):
            reasons.append(f"degraded:{metric}")
    return ("degraded" if reasons else "active"), reasons


def default_compute(
    factor: Mapping[str, Any], snapshot_id: str, manifest: Mapping[str, Any],
    *, data_root: Path,
) -> dict[str, Any]:
    """真实重算（复用现有能力链）。

    取因子来源 ticket 的冻结计划，改写为「目标快照 + 该因子作为唯一先验」，
    跑出一张新 ticket，再按 candidate_id 从 Evidence/Verdict/Certify 取指标。
    这样 evaluate（Evidence 的 IC）→ backtest（Verdict 的 cost_adj_return/
    Portfolio）→ certify（OOS 净收益）都走同一条已上线的能力链，不另起一套
    口径。新 ticket 的 `resolved_snapshot_id` 会由 cmd_run 冻结到目标快照。
    """
    from app.cli.run import cmd_run

    layout = Layout(Path(data_root))
    factor_id = str(factor["factor_id"])
    origin = json.loads(str(factor.get("origin_json") or "{}"))
    origin_ticket = str(origin.get("ticket_id") or "")
    if not origin_ticket:
        raise FlowError(E4002, f"因子 {factor_id} 缺来源 ticket，无法重算",
                        path="replay.origin")
    info = read_ticket(layout, origin_ticket)
    frozen = (info.get("frozen") or {}).get("plan")
    if not isinstance(frozen, dict):
        raise FlowError(E3005, f"来源 ticket {origin_ticket} 没有冻结计划",
                        path="replay.origin.plan")

    plan = copy.deepcopy(frozen)
    supply = plan.get("supply")
    if not isinstance(supply, Mapping):
        raise FlowError(E1004, "来源计划缺 supply 块", path="replay.plan.supply")
    plan["supply"]["snapshot"] = snapshot_id
    search = plan.setdefault("search", {})
    search["prior_pool"] = [f"db:{factor_id}"]
    # 只评这一个因子：生成算法压到最小（schema min=1），唯一关心的候选是该因子
    # 先验；产物里其它候选按 candidate_id 精确过滤掉，不参与证书。
    for algo in (search.get("algorithms") or []):
        if isinstance(algo, dict):
            algo.setdefault("params", {})["max_candidates"] = 1

    tmp = layout.tmp_file(f"replay-plan-{_short_digest(factor_id + snapshot_id)}.json")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    args = type("A", (), {
        "plan": str(tmp), "set": [], "no_strict": False,
        "allow_constitutive_override": False, "live": True, "format": "json",
    })()
    envelope = cmd_run(args)
    run_id = str(envelope.get("run_id") or "")

    fresh = read_ticket(layout, run_id)
    payloads = fresh.get("payloads") or {}
    evidence = payloads.get("Evidence") or {}
    verdict = payloads.get("Verdict") or {}
    certify = payloads.get("Certify") or {}

    ev_row = next((r for r in (evidence.get("per_candidate") or [])
                   if str(r.get("candidate_id")) == factor_id), {})
    vd_row = next((r for r in (verdict.get("per_candidate") or [])
                   if str(r.get("candidate_id")) == factor_id), {})
    metrics: dict[str, Any] = {}
    for src in (ev_row.get("metrics") or {}, vd_row.get("statistics") or {},
                certify.get("metrics") or {}):
        if isinstance(src, Mapping):
            metrics.update(src)

    grade = str(certify.get("evidence_grade") or "in_sample")
    decision = str(vd_row.get("decision") or "")
    # 成本闸：计划 verdict.cost_adj_min 是唯一门槛来源（口径唯一）。
    cost_min = ((plan.get("verdict") or {}).get("params") or {}).get("cost_adj_min")
    cost_now = _as_float(metrics.get("cost_adj_return"))
    cost_gate = (cost_now is not None and cost_min is not None
                 and cost_now >= float(cost_min)) if cost_min is not None else True
    return {
        "metrics": metrics,
        "evidence_grade": grade,
        "oos_passed": grade == "out_of_sample" and decision != "reject",
        "cost_gate_passed": bool(cost_gate),
        "run_id": run_id,
    }


class ReplayService:
    """复盘队列与执行（enqueue / run / diff / decide / show）。"""

    def __init__(
        self, data_root: Path, *, compute: ComputeFn | None = None,
        thresholds: Mapping[str, Any] | None = None,
    ) -> None:
        self.root = Path(data_root)
        self.layout = Layout(self.root)
        self.store = SqliteStore(self.layout.db / DB_FILENAME)
        self.store.migrate(now=_now())
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            self.thresholds.update(dict(thresholds))
        self._compute = compute

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "ReplayService":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def compute(self, factor: Mapping[str, Any], snapshot_id: str,
                manifest: Mapping[str, Any]) -> dict[str, Any]:
        fn = self._compute or (lambda f, s, m: default_compute(
            f, s, m, data_root=self.root))
        return fn(factor, snapshot_id, manifest)

    # ---- 读 ---------------------------------------------------------
    def get(self, replay_id: str) -> dict[str, Any] | None:
        return self.store.one("SELECT * FROM replay_runs WHERE replay_id=?", (replay_id,))

    def list(self, *, snapshot_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM replay_runs WHERE 1=1"
        params: list[Any] = []
        if snapshot_id:
            sql += " AND snapshot_id=?"
            params.append(snapshot_id)
        sql += " ORDER BY requested_at, replay_id"
        return self.store.query(sql, params)

    def report_path(self, replay_id: str) -> Path:
        return self.layout.run_dir("replays") / replay_id / "report.json"

    def show(self, replay_id: str) -> dict[str, Any]:
        row = self.get(replay_id)
        if row is None:
            raise FlowError(E4002, f"复盘不存在: {replay_id}", path="replay.replay_id")
        path = self.report_path(replay_id)
        report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {**row, "report": report}

    # ---- 入队 --------------------------------------------------------
    def enqueue(
        self, *, snapshot_id: str | None = None, all_active: bool = False,
        factor_ids: list[str] | None = None, now: str | None = None,
    ) -> list[dict[str, Any]]:
        """把待复验因子入队。幂等：同 (snapshot, factor, caliber) 已存在则跳过。

        - `all_active=True`：全部 active 因子（§2.4 `--all-active`）；
        - 否则只取「上次认证之后产生的新快照」集合（§2.1），即该因子的最新
          证书时间早于目标快照创建时间。
        """
        stamp = now or _now()
        with SnapshotService(self.root) as snaps:
            sid = snaps.resolve(snapshot_id)  # None/latest → 最新 active
            snap = snaps.get(sid)
            if snap is None:
                raise FlowError(E4002, f"快照不存在: {sid}", path="replay.snapshot")
            manifest = snaps.verify(sid)
        with FactorService(self.root) as factors:
            # §3.3：迁移期若 sqlite 还没有因子（旧库），先从 jsonl 导入并标记待复验，
            # 否则 legacy 资产会静默缺席复盘队列。import_legacy 幂等。
            if not factors.list_factors():
                factors.import_legacy()
            actives = factors.list_active()
            if factor_ids:
                wanted = set(factor_ids)
                actives = [f for f in actives if f["factor_id"] in wanted]
            missing = wanted - {f["factor_id"] for f in actives} if factor_ids else set()
            if missing:
                raise FlowError(E4002, f"因子不是 active 状态: {sorted(missing)[:3]}",
                                path="replay.factor_ids")
            pairs: list[tuple[dict[str, Any], str]] = []
            for f in actives:
                cert = f["certificate"]
                if not all_active and str(cert.get("certified_at") or "") >= str(snap["created_at"]):
                    continue
                pairs.append((f, str(cert.get("caliber_digest") or "unknown")))

        created: list[dict[str, Any]] = []
        for factor, caliber in pairs:
            fid = str(factor["factor_id"])
            rid = f"rpl-{stamp[:10]}-{_short_digest(sid + ':' + fid + ':' + caliber)}"
            existing = self.get(rid)
            if existing is not None:
                # 已完成或排队中的不重复入队；error 允许重试（run 会重置）。
                if existing["status"] != "error":
                    continue
            with self.store.transaction() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO replay_runs
                       (replay_id, snapshot_id, factor_id, caliber_digest, status,
                        requested_at, finished_at, run_id, report_ref)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (rid, sid, fid, caliber, "pending", stamp, None, None, None),
                )
            created.append(self.get(rid) or {})
        return created

    # ---- 执行 --------------------------------------------------------
    def run(self, replay_id: str, *, now: str | None = None) -> dict[str, Any]:
        """执行一次复盘。失败/报错不改证书，只把 replay_runs 标成对应状态。"""
        stamp = now or _now()
        row = self.get(replay_id)
        if row is None:
            raise FlowError(E4002, f"复盘不存在: {replay_id}", path="replay.replay_id")
        if row["status"] in ("running", "passed", "failed"):
            return self.show(replay_id)  # 终态/执行中幂等，不重算

        self.store.execute(
            "UPDATE replay_runs SET status='running' WHERE replay_id=?", (replay_id,))
        sid = str(row["snapshot_id"])
        fid = str(row["factor_id"])
        report: dict[str, Any] = {
            "replay_id": replay_id, "factor_id": fid, "snapshot_id": sid,
            "caliber_digest": row["caliber_digest"], "requested_at": row["requested_at"],
        }
        try:
            with SnapshotService(self.root) as snaps:
                manifest = snaps.verify(sid)
            with FactorService(self.root) as factors:
                factor = factors.get(fid)
                if factor is None:
                    raise FlowError(E4002, f"因子定义不存在: {fid}", path="replay.factor_id")
                previous = factors.latest_cert(fid)
                result = self.compute(factor, sid, manifest)
                metrics = dict(result.get("metrics") or {})
                # 上一张证书的指标存在 metrics_json 里；promote 写入时会套一层
                # {"metrics": {...}}，replay 自己写的也同样，统一取内层再 diff。
                prev_metrics: dict[str, Any] = {}
                if previous:
                    try:
                        stored = json.loads(str(previous.get("metrics_json") or "{}"))
                    except json.JSONDecodeError:
                        stored = {}
                    prev_metrics = stored.get("metrics", stored) \
                        if isinstance(stored, Mapping) else {}
                diff = diff_metrics(prev_metrics, metrics)
                verdict, reasons = decide(
                    diff=diff, current=metrics,
                    evidence_grade=str(result.get("evidence_grade") or "unknown"),
                    oos_passed=bool(result.get("oos_passed")),
                    cost_gate_passed=bool(result.get("cost_gate_passed")),
                    thresholds=self.thresholds,
                )
                # from-run 的 caliber 指纹优先用本次 compute 产物，缺失沿用入队值
                caliber = str(row["caliber_digest"])
                factors.certify(
                    snapshot_id=sid, factor_id=fid, caliber_digest=caliber,
                    evidence_grade=str(result.get("evidence_grade") or "unknown"),
                    verdict=verdict, metrics={"metrics": metrics, "diff": diff,
                                              "reason_codes": reasons},
                    replay_id=replay_id, at=stamp,
                )
                factors.materialize()
        except FlowError as exc:
            code = exc.error.code
            status = "error"
            report.update({"decision": "error", "reason_codes": [code],
                           "error": exc.error.message,
                           "metrics": {}, "diff": {"rows": [], "compared": 0}})
            self.store.execute(
                "UPDATE replay_runs SET status=?, finished_at=?, report_ref=? "
                "WHERE replay_id=?",
                (status, stamp, str(self.report_path(replay_id)), replay_id))
            self._write_report(replay_id, report)
            return self.show(replay_id)
        except Exception as exc:  # 非结构化异常也落到 error，不吞、不改证书
            report.update({"decision": "error", "reason_codes": ["E3004"],
                           "error": str(exc),
                           "metrics": {}, "diff": {"rows": [], "compared": 0}})
            self.store.execute(
                "UPDATE replay_runs SET status='error', finished_at=?, report_ref=? "
                "WHERE replay_id=?",
                (stamp, str(self.report_path(replay_id)), replay_id))
            self._write_report(replay_id, report)
            return self.show(replay_id)

        status = "failed" if verdict == "retired" else "passed"
        report.update({
            "decision": verdict, "reason_codes": reasons, "metrics": metrics,
            "diff": diff, "thresholds": dict(self.thresholds),
            "snapshot_digest": manifest.get("manifest_digest"),
            "evidence_grade": str(result.get("evidence_grade") or "unknown"),
            "run_id": result.get("run_id"),
        })
        self.store.execute(
            "UPDATE replay_runs SET status=?, finished_at=?, run_id=?, report_ref=? "
            "WHERE replay_id=?",
            (status, stamp, str(result.get("run_id") or ""),
             str(self.report_path(replay_id)), replay_id))
        self._write_report(replay_id, report)
        return self.show(replay_id)

    def _write_report(self, replay_id: str, report: Mapping[str, Any]) -> None:
        path = self.report_path(replay_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.tmp_file(f"replay-report-{_short_digest(replay_id)}.json")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
                       encoding="utf-8")
        tmp.replace(path)


__all__ = [
    "ReplayService", "default_compute", "diff_metrics", "decide",
    "DEFAULT_THRESHOLDS", "DIFF_METRICS",
]
