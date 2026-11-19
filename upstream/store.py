"""供给事实的文件式存储（19-supply.md §2）：一份数据集 = 一组 parquet。

与 S2 事务协议的区别，一句话：**没有 manifest**。

- 「数据不会变」由 append-only 保证（同一 timestamp 的旧值永不覆盖），不靠版本目录；
- 「用的是哪份数据」由**读取时现算**的 checksum 保证，它仍进 `blueprint_digest`；
- 「周期」从数据本身推（相邻时间戳中位数），不信任文件名里的 tf 标签。

三条硬纪律：
1. 一个文件只装一个标的——合并成长表是读取时的事，落盘不做跨标的重排；
2. `timestamp` 必须是 UTC、时区感知；不接受 naive、不接受本地时间；
3. 写盘只经 `_atomic_write`（同盘 tmp + os.replace），读侧永不读到半个文件。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from contracts.digest import canonical_text, compute_digest
from contracts.errors import E1004, E1014, E3003, E4002, E4003, E4007, FlowError
from contracts.types import Digest, Pin

TIMESTAMP_COL = "timestamp"
SYMBOL_COL = "symbol"

# 路径段白名单（dataset / exchange / market / symbol / timeframe 共用一套规则）。
# 首字符必须是字母数字：以 `.` 开头的形态一律拒绝，不需要逐个列举 `..`。
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

# bar 序列与事件流分开：bar 的 tf 是身份的一部分，事件流的 tf 是假标签（I34）。
BAR_DATASET = "ohlcv"
EVENT_DATASETS = ("funding", "open_interest", "basis")
DATASETS = (BAR_DATASET,) + EVENT_DATASETS

# 市场维度与交易所是两件事：`binance` 同一时间既有 spot 也有 futures 目录，
# 而 `binanceusdm` 这种 id 只在合约侧存在。把「数据来自哪个市场」写成独立维度，
# 换交易所时路径规则不用动（V4 的 data/binance/{spot,futures} 就是这个形状）。
DEFAULT_MARKET = "futures"
MARKETS = ("spot", "futures")

# 每个市场能有哪些数据集。现货没有资金费、没有持仓量；basis 是「合约相对现货的
# 溢价」，定义域也在合约侧。这条表是采集与编译共用的唯一真相：在采集器里拦一次
# 比在别处拦十次可靠——采集器拦不住时，失败会被记成 skipped 而退出码仍是 0。
MARKET_DATASETS: dict[str, tuple[str, ...]] = {
    "spot": (BAR_DATASET,),
    "futures": DATASETS,
}

# 每个数据集的值列。字段清单是契约：多写一列会让 checksum 变化而无从解释。
VALUE_COLUMNS: dict[str, tuple[str, ...]] = {
    BAR_DATASET: ("open", "high", "low", "close", "volume"),
    "funding": ("funding_rate",),
    "open_interest": ("open_interest",),
    "basis": ("basis",),
}

# 只有 bar 与派生 bar 有「等距」语义；事件流/快照的间隔本身会变。
EQUIDISTANT = (BAR_DATASET, "basis")


def file_stem(symbol: str) -> str:
    """ccxt 符号 → 文件名主干（与 V4 `naming.safe_symbol` 同一规则）。

    结果**必须**再过 `_safe_segment`（调用方负责，见 `dataset_file`）：
    这里只替换 `/` 与 `:`，`\\` 与 `..` 会原样留下——在 Windows 上
    `BTC\\..\\..\\evil` 不做校验就是一次路径逃逸。
    """
    return str(symbol).strip().replace("/", "_").replace(":", "_")


def _safe_segment(value: str, *, label: str) -> str:
    """路径段校验：白名单，而不是黑名单。

    黑名单（只挡 `/`、`\\`、`:`、`..`）漏得掉 `.._.._evil` 这种形态——它不逃逸
    但也不是合法名字，会在目录里留下一个谁也读不懂的文件。白名单是
    `[A-Za-z0-9][A-Za-z0-9_.-]*`：首字符必须是字母数字，所以任何以 `.` 开头的
    形态（`..`、`...`、`.hidden`）一律拒绝。

    用 `fullmatch` 而非 `match` + `$`：Python 的 `$` 允许末尾一个 `\\n`，
    `"ohlcv\\n"` 会通过校验然后透出裸 OSError。
    """
    text = str(value)
    if not _SAFE_SEGMENT.fullmatch(text):
        raise FlowError(
            E1004, f"{label} 不能作为路径段: {value!r}", path=label,
            hint="只允许 [A-Za-z0-9][A-Za-z0-9_.-]*，且不得以 . 开头",
        )
    return text


def require_dataset(dataset: str) -> str:
    if dataset not in DATASETS:
        raise FlowError(
            E1004, f"未知数据集: {dataset!r}", path="dataset",
            hint=f"只支持 {list(DATASETS)}",
        )
    return dataset


def require_market(market: str) -> str:
    """市场维度校验。只接受 `spot` / `futures`——它同时是路径段。
    不在这里放开任意字符串：`market` 会拼进路径，拼错的后果是「读到一个空目录
    然后被解释成没数据」，而不是路径逃逸（`_safe_segment` 已挡）。
    """
    if market not in MARKETS:
        raise FlowError(
            E1004, f"未知市场: {market!r}", path="market",
            hint=f"只支持 {list(MARKETS)}；交易所 id 写在 exchange，不在 market",
        )
    return market


def require_market_dataset(market: str, dataset: str) -> None:
    """市场 × 数据集相容校验（`MARKET_DATASETS` 是唯一真相）。"""
    require_market(market)
    require_dataset(dataset)
    allowed = MARKET_DATASETS[market]
    if dataset not in allowed:
        raise FlowError(
            E1004,
            f"{market} 市场不支持数据集 {dataset}",
            path="datasets",
            hint=(
                f"{market} 只能采 {list(allowed)}；funding / open_interest / basis "
                "的定义域是合约，改用 --market futures"
            ),
        )


def require_market_symbols(market: str, symbols: Sequence[str]) -> None:
    """标的形态 × 市场相容校验（只做单向：现货不得含衍生品后缀）。

    ccxt 用 `:` 区分现货与衍生品：`BTC/USDT` 是现货，`BTC/USDT:USDT` 是线性永续。
    拿永续符号去现货所查必然失败，所以这一侧要拦。

    反方向**不拦**：并非所有交易所的合约符号都带结算币后缀（有的靠
    `defaultType` 区分，符号与现货同名）。硬要求 `:` 会误伤合法命名。那种
    情况由 `cmd_fetch` 的「零成功即失败」兜底——比猜命名约定可靠。
    """
    require_market(market)
    if market != "spot":
        return
    bad = [s for s in symbols if ":" in str(s)]
    if bad:
        raise FlowError(
            E1004,
            f"现货市场收到衍生品符号: {bad[:3]}",
            path="symbols",
            hint=(
                "`BTC/USDT:USDT` 里的 `:USDT` 是结算币，即永续合约标志；"
                "现货要写成 `BTC/USDT`。现货标的集请单列一份"
            ),
        )


def dataset_dir(
    root: str | Path, dataset: str, exchange: str, market: str = "futures",
) -> Path:
    """`<root>/<dataset>/<exchange>/<market>/`。"""
    require_dataset(dataset)
    return (
        Path(root) / _safe_segment(dataset, label="dataset")
        / _safe_segment(exchange, label="exchange")
        / _safe_segment(require_market(market), label="market")
    )


def dataset_file(
    root: str | Path,
    dataset: str,
    exchange: str,
    market: str,
    symbol: str,
    timeframe: str | None = None,
) -> Path:
    """单个标的的数据文件。

    bar 数据集必须给 timeframe（1d 与 4h 是两份数据，不是同一份的两种读法）；
    事件流数据集**不接受** timeframe——把会变的结算间隔写进文件名就是写假标签。
    """
    require_dataset(dataset)
    directory = dataset_dir(root, dataset, exchange, market)
    # 符号是外部输入（来自标的集文件或用户 --symbols）：它决定文件名，必须
    # 当场校验。`file_stem` 只替换 `/` 与 `:`，反斜杠与 `..` 会漏过去。
    stem = _safe_segment(file_stem(symbol), label="symbol")
    if dataset == BAR_DATASET:
        if not timeframe:
            raise FlowError(
                E1004, f"{dataset} 必须显式给 timeframe", path="timeframe",
            )
        return directory / f"{stem}-{_safe_segment(timeframe, label='timeframe')}.parquet"
    if timeframe:
        raise FlowError(
            E1004,
            f"{dataset} 的文件名不带 timeframe（间隔由数据决定）",
            path="timeframe",
            hint="事件流的结算间隔会变，写进文件名会成为假标签（I34）",
        )
    return directory / f"{stem}.parquet"


def _scalar(value: Any) -> Any:
    """行值 → 可进 canonical_text 的标量（与 fingerprint 体系同一时间格式）。"""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise FlowError(
                E1014, "timestamp 必须带时区", path=TIMESTAMP_COL,
                hint="落盘前统一为 UTC；naive 时间无法判断是哪一天",
            )
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def payload_checksum(frame: pd.DataFrame) -> str:
    """内容摘要，行序无关：对**排序后**的逐行 canonical_text 流式 sha256。

    为什么不用 `compute_digest(frame)`：它有 `MAX_INLINE_SEQUENCE=8192` 的护栏，
    真实面板轻松超过；而且行序会进摘要，重排一次就换一个指纹。
    """
    if frame.empty:
        digest = hashlib.sha256(b"dataset_checksum/v3\n")
        return "sha256:" + digest.hexdigest()
    keys = [c for c in (SYMBOL_COL, TIMESTAMP_COL) if c in frame.columns]
    ordered = frame.sort_values(keys, kind="mergesort")
    digest = hashlib.sha256(b"dataset_checksum/v3\n")
    for record in ordered.to_dict("records"):
        digest.update(canonical_text({k: _scalar(v) for k, v in record.items()}).encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _normalise(
    frame: pd.DataFrame, dataset: str, *, path: Path, symbol: str | None = None,
) -> pd.DataFrame:
    """列校验 + 时区归一 + 排序 + 去重。读与写共用这一处，避免两套口径。

    `symbol` 列必须存在或由调用方给出：文件名只是个定位手段，真实标的自描述在
    内容里。靠文件名反推（`BTC_USDT_USDT` → `BTC/USDT:USDT`）在多段符号上会歧义，
    而且会让同一份数据因为「带不带清单读取」得到不同指纹。
    """
    require_dataset(dataset)
    wanted = [TIMESTAMP_COL, *VALUE_COLUMNS[dataset]]
    missing = [c for c in wanted if c not in frame.columns]
    if missing:
        legacy = " 出现 date 列" if "date" in frame.columns else ""
        raise FlowError(
            E1014, f"{path.name} 缺列 {missing}{legacy}", path=str(path),
            hint="统一列名 timestamp + UTC 时区；缺列请用 flow fetch 重新采集",
        )
    out = frame.copy()
    if SYMBOL_COL not in out.columns:
        if not symbol:
            raise FlowError(
                E1014, f"{path.name} 缺 symbol 列且未给符号", path=str(path),
                hint="落盘时写入 symbol 列，读取时不必猜文件名",
            )
        out[SYMBOL_COL] = str(symbol)
    elif symbol is not None:
        actual = set(str(s) for s in out[SYMBOL_COL].dropna().unique())
        if actual and actual != {str(symbol)}:
            raise FlowError(
                E4007,
                f"{path.name} 的 symbol 列与文件名不符: {sorted(actual)} vs {symbol!r}",
                path=str(path),
                hint="一个文件只装一个标的；混装会让指纹与筛选同时失真",
            )
    out = out[[SYMBOL_COL, *wanted]]
    stamps = out[TIMESTAMP_COL]
    if not pd.api.types.is_datetime64_any_dtype(stamps):
        out[TIMESTAMP_COL] = pd.to_datetime(stamps, utc=True, errors="raise")
    elif getattr(stamps.dt, "tz", None) is None:
        raise FlowError(
            E1014, f"{path.name} 的 timestamp 是 naive 时间", path=str(path),
            hint="落盘必须是 UTC 感知时间；naive 无法判断属于哪一天",
        )
    else:
        out[TIMESTAMP_COL] = stamps.dt.tz_convert("UTC")
    for column in VALUE_COLUMNS[dataset]:
        out[column] = pd.to_numeric(out[column], errors="raise").astype("float64")
    out = out.dropna(subset=[TIMESTAMP_COL])
    out = out.sort_values([SYMBOL_COL, TIMESTAMP_COL], kind="mergesort").reset_index(drop=True)
    if out.duplicated(subset=[SYMBOL_COL, TIMESTAMP_COL]).any():
        raise FlowError(
            E4007, f"{path.name} 存在重复时间戳", path=str(path),
            hint="同一时点两次观测会被当成两个独立样本；先去重再落盘",
        )
    if out.empty:
        raise FlowError(E3003, f"{path.name} 为空", path=str(path))
    return out


def _atomic_write(frame: pd.DataFrame, path: Path) -> None:
    """同盘 tmp + os.replace；不覆盖异内容时由调用方保证幂等。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.urandom(6).hex()}.tmp")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, tmp)
    try:
        os.replace(tmp, path)
    except PermissionError:
        # Windows 上读侧持有句柄会短暂失败；这不是数据错误，重试一次即可。
        try:
            os.replace(tmp, path)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            raise


