"""数据根解析 + 四区权限接口（s0/03-runtime.md §1，09-app-storage.md §3）。

两件事，互不混淆：
1. 数据根解析：显式 > FLOW_DATA_ROOT > operations.data_root > ~/factorfactory
2. 权限靠接口形状保证（s0/03 §1.1）：ReadOnlyStore / TicketWriter / DepotWriter
   三者互不兼容——执行器拿 TicketWriter，没有任何 Depot 写方法；能力层没有存储句柄。

"四区"是权限分区，"九区"是物理路径。物理路径只在 storage.layout 里定义。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from contracts.digest import blob_digest
from contracts.errors import E3003, E5001, E5004, FlowError
from storage.layout import Layout

ENV_DATA_ROOT = "FLOW_DATA_ROOT"
DEFAULT_DIRNAME = "factorfactory"

# ticket_id 必须是**单个路径段**：`Ticket` 契约的格式校验在创建票据时做；
# 存储层只负责路径安全，但这一条不能省——`TicketWriter("../../db")` 会把
# `ticket_root` 一起挪到数据根外，之后所有 relative_to 校验都以逃逸后的根为基准。
_TICKET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# 兼容别名：S0 的 StoreLayout 只定义四个逻辑名，现已由 Layout 取代。
# 保留一个版本给迁移期的调用方（文档约定 experiment 别名同样保留一版）。
StoreLayout = Layout


def resolve_data_root(
    explicit: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """按拍板 3 的优先级解析数据根。

    显式参数 > 环境变量 > 配置 operations.data_root > 默认个人目录。
    不做 mkdir——解析是纯函数，是否建目录由调用方决定。
    """
    env = environ if environ is not None else os.environ
    raw = explicit or env.get(ENV_DATA_ROOT)
    if not raw:
        ops = (config or {}).get("operations") or {}
        if isinstance(ops, Mapping):
            raw = ops.get("data_root")
    if not raw:
        raw = Path.home() / DEFAULT_DIRNAME
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise FlowError(
            E5004,
            f"数据根必须是绝对路径: {raw!r}",
            hint="相对路径会随 cwd 漂移，破坏可复现性",
            path="data_root",
        )
    return p


class ReadOnlyStore:
    """执行期只读视图。无写方法——类型上不存在 Depot 修改能力。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    def read_bytes(self, path: Path) -> bytes:
        """读取已落盘对象。路径必须在数据根内，拒绝逃逸。"""
        self._assert_inside(path, self.layout.root)
        if not path.exists():
            # 产物缺失是执行事实，不是"别名指向不存在的版本"（E4001）。
            raise FlowError(E3003, f"对象不存在: {path}", path=str(path))
        return path.read_bytes()

    @staticmethod
    def _assert_inside(path: Path, root: Path) -> None:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            raise FlowError(
                E5004,
                f"路径逃逸数据根: {path}",
                hint="拒绝 .. 与符号链接逃逸",
                path=str(path),
            ) from None


