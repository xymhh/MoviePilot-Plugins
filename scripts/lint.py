#!/usr/bin/env python3
"""
插件仓库 Python 代码质量检查脚本

检查项：
- 语法错误（py_compile + ast.parse）
- Tab 字符混入
- 非 4 空格倍数缩进（排除字符串内容）
- 空行含空格/Tab

用法：
    python scripts/lint.py <file1.py> [file2.py] ...
    python scripts/lint.py --all
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from py_compile import PyCompileError, compile
from typing import Iterable, List, Tuple

_error_count = 0
_warning_count = 0


def _error(path: Path, lineno: int, msg: str) -> None:
    global _error_count
    _error_count += 1
    print(f"  ERROR {path}:{lineno}: {msg}")


def _warn(path: Path, lineno: int, msg: str) -> None:
    global _warning_count
    _warning_count += 1
    print(f"  WARN  {path}:{lineno}: {msg}")


def _get_python_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for p in root.rglob("*.py"):
        # 只检查**相对 root** 的路径分量。直接对 p.parts 判断会连带 root 自身的
        # 绝对路径，一旦仓库位于隐藏目录下（如 ~/.cache/...），父级分量会被
        # 下面的 `startswith(".")` 命中而整体跳过——表现为静默扫描 0 个文件、
        # 永远"通过"，本地预检形同虚设。
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in parts):
            if ".github" not in parts:
                continue
        skip_dirs = {"venv", "node_modules", "__pycache__", ".git"}
        if any(part in skip_dirs for part in parts):
            continue
        if ".github" in parts and "scripts" in parts:
            continue
        files.append(p)
    return sorted(files)


def _check_syntax(path: Path) -> bool:
    ok = True
    try:
        compile(str(path), doraise=True)
    except PyCompileError as e:
        lineno = getattr(e, "lineno", 0) or 0
        _error(path, lineno, f"py_compile: {e}")
        ok = False

    try:
        with open(path, "r", encoding="utf-8") as f:
            ast.parse(f.read(), filename=str(path))
    except SyntaxError as e:
        if ok:
            _error(path, e.lineno or 0, f"ast.parse: {e.msg}")
            ok = False
    except UnicodeDecodeError as e:
        _error(path, 0, f"编码错误: {e}")
        ok = False

    return ok


def _count_triple_quotes(s: str, quote: str) -> int:
    count = 0
    i = 0
    while True:
        idx = s.find(quote, i)
        if idx == -1:
            break
        count += 1
        i = idx + len(quote)
    return count


def _check_indentation(path: Path) -> None:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    in_multiline = False
    ml_quote = ""

    for lineno, raw_line in enumerate(lines, 1):
        line = raw_line.rstrip("\n\r")
        stripped = line.lstrip()

        if "\t" in line:
            _error(path, lineno, "发现 Tab 字符，请统一使用 4 空格缩进")

        if not stripped and line != line.rstrip():
            _warn(path, lineno, "空行包含尾部空格")

        if not in_multiline:
            triple_double = _count_triple_quotes(stripped, '"""')
            triple_single = _count_triple_quotes(stripped, "'''")
            if triple_double % 2 == 1:
                in_multiline = True
                ml_quote = '"""'
            elif triple_single % 2 == 1:
                in_multiline = True
                ml_quote = "'''"
        else:
            if ml_quote in stripped:
                in_multiline = False
                ml_quote = ""
            continue

        if not stripped or stripped.startswith("#"):
            continue

        leading_spaces = len(line) - len(stripped)
        if leading_spaces > 0 and leading_spaces % 4 != 0:
            _warn(path, lineno, f"缩进 {leading_spaces} 空格，不是 4 的倍数")


# ---------------------------------------------------------------- 静态检查项
# ruff F 级（未定义名/F401/F841 等）在 2026-10-28 之前完全没跑过，靠 CI 兜底缺失，
# 结果 71 条 F 级问题长期潜伏（其中一处 F401 删掉的是再导出，直接 ImportError）。
# CI 里装了固定版本 ruff 之后这里就是硬拦截。
RUFF_VERSION = "0.17.3"