def read_file(path: Path, dataset: str, symbol: str | None = None) -> pd.DataFrame:
    """读一个标的的文件；缺文件不返回空表（fail-closed）。"""
    if not path.exists():
        raise FlowError(
            E4002, f"数据文件不存在: {path}", path=str(path),
            hint="先 flow fetch 采这份数据集；读取侧不猜路径也不补采",
        )
    try:
        frame = pq.read_table(path).to_pandas()
    except (OSError, pa.ArrowInvalid) as exc:
        raise FlowError(E3003, f"parquet 不可读: {path} ({exc})", path=str(path)) from exc
    return _normalise(frame, dataset, path=path, symbol=symbol)


def upsert(
    path: Path, frame: pd.DataFrame, dataset: str, *,
    symbol: str | None = None, revise: bool = False,
) -> dict[str, int]:
    """append-only 写入：旧值优先，冲突只计数（19-supply.md §2.3）。

    为什么保留旧值：交易所修订一根历史 K 线时，若按 `keep="last"` 覆盖，过去跑出
    的结论就无法解释。要改历史必须显式 `revise=True`，且调用方必须记录差异。
    """
    incoming = _normalise(frame, dataset, path=path, symbol=symbol)
    stats = {"added": 0, "revised": 0, "unchanged": 0, "rows": 0}
    if not path.exists():
        stats["added"] = len(incoming)
        stats["rows"] = len(incoming)
        _atomic_write(incoming, path)
        return stats

    existing = read_file(path, dataset)
    if revise:
        stats["revised"] = len(incoming)
        stats["rows"] = len(incoming)
        _atomic_write(incoming, path)
        return stats

    # 一个文件只装一个标的，所以按 timestamp 合并即可；symbol 列在合并前剥离，
    # 否则它会变成 symbol_old/symbol_new 两个常量列。
    label = str(incoming[SYMBOL_COL].iloc[0])
    value_cols = list(VALUE_COLUMNS[dataset])
    old_core = existing.drop(columns=[SYMBOL_COL])
    new_core = incoming.drop(columns=[SYMBOL_COL])
    merged = old_core.merge(
        new_core, on=TIMESTAMP_COL, how="outer", suffixes=("_old", "_new"),
        indicator=True,
    )
    added = merged["_merge"] == "right_only"
    both = merged["_merge"] == "both"

    changed = pd.Series(False, index=merged.index)
    for column in value_cols:
        old = merged[f"{column}_old"]
        new = merged[f"{column}_new"]
        # 旧值优先：old 非空就保留 old，只有新增行才取 new。
        merged[column] = old.where(old.notna(), new)
        # NaN == NaN 必须算「没变」，否则每个含空值的行都会被记成修订。
        changed |= both & ~(
            old.fillna(0.0).eq(new.fillna(0.0)) | (old.isna() & new.isna())
        )

    stats["added"] = int(added.sum())
    stats["revised"] = int((changed & both).sum())
    stats["unchanged"] = int((both & ~changed).sum())
    stats["rows"] = len(merged)

    out = merged[[TIMESTAMP_COL, *value_cols]].sort_values(
        TIMESTAMP_COL, kind="mergesort",
    ).reset_index(drop=True)
    out.insert(0, SYMBOL_COL, label)
    if stats["added"] or stats["revised"]:
        _atomic_write(out, path)
    return stats


