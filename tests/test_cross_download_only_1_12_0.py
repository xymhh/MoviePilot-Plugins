"""1.12.0 跨盘只下载到本地（P2）单测。

固定契约：

1. create_from_cloud_file(download_only=True) → 任务字典带
   download_only=True；默认 False；
2. download_only 任务：下载到本地后成功、method="download_only"、
   result_local_path 指向仍存在的本地文件、目标盘无真实上传、
   任务结束后缓存不删（文件归 MP 整理所有）；
3. download_only + 秒传探测命中 → 成功 method="rapid"、不下载、
   不上传（文件已在目标盘 /转存，调用方直接对它触发 MP 整理）；
4. 普通任务（download_only=False）仍走上传、成功后缓存照常删除。
"""

import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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

core_pkg = _register_real_package("cloudsubscribex", _plugin_dir())
core_pkg = _register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.drive.p115", _plugin_dir("drive", "p115"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
core_pkg.OwnerDelegator = OwnerDelegator
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

p115_files_module = register_module(
    "cloudsubscribex.drive.p115.files", package=True
)
p115_files_module.P115DirectoryReader = MagicMock()
p115_files_module.P115FileService = MagicMock()

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)

CloudFile = cloud_module.CloudFile
LocalRapidUploadAdapter = transfer_module.LocalRapidUploadAdapter
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager

PAYLOAD = b"0123456789"
SHA1 = hashlib.sha1(PAYLOAD).hexdigest()
MD5 = hashlib.md5(PAYLOAD).hexdigest()


def make_file_item(with_checksums=True, name="movie.mkv", size=len(PAYLOAD)):
    return CloudFile(
        id="fid1", name=name, is_directory=False, size=size,
        sha1=SHA1 if with_checksums else "",
        md5=MD5 if with_checksums else "",
    )


class FakeRapidUploadService:
    """秒传上传服务替身：记录探测/秒传/真实上传调用。"""

    rapid_requires_local_file = True

    def __init__(self, probe_outcomes=None, rapid_outcomes=None):
        self._probe_outcomes = list(probe_outcomes or [])
        self._rapid_outcomes = list(rapid_outcomes or [False])
        self.probe_calls = []
        self.rapid_calls = []
        self.upload_file_calls = []
        self.on_success = None

    def _next(self, outcomes, default):
        if outcomes:
            return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        return default

    def try_rapid_upload_by_hash(
            self, save_path, target_name, algorithm, checksum, size,
            read_range_bytes_or_hash=None,
    ):
        self.probe_calls.append({
            "save_path": save_path, "target_name": target_name,
            "algorithm": algorithm, "checksum": checksum, "size": size,
        })
        result = self._next(self._probe_outcomes, False)
        if result and self.on_success:
            self.on_success()
        return result

    def try_rapid_upload(self, local_path, save_path, target_name,
                        algorithm, checksum, size):
        self.rapid_calls.append({
            "local_path": local_path, "save_path": save_path,
            "target_name": target_name,
        })
        result = self._next(self._rapid_outcomes, False)
        if result and self.on_success:
            self.on_success()
        return result

    def upload_file(self, local_path, save_path, target_name="",
                    file_sha1="", progress_callback=None):
        self.upload_file_calls.append({
            "local_path": local_path, "save_path": save_path,
            "target_name": target_name,
        })
        if progress_callback:
            progress_callback(len(PAYLOAD), len(PAYLOAD))
        if self.on_success:
            self.on_success()
        return True


class FakeSourceProvider:
    """源盘替身：下载到本地 + 直链接口。"""

    name = "夸克网盘"

    def __init__(self):
        self.download_calls = []

    def supports(self, capability):
        cap = transfer_module.CloudDriveCapability
        return capability == cap.FILE_DOWNLOAD

    def require(self, capability):
        return SimpleNamespace(
            download_file=self.download_file,
            resolve_download_link=self.resolve_download_link,
        )

    def download_file(self, file_item, local_path, progress_callback=None,
                        stop_requested=None, preserve_partial=True,
                        download_threads=5):
        self.download_calls.append(local_path)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PAYLOAD)
        return local_path

    def resolve_download_link(self, file_item):
        return "https://src.example.com/movie.mkv", {"user-agent": "ua"}


class DownloadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.source = FakeSourceProvider()

    def make_manager(self, upload_service, rapid_miss_action="fallback"):
        files_service = MagicMock()
        target_file = CloudFile(
            id="n1", name="movie.mkv", is_directory=False, size=len(PAYLOAD),
            sha1=SHA1, md5=MD5,
        )
        present = {"value": False}

        def _activate_target_file():
            present["value"] = True

        upload_service.on_success = _activate_target_file

        def _find_file(*_args, **_kwargs):
            return target_file if present["value"] else None

        files_service.find_file.side_effect = _find_file
        adapter = LocalRapidUploadAdapter(
            upload_service, files_service, frozenset({"sha1"}),
        )
        local_upload = SimpleNamespace(
            upload_file=upload_service.upload_file,
            upload_from_link=None, upload_progressive=None,
        )
        file_query = SimpleNamespace(find_file=files_service.find_file)

        def require(capability):
            cap = transfer_module.CloudDriveCapability
            if capability == cap.RAPID_UPLOAD:
                return adapter
            if capability == cap.LOCAL_UPLOAD:
                return local_upload
            if capability == cap.FILE_QUERY:
                return file_query
            raise AssertionError(f"测试不应 require {capability}")

        target = SimpleNamespace(
            name="115",
            supports=lambda capability: capability in {
                transfer_module.CloudDriveCapability.RAPID_UPLOAD,
                transfer_module.CloudDriveCapability.LOCAL_UPLOAD,
                transfer_module.CloudDriveCapability.FILE_QUERY,
            },
            require=require,
        )

        def resolver(key):
            return target if key == "115" else self.source

        manager = CrossTransferTaskManager(
            resolver, download_path=self.tempdir.name,
            download_threads=5, max_concurrent=2, direct_stream=False,
            rapid_miss_action=rapid_miss_action,
        )
        return manager, upload_service

    def _wait_terminal(self, manager, task_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with manager._lock:
                task = manager._tasks.get(task_id)
                status = task["status"] if task else None
            if task and status not in manager.ACTIVE:
                return task
            time.sleep(0.05)
        self.fail("任务未在期限内进入终态")

    def test_flag_plumbed_through_create(self):
        upload = FakeRapidUploadService()
        manager, _ = self.make_manager(upload)
        item = make_file_item(with_checksums=False)
        task = manager.create_from_cloud_file(
            "quark", item, "115", "/", "movie.mkv", download_only=True,
        )
        self.assertTrue(task["download_only"])
        item2 = make_file_item(with_checksums=False, name="other.mkv")
        task2 = manager.create_from_cloud_file(
            "quark", item2, "115", "/", "other.mkv",
        )
        self.assertFalse(task2["download_only"])
        # 两个后台任务跑完再退出，否则清理临时目录时会撞上仍在写的线程。
        self._wait_terminal(manager, task["id"])
        self._wait_terminal(manager, task2["id"])

    def test_download_only_keeps_local_file(self):
        upload = FakeRapidUploadService()
        manager, _ = self.make_manager(upload)
        item = make_file_item(with_checksums=False)
        task = manager.create_from_cloud_file(
            "quark", item, "115", "/", "movie.mkv", download_only=True,
        )
        done = self._wait_terminal(manager, task["id"])
        self.assertEqual(done["status"], "success")
        self.assertEqual(done["method"], "download_only")
        local_path = done.get("result_local_path") or ""
        self.assertTrue(local_path, "应返回本地文件路径")
        self.assertTrue(Path(local_path).is_file())
        self.assertEqual(Path(local_path).read_bytes(), PAYLOAD)
        self.assertEqual(upload.upload_file_calls, [])
        self.assertEqual(len(self.source.download_calls), 1)
        # 任务结束不删缓存：文件归 MP 整理所有。1.12.1 起下载直接用目标名落盘。
        self.assertTrue(Path(self.tempdir.name, "movie.mkv").is_file())
        self.assertFalse(
            list(Path(self.tempdir.name).glob("cloud-transfer-*"))
        )

    def test_download_only_probe_hit_no_download(self):
        # 零流量探测只在 abort 模式跑：fallback 模式没有下载前探测，
        # 探测命中这条只在 abort 模式可测。
        upload = FakeRapidUploadService(probe_outcomes=[True])
        manager, _ = self.make_manager(upload, rapid_miss_action="abort")
        item = make_file_item()
        task = manager.create_from_cloud_file(
            "quark", item, "115", "/", "movie.mkv", download_only=True,
        )
        done = self._wait_terminal(manager, task["id"])
        self.assertEqual(done["status"], "success")
        self.assertEqual(done["method"], "rapid")
        self.assertEqual(self.source.download_calls, [])
        self.assertEqual(upload.upload_file_calls, [])

    def test_normal_task_still_uploads_and_cleans_cache(self):
        upload = FakeRapidUploadService(rapid_outcomes=[False])
        manager, _ = self.make_manager(upload)
        item = make_file_item(with_checksums=False)
        task = manager.create_from_cloud_file(
            "quark", item, "115", "/", "movie.mkv",
        )
        done = self._wait_terminal(manager, task["id"])
        self.assertEqual(done["status"], "success")
        self.assertEqual(done["method"], "fallback")
        self.assertEqual(len(upload.upload_file_calls), 1)
        # 普通任务成功后缓存照常删除（1.12.1 起缓存就是目标名文件本身）。
        self.assertFalse(Path(self.tempdir.name, "movie.mkv").exists())
        self.assertFalse(
            list(Path(self.tempdir.name).glob("cloud-transfer-*"))
        )


if __name__ == "__main__":
    unittest.main()
