"""P2：115 存储适配器“115网盘CS版”（v1.12.0）。

适配器把插件现有的 115 实现包装成 MP StorageBase 子类（id=u115cs），
供 MP 整理直接调用；注册函数把类追加进文件管理模块的运行实例列表
与存储配置，并自检。用假 provider 与假 MP 模块覆盖全部方法。
"""

import os
import shutil
import sys
import tempfile
import types
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


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())

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
DirectoryListing = core_cloud.DirectoryListing
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

storage = load_module(
    "cloudsubscribex.drive.p115.storage",
    "plugins.v2/cloudsubscribex/drive/p115/storage.py",
)
U115CSStorage = storage.U115CSStorage


class FakeDirectoryReader:
    def __init__(self):
        self.directories = {}
        self.listings = {}
        self.resolve_calls = []
        self.list_calls = []

    def resolve_directory(self, path, create=False):
        self.resolve_calls.append((path, create))
        checked, directory_id = self.directories.get(path, (False, None))
        return DirectoryLookup(checked=checked, directory_id=directory_id)

    def list_directory(self, directory_id):
        self.list_calls.append(str(directory_id))
        return self.listings.get(
            str(directory_id), DirectoryListing(False, ())
        )


class FakeFileQuery:
    def __init__(self):
        self.files = {}
        self.download_calls = []
        self.download_raises = False
        self.find_file_hook = None

    def find_file(self, path, file_name, **kwargs):
        if self.find_file_hook is not None:
            return self.find_file_hook(path, file_name)
        return self.files.get((path, file_name))

    def download_file(self, file_item, local_path, *args, **kwargs):
        self.download_calls.append((file_item, local_path))
        if self.download_raises:
            raise RuntimeError("下载失败")
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"fake-content")
        return str(local_path)


class FakeFileMutation:
    def __init__(self):
        self.rename_calls = []
        self.move_calls = []
        self.delete_calls = []
        self.rename_result = True
        self.move_result = None
        self.delete_result = True

    def rename_file(self, path, item, target_name):
        self.rename_calls.append((path, item, target_name))
        return self.rename_result

    def move_file(self, item, save_path, target_name):
        self.move_calls.append((item, save_path, target_name))
        return self.move_result

    def delete_file(self, file_id):
        self.delete_calls.append(file_id)
        return self.delete_result


class FakeUpload:
    def __init__(self):
        self.calls = []
        self.result = True
        self.last_error = ""

    def upload_file(self, local_path, save_path, target_name="",
                    file_sha1="", progress_callback=None):
        self.calls.append((local_path, save_path, target_name))
        return self.result


class FakeRateLimiter:
    def __init__(self):
        self.calls = []

    def call(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))
        return func(*args, **kwargs)


class FakeClient:
    def __init__(self):
        self.copy_calls = []
        self.copy_result = {"state": True, "data": {}}
        self.index_result = {
            "state": True,
            "data": {
                "space_info": {
                    "all_total": {"size": 1000},
                    "all_remain": {"size": 400},
                },
            },
        }

    def fs_copy_open(self, payload, pid=0, **kwargs):
        self.copy_calls.append((payload, pid))
        return self.copy_result

    def fs_index_info(self, payload=0, **kwargs):
        return self.index_result


class FakeManager:
    def __init__(self):
        self.client = FakeClient()
        self.rate_limiter = FakeRateLimiter()
        self.login_result = True

    def check_login(self):
        return self.login_result


class FakeProvider:
    def __init__(self, services):
        self.services = dict(services)

    def require(self, capability):
        if capability not in self.services:
            raise KeyError(str(capability))
        return self.services[capability]


def _cloud_file(file_id, name, is_directory=False, size=123,
                pickcode="PC1"):
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=0 if is_directory else size,
        sha1="",
        md5="",
        playback_values={"pickcode": pickcode} if pickcode else {},
        native={},
    )


