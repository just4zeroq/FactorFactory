"""因子管理服务层（docs/21-snapshot-replay-catalog.md §3）。

sqlite 是因子身份与**证书**的真相源；`catalog/factors.jsonl` 降级为只读兼容
视图，由 `materialize()` 从 sqlite 物化导出。这样同时保住两条语义：

1. `factor_id` 仍是内容身份（`candidate_id = H(normalise payload + fields)`），
   `db:<factor_id>` 先验引用不因换快照失效；
2. 「因子 F 在快照 S 上是否有效」由 `factor_certificates` 的证书行明确回答，
   同一因子在不同快照下各有各的证书。

证书身份 `cert_key = snapshot_id + ":" + factor_id`。`upsert` 幂等（同表达式
不产生第二个 factor_id），`certify` 对同一 `(snapshot, factor)` 幂等、对异内容
fail-closed（E2005），因为证书一经签发不可改写，只能追加新快照下的新证书。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from capability.mining import candidate_id, candidate_payload
from contracts.digest import canonical_text
from contracts.errors import E1004, E2005, E3006, E4002, FlowError
from storage.catalog import CATALOG_VERSION, Catalog
from storage.layout import Layout
from storage.sqlite_store import DB_FILENAME, SqliteStore, snapshot_json


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_factor_id(expression: Any, required_fields: Sequence[str]) -> str:
    """从表达式 + 字段集导出内容身份，与 capability.mining 保持同一口径。

    复用 `candidate_payload`/`candidate_id`，保证 promote 落库的 factor_id 与
    这里为旧库/外部条目补算的 id 逐位一致——同一个表达式绝不允许两个身份。
    """
    payload = candidate_payload(expression)
    return candidate_id(payload, [str(f) for f in required_fields])


class FactorService:
    """因子定义 + 证书的读写与物化导出。"""

    def __init__(self, data_root: Path) -> None:
        self.root = Path(data_root)
        self.layout = Layout(self.root)
        self.store = SqliteStore(self.layout.db / DB_FILENAME)
        self.store.migrate(now=_now())

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "FactorService":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---- 因子定义 ---------------------------------------------------
    def get(self, factor_id: str) -> dict[str, Any] | None:
        return self.store.one("SELECT * FROM factors WHERE factor_id=?", (factor_id,))

    def list_factors(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM factors ORDER BY promoted_at, factor_id")

    @staticmethod
    def _factor_row(entry: Mapping[str, Any], *, at: str) -> dict[str, Any]:
        expression = entry.get("expression")
        if not isinstance(expression, Mapping):
            raise FlowError(E1004, "因子条目缺 expression", path="factors.expression")
        fid = str(entry.get("factor_id") or "") or derive_factor_id(
            expression, entry.get("required_fields") or [])
        return {
            "factor_id": fid,
            "name": str(entry.get("name") or fid),
            "expression_json": snapshot_json(expression),
            "required_fields_json": snapshot_json(
                sorted({str(f) for f in entry.get("required_fields") or []})),
            "operators_json": snapshot_json(
                sorted({str(o) for o in entry.get("operators") or []})),
            "origin_json": snapshot_json(dict(entry.get("origin") or {})),
            "promoted_at": str(entry.get("promoted_at") or at),
        }

    def upsert(self, entry: Mapping[str, Any], *, at: str | None = None) -> dict[str, Any]:
        """写入因子定义（幂等）。表达式变化即 E2005，身份不可漂移。"""
        row = self._factor_row(entry, at=at or _now())
        existing = self.get(row["factor_id"])
        if existing is not None:
            if str(existing["expression_json"]) != row["expression_json"] \
                    or str(existing["required_fields_json"]) != row["required_fields_json"]:
                raise FlowError(
                    E2005, f"factor_id {row['factor_id']} 的表达式/字段集与库内不一致",
                    path="factors.expression", hint="内容身份不可变；新表达式是新因子",
                )
            return existing
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO factors
                   (factor_id, name, expression_json, required_fields_json,
                    operators_json, origin_json, promoted_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (row["factor_id"], row["name"], row["expression_json"],
                 row["required_fields_json"], row["operators_json"],
                 row["origin_json"], row["promoted_at"]),
            )
        return self.get(row["factor_id"]) or row

    # ---- 证书 -------------------------------------------------------
    @staticmethod
    def cert_key(snapshot_id: str, factor_id: str) -> str:
        return f"{snapshot_id}:{factor_id}"

    def get_cert(self, snapshot_id: str, factor_id: str) -> dict[str, Any] | None:
        return self.store.one(
            "SELECT * FROM factor_certificates WHERE cert_key=?",
            (self.cert_key(snapshot_id, factor_id),),
        )

    def certificates(self, *, factor_id: str | None = None,
                     snapshot_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM factor_certificates WHERE 1=1"
        params: list[Any] = []
        if factor_id:
            sql += " AND factor_id=?"
            params.append(factor_id)
        if snapshot_id:
            sql += " AND snapshot_id=?"
            params.append(snapshot_id)
        sql += " ORDER BY certified_at, cert_key"
        return self.store.query(sql, params)

    def latest_cert(self, factor_id: str) -> dict[str, Any] | None:
        rows = self.store.query(
            "SELECT * FROM factor_certificates WHERE factor_id=? "
            "ORDER BY certified_at DESC, cert_key DESC LIMIT 1", (factor_id,))
        return rows[0] if rows else None

    def list_active(self, *, snapshot_id: str | None = None) -> list[dict[str, Any]]:
        """当前有效因子：最近一张证书 verdict ∈ {active, degraded} 的因子。

        `degraded` 保留但降权，仍属 active 集合；`retired` 与从未certified
        且已 retired 的因子不进集合。指定 snapshot_id 时只取该快照下的证书。
        """
        out: list[dict[str, Any]] = []
        for factor in self.list_factors():
            cert = (self.get_cert(snapshot_id, factor["factor_id"])
                    if snapshot_id else self.latest_cert(factor["factor_id"]))
            if cert is None or cert["verdict"] not in ("active", "degraded"):
                continue
            out.append({**factor, "certificate": dict(cert)})
        return out

    #: 旧库导入哨兵：不是真快照，只满足证书外键并标记待复验。
    LEGACY_SNAPSHOT = "legacy"

    def ensure_legacy_snapshot(self) -> None:
        """确保 `legacy` 哨兵行存在（幂等）。证书外键要求父行先有。"""
        if self.store.one("SELECT 1 FROM snapshots WHERE snapshot_id=?",
                          (self.LEGACY_SNAPSHOT,)) is None:
            with self.store.transaction() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO snapshots
                       (snapshot_id, manifest_digest, parent_snapshot_id, created_at,
                        status, span_start, span_end, note)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (self.LEGACY_SNAPSHOT, "legacy", None, _now(), "archived",
                     "unknown", "unknown", "legacy 因子迁移哨兵，不代表任何数据快照"),
                )

    def certify(
        self, *, snapshot_id: str, factor_id: str, caliber_digest: str,
        evidence_grade: str, verdict: str, metrics: Mapping[str, Any],
        replay_id: str | None = None, at: str | None = None,
    ) -> dict[str, Any]:
        """签发/追加一张证书。同 cert_key 内容一致幂等返回，内容不同 E2005。"""
        if verdict not in ("active", "degraded", "retired"):
            raise FlowError(E1004, f"未知证书 verdict {verdict!r}", path="certificate.verdict")
        if self.get(factor_id) is None:
            raise FlowError(E3006, f"因子不在定义表中: {factor_id}", path="certificate.factor_id")
        key = self.cert_key(snapshot_id, factor_id)
        # 证书必须挂在一张真实快照（或 legacy 迁移哨兵）下：外键保证这一点，
        # 但裸 IntegrityError 太难排查，这里先给出结构化 E4002。
        if snapshot_id == self.LEGACY_SNAPSHOT:
            self.ensure_legacy_snapshot()
        elif self.store.one("SELECT 1 FROM snapshots WHERE snapshot_id=?",
                            (snapshot_id,)) is None:
            raise FlowError(
                E4002, f"证书引用的快照不存在: {snapshot_id}", path="certificate.snapshot_id",
                hint="先 flow snapshot create；证书不能凭空挂在没有清单的数据上",
            )
        metrics_json = snapshot_json(dict(metrics))
        now = at or _now()
        existing = self.store.one(
            "SELECT * FROM factor_certificates WHERE cert_key=?", (key,))
        if existing is not None:
            if str(existing["metrics_json"]) != metrics_json \
                    or existing["verdict"] != verdict \
                    or str(existing["caliber_digest"]) != str(caliber_digest):
                raise FlowError(
                    E2005, f"证书 {key} 已存在且内容不一致", path="certificate.cert_key",
                    hint="换快照签发新证书，不得改写既有证书",
                )
            return existing
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO factor_certificates
                   (cert_key, snapshot_id, factor_id, caliber_digest, evidence_grade,
                    verdict, metrics_json, certified_at, replay_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (key, snapshot_id, factor_id, str(caliber_digest), str(evidence_grade),
                 verdict, metrics_json, now, replay_id),
            )
        return self.get_cert(snapshot_id, factor_id) or {}

    def import_legacy(self, *, at: str | None = None,
                      snapshot_id: str = "legacy") -> list[str]:
        """旧库导入：每条现有因子建一张 `legacy` 证书，标记待复验。

        `caliber_digest=unknown`，metrics 带 `pending_revalidation=True`：历史
        资产不凭空获得新快照下的有效性，必须经 replay 重新认证。
        """
        now = at or _now()
        catalog = Catalog(self.layout)
        imported: list[str] = []
        for row in catalog.entries():
            fid = str(row.get("factor_id") or "")
            if not fid:
                continue
            entry = {
                "factor_id": fid,
                "name": row.get("name") or fid,
                "expression": row.get("expression"),
                "required_fields": row.get("required_fields") or [],
                "operators": row.get("operators") or [],
                "origin": row.get("origin") or {},
                "promoted_at": row.get("promoted_at") or now,
            }
            self.upsert(entry, at=now)
            if self.get_cert(snapshot_id, fid) is None:
                self.certify(
                    snapshot_id=snapshot_id, factor_id=fid,
                    caliber_digest="unknown",
                    evidence_grade=str(row.get("evidence_grade") or "unknown"),
                    verdict="active",
                    metrics={"pending_revalidation": True,
                             "source": "legacy_import",
                             "metrics": row.get("metrics") or {}},
                    at=now,
                )
            imported.append(fid)
        return imported

    # ---- 提升生命周期（§3.2）-----------------------------------------
    def record_promotion(
        self, entry: Mapping[str, Any], *, snapshot_id: str | None = None,
        caliber_digest: str | None = None, at: str | None = None,
    ) -> dict[str, Any]:
        """promote 的服务层落点：定义入 factors + 首张证书 + 物化兼容视图。

        `snapshot_id=None` 表示该 run 早于本机制（legacy ticket），用 `legacy`
        哨兵并记录 `pending_revalidation`，不让它凭空获得新快照的有效性。
        """
        now = at or _now()
        sid = str(snapshot_id or "legacy")
        digest = str(caliber_digest or "unknown")
        self.upsert(entry, at=now)
        fid = str(entry.get("factor_id") or derive_factor_id(
            entry.get("expression"), entry.get("required_fields") or []))
        if self.get_cert(sid, fid) is None:
            self.certify(
                snapshot_id=sid, factor_id=fid, caliber_digest=digest,
                evidence_grade=str(entry.get("evidence_grade") or "unknown"),
                verdict="active",
                metrics={
                    "pending_revalidation": sid == "legacy",
                    "source": "promote",
                    "statistics": dict(entry.get("statistics") or {}),
                    "metrics": dict(entry.get("metrics") or {}),
                },
                at=now,
            )
        self.materialize()
        return self.get(fid) or {}

    # ---- 物化兼容视图 -------------------------------------------------
    def materialize(self) -> Path:
        """从 sqlite 物化当前因子视图到 `catalog/factors.jsonl`。

        兼容 `storage.catalog.Catalog` 的读取接口（表达式只有一行、append-only
        折叠语义）。导出内容按 factor_id 排序、稳定序列化：同状态重复导出字节
        一致。status 由该因子**最新证书**的 verdict 决定。
        """
        # 与已有 jsonl 合并：sqlite 是身份/证书真相源，而 statistics/generators
        # 等展示字段由 promote 写入且 factors 表不复制它们。物化时以旧记录为基
        # 底，只覆盖身份相关字段与当前证书状态，避免把这些字段清成空。
        prior = {str(r.get("factor_id") or ""): r for r in Catalog(self.layout).entries()}
        factors = self.list_factors()
        sqlite_ids = {str(f["factor_id"]) for f in factors}
        lines: list[str] = []

        # 迁移期兼容：jsonl 里可能存在尚未导入 sqlite 的历史因子（legacy）。
        # 物化是「导出 sqlite 视图」，但绝不能把它们丢掉——否则一次 promote
        # 就会静默抹掉整座旧库。原样保留这些记录，等 import_legacy 逐个纳入真相源。
        for fid, row in prior.items():
            if fid not in sqlite_ids:
                lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")))

        for f in factors:
            fid = f["factor_id"]
            cert = self.latest_cert(fid)
            verdict = str((cert or {}).get("verdict") or "active")
            status = "retired" if verdict == "retired" else "active"
            metrics = json.loads(str((cert or {}).get("metrics_json") or "{}"))
            base = prior.get(fid) or {}
            record = {
                **base,
                "catalog_version": CATALOG_VERSION,
                "factor_id": fid,
                "name": f["name"],
                "expression": json.loads(f["expression_json"]),
                "required_fields": json.loads(f["required_fields_json"]),
                "operators": json.loads(f["operators_json"]),
                "origin": json.loads(f["origin_json"]),
                "evidence_grade": str((cert or {}).get("evidence_grade")
                                      or base.get("evidence_grade") or "unknown"),
                "metrics": metrics.get("metrics", metrics)
                           if isinstance(metrics, dict) else {},
                "status": status,
                "promoted_at": f["promoted_at"],
            }
            lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))
        out = self.layout.catalog / "factors.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        # 内容寻址式原子发布：先写同盘 tmp 再 os.replace，避免半行被读者看到。
        tmp = self.layout.tmp_file(f"factors-{abs(hash(tuple(lines)))}.jsonl")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(out)
        return out


__all__ = ["FactorService", "derive_factor_id"]
