"""1.10.7：失败原因透传——历史记录不再只剩一句通用话。

复现：2026-11-16 早上 5 条失败，页面上只看得到「未分类失败」/
「秒传未命中」，悬浮也只是「重试转存失败」「秒传未命中且普通
上传失败」；日志里两个时段（06:30–06:37 手动重试、02:33–02:43
凌晨同步）完全没有任何插件日志行。根因是原因在代码里被逐层盖掉：

- p115 upload_file 只返回 False（底层报错只写进日志行）；
- CrossDriveTransfer.transfer 返回 None 不带原因；
- _run 的 not_written 分支只抛一句通用的「秒传未命中且普通上传失败」；
- 重试只读记录上残留的旧原因，旧原因一空就只剩「重试转存失败」；
- 同步链路任务字典里没原因时，记录也跟着空（吞噬星空 S01E234）。

本版四处改动：
- drive/p115/upload.py：upload_file 把每次失败的原因留进 last_error。
- core/transfer.py：CrossDriveTransfer.transfer 失败时把原因记进
  self.last_error；_run 的 not_written 分支把底层原因拼进
  RuntimeError（任务 error → 历史记录失败原因）。
- handlers/sync/transfer_ops.py：任务里没原因时按失败阶段兜底。
- handlers/sync/retry.py：重试优先用本次尝试记下来的新原因。
"""

import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

# p115 files 用占位模块（upload 只需要类名做依赖注入）。
p115_files_module = register_module(
    "cloudsubscribex.drive.p115.files", package=True
)
p115_files_module.P115DirectoryReader = MagicMock()
p115_files_module.P115FileService = MagicMock()

transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
upload_module = load_module(
    "cloudsubscribex.drive.p115.upload",
    "plugins.v2/cloudsubscribex/drive/p115/upload.py",
)

CrossDriveTransfer = transfer_module.CrossDriveTransfer
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager
LocalRapidUploadAdapter = transfer_module.LocalRapidUploadAdapter
CloudFile = transfer_module.CloudFile

PAYLOAD = b"0123456789"
SHA1 = hashlib.sha1(PAYLOAD).hexdigest()
MD5 = hashlib.md5(PAYLOAD).hexdigest()
DRIVE_DETAIL = "115 本地文件上传失败：movie.mkv，115 open api 429"


def _read_source(*parts):
    with open(_plugin_dir(*parts), encoding="utf-8") as handle:
        return handle.read()


def _make_source_file(name="movie.mkv", size=len(PAYLOAD)):
    return CloudFile(
        id="fid1", name=name, is_directory=False, size=size,
        sha1=SHA1, md5=MD5,
    )


class _FailingUploadNamespace:
    """模拟带 last_error 的上传服务：返回 False 时把原因留下来。"""

    def __init__(self, detail=DRIVE_DETAIL):
        self.last_error = detail
        self.calls = []

    def upload_file(self, local_path, save_path, target_name="",
                    file_sha1="", progress_callback=None):
        self.calls.append(local_path)
        return False


class _RapidMissAdapter:
    """秒传适配器替身：探测与本地秒传一律未命中。"""

    algorithms = frozenset({"sha1"})

    def upload_by_hash(self, source_path, save_path, target_name,
                       algorithm, checksum, size, progress_callback=None):
        return SimpleNamespace(reused=False, file=None)


def _make_target(upload_service, files_service):
    def require(capability):
        cap = transfer_module.CloudDriveCapability
        if capability == cap.RAPID_UPLOAD:
            return _RapidMissAdapter()
        if capability == cap.LOCAL_UPLOAD:
            return upload_service
        if capability == cap.FILE_QUERY:
            return SimpleNamespace(find_file=files_service.find_file)
        raise AssertionError(f"测试不应 require {capability}")

    return SimpleNamespace(
        name="115",
        supports=lambda capability: capability in {
            transfer_module.CloudDriveCapability.RAPID_UPLOAD,
            transfer_module.CloudDriveCapability.LOCAL_UPLOAD,
            transfer_module.CloudDriveCapability.FILE_QUERY,
        },
        require=require,
    )


