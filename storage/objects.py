"""对象存储（09-app-storage.md §3.1/§3.3/§3.4）。

不变式：一次写永不改；同名即同内容。坏了从上游重算。
写入三段：stage(tmp，同盘) → commit(os.replace，原子) → register(jsonl/duckdb，可 reconcile)。

schema sidecar：每个 parquet 带 {name}.schema.json（列名、dtype、schema_version）。
读时不猜；不兼容变更必须 schema_version+1 + 迁移函数，不做"读时兼容两版"。
编码：时间 UTC aware；暴露 float32、价格与账本 float64。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from contracts.digest import assert_relative_path, blob_digest
from contracts.errors import E3003, E5001, E5004, FlowError

from .layout import Layout

SCHEMA_VERSION = 1

#: parquet 序列化的**固定**参数。它们进内容 digest，因此属于存储契约的一部分：
#: 改这里等于改所有对象的 digest（必须当成一次显式的数据迁移，不是随手的调参）。
#: 选 v1.0 + snappy 是因为两者在老版本 pyarrow 上都稳定，且不引入字典编码的
#: 版本差异；`write_statistics=False` 去掉随数据分布变化的统计页。
_PARQUET_COMPRESSION = "snappy"
_PARQUET_VERSION = "1.0"


@dataclass(frozen=True)
class ObjectRef:
    """一个对象落盘后的引用（与 contracts.Ref 的 derived 形态对应）。"""

    kind: str            # panels / labels / exposures / ledgers / reports
    digest: str          # sha256:<hex>，即文件名
    relpath: str         # 相对数据根
    schema_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "digest": self.digest,
            "relpath": self.relpath, "schema_version": self.schema_version,
        }


def _atomic_write_bytes(target: Path, content: bytes, layout: Layout) -> Path:
    """同盘暂存 + os.replace；异内容拒绝覆盖（不可变）。"""
    if target.exists():
        if target.read_bytes() == content:
            return target
        raise FlowError(E5004, f"对象已存在且内容不同，拒绝覆盖: {target}", path=str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = layout.tmp_file(f"obj-{os.urandom(8).hex()}")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_bytes(content)
        os.replace(tmp, target)
    except OSError as exc:
        # 磁盘满/权限不足 → E5001（与 TicketWriter.write_bytes 同一条规则）。
        raise FlowError(
            E5001, f"对象写入失败（磁盘满或权限不足）: {exc.strerror or exc}",
            path=str(target),
            hint="先归档旧执行目录再 GC（15-operations §4-8）",
        ) from exc
    return target


class ObjectStore:
    """内容寻址的对象存储。只被事实层与供给链使用。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    def _target(self, kind: str, digest: str, *, ext: str = ".parquet") -> Path:
        algo, sep, short = digest.partition(":")
        if not sep or algo != "sha256" or len(short) != 64 \
                or any(c not in "0123456789abcdef" for c in short):
            raise FlowError(
                E3003, f"非法内容寻址 digest: {digest!r}",
                hint="必须是 sha256:<64 位小写十六进制>；截断或换算法会让对象再也找不到",
                path="digest",
            )
        d = self.layout.fact_kind(kind) / short
        return d.parent / (d.name + ext)

    def _resolve(self, ref: ObjectRef) -> Path:
        """把引用解析成真实路径：必须是数据根内的相对路径。

        `ref.relpath` 可能来自落盘记录（可被手改），直接 `root / relpath` 会让
        `../../outside.parquet` 读到数据根外。校验和读取必须用同一个解析结果。
        """
        rel = assert_relative_path(ref.relpath)
        target = (self.layout.root / rel).resolve()
        try:
            target.relative_to(self.layout.root.resolve())
        except ValueError:
            raise FlowError(
                E5004, f"对象路径逃逸数据根: {ref.relpath}",
                hint="拒绝 .. 与符号链接逃逸",
                path=ref.relpath,
            ) from None
        return target

    def write_table(
        self,
        kind: str,
        table: pa.Table,
        *,
        schema_version: int = SCHEMA_VERSION,
        expected_digest: str | None = None,
    ) -> ObjectRef:
        """parquet 落盘 + schema sidecar，内容寻址。"""
        # 规范化序列化后再算 digest：同一逻辑表两次写入字节一致。
        # **序列化参数必须显式固定**：pyarrow 的 `compression` / `version` 默认值
        # 随版本演进，依赖默认值意味着「同一逻辑表」在两个 pyarrow 版本下算出两个
        # digest——对象不再去重、`blueprint_digest` 随依赖升级漂移，而这件事
        # 不会报任何错。固定成一组老版本也支持的组合（v1.0 + snappy）是唯一
        # 能让「跨版本字节一致」有依据的做法。
        tmp = self.layout.tmp_file(f"calc-{os.urandom(6).hex()}.parquet")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            pq.write_table(
                table, tmp,
                compression=_PARQUET_COMPRESSION,
                version=_PARQUET_VERSION,
                write_statistics=False,
                store_schema=True,
            )
            content = tmp.read_bytes()
        finally:
            # 写表或读取失败时不得把 tmp 残留在 tmp/ 区（磁盘满时最容易发生）。
            tmp.unlink(missing_ok=True)

        digest = blob_digest(content)
        if expected_digest is not None and digest != expected_digest:
            raise FlowError(E5004, "表内容与声明的 digest 不一致", path="expected_digest")

        target = self._target(kind, digest)
        _atomic_write_bytes(target, content, self.layout)
        sidecar = self.layout.sidecar(str(target))
        schema_doc = {
            "schema_version": schema_version,
            "columns": [{"name": f.name, "dtype": str(f.type)} for f in table.schema],
            "rows": table.num_rows,
        }
        _atomic_write_bytes(sidecar, json.dumps(schema_doc, ensure_ascii=False, sort_keys=True).encode(), self.layout)
        return ObjectRef(kind=kind, digest=digest,
                         relpath=target.relative_to(self.layout.root).as_posix(),
                         schema_version=schema_version)

    def read_table(self, ref: ObjectRef) -> pa.Table:
        """按引用读；先校验 sidecar，不猜 schema。"""
        target = self._resolve(ref)
        if not target.exists():
            raise FlowError(E3003, f"对象缺失: {target}", path=ref.relpath)
        content = target.read_bytes()
        if blob_digest(content) != ref.digest:
            raise FlowError(E5004, f"对象内容指纹失配: {target}", path=ref.relpath)
        sidecar = self.layout.sidecar(str(target))
        if not sidecar.exists():
            raise FlowError(E3003, f"schema sidecar 缺失: {sidecar}", path=str(sidecar))
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        if meta.get("schema_version") != ref.schema_version:
            raise FlowError(
                E3003,
                f"schema 版本不符: 期望 {ref.schema_version}，实得 {meta.get('schema_version')}",
                hint="不兼容变更须升版本并提供迁移函数，禁止读时兼容两版",
                path=str(sidecar),
            )
        return pq.read_table(target)

    def write_json(self, kind: str, payload: Mapping[str, Any], *, expected_digest: str | None = None) -> ObjectRef:
        """reports 等 JSON 对象。"""
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = blob_digest(content)
        if expected_digest is not None and digest != expected_digest:
            raise FlowError(E5004, "内容与声明的 digest 不一致", path="expected_digest")
        target = self._target(kind, digest, ext=".json")
        _atomic_write_bytes(target, content, self.layout)
        return ObjectRef(kind=kind, digest=digest,
                         relpath=target.relative_to(self.layout.root).as_posix(), schema_version=SCHEMA_VERSION)

    def read_json(self, ref: ObjectRef) -> Any:
        target = self._resolve(ref)
        if not target.exists():
            raise FlowError(E3003, f"对象缺失: {target}", path=ref.relpath)
        content = target.read_bytes()
        if blob_digest(content) != ref.digest:
            raise FlowError(E5004, f"对象内容指纹失配: {target}", path=ref.relpath)
        return json.loads(content)
