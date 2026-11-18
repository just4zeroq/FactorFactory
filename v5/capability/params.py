"""能力参数 / 输入 / 返回的 schema 校验（s0/04 §1 第 2–3、5 步，§2 类型系统）。

三条边界，写成代码而不是口号：

1. **schema 是契约数据**：`registry/params.json` 是唯一真相，本模块只读它。
   能力实现里不得再写一份"必填哪些键"的判断——两份规则必然漂移。
2. **多余字段一律 `E1003`**（`additional_properties=false` 的等价物）。
   `verdict.params.n_trials`、`certify.params.holdout` 属于这一类：
   它们不是"配了没用"，而是**配了就错**（C16 验收）。
3. **运行期不存在"取不到就用默认"**：默认值在编译期展开进 `values`，
   所以运行期缺必填即 `E1008`，绝不补默认。

`ref` 类型只接受字符串标识，不接受内联内容：把配置块塞进 ref 等于绕过
「口径只有一个来源」。
"""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from v5.contracts.errors import E1003, E1004, E1008, E2003, E3001, E3003, FlowError

_SCHEMA_PATH = Path(__file__).parent / "registry" / "params.json"
_SCALARS = frozenset({"int", "float", "bool", "str", "enum", "timestamp", "ref", "duration"})
_CONTAINERS = frozenset({"list", "map"})
# outputs 允许的结构标记。`any` 只留给确实无法结构化的通道（目前为零），
# 核心产物（掩码、权重、换手）用 `array`：字符串与标量不得冒充数组。
_OUTPUT_MARKERS = frozenset(
    {"str", "int", "float", "bool", "list", "map", "array", "opaque", "any"}
)
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_DURATION = re.compile(r"^\d+(bar|d|h|m)$")

_CACHE: dict[str, Any] | None = None
_DEFAULTS_CACHE: dict[str, Any] | None = None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_timestamp(value: Any, path: str) -> None:
    """UTC RFC3339，显式 `Z`。

    接受 `+08:00` 会让同一逻辑时刻有两种写法：指纹里保留原字符串，于是
    `certify.oos_window` 的边界可以静默偏移 8 小时而没有任何报错。
    时区换算一律在数据层完成，契约层只认 UTC。
    """
    if not isinstance(value, str) or not _TS.match(value):
        raise FlowError(
            E1004, f"{path} 必须是 UTC RFC3339（以 Z 结尾），得到 {value!r}", path=path,
        )
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FlowError(E1004, f"{path} 时间戳非法: {exc}", path=path) from exc


def _check_finite(value: float, path: str) -> None:
    """NaN / Infinity 不得进入任何通道。

    `nan < b` 与 `nan > b` 恒为 False，六个方向的范围检查全部穿过 NaN。
    上游若靠指纹的 `E2002` 兜底，错误码就从"范围违规"变成"指纹非法"，
    上层按码分类会误判。
    """
    if not math.isfinite(value):
        raise FlowError(E1004, f"{path} 必须是有限值，得到 {value!r}", path=path)


def _check_duration(value: Any, path: str) -> None:
    if _is_int(value):
        if value < 1:
            raise FlowError(E1004, f"{path} 跨度必须 >= 1 bar，得到 {value}", path=path)
        return
    if isinstance(value, str) and _DURATION.match(value) and int(re.match(r"^\d+", value).group()) >= 1:
        return
    raise FlowError(
        E1004, f"{path} 必须是正整数 bar 或 '5bar'/'5d' 形式，得到 {value!r}", path=path,
    )


_UNIT_NS = {"d": 86_400_000_000_000, "h": 3_600_000_000_000, "m": 60_000_000_000}