class U115CSStorageTestBase(unittest.TestCase):
    def setUp(self):
        self.reader = FakeDirectoryReader()
        self.query = FakeFileQuery()
        self.mutation = FakeFileMutation()
        self.upload = FakeUpload()
        self.manager = FakeManager()
        self.provider = FakeProvider({
            CloudDriveCapability.DIRECTORY_READ: self.reader,
            CloudDriveCapability.FILE_QUERY: self.query,
            CloudDriveCapability.FILE_MUTATION: self.mutation,
            CloudDriveCapability.LOCAL_UPLOAD: self.upload,
            CloudDriveCapability.ACCOUNT: self.manager,
        })
        storage.bind_u115cs_provider(self.provider)
        self.store = U115CSStorage()
        self.movie = _cloud_file("11", "a.mkv", size=123)
        self.subdir = _cloud_file("12", "sub", is_directory=True)
        self.reader.directories["/dst"] = (True, "200")
        self.reader.listings["200"] = DirectoryListing(
            True, (self.movie, self.subdir)
        )
        self.query.files[("/dst", "a.mkv")] = self.movie
        self.query.files[("/dst", "sub")] = self.subdir
        self.query.files[("/", "dst")] = _cloud_file(
            "200", "dst", is_directory=True, pickcode=""
        )

    def tearDown(self):
        storage._BOUND_PROVIDER = None


class TestSchemaAndBinding(U115CSStorageTestBase):
    def test_schema_value_is_u115cs(self):
        self.assertEqual(U115CSStorage.schema.value, "u115cs")
        self.assertEqual(U115CSStorage.schema.name, "U115CS")

    def test_bind_rejects_invalid_provider(self):
        self.assertFalse(storage.bind_u115cs_provider(None))
        self.assertFalse(storage.bind_u115cs_provider(object()))
        self.assertTrue(storage.bind_u115cs_provider(self.provider))

    def test_unbound_methods_return_failure_values(self):
        storage._BOUND_PROVIDER = None
        store = U115CSStorage()
        item = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        self.assertFalse(store.check())
        self.assertEqual(store.list(item), [])
        self.assertIsNone(store.get_item(Path("/dst/a.mkv")))
        self.assertIsNone(store.get_folder(Path("/dst")))
        self.assertIsNone(store.detail(item))
        self.assertIsNone(
            store.upload(item, Path("/tmp/a.mkv"), "a.mkv")
        )
        self.assertFalse(store.copy(item, Path("/dst"), "b.mkv"))
        self.assertFalse(store.move(item, Path("/dst"), "b.mkv"))
        self.assertFalse(store.rename(item, "b.mkv"))
        self.assertFalse(store.delete(item))
        self.assertIsNone(store.download(item, Path("/tmp")))
        self.assertIsNone(store.create_folder(item, "new"))
        self.assertIsNone(store.usage())
        self.assertFalse(store.link(item, Path("/tmp/x")))
        self.assertFalse(store.softlink(item, Path("/tmp/x")))
        self.assertFalse(store.reflink(item, Path("/tmp/x")))
        self.assertIsNone(store.close())


