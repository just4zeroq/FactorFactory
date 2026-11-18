"""flow factor_db —— 因子库管理（写 catalog，不写账本）。

`factors` 是只读查询，这条是它的写侧：下架（retire）。分开的理由是 mutation
等级不同——查询永远安全，写库需要 --yes。

**下架是追加一条记录，不是删行**。库是资产目录，删掉一行就再也说不清
「这个因子曾经进过库、什么时候因为什么被下架」。`status` 由最后一条记录生效，
历史可回放。

不做物理删除：真正要回收空间时走 `storage.gc`（按引用计数），而不是在业务
命令里 `unlink`。
"""

from __future__ import annotations

from typing import Any

from contracts.errors import E1004, E3006, FlowError
from facilities.clock import Clock
from runtime.store import resolve_data_root
from storage.catalog import Catalog
from storage.layout import Layout

from .common import envelope_ok


def cmd_factor_db(args: Any) -> dict[str, Any]:
    layout = Layout(resolve_data_root())
    catalog = Catalog(layout)
    retire = getattr(args, "retire", None)

    if not retire:
        # 没有动作就是查询：不猜「你想干嘛」，也不默认打印全库
        return envelope_ok("factor_db", None, {
            "entries": len(catalog.entries()),
            "active": len(catalog.list(status="active")),
            "retired": len(catalog.list(status="retired")),
            "path": str(catalog.path),
            "hint": "用 --retire <factor_id> 下架一个因子",
        })

    # 下架会改库里的现行状态，和写账本同一类风险，所以同样要显式确认。
    # 不走 require_confirmation：那条按 WRITES_LEDGER 判定，而这里写的是 catalog
    # 不是 ledger——把 mutation 谎报成 writes_ledger 会让 --yes 的语义失准。
    if not getattr(args, "yes", False):
        raise FlowError(
            E1004, "factor_db --retire 会改因子库状态，必须显式 --yes",
            path="factor_db.yes", hint="先 flow factors 确认 factor_id",
        )
    target = str(retire)
    if catalog.get(target) is None:
        raise FlowError(
            E3006, f"因子不在库中: {target}", path="factor_db.retire",
            hint="先 flow factors 看 factor_id",
        )
    now = Clock().wall().isoformat().replace("+00:00", "Z")
    note = str(getattr(args, "note", "") or "")
    record = catalog.retire(target, at=now, note=note)
    return envelope_ok("factor_db", None, {
        "entries": len(catalog.entries()),
        "retired_now": target,
        "status": record.get("status"),
        "note": note,
        "path": str(catalog.path),
    })