class CrossDriveTransferLastErrorTests(unittest.TestCase):
    def test_upload_false_carries_drive_detail(self):
        """上传返回 False 时，驱动的 last_error 要进 transfer.last_error。"""
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "movie.mkv"
            source.write_bytes(PAYLOAD)
            upload = _FailingUploadNamespace()
            target = _make_target(upload, MagicMock())
            transfer = CrossDriveTransfer()
            result = transfer.transfer(
                str(source), target, "/", "movie.mkv",
                "sha1", SHA1, fallback=True,
            )
            self.assertIsNone(result)
            self.assertEqual(transfer.last_error, DRIVE_DETAIL)

    def test_upload_false_without_detail_uses_fallback_text(self):
        """上传服务没给原因时，给一句兜底而不是空串。"""
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "movie.mkv"
            source.write_bytes(PAYLOAD)
            upload = SimpleNamespace(
                upload_file=lambda *args, **kwargs: False,
            )
            target = _make_target(upload, MagicMock())
            transfer = CrossDriveTransfer()
            result = transfer.transfer(
                str(source), target, "/", "movie.mkv",
                "sha1", SHA1, fallback=True,
            )
            self.assertIsNone(result)
            self.assertEqual(
                transfer.last_error, "普通上传失败（目标盘未返回原因）"
            )

    def test_abort_mode_miss_records_rapid_miss(self):
        """abort 模式秒传未命中：不进上传，原因记为秒传未命中。"""
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "movie.mkv"
            source.write_bytes(PAYLOAD)
            upload = _FailingUploadNamespace()
            target = _make_target(upload, MagicMock())
            transfer = CrossDriveTransfer()
            result = transfer.transfer(
                str(source), target, "/", "movie.mkv",
                "sha1", SHA1, fallback=False,
            )
            self.assertIsNone(result)
            self.assertEqual(upload.calls, [], "abort 不得真实上传")
            self.assertEqual(transfer.last_error, "秒传未命中")

    def test_upload_success_still_works(self):
        """last_error 的加入不得破坏成功路径。"""
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "movie.mkv"
            source.write_bytes(PAYLOAD)
            uploaded = CloudFile(
                id="n1", name="movie.mkv", is_directory=False,
                size=len(PAYLOAD), sha1=SHA1, md5=MD5,
            )
            files_service = MagicMock()
            files_service.find_file.return_value = uploaded
            upload = SimpleNamespace(
                upload_file=lambda *args, **kwargs: True,
            )
            target = _make_target(upload, files_service)
            transfer = CrossDriveTransfer()
            result = transfer.transfer(
                str(source), target, "/", "movie.mkv",
                "sha1", SHA1, fallback=True,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.method, "fallback")
            self.assertEqual(result.file, uploaded)
            self.assertEqual(transfer.last_error, "")


class FakeSourceProvider:
    """源盘替身：下载到本地。"""

    name = "夸克网盘"

    def supports(self, capability):
        return capability == transfer_module.CloudDriveCapability.FILE_DOWNLOAD

    def require(self, capability):
        return SimpleNamespace(
            download_file=self.download_file,
            resolve_download_link=self.resolve_download_link,
        )

    def download_file(self, file_item, local_path, progress_callback=None,
                      stop_requested=None, preserve_partial=True,
                      download_threads=5):
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PAYLOAD)
        return local_path

    def resolve_download_link(self, file_item):
        return "https://src.example.com/movie.mkv", {"user-agent": "ua"}


class FakeRapidUploadService:
    """秒传服务替身：探测/秒传一律未命中。"""

    rapid_requires_local_file = True

    def try_rapid_upload_by_hash(
            self, save_path, target_name, algorithm, checksum, size,
            read_range_bytes_or_hash=None,
    ):
        return False

    def try_rapid_upload(self, local_path, save_path, target_name,
                         algorithm, checksum, size):
        return False


