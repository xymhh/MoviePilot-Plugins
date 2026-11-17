"""1.12.17：cloud 策略下 Magnet 接管门控修复。

根因：Magnet 接管（走 115 离线下载）的门控是 `"magnet" in
_resource_type_order`（core/hook/events.py），但默认顺序只有
["115", "ed2k"]；drive_manager 的收窄还会把 magnet 滤掉（没有对应
的网盘驱动，registry.get 抛 KeyError）——cloud 策略下每个平台
Magnet 下载都会走到“接管失败，已阻止平台下载”。

修复分两处：
1. _apply_search_config 构建顺序时，policy == "cloud" 且顺序里没
   magnet 就自动补上（记 warning）；策略从 config 字典读（该函数
   运行时 _platform_download_policy 还没落到 self 上）。
2. drive_manager 收窄时，magnet 按策略保留（cloud 保留、block 滤掉），
   不再走“没有驱动 = 不支持”的分支。
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

register_module("app", package=True)
register_module("app.log", logger=MagicMock())
register_module("cloudsubscribex", package=True)
register_module("cloudsubscribex.search", package=True)

types_module = load_module(
    "cloudsubscribex.search.types",
    "plugins.v2/cloudsubscribex/search/types.py",
)
ensure_magnet_for_cloud_policy = types_module.ensure_magnet_for_cloud_policy


class EnsureMagnetTests(unittest.TestCase):
    def test_cloud_appends_magnet_to_default_order(self):
        order, added = ensure_magnet_for_cloud_policy(["115", "ed2k"], "cloud")
        self.assertEqual(order, ["115", "ed2k", "magnet"])
        self.assertTrue(added)

    def test_block_leaves_order_unchanged(self):
        order, added = ensure_magnet_for_cloud_policy(["115", "ed2k"], "block")
        self.assertEqual(order, ["115", "ed2k"])
        self.assertFalse(added)

    def test_cloud_with_magnet_already_present_is_noop(self):
        order, added = ensure_magnet_for_cloud_policy(
            ["115", "magnet", "ed2k"], "cloud"
        )
        self.assertEqual(order, ["115", "magnet", "ed2k"])
        self.assertFalse(added)

    def test_empty_order_cloud_gives_magnet_only(self):
        order, added = ensure_magnet_for_cloud_policy([], "cloud")
        self.assertEqual(order, ["magnet"])
        self.assertTrue(added)

    def test_missing_policy_is_block(self):
        for policy in (None, "", "BLOCK"):
            order, added = ensure_magnet_for_cloud_policy(["115"], policy)
            self.assertEqual(order, ["115"])
            self.assertFalse(added)


class _FakeSource:
    def supports(self, capability):
        return True


class _FakeRegistry:
    def get(self, value):
        if value == "magnet":
            raise KeyError("magnet")
        return _FakeSource()


class _FakeDrive:
    name = "115网盘"

    def supports_resource_type(self, value):
        return value == "115"

    def supports(self, capability):
        return True


class DriveManagerMagnetFilterTests(unittest.TestCase):
    def setUp(self):
        # 下面几个 register_* 会替换 sys.modules 里的真实模块：用完必须
        # 恢复，否则后跑的用例加载 strm_export 等模块时会拿到这里的桩
        # （CloudDriveCapability 是桩时，STRM_EXPORT 的 == 比较会失败，
        # 全量套件里见过）。保存旧值，tearDown 原样恢复。
        self._saved_modules = {}
        for name in (
            "cloudsubscribex.core",
            "cloudsubscribex.core.cloud",
            "cloudsubscribex.core.transfer",
            "cloudsubscribex.drive",
            "cloudsubscribex.drive.scanner",
            "cloudsubscribex.search.types",
        ):
            self._saved_modules[name] = sys.modules.get(name)
        self.addCleanup(self._restore_modules)
        register_module("cloudsubscribex.core", package=True)
        register_mock(
            "cloudsubscribex.core.cloud",
            # 能力枚举用 MagicMock 兜底：真实枚举成员很多（如
            # STRM_EXPORT），手写不全的桩类会在别的用例加载
            # strm_export 等模块时炸掉。这里只要求 supports()
            # 返回 True，能力值本身不参与比较。
            CloudDriveCapability=MagicMock(),
            CloudDriveProvider=object,
            CloudDriveRegistry=object,
        )
        register_mock(
            "cloudsubscribex.core.transfer",
            CrossTransferTaskManager=MagicMock(),
        )
        register_module("cloudsubscribex.drive", package=True)
        register_mock("cloudsubscribex.drive.scanner", DriverRegistry=MagicMock())
        register_module(
            "cloudsubscribex.search.types",
            normalize_resource_type=types_module.normalize_resource_type,
        )
        manager_module = load_module(
            "cloudsubscribex.core.drive_manager",
            "plugins.v2/cloudsubscribex/core/drive_manager.py",
        )
        self.CloudDriveManager = manager_module.CloudDriveManager

    def _restore_modules(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _manager(self, policy, cross_enabled=True):
        manager = self.CloudDriveManager.__new__(self.CloudDriveManager)
        manager._plugin = SimpleNamespace(
            _resource_type_order=["115", "ed2k", "magnet"],
            _cross_transfer_enabled=cross_enabled,
            _platform_download_policy=policy,
            _cloud_drive_key="115",
        )
        manager.registry = _FakeRegistry()
        manager.active_drive = _FakeDrive()
        return manager

    def test_cloud_policy_keeps_magnet(self):
        manager = self._manager("cloud")
        manager._filter_supported_resource_types()
        self.assertIn("magnet", manager._plugin._resource_type_order)
        self.assertIn("magnet", manager._plugin._cloud_drive_resource_types)

    def test_block_policy_strips_magnet(self):
        manager = self._manager("block")
        manager._filter_supported_resource_types()
        self.assertNotIn("magnet", manager._plugin._resource_type_order)
        self.assertNotIn("magnet", manager._plugin._cloud_drive_resource_types)

    def test_cloud_policy_keeps_magnet_without_cross_transfer(self):
        """Magnet 落盘走 115 离线下载，不依赖跨盘开关。"""
        manager = self._manager("cloud", cross_enabled=False)
        manager._filter_supported_resource_types()
        self.assertIn("magnet", manager._plugin._resource_type_order)
