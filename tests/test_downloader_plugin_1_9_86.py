"""1.9.86：插件本体的下载器投影方法。

加载真实的 plugins.v2/cloudsubscribex/__init__.py（app.* 与第三方依赖
全部打桩），在裸实例上验证：下载器条目登记/移除、list_torrents 投影、
torrent_files、stop_torrents 复用停止逻辑、remove_torrents 只清终态记录。
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
)


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _File:
    """桩 DownloaderFile：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeSystemConfig:
    """桩 SystemConfigOper：只记 Downloaders 列表。"""

    def __init__(self, downloaders=None):
        self.downloaders = list(downloaders or [])
        self.set_calls = 0

    def get(self, key):
        return list(self.downloaders)

    def set(self, key, value):
        self.downloaders = list(value)
        self.set_calls += 1


TASKS = [
    {
        "id": "subscribe:123",
        "title": "谷雨",
        "media_type": "电视剧",
        "year": "2026",
        "season": 1,
        "target_episodes": [21, 22],
        "status": "transferring",
        "progress": 45,
        "transferred": 450,
        "total": 1000,
        "speed_bytes_per_second": 2097152,
        "current_file": "",
    },
    {
        "id": "cross:abcdef123456",
        "task_kind": "cross_transfer",
        "parent_task_id": "",
        "title": "谷雨 S01E21.mkv",
        "target_path": "/转存/电视剧/谷雨 (2026)/Season 1/谷雨 - S01E21.mkv",
        "status": "running",
        "progress": 60,
        "transferred": 600,
        "total": 1000,
        "speed_bytes_per_second": 1048576,
    },
]


def _load_plugin_module():
    """打桩后加载真实插件模块（只加载一次）。

    注意：AutoMockModule 对任意属性都返回 MagicMock，hasattr 恒为真，
    所以缓存判定必须查 __dict__ 里的真实类，不能用 hasattr。
    """
    module = sys.modules.get("cloudsubscribex")
    if isinstance(getattr(module, "__dict__", {}).get("CloudSubscribeX"), type):
        return module
    app_schemas = ensure_package("app.schemas")
    app_schemas.DownloaderTorrent = _Torrent
    app_schemas.DownloaderFile = _File
    app_schemas.Notification = _Torrent

    types_mod = ensure_package("app.schemas.types")

    class _SystemConfigKey:
        Downloaders = "Downloaders"

    types_mod.SystemConfigKey = _SystemConfigKey

    class _TorrentQueryStatus:
        ALL = "all"

    types_mod.TorrentQueryStatus = _TorrentQueryStatus

    class _EventManager:
        def register(self, *args, **kwargs):
            def deco(fn):
                return fn

            return deco

    app_core_event = ensure_package("app.core.event")
    app_core_event.eventmanager = _EventManager()
    app_core_event.Event = object

    class _PluginBase:
        def __init__(self, *args, **kwargs):
            pass

    ensure_package("app.plugins")._PluginBase = _PluginBase

    install_app_mocks(
        "app.core", "app.core.config", "app.api", "app.api.endpoints",
        "app.api.endpoints.plugin",
        "apscheduler", "apscheduler.schedulers",
        "apscheduler.schedulers.background",
        "apscheduler.triggers", "apscheduler.triggers.cron",
        "apscheduler.triggers.interval", "pytz",
        "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.utils",
        "cloudsubscribex.utils.notify_template",
        "cloudsubscribex.core.aria2", "cloudsubscribex.core.api",
        "cloudsubscribex.core.hook", "cloudsubscribex.core.legacy_migration",
        "cloudsubscribex.core.media_scope", "cloudsubscribex.core.services",
        "cloudsubscribex.core.storage", "cloudsubscribex.core.retry_policy",
        "cloudsubscribex.core.task_center", "cloudsubscribex.core.subscribe",
        "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
        "cloudsubscribex.handlers", "cloudsubscribex.search",
        "cloudsubscribex.search.scanner", "cloudsubscribex.subscribe",
        "cloudsubscribex.utils.file_parser", "cloudsubscribex.utils.http_client",
        "cloudsubscribex.core.services.upload_quota",
    )
    install_core_delegation()
    sys.modules["app.core.config"].settings = SimpleNamespace(
        API_TOKEN="test-token", TZ="Asia/Shanghai"
    )
    return load_module(
        "cloudsubscribex", "plugins.v2/cloudsubscribex/__init__.py"
    )


def _plugin():
    """裸插件实例：运行态与系统配置全部打桩。"""
    module = _load_plugin_module()
    plugin = module.CloudSubscribeX.__new__(module.CloudSubscribeX)
    plugin.systemconfig = _FakeSystemConfig([
        {"name": "Qbittorrent", "type": "Qbittorrent", "enabled": True},
    ])
    plugin._serialize_runtime_tasks = lambda: [dict(task) for task in TASKS]
    plugin._sync_tasks = {}
    plugin._sync_tasks_lock = threading.RLock()
    plugin._stop_calls = []
    plugin.api_stop_sync_task = lambda apikey, task_id: (
        plugin._stop_calls.append((apikey, task_id)),
        {"success": True, "message": "已请求停止"},
    )[1]
    return plugin


