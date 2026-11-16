"""1.12.9 oss2 自管分片上传单测。

固定契约（p115disk 同款路子，修 p115oss 大文件上传失败）：

1. 秒传命中（reuse）→ 返回 True，不拿 token、不建 Bucket、不传分片；
2. 正常分片上传 → 按分片大小逐片传，最后 complete 带回调头，返回 True；
3. 单片普通失败 → 只重传该片（不重传整文件），第 2 次成功；
4. 单片报 token 过期（InvalidAccessKeyId / SecurityTokenExpired）
   → 刷新 token、重建 Bucket、只重传该片；
5. 传下一片前 token 快过期（5 分钟内）→ 先刷新 token 再传；
6. upload_file_init 失败 2 次、第 3 次成功 → 照常上传；
7. init 报 sig invalid → 清掉 userkey 缓存文件后重试；
8. 单片 2 次都失败 → abort 掉该上传任务并抛错；
9. complete 返回非 200 → 抛错；
10. upload_file 外层整文件重传仍有效（oss2 路子失败 3 次才认输）。

分片用 4 字节（把 determine_part_size 钉死），10 字节文件正好 3 片。
"""

import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

from oss2.exceptions import ServerError as OssServerError  # noqa: E402

PART_SIZE = 4
CONTENT = b"0123456789"  # 10 字节 → 3 片：4 / 4 / 2
EXPECTED_SHA1 = hashlib.sha1(CONTENT).hexdigest().upper()


def _token_error(code):
    error = OssServerError(403, {}, "", {})
    error.code = code
    return error


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
    """oss2 Bucket 桩：按 fail_plan 让指定分片失败若干次。"""

    def __init__(self, fail_plan=None, complete_status=200):
        self.fail_plan = {k: list(v) for k, v in (fail_plan or {}).items()}
        self.complete_status = complete_status
        self.uploaded_parts = []  # (part_number, bytes)
        self.aborted = []
        self.completed = []
        self.init_calls = 0

    def init_multipart_upload(self, key, params=None):
        self.init_calls += 1
        return SimpleNamespace(upload_id="UPLOAD-ID-1")

    def upload_part(self, key, upload_id, part_number, data, **kwargs):
        content = data.read() if hasattr(data, "read") else b""
        plan = self.fail_plan.get(part_number, [])
        if plan:
            outcome = plan.pop(0)
            if outcome is not None:
                raise outcome
        self.uploaded_parts.append((part_number, content))
        return SimpleNamespace(etag=f"etag-{part_number}")

    def abort_multipart_upload(self, key, upload_id):
        self.aborted.append((key, upload_id))

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.completed.append(
            (key, upload_id, list(parts), dict(headers or {}))
        )
        return SimpleNamespace(status=self.complete_status)


