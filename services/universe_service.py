"""L0 数据基座的交易对元数据（universe 生命周期表）。

为什么单独一张表：备份站与 ccxt 都只能回答「现在有哪些合约」，回答不了
「历史上曾经有哪些」。而没有后者，回测就只在自己的幸存者池上做横截面排序——
归零币被系统性剔除，IC 天然偏高。这张表把「上过、下过」记成事实。

设计约束：
- **表是增量事实，不是快照**：`listed_at` 取历史上最早的一次观测，不因为
  本次探测窗口变窄而后移；`last_bar` 取最新一次。同一标的重复刷新不会让
  上市日往后漂。
- **下架要能自证**：`status=delisted` 只在这一条记录的 `last_bar` 距观测时点
  超过 `delist_gap_days` 时才成立。备份站月度归档有发布延迟，一天的缺口
  不等于退市。
- **存储无依赖**：一行一个 JSON 的 jsonl，谁都能 `cat`/`grep`，不需要 sqlite
  连接；文件本身可以被 snapshot 冻结。
- **发现可注入**：ccxt 调用只在 CLI 层，本模块只认 `Sequence[str]` 或已探好的
  映射，测试不需要网络。

落点：`<data_root>/base/universe/markets.jsonl`（`base` 即 L0 根）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from contracts.errors import E1004, E4002, FlowError
from upstream import store
from upstream.binance_vision import vision_symbol

#: L0 数据基座在数据根下的目录名。L1 仍用数据根本身，所以 `dataset_file`
#: 传 `base_root(root)` 即可复用，不需要给存储层加参数。
BASE_DIRNAME = "base"

#: universe 表相对 L0 根的路径。
UNIVERSE_RELPATH = ("universe", "markets.jsonl")

#: 资产类别。横截面排序只在同类内成立——股票代币永续有交易时段与隔夜跳空，
#: 和 24h 加密资产放同一个 rank 里得到的是噪声，所以类别必须是一等字段。
ASSET_CLASSES = ("crypto", "equity", "index", "stable")

#: 稳定币 base。它们不是「另一种加密资产」：价格被锚定，波动率结构不同，
#: 在动量/波动类因子上是纯噪声源。
STABLE_BASES = frozenset({
    "USDT", "USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "USDD",
    "USDE", "PYUSD", "EUR", "EURI", "AEUR", "USD1", "XUSD", "USTC",
})

#: 合约状态。
STATUS_ACTIVE = "active"
STATUS_DELISTED = "delisted"
STATUS_UNKNOWN = "unknown"


def base_root(root: str | Path) -> Path:
    """L0 数据基座的根目录。`dataset_file(base_root(root), ...)` 直接可用。"""
    return Path(root) / BASE_DIRNAME


#: 落盘文件名的合法字符集，与 `upstream.store._safe_segment` 同一份规则。
_SAFE_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def is_storable(symbol: str) -> bool:
    """该标的能否安全落成 parquet 文件名。

    交易所确有中文名合约（如 `哈基米/USDT:USDT`），它们进不了文件名。这类标的
    在发现阶段就跳过并记警告——一个脏符号不该让全市场刷新整体失败。
    """
    try:
        stem = store.file_stem(str(symbol))
    except FlowError:
        return False
    return bool(_SAFE_STEM.match(stem))


def universe_path(root: str | Path) -> Path:
    return base_root(root).joinpath(*UNIVERSE_RELPATH)


def _parse_day(value: str | None) -> date | None:
    """接受 `YYYY-MM-DD`、`YYYY-MM` 或带时区的完整时间戳。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y-%m", 7)):
        if len(text) >= size:
            try:
                return datetime.strptime(text[:size], fmt).date()
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _earliest(values: Iterable[str | None]) -> str | None:
    """按日期取最早，**原样返回字符串**（不重新格式化，避免抹掉时刻信息）。"""
    pairs = [(v, _parse_day(v)) for v in values if v]
    pairs = [(raw, day) for raw, day in pairs if day is not None]
    if not pairs:
        return None
    return min(pairs, key=lambda p: p[1])[0]


def _latest(values: Iterable[str | None]) -> str | None:
    pairs = [(v, _parse_day(v)) for v in values if v]
    pairs = [(raw, day) for raw, day in pairs if day is not None]
    if not pairs:
        return None
    return max(pairs, key=lambda p: p[1])[0]


def age_days(value: str | None, observed_at: str) -> int | None:
    """`value` 距 `observed_at` 的自然日数；任一无法解析返回 None。"""
    a = _parse_day(value)
    b = _parse_day(observed_at)
    if a is None or b is None:
        return None
    return (b - a).days


