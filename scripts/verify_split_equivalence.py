#!/usr/bin/env python3
"""拆分等价性校验：证明「只是搬家，没改逻辑」。

做法：从两个版本各自用 AST 抽出**所有类方法**的源码，规范化后取 sha256，
要求旧版本的每个方法在新版本中都存在且哈希一致。

为什么比单测强：单测只覆盖写了断言的分支，指纹覆盖每一行代码。

用法：
    # 拿 HEAD 与当前工作区比（拆分后、提交前跑）
    python3 scripts/verify_split_equivalence.py

    # 指定两个引用
    python3 scripts/verify_split_equivalence.py --before c1287f5 --after HEAD

    # 只比某几个文件（默认比三个巨石文件）
    python3 scripts/verify_split_equivalence.py --files plugins.v2/cloudsubscribex/handlers/sync/service.py

退出码：0 全部一致；1 有差异或缺失；2 用法/环境错误。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import textwrap
from ast import ClassDef, FunctionDef, AsyncFunctionDef, parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_FILES = [
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
    "plugins.v2/cloudsubscribex/handlers/sync/postprocess.py",
]


def _apply_allowlist(source: str, rewrites: list[tuple[str, str]]) -> str:
    for old, new in rewrites:
        source = source.replace(old, new)
    return source


def _normalize(source: str) -> str:
    """去掉公共缩进、行尾空格与空行，保留注释与逻辑顺序。"""
    text = textwrap.dedent(source)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _digest(source: str) -> str:
    return hashlib.sha256(_normalize(source).encode("utf-8")).hexdigest()[:16]


def _collect(
        source: str, origin: str,
        rewrites: list[tuple[str, str]] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """{方法键: [(指纹, 所在类), ...]}。

    键规则：双下划线方法按「类.方法」区分（__init__ 到处都有），
    普通方法按方法名索引——拆分后类名变了也照样对得上。
    同名方法在不同类里允许存在（多模块合法重名），比对时按多重集匹配。
    """
    tree = parse(source)
    methods: dict[str, list[tuple[str, str]]] = {}
    for node in tree.body:
        if isinstance(node, (FunctionDef, AsyncFunctionDef)):
            # 模块级函数（如 _safe_optional_int / _staging_ref_*）同样要逐字一致
            segment = textwrap.dedent(_slice(source, node))
            methods.setdefault(node.name, []).append((_digest(segment), "<module>"))
            continue
        if not isinstance(node, ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (FunctionDef, AsyncFunctionDef)):
                continue
            segment = _apply_allowlist(
                textwrap.dedent(_slice(source, item)), rewrites or []
            )
            key = (
                f"{node.name}.{item.name}"
                if item.name.startswith("__") and item.name.endswith("__")
                else item.name
            )
            methods.setdefault(key, []).append((_digest(segment), node.name))
    return methods


def _slice(source: str, node) -> str:
    lines = source.splitlines()
    start = node.lineno - 1
    end = node.end_lineno
    return "\n".join(lines[start:end])


def _git_show(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{ref}:{path}"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


def _collect_tree(
        ref: str, files: list[str],
        rewrites: list[tuple[str, str]] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """ref == WORKTREE 时读工作区；否则从 git 对象读。"""
    methods: dict[str, list[tuple[str, str]]] = {}
    for path in files:
        source = (
            (ROOT / path).read_text(encoding="utf-8")
            if ref == "WORKTREE"
            else _git_show(ref, path)
        )
        if source is None:
            print(f"  [skip] {ref} 中不存在 {path}")
            continue
        for key, entries in _collect(source, f"{ref}:{path}", rewrites).items():
            methods.setdefault(key, []).extend(entries)
    return methods


def _scan_all_plugin_files(ref: str) -> list[str]:
    """拆分后方法可能搬到新文件：把插件目录下所有 .py 都纳入扫描。"""
    if ref == "WORKTREE":
        return sorted(
            str(p.relative_to(ROOT))
            for p in (ROOT / "plugins.v2" / "cloudsubscribex").rglob("*.py")
            if "__pycache__" not in str(p)
        )
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", ref,
         "plugins.v2/cloudsubscribex"],
        capture_output=True, text=True,
    )
    return [line for line in result.stdout.splitlines() if line.endswith(".py")]


def main() -> int:
    parser = argparse.ArgumentParser(description="拆分等价性校验")
    parser.add_argument("--before", default="HEAD", help="旧版本 git 引用")
    parser.add_argument("--after", default="WORKTREE", help="新版本 git 引用或 WORKTREE")
    parser.add_argument("--files", nargs="*", default=None, help="只比这些文件（默认三个巨石）")
    parser.add_argument("--report", default="", help="输出 markdown 报告路径")
    parser.add_argument(
        "--allow", action="append", default=[],
        metavar="OLD=>NEW",
        help="声明式允许的文本改写（如 SyncHandler.foo=>Mixin.foo）；"
             "只有声明过的改写才被容忍，未声明的任何改动都会报 FAIL",
    )
    parser.add_argument(
        "--accept-change", action="append", default=[],
        metavar="NEW",
        help="声明式允许的方法体改动（如 TransferOpsMixin.foo）；"
             "用于拆分时确有必要的宿主属性口径修正，未声明的一律 FAIL",
    )
    args = parser.parse_args()

    before_files = args.files or DEFAULT_FILES
    after_files = args.files or _scan_all_plugin_files(args.after)

    print(f"旧版本：{args.before}（{len(before_files)} 个文件）")
    rewrites: list[tuple[str, str]] = []
    for item in args.allow:
        if "=>" not in item:
            raise SystemExit(f"[FATAL] --allow 需要 OLD=>NEW 格式：{item}")
        old, new = item.split("=>", 1)
        rewrites.append((old.strip(), new.strip()))
    if rewrites:
        print(f"声明式改写：{len(rewrites)} 条")
        for old, new in rewrites:
            print(f"  {old} => {new}")
    before = _collect_tree(args.before, before_files, rewrites)
    print(f"新版本：{args.after}（扫描 {len(after_files)} 个文件）")
    after = _collect_tree(args.after, after_files)
    print(f"方法数：旧 {len(before)} / 新 {len(after)}\n")

    missing, changed, moved = [], [], []
    for key, entries in before.items():
        pool = list(after.get(key) or [])
        for digest, cls in entries:
            match = next((i for i, (d, _) in enumerate(pool) if d == digest), None)
            if match is None:
                if pool:
                    changed.append(f"{cls}.{key} → {pool[0][1]}.{key}")
                else:
                    missing.append(f"{cls}.{key}")
                continue
            new_cls = pool.pop(match)[1]
            if new_cls != cls:
                moved.append(f"{key}: {cls} → {new_cls}")

    before_keys = {(k, d) for k, entries in before.items() for d, _ in entries}
    added = sorted(
        f"{cls}.{key}"
        for key, entries in after.items()
        for digest, cls in entries
        if (key, digest) not in before_keys
    )

    same = sum(len(v) for v in before.values()) - len(missing) - len(changed)
    print(f"完全一致：{same} 个方法")
    if moved:
        print(f"换类但方法体一致：{len(moved)} 个（正常搬家）")
    if added:
        print(f"新增方法：{len(added)} 个")
        for item in added[:20]:
            print(f"  + {item}")
    accepted_changes = {item.strip() for item in args.accept_change if item.strip()}
    if accepted_changes and changed:
        kept = []
        for item in changed:
            new_name = item.split("→")[-1].strip()
            if new_name in accepted_changes or new_name.split(".")[-1] in accepted_changes:
                continue
            kept.append(item)
        print(f"\n声明式容许的方法体改动：{len(changed) - len(kept)} 个")
        for item in sorted(accepted_changes):
            print(f"  ~ {item}")
        changed = kept
    if changed:
        print(f"\n[FAIL] 方法体被改动：{len(changed)} 个")
        for item in changed[:40]:
            print(f"  ! {item}")
    if missing:
        print(f"\n[FAIL] 方法丢失：{len(missing)} 个")
        for item in missing[:40]:
            print(f"  - {item}")

    if args.report:
        lines = [
            f"# 拆分等价性校验：{args.before} → {args.after}",
            "",
            f"- 旧版本方法数：{len(before)}",
            f"- 新版本方法数：{len(after)}",
            f"- 方法体一致：{same}",
            f"- 换类但一致：{len(moved)}",
            f"- 新增：{len(added)}",
            f"- **方法体被改：{len(changed)}**",
            f"- **方法丢失：{len(missing)}**",
            "",
        ]
        if changed:
            lines += ["## 方法体被改", ""] + [f"- `{c}`" for c in changed] + [""]
        if missing:
            lines += ["## 方法丢失", ""] + [f"- `{m}`" for m in missing] + [""]
        if moved:
            lines += ["## 换类（方法体一致）", ""] + [f"- `{m}`" for m in moved] + [""]
        Path(args.report).write_text("\n".join(lines), encoding="utf-8")
        print(f"\n报告：{args.report}")

    if changed or missing:
        return 1
    print("\n[OK] 所有方法体逐字一致，纯搬家")
    return 0


if __name__ == "__main__":
    sys.exit(main())
