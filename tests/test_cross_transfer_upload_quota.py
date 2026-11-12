"""1.9.75 跨盘真实上传 115：上传代理 + 每日 900G 额度单测。

覆盖：limit clamp 边界、按天重置、超限跳过、并发原子累加、秒传/0 字节不计数、
读写异常 fail-open、代理地址规范化、transfer.py 接线不被删。
"""
import os
import sys
import threading
import unittest
from datetime import datetime

import pytz

from plugin_env import install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core", "app.utils",
    "cloudsubscribex", "cloudsubscribex.core",
    "cloudsubscribex.core.services", "cloudsubscribex.utils",
)

quota_module = load_module(
    "cloudsubscribex.core.services.upload_quota",
    "plugins.v2/cloudsubscribex/core/services/upload_quota.py",
)
http_module = load_module(
    "cloudsubscribex.utils.http_client",
    "plugins.v2/cloudsubscribex/utils/http_client.py",
)

UploadQuota = quota_module.UploadQuota
clamp_limit_gb = quota_module.clamp_limit_gb
MAX_LIMIT_GB = quota_module.MAX_LIMIT_GB
GB = quota_module.GB


class FakeRuntime:
    """最小 runtime 仓储；fail=True 模拟数据库故障。"""

    def __init__(self):
        self.data = {}
        self.fail = False

    def load(self, namespace, key, default=None):
        if self.fail:
            raise RuntimeError("runtime down")
        return self.data.get((namespace, key), default)

    def save(self, namespace, key, value):
        if self.fail:
            raise RuntimeError("runtime down")
        self.data[(namespace, key)] = value


def make_quota(limit_gb=900):
    runtime = FakeRuntime()
    return UploadQuota(limit_gb=limit_gb, runtime=runtime), runtime


class TestClampLimit(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(clamp_limit_gb(1), 1)
        self.assertEqual(clamp_limit_gb(900), 900)
        self.assertEqual(clamp_limit_gb(901), 900)      # 硬上限
        self.assertEqual(clamp_limit_gb(10000), 900)
        self.assertEqual(clamp_limit_gb(0), 1)          # 不允许关掉限额
        self.assertEqual(clamp_limit_gb(-5), 1)
        self.assertEqual(clamp_limit_gb("abc"), 900)    # 非法回落默认
        self.assertEqual(clamp_limit_gb(None), 900)
        self.assertEqual(clamp_limit_gb(""), 900)
        self.assertEqual(clamp_limit_gb("500"), 500)

    def test_limit_bytes_uses_gib(self):
        quota, _ = make_quota(900)
        self.assertEqual(quota.limit_bytes, 900 * GB)


class TestDateKey(unittest.TestCase):
    def test_natural_day_key(self):
        tz = pytz.timezone("Asia/Shanghai")
        day1 = tz.localize(datetime(2026, 11, 12, 23, 59))
        day2 = tz.localize(datetime(2026, 11, 13, 0, 1))
        self.assertEqual(UploadQuota.date_key(day1), "2026-11-12")
        self.assertEqual(UploadQuota.date_key(day2), "2026-11-13")

    def test_next_day_resets_used_bytes(self):
        quota, runtime = make_quota(1)
        yesterday = "2026-11-11"
        runtime.save(quota_module.NAMESPACE, yesterday, {"bytes": 900 * GB})
        # 当天没有记录 → 0，不会被昨天的数据吃掉额度
        self.assertEqual(quota.used_bytes("2026-11-12"), 0)
        self.assertEqual(quota.used_bytes(yesterday), 900 * GB)


class TestQuotaFlow(unittest.TestCase):
    def test_can_start_blocks_when_full(self):
        quota, _ = make_quota(1)
        quota.add(int(0.6 * GB))
        ok, reason = quota.can_start(int(0.5 * GB))
        self.assertFalse(ok)
        self.assertIn("每日上传额度已满", reason)
        ok2, _ = quota.can_start(int(0.3 * GB))
        self.assertTrue(ok2)

    def test_add_accumulates(self):
        quota, _runtime = make_quota(900)
        quota.add(100)
        quota.add(250)
        self.assertEqual(quota.used_bytes(), 350)
        self.assertEqual(quota.remaining_bytes(), 900 * GB - 350)

    def test_zero_and_negative_not_counted(self):
        quota, runtime = make_quota(900)
        quota.add(0)
        quota.add(-100)
        self.assertEqual(quota.used_bytes(), 0)
        self.assertEqual(runtime.data, {})

    def test_concurrent_add_is_atomic(self):
        quota, _ = make_quota(900)
        threads = [
            threading.Thread(target=quota.add, args=(1024 * 1024,))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(quota.used_bytes(), 20 * 1024 * 1024)

    def test_fail_open_on_runtime_error(self):
        quota, runtime = make_quota(1)
        runtime.fail = True
        self.assertEqual(quota.used_bytes(), 0)
        ok, _ = quota.can_start(10 * GB)
        self.assertTrue(ok)
        quota.add(1024)  # 不抛异常
        runtime.fail = False
        self.assertEqual(quota.used_bytes(), 0)  # 故障期间不写坏数据


class TestProxyNormalize(unittest.TestCase):
    def test_http_proxy_normalized_for_requests_client(self):
        proxies = http_module.normalize_proxies("http://mihomo:7890")
        self.assertIsInstance(proxies, dict)
        self.assertEqual(proxies["http"], "http://mihomo:7890")
        self.assertEqual(proxies["https"], "http://mihomo:7890")

    def test_invalid_proxy_rejected(self):
        with self.assertRaises(ValueError):
            http_module.validate_proxy_address("ftp://1.2.3.4:8080")

    def test_empty_proxy_disables(self):
        self.assertIsNone(http_module.normalize_proxies(""))


class TestTransferWiring(unittest.TestCase):
    """静态接线断言：防止额度预检/累加被后续重构删掉。"""

    def setUp(self):
        path = os.path.join(
            project_root, "plugins.v2", "cloudsubscribex", "core", "transfer.py"
        )
        with open(path, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_quota_hooks_present(self):
        self.assertIn("self._quota.can_start", self.source)
        self.assertIn("self._quota.add", self.source)
        self.assertIn('not in ("rapid", "reuse")', self.source)
        self.assertIn('quota: object = None', self.source)


if __name__ == "__main__":
    unittest.main()
