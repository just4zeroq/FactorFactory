"""包独立性守卫（依赖方向约束）。

历史背景：本仓库的前身与 core/api/flow/web 四套旧实现并存于同一仓库，
任何跨包 import 都会让「旧路径保持可跑」这条回滚手段失效。拆分为独立
仓库后，那些包已不在树内，但**约束仍然有效**——它们哪天被 vendored 回来，
或有人从旧仓库复制代码片段，静态扫描会在 CI 里先红，而不是等运行期才暴露。

同一套扫描还守着三条结构不变量：契约层无业务依赖、能力层不碰存储/路径、
能力签名不含账本/配置/结论入参。
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# 顶层的八个包（原 v5/ 下的一层目录，现在直接位于仓库根）。
PACKAGES = (
    "contracts", "facilities", "storage", "upstream",
    "runtime", "capability", "orchestration", "app",
)
FORBIDDEN_TOP = {"core", "api", "flow", "web"}


def _sources():
    """全部生产源码（不含 tests/、docs/、examples/）。"""
    for pkg in PACKAGES:
        yield from sorted((ROOT / pkg).rglob("*.py"))


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_v5_never_imports_legacy_packages() -> None:
    """v5/** 的绝对 import 不得指向 core/api/flow/web。"""
    violations = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _import_roots(tree) & FORBIDDEN_TOP
        if bad:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(bad)}")
    assert not violations, "不得 import 旧包：\n" + "\n".join(violations)


def test_contracts_has_no_runtime_dependency() -> None:
    """契约层只依赖标准库——它是全系统最底层，不能有业务依赖。"""
    allowed = {
        "__future__", "abc", "dataclasses", "datetime", "decimal", "enum",
        "functools", "hashlib", "json", "math", "re", "types", "typing",
        "unicodedata", "uuid", "collections", "pathlib", "copy", "numbers",
    }
    violations = []
    for path in sorted((ROOT / "contracts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots = _import_roots(tree)
        bad = {r for r in roots if r not in allowed and r != "contracts"}
        if bad:
            violations.append(f"{path.name}: {sorted(bad)}")
    assert not violations, "契约层出现非标准库依赖：\n" + "\n".join(violations)


def test_capability_has_no_storage_or_path_access() -> None:
    """AC31：能力层不得 import duckdb/pyarrow/拼任何文件路径。

    能力是纯计算，只拿事实层给的 handle；一旦自己读文件或查库，
    「只读 handle」的边界就破了，C20 的结构保证失效。
    """
    # 只约束 ops/（纯计算能力）。registry.py 是编译期契约数据加载器，
    # 契约明确它读两个 JSON 文件（11 §4.1），用 pathlib 是允许的。
    forbidden = {"duckdb", "pyarrow", "pathlib", "os", "io"}
    violations = []
    for path in sorted((ROOT / "capability" / "ops").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _import_roots(tree) & forbidden
        if bad:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(bad)}")
    assert not violations, "能力层越权访问存储/路径：\n" + "\n".join(violations)


def test_capability_ops_have_no_ledger_or_verdict_params() -> None:
    """五条禁令的静态检查：ops 函数签名不含账本/配置/结论/指标入参。"""
    banned = {"ledger", "config", "verdict", "p_value", "promote", "significant", "metrics"}
    violations = []
    for path in sorted((ROOT / "capability" / "ops").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [a.arg for a in node.args.args + node.args.kwonlyargs]
                bad = set(names) & banned
                if bad:
                    violations.append(f"{path.name}:{node.name}: {sorted(bad)}")
    assert not violations, "能力签名出现禁止参数：\n" + "\n".join(violations)


def test_contracts_files_exist() -> None:
    """七个契约文件齐全（含 plan_schema.json）。"""
    expected = {
        "__init__.py", "types.py", "digest.py", "errors.py",
        "exit_codes.py", "plan_schema.py", "plan_schema.json",
    }
    actual = {p.name for p in (ROOT / "contracts").iterdir()}
    assert expected <= actual, f"缺契约文件：{sorted(expected - actual)}"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])




def test_annualisation_constant_has_one_definition():
    """G8：365 是加密 24/7 的定义性常量，只允许有一处定义。

    它决定 `bars_per_year` 与年化公式；开放成参数等于允许同一份 Sharpe 有
    两个口径，所以这里守卫的是「单一来源」，不是「可调」。
    """
    import re

    hits = []
    for path in _sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*(?:CALENDAR_DAYS_PER_YEAR|_ANNUALISATION|_ANNUAL_DAYS)"
                        r"\s*=\s*365(?:\.0)?\b", line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert hits == ["capability/timeaxis.py:40"], hits


def test_block_bootstrap_minimum_is_not_a_tunable():
    """G8：`_BLOCK_MIN=4` 改一个值就改零分布与 `empirical_p`，属判据的一部分，
    不是实现缺省。钉住它，防止有人把它搬进 defaults 当旋钮。"""
    from capability.ops.observe import _BLOCK_MIN

    assert _BLOCK_MIN == 4

def test_call_has_exactly_one_definition() -> None:
    """`Call` 全仓只能有一处定义。

    曾经有两份：契约层 16 字段（`inputs: list[Ref]`、三个子摘要存字段）与能力层
    12 字段（`inputs: tuple[InputRef]`、摘要现算）。生产代码只构造后者，前者是
    一张没人对得上的规格表，而且**校验更强**——于是"哪个才算数"只能靠猜，空
    `impl` 这类输入在两份定义下会走到两个不同的错误码。这条断言把"再抄一份"
    挡在 CI 里：加回第二份，它会红。
    """
    hits = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Call":
                hits.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    assert hits == ["capability/runtime.py"], hits

def test_method_stage_tags_have_one_source():
    """G1/G2：`@S3`/`@S4`/`@S7` 方法标签不得散落在实现里。

    曾经五处各写一遍；其中组合域写 @S4 而实现已在 S7，读代码的人无法确定
    哪个才是口径。现在只有规格层的一张表，改它必过指纹对拍。
    """
    import ast
    import re

    hits = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"@S[0-9]", node.value):
                    hits.append((path.relative_to(ROOT).as_posix(), node.value))
    assert {p for p, _ in hits} <= {"capability/spec.py"}, hits[:10]
    from capability.spec import method_id

    assert method_id("combine_domain") == "combine_domain@S7"
    assert method_id("simple_longshort") == "simple_longshort@S3"
    assert method_id("allocate+backtest") == "allocate+backtest@S4"

