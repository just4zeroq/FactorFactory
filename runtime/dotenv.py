"""本地 ``.env`` 配置读取（标准库实现，刻意不引入 python-dotenv）。

``requirements.txt`` 的原则是「只声明代码直接 import 的包」，而这里需要的只是
``KEY=VALUE`` 这一个语法子集：为一个十几行的解析器引入新依赖，等于让环境复现
多一个变量。支持的语法写在 `parse_env` 里，**超出范围一律报错，不静默忽略**——
配置写错却按默认数据根跑，是比直接失败更糟的结果。

语义与常见 dotenv 一致：

- 进程里已有的环境变量优先，``.env`` 不覆盖（shell / CI 的显式设置不该被本地
  文件悄悄改掉）；需要 .env 强制覆盖时显式传 ``override=True``。
- 只注入 ``.env`` 里出现的键，不读写其它文件，不执行命令，不做跨文件引用。
- ``$VAR`` / ``${VAR}``（Windows 上还有 ``%VAR%``）由 `os.path.expandvars` 展开；
  单引号内的值不展开。未解析的 ``${...}`` 占位是错误，不许混进路径。
- ``~`` 不在这里展开：数据根由 `runtime.store.resolve_data_root` 用
  `Path.expanduser()` 处理，其它值原样注入环境。

查找顺序（第一个存在的生效，找不到就当没有 ``.env``）：

1. ``$FLOW_ENV_FILE`` 显式指定的路径（从别的目录启动 / CI 用）；
2. 当前工作目录下的 ``.env``；
3. 仓库根（由本文件位置推导）下的 ``.env``。

故意**不向上级目录逐级搜索**：那会静默捡到某个无关的 ``.env``，把数据根换到
另一台机器的路径上，破坏可复现性。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, MutableMapping

from contracts.errors import E1004, FlowError

ENV_FILE_NAME = ".env"
#: 显式指定 .env 位置的进程环境变量（不能写在 .env 自己里面）。
ENV_FILE_PATH_VAR = "FLOW_ENV_FILE"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_UNRESOLVED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_INLINE_COMMENT = re.compile(r"\s+#")

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r",
    "\\": "\\", '"': '"', "'": "'", "$": "$",
}

__all__ = [
    "ENV_FILE_NAME", "ENV_FILE_PATH_VAR", "PROJECT_ROOT",
    "parse_env", "find_env_file", "dotenv_values", "load_dotenv",
]


def _expand(value: str, lineno: int) -> str:
    expanded = os.path.expandvars(value)
    if _UNRESOLVED.search(expanded):
        raise ValueError(f"第 {lineno} 行存在未解析的变量占位: {value!r}")
    return expanded


def _parse_value(raw: str, lineno: int) -> str:
    """拆解 ``=`` 右侧：引号 / 转义 / 行内注释 / 变量展开。"""
    text = raw.strip()
    if not text:
        return ""
    if text[0] in "\"'":
        quote = text[0]
        chars: list[str] = []
        i = 1
        while i < len(text):
            ch = text[i]
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                chars.append(_ESCAPES.get(text[i + 1], "\\" + text[i + 1]))
                i += 2
                continue
            if ch == quote:
                break
            chars.append(ch)
            i += 1
        else:
            raise ValueError(f"第 {lineno} 行引号未闭合: {text!r}")
        trailing = text[i + 1:].strip()
        if trailing and not trailing.startswith("#"):
            raise ValueError(f"第 {lineno} 行闭合引号后有多余内容: {trailing!r}")
        value = "".join(chars)
        # 单引号 = 字面量，不展开变量（与 dotenv 习惯一致）。
        return value if quote == "'" else _expand(value, lineno)
    # 未加引号：只有「空白 + #」才算注释，`abc#def` 是值的一部分。
    value = _INLINE_COMMENT.split(text, maxsplit=1)[0].rstrip()
    return _expand(value, lineno)


def parse_env(text: str) -> dict[str, str]:
    """把 ``.env`` 文本解析成 ``{KEY: VALUE}``；语法错误抛 `ValueError`。

    支持：``#`` 注释、空行、``export KEY=VALUE``、单/双引号、双引号内的
    ``\\n`` / ``\\t`` / ``\\r`` / ``\\\\`` / ``\\$`` 转义、未加引号值的行内注释。
    后出现的同名键覆盖先出现的（一个文件内，最后一处说了算）。
    """
    out: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY.match(line)
        if not match:
            raise ValueError(f"第 {lineno} 行不是 KEY=VALUE: {line!r}")
        out[match.group(1)] = _parse_value(match.group(2), lineno)
    return out


def find_env_file(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path | None:
    """定位 ``.env``；返回 None 表示没有本地配置文件（不是错误）。"""
    env = os.environ if environ is None else environ
    explicit = env.get(ENV_FILE_PATH_VAR)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FlowError(
                E1004, f"{ENV_FILE_PATH_VAR} 指向的文件不存在: {path}",
                hint="改成 .env 的绝对路径，或取消该变量以使用默认查找",
                path=ENV_FILE_PATH_VAR,
            )
        return path
    base = Path.cwd() if cwd is None else Path(cwd)
    for candidate in (base / ENV_FILE_NAME, PROJECT_ROOT / ENV_FILE_NAME):
        if candidate.is_file():
            return candidate
    return None


def dotenv_values(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> dict[str, str]:
    """只读地返回 ``.env`` 的键值，不改动 `os.environ`。"""
    target = path if path is not None else find_env_file(environ=environ, cwd=cwd)
    if target is None:
        return {}
    target = Path(target).expanduser()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise FlowError(
            E1004, f".env 无法读取: {exc}", path=str(target),
            hint="检查文件权限与编码（UTF-8）",
        ) from exc
    try:
        return parse_env(text)
    except ValueError as exc:
        raise FlowError(
            E1004, f".env 格式错误: {exc}", path=str(target),
            hint="每行必须是 KEY=VALUE；注释单独成行以 # 开头",
        ) from exc


def load_dotenv(
    path: str | Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    override: bool = False,
) -> dict[str, str]:
    """把 ``.env`` 注入环境，返回实际写入的键值。

    默认 ``override=False``：已存在的进程环境变量不变。`environ` 供测试注入
    一个隔离的 dict，避免污染整个进程。
    """
    target: MutableMapping[str, str] = os.environ if environ is None else environ
    values = dotenv_values(path, environ=target)
    applied: dict[str, str] = {}
    for key, value in values.items():
        if override or key not in target:
            target[key] = value
            applied[key] = value
    return applied