# 静默失败面预算（棘轮）：只允许下降，不允许反弹。
# 统计口径（AST）：
#   - except 块体只有一条 pass → 「完全静默」
#   - except 块体只有 logger.debug(...) 且没带 exc_info → 「只留一句话、没有栈」
# 新增静默点前先问「失败该不该被看见」；确实要新增，必须同时改这里的数字并说明原因。
SILENT_PASS_BUDGET = 87
SILENT_DEBUG_BUDGET = 77
PLUGIN_SOURCES = Path("plugins.v2")


# 检查范围：本仓库自己维护的 Python。plugins.v3/p115strmhelper 是另一个插件的历史目录，
# 有大量自带的 stub 噪声，不在本脚本射程内。
RUFF_TARGETS = ("plugins.v2", "tests", "scripts", ".github/scripts")


def _check_ruff(ruff_root: Path) -> None:
    """跑 ruff F 级（+E9 语法级），把每条命中记成 ERROR。"""
    targets = [str(ruff_root / name) for name in RUFF_TARGETS if (ruff_root / name).exists()]
    if not targets:
        return
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "ruff", "check", *targets,
                "--select", "F,E9", "--output-format", "concise", "--quiet",
            ],
            capture_output=True, text=True,
        )
    except OSError as e:
        _warn(ruff_root, 0, f"ruff 无法执行（{e}），跳过 F 级检查")
        return
    if proc.returncode == 0:
        return
    if "No module named" in (proc.stderr or ""):
        _warn(ruff_root, 0, f"ruff 未安装（CI 会装 ruff=={RUFF_VERSION}），本地跳过 F 级检查")
        return
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        _error(Path("<ruff>"), 0, line.strip())


def _count_silent_points(plugin_root: Path) -> Tuple[int, int]:
    """按 AST 口径统计静默失败点数量，返回 (except:pass, debug-only 无 exc_info)。"""
    silent_pass = 0
    debug_only = 0
    for py in sorted(plugin_root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                silent_pass += 1
                continue
            if body and all(
                isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Call)
                and isinstance(item.value.func, ast.Attribute)
                and item.value.func.attr == "debug"
                and not any(kw.arg == "exc_info" for kw in item.value.keywords)
                for item in body
            ):
                debug_only += 1
    return silent_pass, debug_only


def _check_silent_budget(plugin_root: Path) -> None:
    if not plugin_root.exists():
        return
    silent_pass, debug_only = _count_silent_points(plugin_root)
    print(
        f"  静默失败面：except:pass={silent_pass}（预算 {SILENT_PASS_BUDGET}），"
        f"debug-only 无栈={debug_only}（预算 {SILENT_DEBUG_BUDGET}）"
    )
    if silent_pass > SILENT_PASS_BUDGET:
        _error(
            plugin_root, 0,
            f"except: pass 数量 {silent_pass} 超过预算 {SILENT_PASS_BUDGET}："
            "新增静默点必须改成 logger.debug(..., exc_info=True)，或同步提高预算并说明原因",
        )
    if debug_only > SILENT_DEBUG_BUDGET:
        _error(
            plugin_root, 0,
            f"只记 debug 不带栈的 except 数量 {debug_only} 超过预算 {SILENT_DEBUG_BUDGET}："
            "给 logger.debug 补 exc_info=True",
        )


def lint_file(path: Path) -> bool:
    print(f"Checking {path}")
    ok = _check_syntax(path)
    _check_indentation(path)
    return ok


def lint_files(paths: Iterable[Path]) -> Tuple[int, int]:
    global _error_count, _warning_count
    _error_count = 0
    _warning_count = 0
    checked = 0

    for p in paths:
        if not p.exists():
            _error(p, 0, "文件不存在")
            continue
        lint_file(p)
        checked += 1

    print()
    print(
        f"Checked {checked} file(s), {_error_count} error(s), {_warning_count} warning(s)"
    )
    return _error_count, _warning_count


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/lint.py <file.py> [file2.py] ...")
        print("       python scripts/lint.py --all")
        return 1

    if sys.argv[1] == "--all":
        repo_root = Path(__file__).resolve().parent.parent
        files = _get_python_files(repo_root)
        errors, warnings = lint_files(files)
        _check_ruff(repo_root)
        _check_silent_budget(repo_root / PLUGIN_SOURCES)
        print(
            f"合计 {_error_count} error(s), {_warning_count} warning(s)（含 ruff 与静默面预算）"
        )
        return 1 if _error_count > 0 else 0
    else:
        files = [Path(p) for p in sys.argv[1:]]
        errors, warnings = lint_files(files)

    return 1 if errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