def classify_asset(
    symbol: str,
    *,
    overrides: Mapping[str, str] | None = None,
    underlying_type: str | None = None,
) -> str:
    """判定资产类别。优先级：显式覆盖 > 交易所元数据 > 稳定币名单 > crypto。

    股票类无法从符号字形可靠推出（`BR` 既是 Bed Bath & Beyond 也可能是个
    base asset），所以只认交易所的 `underlyingType` 与人工覆盖表，不猜。
    """
    if overrides and symbol in overrides:
        return str(overrides[symbol])

    text = str(underlying_type or "").strip().upper()
    if text in {"EQUITY", "STOCK", "PREMARKET"}:
        return "equity"
    if text in {"INDEX", "INDEX_PRICE"}:
        return "index"

    base = str(symbol).split("/")[0].upper()
    if base in STABLE_BASES:
        return "stable"
    return "crypto"


def make_record(
    symbol: str,
    *,
    exchange: str = "binanceusdm",
    market: str = "futures",
    asset_class: str = "crypto",
    first_archive_month: str | None = None,
    contract_type: str | None = None,
    underlying_type: str | None = None,
    exchange_state: str | None = None,
    exchange_status: str | None = None,
    contract_status: str | None = None,
    onboard_date: str | None = None,
    delivery_date: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """一条 universe 记录的初始形态（尚未观测到 bar 数据）。"""
    text = str(symbol).strip()
    if not text or "/" not in text:
        raise FlowError(E1004, f"非法标的: {symbol!r}", path="symbols")
    if asset_class not in ASSET_CLASSES:
        raise FlowError(
            E1004, f"未知资产类别 {asset_class!r}", path="asset_class",
            hint=f"允许值: {', '.join(ASSET_CLASSES)}",
        )
    parts = text.split(":")
    base, _, quote = parts[0].partition("/")
    return {
        "symbol": text,
        "vision_symbol": vision_symbol(text),
        "base": base,
        "quote": quote,
        "settle": parts[1] if len(parts) > 1 else None,
        "exchange": exchange,
        "market": market,
        "contract_type": contract_type,
        "underlying_type": underlying_type,
        "asset_class": asset_class,
        # 交易所原始元数据：状态以交易所为准，数据侧观测只作补充。
        "exchange_state": exchange_state,       # trading / closed / None
        "exchange_status": exchange_status,     # TRADING / BREAK / ...
        "contract_status": contract_status,     # TRADING / CLOSE / ...
        "onboard_date": onboard_date,
        "delivery_date": delivery_date,
        "listed_at": None,
        "delisted_at": None,
        "status": STATUS_UNKNOWN,
        "first_archive_month": first_archive_month,
        "first_bar": None,
        "last_bar": None,
        "bars": 0,
        "observed_at": observed_at,
    }


def attach_bars(
    record: Mapping[str, Any], *,
    first_bar: str | None, last_bar: str | None, bars: int,
) -> dict[str, Any]:
    """把数据侧观测（L0 里的首末 bar）并进元数据。

    上市时间的最终来源是**归档月**：备份站在标的上线当月就有文件，比第一根
    能读到的 bar 更早也更稳定。数据侧的值只用于补齐探测不到的标的。
    """
    out = dict(record)
    out["first_bar"] = _earliest([out.get("first_bar"), first_bar])
    out["last_bar"] = _latest([out.get("last_bar"), last_bar])
    out["bars"] = max(int(out.get("bars") or 0), int(bars or 0))
    if out.get("listed_at") is None:
        out["listed_at"] = out.get("first_archive_month") or out.get("first_bar")
    return out


def merge_universe(
    previous: Mapping[str, Mapping[str, Any]],
    discovered: Mapping[str, Mapping[str, Any]],
    *,
    observed_at: str | None = None,
    delist_gap_days: int = 7,
) -> dict[str, dict[str, Any]]:
    """把本次发现并进历史表。

    四条规则，每条都对应一个曾经踩过的坑：
    1. **状态以交易所为准**：`exchange_state=closed` 直接判退市——备份站会给
       退市币继续生成零成交量僵尸 bar，"末根 bar 距今"永远不会判退市；
    2. 交易所明确 `trading` 的才判 active，清掉 `delisted_at`——重新上市的
       标的不能还挂着退市标记；
    3. 本次未发现、也没有交易所状态时才用 `last_bar` 距今兜底——月度归档
       有发布延迟，一天的缺口不是退市；
    4. `listed_at` / `first_bar` 取历史最早，`last_bar` 取最新——刷新一次就把
       上市日往后推，会让「上市满 N 天」的筛选随时间漂移。
    """
    stamp = observed_at or datetime.now(timezone.utc).isoformat()
    out: dict[str, dict[str, Any]] = {s: dict(r) for s, r in previous.items()}

    def state_of(new: Mapping[str, Any], old: Mapping[str, Any] | None) -> str | None:
        """交易所状态优先（本次记录 > 历史记录），避免被旧状态覆盖。"""
        for rec in (new, old or {}):
            state = str(rec.get("exchange_state") or "").lower()
            if state in ("closed", "trading"):
                return state
        return None

    for symbol, new in discovered.items():
        old = out.get(symbol)
        if old is None:
            rec = dict(new)
            rec.setdefault("bars", 0)
            rec["listed_at"] = (
                rec.get("onboard_date") or rec.get("listed_at")
                or rec.get("first_archive_month") or rec.get("first_bar")
            )
        else:
            rec = dict(old)
            rec["symbol"] = symbol
            for key in (
                "vision_symbol", "base", "quote", "settle", "exchange", "market",
                "contract_type", "underlying_type", "asset_class",
                "exchange_state", "exchange_status", "contract_status",
                "onboard_date", "delivery_date",
            ):
                if new.get(key) is not None:
                    rec[key] = new[key]
            rec["listed_at"] = _earliest([
                old.get("listed_at"), new.get("onboard_date"), new.get("listed_at"),
                new.get("first_archive_month"), new.get("first_bar"),
            ])
            rec["first_archive_month"] = _earliest([
                old.get("first_archive_month"), new.get("first_archive_month"),
            ])
            rec["first_bar"] = _earliest([old.get("first_bar"), new.get("first_bar")])
            rec["last_bar"] = _latest([old.get("last_bar"), new.get("last_bar")])
            rec["bars"] = max(int(old.get("bars") or 0), int(new.get("bars") or 0))

        state = state_of(new, old)
        if state == "closed":
            rec["status"] = STATUS_DELISTED
            prior_delist = str((old or {}).get("delisted_at") or "") or None
            rec["delisted_at"] = (
                rec.get("delivery_date") or prior_delist
                or rec.get("last_bar") or stamp[:10]
            )
        else:
            rec["status"] = STATUS_ACTIVE
            rec["delisted_at"] = None
        rec["observed_at"] = stamp
        out[symbol] = rec

    for symbol, rec in out.items():
        if symbol in discovered:
            continue
        state = str(rec.get("exchange_state") or "").lower()
        if state == "closed":
            rec["status"] = STATUS_DELISTED
            rec["delisted_at"] = rec.get("delisted_at") or rec.get("last_bar") or stamp[:10]
            rec["observed_at"] = stamp
            continue
        gap = age_days(rec.get("last_bar"), stamp)
        if gap is None:
            # 从没采到数据，也没有交易所状态：不知道它是否还在交易。
            rec["status"] = STATUS_UNKNOWN
        elif gap > delist_gap_days:
            rec["status"] = STATUS_DELISTED
            rec["delisted_at"] = rec.get("last_bar")
        rec["observed_at"] = stamp
    return out


def load_universe(root: str | Path) -> dict[str, dict[str, Any]]:
    """读历史表。文件不存在返回空表——首次运行不是错误。"""
    path = universe_path(root)
    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FlowError(
                E4002, f"universe 表第 {lineno} 行不是合法 JSON", path=str(path),
                hint="该文件由 flow base universe 生成，手工编辑后需保持一行一个对象",
            ) from exc
        symbol = row.get("symbol")
        if not symbol:
            raise FlowError(E4002, f"universe 表第 {lineno} 行缺 symbol", path=str(path))
        rows[str(symbol)] = row
    return rows


def write_universe(root: str | Path, records: Mapping[str, Mapping[str, Any]]) -> Path:
    """按符号排序写回，保证同一份事实两次写入的字节一致。"""
    path = universe_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(records[symbol], ensure_ascii=False, sort_keys=True)
        for symbol in sorted(records)
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def active_symbols(records: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(s for s, r in records.items() if r.get("status") == STATUS_ACTIVE)


def all_symbols(records: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(records)


def summarize(records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """给 CLI 回执用的分布摘要（按状态与资产类别）。"""
    by_status: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for rec in records.values():
        by_status[str(rec.get("status"))] = by_status.get(str(rec.get("status")), 0) + 1
        by_class[str(rec.get("asset_class"))] = by_class.get(str(rec.get("asset_class")), 0) + 1
    return {"total": len(records), "by_status": by_status, "by_asset_class": by_class}


__all__ = [
    "BASE_DIRNAME", "UNIVERSE_RELPATH", "ASSET_CLASSES", "STABLE_BASES",
    "STATUS_ACTIVE", "STATUS_DELISTED", "STATUS_UNKNOWN",
    "base_root", "universe_path", "age_days", "classify_asset", "make_record",
    "attach_bars", "merge_universe", "load_universe", "write_universe",
    "active_symbols", "all_symbols", "summarize", "is_storable",
]