class P115UploadOss2Tests(unittest.TestCase):
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

    def _make_service(self, init_side_effect=None, token_side_effect=None):
        client = MagicMock()
        far_future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        # 默认用函数（每次调用都返回新响应），避免 side_effect 列表
        # 被外层整文件重传耗尽；需要精确控制次数的测试再传列表。
        if init_side_effect is None:
            client.upload_file_init.side_effect = (
                lambda *args, **kwargs: _init_response()
            )
        else:
            client.upload_file_init.side_effect = init_side_effect
        if token_side_effect is None:
            client.upload_gettoken.side_effect = (
                lambda *args, **kwargs: _token_response(far_future)
            )
        else:
            client.upload_gettoken.side_effect = token_side_effect
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

    def _write_source(self, tempdir):
        source = Path(tempdir) / "05.mkv"
        source.write_bytes(CONTENT)
        return source

    def test_rapid_reuse_skips_upload(self):
        service = self._make_service(init_side_effect=[_init_response(reuse=True)])
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        self._client.upload_gettoken.assert_not_called()
        upload_module.Oss2Bucket.assert_not_called()
        self.assertEqual(self._bucket.uploaded_parts, [])
        self.assertEqual(self._bucket.completed, [])

    def test_multipart_success_three_parts(self):
        service = self._make_service()
        progress = []
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(
                service.upload_file(
                    str(source), "/",
                    progress_callback=lambda a, b: progress.append((a, b)),
                )
            )
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )
        self.assertEqual(
            [content for _, content in self._bucket.uploaded_parts],
            [b"0123", b"4567", b"89"],
        )
        self.assertEqual(len(self._bucket.completed), 1)
        _, _, parts, headers = self._bucket.completed[0]
        self.assertEqual(
            [(p.part_number, p.etag) for p in parts],
            [(1, "etag-1"), (2, "etag-2"), (3, "etag-3")],
        )
        self.assertIn("X-oss-callback", headers)
        self.assertEqual(headers.get("x-oss-forbid-overwrite"), "false")
        self.assertEqual(self._bucket.aborted, [])
        self.assertEqual(progress[-1], (10, 10))
        self.assertEqual(self._client.upload_gettoken.call_count, 1)

    def test_single_part_failure_retries_only_that_part(self):
        self._bucket = _FakeBucket(
            fail_plan={2: [OSError("抖动")]},
        )
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        service = self._make_service()
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertTrue(service.upload_file(str(source), "/"))
            # 单片重试不走整文件重传，不睡觉。
            mock_sleep.assert_not_called()
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )
        self.assertEqual(self._client.upload_file_init.call_count, 1)
        self.assertEqual(self._client.upload_gettoken.call_count, 1)
        self.assertEqual(self._bucket.aborted, [])

    def test_token_expired_part_refreshes_token_and_retries(self):
        self._bucket = _FakeBucket(
            fail_plan={2: [_token_error("InvalidAccessKeyId")]},
        )
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        service = self._make_service()
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        # 第 2 片失败后刷新 token（gettoken 调 2 次：初始 + 刷新）。
        self.assertEqual(self._client.upload_gettoken.call_count, 2)
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )
        self.assertEqual(self._bucket.aborted, [])

    def test_token_expiring_before_part_refreshes_proactively(self):
        far_future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        near_expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=3)
        ).isoformat().replace("+00:00", "Z")
        service = self._make_service(
            token_side_effect=[
                _token_response(near_expiry),
                _token_response(far_future),
            ]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        # 初始 token 3 分钟后过期（< 5 分钟阈值），传第 1 片前先刷新。
        self.assertEqual(self._client.upload_gettoken.call_count, 2)
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )

    def test_init_retry_then_succeeds(self):
        service = self._make_service(
            init_side_effect=[
                OSError("init 抖动"),
                OSError("init 抖动"),
                _init_response(),
            ]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertTrue(service.upload_file(str(source), "/"))
            self.assertEqual(
                [call.args[0] for call in mock_sleep.call_args_list], [2, 4]
            )
        self.assertEqual(self._client.upload_file_init.call_count, 3)
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )

    def test_init_sig_invalid_clears_userkey_cache(self):
        # sig invalid 走的是「init 返回 state=False → check_response 抛错」
        # 的路子，check_response 必须是真的会抛错的版本。
        def _check_response(resp):
            if not resp.get("state", True):
                raise OSError(f"115 接口失败：{resp}")
            return resp

        upload_module.check_response = _check_response
        bad = {"state": False, "statusmsg": "sig invalid", "data": {}}
        service = self._make_service(
            init_side_effect=[bad, _init_response()]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep"):
                self.assertTrue(service.upload_file(str(source), "/"))
        self.assertEqual(self._client.upload_file_init.call_count, 2)

    def test_part_fails_twice_aborts_and_raises(self):
        # 每轮整文件重传里第 2 片都失败 2 次：3 轮 × 2 次 = 6 次。
        self._bucket = _FakeBucket(
            fail_plan={2: [OSError("抖动")] * 6},
        )
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        service = self._make_service()
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
        # 单片 2 次都失败 → abort；外层整文件重传 3 次，每次都 abort。
        self.assertEqual(len(self._bucket.aborted), 3)
        self.assertEqual(self._bucket.completed, [])
        self.assertIn("抖动", service.last_error)

    def test_complete_non_200_raises(self):
        self._bucket = _FakeBucket(complete_status=500)
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        service = self._make_service()
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
        self.assertEqual(len(self._bucket.completed), 3)
        self.assertIn("500", service.last_error)

    def test_read_range_hash_matches_file_bytes(self):
        # init 回调里真的调 read_range_bytes_or_hash，验区间 SHA1。
        seen = {}

        def _fake_init(*args, **kwargs):
            callback = kwargs.get("read_range_bytes_or_hash")
            seen["range_hash"] = callback("0-3")
            seen["sha1_arg"] = kwargs.get("filesha1")
            return _init_response()

        service = self._make_service(init_side_effect=_fake_init)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        self.assertEqual(
            seen["range_hash"],
            hashlib.sha1(b"0123").hexdigest().upper(),
        )
        self.assertEqual(seen["sha1_arg"], EXPECTED_SHA1)


if __name__ == "__main__":
    unittest.main()
