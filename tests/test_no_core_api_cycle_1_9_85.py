"""1.9.85 消除 core.api <-> core 导入环（插件热重载死锁的第二个来源）。

事故：2026-11-14 09:37 MP 热重载报
`deadlock detected by _ModuleLock('app.plugins.cloudsubscribex.core.api.sync')`，
traceback 指向 `core/api/__init__.py:18 -> from .sync import SyncApi`，
而 `core/api/sync.py` 的 `from .. import CloudDriveCapability, OwnerDelegator, ...`
会触发 `core/__init__.py`（正在初始化）→ 同线程递归拿同一把锁。

修法：`core/api/*.py` 的 `from .. import X` 全部改为直连子模块
（`from ..cloud import CloudDriveCapability` 等），导入 `core.cloud` 只执行该子模块，
不会执行 `core/__init__.py`，环断开。
"""
import ast
import os
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
PKG = os.path.join(project_root, "plugins.v2", "cloudsubscribex")
API_DIR = os.path.join(PKG, "core", "api")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def package_level_core_imports(path):
    """返回模块顶层里 `from .. import ...`（会触发 core/__init__）的行号。"""
    tree = ast.parse(read(path))
    offenders = []
    for node in tree.body:  # 只看模块级
        if not isinstance(node, ast.ImportFrom):
            continue
        # level==2 且 module 为 None => `from .. import X`，会执行 core/__init__.py
        if node.level == 2 and node.module is None:
            offenders.append((node.lineno, [a.name for a in node.names]))
    return offenders


class TestNoCoreApiCycle(unittest.TestCase):
    def test_no_core_api_file_imports_core_package(self):
        """覆盖全部 core/api/*.py —— 只查 sync.py 会漏掉其余 16 个文件。"""
        offenders = {}
        for name in sorted(os.listdir(API_DIR)):
            if not name.endswith(".py"):
                continue
            found = package_level_core_imports(os.path.join(API_DIR, name))
            if found:
                offenders[name] = found
        self.assertEqual(
            offenders,
            {},
            f"core/api 仍有 `from .. import`（会触发 core/__init__，与 core.api 成环）: {offenders}",
        )

    def test_core_api_imports_submodules_directly(self):
        """改后必须直连子模块，且符号来自正确位置。"""
        sync = read(os.path.join(API_DIR, "sync.py"))
        self.assertIn("from ..cloud import CloudDriveCapability", sync)
        self.assertIn("from ..delegation import OwnerDelegator", sync)
        self.assertIn("from ..search import SearchCapability", sync)

    def test_core_package_does_not_import_api(self):
        """反向确认：core/__init__.py 不得 import api 子包。"""
        tree = ast.parse(read(os.path.join(PKG, "core", "__init__.py")))
        mods = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertNotIn("api", mods, "core/__init__.py 直接 import 了 api 子包")

    def test_utils_does_not_import_core(self):
        """1.9.80 修的另一个环，防止回退。"""
        utils_dir = os.path.join(PKG, "utils")
        offenders = {}
        for name in sorted(os.listdir(utils_dir)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(read(os.path.join(utils_dir, name)))
            for node in tree.body:
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.level >= 2
                    and (node.module or "").split(".")[0] == "core"
                ):
                    offenders.setdefault(name, []).append(node.lineno)
        self.assertEqual(offenders, {}, f"utils 又反向 import core: {offenders}")


if __name__ == "__main__":
    unittest.main()
