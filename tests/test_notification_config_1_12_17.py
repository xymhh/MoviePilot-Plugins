"""1.12.17：通知配置合并——轻量版遮蔽完整版的回归。

历史事故：``SyncHandler.update_notification_config`` 按 7 个参数调用，
但 ``SyncNotificationService`` 有一个同名轻量版（只改开关与渠道类型、
吞掉其余 5 个参数），因组件顺序排在 ``MediaRefreshMixin`` 之前而把它
遮蔽，启动后改通知 / 媒体刷新配置不生效。1.12.17 删除轻量版，只留
完整版（重建 ``MediaServerNotifier``、更新延迟）。

本文件钉死两点：
1. 轻量版已删除：全仓只剩 ``media_refresh.py`` 一个
   ``update_notification_config`` 定义；
2. 完整版行为：7 个参数全部生效（开关、渠道、延迟更新，旧通知器
   关闭、新通知器按参数重建；旧通知器缺失时也不抛错）。
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugin_env import (
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_mock,
)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PLUGIN_DIR = os.path.join(PROJECT_ROOT, "plugins.v2", "cloudsubscribex")

install_app_mocks(
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification",
)
install_core_delegation()


class _FakeNotifier:
    """记录构造参数与 close 调用的 MediaServerNotifier 替身。"""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.closed = []
        type(self).instances.append(self)

    def close(self, flush=False):
        self.closed.append(bool(flush))


class _FakeBypassGuard:
    """直通 bypass_guard：测试不走旁路熔断。"""

    @staticmethod
    def call(name, func, fallback=None, **kwargs):
        return func()


register_mock(
    "cloudsubscribex.handlers.notification",
    MediaServerNotifier=_FakeNotifier,
    MediaServerResolver=object,
)
register_mock(
    "cloudsubscribex.core.bypass_guard",
    bypass_guard=_FakeBypassGuard(),
)

_media_refresh_module = load_module(
    "cloudsubscribex.handlers.sync.media_refresh",
    "plugins.v2/cloudsubscribex/handlers/sync/media_refresh.py",
)
MediaRefreshMixin = _media_refresh_module.MediaRefreshMixin


class _FakeOwner(SimpleNamespace):
    """SyncHandler 替身：只提供完整版读写的那几个属性。"""


class NotificationConfigTests(unittest.TestCase):
    def setUp(self):
        _FakeNotifier.instances.clear()

    def _owner(self, old_notifier=_FakeNotifier(enabled=False)):
        return _FakeOwner(
            _notify=False,
            _notification_type="Plugin",
            _notification_delay_seconds=0,
            _media_server_notifier=old_notifier,
        )

    def _call_complete(self, owner):
        MediaRefreshMixin(owner).update_notification_config(
            notify=True,
            notification_type="Telegram",
            media_server_refresh_enabled=True,
            media_servers=["emby"],
            media_server_path_mappings="{}",
            media_server_refresh_delay=30,
            emby_mediainfo_enabled=True,
        )

    def test_lightweight_version_is_gone(self):
        """轻量版已删除：全仓只剩 media_refresh.py 一个同名定义。"""
        sync_dir = Path(PLUGIN_DIR) / "handlers" / "sync"
        definitions = []
        for path in sorted(sync_dir.glob("*.py")):
            for match in re.finditer(
                r"^    def update_notification_config\(", path.read_text(encoding="utf-8"), re.M
            ):
                definitions.append(path.name)
        self.assertEqual(definitions, ["media_refresh.py"])
        notify_source = (sync_dir / "notify.py").read_text(encoding="utf-8")
        self.assertNotIn("def update_notification_config", notify_source)

    def test_complete_version_rebuilds_notifier(self):
        old = _FakeNotifier(enabled=False)
        owner = self._owner(old_notifier=old)
        self._call_complete(owner)
        self.assertTrue(owner._notify)
        self.assertEqual(owner._notification_type, "Telegram")
        self.assertEqual(owner._notification_delay_seconds, 30)
        self.assertEqual(old.closed, [True])
        self.assertIsInstance(owner._media_server_notifier, _FakeNotifier)
        self.assertIsNot(owner._media_server_notifier, old)
        self.assertEqual(
            owner._media_server_notifier.kwargs,
            {
                "enabled": True,
                "mediaservers": ["emby"],
                "path_mappings": "{}",
                "delay_seconds": 30,
                "emby_mediainfo_enabled": True,
            },
        )

    def test_complete_version_tolerates_missing_old_notifier(self):
        owner = self._owner(old_notifier=None)
        self._call_complete(owner)
        self.assertIsInstance(owner._media_server_notifier, _FakeNotifier)
        self.assertEqual(owner._notification_delay_seconds, 30)


if __name__ == "__main__":
    unittest.main()