def duration_bars(value: Any, *, step_ns: int | None = None) -> int:
    """把 `duration` 归一成 bar 数。

    整数与 `5bar` 直接取 5；`5d` / `5h` / `5m` 必须给 `step_ns` 才能换算，缺
    `step_ns` 即 `E1004`——在 1d 面板上把 `5h` 当 5 bar，窗口长度整体错 24 倍，
    而这种错不会让任何测试变红，只会让「窗口 60」变成两个不同的量。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 1:
            raise FlowError(E1004, f"duration 必须 >= 1 bar，得到 {value}", path="duration")
        return value
    if not isinstance(value, str) or not _DURATION.match(value):
        raise FlowError(
            E1004, f"duration 形式非法: {value!r}", path="duration",
            hint="正整数 bar 或 '5bar' / '5d' / '5h' / '5m'",
        )
    num = int(re.match(r"^\d+", value).group())
    unit = value[len(str(num)):]
    if num < 1:
        raise FlowError(E1004, f"duration 必须 >= 1 bar，得到 {value!r}", path="duration")
    if unit == "bar":
        return num
    if not step_ns or step_ns <= 0:
        raise FlowError(
            E1004, f"duration {value!r} 需要时间步长才能换算成 bar", path="duration",
            hint="时间轴只有一根 bar 时无法换算；请改用整数 bar",
        )
    bars = int(round(num * _UNIT_NS[unit] / step_ns))
    if bars < 1:
        raise FlowError(
            E1004,
            f"duration {value!r} 在步长 {step_ns}ns 的轴上不足一根 bar",
            path="duration",
            hint="静默抬成 1 会把「1 小时」当成「1 天」，窗口整体放大 24 倍",
        )
    return bars


def _bounds(spec: Mapping[str, Any], value: float, path: str) -> None:
    for key, bad in (("min", lambda v, b: v < b), ("max", lambda v, b: v > b),
                     ("gt", lambda v, b: v <= b), ("ge", lambda v, b: v < b),
                     ("lt", lambda v, b: v >= b), ("le", lambda v, b: v > b)):
        if key in spec and bad(value, spec[key]):
            raise FlowError(
                E1004, f"{path}={value!r} 违反约束 {key}={spec[key]!r}", path=path,
            )


def _check_value(spec: Mapping[str, Any], value: Any, path: str) -> Any:
    kind = spec.get("type")
    if kind == "int":
        if not _is_int(value):
            raise FlowError(E1004, f"{path} 必须是整数，得到 {type(value).__name__}", path=path)
        _bounds(spec, value, path)
        return value
    if kind == "float":
        if not _is_number(value):
            raise FlowError(E1004, f"{path} 必须是实数，得到 {type(value).__name__}", path=path)
        _check_finite(float(value), path)
        _bounds(spec, float(value), path)
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise FlowError(E1004, f"{path} 必须是 bool，得到 {value!r}", path=path)
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise FlowError(E1004, f"{path} 必须是字符串，得到 {value!r}", path=path)
        pattern = spec.get("pattern")
        if pattern and not re.search(pattern, value):
            raise FlowError(E1004, f"{path} 不匹配 {pattern}", path=path)
        if "max_len" in spec and len(value) > spec["max_len"]:
            raise FlowError(E1004, f"{path} 长度超过 {spec['max_len']}", path=path)
        return value
    if kind == "ref":
        if not isinstance(value, str) or not value:
            raise FlowError(
                E1004, f"{path} 必须是引用标识字符串（不接受内联内容）", path=path,
            )
        return value
    if kind == "timestamp":
        _check_timestamp(value, path)
        return value
    if kind == "duration":
        _check_duration(value, path)
        return value
    if kind == "enum":
        values = spec.get("values") or []
        if not isinstance(value, str) or value not in values:
            raise FlowError(
                E1004, f"{path}={value!r} 不在白名单 {list(values)}", path=path,
            )
        return value
    if kind == "list":
        if not isinstance(value, (list, tuple)):
            raise FlowError(E1004, f"{path} 必须是列表，得到 {type(value).__name__}", path=path)
        if "min_len" in spec and len(value) < spec["min_len"]:
            raise FlowError(E1004, f"{path} 元素数 {len(value)} < {spec['min_len']}", path=path)
        if "max_len" in spec and len(value) > spec["max_len"]:
            raise FlowError(E1004, f"{path} 元素数 {len(value)} > {spec['max_len']}", path=path)
        item = spec.get("item")
        if item is None:
            return list(value)
        return [_check_value(item, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if kind == "map":
        if not isinstance(value, Mapping):
            raise FlowError(E1004, f"{path} 必须是映射，得到 {type(value).__name__}", path=path)
        if "min_len" in spec and len(value) < spec["min_len"]:
            raise FlowError(E1004, f"{path} 键数 {len(value)} < {spec['min_len']}", path=path)
        if "max_len" in spec and len(value) > spec["max_len"]:
            raise FlowError(E1004, f"{path} 键数 {len(value)} > {spec['max_len']}", path=path)
        keys = spec.get("keys")
        if keys is not None:
            out: dict[str, Any] = {}
            for key, sub in keys.items():
                if key in value:
                    out[key] = _check_value(sub, value[key], f"{path}.{key}")
                elif sub.get("required"):
                    raise FlowError(E1008, f"{path} 缺必填键 {key}", path=f"{path}.{key}")
            extra = sorted(set(value) - set(keys))
            if extra:
                raise FlowError(
                    E1003, f"{path} 含 schema 之外的键 {extra}", path=f"{path}.{extra[0]}",
                    hint="映射型的已知键由 params.json 固定",
                )
            return out
        # 自由键映射（如 read.fields：键是数据集名）：键名不预设，值按 item 递归
        item = spec.get("item")
        if item is None:
            return dict(value)
        out = {}
        for key, sub_value in value.items():
            if not isinstance(key, str):
                raise FlowError(E1004, f"{path} 的键必须是字符串，得到 {key!r}", path=path)
            out[key] = _check_value(item, sub_value, f"{path}.{key}")
        return out
    raise FlowError(E2003, f"{path} 的 schema 类型未知: {kind!r}", path=path)


def _self_check(name: str, doc: Mapping[str, Any]) -> None:
    for section in ("params", "inputs", "outputs"):
        if section not in doc:
            raise FlowError(E2003, f"能力 {name} 的 param schema 缺 {section}", path=name)
    if not isinstance(doc["params"], Mapping):
        raise FlowError(E2003, f"能力 {name}.params 必须是映射", path=f"{name}.params")
    for fname, spec in doc["params"].items():
        if not isinstance(spec, Mapping) or "type" not in spec:
            raise FlowError(E2003, f"{name}.params.{fname} 缺 type", path=f"{name}.params.{fname}")
        kind = spec["type"]
        if kind not in _SCALARS | _CONTAINERS:
            raise FlowError(E2003, f"{name}.params.{fname} 类型未知: {kind!r}",
                            path=f"{name}.params.{fname}")
        if kind == "enum" and not spec.get("values"):
            raise FlowError(E2003, f"{name}.params.{fname} 枚举无取值",
                            path=f"{name}.params.{fname}")
        if kind == "list" and not isinstance(spec.get("item"), Mapping):
            raise FlowError(E2003, f"{name}.params.{fname} 是 list 但缺 item",
                            path=f"{name}.params.{fname}")
        if kind == "map" and spec.get("keys") is None and not isinstance(spec.get("item"), Mapping):
            raise FlowError(E2003, f"{name}.params.{fname} 是 map 但既无 keys 也无 item",
                            path=f"{name}.params.{fname}")
    for i, item in enumerate(doc["inputs"]):
        if not isinstance(item, Mapping) or not item.get("kind"):
            raise FlowError(E2003, f"{name}.inputs[{i}] 缺 kind", path=f"{name}.inputs[{i}]")
    if not isinstance(doc["outputs"], Mapping) or not doc["outputs"]:
        raise FlowError(E2003, f"能力 {name}.outputs 为空", path=f"{name}.outputs")
    for key, marker in doc["outputs"].items():
        if marker not in _OUTPUT_MARKERS:
            raise FlowError(
                E2003, f"{name}.outputs.{key} 的类型标记未知: {marker!r}",
                path=f"{name}.outputs.{key}",
            )
    rule = doc.get("outputs_by")
    if rule is not None:
        if not isinstance(rule, Mapping) or not rule.get("param") or not rule.get("cases"):
            raise FlowError(E2003, f"{name}.outputs_by 结构非法", path=f"{name}.outputs_by")
        for case, spec in rule["cases"].items():
            if not isinstance(spec, Mapping):
                raise FlowError(E2003, f"{name}.outputs_by.{case} 必须是映射",
                                path=f"{name}.outputs_by.{case}")
            for key, marker in spec.items():
                if marker not in _OUTPUT_MARKERS:
                    raise FlowError(
                        E2003, f"{name}.outputs_by.{case}.{key} 类型标记未知: {marker!r}",
                        path=f"{name}.outputs_by.{case}.{key}",
                    )


def load_param_schemas(path: str | Path | None = None) -> dict[str, Any]:
    """加载并整体自检。失败抛 E2003，不返回部分结果（跳过一条等于删掉它）。"""
    target = Path(path) if path is not None else _SCHEMA_PATH
    if not target.exists():
        raise FlowError(E2003, f"能力参数 schema 缺失: {target}", path=str(target))
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlowError(E2003, f"能力参数 schema 解析失败: {exc}", path=str(target)) from exc
    caps = doc.get("capabilities")
    if not isinstance(caps, Mapping) or not caps:
        raise FlowError(E2003, "能力参数 schema 为空", path=str(target))
    for name, entry in caps.items():
        _self_check(str(name), entry)
    return {str(k): v for k, v in caps.items()}


def schemas() -> dict[str, Any]:
    """返回**深拷贝**：契约数据在进程内不得被改写。

    `schemas()["verdict"]["params"]["n_trials"] = {...}` 曾能让 C16 永久失效，
    而没有任何报错。C16 是「`n_trials` 写进 params 必须 E1003」的验收点，
    一条赋值就废掉一条验收，这种口子不能留。
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = load_param_schemas()
    return copy.deepcopy(_CACHE)


