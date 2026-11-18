"""1.18.0 Phase 3b-10：上传熔断器单测。

覆盖：
1. UploadCircuitBreaker 状态机：closed → open（连续失败达阈值）
   → half_open（冷却到期）→ closed（探针成功）/ 重新 open（探针失败）；
2. 服务层接线：熔断打开时 upload_file 直接跳过（不调客户端），
   last_error 带“熔断”；秒传探针 try_rapid_upload_by_hash 不受熔断影响。
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
policy_module = load_module(
    "cloudsubscribex.drive.p115.upload_policy",
    "plugins.v2/cloudsubscribex/drive/p115/upload_policy.py",
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


class _FakeClock:
    """只给 upload_policy 用的假时钟：不碰全局 time 模块。"""

    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now


class CircuitBreakerStateTests(unittest.TestCase):
    def setUp(self):
        self._clock = _FakeClock()
        self._saved_time = policy_module.time
        policy_module.time = SimpleNamespace(monotonic=self._clock.monotonic)
        self.addCleanup(self._restore_time)

    def _restore_time(self):
        policy_module.time = self._saved_time

    def test_closed_until_threshold_then_open(self):
        breaker = policy_module.UploadCircuitBreaker(
            failure_threshold=3, cooldown_seconds=60.0
        )
        self.assertEqual(breaker.state(), "closed")
        self.assertTrue(breaker.can_upload())
        breaker.record_failure()
        breaker.record_failure()
        self.assertEqual(breaker.state(), "closed")
        self.assertTrue(breaker.can_upload())
        breaker.record_failure()
        self.assertEqual(breaker.state(), "open")
        self.assertFalse(breaker.can_upload())
        self.assertGreater(breaker.cooldown_remaining(), 0)

    def test_success_resets_failure_count(self):
        breaker = policy_module.UploadCircuitBreaker(
            failure_threshold=3, cooldown_seconds=60.0
        )
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        self.assertEqual(breaker.state(), "closed")

    def test_half_open_after_cooldown_then_success_closes(self):
        breaker = policy_module.UploadCircuitBreaker(
            failure_threshold=2, cooldown_seconds=60.0
        )
        breaker.record_failure()
        breaker.record_failure()
        self.assertEqual(breaker.state(), "open")
        self._clock.now += 59.0
        self.assertEqual(breaker.state(), "open")
        self.assertFalse(breaker.can_upload())
        self._clock.now += 2.0
        self.assertEqual(breaker.state(), "half_open")
        self.assertTrue(breaker.can_upload())
        breaker.record_success()
        self.assertEqual(breaker.state(), "closed")
        self.assertEqual(breaker.cooldown_remaining(), 0.0)

    def test_half_open_failure_reopens(self):
        breaker = policy_module.UploadCircuitBreaker(
            failure_threshold=2, cooldown_seconds=60.0
        )
        breaker.record_failure()
        breaker.record_failure()
        self._clock.now += 61.0
        self.assertTrue(breaker.can_upload())
        breaker.record_failure()
        self.assertEqual(breaker.state(), "open")
        self.assertFalse(breaker.can_upload())

    def test_configure_clamps(self):
        breaker = policy_module.UploadCircuitBreaker()
        breaker.configure(failure_threshold=0, cooldown_seconds=1.0)
        self.assertEqual(breaker._failure_threshold, 1)
        self.assertEqual(breaker._cooldown_seconds, 5.0)
        breaker.configure(failure_threshold=7, cooldown_seconds=600.0)
        self.assertEqual(breaker._failure_threshold, 7)
        self.assertEqual(breaker._cooldown_seconds, 600.0)

    def test_defaults(self):
        breaker = policy_module.UploadCircuitBreaker()
        self.assertEqual(
            breaker._failure_threshold,
            policy_module.UPLOAD_BREAKER_DEFAULT_THRESHOLD,
        )
        self.assertEqual(
            breaker._cooldown_seconds,
            policy_module.UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS,
        )


class UploadBreakerWiringTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "P115OSS_AVAILABLE": upload_module.P115OSS_AVAILABLE,
            "OSS2_AVAILABLE": upload_module.OSS2_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
        }
        upload_module.P115_AVAILABLE = True
        upload_module.P115OSS_AVAILABLE = True
        # 只测外层熔断接线：oss2 关掉，走 p115oss 整文件路子。
        upload_module.OSS2_AVAILABLE = False
        upload_module.check_response = MagicMock()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, client, threshold=1):
        self._client = client
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
            _cross_transfer_upload_concurrency=0,
            _upload_breaker_threshold=threshold,
            _upload_breaker_cooldown_seconds=300.0,
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def test_terminal_failure_opens_breaker_and_next_upload_skips(self):
        client = MagicMock()
        client.upload_file.side_effect = OSError("p115oss 失败")
        service = self._make_service(client, threshold=1)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
            calls_after_first = self._client.upload_file.call_count
            self.assertEqual(calls_after_first, 3)
            # 熔断已打开：第二次 upload_file 直接跳过，不再调客户端。
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
            self.assertEqual(self._client.upload_file.call_count, calls_after_first)
            self.assertIn("熔断", service.last_error)

    def test_rapid_probe_bypasses_open_breaker(self):
        client = MagicMock()
        client.upload_file.side_effect = OSError("p115oss 失败")
        service = self._make_service(client, threshold=1)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
            # 熔断已打开；秒传探针仍放行（init 返回 reuse 即命中）。
            client.upload_file_init.return_value = {
                "state": True, "reuse": True, "data": {},
            }
            self.assertTrue(
                service.try_rapid_upload_by_hash(
                    "/", "05.mkv", "sha1", "A" * 40, 10,
                )
            )

    def test_success_resets_breaker(self):
        client = MagicMock()
        client.upload_file.side_effect = [
            OSError("p115oss 失败"),
            MagicMock(return_value={"ok": True}),
        ]
        service = self._make_service(client, threshold=2)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            with patch.object(upload_module.time, "sleep"):
                # 第 1 次失败 1 次后第 2 次成功：熔断计数清零。
                self.assertTrue(service.upload_file(str(source), "/"))
            # 再失败 1 次也不应打开（阈值 2，计数已清零）。
            client.upload_file.side_effect = OSError("p115oss 又失败")
            with patch.object(upload_module.time, "sleep"):
                self.assertFalse(service.upload_file(str(source), "/"))
            self.assertIn("p115oss 又失败", service.last_error)
            self.assertNotIn("熔断", service.last_error)


if __name__ == "__main__":
    unittest.main()
