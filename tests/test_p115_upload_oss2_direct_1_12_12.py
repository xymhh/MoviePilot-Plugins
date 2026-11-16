"""1.12.12 oss2 数据面直连单测。

固定契约（修“只有秒传成功、分片上传全失败”）：

1. 插件配了上传代理（mihomo:7890）时，oss2 数据面也不走代理：
   `_oss2_proxy_dict()` 恒返回空字典；
2. 分片上传建 Bucket 时传给 oss2 的 proxies 是空字典（直连），
   与 p115disk 原版一致；秒传只调 p115 API，本来就不走 oss2，
   不受此改动影响。

根因：1.12.9 把插件的上传代理传给 oss2 Bucket，分片 PUT 全走
mihomo 代理失败；秒传只调 upload_file_init（p115 客户端的代理
注入实际无效、走直连）所以成功，cd2 直连也成功。
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


class _FakeBucket:
    """oss2 Bucket 桩：记录构造时收到的 proxies。"""

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


class Oss2DirectConnectionTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "OSS2_AVAILABLE": upload_module.OSS2_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
            "Oss2Bucket": upload_module.Oss2Bucket,
            "Oss2StsAuth": upload_module.Oss2StsAuth,
            "oss2_determine_part_size": upload_module.oss2_determine_part_size,
        }
        upload_module.P115_AVAILABLE = True
        upload_module.OSS2_AVAILABLE = True
        upload_module.check_response = MagicMock()
        upload_module.oss2_determine_part_size = MagicMock(
            return_value=PART_SIZE
        )
        self._bucket = _FakeBucket()
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        upload_module.Oss2StsAuth = MagicMock(return_value=MagicMock())
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self):
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
        self._client = client
        # 代理配在 owner 上（manager 的 _upload_proxies + 插件的
        # _cross_transfer_upload_proxy 两种来源都配上）。
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
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

    def test_oss2_proxy_dict_always_empty_despite_configured_proxy(self):
        """配了 mihomo 代理时，oss2 数据面仍返回空代理字典（直连）。"""
        service = self._make_service()
        self.assertEqual(service._oss2_proxy_dict(), {})

    def test_multipart_bucket_built_without_proxy(self):
        """分片上传建 Bucket 时 proxies 为空字典，不把 mihomo 代理传给 oss2。"""
        service = self._make_service()
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(CONTENT)
            self.assertTrue(service.upload_file(str(source), "/"))
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )
        self.assertEqual(len(self._bucket.completed), 1)
        upload_module.Oss2Bucket.assert_called_once()
        _, kwargs = upload_module.Oss2Bucket.call_args
        self.assertEqual(kwargs.get("proxies"), {})

    def test_rapid_reuse_still_skips_upload(self):
        """直连改动不影响秒传：reuse 命中仍直接成功，不建 Bucket。"""
        service = self._make_service()
        self._client.upload_file_init.side_effect = [
            _init_response(reuse=True)
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(CONTENT)
            self.assertTrue(service.upload_file(str(source), "/"))
        upload_module.Oss2Bucket.assert_not_called()
        self.assertEqual(self._bucket.uploaded_parts, [])


if __name__ == "__main__":
    unittest.main()
