"""字段/算子注册表唯一加载器（11-field-operator-registry.md §4.1）。

JSON 是真相，本模块只读它、自检它、算它的内容哈希。代码里不得再有
字符串白名单常量表——那会与 JSON 各说一套。

加载即自检（E1015）：负位移、窗口 <= 0、center、bfill、缺 impl_symbol、
degenerate_policy 是常数替换，任何一条不满足就整体失败，不跳过单条。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from v5.contracts.digest import compute_digest
from v5.contracts.errors import E1002, E1015, E2006, E2003, FlowError

_REGISTRY_DIR = Path(__file__).parent / "registry"
_BANNED_NAMES = frozenset({"center", "bfill", "backfill", "lead", "future", "pct_change_negative"})
# 名字空间四档（s0/04 §5.1）
NAMESPACES = frozenset({"open", "closed_multi", "single", "fixed"})


@dataclass(frozen=True)
class Registry:
    fields: dict[str, dict[str, Any]]
    operators: dict[str, dict[str, Any]]
    fields_digest: str
    operators_version: str

    def field(self, name: str) -> dict[str, Any]:
        if name not in self.fields:
            raise FlowError(E1015, f"字段未注册: {name}", path=f"fields.{name}")
        return self.fields[name]

    def operator(self, name: str) -> dict[str, Any]:
        if name not in self.operators:
            raise FlowError(E1015, f"算子未注册: {name}", path=f"operators.{name}")
        return self.operators[name]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise FlowError(E1015, f"注册表文件缺失: {path}", path=str(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlowError(E1015, f"{label} JSON 解析失败: {exc}", path=str(path)) from exc


def _check_operator(op: dict[str, Any], label: str) -> None:
    name = str(op.get("name", ""))
    if not name:
        raise FlowError(E1015, f"{label} 算子缺 name", path=label)
    low = name.lower()
    if low in _BANNED_NAMES or any(b in low for b in ("bfill", "backfill", "future", "lead")):
        raise FlowError(E1015, f"算子 '{name}' 命中因果禁令", path=label)
    # center 用真值判断：center:1 / "yes" 同样是居中窗口，只查 `is True`
    # 会让这些写法穿透（P1-11）。
    if op.get("center"):
        raise FlowError(E1015, f"算子 '{name}' 含居中窗口", path=label)
    if not isinstance(op.get("arity"), int) or isinstance(op.get("arity"), bool):
        raise FlowError(E1015, f"算子 '{name}' 缺合法 arity", path=f"{label}.arity")
    if not op.get("impl_symbol"):
        raise FlowError(E1015, f"算子 '{name}' 缺 impl_symbol", path=label)
    policy = op.get("degenerate_policy")
    if policy not in ("nan", "reject_batch"):
        raise FlowError(
            E1015,
            f"算子 '{name}' 的 degenerate_policy='{policy}' 非法（不得用常数兜底）",
            path=f"{label}.degenerate_policy",
        )
    # 参数域：窗口 >= 1、位移 >= 1（负位移即前视）。min/max 双向校验：
    # 只查 min 时 `{"lag":{"max":-3}}` 这类负位移/零窗口会穿透（P1-11）。
    # 不强制两界都存在——11 个无参算子（rank/add/…）的 param_domain 为空。
    for pname, domain in (op.get("param_domain") or {}).items():
        if not isinstance(domain, Mapping):
            raise FlowError(E1015, f"算子 '{name}' 的 param_domain.{pname} 非法", path=label)
        bounds: dict[str, float] = {}
        for side in ("min", "max"):
            value = domain.get(side)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FlowError(
                    E1015,
                    f"算子 '{name}' 的 {pname}.{side} 必须是数字，得到 {value!r}",
                    path=f"{label}.param_domain.{pname}.{side}",
                )
            if value < 1:
                raise FlowError(
                    E1015,
                    f"算子 '{name}' 的 {pname}.{side}={value} < 1（窗口/位移必须为正）",
                    path=f"{label}.param_domain.{pname}.{side}",
                )
            bounds[side] = value
        if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
            raise FlowError(
                E1015,
                f"算子 '{name}' 的 {pname} 下界 {bounds['min']} > 上界 {bounds['max']}",
                path=f"{label}.param_domain.{pname}",
            )
    if any(tok in str(op) for tok in ("shift(-", "pct_change(-", "periods\": -")):
        raise FlowError(E1015, f"算子 '{name}' 含负位移", path=label)


def load_registry(dirpath: str | Path | None = None) -> Registry:
    """加载两个 JSON 并整体自检。失败抛 E1015，不返回部分结果。"""
    root = Path(dirpath) if dirpath is not None else _REGISTRY_DIR
    fields_doc = _load_json(root / "fields.json", "fields")
    ops_doc = _load_json(root / "operators.json", "operators")
    params_doc = _load_json(root / "params.json", "params")
    # 三份注册表必须同 schema_version 且必填：版本漂移会让算子/字段/参数
    # 三者 belonging 到不同契约代，编译期却能静默加载。
    versions = {
        label: doc.get("schema_version")
        for label, doc in (("fields", fields_doc), ("operators", ops_doc), ("params", params_doc))
    }
    if any(not isinstance(v, str) or not v for v in versions.values()):
        raise FlowError(E1015, f"注册表缺 schema_version: {versions}", path=str(root))
    if len(set(versions.values())) != 1:
        raise FlowError(E1015, f"注册表 schema_version 不一致: {versions}", path=str(root))

    fields_list = fields_doc.get("fields") or []
    ops_list = ops_doc.get("operators") or []
    if not fields_list or not ops_list:
        raise FlowError(E1015, "注册表为空", path=str(root))

    fields: dict[str, dict[str, Any]] = {}
    for i, f in enumerate(fields_list):
        name = str(f.get("name", ""))
        if not name:
            raise FlowError(E1015, f"fields[{i}] 缺 name", path=f"fields[{i}]")
        if name in fields:
            raise FlowError(E1015, f"字段重名: {name}", path=f"fields[{i}]")
        # `unit` / `datasets` 决定字段能否用于某类数据集；缺失会让
        # 「字段清单检查」静默放行无来源字段（11 §2.1）。
        for required in ("unit", "datasets"):
            if required not in f:
                raise FlowError(E1015, f"字段 '{name}' 缺 {required}", path=f"fields[{i}].{required}")
        if not isinstance(f["datasets"], (list, tuple)) or not f["datasets"]:
            raise FlowError(E1015, f"字段 '{name}' 的 datasets 必须是非空列表",
                            path=f"fields[{i}].datasets")
        # `11` §2.1/§2.3：只允许 `>= 0`，且 close/funding_rate 明确为 0
        # （bar t 的收盘价在 t+1 开盘前已知，标签同索引不构成前视）。
        # 曾经这里要求 `>= 1`，与文档直接矛盾：它会把「可用时刻」整体推后
        # 一根 bar，与 `label.entry=next_close` 叠加后就是双重滞后。
        lag = f.get("available_lag")
        if not isinstance(lag, int) or isinstance(lag, bool) or lag < 0:
            raise FlowError(
                E1015,
                f"字段 '{name}' 的 available_lag 必须是非负整数，得到 {lag!r}",
                path=f"fields[{i}]",
            )
        fields[name] = dict(f)

    operators: dict[str, dict[str, Any]] = {}
    for i, op in enumerate(ops_list):
        _check_operator(op, f"operators[{i}]")
        name = str(op["name"])
        if name in operators:
            raise FlowError(E1015, f"算子重名: {name}", path=f"operators[{i}]")
        operators[name] = dict(op)

    return Registry(
        fields=fields, operators=operators,
        fields_digest=compute_digest(fields_doc),
        # 只有**语义字段**进摘要：`basis` / `_self_check_note` 是注释，改一句话
        # 就让所有历史计划的 `operator_set` 失效，那是把「注释」当成了口径。
        operators_version=compute_digest({
            "schema_version": ops_doc.get("schema_version"),
            "operators": [
                {
                    "name": op.get("name"),
                    "arity": op.get("arity"),
                    "impl_symbol": op.get("impl_symbol"),
                    "param_domain": op.get("param_domain") or {},
                    "degenerate_policy": op.get("degenerate_policy"),
                    "since": op.get("since"),
                }
                for op in sorted(operators.values(), key=lambda o: str(o.get("name")))
            ],
        }),
    )


_DEFAULT: "Registry | None" = None


def default_registry() -> Registry:
    """进程内单例。注册表是契约数据，一次执行内不会变。

    每次调用都重新读盘，等于给「同一次运行读了两份注册表」留了口子：两份内容
    不同时，同一条执行里的 `available_lag` 会因调用顺序而异，而两边都不会报错。
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_registry()
    return _DEFAULT