@dataclass(frozen=True)
class Dataset:
    """一次读取的供给事实 + 现算身份。它不是落盘对象，只在内存与指纹里存在。"""

    dataset: str
    exchange: str
    market: str
    timeframe: str | None
    columns: Mapping[str, list[Any]]
    symbols: tuple[str, ...]
    rows: int
    checksum: str
    coverage: Mapping[str, datetime]
    interval_ns: int
    acquired_at: datetime
    end_closed: bool
    gaps: tuple[dict[str, Any], ...]
    per_symbol_bounds: Mapping[str, tuple[datetime, datetime, int]]

    @property
    def version(self) -> str:
        """内容摘要前 16 位——仍是内容寻址，只是不再有对应目录。"""
        return self.checksum.split(":", 1)[-1][:16]

    @property
    def passed(self) -> bool:
        return self.end_closed and not self.gaps

    @property
    def reason_code(self) -> str | None:
        if not self.end_closed:
            return E4003
        if self.gaps:
            return E4002
        return None

    def pin(self) -> Pin:
        """构造内存 Pin。字段来源见 19-supply.md §2.4。"""
        return Pin(
            dataset=self.dataset,
            version=self.version,
            checksum=Digest.parse(self.checksum),
            coverage=dict(self.coverage),
            symbols_digest=Digest.parse(compute_digest(sorted(self.symbols))),
            end_closed=self.end_closed,
            schema_version="1",
            acquired_at=self.acquired_at,
            gate_a={"passed": self.passed, "reason_code": self.reason_code},
        )

    def meta(self) -> dict[str, Any]:
        """门禁 B 要的元信息：等价于旧 manifest 的最小子集。"""
        return {
            "dataset": self.dataset,
            "version": self.version,
            "exchange": self.exchange,
            "market": self.market,
            "timeframe": self.timeframe or "",
            "rows": self.rows,
            "coverage": {k: v.isoformat() for k, v in self.coverage.items()},
            "checksum": self.checksum,
        }

    def check_window(self, start: datetime | None, end: datetime | None) -> None:
        """覆盖检查：window 必须落在已采集区间内，不足即 E4002（不缩窗）。

        这是「静默缩短研究窗口」的唯一防线：宁可让研究失败，也不能让它悄悄少跑一段。
        """
        gaps: list[str] = []
        for symbol, (lo, hi, _count) in self.per_symbol_bounds.items():
            if start is not None and start < lo:
                gaps.append(f"{symbol} 起点缺 {start.date()}..{lo.date()}")
            # 末根覆盖到 hi + interval；早于窗口终点即为缺口
            tail = hi + pd.Timedelta(nanoseconds=self.interval_ns).to_pytimedelta()
            if end is not None and tail < end:
                gaps.append(f"{symbol} 终点缺 {tail.date()}..{end.date()}")
        if gaps:
            raise FlowError(
                E4002,
                f"{self.dataset} 覆盖不足: {'; '.join(gaps[:5])}"
                + (f"（共 {len(gaps)} 项）" if len(gaps) > 5 else ""),
                path=f"supply.{self.dataset}",
                hint="延长 flow fetch 的区间或缩小计划时间轴；不得自动缩窗",
            )


