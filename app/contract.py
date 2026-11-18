"""命令面唯一真相（09-app-storage.md §2.2 / §2.4 / §4）。

19 条命令在此**只声明元数据**：mutation 等级、是否产 Ticket、最大退出码、
`data` 顶层键、参数 schema（字段名）。真正的执行在 `cli/<命令>.py`，
渲染在 `render/`。任何层想知道「某命令是否写账本」「是否该弹 --yes」，
只读这里，不读散落在 CLI 里的字面量。

三条应用层规则（09 §2.5）：
- P1：命令参数不得出现口径字段（`caliber` 内任何字段名）；口径只在 plan 的
  `caliber` 块里。违反 → `assert_no_caliber_args` 启动即抛 E1020。
- P2：除纯展示参数（`--format`/`--limit`）外，缺参即报错，不猜、不用上次配置。
- P3：`writes_ledger` 以上必须 `--yes` 显式确认；缺 → E1004（退出码 2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from contracts.errors import E1004, E1020, FlowError

# mutation 四级（09 §2.3）
READ_ONLY = "read_only"
WRITES_SUPPLY = "writes_supply"
WRITES_FACTS = "writes_facts"
WRITES_LEDGER = "writes_ledger"
MUTATIONS = (READ_ONLY, WRITES_SUPPLY, WRITES_FACTS, WRITES_LEDGER)
# 需要 --yes 显式确认的等级（P3 / I31）
CONFIRM_REQUIRED = frozenset({WRITES_LEDGER})

# 核心五条：S0 冻结，只依赖 plan + ticket（09 §2.2）
CORE_COMMANDS: tuple[str, ...] = ("run", "station", "explain", "inspect", "stations")
# 业务十四条：S1 起逐步、S7 齐，快照/复盘后补；每条建立在核心五条之上，不新增执行语义
BUSINESS_COMMANDS: tuple[str, ...] = (
    "doctor", "estimate", "fetch", "pipeline", "factors", "backtest",
    "evaluate", "promote", "factor_db", "holdout", "certify", "reconcile",
    "snapshot", "replay", "vision",
)
ALL_COMMANDS: tuple[str, ...] = CORE_COMMANDS + BUSINESS_COMMANDS
# experiment 是 run 的旧名，保留一个版本用于迁移（09 §2.2）
ALIASES: Mapping[str, str] = {"experiment": "run"}


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """一条命令的元数据。字段与 09 §2.2 表格逐列对应。

    `mutation` 是 frozenset：文档表格中 promote/certify 是「writes_facts +
    writes_ledger」复合等级，丢掉其中任何一项都会让 P3/I31 的 --yes 守卫失效。
    """

    name: str
    mutation: frozenset[str]
    produces_ticket: bool
    max_exit_code: int
    data_keys: tuple[str, ...]
    params: tuple[str, ...] = ()
    implemented: bool = False  # 是否有能返回信封的实现；S1 = run/explain/inspect/stations

    def __post_init__(self) -> None:
        if isinstance(self.mutation, str):
            object.__setattr__(self, "mutation", frozenset({self.mutation}))
        bad = self.mutation - set(MUTATIONS)
        if bad:
            raise FlowError(E1004, f"{self.name} 非法 mutation: {sorted(bad)}")
        if self.max_exit_code not in {0, 2, 3, 4}:
            raise FlowError(E1004, f"{self.name} 非法 max_exit_code: {self.max_exit_code}")

    def needs_confirmation(self) -> bool:
        """P3 / I31：mutation 含 writes_ledger 即须 --yes。"""
        return bool(self.mutation & CONFIRM_REQUIRED)


def _specs() -> dict[str, CommandSpec]:
    """命令 → 规格。data_keys 来自 09 §2.4，与 CLI 实际返回对齐。"""
    F_L = frozenset({WRITES_FACTS, WRITES_LEDGER})
    rows = [
        # name, mutation, ticket, max_exit, data_keys, params, implemented
        ("doctor", frozenset({READ_ONLY}), False, 2, ("checks", "warnings"),
         ("plan", "dataset"), True),
        ("estimate", frozenset({READ_ONLY}), False, 2, ("estimate",),
         ("plan", "turnover"), True),
        # 一份数据集 = 一组 parquet，无暂存/版本/别名（19-supply.md）。
        ("fetch", frozenset({WRITES_SUPPLY}), False, 4,
         ("exchange", "market", "data_root", "datasets"),
         ("datasets", "symbols", "symbol-set", "start", "end", "timeframe",
          "exchange", "market", "from-v4", "from-binance-vision", "proxy",
          "revise", "limit", "spot-exchange"), True),
        ("run", frozenset({WRITES_FACTS}), True, 4,
         ("station_specs", "fingerprints", "origins_count", "parcels", "traces", "state"),
         ("plan", "set", "no_strict", "allow_constitutive_override"), True),
        ("station", frozenset({WRITES_FACTS}), True, 4, ("station", "parcel", "trace"),
         ("name", "on", "probe", "commit"), False),
        # explain 与 run 走同一套十步编译（含门禁 B），供给不可用同样要给 4；
        # 文档原表写 2，与「编译期供给门禁」自相矛盾（S2-M5 契约修正）。
        ("explain", frozenset({READ_ONLY}), False, 4, ("origins", "fingerprints"),
         ("plan", "set", "no_strict", "allow_constitutive_override"), True),
        ("inspect", frozenset({READ_ONLY}), False, 2, ("run_id", "records"),
         ("run_id", "station", "full"), True),
        ("stations", frozenset({READ_ONLY}), False, 2, ("stations", "count"), (), True),
        ("pipeline", frozenset({WRITES_FACTS}), True, 4, ("steps",),
         ("plan", "set", "no_strict", "allow_constitutive_override", "live",
          "dataset", "force"), True),
        ("factors", frozenset({READ_ONLY}), False, 2, ("factors", "count"),
         ("limit", "status"), True),
        ("backtest", frozenset({READ_ONLY}), False, 3, ("summary",), (), False),
        ("evaluate", frozenset({READ_ONLY}), False, 2, ("evaluation",), (), False),
        ("promote", F_L, False, 3, ("promotion",),
         ("run_id", "candidate", "min_grade", "yes"), True),
        ("factor_db", frozenset({WRITES_FACTS}), False, 3, ("entries",),
         ("retire", "note", "yes"), True),
        ("holdout", frozenset({WRITES_LEDGER}), False, 4, ("holdout",),
         ("plan", "set", "no_strict", "allow_constitutive_override", "yes"), True),
        ("certify", F_L, False, 3, ("certificate",), (), False),
        # S6：三方一致性核对（登记/产物链/对象），只读；发现损坏 exit 3。
        ("reconcile", frozenset({READ_ONLY}), False, 3, ("tickets", "parcels", "registers", "violations"),
         (), True),
        # 快照管理：create/archive 写的是快照清单（facts），不是全局账本；
        # list/show 只读。加 snapshot 命令不引入 --yes 需求。
        ("snapshot", frozenset({WRITES_FACTS}), False, 4,
         ("snapshot", "snapshots", "manifest"),
         ("create", "list", "show", "archive", "snapshot", "datasets",
          "exchange", "market", "symbols", "timeframe", "parent", "note",
          "limit", "yes", "no_enqueue"), True),
        # 复盘闭环：enqueue/run 写证书+报告（facts），show 只读；不写全局账本。
        ("replay", frozenset({WRITES_FACTS}), False, 4,
         ("enqueued", "count", "replay_id", "status", "decision", "report"),
         ("enqueue", "run", "show", "replay_id", "snapshot", "all_active",
          "factor"), True),
        # 全市场日 K 发现/筛选/拉齐：list 只读；scan 写候选清单（facts）；
        # fetch 写供给（parquet）。均不写全局账本，无需 --yes。
        ("vision", frozenset({WRITES_FACTS, WRITES_SUPPLY}), False, 4,
         ("action", "count", "eligible", "rejected", "scanned", "symbols", "datasets"),
         ("vision_action", "list", "scan", "fetch", "refresh", "scan_start",
          "scan_end", "min_days", "write_symbol_set", "datasets", "exchange",
          "market", "timeframe", "proxy", "timeout", "workers", "revise"), True),
    ]
    return {r[0]: CommandSpec(*r) for r in rows}


COMMANDS: dict[str, CommandSpec] = _specs()


def get(name: str) -> CommandSpec:
    """取命令规格；别名先归一；未登记命令 → E1004。"""
    canon = ALIASES.get(name, name)
    if canon not in COMMANDS:
        raise FlowError(E1004, f"未知命令: {name}", hint=f"可用命令: {sorted(ALL_COMMANDS)}")
    return COMMANDS[canon]


def require_confirmation(name: str, args: Any) -> None:
    """P3：writes_ledger 以上命令缺 --yes → E1004，退出码 2。"""
    spec = get(name)
    if spec.needs_confirmation() and not getattr(args, "yes", False):
        raise FlowError(
            E1004, f"{name} 会写全局账本，必须显式 --yes",
            path=f"{name}.yes",
            hint="账本不可撤销；先 --dry-run 看完整预览再确认",
        )


# 口径字段 → CLI 禁止的开关名。来源：compiler.CALIBER_FIELDS（唯一真相），
# 延迟导入避免 app→runtime→…→app 的潜在环；这里只做静态守卫，不复制第二份字段表。
def forbidden_cli_options() -> frozenset[str]:
    from runtime.compiler import CALIBER_FIELDS

    out = set()
    for field in CALIBER_FIELDS:
        out.add(f"--{field.replace('_', '-')}")
        out.add(f"--{field}")
    return frozenset(out)


def assert_no_caliber_args(option_strings: Sequence[str]) -> None:
    """P1 / I30：启动期扫描所有已声明开关，出现口径字段即 E1020。"""
    banned = forbidden_cli_options()
    hit = sorted(set(option_strings) & banned)
    if hit:
        raise FlowError(
            E1020, f"命令参数含口径字段: {hit}",
            hint="口径只写在 plan 的 caliber 块；命令行可传则 Web 请求可绕过编译期一致性检查",
            context={"forbidden": hit},
        )
