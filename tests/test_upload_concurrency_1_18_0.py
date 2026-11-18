"""1.18.0 Phase 3b-9：分片并发参数化 + 本地 oss2 并发分片单测。

覆盖：
1. 默认并发度 2（配置表/插件属性/驱动常量三处一致），上限 16，
   0 回退顺序；
2. 本地 oss2 上传：并发度 > 1 且多于一片时多 worker 并行 PUT，
   etag 按分片号排序后 complete；
3. 并发度 0/1 或单片文件时走顺序循环（单线程）；
4. 并发下 token 过期：单 worker 加锁刷新后重传该片。
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
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
load_module(
    "cloudsubscribex.core.failure",
    "plugins.v2/cloudsubscribex/core/failure.py",
)
# 先加载真实 drive.common：upload.py 的数据面兜底限流器在函数内
# 现取 DriveRateLimiter，全量测试里桩过的 common 会让它退回空操作。
load_module(
    "cloudsubscribex.drive.common",
    "plugins.v2/cloudsubscribex/drive/common.py",
)
upload_module = load_module(
    "cloudsubscribex.drive.p115.upload",
    "plugins.v2/cloudsubscribex/drive/p115/upload.py",
)

from oss2.exceptions import ServerError as OssServerError  # noqa: E402

CONTENT = b"0123456789"  # 10 字节；分片 4 字节 → 3 片
PART_SIZE = 4


def _token_response(expiration):
    return {
        "state": True,
        "data": {
            "AccessKeyId": "AKID",
            "AccessKeySecret": "AKSECRET",
            "SecurityToken": "TOKEN",
            "Expiration": expiration,
        },
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


def _token_error(code):
    return OssServerError(
        403, {}, "", {"Code": code, "Message": "token expired"}
    )


class _FakeBucket:
    """oss2 Bucket 桩：记每片由哪个线程 PUT（并发断言用）。"""

    def __init__(self, fail_plan=None, complete_status=200, part_delay=0.0):
        self.fail_plan = {k: list(v) for k, v in (fail_plan or {}).items()}
        self.complete_status = complete_status
        self.part_delay = part_delay
        self.uploaded_parts = []  # (part_number, bytes, thread_ident)
        self.aborted = []
        self.completed = []
        self.init_calls = 0

    def init_multipart_upload(self, key, params=None):
        self.init_calls += 1
        return SimpleNamespace(upload_id="UPLOAD-ID-1")

    def upload_part(self, key, upload_id, part_number, data, **kwargs):
        if self.part_delay:
            time.sleep(self.part_delay)
        content = data.read() if hasattr(data, "read") else b""
        plan = self.fail_plan.get(part_number, [])
        if plan:
            outcome = plan.pop(0)
            if outcome is not None:
                raise outcome
        self.uploaded_parts.append(
            (part_number, content, threading.get_ident())
        )
        return SimpleNamespace(etag=f"etag-{part_number}")

    def abort_multipart_upload(self, key, upload_id):
        self.aborted.append((key, upload_id))

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.completed.append(
            (key, upload_id, list(parts), dict(headers or {}))
        )
        return SimpleNamespace(status=self.complete_status)


class ConcurrencyDefaultsTests(unittest.TestCase):
    def test_default_concurrency_is_2_everywhere(self):
        self.assertEqual(upload_module.P115_DEFAULT_UPLOAD_CONCURRENCY, 2)
        self.assertEqual(upload_module.P115_MAX_UPLOAD_CONCURRENCY, 16)

    def _service_with_owner_attr(self, value):
        owner = SimpleNamespace(
            _cross_transfer_upload_concurrency=value,
            _target_file_cache=MagicMock(),
        )
        return upload_module.P115UploadService(OwnerDelegator(owner))

    def test_upload_concurrency_reads_owner_and_clamps(self):
        self.assertEqual(self._service_with_owner_attr(3)._upload_concurrency(), 3)
        self.assertEqual(self._service_with_owner_attr(0)._upload_concurrency(), 0)
        self.assertEqual(self._service_with_owner_attr(1)._upload_concurrency(), 1)
        self.assertEqual(self._service_with_owner_attr(99)._upload_concurrency(), 16)
        self.assertEqual(self._service_with_owner_attr(-5)._upload_concurrency(), 0)
        self.assertEqual(
            self._service_with_owner_attr("bad")._upload_concurrency(), 2
        )

    def test_upload_concurrency_defaults_to_2_without_owner_attr(self):
        owner = SimpleNamespace(_target_file_cache=MagicMock())
        service = upload_module.P115UploadService(OwnerDelegator(owner))
        self.assertEqual(service._upload_concurrency(), 2)


class LocalConcurrentUploadTests(unittest.TestCase):
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
        self._bucket = _FakeBucket(part_delay=0.02)
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        upload_module.Oss2StsAuth = MagicMock(return_value=MagicMock())
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, concurrency, init_side_effect=None, token_side_effect=None):
        client = MagicMock()
        far_future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
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
            _cross_transfer_upload_concurrency=concurrency,
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def _upload(self, service, content=CONTENT, patch_sleep=True):
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(content)
            progress = []
            # 不 patch 时假桶的 part_delay 要真睡，否则多 worker 断言会抖。
            sleep_patch = (
                patch.object(upload_module.time, "sleep")
                if patch_sleep
                else nullcontext()
            )
            with sleep_patch:
                self.assertTrue(
                    service.upload_file(
                        str(source), "/",
                        progress_callback=lambda a, b: progress.append((a, b)),
                    )
                )
            return progress

    def test_concurrent_upload_uses_multiple_workers(self):
        service = self._make_service(concurrency=2)
        self._upload(service, patch_sleep=False)
        parts = sorted(self._bucket.uploaded_parts)
        self.assertEqual(
            [(number, content) for number, content, _ in parts],
            [(1, b"0123"), (2, b"4567"), (3, b"89")],
        )
        # 3 片、2 路 worker：至少 2 个不同线程 PUT 过。
        idents = {ident for _, _, ident in self._bucket.uploaded_parts}
        self.assertGreaterEqual(len(idents), 2)
        # complete 收到按分片号排序的 parts。
        self.assertEqual(len(self._bucket.completed), 1)
        _, _, completed_parts, _ = self._bucket.completed[0]
        self.assertEqual(
            [(p.part_number, p.etag) for p in completed_parts],
            [(1, "etag-1"), (2, "etag-2"), (3, "etag-3")],
        )
        self.assertEqual(self._bucket.aborted, [])

    def test_concurrency_zero_or_one_is_sequential(self):
        for concurrency in (0, 1):
            self._bucket = _FakeBucket(part_delay=0.0)
            upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
            service = self._make_service(concurrency=concurrency)
            self._upload(service)
            parts = sorted(self._bucket.uploaded_parts)
            self.assertEqual(
                [number for number, _, _ in parts], [1, 2, 3]
            )
            # 顺序循环：全部在主线程 PUT。
            main_ident = threading.get_ident()
            self.assertEqual(
                {ident for _, _, ident in self._bucket.uploaded_parts},
                {main_ident},
            )

    def test_single_part_file_is_sequential_even_with_concurrency(self):
        service = self._make_service(concurrency=2)
        self._upload(service, content=b"012")
        self.assertEqual(len(self._bucket.uploaded_parts), 1)
        main_ident = threading.get_ident()
        self.assertEqual(
            {ident for _, _, ident in self._bucket.uploaded_parts},
            {main_ident},
        )

    def test_concurrent_token_error_refreshes_once_and_retries(self):
        self._bucket = _FakeBucket(
            fail_plan={2: [_token_error("InvalidAccessKeyId")]},
            part_delay=0.0,
        )
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        service = self._make_service(concurrency=2)
        with patch.object(upload_module.time, "sleep"):
            self._upload(service)
        # 第 2 片 token 过期 → 刷新一次（gettoken 2 次：初始 + 刷新）。
        self.assertEqual(self._client.upload_gettoken.call_count, 2)
        parts = sorted(self._bucket.uploaded_parts)
        self.assertEqual([number for number, _, _ in parts], [1, 2, 3])
        self.assertEqual(self._bucket.aborted, [])


if __name__ == "__main__":
    unittest.main()