class TicketWriter(ReadOnlyStore):
    """只对本 Ticket 目录生效的写接口。

    继承 ReadOnlyStore（能读共享事实），但**没有任何 Depot 写方法**——
    这是 s0/03 §1.1「执行期不得写共享仓」的结构保证。
    写入即校验四条（s0/03 §1.2），任一失败 E5004。
    """

    def __init__(self, layout: Layout, ticket_id: str) -> None:
        super().__init__(layout)
        if not isinstance(ticket_id, str) or not _TICKET_ID.fullmatch(ticket_id):
            raise FlowError(
                E5004,
                f"ticket_id 非法（必须是单个路径段）: {ticket_id!r}",
                hint="票号形如 20261116T053000Z-1a2b3c4d；不得含分隔符或 ..",
                path="ticket_id",
            )
        self.ticket_id = ticket_id
        self.ticket_root = layout.run_dir(ticket_id)
        # 双保险：run_dir 的结果必须落在 runs/ 之内（防止 layout 被改坏）。
        self._assert_inside(self.ticket_root, layout.runs)

    def _target(self, relpath: str | Path) -> Path:
        """校验 1+2：路径规范化后仍在 Ticket/<id>/ 内，不接受别的 id。"""
        target = (self.ticket_root / relpath).resolve()
        try:
            target.relative_to(self.ticket_root.resolve())
        except ValueError:
            raise FlowError(E5004, f"写入路径逃逸 Ticket: {relpath}", path=str(relpath)) from None
        return target

    def write_bytes(self, relpath: str | Path, content: bytes, *, expected_digest: str | None = None) -> Path:
        """校验 3+4：内容寻址一致；目标不存在或内容相同，内容不同拒绝覆盖。"""
        target = self._target(relpath)
        if expected_digest is not None and blob_digest(content) != expected_digest:
            raise FlowError(E5004, "内容与声明的 digest 不一致", path=str(target))
        if target.exists():
            if target.read_bytes() == content:
                return target  # 幂等
            raise FlowError(
                E5004,
                f"目标已存在且内容不同，拒绝覆盖: {target}",
                hint="不可变产物只能新增版本",
                path=str(target),
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.tmp_file(f"ticket-{self.ticket_id}-{os.urandom(6).hex()}")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_bytes(content)
            os.replace(tmp, target)  # 同盘暂存 + 原子 replace（09 §3.3）
        except OSError as exc:
            # 磁盘满/权限不足（AC51 六类故障演练之一）：必须是结构化错误，
            # 不能让裸 OSError 变成 E3004「未预期异常」——那会让运维把
            # 存储故障误判成执行 bug。tmp 残留留给 Layout.tmp 清扫。
            raise FlowError(
                E5001, f"写入失败（磁盘满或权限不足）: {exc.strerror or exc}",
                path=str(target),
                hint="先归档旧执行目录再 GC（15-operations §4-8）；不要 rm -rf 任何目录",
            ) from exc
        return target

    def append_jsonl(self, relpath: str | Path, record: Mapping[str, Any]) -> Path:
        """只追加写本 Ticket 的 jsonl（trace / 本执行事件）。

        只允许 `.jsonl`：`append_jsonl("…/parcel.json", …)` 会把字节追加进已落盘的
        不可变产物，`write_bytes` 的"异内容拒绝覆盖"保护被整个绕过。
        """
        if Path(str(relpath)).suffix != ".jsonl":
            raise FlowError(
                E5004, f"append_jsonl 只接受 .jsonl 目标: {relpath}",
                hint="不可变产物用 write_bytes；追加写只用于 trace/事件日志",
                path=str(relpath),
            )
        target = self._target(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return target


class DepotWriter:
    """供给链专用（唯一能写共享仓的接口）。执行器永远拿不到这个对象。"""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    def publish_bytes(self, target: Path, content: bytes, *, expected_digest: str | None = None) -> Path:
        """原子发布不可变对象：内容寻址 + tmp + os.replace，不覆盖异内容。

        只允许写 `supply/` 区：DepotWriter 是供给链专用接口，若只校验"在数据根内"，
        一个错误的派生路径就能覆盖别的 Ticket 产物或自建 `db/ledger.jsonl`，
        从而绕过账本的去重与追加语义。
        """
        depot_root = self.layout.supply
        ReadOnlyStore._assert_inside(target, depot_root)
        if expected_digest is not None and blob_digest(content) != expected_digest:
            raise FlowError(E5004, "内容与声明的 digest 不一致", path=str(target))
        if target.exists():
            if target.read_bytes() == content:
                return target
            raise FlowError(E5004, f"目标已存在且内容不同: {target}", path=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.tmp_file(f"depot-{os.urandom(8).hex()}")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(content)
        os.replace(tmp, target)
        return target


@dataclass(frozen=True)
class StoreContext:
    """一次运行的存储上下文：Layout + 三个权限视图。"""

    layout: Layout
    reader: ReadOnlyStore

    @classmethod
    def from_root(cls, root: Path) -> "StoreContext":
        layout = Layout(root)
        return cls(layout=layout, reader=ReadOnlyStore(layout))

    def for_ticket(self, ticket_id: str) -> TicketWriter:
        return TicketWriter(self.layout, ticket_id)

    def for_depot(self) -> DepotWriter:
        return DepotWriter(self.layout)
