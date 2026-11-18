"""1.12.13 oss2 失败回落 p115oss 单测。

固定契约（修“只有秒传成功、分片上传全失败”，代理已排除）：

1. oss2 分片上传失败时，回落 p115oss 旧路子继续传，成功则整个
   upload_file 返回 True；回落前把 oss2 的原错记下来；
2. oss2 成功时不走 p115oss（client.upload_file 不被调用）；
3. oss2 失败且 p115oss 不可用（P115OSS_AVAILABLE=False）时，
   上传失败（返回 False），不静默成功；
4. 代理恢复给 oss2：配了上传代理时 _oss2_proxy_dict 返回该代理
   （1.12.12 的直连改动是误诊，已撤销；cd2 走同一代理正常）。
"""

import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

PART_SIZE = 4
CONTENT = b"0123456789"  # 10 字节 → 3 片：4 / 4 / 2
EXPECTED_SHA1 = hashlib.sha1(CONTENT).hexdigest().upper()
PROXY_URL = "http://mihomo:7890"


def _token_response(expiration):
    return {
        "AccessKeyId": "AKID",
        "AccessKeySecret": "AKSECRET",
        "SecurityToken": "TOKEN",
        "Expiration": expiration,
    }


def _init_response(reuse=False):
    return {
        "state": True,
        "reuse": reuse,
        "data": {
            "bucket": "bucket-115",
            "object": "object-key",
            "callback": {"callback": "cb", "callback_var": "var"},
        },
    }


class _FailingBucket:
    """oss2 Bucket 桩：分片上传直接抛错，模拟线上 oss2 全失败。"""

    def __init__(self, *args, **kwargs):
        self.construct_kwargs = kwargs
        self.uploaded_parts = []

    def init_multipart_upload(self, key, params=None):
        return SimpleNamespace(upload_id="UPLOAD-ID-1")

    def upload_part(self, key, upload_id, part_number, data, **kwargs):
        raise RuntimeError("oss2 分片上传失败（模拟）")

    def abort_multipart_upload(self, key, upload_id):
        pass

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        return SimpleNamespace(status=200)


class _OkBucket:
    """oss2 Bucket 桩：正常传完 3 片。"""

    def __init__(self, *args, **kwargs):
        self.construct_kwargs = kwargs
        self.uploaded_parts = []
        self.completed = []

    def init_multipart_upload(self, key, params=None):
        return SimpleNamespace(upload_id="UPLOAD-ID-1")

    def upload_part(self, key, upload_id, part_number, data, **kwargs):
        content = data.read() if hasattr(data, "read") else b""
        self.uploaded_parts.append((part_number, content))
        return SimpleNamespace(etag=f"etag-{part_number}")

    def abort_multipart_upload(self, key, upload_id):
        pass

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.completed.append((key, upload_id, list(parts), dict(headers or {})))
        return SimpleNamespace(status=200)


class Oss2FallbackTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "OSS2_AVAILABLE": upload_module.OSS2_AVAILABLE,
            "P115OSS_AVAILABLE": upload_module.P115OSS_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
            "Oss2Bucket": upload_module.Oss2Bucket,
            "Oss2StsAuth": upload_module.Oss2StsAuth,
            "oss2_determine_part_size": upload_module.oss2_determine_part_size,
        }
        upload_module.P115_AVAILABLE = True
        upload_module.OSS2_AVAILABLE = True
        upload_module.P115OSS_AVAILABLE = True
        upload_module.check_response = MagicMock()
        upload_module.oss2_determine_part_size = MagicMock(
            return_value=PART_SIZE
        )
        upload_module.Oss2StsAuth = MagicMock(return_value=MagicMock())
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, bucket_cls):
        client = MagicMock()
        far_future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        client.upload_file_init.side_effect = (
            lambda *args, **kwargs: _init_response()
        )
        client.upload_gettoken.side_effect = (
            lambda *args, **kwargs: _token_response(far_future)
        )
        # p115oss 旧路子：client.upload_file 成功返回。
        client.upload_file.side_effect = lambda *args, **kwargs: {"state": True}
        self._client = client
        self._bucket = bucket_cls()
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
            # 本文件钉顺序引擎：并发度 0，不走并发分片分支；
            # 并发分片另有 test_upload_concurrency_1_18_0.py 专测。
            _cross_transfer_upload_concurrency=0,
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
            _upload_proxies={"http": PROXY_URL, "https": PROXY_URL},
            _cross_transfer_upload_proxy=PROXY_URL,
        ))
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def _write_source(self, tempdir):
        source = Path(tempdir) / "05.mkv"
        source.write_bytes(CONTENT)
        return source

    def test_oss2_failure_falls_back_to_p115oss(self):
        """oss2 分片失败 → 回落 p115oss 成功，整个上传返回 True。"""
        service = self._make_service(_FailingBucket)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        self._client.upload_file.assert_called_once()
        self.assertEqual(self._bucket.uploaded_parts, [])

    def test_oss2_success_does_not_call_p115oss(self):
        """oss2 成功时不走 p115oss 旧路子。"""
        service = self._make_service(_OkBucket)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        self._client.upload_file.assert_not_called()
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )

    def test_oss2_failure_without_p115oss_fails(self):
        """oss2 失败且 p115oss 不可用时，上传失败（不静默成功）。"""
        upload_module.P115OSS_AVAILABLE = False
        service = self._make_service(_FailingBucket)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertFalse(service.upload_file(str(source), "/"))
        self._client.upload_file.assert_not_called()

    def test_oss2_proxy_dict_uses_configured_proxy(self):
        """代理恢复给 oss2：配了上传代理时返回该代理（撤销 1.12.12 直连）。"""
        service = self._make_service(_OkBucket)
        self.assertEqual(
            service._oss2_proxy_dict(),
            {"http": PROXY_URL, "https": PROXY_URL},
        )


if __name__ == "__main__":
    unittest.main()