class TestDownloaderEntryWiring(unittest.TestCase):
    def test_ensure_registers_entry(self):
        plugin = _plugin()
        plugin._ensure_downloader_entry()
        self.assertEqual(plugin.systemconfig.set_calls, 1)
        entry = next(
            item for item in plugin.systemconfig.downloaders
            if item["name"] == "网盘订阅助手"
        )
        self.assertEqual(entry["type"], "CloudSubscribeX")
        self.assertTrue(entry["enabled"])
        self.assertFalse(entry["default"])

    def test_ensure_is_idempotent(self):
        plugin = _plugin()
        plugin._ensure_downloader_entry()
        plugin._ensure_downloader_entry()
        self.assertEqual(plugin.systemconfig.set_calls, 1)

    def test_remove_entry(self):
        plugin = _plugin()
        plugin._ensure_downloader_entry()
        plugin._remove_downloader_entry()
        names = [item["name"] for item in plugin.systemconfig.downloaders]
        self.assertEqual(names, ["Qbittorrent"])

    def test_remove_is_noop_when_absent(self):
        plugin = _plugin()
        plugin._remove_downloader_entry()
        self.assertEqual(plugin.systemconfig.set_calls, 0)

    def test_stop_service_removes_entry(self):
        """静态断言：stop_service 末尾必须调 _remove_downloader_entry。"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugins.v2", "cloudsubscribex", "__init__.py",
        )
        source = open(path, encoding="utf-8").read()
        stop_at = source.index("def stop_service")
        stop_body = source[stop_at:source.index("def reload_plugin_api", stop_at)]
        self.assertIn("self._remove_downloader_entry()", stop_body)
        init_at = source.index("def init_plugin")
        init_body = source[init_at:source.index("def _ensure_notify_template_defaults", init_at)]
        self.assertIn("self._ensure_downloader_entry()", init_body)


class TestPluginListTorrents(unittest.TestCase):
    def test_lists_runtime_tasks(self):
        plugin = _plugin()
        rows = plugin.list_torrents()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].downloader, "网盘订阅助手")
        self.assertEqual(rows[0].title, "谷雨")
        self.assertEqual(rows[0].state, "downloading")

    def test_other_downloader_returns_empty(self):
        plugin = _plugin()
        self.assertEqual(plugin.list_torrents(downloader="网盘直链"), [])

    def test_hash_filter(self):
        plugin = _plugin()
        from cloudsubscribex.core.downloader_view import task_hash

        rows = plugin.list_torrents(hashs=[task_hash("subscribe:123")])
        self.assertEqual([row.title for row in rows], ["谷雨"])

    def test_torrent_files(self):
        plugin = _plugin()
        from cloudsubscribex.core.downloader_view import task_hash

        files = plugin.torrent_files(tid=task_hash("cross:abcdef123456"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "谷雨 S01E21.mkv")
        self.assertEqual(
            plugin.torrent_files(tid=task_hash("subscribe:123")), []
        )
        self.assertEqual(plugin.torrent_files(tid="0" * 40), [])
        self.assertEqual(plugin.torrent_files(), [])

    def test_start_is_not_supported(self):
        plugin = _plugin()
        self.assertFalse(plugin.start_torrents(hashs=["a" * 40]))

    def test_stop_delegates_to_sync_stop(self):
        plugin = _plugin()
        from cloudsubscribex.core.downloader_view import task_hash

        self.assertTrue(
            plugin.stop_torrents(hashs=[task_hash("subscribe:123")])
        )
        self.assertEqual(
            plugin._stop_calls, [("test-token", "subscribe:123")]
        )
        self.assertFalse(plugin.stop_torrents(hashs=["0" * 40]))
        self.assertFalse(plugin.stop_torrents())

    def test_remove_only_clears_terminal_tasks(self):
        plugin = _plugin()
        from cloudsubscribex.core.downloader_view import task_hash

        plugin._sync_tasks = {
            "subscribe:123": {"status": "transferring"},
            "subscribe:999": {"status": "completed"},
            "subscribe:998": {"status": "failed"},
        }
        self.assertTrue(
            plugin.remove_torrents(hashs=[task_hash("subscribe:999")])
        )
        self.assertNotIn("subscribe:999", plugin._sync_tasks)
        self.assertIn("subscribe:123", plugin._sync_tasks)
        self.assertFalse(
            plugin.remove_torrents(hashs=[task_hash("subscribe:123")])
        )
        self.assertIn("subscribe:123", plugin._sync_tasks)
        self.assertFalse(plugin.remove_torrents(hashs=["0" * 40]))


if __name__ == "__main__":
    unittest.main()
