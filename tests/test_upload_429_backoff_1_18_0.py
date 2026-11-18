"""1.18.0 Phase 3b-10：上传 429 退避单测。

覆盖：
1. 单片 429：重试时按 5 秒起步退避（±20% 抖动），成功后计量记
   upload:429 一次、本次统计 rate_limited=1；
2. init 429：两次 429 后成功，退避约 5 秒、10 秒；
3. 429 打满：整文件失败后 last_error 仍含“429”，下游按风控分类。
"""

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
failure_module = load_module(
    "cloudsubscribex.core.failure",
    "plugins.v2/cloudsubscribex/core/failure.py",
)
# api_meter 是进程内单例：只在还没人加载过 telemetry 时才加载，
# 绝不能无条件重载——重载会造出第二个 api_meter，别的测试文件
# （test_api_meter / test_episode_telemetry）绑的是旧实例，快照就对不上。
telemetry_module = sys.modules.get("cloudsubscribex.core.telemetry")
if telemetry_module is None:
    telemetry_module = load_module(
        "cloudsubscribex.core.telemetry",
        "plugins.v2/cloudsubscribex/core/telemetry.py",
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


def _rate_limit_error():
    return OssServerError(
        429, {}, "", {"Code": "TooManyRequests", "Message": "slow down"}
    )


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


class Upload429BackoffTests(unittest.TestCase):
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
        telemetry_module.api_meter.reset()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, init_side_effect=None, bucket=None):
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
        client.upload_gettoken.side_effect = (
            lambda *args, **kwargs: _token_response(far_future)
        )
        # p115oss 回落也失败：只测 oss2 路子的 429 行为。
        client.upload_file.side_effect = OSError("p115oss 回落也失败")
        self._client = client
        if bucket is not None:
            self._bucket = bucket
            upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
            # 顺序引擎：单片 429 的退避断言要确定性。
            _cross_transfer_upload_concurrency=0,
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

    def test_part_429_retries_with_5s_backoff_and_meters(self):
        bucket = _FakeBucket(fail_plan={2: [_rate_limit_error()]})
        service = self._make_service(bucket=bucket)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertTrue(service.upload_file(str(source), "/"))
        # 单片 429 只睡一次：5 秒起步 ±20% 抖动。数据面 pacing 的
        # 起步等待（≤0.12 秒）不在断言里，只看 ≥0.5 秒的重试退避。
        delays = [
            call.args[0] for call in mock_sleep.call_args_list
            if call.args[0] >= 0.5
        ]
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 5.0, delta=1.05)
        self.assertEqual(
            [number for number, _ in self._bucket.uploaded_parts], [1, 2, 3]
        )
        # 计量：upload:429 一次；本次统计 rate_limited=1。
        snapshot = telemetry_module.api_meter.snapshot()
        self.assertEqual(snapshot.get("upload:429", {}).get("calls"), 1)
        self.assertEqual(service._upload_stat().get("rate_limited"), 1)

    def test_init_429_backs_off_5s_then_10s(self):
        service = self._make_service(init_side_effect=[
            _rate_limit_error(),
            _rate_limit_error(),
            _init_response(),
        ])
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep") as mock_sleep:
                self.assertTrue(service.upload_file(str(source), "/"))
        delays = [
            call.args[0] for call in mock_sleep.call_args_list
            if call.args[0] >= 0.5
        ]
        self.assertEqual(len(delays), 2)
        self.assertAlmostEqual(delays[0], 5.0, delta=1.05)
        self.assertAlmostEqual(delays[1], 10.0, delta=2.05)
        snapshot = telemetry_module.api_meter.snapshot()
        self.assertEqual(snapshot.get("upload:429", {}).get("calls"), 2)

    def test_terminal_429_last_error_stays_classifiable(self):
        # 每轮第 2 片都 429 两次：3 轮 × 2 次 = 6 次，上传整体失败。
        bucket = _FakeBucket(fail_plan={2: [_rate_limit_error()] * 6})
        service = self._make_service(bucket=bucket)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
        # 失败面仍带 429：下游 transfer_ops 批量熔断与 failure 分类能认出来。
        self.assertIn("429", service.last_error)
        self.assertEqual(
            failure_module.classify_failure(service.last_error),
            failure_module.KIND_RISK_CONTROL,
        )
        snapshot = telemetry_module.api_meter.snapshot()
        # 429 计量 5 次：每轮第 2 片第 1 次失败记 1 次（3 轮）+ 整文件
        # 重试前记 1 次（前 2 轮；第 3 轮终态不再记）。
        self.assertEqual(snapshot.get("upload:429", {}).get("calls"), 5)


if __name__ == "__main__":
    unittest.main()
