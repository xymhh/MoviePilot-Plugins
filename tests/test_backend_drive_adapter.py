"""网盘驱动统一存储适配器（DriveStorageAdapter）单元测试。"""

import os
import sys
import unittest

from plugin_env import REPO_ROOT, load_module, register_package

register_package("cloudsubscribex", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex")])
register_package("cloudsubscribex.core", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core")])

_exceptions = load_module(
    "cloudsubscribex.core.exceptions",
    "plugins.v2/cloudsubscribex/core/exceptions.py",
)
sys.modules["cloudsubscribex.core.exceptions"] = _exceptions

_cloud = load_module(
    "cloudsubscribex.core.cloud",
    "plugins.v2/cloudsubscribex/core/cloud.py",
)
sys.modules["cloudsubscribex.core.cloud"] = _cloud

Adapter = _cloud.DriveStorageAdapter
CloudFile = _cloud.CloudFile


class DriveStorageAdapterNormalizationTest(unittest.TestCase):
    def test_normalize_yun139_special_casing(self):
        """测试移动云盘特殊的 caID / coID 字段被统一归一化。"""
        raw = {
            "caID": "node_139_xyz",
            "name": "庆余年.S02E01.4K.mp4",
            "size": "4500000000",
            "is_dir": False,
        }
        item = Adapter.normalize_cloud_file(raw, parent_path="Season 02")
        self.assertIsNotNone(item)
        self.assertEqual(item.id, "node_139_xyz")
        self.assertEqual(item.name, "庆余年.S02E01.4K.mp4")
        self.assertEqual(item.size, 4500000000)
        self.assertEqual(item.parent_path, "Season 02")
        self.assertEqual(item.relative_path, "Season 02/庆余年.S02E01.4K.mp4")
        # 支持字典取值
        self.assertEqual(item["relative_path"], "Season 02/庆余年.S02E01.4K.mp4")

    def test_normalize_quark_fid_and_file_name(self):
        """测试夸克网盘 fid / file_name 映射。"""
        raw = {
            "fid": "quark_file_001",
            "file_name": "你能看到一切.2026.mp4",
            "file_size": 2048000,
            "dir": 0,
        }
        item = Adapter.normalize_cloud_file(raw)
        self.assertIsNotNone(item)
        self.assertEqual(item.id, "quark_file_001")
        self.assertEqual(item.name, "你能看到一切.2026.mp4")
        self.assertFalse(item.is_directory)
        self.assertEqual(item.relative_path, "你能看到一切.2026.mp4")

    def test_normalize_directory_node(self):
        raw = {
            "id": "dir_001",
            "name": "第 01 季",
            "file_type": 0,
        }
        item = Adapter.normalize_cloud_file(raw)
        self.assertIsNotNone(item)
        self.assertTrue(item.is_directory)
        self.assertEqual(item.size, 0)


class DriveStorageAdapterExceptionMappingTest(unittest.TestCase):
    def test_network_timeout_mapped_to_network_error(self):
        err = Adapter.normalize_drive_exception(TimeoutError("Read timed out"), provider="夸克")
        self.assertIsInstance(err, _exceptions.NetworkError)
        self.assertTrue(err.retryable)

    def test_unauthorized_mapped_to_auth_expired(self):
        err = Adapter.normalize_drive_exception(RuntimeError("HTTP 401: token expired"), provider="115")
        self.assertIsInstance(err, _exceptions.AuthExpiredError)
        self.assertEqual(err.provider, "115")

    def test_deleted_share_mapped_to_resource_invalid(self):
        err = Adapter.normalize_drive_exception(ValueError("该分享文件已被删除"), provider="阿里")
        self.assertIsInstance(err, _exceptions.ResourceInvalidError)


if __name__ == "__main__":
    unittest.main()
