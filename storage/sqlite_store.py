"""sqlite 底层基础设施（docs/21-snapshot-replay-catalog.md §1.3/§4）。

只提供机制，不承载业务判断：连接管理、WAL、事务、schema 迁移、路径安全、
读写权限形状。业务规则（latest 怎么解析、哪些因子复验）属于 services 层。

**为什么单独一层**：项目已有三类存储——`events.jsonl` 是 append-only 事件
真相，`catalog/factors.jsonl` 是因子目录，`meta.duckdb` 是可重建索引。新增
sqlite 只做快照清单 / 复盘记录 / 因子证书的真相源，不与前三者混职责。把
连接、迁移、权限收在这里，服务层才不会各自开连接、各自建表、各自吞异常。

错误分层遵循 contracts/errors.py：路径/参数 E5xxx，配置 E1xxx。磁盘满等
IO 故障必须抛结构化错误，不能让裸 OSError 变成 E3004。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from storage.layout import Layout

from contracts.digest import assert_relative_path
from contracts.errors import E1004, E2005, E5001, E5004, FlowError

#: 数据库文件名（物理位置由 Layout.db 给出，不在这里拼九区字面量）。
DB_FILENAME = "snapshots.sqlite3"

#: 当前 schema 版本。任何 DDL 变更必须 +1 并追加一条 migration，不得改旧条目。
SCHEMA_VERSION = 1


def _migrations() -> tuple[tuple[int, str], ...]:
    """按版本升序的 DDL。checksum = H(DDL)，用于检测已应用后被篡改。"""
    return (
        (1, """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY,
  manifest_digest TEXT NOT NULL UNIQUE,
  parent_snapshot_id TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('active','archived')),
  span_start TEXT NOT NULL,
  span_end TEXT NOT NULL,
  note TEXT,
  FOREIGN KEY(parent_snapshot_id) REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT NOT NULL,
  dataset TEXT NOT NULL,
  exchange TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  relpath TEXT NOT NULL,
  blob_digest TEXT NOT NULL,
  columns_digest TEXT NOT NULL,
  value_columns TEXT NOT NULL,
  UNIQUE(snapshot_id, dataset, exchange, market, symbol, timeframe),
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entries_lookup
  ON snapshot_entries(dataset, exchange, market, symbol, timeframe);

CREATE TABLE IF NOT EXISTS replay_runs (
  replay_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  caliber_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','passed','failed','error')),
  requested_at TEXT NOT NULL,
  finished_at TEXT,
  run_id TEXT,
  report_ref TEXT,
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS factor_certificates (
  cert_key TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL,
  factor_id TEXT NOT NULL,
  caliber_digest TEXT NOT NULL,
  evidence_grade TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('active','degraded','retired')),
  metrics_json TEXT NOT NULL,
  certified_at TEXT NOT NULL,
  replay_id TEXT,
  FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id),
  FOREIGN KEY(replay_id) REFERENCES replay_runs(replay_id)
);

