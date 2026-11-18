"""索引库：duckdb 查询面，可从对象 + 登记完整重建（09 §3.1）。

一条规矩：**索引不是真相**。meta.duckdb 不进备份；ledger.jsonl 与
facts/*.parquet 才进。任何人不得只写索引不写登记。

判据：删掉 meta.duckdb 后，reconcile() 能恢复全部查询能力。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb

from contracts.errors import E3003, E5004, FlowError

from .layout import FACT_KINDS, Layout
from .ledger import VALID_REGISTERS, Ledger
from .objects import SCHEMA_VERSION

# 登记行进入索引时统一保留的字段；event_id 便于去重核对。
_LEDGER_COLUMNS = ("event_id", "kind", "ticket_id", "recorded_at", "payload")


def _ledger_row(rec: Mapping[str, Any]) -> dict[str, Any]:
    """账本记录 → 索引行。reconcile 与一致性校验必须逐字用同一投影。"""
    return {
        "event_id": rec.get("event_id"),
        "kind": rec.get("kind"),
        "ticket_id": rec.get("ticket_id"),
        "recorded_at": rec.get("recorded_at"),
        "payload": json.dumps(rec, ensure_ascii=False),
    }


class IndexDB:
    """duckdb 索引。所有写操作都从真相重放，不独立产生状态。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.path = layout.index_db

    def _connect(self) -> duckdb.DuckDBPyConnection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(self.path))

    def reconcile(self, ledger: Ledger | None = None) -> dict[str, int]:
        """从真相完整重建索引。幂等：重跑结果逐位一致。"""
        ledger = ledger or Ledger(self.layout)
        counts: dict[str, int] = {}
        with self._connect() as con:
            for name in VALID_REGISTERS:
                con.execute(f"DROP TABLE IF EXISTS reg_{name}")
                con.execute(
                    f"CREATE TABLE reg_{name} ("
                    "event_id VARCHAR, kind VARCHAR, ticket_id VARCHAR, "
                    "recorded_at VARCHAR, payload JSON)"
                )
                records = ledger.records(name)
                for rec in records:
                    row = _ledger_row(rec)
                    con.execute(
                        f"INSERT INTO reg_{name} VALUES (?, ?, ?, ?, ?)",
                        [row[c] for c in _LEDGER_COLUMNS],
                    )
                counts[name] = len(records)
            # 对象面从 sidecar 建索引（不读 parquet 内容，只读清单）
            con.execute("DROP TABLE IF EXISTS objects")
            con.execute(
                "CREATE TABLE objects (kind VARCHAR, digest VARCHAR, relpath VARCHAR, schema_version INTEGER)"
            )
            rows = self._rebuild_object_rows()
            for row in rows:
                con.execute(
                    "INSERT INTO objects VALUES (?, ?, ?, ?)",
                    [row["kind"], row["digest"], row["relpath"], row["schema_version"]],
                )
            counts["objects"] = len(rows)
        return counts

    def _rebuild_object_rows(self) -> list[dict[str, Any]]:
        """从 sidecar / json 对象重建索引行——reconcile 与一致性校验共用。"""
        out: list[dict[str, Any]] = []
        # 类别白名单只有一个来源（`layout.FACT_KINDS`）：这里曾手抄 5 类、
        # 漏掉 `parcel_data`，那类对象因此永不进索引也永不受一致性校验，
        # 与 GC 的 6 类形成分叉。
        for kind in FACT_KINDS:
            folder = self.layout.fact_kind(kind)
            if not folder.exists():
                continue
            for sidecar in sorted(folder.glob("*.schema.json")):
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                # `sidecar.stem` 是 "abc.parquet.schema"，不是 digest；文件名
                # 的真正主体要先去掉 `.schema.json` 再去一次扩展名。
                file_name = sidecar.name[: -len(".schema.json")]
                out.append({
                    "kind": kind,
                    "digest": f"sha256:{Path(file_name).stem}",
                    "relpath": (folder / file_name).relative_to(self.layout.root).as_posix(),
                    "schema_version": meta.get("schema_version", 0),
                })
            # write_json 的对象没有 sidecar；只按 *.schema.json 枚举会让
            # reports 这类产物永远进不了索引，且一致性检查发现不了缺口。
            for obj in sorted(folder.glob("*.json")):
                if obj.name.endswith(".schema.json"):
                    continue
                out.append({
                    "kind": kind,
                    "digest": f"sha256:{obj.stem}",
                    "relpath": obj.relative_to(self.layout.root).as_posix(),
                    "schema_version": SCHEMA_VERSION,
                })
        return out

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """只读查询。抛出的 SQL 错误原样上抛（调用方按 E3003 包装）。"""
        if not self.path.exists():
            raise FlowError(E3003, "索引库不存在，先 reconcile()", path=str(self.path))
        with self._connect() as con:
            cur = con.execute(sql, params or [])
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def assert_consistent(self, ledger: Ledger | None = None) -> dict[str, Any]:
        """一致性校验：索引行数与真相逐类相等，否则报 E5004。"""
        ledger = ledger or Ledger(self.layout)
        result: dict[str, Any] = {}
        for name in (*VALID_REGISTERS, "objects"):
            if name == "objects":
                truth = self._rebuild_object_rows()
                table = "objects"
            else:
                truth = [_ledger_row(r) for r in ledger.records(name)]
                table = f"reg_{name}"
            try:
                rows = self.query(f"SELECT * FROM {table}")
            except FlowError:
                rows = None
            indexed = len(rows) if rows is not None else -1
            ok = rows is not None and self._rows_equal(rows, truth)
            result[name] = {"truth": len(truth), "index": indexed, "ok": ok}
            if not ok:
                raise FlowError(
                    E5004,
                    f"索引与真相不一致: {name} 真相 {len(truth)} 行 / 索引 {indexed} 行",
                    hint="删掉 meta.duckdb 后 reconcile() 重建；不得直接改索引",
                    path=f"index.{table}",
                )
        return result

    @staticmethod
    def _rows_equal(indexed: list[dict[str, Any]], truth: Sequence[Mapping[str, Any]]) -> bool:
        if len(indexed) != len(truth):
            return False
        norm = lambda r: tuple(sorted((str(k), str(v)) for k, v in dict(r).items()))
        return sorted(map(norm, indexed)) == sorted(map(norm, truth))

    def counts(self) -> dict[str, int]:
        out = {}
        for name in VALID_REGISTERS:
            rows = self.query(f"SELECT COUNT(*) AS n FROM reg_{name}")
            out[name] = rows[0]["n"]
        rows = self.query("SELECT COUNT(*) AS n FROM objects")
        out["objects"] = rows[0]["n"]
        return out
