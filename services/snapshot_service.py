"""快照管理服务层（docs/21-snapshot-replay-catalog.md §1）。

业务规则在这一层，sqlite_store 只提供机制：

- `create()` 扫描真实数据文件，逐文件读取时间范围/行数/digest 建清单；
- `resolve()` 把 `"latest"` 解析成具体快照 id；
- 快照一经写入不可修改（同 manifest_digest 幂等，异内容 E2005）。

清单的 start/end 是**实测事实**，不是用户声明——过去出现过「计划声明起点早于
标的实际上市日」导致 doctor 通过而 run 失败，快照必须从文件读。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pyarrow.parquet as pq

from contracts.digest import assert_relative_path, blob_digest, canonical_text
from contracts.errors import E1004, E1008, E2005, E4002, FlowError
from storage.layout import Layout
from storage.sqlite_store import DB_FILENAME, SqliteStore, snapshot_json
from upstream import store as upstream_store


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_text(value: Any) -> str:
    """时间戳 → 规范化 UTC 文本，进清单不带本地时区歧义。"""
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        raise FlowError(E1008, "数据文件含 naive 时间戳", path="timestamp")
    if hasattr(value, "astimezone"):
        value = value.astimezone(timezone.utc)
    text = str(value)
    return text.replace("+00:00", "Z")


def verify_manifest(data_root: Path, manifest: Mapping[str, Any]) -> None:
    """逐文件核对清单声明与磁盘字节：缺文件或被改写都 fail-closed。

    这里**不查 status**：归档快照只是不能再被新计划引用，旧 run 的复盘仍必须
    能按原清单重建。能否引用由 `SnapshotService.resolve` 单独判定。
    """
    root = Path(data_root)
    for entry in manifest.get("entries") or []:
        relpath = str(entry["relpath"])
        target = root / relpath
        if not target.exists():
            raise FlowError(
                E4002, f"快照 {manifest.get('snapshot_id', '?')} 声明的文件缺失: {relpath}",
                path="supply.snapshot", hint="重新采集或改用其它快照",
            )
        actual = blob_digest(target.read_bytes())
        if actual != entry["blob_digest"]:
            raise FlowError(
                E2005,
                f"快照 {manifest.get('snapshot_id', '?')} 与当前供给不一致: {relpath}",
                path="supply.snapshot",
                hint="数据已被改写；选新快照重跑，不得在原 run 上继续",
            )


def _make_snapshot_id(root: Path, stamp: str, entries: Sequence[Mapping[str, Any]],
                      *, parent: str | None) -> str:
    """id 含日期 + digest 前 8 位：同内容重复 create 得到同一 id（幂等）。"""
    payload = canonical_text({
        "entries": [dict(e) for e in entries],
        "parent": parent,
    })
    short = blob_digest(payload.encode("utf-8")).split(":", 1)[-1][:8]
    day = stamp[:10].replace("-", "")
    return f"snap-{day}-{short}"


class SnapshotService:
    """快照清单的读写与解析。"""

    def __init__(self, data_root: Path) -> None:
        self.root = Path(data_root)
        self.store = SqliteStore(Layout(self.root).db / DB_FILENAME)
        self.store.migrate(now=_now())

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "SnapshotService":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---- 读 ---------------------------------------------------------
    def list(self, *, status: str | None = None,
             limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM snapshots"
        params: list[Any] = []
        if status:
            if status not in ("active", "archived"):
                raise FlowError(E1004, f"未知快照状态 {status!r}", path="snapshot.status")
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, snapshot_id DESC"
        if limit is not None and limit >= 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.store.query(sql, params)

    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        return self.store.one("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,))

    def entries(self, snapshot_id: str, *, dataset: str | None = None,
                symbol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM snapshot_entries WHERE snapshot_id=?"
        params: list[Any] = [snapshot_id]
        if dataset:
            sql += " AND dataset=?"
            params.append(dataset)
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY dataset, symbol"
        return self.store.query(sql, params)

    def manifest(self, snapshot_id: str) -> dict[str, Any]:
        """完整清单：快照头 + 逐文件条目，digest 与入库时一致。"""
        snap = self.get(snapshot_id)
        if snap is None:
            raise FlowError(E4002, f"快照不存在: {snapshot_id}", path="snapshot.snapshot_id")
        rows = self.entries(snapshot_id)
        entries = [{
            "dataset": r["dataset"], "exchange": r["exchange"],
            "market": r["market"], "symbol": r["symbol"],
            "timeframe": r["timeframe"], "start_ts": r["start_ts"],
            "end_ts": r["end_ts"], "row_count": int(r["row_count"]),
            "relpath": r["relpath"], "blob_digest": r["blob_digest"],
            "columns_digest": r["columns_digest"],
            "value_columns": json.loads(r["value_columns"]),
        } for r in rows]
        return {
            "snapshot_id": snap["snapshot_id"],
            "manifest_digest": snap["manifest_digest"],
            "parent_snapshot_id": snap["parent_snapshot_id"],
            "created_at": snap["created_at"],
            "status": snap["status"],
            "span": {"start": snap["span_start"], "end": snap["span_end"]},
            "entries": entries,
        }

    def verify(self, snapshot_id: str) -> dict[str, Any]:
        """取清单并核对磁盘字节；归档快照也允许（旧 run 复盘用）。

        与 `resolve()` 的分工：`resolve` 管「新计划能不能引用」，`verify` 管
        「这张ticket承诺的数据还在不在」。前者拒绝 archived，后者不拒绝。
        """
        manifest = self.manifest(snapshot_id)
        verify_manifest(self.root, manifest)
        return manifest

    def active_since(self, timestamp: str | None, *,
                     current: str | None = None) -> list[dict[str, Any]]:
        """`created_at > timestamp` 的 active 快照（复盘队列用，§2.1）。

        `timestamp=None` 表示全部 active。比较的是 UTC 字符串（本仓库一律
        `%Y-%m-%dT%H:%M:%SZ` 定宽格式，字典序即时间序），不用解析时区。
        `current` 用于测试注入「现在」，默认取真实 UTC。
        """
        now = current or _now()
        rows = self.list(status="active")
        if timestamp is None:
            return rows
        return [r for r in rows if str(r["created_at"]) > str(timestamp)]

    def resolve(self, ref: str | None) -> str:
        """`None` / `"latest"` → 最新 active 快照；具体 id 原样校验后返回。"""
        if ref is None or str(ref).strip().lower() in {"", "latest"}:
            rows = self.list(status="active", limit=1)
            if not rows:
                raise FlowError(
                    E4002, "还没有可用快照，先 flow snapshot create",
                    path="supply.snapshot", hint="快照必须先于 run 冻结",
                )
            return str(rows[0]["snapshot_id"])
        snapshot_id = str(ref)
        snap = self.get(snapshot_id)
        if snap is None:
            raise FlowError(E4002, f"快照不存在: {snapshot_id}", path="supply.snapshot")
        if snap["status"] != "active":
            raise FlowError(
                E4002, f"快照 {snapshot_id} 已归档，不能被新计划引用",
                path="supply.snapshot", hint="选一个 active 快照或新建",
            )
        return snapshot_id

    # ---- 写 ---------------------------------------------------------
    def create(
        self, *, datasets: Sequence[str], exchange: str,
        market: str = "futures", symbols: Sequence[str] | None = None,
        timeframe: str = "1d", parent: str | None = None,
        note: str = "", stamp: str | None = None,
    ) -> dict[str, Any]:
        """扫描现有供给生成快照清单。

        `symbols=None` 表示扫描目录下全部标的；给了清单则缺文件即 E4002。
        不修改任何 parquet，只写清单与 manifest。
        """
        stamp = stamp or _now()
        datasets = tuple(dict.fromkeys(str(x) for x in datasets))
        if not datasets:
            raise FlowError(E1004, "快照必须至少含一个数据集", path="datasets")
        for ds in datasets:
            upstream_store.require_market_dataset(market, ds)
        if parent is not None:
            self.resolve(parent)

        entries: list[dict[str, Any]] = []
        for ds in datasets:
            stems = tuple(dict.fromkeys(str(s) for s in symbols)) if symbols else \
                upstream_store.scan_symbols(self.root, ds, exchange, market,
                                            timeframe if ds == "ohlcv" else None)
            if not stems:
                raise FlowError(
                    E4002, f"{ds} 在 {exchange}/{market} 下没有数据文件",
                    path="datasets", hint="先 flow fetch 采集",
                )
            for stem in stems:
                path = upstream_store.dataset_file(
                    self.root, ds, exchange, market, stem,
                    timeframe if ds == "ohlcv" else None,
                )
                if not path.exists():
                    raise FlowError(
                        E4002, f"快照声明的标的文件不存在: {path}",
                        path="symbols", hint="快照不跳过缺失标的",
                    )
                # 真实符号以文件内 symbol 列为准，不用文件名反推。
                content = path.read_bytes()
                frame = pq.read_table(path).to_pandas()
                if "timestamp" not in frame.columns:
                    raise FlowError(E3003, f"数据文件缺 timestamp: {path}", path=str(path))
                ts = frame["timestamp"]
                if ts.empty:
                    raise FlowError(E3003, f"数据文件为空: {path}", path=str(path))
                value_cols = [
                    c for c in upstream_store.VALUE_COLUMNS[ds] if c in frame.columns
                ]
                missing_cols = set(upstream_store.VALUE_COLUMNS[ds]) - set(frame.columns)
                if missing_cols:
                    raise FlowError(
                        E3003, f"{path.name} 缺值列 {sorted(missing_cols)}", path=str(path),
                    )
                entries.append({
                    "dataset": ds, "exchange": exchange, "market": market,
                    "symbol": str(frame["symbol"].iloc[0]) if "symbol" in frame else stem,
                    "timeframe": timeframe if ds == "ohlcv" else "",
                    "start_ts": _ts_text(ts.min()), "end_ts": _ts_text(ts.max()),
                    "row_count": int(len(frame)),
                    "relpath": assert_relative_path(
                        path.resolve().relative_to(self.root.resolve()).as_posix()),
                    "blob_digest": blob_digest(content),
                    "columns_digest": blob_digest(canonical_text(value_cols).encode("utf-8")),
                    "value_columns": value_cols,
                })

        manifest_digest = blob_digest(canonical_text(entries).encode("utf-8"))
        snapshot_id = _make_snapshot_id(self.root, stamp, entries, parent=parent)
        existing = self.get(snapshot_id)
        if existing is not None:
            if str(existing["manifest_digest"]) != manifest_digest:
                raise FlowError(
                    E2005, f"快照 id 冲突: {snapshot_id}", path="snapshot.manifest_digest",
                )
            return self.manifest(snapshot_id)

        starts = [e["start_ts"] for e in entries]
        ends = [e["end_ts"] for e in entries]
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO snapshots
                   (snapshot_id, manifest_digest, parent_snapshot_id, created_at,
                    status, span_start, span_end, note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (snapshot_id, manifest_digest, parent, stamp, "active",
                 min(starts), max(ends), note),
            )
            conn.executemany(
                """INSERT INTO snapshot_entries
                   (snapshot_id, dataset, exchange, market, symbol, timeframe,
                    start_ts, end_ts, row_count, relpath, blob_digest,
                    columns_digest, value_columns)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(
                    snapshot_id, e["dataset"], e["exchange"], e["market"],
                    e["symbol"], e["timeframe"], e["start_ts"], e["end_ts"],
                    e["row_count"], e["relpath"], e["blob_digest"],
                    e["columns_digest"], snapshot_json(e["value_columns"]),
                ) for e in entries],
            )
        # 清单 json 作为审计产物落盘；与 sqlite 头里的 digest 必须一致。
        manifest = self.manifest(snapshot_id)
        out = Layout(self.root).catalog / "snapshots" / f"{snapshot_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return manifest

    def archive(self, snapshot_id: str) -> dict[str, Any]:
        """归档快照：仍可查、不可被新计划引用。"""
        snap = self.get(snapshot_id)
        if snap is None:
            raise FlowError(E4002, f"快照不存在: {snapshot_id}", path="snapshot.snapshot_id")
        if snap["status"] == "archived":
            return self.manifest(snapshot_id)
        self.store.execute(
            "UPDATE snapshots SET status='archived' WHERE snapshot_id=?", (snapshot_id,))
        return self.manifest(snapshot_id)


__all__ = ["SnapshotService"]