def implementation_defaults(capability: str) -> dict[str, Any]:
    """该能力的**实现缺省值**：唯一真相是 `registry/params.json` 的 `defaults`。

    与 baseline 的口径默认值是两类东西：

    - baseline 默认值是**口径**，编译期展开进 plan、进 `policy_digest`；缺了就
      `E1008`，运行期绝不补（见本模块 docstring 第 3 条）。
    - 这里是**实现细节**（分位边界、GP 种群大小、求解器正则）：不进 plan、不进
      指纹，只在调用方没有显式给值时使用。

    混成一类会让「调一下 GP 种群」悄悄改变 `policy_digest`；分成两类后，前者
    只在注册表里有唯一一处定义，后者仍遵守「口径不补默认」。
    """
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is None:
        try:
            doc = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FlowError(E2003, f"实现缺省值读取失败: {exc}", path=str(_SCHEMA_PATH)) from exc
        raw = doc.get("defaults") or {}
        if not isinstance(raw, Mapping):
            raise FlowError(E2003, "defaults 必须是映射", path="params.json.defaults")
        _DEFAULTS_CACHE = {str(k): v for k, v in raw.items()}
    entry = _DEFAULTS_CACHE.get(capability) or {}
    if not isinstance(entry, Mapping):
        raise FlowError(E2003, f"defaults.{capability} 必须是映射",
                        path=f"params.json.defaults.{capability}")
    return copy.deepcopy(dict(entry))


