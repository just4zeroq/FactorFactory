"""环境快照（s0/03-runtime.md §7）。

环境可记录但不进 parcel_digest——换机器、升级无关依赖不该让历史产物失效。
三档处理：同环境完全可复现 / 跨环境产物相同记 env_mismatch / 跨环境产物不同
再记 reproduction_failed，但**仍不判失败**。

凭据三条禁令（硬）：
1. 值绝不出现在 Blueprint / Parcel / 日志 / trace / 报告 / 指纹任何一处；
2. 取值失败必须 E5002，不得回退匿名或只读；
3. 只存引用名（环境变量名 / 密钥别名），因为引用名不泄露值。
"""

from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from v5.contracts.digest import environment_digest
from v5.contracts.errors import E2004, E5002, FlowError

# 明确禁止出现在环境快照里的键名片段——即便调用方误传也要拦。
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "credential_value")
# 引用名必须是**环境变量名**（全大写）。凭据值几乎不可能是这种形态，
# 这条比"含 = 或长度 > 128"可靠得多：`sk_live_ABCDEF…` 两个条件都不满足。
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class Envelope:
    python_version: str
    package_versions: Mapping[str, str]
    platform: str
    machine: str
    cpu_count: int
    blas_threads: str
    clock_basis: str
    rng_impl: str
    resource_limits: Mapping[str, Any]
    device: str
    data_root_resolved: str
    credential_refs: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "python_version": self.python_version,
            "package_versions": dict(sorted(self.package_versions.items())),
            "platform": self.platform,
            "machine": self.machine,
            "cpu_count": self.cpu_count,
            "blas_threads": self.blas_threads,
            "clock_basis": self.clock_basis,
            "rng_impl": self.rng_impl,
            "resource_limits": dict(self.resource_limits),
            "device": self.device,
            "data_root_resolved": self.data_root_resolved,
            "credential_refs": dict(sorted(self.credential_refs.items())),
        }

    @property
    def digest(self) -> str:
        """只用于记录与跨环境比对，不进 parcel_digest。"""
        d = self.to_dict()
        return environment_digest(
            version_subset={"python": d["python_version"], "packages": d["package_versions"]},
            clock_basis=d["clock_basis"], rng_impl=d["rng_impl"],
            resource_limits=d["resource_limits"],
        )

    def compare(self, other: "Envelope", *, parcel_digest_same: bool) -> dict[str, Any]:
        """返回跨环境比对结果；任何情形都不产生失败结论。"""
        same = self.digest == other.digest
        return {
            "env_mismatch": not same,
            "reproduction_failed": (not same) and (not parcel_digest_same),
            "this_environment_digest": self.digest,
            "other_environment_digest": other.digest,
        }


def _blas_threads() -> str:
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        value = os.environ.get(var)
        if value:
            return f"{var}={value}"
    return "unset"


def _resource_limits() -> dict[str, Any]:
    try:
        import resource  # POSIX only
        out = {}
        for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_NOFILE"):
            value = getattr(resource, name, None)
            if value is not None:
                soft, hard = resource.getrlimit(value)
                out[name] = {"soft": soft, "hard": hard}
        return out
    except (ImportError, OSError):
        return {}


def build_envelope(
    data_root: Path,
    *,
    packages: Mapping[str, str] | None = None,
    credential_env_names: Mapping[str, str] | None = None,
    rng_impl: str = "v5.facilities.rng",
) -> Envelope:
    """构造环境快照。

    credential_env_names: 用途名 → 环境变量名（只记录名字，不读取值）。
    若要校验凭据存在，用 require_credentials()——它会取值但绝不保存。
    """
    refs = dict(credential_env_names or {})
    for name, ref in refs.items():
        if not isinstance(ref, str) or not _ENV_VAR_NAME.fullmatch(ref):
            raise FlowError(
                E2004,
                f"凭据引用必须是环境变量名（全大写）: {name}={ref!r}",
                hint="只记录名字，不记录值；把值传进来会随快照落盘",
                path=f"credential_refs.{name}",
            )
    return Envelope(
        python_version=sys.version.split()[0],
        package_versions=dict(packages or {}),
        platform=platform.platform(),
        machine=platform.machine(),
        cpu_count=os.cpu_count() or 0,
        blas_threads=_blas_threads(),
        clock_basis="utc",
        rng_impl=rng_impl,
        resource_limits=_resource_limits(),
        device="cpu",
        data_root_resolved=str(Path(data_root).resolve()),
        credential_refs=refs,
    )


def require_credentials(names: Mapping[str, str], *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """校验凭据存在并原样返回给调用方使用——但调用方不得写入任何产物。

    取值失败即 E5002，明确禁止回退匿名/只读模式（禁令 2）。
    """
    env = environ if environ is not None else os.environ
    out: dict[str, str] = {}
    for purpose, env_name in names.items():
        value = env.get(env_name)
        if not value:
            raise FlowError(
                E5002,
                f"凭据缺失: {purpose} 需要环境变量 {env_name}",
                hint="不得回退匿名或只读模式",
                path=f"credentials.{purpose}",
            )
        out[purpose] = value
    return out
