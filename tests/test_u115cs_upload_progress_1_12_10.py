"""v1.12.10：115 存储适配器上传时上报字节进度（MP 整理队列可见）。

MP 整理队列的进度是按文件数算的（已处理/总数），单文件整理全程
0%、做完才跳 100%，所以 1.31GB 的 S01E232 整理时页面一直 0%——
不是卡住。本版让 U115CSStorage.upload 把进度回调传给 upload_file，
回调把字节百分比写进 MP 的 ProgressHelper(FileTransfer)，前端的
整理进度就能动了。
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=mock.MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=mock.MagicMock())
register_mock("app.log", logger=mock.MagicMock())
register_module("app.runtime", package=True)

_register_real_package("cloudsubscribex", _plugin_dir())
_core_pkg = _register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_core_pkg.OwnerDelegator = OwnerDelegator
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.drive.p115", _plugin_dir("drive", "p115"))

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
    """只记构造参数的 FileItem 替身。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageUsage:
    """只记构造参数的 StorageUsage 替身。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageBase:
    """StorageBase 替身：无参构造，不碰 MP 的 StorageHelper。"""

    schema = None

    def __init__(self):
        self.storagehelper = None


class StorageQueryError(Exception):
    """MP app.schemas.exception.StorageQueryError 的测试替身。"""


register_module(
    "app.modules.filemanager.storages", StorageBase=FakeStorageBase
)
register_module(
    "app.schemas.file", FileItem=FakeFileItem, StorageUsage=FakeStorageUsage
)
register_module("app.schemas.exception", StorageQueryError=StorageQueryError)


class FakeProgressHelper:
    """MP app.runtime.progress.ProgressHelper 的测试替身。"""

    updates = []

    def __init__(self, key):
        self.key = key

    def update(self, value=None, text=None, data=None):
        FakeProgressHelper.updates.append((value, text))


register_module("app.runtime.progress", ProgressHelper=FakeProgressHelper)

storage = load_module(
    "cloudsubscribex.drive.p115.storage",
    "plugins.v2/cloudsubscribex/drive/p115/storage.py",
)
U115CSStorage = storage.U115CSStorage


class FakeDirectoryReader:
    def __init__(self):
        self.directories = {}

    def resolve_directory(self, path, create=False):
        checked, directory_id = self.directories.get(path, (False, None))
        return DirectoryLookup(checked=checked, directory_id=directory_id)


class FakeFileQuery:
    def __init__(self):
        self.files = {}

    def find_file(self, path, file_name, **kwargs):
        return self.files.get((path, file_name))


class FakeUploadWithProgress:
    """上传时按给定序列调 progress_callback 的替身。"""

    def __init__(self, total, fractions):
        self.total = total
        self.fractions = list(fractions)
        self.calls = []
        self.callback = None
        self.result = True
        self.last_error = ""

    def upload_file(self, local_path, save_path, target_name="",
                    file_sha1="", progress_callback=None):
        self.calls.append((local_path, save_path, target_name))
        self.callback = progress_callback
        if progress_callback is not None:
            for fraction in self.fractions:
                uploaded = int(self.total * fraction)
                if fraction >= 1.0:
                    uploaded = self.total
                progress_callback(uploaded, self.total)
        return self.result


class FakeProvider:
    def __init__(self, services):
        self.services = dict(services)

    def require(self, capability):
        if capability not in self.services:
            raise KeyError(str(capability))
        return self.services[capability]


def _cloud_file(file_id, name, size=123, pickcode="PC1"):
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=False,
        size=size,
        sha1="",
        md5="",
        playback_values={"pickcode": pickcode} if pickcode else {},
        native={},
    )


class UploadProgressTestBase(unittest.TestCase):
    def setUp(self):
        FakeProgressHelper.updates = []
        self.total = 100 * 1024 * 1024
        self.tmp = tempfile.TemporaryDirectory()
        self.local_path = Path(self.tmp.name) / "a.mkv"
        self.local_path.write_bytes(b"x" * 1024)
        self.reader = FakeDirectoryReader()
        self.query = FakeFileQuery()
        self.reader.directories["/dst"] = (True, "200")
        self.movie = _cloud_file("11", "a.mkv", size=123)
        self.query.files[("/dst", "a.mkv")] = self.movie

    def tearDown(self):
        storage._BOUND_PROVIDER = None
        self.tmp.cleanup()
        register_module("app.runtime.progress",
                        ProgressHelper=FakeProgressHelper)

    def _make_store(self, fractions):
        self.upload = FakeUploadWithProgress(self.total, fractions)
        self.provider = FakeProvider({
            CloudDriveCapability.DIRECTORY_READ: self.reader,
            CloudDriveCapability.FILE_QUERY: self.query,
            CloudDriveCapability.LOCAL_UPLOAD: self.upload,
        })
        storage.bind_u115cs_provider(self.provider)
        return U115CSStorage()

    def _upload(self, store):
        item = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        return store.upload(item, self.local_path, new_name="a.mkv")


class TestUploadProgress(UploadProgressTestBase):
    def test_upload_passes_progress_callback_and_reports(self):
        """上传把进度回调传下去，MP 进度器收到递增的字节百分比。"""
        store = self._make_store([0.0, 0.25, 0.5, 0.75, 1.0])
        counter = [999.0]

        def _tick():
            counter[0] += 1.5
            return counter[0]

        with mock.patch("time.monotonic", side_effect=_tick):
            result = self._upload(store)
        self.assertIsNotNone(result)
        self.assertIsNotNone(self.upload.callback)
        self.assertEqual(len(self.upload.calls), 1)
        updates = FakeProgressHelper.updates
        self.assertEqual(len(updates), 5)
        values = [value for value, _ in updates]
        self.assertEqual(values, [0.0, 25.0, 50.0, 75.0, 100.0])
        last_value, last_text = updates[-1]
        self.assertEqual(last_value, 100.0)
        self.assertIn("a.mkv", last_text)
        self.assertIn("100MB/100MB", last_text)

    def test_upload_progress_throttled_within_one_second(self):
        """1 秒内多次回调只上报一次，100% 那次一定上报。"""
        store = self._make_store([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0])
        with mock.patch("time.monotonic", return_value=1000.0):
            result = self._upload(store)
        self.assertIsNotNone(result)
        updates = FakeProgressHelper.updates
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0][0], 0.0)
        self.assertEqual(updates[-1][0], 100.0)

    def test_upload_progress_helper_failure_does_not_break_upload(self):
        """进度器 update 抛错也不影响上传成功。"""
        class RaisingHelper(FakeProgressHelper):
            def update(self, value=None, text=None, data=None):
                raise RuntimeError("进度缓存挂了")

        register_module("app.runtime.progress",
                        ProgressHelper=RaisingHelper)
        store = self._make_store([0.0, 0.5, 1.0])
        counter = [999.0]

        def _tick():
            counter[0] += 1.5
            return counter[0]

        with mock.patch("time.monotonic", side_effect=_tick):
            result = self._upload(store)
        self.assertIsNotNone(result)

    def test_upload_without_progress_module_still_works(self):
        """MP 进度模块不存在时上传照常成功（惰性导入兜底）。"""
        sys.modules.pop("app.runtime.progress", None)
        store = self._make_store([0.0, 0.5, 1.0])
        counter = [999.0]

        def _tick():
            counter[0] += 1.5
            return counter[0]

        with mock.patch("time.monotonic", side_effect=_tick):
            result = self._upload(store)
        self.assertIsNotNone(result)
        self.assertEqual(FakeProgressHelper.updates, [])


if __name__ == "__main__":
    unittest.main()