def reset_cache() -> None:
    """仅供测试：换一份 schema 后必须重新加载。"""
    global _CACHE, _DEFAULTS_CACHE
    _CACHE = None
    _DEFAULTS_CACHE = None


def schema_for(capability: str) -> Mapping[str, Any]:
    doc = schemas()
    if capability not in doc:
        raise FlowError(E2003, f"能力 {capability} 未登记 param schema", path="capability")
    return doc[capability]


def validate_params(capability: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """第 2 步：按 param_schema 校验。多余键 E1003、类型/范围 E1004、缺必填 E1008。"""
    doc = schema_for(capability)
    spec: Mapping[str, Any] = doc["params"]
    given = dict(params or {})
    unknown = sorted(set(given) - set(spec))
    if unknown and not doc.get("additional"):
        raise FlowError(
            E1003, f"{capability}.params 含 schema 之外的字段 {unknown}",
            path=f"{capability}.params.{unknown[0]}",
            hint="能力参数以 registry/params.json 为唯一真相；口径字段属于 caliber",
        )
    out: dict[str, Any] = {}
    for name, field in spec.items():
        if name in given:
            out[name] = _check_value(field, given[name], f"{capability}.params.{name}")
        elif field.get("required"):
            raise FlowError(
                E1008, f"{capability}.params 缺必填字段 {name}",
                path=f"{capability}.params.{name}",
                hint="默认值只在编译期展开；运行期不补默认",
            )
    if doc.get("additional"):
        for name, value in given.items():
            if name not in out:
                out[name] = value
    return out


def validate_inputs(capability: str, inputs: Any) -> tuple[Any, ...]:
    """第 3 步：数量、顺序与 kind 必须与本能力声明一致（`E3001`）。

    **按位置比对**，规则两条：

    - 尾部的可选输入可以整体省略（不写就不绑定）；
    - 中间的可选输入必须用 `None` 占位。`backtest` 的 `funding` 就是这种情况：
      省略它会让 `tiers` 前移到 `funding` 的位置，两个 kind 相同时就是
      静默错位绑定。显式 `None` 比"猜调用方想省略哪一项"安全。
    """
    spec = list(schema_for(capability)["inputs"])
    items = tuple(inputs or ())
    if len(items) > len(spec):
        raise FlowError(
            E3001,
            f"{capability} 输入数量 {len(items)} 超过声明 {len(spec)}",
            path=f"{capability}.inputs",
            hint="顺序即契约：inputs[i] 对应声明第 i 项",
        )
    for i, item in enumerate(items):
        if item is None:
            if spec[i].get("required", True):
                raise FlowError(
                    E3001, f"{capability}.inputs[{i}] 是必填输入，不得为 None",
                    path=f"{capability}.inputs[{i}]",
                )
            continue
        kind = getattr(item, "kind", None)
        if kind != spec[i]["kind"]:
            raise FlowError(
                E3001,
                f"{capability}.inputs[{i}] 的 kind={kind!r} 与声明 {spec[i]['kind']!r} 不符",
                path=f"{capability}.inputs[{i}]",
            )
    # 省略只允许出现在尾部
    for i, decl in enumerate(spec[len(items):], start=len(items)):
        if decl.get("required", True):
            raise FlowError(
                E3001, f"{capability} 缺必填输入 {decl['kind']!r}（index {i}）",
                path=f"{capability}.inputs[{i}]",
            )
    return items


# outputs 的类型标记（§1 第 5 步）：只做结构校验，不重复实现业务校验
_OUTPUT_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,), "int": (int,), "float": (int, float), "bool": (bool,),
    "list": (list, tuple), "map": (dict,), "any": (object,),
}


