"""1.12.0 驱动层上传重传单测。

固定契约（凡人修仙传 S01E200 的教训：p115oss 分片上传没有重试，
服务端收到的分块 md5 与客户端算出的不一致、两次失败在不同分块，
一次失败就杀死整个任务）：

1. 第 1 次失败、第 2 次成功 → 返回 True，客户端调 2 次，SHA1 只算 1 次；
2. 3 次都失败 → 返回 False，客户端调 3 次，last_error 是拆包后的原错；
3. 第 1 次成功 → 客户端只调 1 次，不睡觉；
4. 客户端未初始化 → 返回 False，不调客户端、不睡觉；
5. 退避按 2 秒、4 秒指数递增（±20% 抖动，见统一重试策略表）。
"""

import os
import sys
import tempfile
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

p115_files_module = register_module(
    "cloudsubscribex.drive.p115.files", package=True
)
p115_files_module.P115DirectoryReader = MagicMock()
p115_files_module.P115FileService = MagicMock()

load_module(
    "cloudsubscribex.core.error_text",
    "plugins.v2/cloudsubscribex/core/error_text.py",
)
upload_module = load_module(
    "cloudsubscribex.drive.p115.upload",
    "plugins.v2/cloudsubscribex/drive/p115/upload.py",
)

from p115oss.upload import MultipartUploadAbort  # noqa: E402

FAKE_USER_KEY = "FAKEUSERKEY0123456789ABCDEF0123"
UPLOAD_PARAMS = {
    "pid": 3538356044075894093,
    "filename": "05.mkv",
    "filesha1": "8BC9718D179B4F83343A595713A631DC842BDAFA",
    "filesize": 3239498038,
    "user_id": 101991011,
    "user_key": FAKE_USER_KEY,
    "partsize": 16777216,
    "callback": {},
    "upload_id": "",
}


def _abort_with_cause(cause):
    try:
        raise MultipartUploadAbort(dict(UPLOAD_PARAMS)) from cause
    except MultipartUploadAbort as error:
        return error


_NO_CLIENT = object()


class P115UploadRetryTests(unittest.TestCase):
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
        # （p115oss），只测外层整文件重传契约——分片级重试另有专测。
        upload_module.OSS2_AVAILABLE = False
        upload_module.check_response = MagicMock()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, upload_side_effect, client=_NO_CLIENT):
        if client is _NO_CLIENT:
            client = MagicMock()
            client.upload_file.side_effect = upload_side_effect
        self._client = client
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def test_retry_once_then_succeeds(self):
        cause = OSError(5, "the server side failed to submit data")
        service = self._make_service([
            _abort_with_cause(cause),
            MagicMock(return_value={"ok": True}),
        ])
        sha1_spy = MagicMock(
            wraps=service._file_sha1, return_value="A" * 40
        )
        # _file_sha1 是 staticmethod，普通赋值会被 OwnerDelegator 委托
        # 到宿主上、桩不上服务实例；object.__setattr__ 才能桩住。
        object.__setattr__(service, "_file_sha1", sha1_spy)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertTrue(service.upload_file(str(source), "/"))
            # 整文件第 1 次失败后退避 2 秒 ±20% 抖动。
            mock_sleep.assert_called_once()
            self.assertAlmostEqual(mock_sleep.call_args[0][0], 2.0, delta=0.45)
        self.assertEqual(self._client.upload_file.call_count, 2)
        self.assertEqual(sha1_spy.call_count, 1)
        self.assertEqual(service.last_error, "")

    def test_three_failures_give_up(self):
        cause = OSError(5, "the server side failed to submit data")
        service = self._make_service(_abort_with_cause(cause))
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertFalse(service.upload_file(str(source), "/"))
            # 整文件退避 2 秒、4 秒指数递增，各 ±20% 抖动。
            delays = [call.args[0] for call in mock_sleep.call_args_list]
            self.assertEqual(len(delays), 2)
            self.assertAlmostEqual(delays[0], 2.0, delta=0.45)
            self.assertAlmostEqual(delays[1], 4.0, delta=0.85)
        self.assertEqual(self._client.upload_file.call_count, 3)
        self.assertIn("the server side failed", service.last_error)
        self.assertNotIn(FAKE_USER_KEY, service.last_error)

    def test_first_try_success_no_sleep(self):
        service = self._make_service(
            MagicMock(return_value={"ok": True})
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertTrue(service.upload_file(str(source), "/"))
            mock_sleep.assert_not_called()
        self.assertEqual(self._client.upload_file.call_count, 1)

    def test_client_not_initialized_no_retry(self):
        service = self._make_service(None, client=None)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertFalse(service.upload_file(str(source), "/"))
            mock_sleep.assert_not_called()
        self.assertIn("客户端未初始化", service.last_error)


if __name__ == "__main__":
    unittest.main()