class TestListAndQuery(U115CSStorageTestBase):
    def test_list_directory_maps_items(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst", type="dir", fileid="200"
        )
        items = self.store.list(item)
        self.assertEqual(self.reader.list_calls, ["200"])
        self.assertEqual(len(items), 2)
        movie, sub = items
        self.assertEqual(movie.storage, "u115cs")
        self.assertEqual(movie.path, "/dst/a.mkv")
        self.assertEqual(movie.type, "file")
        self.assertEqual(movie.name, "a.mkv")
        self.assertEqual(movie.basename, "a")
        self.assertEqual(movie.extension, "mkv")
        self.assertEqual(movie.size, 123)
        self.assertEqual(movie.fileid, "11")
        self.assertEqual(movie.pickcode, "PC1")
        self.assertEqual(sub.path, "/dst/sub/")
        self.assertEqual(sub.type, "dir")
        self.assertIsNone(sub.extension)
        self.assertIsNone(sub.size)

    def test_list_resolves_directory_id_when_missing(self):
        item = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        items = self.store.list(item)
        self.assertEqual(len(items), 2)
        self.assertIn(("/dst", False), self.reader.resolve_calls)

    def test_list_missing_directory_returns_empty(self):
        item = FakeFileItem(storage="u115cs", path="/nope", type="dir")
        self.assertEqual(self.store.list(item), [])

    def test_list_file_item_returns_detail(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv",
        )
        items = self.store.list(item)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].path, "/dst/a.mkv")

    def test_get_item_found_missing_and_root(self):
        found = self.store.get_item(Path("/dst/a.mkv"))
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "a.mkv")
        self.assertEqual(found.fileid, "11")
        self.assertIsNone(self.store.get_item(Path("/dst/nope.mkv")))
        root = self.store.get_item(Path("/"))
        self.assertEqual(root.type, "dir")
        self.assertEqual(root.fileid, "0")
        self.assertEqual(root.path, "/")

    def test_detail_uses_path(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv",
        )
        detail = self.store.detail(item)
        self.assertEqual(detail.name, "a.mkv")
        self.assertIsNone(
            self.store.detail(FakeFileItem(storage="u115cs"))
        )

    def test_get_folder_existing_and_creates_missing(self):
        folder = self.store.get_folder(Path("/dst"))
        self.assertEqual(folder.type, "dir")
        self.assertEqual(folder.path, "/dst/")
        self.reader.directories["/new"] = (True, "300")

        def hook(path, name):
            # /new 只有在 resolve_directory(create=True) 之后才出现。
            if (path, name) == ("/", "new"):
                if ("/new", True) in self.reader.resolve_calls:
                    return _cloud_file(
                        "300", "new", is_directory=True, pickcode=""
                    )
                return None
            return self.query.files.get((path, name))

        self.query.find_file_hook = hook
        created = self.store.get_folder(Path("/new"))
        self.assertEqual(created.name, "new")
        self.assertIn(("/new", True), self.reader.resolve_calls)

    def test_create_folder(self):
        parent = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        self.reader.directories["/dst/made"] = (True, "301")
        self.query.files[("/dst", "made")] = _cloud_file(
            "301", "made", is_directory=True, pickcode=""
        )
        created = self.store.create_folder(parent, "made")
        self.assertEqual(created.name, "made")
        self.assertIn(("/dst/made", True), self.reader.resolve_calls)


class TestUpload(U115CSStorageTestBase):
    def _local_file(self):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, True)
        local = Path(temp_dir) / "b.mkv"
        local.write_bytes(b"0123456789")
        return local

    def test_upload_success_returns_found_item(self):
        local = self._local_file()
        target = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        self.query.files[("/dst", "b.mkv")] = _cloud_file(
            "21", "b.mkv", size=10
        )
        result = self.store.upload(target, local, "b.mkv")
        self.assertEqual(
            self.upload.calls, [(str(local), "/dst", "b.mkv")]
        )
        self.assertEqual(result.path, "/dst/b.mkv")
        self.assertEqual(result.fileid, "21")
        self.assertIn(("/dst", True), self.reader.resolve_calls)

    def test_upload_failure_returns_none(self):
        local = self._local_file()
        target = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        self.upload.result = False
        self.upload.last_error = "115 本地文件上传失败"
        self.assertIsNone(self.store.upload(target, local, "b.mkv"))

    def test_upload_synthesizes_item_when_index_invisible(self):
        local = self._local_file()
        target = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        result = self.store.upload(target, local)
        self.assertIsNotNone(result)
        self.assertEqual(result.path, "/dst/b.mkv")
        self.assertEqual(result.size, 10)
        self.assertEqual(result.extension, "mkv")

    def test_upload_rejects_missing_local_file(self):
        target = FakeFileItem(storage="u115cs", path="/dst", type="dir")
        self.assertIsNone(
            self.store.upload(target, Path("/tmp/no-such-file.mkv"))
        )
        self.assertEqual(self.upload.calls, [])

    def test_upload_rejects_unavailable_directory(self):
        local = self._local_file()
        target = FakeFileItem(storage="u115cs", path="/nope", type="dir")
        self.assertIsNone(self.store.upload(target, local))
        self.assertEqual(self.upload.calls, [])