def _interval_ns(frames: Mapping[str, pd.DataFrame]) -> int:
    """相邻时间戳差的中位数（纳秒）。事件流间隔会变，所以取中位数而非众数。"""
    diffs: list[pd.Series] = []
    for frame in frames.values():
        if len(frame) < 2:
            continue
        deltas = frame[TIMESTAMP_COL].diff().dropna().dt.total_seconds() * 1e9
        diffs.append(deltas[deltas > 0])
    if not diffs:
        raise FlowError(E3003, "数据不足两根，无法推导间隔", path="interval")
    pooled = pd.concat(diffs, ignore_index=True)
    return int(pooled.median())


def _gaps(
    frame: pd.DataFrame, symbol: str, interval_ns: int, now: datetime,
    *, equidistant: bool,
) -> list[dict[str, Any]]:
    """缺口：按标的逐段列。

    容差按数据集语义取：bar 是 1.5× 中位间隔（少一根就该报），事件流是 3×
    （结算间隔本身在 1h/4h/8h 之间变，用 1.5× 会把正常变化报成缺口）。
    """
    stamps = frame[TIMESTAMP_COL]
    tolerance = (1.5 if equidistant else 3.0) * interval_ns
    deltas = stamps.diff().dropna().dt.total_seconds() * 1e9
    found: list[dict[str, Any]] = []
    for index, delta in enumerate(deltas, start=1):
        if delta > tolerance:
            found.append({
                "kind": "interior", "symbol": symbol,
                "from": stamps.iloc[index - 1].isoformat(),
                "to": stamps.iloc[index].isoformat(),
            })
    tail = stamps.iloc[-1].to_pydatetime() + pd.Timedelta(nanoseconds=interval_ns).to_pytimedelta()
    if tail > now:
        found.append({
            "kind": "unclosed_end", "symbol": symbol,
            "last_bar_end": tail.isoformat(),
        })
    return found


