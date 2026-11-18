"""Phase 5 第 3 项（v1.20.0）：SyncHandler.__init__ 按配置分组拆 _init_* 的回归单测。

拆分只做机械搬移：__init__ 保留签名、docstring、具名服务字段
（v1.19.0）与 _get_val 闭包，其余 9 组按原顺序调 _init_* 方法。
本文件断言拆分的结构（方法存在、__init__ 按顺序调一遍）与行为
（构造后各组属性齐全、单组方法可独立调用）。
"""
import inspect
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    install_real_ttl_cache,
    load_module,
)

# 与 test_explicit_composition_1_19_0.py 同款 install 清单（已验证可全量通过）：
# service.py 在模块级加载，需要 app.utils.http 等 mock 与 20 个 sibling mock。
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
install_real_ttl_cache()
ensure_package("cloudsubscribex.core.sync_pause").is_paused = lambda *a, **k: False

SERVICE_PATH = "plugins.v2/cloudsubscribex/handlers/sync/service.py"
service = load_module("cloudsubscribex.handlers.sync.service", SERVICE_PATH)
SyncHandler = service.SyncHandler
# test_notification_config_1_12_17 把 notification mock 的
# MediaServerResolver 置为 object；全量顺序下 ensure_package 会把它拷进
# 本文件的 mock 包，service.py 重载后就绑了 object，导致
# _init_strm_media_server 里的 MediaServerResolver.configure 挂掉。
# 这里把 service 命名空间里的两个名字恢复成可用桩（只影响本文件
# load 出来的模块实例）。
service.MediaServerResolver = MagicMock()
service.MediaServerNotifier = MagicMock()

INIT_METHODS = [
    "_init_host_handles",
    "_init_organize_flags",
    "_init_cloud_services",
    "_init_batch_limits",
    "_init_plugin_callbacks",
    "_init_upgrade_rules",
    "_init_cloud_paths",
    "_init_strm_media_server",
    "_init_locks_caches",
]


class InitSplitStructureTests(unittest.TestCase):
    def test_nine_init_methods_exist(self):
        for name in INIT_METHODS:
            self.assertTrue(
                callable(getattr(SyncHandler, name, None)),
                f"缺 _init_* 方法：{name}",
            )

    def test_init_calls_each_group_once_in_order(self):
        source = inspect.getsource(SyncHandler.__init__)
        positions = [source.index(f"self.{name}(") for name in INIT_METHODS]
        self.assertEqual(positions, sorted(positions), "9 组调用顺序必须与原 __init__ 一致")
        for name in INIT_METHODS:
            self.assertEqual(source.count(f"self.{name}("), 1, f"{name} 在 __init__ 里只能调一次")

    def test_init_body_is_short(self):
        # __init__ 只剩头部 + _get_val + 9 个调用，不再是 400 行巨块
        source = inspect.getsource(SyncHandler.__init__)
        self.assertLess(len(source.splitlines()), 80, f"__init__ 仍有 {len(source.splitlines())} 行")


class InitSplitBehaviorTests(unittest.TestCase):
    def _bare_handler(self):
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = None
        return handler

    def test_single_group_callable_standalone(self):
        # 锁与缓存组不依赖其他组：裸 handler 上可独立调用
        handler = self._bare_handler()
        handler._init_locks_caches()
        self.assertIsNotNone(handler._offline_pending_lock)
        self.assertIsNotNone(handler._season_listing_cache)
        self.assertIsNotNone(handler._media_recognition_cache)

    def test_batch_group_standalone_with_get_val(self):
        handler = self._bare_handler()
        handler._cloud_drive = None
        handler._init_batch_limits(lambda name, default=None: default)
        self.assertEqual(handler._transfer_task_batch_size, 50)
        self.assertEqual(handler._batch_size, 20)
        self.assertEqual(handler._transfer_risk_cooldown, 1800)

    def test_full_init_state_complete(self):
        handler = SyncHandler(plugin=None)
        # 每组至少一个标志性属性
        self.assertIsNone(handler._cloud_drive)
        self.assertIsNone(handler._search_handler)
        self.assertTrue(handler._organize_after_transfer)
        self.assertFalse(handler._cross_transfer_enabled)
        self.assertIsNone(handler._cloud_auth)
        self.assertEqual(handler._transfer_task_batch_size, 50)
        self.assertFalse(handler._notify)
        self.assertEqual(handler._upgrade_mode, "largest")
        self.assertEqual(handler._cloud_transfer_path, "/")
        self.assertEqual(handler._CLOUD_MEDIA_ROOT, "/")
        self.assertTrue(handler._strm_generate_enabled is False)  # 无播放引用时关掉
        self.assertIsNotNone(handler._media_server_notifier)
        self.assertIsNotNone(handler._offline_pending_lock)
        self.assertIsNotNone(handler._season_listing_cache)
        # 具名服务字段仍在 __init__ 最前面建好
        self.assertIsNotNone(handler.movie)
        self.assertIsNotNone(handler.transfer_ops)


if __name__ == "__main__":
    unittest.main()