class TestCopyMoveRenameDelete(U115CSStorageTestBase):
    def _copy_setup(self):
        self.query.files[("/src", "a.mkv")] = self.movie
        self.query.files[("/", "src")] = _cloud_file(
            "100", "src", is_directory=True, pickcode=""
        )
        self.query.files[("/", "dst")] = _cloud_file(
            "200", "dst", is_directory=True, pickcode=""
        )
        source = FakeFileItem(
            storage="u115cs", path="/src/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        return source

    def test_copy_success_with_rename(self):
        source = self._copy_setup()
        self.query.files[("/dst", "a.mkv")] = _cloud_file("31", "a.mkv")

        def hook(path, name):
            # 复制前 /dst/b.mkv 不存在（否则适配器会拒绝）；复制后才出现。
            if (path, name) == ("/dst", "b.mkv"):
                if self.manager.client.copy_calls:
                    return _cloud_file("31", "b.mkv")
                return None
            return self.query.files.get((path, name))

        self.query.find_file_hook = hook
        self.assertTrue(self.store.copy(source, Path("/dst"), "b.mkv"))
        self.assertEqual(self.manager.client.copy_calls, [(11, 200)])
        self.assertEqual(
            self.mutation.rename_calls,
            [("/dst", self.query.files[("/dst", "a.mkv")], "b.mkv")],
        )

    def test_copy_same_name_skips_rename(self):
        source = self._copy_setup()

        def hook(path, name):
            # 复制前 /dst/a.mkv 不存在；复制后才出现（与源同名，无需改名）。
            if (path, name) == ("/dst", "a.mkv"):
                if self.manager.client.copy_calls:
                    return _cloud_file("31", "a.mkv")
                return None
            return self.query.files.get((path, name))

        self.query.find_file_hook = hook
        self.assertTrue(self.store.copy(source, Path("/dst"), "a.mkv"))
        self.assertEqual(self.mutation.rename_calls, [])

    def test_copy_refuses_existing_target(self):
        source = self._copy_setup()
        self.query.files[("/dst", "b.mkv")] = _cloud_file("32", "b.mkv")
        self.assertFalse(self.store.copy(source, Path("/dst"), "b.mkv"))
        self.assertEqual(self.manager.client.copy_calls, [])

    def test_copy_api_failure_returns_false(self):
        source = self._copy_setup()
        self.manager.client.copy_result = {"state": False}
        self.assertFalse(self.store.copy(source, Path("/dst"), "b.mkv"))

    def test_copy_missing_target_directory_returns_false(self):
        source = self._copy_setup()
        self.assertFalse(self.store.copy(source, Path("/nope"), "b.mkv"))

    def test_move_success_and_failure(self):
        source = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        self.mutation.move_result = _cloud_file("11", "b.mkv")
        self.assertTrue(self.store.move(source, Path("/dst"), "b.mkv"))
        self.assertEqual(
            self.mutation.move_calls,
            [(self.movie, "/dst", "b.mkv")],
        )
        self.mutation.move_result = None
        self.assertFalse(self.store.move(source, Path("/dst"), "b.mkv"))

    def test_rename_success_and_failure(self):
        source = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        self.assertTrue(self.store.rename(source, "b.mkv"))
        self.assertEqual(
            self.mutation.rename_calls, [("/dst", self.movie, "b.mkv")]
        )
        self.mutation.rename_result = False
        self.assertFalse(self.store.rename(source, "b.mkv"))
        self.assertFalse(self.store.rename(source, ""))

    def test_delete_by_fileid_and_by_path(self):
        by_id = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        self.assertTrue(self.store.delete(by_id))
        self.assertEqual(self.mutation.delete_calls, ["11"])
        by_path = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv",
        )
        self.assertTrue(self.store.delete(by_path))
        self.assertEqual(self.mutation.delete_calls, ["11", "11"])
        self.mutation.delete_result = False
        self.assertFalse(self.store.delete(by_id))