CREATE TABLE IF NOT EXISTS factors (
  factor_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  expression_json TEXT NOT NULL,
  required_fields_json TEXT NOT NULL,
  operators_json TEXT NOT NULL,
  origin_json TEXT NOT NULL,
  promoted_at TEXT NOT NULL
);
"""),
    )


def ddl_checksum(ddl: str) -> str:
    """DDL 内容 digest。migration 一旦应用，内容变化就是 E2005。"""
    return "sha256:" + hashlib.sha256(ddl.strip().encode("utf-8")).hexdigest()


class SqliteStore:
    """sqlite 连接与事务的持有者。

    `readonly=True` 时连接以 immutable 模式打开，且 `execute` 拒绝任何写语句
    ——权限由对象形状保证，而不是靠调用方自觉（与 runtime 四区同构）。
    """

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        if not isinstance(path, Path):
            raise FlowError(E1004, f"sqlite 路径必须是 Path，得到 {type(path).__name__}",
                            path="db_path")
        self.path = path
        self.readonly = bool(readonly)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self.readonly:
            if not self.path.exists():
                raise FlowError(E5004, f"sqlite 不存在: {self.path}", path=str(self.path))
            # Windows 上必须走 Path.as_uri() 生成 file:///C:/...；手写
            # file:C:/... 会被 sqlite 解释成相对路径，只读连接打开一个空库。
            uri = self.path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30)
            # WAL：读写并发不互锁；busy_timeout 让并发写排队而不是立刻失败。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        self._conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connect()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务。异常回滚；readonly 连接不得进入。"""
        if self.readonly:
            raise FlowError(E2005, "只读连接不得开启写事务", path="sqlite.readonly")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """只读连接的写语句在这里被拒绝，避免绕过 transaction。

        写语句在**没有显式事务时**自动提交：单条写不该因为调用方忘了 commit
        而在 close 时静默回滚（这正是「刚写的行读不到」的典型来源）。处于
        `transaction()` 内时交给事务统一提交，不在这里打断原子性。
        """
        head = sql.strip().lstrip("(").split(None, 1)[0].upper() if sql.strip() else ""
        if self.readonly and head in {"INSERT", "UPDATE", "DELETE", "REPLACE",
                                      "CREATE", "ALTER", "DROP", "BEGIN", "COMMIT"}:
            raise FlowError(E2005, f"只读连接不得执行 {head}", path="sqlite.readonly")
        write_head = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"}
        try:
            in_tx = bool(self.conn.in_transaction)
            cur = self.conn.execute(sql, tuple(params))
            if head in write_head and not in_tx:
                self.conn.commit()
            return cur
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "disk full" in msg or "unable to open" in msg or "readonly" in msg:
                raise FlowError(
                    E5001, f"sqlite IO 失败: {exc}",
                    path=str(self.path), hint="检查磁盘空间与目录权限",
                ) from None
            raise

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def migrate(self, *, now: str) -> int:
        """幂等应用迁移。已应用版本 DDL 变化 → E2005，不静默跳过。"""
        if self.readonly:
            raise FlowError(E2005, "只读连接不得执行迁移", path="sqlite.readonly")
        # 首次运行时 schema_migrations 自身还不存在：先建它，再去读已应用版本。
        # 定义文本与 migration 1 中的保持一致，CREATE IF NOT EXISTS 幂等。
        self.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL,
              checksum TEXT NOT NULL
            )
        """)
        applied = {int(r["version"]): r for r in self.query(
            "SELECT version, checksum FROM schema_migrations")}
        top = 0
        for version, ddl in _migrations():
            checksum = ddl_checksum(ddl)
            row = applied.get(version)
            if row is not None:
                if str(row["checksum"]) != checksum:
                    raise FlowError(
                        E2005, f"迁移 {version} 的 DDL 已被改动",
                        path="schema_migrations", hint="新增版本，不要改已应用 DDL",
                    )
                top = max(top, version)
                continue
            with self.transaction() as conn:
                try:
                    conn.executescript(ddl)
                except sqlite3.Error as exc:
                    raise FlowError(E5001, f"迁移 {version} 失败: {exc}",
                                    path="schema_migrations") from None
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at, checksum) VALUES(?,?,?)",
                    (version, now, checksum),
                )
            top = max(top, version)
        if top != SCHEMA_VERSION:
            raise FlowError(E2005, f"迁移版本不连续: {top} != {SCHEMA_VERSION}",
                            path="schema_migrations")
        return top

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SqliteStore":
        self._connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def resolve_db_path(data_root: Path) -> Path:
    """数据库物理位置由 Layout 唯一给出，不在此读环境变量或拼九区字面量。"""
    assert_relative_path(DB_FILENAME)
    return Layout(Path(data_root)).db / DB_FILENAME


def snapshot_json(value: Any) -> str:
    """写入 json 列的统一序列化：稳定排序，避免同内容两次写入得到不同字节。"""
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "DB_FILENAME", "SCHEMA_VERSION", "SqliteStore", "ddl_checksum",
    "resolve_db_path", "snapshot_json",
]
