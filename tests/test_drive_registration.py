"""驱动定义与 dataclass 字段一致性冒烟测试。

1.7.35 删 Gopeed 时漏掉 `drive/quark/definition.py` 里多行写法实参
`cross_transfer_downloader=...`，导致线上
`QuarkDrive.__init__() got an unexpected keyword argument`，
夸克驱动整体注册失败、网盘显示未登录。

这里锁两件事：dataclass 里没有已删字段；definition 的构造调用里不出现它们。
"""

import os
import unittest

from plugin_env import install_app_mocks

install_app_mocks()

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "plugins.v2", "cloudsubscribex")

_REMOVED = ("cross_transfer_downloader", "gopeed_url", "gopeed_token",
            "gopeed_path_prefix", "gopeed_connections")


class DriveRegistrationTest(unittest.TestCase):

    def test_quark_drive_has_no_removed_fields(self):
        # 不 import provider（会拉起 app.* 依赖），直接查 dataclass 字段声明
        path = os.path.join(_PKG, "drive", "quark", "provider.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for field in _REMOVED:
            self.assertNotRegex(
                source, rf"(?m)^\s+{field}\s*:", f"QuarkDrive 仍残留字段 {field}"
            )

    def test_definition_passes_no_removed_fields(self):
        path = os.path.join(_PKG, "drive", "quark", "definition.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for field in _REMOVED:
            self.assertNotIn(f"{field}=", source, f"definition.py 仍传 {field}=")


if __name__ == "__main__":
    unittest.main()