class FallbackFailureMessageTests(unittest.TestCase):
    """真实 _run：fallback 回落上传失败时，任务 error 要带上底层原因。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.source = FakeSourceProvider()

    def _make_manager(self):
        files_service = MagicMock()
        files_service.find_file.return_value = None
        adapter = LocalRapidUploadAdapter(
            FakeRapidUploadService(), files_service, frozenset({"sha1"}),
        )
        local_upload = SimpleNamespace(
            upload_file=None, upload_from_link=None, upload_progressive=None,
        )

        def _fail_upload(local_path, save_path, target_name="",
                         file_sha1="", progress_callback=None):
            # 模拟 p115 服务的行为：先把原因留进 last_error 再返回 False。
            local_upload.last_error = DRIVE_DETAIL
            return False

        local_upload.upload_file = _fail_upload
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

        return CrossTransferTaskManager(
            resolver, download_path=self.tempdir.name,
            download_threads=5, max_concurrent=2, direct_stream=False,
            rapid_miss_action="fallback",
        )

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

    def test_fallback_upload_failure_keeps_drive_detail(self):
        manager = self._make_manager()
        file_item = _make_source_file()
        public = manager.create(
            source_key="quark", source_file=file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task = self._wait_terminal(manager, public["id"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("秒传未命中且普通上传失败", task["error"])
        self.assertIn("429", task["error"])
        self.assertIn("movie.mkv", task["error"])
        self.assertFalse(
            list(Path(self.tempdir.name).glob("cloud-transfer-*")),
            "失败后中继缓存必须删除（1.10.0 语义）",
        )


class P115UploadLastErrorTests(unittest.TestCase):
    """p115 upload_file：失败时 last_error 留下原因，成功时清掉。"""

    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "P115OSS_AVAILABLE": upload_module.P115OSS_AVAILABLE,
            "OSS2_AVAILABLE": upload_module.OSS2_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
        }
        upload_module.P115_AVAILABLE = True
        upload_module.P115OSS_AVAILABLE = False
        # 1.12.9 起 upload_file 默认走 oss2 自管分片；这里钉死回退路子
        # （p115oss），只测旧路子的 last_error 语义——分片级重试另有专测。
        upload_module.OSS2_AVAILABLE = False
        upload_module.check_response = MagicMock()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, upload_side_effect=None):
        owner = OwnerDelegator(SimpleNamespace(
            client=MagicMock(),
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        client = owner.client
        if upload_side_effect is not None:
            client.upload_file.side_effect = upload_side_effect
        else:
            client.upload_file.return_value = {}
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner), client

    def test_client_error_stashes_detail(self):
        service, _client = self._make_service(
            RuntimeError("115 open api 429"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "f.mkv"
            source.write_bytes(PAYLOAD)
            # 429 整文件退避是 5/10 秒真等，测试只断言 last_error，不等它。
            with patch.object(upload_module.time, "sleep"):
                result = service.upload_file(str(source), "/movies", "f.mkv")
        self.assertIs(result, False)
        self.assertIn("429", service.last_error)
        self.assertIn("f.mkv", service.last_error)

    def test_success_clears_last_error(self):
        service, _client = self._make_service(
            RuntimeError("115 open api 429"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "f.mkv"
            source.write_bytes(PAYLOAD)
            with patch.object(upload_module.time, "sleep"):
                self.assertIs(
                    service.upload_file(str(source), "/movies", "f.mkv"), False,
                )
            self.assertIn("429", service.last_error)
            service.client.upload_file.side_effect = None
            service.client.upload_file.return_value = {}
            self.assertIs(
                service.upload_file(str(source), "/movies", "f.mkv"), True,
            )
            self.assertEqual(service.last_error, "")


class FailureReasonWiringTests(unittest.TestCase):
    """重试与同步链路的源码接线：原因必须来自本次尝试、不得留空。"""

    def test_retry_prefers_fresh_failure_reason(self):
        text = _read_source("handlers", "sync", "retry.py")
        # Phase 5：_transfer_failure_reason 在 transfer_ops 组件上，
        # retry 经 owner.transfer_ops 调用。
        self.assertIn(
            "self._owner.transfer_ops._transfer_failure_reason(file_id)",
            text,
        )
        self.assertLess(
            text.index(
                "self._owner.transfer_ops._transfer_failure_reason(file_id)"
            ),
            text.index('"重试转存失败"'),
            "本次尝试的新原因必须排在通用兜底之前",
        )

    def test_sync_failure_reason_falls_back_to_phase(self):
        text = _read_source("handlers", "sync", "transfer_ops.py")
        self.assertIn("跨盘转存失败（阶段：", text)
        self.assertIn("self._remember_transfer_failure(file_item, failure_reason)", text)

    def test_run_error_message_includes_detail(self):
        text = _read_source("core", "transfer.py")
        self.assertIn("local_error", text)
        self.assertIn("秒传未命中且普通上传失败：{detail}", text)


if __name__ == "__main__":
    unittest.main()