def reset_default_registry() -> None:
    """仅测试使用：让下一次 `default_registry()` 重新读盘。"""
    global _DEFAULT
    _DEFAULT = None


# 四档名字空间的解析规则（s0/04 §5.1）；供编译器 step 5 调用。
def resolve_impl(
    catalog: Mapping[str, Any], station: str, name: str | None,
    namespace: str, *, registered: set[str] | None = None,
) -> str:
    """按名字空间解析实现名。返回实现名；越界抛 E1002/E2006/E2003。"""
    if namespace not in NAMESPACES:
        raise FlowError(E2003, f"未知名字空间: {namespace}", path="namespace")
    entries = catalog.get(station) or {}
    if namespace == "fixed":
        if name is not None:
            raise FlowError(E2003, f"fixed 名字空间不得出现 impl: {station}", path=f"{station}.impl")
        if len(entries) != 1:
            raise FlowError(E2006, f"fixed 名字空间 {station} 条目数 {len(entries)} != 1", path=station)
        return next(iter(entries))
    if namespace == "single":
        if len(entries) != 1:
            raise FlowError(E2006, f"single 名字空间 {station} 条目数 {len(entries)} != 1", path=station)
        return next(iter(entries))
    if not name:
        raise FlowError(E1002, f"{station} 缺 impl 名", path=f"{station}.impl")
    if namespace == "closed_multi":
        if name not in entries:
            raise FlowError(E1002, f"实现名 '{name}' 不在 {station} 白名单", path=f"{station}.impl")
    # open 档：名字缺失已拒绝；允许目录外新增
    return name
