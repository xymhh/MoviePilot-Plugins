"""SyncHandler 转存入口直路测试（v1.13.0 起账本已删除）。"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package, install_app_mocks, install_core_delegation, load_module,
    stub_component_method,
)

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription", "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache", "cloudsubscribex.search.types",
    *[
        f"cloudsubscribex.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "cleanup", "episode_dedup", "history", "landed",
            "matching", "metadata", "movie", "naming", "notify",
            "platform_history", "postprocess", "pt_upgrade", "resources",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)




install_core_delegation()
pause_module = ensure_package("cloudsubscribex.core.sync_pause")
pause_module.is_paused = MagicMock(return_value=False)
ensure_package("cloudsubscribex.core").sync_pause = pause_module
mock_utils = ensure_package("cloudsubscribex.utils")
MediaFileParser = load_module(
    "cloudsubscribex.utils.file_parser",
    "plugins.v2/cloudsubscribex/utils/file_parser.py",
).MediaFileParser
mock_utils.MediaFileParser = MediaFileParser
mock_utils.parse_magnet_metadata = MagicMock()

load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
load_module(
    "cloudsubscribex.utils.magnet",
    "plugins.v2/cloudsubscribex/utils/magnet.py",
)
load_module(
    "cloudsubscribex.search.magnet",
    "plugins.v2/cloudsubscribex/search/magnet.py",
)
_service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)
SyncHandler = _service_module.SyncHandler
TransferOpsMixin = _service_module.TransferOpsMixin


class SyncTransferWiringTests(unittest.TestCase):
    """v1.13.0 起同步作业账本已删除：转存入口直接调实现，不再认领作业。"""

    def _handler(self, *, impl_result=True, impl_error=None):
        handler = SyncHandler.__new__(SyncHandler)
        handler._cloud_drive = SimpleNamespace(key="115")
        handler._stop_requested = lambda: False
        handler._resource_provider_for_url = lambda url: SimpleNamespace(key="115")
        stub_component_method(
            handler, TransferOpsMixin, "_transfer_file_impl",
            MagicMock(return_value=impl_result, side_effect=impl_error),
        )
        handler._sync_pause_cache = None
        return handler

    @staticmethod
    def _args():
        return (
            "https://115.com/s/example",
            {
                "id": "f1",
                "name": "第17集.mp4",
                "size": 1024,
                "sha1": "a" * 40,
                "subscribe_id": "42",
            },
            "/待整理",
            "第17集.mp4",
        )

    def test_transfer_file_calls_impl_directly(self):
        handler = self._handler()
        self.assertTrue(handler._transfer_file(*self._args()))
        handler._transfer_file_impl.assert_called_once()

    def test_sync_paused_reads_global_switch(self):
        """暂停开关从 core.sync_pause 读取，且只认严格的 True。"""
        core_pkg = sys.modules["cloudsubscribex.core"]
        original = getattr(core_pkg, "sync_pause", None)
        core_pkg.sync_pause = pause_module
        try:
            handler = SyncHandler.__new__(SyncHandler)
            # __init__ 才会挂的宿主插件；缺失会让暂停开关走 except 分支恒为 False
            handler._plugin = SimpleNamespace(_sync_pause_repository=None)
            handler._sync_pause_cache = None
            pause_module.is_paused.return_value = True
            self.assertTrue(handler._sync_paused())
            handler._sync_pause_cache = None
            pause_module.is_paused.return_value = MagicMock()
            self.assertFalse(
                handler._sync_paused(),
                "非 True（例如 mock 对象）一律按未暂停处理",
            )
        finally:
            pause_module.is_paused.return_value = False
            if original is not None:
                core_pkg.sync_pause = original
            else:
                delattr(core_pkg, "sync_pause")


if __name__ == "__main__":
    unittest.main()