class TestDownloadCheckUsage(U115CSStorageTestBase):
    def test_download_success(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.store.download(item, Path(temp_dir))
            self.assertIsNotNone(result)
            self.assertTrue(result.is_file())
            self.assertEqual(result.name, "a.mkv")
            self.assertEqual(
                self.query.download_calls,
                [(self.movie, str(Path(temp_dir) / "a.mkv"))],
            )

    def test_download_to_default_temp_dir(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        result = self.store.download(item)
        try:
            self.assertIsNotNone(result)
            self.assertTrue(str(result).startswith(
                str(Path(tempfile.gettempdir()) / "u115cs-download")
            ))
        finally:
            shutil.rmtree(
                Path(tempfile.gettempdir()) / "u115cs-download",
                ignore_errors=True,
            )

    def test_download_missing_file_returns_none(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst/nope.mkv", type="file",
            name="nope.mkv",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(self.store.download(item, Path(temp_dir)))
        self.assertEqual(self.query.download_calls, [])

    def test_download_failure_returns_none(self):
        item = FakeFileItem(
            storage="u115cs", path="/dst/a.mkv", type="file",
            name="a.mkv", fileid="11",
        )
        self.query.download_raises = True
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertIsNone(self.store.download(item, Path(temp_dir)))

    def test_check_login(self):
        self.assertTrue(self.store.check())
        self.manager.login_result = False
        self.assertFalse(self.store.check())

    def test_usage_success(self):
        usage = self.store.usage()
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total, 1000.0)
        self.assertEqual(usage.available, 400.0)

    def test_usage_missing_data_returns_none(self):
        self.manager.client.index_result = {"state": False}
        self.assertIsNone(self.store.usage())
        self.manager.client.index_result = {
            "state": True, "data": {"space_info": {}},
        }
        self.assertIsNone(self.store.usage())


class FakeFileManagerModule:
    _storage_schemas = []
    _support_storages = []


class FakeModuleManager:
    def __init__(self, instance):
        self.instance = instance

    def get_running_subtype_module(self, subtype):
        if self.instance is not None:
            yield self.instance

    def get_running_modules(self, method):
        if self.instance is not None:
            yield self.instance


class FakeStorageHelper:
    configs = {}

    def get_storage(self, storage):
        return self.configs.get(storage)

    def add_storage(self, storage, name, conf):
        self.configs[storage] = {
            "type": storage, "name": name, "config": conf,
        }


class FakeOtherModulesType:
    FileManager = "filemanager"


class TestRegisterU115CSStorage(unittest.TestCase):
    def setUp(self):
        self.instance = FakeFileManagerModule()
        self.instance._storage_schemas = []
        self.instance._support_storages = []
        FakeFileManagerModule._storage_schemas = []
        FakeFileManagerModule._support_storages = []
        FakeStorageHelper.configs = {}
        self.module_manager = FakeModuleManager(self.instance)
        register_module(
            "app.modules.filemanager.module",
            FileManagerModule=FakeFileManagerModule,
        )
        register_module(
            "app.application.module",
            get_module_manager=lambda: self.module_manager,
        )
        register_module(
            "app.application.storage", StorageHelper=FakeStorageHelper
        )
        register_module(
            "app.schemas.types", OtherModulesType=FakeOtherModulesType
        )

    def test_register_success_and_dedup(self):
        self.assertTrue(storage.register_u115cs_storage())
        self.assertIn(
            storage.U115CSStorage, self.instance._storage_schemas
        )
        self.assertIn("u115cs", self.instance._support_storages)
        self.assertIn(
            storage.U115CSStorage, FakeFileManagerModule._storage_schemas
        )
        self.assertIn("u115cs", FakeFileManagerModule._support_storages)
        self.assertEqual(
            FakeStorageHelper.configs["u115cs"]["name"], "115网盘CS版"
        )
        # 二次注册不重复追加。
        self.assertTrue(storage.register_u115cs_storage())
        self.assertEqual(len(self.instance._storage_schemas), 1)
        self.assertEqual(len(self.instance._support_storages), 1)
        self.assertEqual(len(FakeStorageHelper.configs), 1)

    def test_register_without_live_instance_returns_false(self):
        self.module_manager.instance = None
        self.assertFalse(storage.register_u115cs_storage())


if __name__ == "__main__":
    unittest.main()
