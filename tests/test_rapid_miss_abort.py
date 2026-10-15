"""1.6.45 跨盘转存「秒传未命中即中止」单测。

固定契约：

1. abort + 秒传命中：任务成功、method="rapid"、本地缓存删除、未发生真实上传；
2. abort + 未命中 + 源文件有 sha1：不下载、不真实上传、failed/aborted、
   abort_reason="rapid_miss"、缓存与目标盘无残留；
3. abort + 未命中 + 无校验和：允许下载，但不得真实上传，且缓存被删除；
4. fallback + 未命中：与 1.6.44 一致（回落上传、method="fallback"）；
5. 配置非法值 → 按 fallback 处理，不抛异常；
6. p115 try_rapid_upload_by_hash：探测只走 upload_file_init、≥1MB 无数据源返回
   None、未命中清理可能的上传会话残留；
7. _RemoteSha1RangeReader：按 115 二次校验范围读取源盘并返回 SHA1。
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    return os.path.join(plugins_v2_path, "cloudsubscribe", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())

# ---- cloudsubscribe 包层级 ----
core_pkg = _register_real_package("cloudsubscribe", _plugin_dir())
core_pkg = _register_real_package("cloudsubscribe.core", _plugin_dir("core"))
_register_real_package("cloudsubscribe.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribe.drive.p115", _plugin_dir("drive", "p115"))
_register_real_package("cloudsubscribe.utils", _plugin_dir("utils"))
core_pkg.OwnerDelegator = OwnerDelegator
register_mock(
    "cloudsubscribe.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

# p115 files 用占位模块（upload 只需要类名做依赖注入）
p115_files_module = register_module(
    "cloudsubscribe.drive.p115.files", package=True
)
p115_files_module.P115DirectoryReader = MagicMock()
p115_files_module.P115FileService = MagicMock()

cloud_module = load_module(
    "cloudsubscribe.core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py"
)
transfer_module = load_module(
    "cloudsubscribe.core.transfer", "plugins.v2/cloudsubscribe/core/transfer.py"
)
upload_module = load_module(
    "cloudsubscribe.drive.p115.upload",
    "plugins.v2/cloudsubscribe/drive/p115/upload.py",
)

CloudFile = cloud_module.CloudFile
LocalRapidUploadAdapter = transfer_module.LocalRapidUploadAdapter
RapidUploadResult = transfer_module.RapidUploadResult
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
        # probe_outcomes / rapid_outcomes：按调用顺序出队的 True/False/None，
        # 耗尽后重复最后一个值。
        self._probe_outcomes = list(probe_outcomes or [])
        self._rapid_outcomes = list(rapid_outcomes or [False])
        self.probe_calls = []
        self.rapid_calls = []
        self.upload_file_calls = []

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
            "read_range": read_range_bytes_or_hash,
        })
        return self._next(self._probe_outcomes, False)

    def try_rapid_upload(self, local_path, save_path, target_name,
                         algorithm, checksum, size):
        self.rapid_calls.append({
            "local_path": local_path, "save_path": save_path,
            "target_name": target_name, "algorithm": algorithm,
            "checksum": checksum, "size": size,
        })
        return self._next(self._rapid_outcomes, False)

    def upload_file(self, local_path, save_path, target_name="",
                    file_sha1="", progress_callback=None):
        self.upload_file_calls.append({
            "local_path": local_path, "save_path": save_path,
            "target_name": target_name,
        })
        if progress_callback:
            progress_callback(len(PAYLOAD), len(PAYLOAD))
        return True


class FakeSourceProvider:
    """源盘替身：下载到本地 + 直链接口。"""

    name = "夸克网盘"

    def __init__(self):
        self.download_calls = []
        self.link_calls = []

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
        self.link_calls.append(1)
        return "https://src.example.com/movie.mkv", {"user-agent": "ua"}


class RapidMissAbortTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.source = FakeSourceProvider()

    def make_manager(self, upload_service, rapid_miss_action="abort",
                     files_service=None):
        files_service = files_service or MagicMock()
        files_service.find_file.return_value = CloudFile(
            id="n1", name="movie.mkv", is_directory=False, size=len(PAYLOAD),
            sha1=SHA1, md5=MD5,
        )
        adapter = LocalRapidUploadAdapter(
            upload_service, files_service, frozenset({"sha1"}),
        )
        local_upload = SimpleNamespace(
            upload_file=upload_service.upload_file,
            upload_from_link=None, upload_progressive=None,
        )
        file_query = SimpleNamespace(
            find_file=lambda *_args: files_service.find_file.return_value,
        )

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
        return manager, files_service

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

    def _cache_triples(self, manager, file_item):
        cache_path = manager._cache_path(manager._cache_key("quark", file_item))
        return (
            cache_path,
            manager._cache_meta_path(cache_path),
            manager._cache_parts_path(cache_path),
        )

    def _seed_complete_cache(self, manager, file_item):
        cache_path = manager._cache_path(manager._cache_key("quark", file_item))
        cache_path.write_bytes(PAYLOAD)
        manager._mark_complete_cache(
            cache_path, manager._cache_key("quark", file_item), file_item,
        )
        return cache_path

    def _no_cache_left(self, manager):
        return not list(Path(self.tempdir.name).glob("cloud-transfer-*"))

    def test_abort_probe_hit_succeeds_and_cleans_cache(self):
        """验收1：abort + 命中 → 成功 method=rapid、缓存删除、无真实上传。"""
        upload = FakeRapidUploadService(probe_outcomes=[True])
        manager, files_service = self.make_manager(upload)
        file_item = make_file_item()
        cache_path = self._seed_complete_cache(manager, file_item)
        meta_path = manager._cache_meta_path(cache_path)

        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "success")
        self.assertEqual(task["method"], "rapid")
        self.assertEqual(len(upload.probe_calls), 1)
        self.assertEqual(upload.probe_calls[0]["checksum"], SHA1.upper())
        self.assertEqual(upload.upload_file_calls, [], "命中秒传不得真实上传")
        self.assertEqual(self.source.download_calls, [], "命中秒传不得本地下载")
        self.assertFalse(cache_path.exists(), "命中后缓存必须删除")
        self.assertFalse(meta_path.exists(), "命中后 meta 必须删除")
        files_service.find_file.assert_called()

    def test_abort_probe_miss_with_sha1_aborts_without_download_or_upload(self):
        """验收2：abort + 未命中 + 有 sha1 → 不下载、不上传、清理、aborted。"""
        upload = FakeRapidUploadService(probe_outcomes=[False])
        manager, files_service = self.make_manager(upload)
        file_item = make_file_item()

        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["phase"], "aborted")
        self.assertEqual(task["abort_reason"], "rapid_miss")
        self.assertIn("未命中秒传，已按设置中止", task["message"])
        self.assertEqual(task["error"], task["message"])
        self.assertEqual(self.source.download_calls, [], "有 sha1 时不得本地下载")
        self.assertEqual(upload.upload_file_calls, [], "不得调用真实上传")
        self.assertEqual(upload.rapid_calls, [], "不得进入带本地文件的秒传")
        files_service.find_file.assert_not_called()
        self.assertTrue(
            self._no_cache_left(manager), "缓存目录不得残留本任务文件"
        )

    def test_abort_without_checksum_downloads_but_never_uploads(self):
        """验收3：abort + 无校验和 → 允许下载，但不得真实上传且缓存被删除。"""
        upload = FakeRapidUploadService(rapid_outcomes=[False])
        manager, _files_service = self.make_manager(upload)
        file_item = make_file_item(with_checksums=False)

        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["phase"], "aborted")
        self.assertEqual(task["abort_reason"], "rapid_miss")
        self.assertEqual(len(self.source.download_calls), 1, "无校验和时允许下载")
        self.assertEqual(len(upload.rapid_calls), 1, "允许下载后探测")
        self.assertEqual(upload.upload_file_calls, [], "不得调用真实上传")
        self.assertEqual(upload.probe_calls, [], "无校验和时不走零流量探测")
        self.assertTrue(self._no_cache_left(manager), "中止后缓存必须删除")

    def test_fallback_miss_falls_back_to_upload(self):
        """验收4：fallback（默认）+ 未命中 → 回落上传、method=fallback。"""
        upload = FakeRapidUploadService(rapid_outcomes=[False])
        manager, _files_service = self.make_manager(
            upload, rapid_miss_action="fallback",
        )
        file_item = make_file_item()

        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "success")
        self.assertEqual(task["method"], "fallback")
        self.assertEqual(upload.probe_calls, [], "fallback 模式不做零流量探测")
        self.assertEqual(len(upload.upload_file_calls), 1, "未命中必须回落真实上传")
        self.assertEqual(len(self.source.download_calls), 1)
        self.assertEqual(task.get("abort_reason", ""), "")

    def test_fallback_miss_keeps_cache_on_failure(self):
        """验收4：fallback 失败时缓存保留策略不变（完整缓存供重试）。"""
        upload = FakeRapidUploadService(rapid_outcomes=[False])
        upload.upload_file = MagicMock(return_value=False)
        manager, _files_service = self.make_manager(
            upload, rapid_miss_action="fallback",
        )
        file_item = make_file_item()
        cache_path, meta_path, _parts = self._cache_triples(manager, file_item)

        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["phase"], "failed")
        self.assertEqual(task.get("abort_reason", ""), "")
        self.assertTrue(
            cache_path.is_file() and meta_path.is_file(),
            "fallback 失败时完整缓存必须保留（1.6.44 语义）",
        )

    def test_abort_complete_cache_deleted_even_on_miss_after_download(self):
        """abort 下载后未命中：完整缓存也必须删除（与 fallback 保留语义不同）。"""
        upload = FakeRapidUploadService(rapid_outcomes=[False])
        manager, _files_service = self.make_manager(upload)
        file_item = make_file_item(with_checksums=False)
        cache_path, meta_path, _parts = self._cache_triples(manager, file_item)

        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["phase"], "aborted")
        self.assertFalse(cache_path.exists(), "abort 必须删除完整缓存 .bin")
        self.assertFalse(meta_path.exists(), "abort 必须删除 .meta.json")

    def test_abort_multi_file_isolation(self):
        """多文件任务：单个文件中止不影响其它文件的转存。"""
        upload = FakeRapidUploadService(
            probe_outcomes=[False, True], rapid_outcomes=[False],
        )
        manager, _files_service = self.make_manager(upload)
        first = make_file_item(name="a.mkv")
        second = make_file_item(name="b.mkv")

        public_a = manager.create(
            source_key="quark", source_file=first,
            target_key="115", save_path="/", target_name="a.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        public_b = manager.create(
            source_key="quark", source_file=second,
            target_key="115", save_path="/", target_name="b.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task_a = self._wait_terminal(manager, public_a["id"])
        task_b = self._wait_terminal(manager, public_b["id"])

        self.assertEqual(task_a["status"], "failed")
        self.assertEqual(task_a["abort_reason"], "rapid_miss")
        self.assertEqual(task_b["status"], "success")
        self.assertEqual(task_b["method"], "rapid")

    def test_illegal_rapid_miss_action_falls_back(self):
        """验收5：非法配置值 → 按 fallback 处理，不抛异常。"""
        manager = CrossTransferTaskManager(
            lambda _key: MagicMock(), download_path=self.tempdir.name,
            rapid_miss_action="bogus",
        )
        self.assertEqual(manager._rapid_miss_action, "fallback")
        manager = CrossTransferTaskManager(
            lambda _key: MagicMock(), download_path=self.tempdir.name,
            rapid_miss_action="",
        )
        self.assertEqual(manager._rapid_miss_action, "fallback")
        manager = CrossTransferTaskManager(
            lambda _key: MagicMock(), download_path=self.tempdir.name,
            rapid_miss_action="abort",
        )
        self.assertEqual(manager._rapid_miss_action, "abort")


class P115TryRapidUploadByHashTests(unittest.TestCase):
    """p115 无本地文件秒传探测（只走 upload_file_init）。"""

    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "P115OSS_AVAILABLE": upload_module.P115OSS_AVAILABLE,
        }
        upload_module.P115_AVAILABLE = True
        upload_module.P115OSS_AVAILABLE = False
        upload_module.check_response = MagicMock()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def make_service(self, init_response=None, init_side_effect=None):
        owner = OwnerDelegator(SimpleNamespace(
            client=MagicMock(),
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        client = owner.client
        if init_side_effect:
            client.upload_file_init.side_effect = init_side_effect
        else:
            client.upload_file_init.return_value = init_response or {}
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = SimpleNamespace(
            checked=True, directory_id=42,
        )
        service = upload_module.P115UploadService(owner)
        return service, client

    def test_small_file_hit_zero_traffic(self):
        service, client = self.make_service({"reuse": True, "data": {}})
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, len(PAYLOAD),
        )
        self.assertIs(result, True)
        client.upload_file_init.assert_called_once()
        kwargs = client.upload_file_init.call_args.kwargs
        self.assertEqual(kwargs["filesha1"], SHA1.upper())
        self.assertEqual(kwargs["filesize"], len(PAYLOAD))
        self.assertIsNone(kwargs["read_range_bytes_or_hash"])

    def test_small_file_miss_returns_false(self):
        service, _client = self.make_service({"reuse": False, "data": {}})
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, len(PAYLOAD),
        )
        self.assertIs(result, False)

    def test_large_file_without_read_range_returns_none(self):
        service, client = self.make_service({"reuse": False, "data": {}})
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, 2 * 1024 * 1024,
        )
        self.assertIsNone(result, "≥1MB 无二次校验数据源必须返回无法探测")
        client.upload_file_init.assert_not_called()

    def test_large_file_with_read_range_probes(self):
        seen = {}

        def init(filename, filesha1, filesize, dirname="", **kwargs):
            seen["read_range"] = kwargs.get("read_range_bytes_or_hash")
            return {"reuse": True, "data": {}}

        service, _client = self.make_service(init_side_effect=init)
        callback = MagicMock(return_value="A" * 40)
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, 2 * 1024 * 1024,
            read_range_bytes_or_hash=callback,
        )
        self.assertIs(result, True)
        self.assertIs(seen["read_range"], callback)

    def test_client_error_returns_none(self):
        service, _client = self.make_service(
            init_side_effect=RuntimeError("boom"),
        )
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, len(PAYLOAD),
        )
        self.assertIsNone(result, "探测异常必须按无法判定处理，不误伤任务")

    def test_miss_cancels_residue_session(self):
        upload_module.P115OSS_AVAILABLE = True
        upload_module.oss_multipart_upload_cancel = MagicMock()
        service, _client = self.make_service({
            "reuse": False,
            "data": {"upload_id": "u-1", "url": "https://oss.example/o"},
        })
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, len(PAYLOAD),
        )
        self.assertIs(result, False)
        upload_module.oss_multipart_upload_cancel.assert_called_once_with(
            "https://oss.example/o", "u-1",
        )

    def test_miss_without_residue_skips_cancel(self):
        upload_module.P115OSS_AVAILABLE = True
        upload_module.oss_multipart_upload_cancel = MagicMock()
        service, _client = self.make_service({"reuse": False, "data": {}})
        result = service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, len(PAYLOAD),
        )
        self.assertIs(result, False)
        upload_module.oss_multipart_upload_cancel.assert_not_called()

    def test_non_sha1_or_missing_inputs_return_none(self):
        service, client = self.make_service({"reuse": True, "data": {}})
        self.assertIsNone(service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "md5", MD5, len(PAYLOAD),
        ))
        self.assertIsNone(service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", "", len(PAYLOAD),
        ))
        self.assertIsNone(service.try_rapid_upload_by_hash(
            "/movies", "f.mkv", "sha1", SHA1, 0,
        ))
        client.upload_file_init.assert_not_called()


class RemoteSha1RangeReaderTests(unittest.TestCase):
    """_RemoteSha1RangeReader：按 115 二次校验范围读取并返回 SHA1。"""

    def _response(self, status_code, content):
        return SimpleNamespace(status_code=status_code, content=content)

    def test_reads_range_and_returns_sha1(self):
        payload = b"0123456789"
        expected = hashlib.sha1(payload[2:5]).hexdigest().upper()
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            return self._response(206, payload[2:5])

        reader = transfer_module._RemoteSha1RangeReader(
            "https://src.example/f", {"user-agent": "ua"}, len(payload),
        )
        with patch.object(transfer_module.requests, "get", side_effect=fake_get):
            result = reader.read_range_sha1("2-4")
        self.assertEqual(result, expected)
        self.assertEqual(captured["headers"].get("Range"), "bytes=2-4")

    def test_non_206_raises(self):
        reader = transfer_module._RemoteSha1RangeReader(
            "https://src.example/f", {}, 10,
        )
        with patch.object(
            transfer_module.requests, "get",
            return_value=self._response(200, b"x" * 3),
        ):
            with self.assertRaises(IOError):
                reader.read_range_sha1("2-4")

    def test_stop_requested_raises_interrupted(self):
        reader = transfer_module._RemoteSha1RangeReader(
            "https://src.example/f", {}, 10, stop_requested=lambda: True,
        )
        with self.assertRaises(InterruptedError):
            reader.read_range_sha1("2-4")

    def test_invalid_range_rejected(self):
        reader = transfer_module._RemoteSha1RangeReader(
            "https://src.example/f", {}, 10,
        )
        with self.assertRaises(ValueError):
            reader.read_range_sha1("5-2")
        with self.assertRaises(ValueError):
            reader.read_range_sha1("abc")


if __name__ == "__main__":
    unittest.main()