def scan_symbols(
    root: str | Path, dataset: str, exchange: str, market: str,
    timeframe: str | None = None,
) -> tuple[str, ...]:
    """目录里已存在的标的（从文件名反推）。

    反推不是「猜数据」：文件名是唯一发现已采集标的的手段。它不参与帧身份，
    真实标的清单以读到的内容为准。
    """
    directory = dataset_dir(root, dataset, exchange, market)
    if not directory.exists():
        return ()
    suffix = f"-{timeframe}.parquet" if dataset == BAR_DATASET else ".parquet"
    found: list[str] = []
    for path in sorted(directory.glob("*.parquet")):
        name = path.name
        if not name.endswith(suffix):
            continue
        stem = name[: -len(suffix)]
        if not stem:
            continue
        found.append(stem)
    return tuple(found)


def read_dataset(
    root: str | Path,
    dataset: str,
    exchange: str,
    market: str,
    *,
    symbols: Sequence[str] | None = None,
    timeframe: str | None = None,
    now: datetime | None = None,
) -> Dataset:
    """读一份数据集：逐标的读文件 → 拼长表 → 现算身份。

    `symbols=None` 表示「目录里有什么就读什么」；给了清单则**缺失即报错**
    （研究声明的标的不许静默少读一个）。
    """
    require_dataset(dataset)
    require_market_dataset(market, dataset)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise FlowError(E1014, "now 必须带时区", path="now")

    requested = tuple(dict.fromkeys(str(s) for s in symbols)) if symbols else None
    stems = requested or scan_symbols(root, dataset, exchange, market, timeframe)
    if not stems:
        raise FlowError(
            E4002, f"{dataset} 在 {exchange}/{market} 下没有任何数据文件",
            path=f"supply.{dataset}",
            hint=f"先 flow fetch --datasets {dataset} --exchange {exchange}",
        )

    frames: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    paths: list[Path] = []
    for stem in stems:
        path = dataset_file(root, dataset, exchange, market, stem, timeframe)
        if not path.exists():
            missing.append(stem)
            continue
        frame = read_file(path, dataset)
        # 内容里的符号才是事实（文件名只是定位手段，无法反推多段符号）。
        frames[str(frame[SYMBOL_COL].iloc[0])] = frame
        paths.append(path)
    if missing:
        raise FlowError(
            E4002, f"{dataset} 缺标的文件: {missing[:5]}", path=f"supply.{dataset}",
            hint="同一份供给必须覆盖计划声明的全部标的；不得只跑有数据的那些",
        )

    # 长表：symbol + timestamp + 值列。symbol 取文件内容而不是文件名——
    # 文件名是定位手段，内容才是事实（见 _normalise 的说明）。
    long: dict[str, list[Any]] = {SYMBOL_COL: [], TIMESTAMP_COL: []}
    for column in VALUE_COLUMNS[dataset]:
        long[column] = []
    for frame in frames.values():
        long[SYMBOL_COL].extend(frame[SYMBOL_COL].astype(str).tolist())
        long[TIMESTAMP_COL].extend(frame[TIMESTAMP_COL].dt.to_pydatetime().tolist())
        for column in VALUE_COLUMNS[dataset]:
            long[column].extend(frame[column].tolist())

    long_frame = pd.DataFrame(long)[[SYMBOL_COL, TIMESTAMP_COL, *VALUE_COLUMNS[dataset]]]
    checksum = payload_checksum(long_frame)
    actual_symbols = tuple(dict.fromkeys(long_frame[SYMBOL_COL].astype(str)))

    bounds = {
        symbol: (frame[TIMESTAMP_COL].iloc[0].to_pydatetime(),
                 frame[TIMESTAMP_COL].iloc[-1].to_pydatetime(),
                 len(frame))
        for symbol, frame in frames.items()
    }
    lo = min(v[0] for v in bounds.values())
    hi = max(v[1] for v in bounds.values())
    interval_ns = _interval_ns(frames)

    gaps: list[dict[str, Any]] = []
    equidistant = dataset in EQUIDISTANT
    for symbol, frame in frames.items():
        gaps.extend(_gaps(frame, symbol, interval_ns, stamp, equidistant=equidistant))

    mtimes = [
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        for path in paths
    ]
    end_closed = not any(g["kind"] == "unclosed_end" for g in gaps)

    return Dataset(
        dataset=dataset,
        exchange=exchange,
        market=market,
        timeframe=timeframe,
        columns=long,
        symbols=actual_symbols,
        rows=sum(len(f) for f in frames.values()),
        checksum=checksum,
        coverage={"start": lo, "end": hi},
        interval_ns=interval_ns,
        acquired_at=max(mtimes),
        end_closed=end_closed,
        gaps=tuple(gaps),
        per_symbol_bounds=bounds,
    )
