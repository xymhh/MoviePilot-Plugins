"""115网盘CS版存储的严格查询 get_item_strict（1.12.3）。

背景（2026-11-16）：用户更新到 v1.12.2 后重试《十万狂花入梦来》
S01E06，下载这回落对了名（/downloads/十万狂花入梦来 - S01E06 -
第6集.mkv），MP 也认出了剧名，但整理在“目标存储访问”阶段失败：
“无法确认目标文件状态，已跳过整理以避免误覆盖……存储 U115CS
未实现严格查询，无法确认目标状态”。根因：MP 的整理在写目标文件
前调 ``get_item_strict`` 确认目标不存在才敢写（转存处理器的覆盖
检查），适配器没实现它，继承了 StorageBase 的默认实现——直接抛
StorageQueryError，MP 只能跳过。

固定契约（与 MP 自带 u115 的 get_item_strict 一致）：
1. 目标存在 → 返回 FileItem；目标确认不存在 → 返回 None；
2. 状态无法确认（提供方未绑定、父目录解析失败、目录列举失败）
   → 抛 StorageQueryError，绝不把故障吞成 None（那会被 MP 当成
   “确认不存在”去覆盖）；
3. 父目录确认不存在时直接返回 None，不调 find_file_strict——
   整理目标的父目录常常还不存在（如刚按剧名新建的季目录），
   find_file_strict 会因父目录解析不到而抛错，整理又会被跳过。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


class StorageQueryError(Exception):
    """MP app.schemas.exception.StorageQueryError 的测试替身。"""


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())
register_module("app.schemas.exception", StorageQueryError=StorageQueryError)

_register_real_package("cloudsubscribex", _plugin_dir())
_core_pkg = _register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_core_pkg.OwnerDelegator = OwnerDelegator
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.drive.p115", _plugin_dir("drive", "p115"))

import types

_existing_cloud = sys.modules.get("cloudsubscribex.core.cloud")
if isinstance(_existing_cloud, types.ModuleType) and isinstance(
    getattr(_existing_cloud, "CloudDriveCapability", None), type
):
    core_cloud = _existing_cloud
else:
    core_cloud = load_module(
        "cloudsubscribex.core.cloud",
        "plugins.v2/cloudsubscribex/core/cloud.py",
    )
CloudDriveCapability = core_cloud.CloudDriveCapability
CloudFile = core_cloud.CloudFile
DirectoryLookup = core_cloud.DirectoryLookup


class FakeFileItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageUsage:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageBase:
    schema = None

    def __init__(self):
        self.storagehelper = None


register_module(
    "app.modules.filemanager.storages", StorageBase=FakeStorageBase
)
register_module(
    "app.schemas.file", FileItem=FakeFileItem, StorageUsage=FakeStorageUsage
)

storage = load_module(
    "cloudsubscribex.drive.p115.storage",
    "plugins.v2/cloudsubscribex/drive/p115/storage.py",
)
U115CSStorage = storage.U115CSStorage

TARGET_DIR = "/Alist/影视/moviepilot/转存/十万狂花入梦来 (2026) {tmdb-261315}/Season 1"
TARGET_NAME = "十万狂花入梦来 - S01E06 - 第6集.mkv"
TARGET_PATH = f"{TARGET_DIR}/{TARGET_NAME}"


class FakeDirectoryReader:
    def __init__(self):
        self.directories = {}
        self.resolve_calls = []

    def resolve_directory(self, path, create=False):
        self.resolve_calls.append((path, create))
        checked, directory_id = self.directories.get(path, (False, None))
        return DirectoryLookup(checked=checked, directory_id=directory_id)


class FakeFileQuery:
    def __init__(self):
        self.files = {}
        self.strict_calls = []
        self.strict_raises = None

    def find_file_strict(self, path, file_name):
        self.strict_calls.append((path, file_name))
        if self.strict_raises is not None:
            raise self.strict_raises
        return self.files.get((path, file_name))


class FakeProvider:
    def __init__(self, services):
        self.services = dict(services)

    def require(self, capability):
        if capability not in self.services:
            raise KeyError(str(capability))
        return self.services[capability]


def _cloud_file(file_id, name, is_directory=False, size=377000000):
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=0 if is_directory else size,
        sha1="",
        md5="",
        playback_values={"pickcode": "PC1"} if not is_directory else {},
        native={},
    )


class StrictQueryTest(unittest.TestCase):
    def setUp(self):
        storage._BOUND_PROVIDER = None
        self.reader = FakeDirectoryReader()
        self.query = FakeFileQuery()
        self.provider = FakeProvider({
            CloudDriveCapability.DIRECTORY_READ: self.reader,
            CloudDriveCapability.FILE_QUERY: self.query,
        })
        # 父目录默认已存在（目录 id "100"）。
        self.reader.directories[TARGET_DIR] = (True, "100")
        storage.bind_u115cs_provider(self.provider)
        self.storage = U115CSStorage()

    def tearDown(self):
        storage._BOUND_PROVIDER = None

    def test_existing_target_returns_file_item(self):
        """目标已在 → 返回 FileItem，MP 按覆盖策略处理。"""
        self.query.files[(TARGET_DIR, TARGET_NAME)] = _cloud_file(
            "200", TARGET_NAME
        )

        item = self.storage.get_item_strict(Path(TARGET_PATH))

        self.assertIsNotNone(item)
        self.assertEqual("u115cs", item.storage)
        self.assertEqual(TARGET_PATH, item.path)
        self.assertEqual(TARGET_NAME, item.name)
        self.assertEqual("file", item.type)

    def test_absent_target_returns_none(self):
        """父目录在、目标不在 → 返回 None，MP 可以写（S01E06 的现场）。"""
        item = self.storage.get_item_strict(Path(TARGET_PATH))

        self.assertIsNone(item)
        self.assertEqual([(TARGET_DIR, TARGET_NAME)], self.query.strict_calls)

    def test_missing_parent_returns_none_without_strict_lookup(self):
        """父目录确认不存在 → 直接返回 None，不调 find_file_strict。

        整理目标的父目录（如刚按剧名新建的季目录）常常还不存在；
        find_file_strict 会因父目录解析不到而抛错，整理又会被跳过。
        """
        del self.reader.directories[TARGET_DIR]
        self.reader.directories[TARGET_DIR] = (True, None)

        item = self.storage.get_item_strict(Path(TARGET_PATH))

        self.assertIsNone(item)
        self.assertEqual([], self.query.strict_calls)

    def test_unchecked_parent_raises(self):
        """父目录解析本身失败 → 抛错，不把故障当成不存在。"""
        self.reader.directories[TARGET_DIR] = (False, None)

        with self.assertRaises(StorageQueryError):
            self.storage.get_item_strict(Path(TARGET_PATH))
        self.assertEqual([], self.query.strict_calls)

    def test_listing_failure_raises(self):
        """目录列举失败 → 抛错，不把故障当成不存在。"""
        self.query.strict_raises = RuntimeError("无法读取115目录")

        with self.assertRaises(StorageQueryError):
            self.storage.get_item_strict(Path(TARGET_PATH))

    def test_unbound_provider_raises(self):
        """提供方未绑定 → 抛错，绝不返回 None 误导 MP 去覆盖。"""
        storage._BOUND_PROVIDER = None

        with self.assertRaises(StorageQueryError):
            self.storage.get_item_strict(Path(TARGET_PATH))

    def test_root_returns_root_dir(self):
        item = self.storage.get_item_strict(Path("/"))

        self.assertIsNotNone(item)
        self.assertEqual("/", item.path)
        self.assertEqual("dir", item.type)

    def test_existing_directory_target_returns_dir_item(self):
        """目标是已存在的目录 → 返回 dir 项（MP 清理旧目标时也用严格查询）。"""
        season_dir = "Season 1"
        parent = "/Alist/影视/moviepilot/转存/十万狂花入梦来 (2026) {tmdb-261315}"
        self.reader.directories[parent] = (True, "99")
        self.query.files[(parent, season_dir)] = _cloud_file(
            "99", season_dir, is_directory=True
        )

        item = self.storage.get_item_strict(Path(f"{parent}/{season_dir}"))

        self.assertIsNotNone(item)
        self.assertEqual("dir", item.type)
        self.assertEqual(f"{parent}/{season_dir}/", item.path)


if __name__ == "__main__":
    unittest.main()
