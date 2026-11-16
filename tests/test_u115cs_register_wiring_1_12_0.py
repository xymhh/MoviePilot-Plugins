"""1.12.0：115网盘CS版存储的插件接线。

在裸插件实例上验证 ``_register_u115cs_storage``：
- 驱动管理器已就绪时绑定 115 提供方并注册存储；
- 配置保存路径（驱动管理器尚未建立）只注册、不绑定；
- 绑定失败时不注册，且任何一步失败都不抛回插件加载流程。
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import load_module  # noqa: E402

from test_downloader_plugin_1_9_86 import _plugin  # noqa: E402


def _storage_module():
    module = sys.modules.get("cloudsubscribex.drive.p115.storage")
    if module is not None and hasattr(module, "register_u115cs_storage"):
        return module
    return load_module(
        "cloudsubscribex.drive.p115.storage",
        "plugins.v2/cloudsubscribex/drive/p115/storage.py",
    )


class TestU115CSRegisterWiring(unittest.TestCase):
    def setUp(self):
        self.plugin = _plugin()
        self.storage = _storage_module()
        self._orig_bind = self.storage.bind_u115cs_provider
        self._orig_register = self.storage.register_u115cs_storage
        self.mock_bind = MagicMock(return_value=True)
        self.mock_register = MagicMock(return_value=True)
        self.storage.bind_u115cs_provider = self.mock_bind
        self.storage.register_u115cs_storage = self.mock_register
        self.addCleanup(self._restore_storage)

    def _restore_storage(self):
        self.storage.bind_u115cs_provider = self._orig_bind
        self.storage.register_u115cs_storage = self._orig_register

    def test_bind_and_register_when_registry_ready(self):
        """驱动管理器已就绪：绑定 115 提供方并注册存储。"""
        provider = SimpleNamespace(key="115")
        self.plugin._cloud_drive_registry = SimpleNamespace(
            get=MagicMock(return_value=provider)
        )
        self.plugin._register_u115cs_storage()
        self.mock_bind.assert_called_once_with(provider)
        self.mock_register.assert_called_once_with()

    def test_register_only_without_registry(self):
        """配置保存钩子（_init_clients 之前）：只注册、不绑定。"""
        self.assertNotIn("_cloud_drive_registry", self.plugin.__dict__)
        self.plugin._register_u115cs_storage()
        self.mock_bind.assert_not_called()
        self.mock_register.assert_called_once_with()

    def test_bind_failure_skips_register(self):
        """取不到 115 提供方：不绑定、不注册，且不抛异常。"""
        self.plugin._cloud_drive_registry = SimpleNamespace(
            get=MagicMock(side_effect=KeyError("115"))
        )
        self.plugin._register_u115cs_storage()
        self.mock_bind.assert_not_called()
        self.mock_register.assert_not_called()

    def test_register_failure_does_not_raise(self):
        """注册本身失败：不抛回插件加载流程。"""
        provider = SimpleNamespace(key="115")
        self.plugin._cloud_drive_registry = SimpleNamespace(
            get=MagicMock(return_value=provider)
        )
        self.mock_register.side_effect = RuntimeError("MP 存储注册表结构已变化")
        self.plugin._register_u115cs_storage()
        self.mock_bind.assert_called_once_with(provider)
        self.mock_register.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
