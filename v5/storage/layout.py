"""物理分区的唯一定义（09-app-storage.md §3.2 / §3.4）。

硬规则：任何模块不得自己拼 "facts" / "parcels" 等字面量（AC31）。
所有路径都从这里取，逻辑分区（Charter/Depot/Ticket/Scratch）到物理路径的
映射也只在这里定义一次。

**「四区」是权限分区**（`runtime/store.py` 的 ReadOnly / Ticket / Depot），
**「九区」是文档里对物理目录的分组说法**；`ZONE_NAMES` 实际列了 11 个
（多 `plans` / `schemas` / `catalog` 三个索引与契约存放处）。两者不是同一个
概念，别拿数字互相对照。

数据根由 runtime.store.resolve_data_root 解析后传入；本模块不读环境变量、
不读配置——它只是纯路径构造。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 物理区名（实际 11 个，见模块 docstring：「九区」是文档分组说法）。
# 测试用这个集合做 AST 守卫：除本文件外不得出现这些字面量
# 与 Path 拼接（用例在 tests/v5/test_v5_storage.py）。
ZONE_NAMES = (
    "supply", "facts", "parcels", "runs", "db",
    "cache", "tmp", "baselines", "plans", "schemas", "catalog",
)

#: `facts/` 下的对象类别白名单，**唯一真相**。
#:
#: 索引重建（`storage/index.py`）、引用计数回收（`storage/gc.py`）与落盘路径
#: （`fact_kind`）必须引用同一份：曾经 index 漏了 `parcel_data`，于是这类对象
#: 永不进索引、永不受一致性校验，而 GC 认为它是活对象——"谁算活对象"出现两个
#: 答案，且不报任何错。新增类别只改这里。
FACT_KINDS = (
    "panels", "labels", "exposures", "ledgers", "reports", "parcel_data",
)

def all_zone_literals() -> tuple[str, ...]:
    """返回所有物理区名，供守卫测试引用（避免测试自己抄一份字面量）。"""
    return ZONE_NAMES


@dataclass(frozen=True)
class Layout:
    """数据根下的分区路径。全部是属性，不产生任何 IO。"""

    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError(f"Layout.root 必须是 Path，得到 {type(self.root).__name__}")

    # ---- 九区 ---------------------------------------------------------
    @property
    def supply(self) -> Path:
        """Depot·种子：采集结果与别名表，只由供给链写。"""
        return self.root / "supply"

    @property
    def facts(self) -> Path:
        """Depot·派生：内容寻址的不可变对象。"""
        return self.root / "facts"

    @property
    def parcels(self) -> Path:
        """Ticket·工位产物引用。"""
        return self.root / "parcels"

    @property
    def runs(self) -> Path:
        """Ticket：一次执行的状态主体。"""
        return self.root / "runs"

    @property
    def db(self) -> Path:
        """登记库 + 索引库。"""
        return self.root / "db"

    @property
    def cache(self) -> Path:
        """可全删缓存。"""
        return self.root / "cache"

    @property
    def tmp(self) -> Path:
        """同盘暂存，供 os.replace。"""
        return self.root / "tmp"

    @property
    def baselines(self) -> Path:
        return self.root / "baselines"

    @property
    def plans(self) -> Path:
        return self.root / "plans"

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    @property
    def catalog(self) -> Path:
        return self.root / "catalog"

    # ---- 具体落点 -----------------------------------------------------
    @property
    def symbol_sets(self) -> Path:
        """标的集名 → 成员清单（charter 只读，编译期解析 supply.symbols）。"""
        return self.schemas / "symbol_sets.json"

    @property
    def ledger(self) -> Path:
        """append-only 真相（唯一真相）。"""
        return self.db / "ledger.jsonl"

    @property
    def index_db(self) -> Path:
        """可重建的 duckdb 索引。"""
        return self.db / "meta.duckdb"

    @property
    def registers(self) -> Path:
        return self.db

    def register(self, name: str) -> Path:
        """四类登记账本：trials / events / published / members。"""
        if name not in ("trials", "events", "published", "members"):
            raise ValueError(f"未知登记账本: {name!r}")
        return self.db / f"{name}.jsonl"

    def fact_kind(self, kind: str) -> Path:
        """facts 下的对象类别目录。"""
        # parcel_data：工位能力 data 通道的落地类别（S3-M6）。数据通道要么
        # 是数组、要么是值对象/候选集，既不属于供给事实也不是查询索引，单独
        # 一类避免把 masks 混进 panels/、把候选混进 reports/。
        if kind not in FACT_KINDS:
            raise ValueError(f"未知对象类别: {kind!r}")
        return self.facts / kind

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def run_parcels_dir(self, run_id: str) -> Path:
        """一次执行落盘的工位产物目录（`runs/<id>/parcels/`）。

        与 `parcel_dir()` 不是同一处：后者是 Depot 侧按 digest 归置的对象，
        前者是 Ticket 侧按执行顺序排列的产物。读 ticket 只认这里。
        """
        return self.run_dir(run_id) / "parcels"

    def parcel_dir(self, run_id: str, station_seq: int, station: str, parcel_digest: str) -> Path:
        """parcels/{run_id}/{seq}-{station}-{digest}/，digest 去掉算法前缀。"""
        short = parcel_digest.split(":", 1)[-1]
        return self.parcels / run_id / f"{station_seq:02d}-{station}-{short}"

    def log_dir(self, run_id: str) -> Path:
        """每次执行的日志目录（runs/{run_id}/logs/）。"""
        return self.run_dir(run_id) / "logs"

    def log_file(self, run_id: str, call_digest: str) -> Path:
        """工位日志：<call_digest[:16]>.jsonl（s0/02 §2.2）。

        日志在产物前写，此时 parcel_digest 还不存在；用 call_digest 命名
        打破「指纹需要日志路径、日志命名需要指纹」的循环。文件名规则
        复用 facilities.log.log_path，避免两处各写一遍前缀处理。
        """
        from v5.facilities.log import log_path
        return self.log_dir(run_id) / log_path(call_digest)

    def sidecar(self, name: str) -> Path:
        """parquet 的 schema sidecar（09 §3.4）。"""
        return Path(str(name) + ".schema.json")

    def tmp_file(self, token: str) -> Path:
        """同盘暂存文件；token 由调用方生成（通常是 uuid）。"""
        return self.tmp / token

    # ---- 逻辑分区映射（s0/03 §1）-----------------------------------
    @property
    def charter_paths(self) -> tuple[Path, ...]:
        """Charter（W1）：编译期只读。"""
        return (self.baselines, self.plans, self.schemas, self.catalog)

    @property
    def depot_seed(self) -> Path:
        return self.supply

    @property
    def depot_derived(self) -> Path:
        return self.facts

    @property
    def depot_register(self) -> Path:
        return self.db

    @property
    def scratch_paths(self) -> tuple[Path, ...]:
        """Scratch（W4）：可随时清空，不得被产物引用。"""
        return (self.cache, self.tmp)
