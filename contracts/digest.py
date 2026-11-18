"""规范化 JSON 与八个指纹（S0 契约层）。

所有指纹都作用在**规范化字节串**上，不是作用在内存对象或原始字符串上。
规范化解决的是同一个逻辑值有多种写法的问题：键顺序、`1` 与 `1.0`、时区偏移、
Unicode 组合形式、Windows 反斜杠。不统一它们，"同一内容"就会产生多个指纹。

八条规则与对应失败模式（`s0/01-data-contracts.md` §2.1）：

============  ==========================================  ==========================
规则           内容                                         不遵守的后果
============  ==========================================  ==========================
键顺序         对象键按 UTF-8 字节序升序                      同一内容产生不同指纹
分隔符         ``;`` 与 ``:`` 后无空白                       ——
数字           整数不带小数点；整数值的浮点归一为整数；      ``1`` 与 ``1.0`` 不同指纹
              ``-0.0`` 归一为 ``0``
非法值         ``NaN`` / ``Infinity`` 直接拒绝（E2002）     不可比较、不可复现
字符串         Unicode NFC 归一；禁止孤立代理项              同义字串指纹不一致
时间           一律 UTC，RFC3339，显式 ``Z``，微秒精度        时区偏移导致指纹漂移
路径           相对根，``/`` 分隔，禁止 ``..`` 与绝对路径     换机器即失配
集合           无序集合按元素 digest 升序                     迭代顺序影响指纹
空值           ``null`` 与"字段缺失"不等价                   增删字段无法被区分
============  ==========================================  ==========================

**禁止把浮点数组放进规范化 JSON**：大数组走 `Ref`，指纹只覆盖引用与形状摘要。
这一条在代码里做成硬护栏（`MAX_INLINE_SEQUENCE`），因为"把整个面板塞进指纹"
是性能事故里最常见的一种。
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import fields as dataclass_fields, is_dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .errors import E2002, FlowError

DIGEST_PREFIX = "sha256:"

MAX_INLINE_SEQUENCE = 8192
"""单个内联序列的元素上限。超过即拒绝：那是数组，应该走 `Ref`。"""


# --------------------------------------------------------------------------
# 校验辅助
# --------------------------------------------------------------------------


def _bad(message: str, *, path: str | None = None, hint: str | None = None) -> FlowError:
    return FlowError(E2002, message, path=path, hint=hint, retryable=False)


def assert_relative_path(relpath: str) -> str:
    """校验一个相对根路径，返回归一后的字符串（`\\` 转 `/`）。

    反斜杠不做拒绝而是归一：Windows 上拼出来的路径如果在指纹里保留反斜杠，
    换到 Linux 读同一份内容就会失配。归一化解决的正是这件事；`..` 与绝对
    路径则必须拒绝，因为它们让"相对哪个根"失去意义。
    """
    if not isinstance(relpath, str) or not relpath:
        raise _bad("路径必须是非空字符串")
    normalized = relpath.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise _bad(
            f"禁止绝对路径进指纹: {relpath!r}",
            hint="改成相对数据根的路径，例如 facts/ohlcv/1d",
        )
    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        raise _bad(
            f"禁止 '..' 段进指纹: {relpath!r}",
            hint="路径必须相对根且不可回退，否则换机器后指向别处",
        )
    return normalized


# --------------------------------------------------------------------------
# 单值编码
# --------------------------------------------------------------------------


def _encode_scalar(value: Any) -> str | None:
    """编码标量；不是标量则返回 `None` 交给调用方继续分支。"""
    if value is None:
        return "null"
    if isinstance(value, bool):  # bool 是 int 子类，必须先判
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _encode_str(value)
    if isinstance(value, datetime):
        return _encode_datetime(value)
    return None


def _encode_float(value: float) -> str:
    # 先归一到 Python float：np.float64 等 float 子类的 repr 带
    # `np.float64(...)` 前缀，而 json 往返后是纯数字——不归一会让
    # 「内存里算的 payload_digest」与「reload 后重算的」不一致（S5-M2
    # 单步装载必 E3002，实测 Evidence parcel 即中招）。
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise _bad(
            f"指纹输入含非法数值 {value!r}",
            hint="NaN/Infinity 不可比较也不可复现；在下游先显式标记为不可用",
        )
    if value == 0.0:
        return "0"  # -0.0 与 0.0 归一
    if value.is_integer() and abs(value) < 2**53:
        # `1.0` 与 `1` 必须是同一个指纹（s0/01 §2.1 与 S0 验收 D1）。
        return str(int(value))
    # repr 是 Python 的最短往返表示，保证 round-trip 且确定。
    return repr(value)


def _encode_str(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # 孤立代理项
        raise _bad(
            "字符串含孤立代理项，无法编码为 UTF-8",
            hint="检查上游是否用 surrogateescape 读入了非 UTF-8 文件名或字段",
        ) from exc
    normalized = unicodedata.normalize("NFC", value)
    return json.dumps(normalized, ensure_ascii=False)


def _encode_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise _bad(
            f"naive datetime 不能进指纹（必须带时区）: {value.isoformat()}",
            hint="补上 tzinfo（UTC 用 timezone.utc）；静默把本地时间当 UTC 会偏移数小时",
        )
    utc = value.astimezone(timezone.utc)
    return json.dumps(utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def _sorted_items(value: Mapping[str, Any]) -> list[tuple[str, Any]]:
    for key in value:
        if not isinstance(key, str):
            raise _bad(f"规范化 JSON 的对象键必须是字符串，得到 {type(key).__name__}")
    # 按 UTF-8 字节序，不是按 Unicode 码位序：二者对 BMP 之外字符不同。
    return sorted(value.items(), key=lambda kv: kv[0].encode("utf-8"))


def _encode_mapping(value: Mapping[str, Any], depth: int) -> str:
    parts = [
        f"{_encode_str(k)}:{canonical_text(v, depth=depth + 1)}"
        for k, v in _sorted_items(value)
    ]
    return "{" + ",".join(parts) + "}"


def _encode_sequence(value: Sequence[Any], depth: int) -> str:
    if len(value) > MAX_INLINE_SEQUENCE:
        raise _bad(
            f"内联序列长度 {len(value)} 超过 {MAX_INLINE_SEQUENCE}",
            hint="大数组不进规范化 JSON：落成列式文件后用 Ref 引用，指纹只覆盖引用与形状",
        )
    return "[" + ",".join(canonical_text(v, depth=depth + 1) for v in value) + "]"


def _encode_set(value: Iterable[Any], depth: int) -> str:
    # 无序集合按元素 digest 升序，保证不同进程、不同哈希种子下顺序一致。
    items = list(value)
    decorated = sorted(
        (
            hashlib.sha256(canonical_text(item, depth=depth + 1).encode("utf-8")).hexdigest(),
            index,
        )
        for index, item in enumerate(items)
    )
    ordered = [items[index] for _, index in decorated]
    return _encode_sequence(ordered, depth)


def canonical_text(value: Any, *, depth: int = 0) -> str:
    """把一个值编码为规范化 JSON 文本（不含行尾空白）。"""
    if depth > 64:
        raise _bad("规范化输入嵌套过深（>64），可能是自引用结构")

    scalar = _encode_scalar(value)
    if scalar is not None:
        return scalar

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise _bad(
            "二进制内容不能直接进指纹",
            hint="落盘后用 Ref 引用；指纹只覆盖 digest 与形状",
        )

    hook = getattr(value, "__flow_canonical__", None)
    if hook is not None:
        return canonical_text(hook(), depth=depth)

    if is_dataclass(value) and not isinstance(value, type):
        return _encode_mapping(
            {f.name: getattr(value, f.name) for f in dataclass_fields(value)}, depth
        )

    if isinstance(value, MappingProxyType) or isinstance(value, Mapping):
        return _encode_mapping(value, depth)
    if isinstance(value, (set, frozenset)):
        return _encode_set(value, depth)
    if isinstance(value, (list, tuple)):
        return _encode_sequence(value, depth)

    raise _bad(
        f"不可规范化的类型 {type(value).__name__}",
        hint="跨层结构只允许标量、字符串、列表、映射、Ref；不存活对象",
    )


def canonical_bytes(value: Any) -> bytes:
    """规范化文本的 UTF-8 字节串。指纹只作用在它上面。"""
    return canonical_text(value).encode("utf-8")


def compute_digest(value: Any) -> str:
    """`H(x) = "sha256:" + hex(sha256(canonical_bytes(x)))`。"""
    return DIGEST_PREFIX + hashlib.sha256(canonical_bytes(value)).hexdigest()


def blob_digest(content: bytes) -> str:
    """原始字节的摘要，用于内容寻址对象（parquet / json 落盘）。

    与 compute_digest 的区别：后者只接受可规范化结构（拒绝 bytes，见 E2002）；
    对象存储寻址的是「磁盘上的字节」，必须直接哈希文件内容，不能经过 JSON 规范化。
    """
    if not isinstance(content, bytes):
        raise _bad(f"blob_digest 只接受 bytes，得到 {type(content).__name__}")
    return DIGEST_PREFIX + hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------
# 八个指纹
#
# 每个函数都显式列出参与哈希的键名，而不是把整个对象塞进去。理由：
# `s0/01 §2.3` 的"什么进指纹"表里有大量"否"，自动序列化无法表达这些排除，
# 而且以后给对象加一个字段就会静默改变全部历史指纹。
# --------------------------------------------------------------------------


def plan_digest(plan_raw: Any) -> str:
    """`H(plan_raw)`。只覆盖用户写下的计划原文。"""
    return compute_digest({"kind": "plan", "raw": plan_raw})


def caliber_digest(
    *,
    execution: Any,
    label: Any,
    allocate: Any,
    net: Any,
) -> str:
    """`H(execution, label, allocate, net)`。四子块整体参与口径。"""
    return compute_digest(
        {
            "kind": "caliber",
            "execution": execution,
            "label": label,
            "allocate": allocate,
            "net": net,
        }
    )


def domain_digest(
    *,
    members: Sequence[Mapping[str, Any]],
    fit_window: Any,
    embargo: Any,
    eval_window: Any,
) -> str:
    """`H(sorted(members: id, version, available_from), fit_window, embargo, eval_window)`。"""
    for member in members:
        missing = {"id", "version", "available_from"} - set(member)
        if missing:
            raise _bad(
                f"组合域成员缺字段 {sorted(missing)}",
                hint="每个成员必须有 id / version / available_from；缺 available_from 就无法判前视",
            )
    ordered = sorted(
        members,
        key=lambda m: (str(m["id"]), str(m["version"]), canonical_text(m["available_from"])),
    )
    return compute_digest(
        {
            "kind": "domain",
            "members": ordered,
            "fit_window": fit_window,
            "embargo": embargo,
            "eval_window": eval_window,
        }
    )


def supply_digest(pins: Sequence[Any]) -> str:
    """`H(sorted(pins))`。

    **排除 `acquired_at`**：它是"采集完成时间"，属于 §2.3「只要一个字段会随
    时间变化，它就不能进任何指纹」的硬性排除项。把重采一次数据当成"事实变了"
    会让全部下游产物无谓失效，而内容其实逐字节相同。
    """
    views = [_pin_digest_view(p) for p in pins]
    ordered = sorted(views, key=lambda v: (str(v["dataset"]), str(v["version"])))
    return compute_digest({"kind": "supply", "pins": ordered})


_PIN_VIEW_KEYS = (
    "dataset", "version", "checksum", "coverage", "symbols_digest",
    "end_closed", "schema_version", "gate_a",
)


def _pin_digest_view(pin: Any) -> Mapping[str, Any]:
    hook = getattr(pin, "digest_view", None)
    if hook is not None:
        return hook()
    if isinstance(pin, Mapping):
        # 映射形态也必须按**白名单**投影：只剔除 acquired_at 会让 updated_at /
        # path / note 这类会随时间或机器变化的字段混进指纹。
        return {k: pin[k] for k in _PIN_VIEW_KEYS if k in pin}
    raise _bad(
        f"supply_digest 收到不可识别的 pin 类型 {type(pin).__name__}",
        hint="pin 必须是 Pin 值对象或映射",
    )


def environment_digest(
    *,
    version_subset: Any,
    clock_basis: Any,
    rng_impl: Any,
    resource_limits: Any,
) -> str:
    """`H(version_subset, clock_basis, rng_impl, resource_limits)`。

    它**不进任何产物指纹**。用途是单独记录，在重放不一致时作为第一排查对象。
    一旦进了产物指纹，换台机器或升级无关依赖就会让全部历史产物"失效"，
    可复现反而不可比。
    """
    return compute_digest(
        {
            "kind": "environment",
            "version_subset": version_subset,
            "clock_basis": clock_basis,
            "rng_impl": rng_impl,
            "resource_limits": resource_limits,
        }
    )


def policy_digest(
    *,
    baseline_version: str,
    constitutive_subset: Any,
    caliber: str,
    domain: str,
) -> str:
    """`H(baseline_version, constitutive_subset, caliber_digest, domain_digest)`。

    不含 `supply_pins`：数据刷新是事实演进，不是研究自由度。它让下游失效，
    但不新建试验族。
    """
    return compute_digest(
        {
            "kind": "policy",
            "baseline_version": baseline_version,
            "constitutive_subset": constitutive_subset,
            "caliber_digest": caliber,
            "domain_digest": domain,
        }
    )


def blueprint_digest(
    *,
    baseline_version: str,
    values: Any,
    caliber: Any,
    supply_pins: Sequence[Any],
    domain: str,
) -> str:
    """`H(baseline_version, values, caliber, supply_pins, domain_digest)`。

    与 `policy_digest` 的唯一区别是**含数据版本**：蓝图描述"展开后的方案"，
    方案里的事实版本变了，蓝图就是另一个蓝图。
    """
    return compute_digest(
        {
            "kind": "blueprint",
            "baseline_version": baseline_version,
            "values": values,
            "caliber": caliber,
            "supply_pins": [_pin_digest_view(p) for p in supply_pins],
            "domain_digest": domain,
        }
    )


def runtime_facts_digest(runtime_facts: Mapping[str, Any]) -> str:
    """`H(计数快照, 揭示状态, 审核状态)`。

    这是"本次执行读到的快照"，不是账本里的滚动数字。前者改变判据，必须让
    缓存失效；后者每次都不同，进指纹等于让缓存永远失效。
    """
    return compute_digest({"kind": "runtime_facts", "facts": dict(runtime_facts)})


def call_digest(
    *,
    station: str,
    station_revision: str,
    impl: str,
    impl_version: str,
    phase: str,
    params_digest: str,
    input_digests: Sequence[str],
    policy: str,
    runtime_facts: str,
    composed_digests: Sequence[str],
    seed: int | None,
    capability: str = "",
    input_kinds: Sequence[str] = (),
) -> str:
    """`H(station@revision, impl@version, params_digest, inputs, policy, facts, composed, seed)`。

    两项说明：

    - **`seed` 必须在内**。它改变结果却不改变参数，漏掉就会错误命中缓存。
    - **`phase` 也在内**，这一点是 `s0/01 §2.2` 的公式没有列出、但必须加的：
      `fit` 与 `generate` 是同一个实现的两条不同计算路径，不进指纹就会出现
      "拟合结果被当成生成结果命中"。理由与 `seed` 完全相同，见
      `s0/01-data-contracts.md` §2.2 的"改变结果的东西必须进指纹"原则。

    能力层额外两个通道（工位层传空，因此工位指纹不受影响）：

    - **`capability`**：`evaluate@qweave@1` 与 `align@qweave@1` 若只按 impl 名
      算，两个不同能力的调用单会共享同一个缓存键——这正是 §1.3「换成本实现而
      父产物命中旧缓存」的形态。
    - **`input_kinds`**：`kind` 是 `E3001` 的唯一判据。只算 digest 时，把输入
      角色从 `payload` 改成 `panel` 指纹不变，"输入顺序即契约"就只剩运行期
      瞬时成立。序列与 `input_digests` 一一对应。
    """
    return compute_digest(
        {
            "kind": "call",
            "station": f"{station}@{station_revision}",
            "capability": capability,
            "impl": f"{impl}@{impl_version}",
            "phase": phase,
            "params_digest": params_digest,
            "input_digests": list(input_digests),
            "input_kinds": list(input_kinds),
            "policy_digest": policy,
            "runtime_facts_digest": runtime_facts,
            "composed_digests": list(composed_digests),
            "seed": seed,
        }
    )


def parcel_digest(call: str, payload: str) -> str:
    """`H(call_digest, payload_digest)`。

    与 `call_digest` 必须分开：前者在计算前就能算出，用于查缓存与判依赖；
    后者含输出，用于标识产物。合成一个，缓存就永远无法命中。
    """
    return compute_digest({"kind": "parcel", "call_digest": call, "payload_digest": payload})


__all__ = [
    "DIGEST_PREFIX",
    "MAX_INLINE_SEQUENCE",
    "assert_relative_path",
    "blueprint_digest",
    "caliber_digest",
    "call_digest",
    "canonical_bytes",
    "canonical_text",
    "compute_digest",
    "domain_digest",
    "environment_digest",
    "parcel_digest",
    "plan_digest",
    "policy_digest",
    "runtime_facts_digest",
    "supply_digest",
]