def _is_array(value: Any) -> bool:
    """数组型产物：numpy 数组或其容器形态。不接受标量、字符串。"""
    if isinstance(value, (str, bytes, dict)):
        return False
    if isinstance(value, (list, tuple)):
        return True
    return hasattr(value, "__array__") and hasattr(value, "shape")


def _output_spec(capability: str, doc: Mapping[str, Any], params: Mapping[str, Any] | None) -> Mapping[str, str]:
    """按参数选档返回 schema（`backtest` 的 summary / full 两档字段不同）。

    只声明 summary 档会让 `detail=full` 缺 `ledger_ref` 也算成功——两档同源
    正是 §3.11 的核心要求，缺档必须 `E3003`。
    """
    rule = doc.get("outputs_by")
    if not rule:
        return doc["outputs"]
    value = (params or {}).get(rule["param"])
    cases = rule.get("cases") or {}
    if value not in cases:
        raise FlowError(
            E3003,
            f"{capability}.{rule['param']}={value!r} 没有对应的 outputs 档位",
            path=f"{capability}.outputs",
        )
    return cases[value]


def validate_outputs(
    capability: str, payload: Any, params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """第 5 步：返回结构不符 `outputs_schema` → `E3003`，不当作成功。"""
    doc = schema_for(capability)
    spec: Mapping[str, str] = _output_spec(capability, doc, params)
    if not isinstance(payload, Mapping):
        raise FlowError(
            E3003, f"{capability} 返回值必须是映射，得到 {type(payload).__name__}",
            path=f"{capability}.outputs",
        )
    missing = [k for k in spec if k not in payload]
    if missing:
        raise FlowError(
            E3003, f"{capability} 返回值缺字段 {missing}",
            path=f"{capability}.outputs.{missing[0]}",
            hint="返回结构由 outputs_schema 冻结；缺字段不得当作成功",
        )
    extra = sorted(set(payload) - set(spec))
    if extra:
        # 与参数侧对称：契约之外的东西不得悄悄带出去（结论、指标、排序都可能藏在这里）
        raise FlowError(
            E3003, f"{capability} 返回值含 schema 之外的字段 {extra}",
            path=f"{capability}.outputs.{extra[0]}",
            hint="多带一个字段就是给下游一条未声明的通道",
        )
    for key, marker in spec.items():
        value = payload[key]
        if marker == "opaque":
            # 值对象通道（`Panel` / `Scope` / `FieldStream`）：只在调用单内部传递，
            # 不落盘、不进指纹。只要求值对象的公共接口 `digest()`——不加这一条，
            # 一个 dict 或一串 str 也能冒充「面板」，断链要等到下一个工位才炸。
            if value is None or not callable(getattr(value, "digest", None)):
                raise FlowError(
                    E3003,
                    f"{capability}.outputs.{key} 是值对象通道，必须是带 digest() 的对象，"
                    f"得到 {type(value).__name__}",
                    path=f"{capability}.outputs.{key}",
                )
            continue
        if marker == "array":
            if not _is_array(value):
                raise FlowError(
                    E3003, f"{capability}.outputs.{key} 必须是数组，"
                           f"得到 {type(value).__name__}",
                    path=f"{capability}.outputs.{key}",
                )
            continue
        expected = _OUTPUT_TYPES.get(str(marker))
        if expected is None:
            raise FlowError(E2003, f"{capability}.outputs.{key} 的类型标记未知: {marker!r}",
                            path=f"{capability}.outputs.{key}")
        if marker in ("int", "float") and isinstance(value, bool):
            raise FlowError(
                E3003, f"{capability}.outputs.{key} 必须是{marker == 'int' and '整数' or '实数'}"
                       f"（bool 不算）",
                path=f"{capability}.outputs.{key}",
            )
        if not isinstance(value, expected):
            raise FlowError(
                E3003,
                f"{capability}.outputs.{key} 类型不符: 期望 {marker}, "
                f"得到 {type(value).__name__}",
                path=f"{capability}.outputs.{key}",
            )
        if marker == "float":
            if not math.isfinite(float(value)):
                raise FlowError(
                    E3003,
                    f"{capability}.outputs.{key} 必须是有限值，得到 {value!r}",
                    path=f"{capability}.outputs.{key}",
                    hint="NaN/inf 不得当成功返回；那会让下游统计整段失效",
                )
    return dict(payload)
