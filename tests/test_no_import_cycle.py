"""回归：禁止 utils 包（含子模块）在模块级反向 import core。

2026-11-12 真机事故：MP 热重载报
`deadlock detected by _ModuleLock('app.plugins.cloudsubscribex.core.api.sync')`，
插件更新失败两次（13:51 / 16:38）。成因是两条环路：

1. core/__init__.py:3  `from ..utils.notify_template import ...`  → 触发 utils 包
   utils/__init__.py:22 `from ..core.media import media_identity`  → 回到 core 包
2. core/__init__.py    → utils/__init__ → utils/strm_export.py:18
   `from ..core.cloud import CloudDriveCapability` → 回到 core 包

同一线程递归拿同一把模块锁，CPython 直接报 deadlock。
修法：utils 侧一律改为函数内延迟 import（此时 core 包已初始化完）。
"""
import ast
import os
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
PKG = os.path.join(project_root, "plugins.v2", "cloudsubscribex")
UTILS = os.path.join(PKG, "utils")


def module_level_core_imports(path):
    """返回模块顶层（不含函数体）里指向 core 的相对 import 行号。"""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    offenders = []
    for node in tree.body:  # 只看模块级，函数内延迟 import 合法
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        # level>=2 且模块名以 core 开头 => 反向依赖
        if node.level >= 2 and module.split(".")[0] == "core":
            offenders.append((node.lineno, node.level, module))
    return offenders


class TestNoImportCycle(unittest.TestCase):
    def test_utils_init_has_no_core_import(self):
        offenders = module_level_core_imports(os.path.join(UTILS, "__init__.py"))
        self.assertEqual(offenders, [], f"utils/__init__.py 反向 import core: {offenders}")

    def test_no_utils_submodule_imports_core_at_module_level(self):
        """覆盖全部 utils/*.py —— 之前只查 __init__.py，strm_export.py 漏网。"""
        offenders = {}
        for name in sorted(os.listdir(UTILS)):
            if not name.endswith(".py"):
                continue
            found = module_level_core_imports(os.path.join(UTILS, name))
            if found:
                offenders[name] = found
        self.assertEqual(
            offenders,
            {},
            f"utils 子模块在模块级反向 import core（会与 core -> utils 成环）: {offenders}",
        )

    def test_core_init_does_not_import_api_package(self):
        with open(os.path.join(PKG, "core", "__init__.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        mods = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertNotIn("api", mods, "core/__init__.py 直接 import 了 api 子包")


if __name__ == "__main__":
    unittest.main()
