"""1.18.0 Phase 3b：统一重试策略表 + 429 判据 + 数据面 pacing 单测。

覆盖：
1. UPLOAD_RETRY_POLICY 四阶段（init/part/file/range）的次数与基数
   与 v1.17.0 各实现的既有值一致；
2. retry_delay 指数退避 + ±20% 抖动 + 60 秒上限 + 429 用 5 秒起步；
3. is_rate_limit_error：数字码 429 优先，文本按风控家族归类；
4. DriveRateLimiter.acquire() 只做起步 pacing（不执行、不重试）。
"""

import os
import sys
import time
import unittest
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

load_module(
    "cloudsubscribex.core.error_text",
    "plugins.v2/cloudsubscribex/core/error_text.py",
)
failure_module = load_module(
    "cloudsubscribex.core.failure",
    "plugins.v2/cloudsubscribex/core/failure.py",
)
policy_module = load_module(
    "cloudsubscribex.drive.p115.upload_policy",
    "plugins.v2/cloudsubscribex/drive/p115/upload_policy.py",
)
common_module = load_module(
    "cloudsubscribex.drive.common",
    "plugins.v2/cloudsubscribex/drive/common.py",
)

from oss2.exceptions import ServerError as OssServerError  # noqa: E402


class RetryPolicyTableTests(unittest.TestCase):
    def test_table_values_match_v1170_behavior(self):
        table = policy_module.UPLOAD_RETRY_POLICY
        # 次数与基数沿用 v1.17.0 各实现的既有值，只收拢到一张表。
        self.assertEqual(table["init"]["attempts"], 3)
        self.assertEqual(table["init"]["base_delay"], 2.0)
        self.assertEqual(table["part"]["attempts"], 2)
        self.assertEqual(table["part"]["base_delay"], 1.0)
        self.assertEqual(table["file"]["attempts"], 3)
        self.assertEqual(table["file"]["base_delay"], 2.0)
        self.assertEqual(table["range"]["attempts"], 3)
        self.assertEqual(table["range"]["base_delay"], 0.5)

    def test_429_backoff_and_constants(self):
        backoff = policy_module.UPLOAD_429_BACKOFF
        self.assertEqual(backoff["base_delay"], 5.0)
        self.assertEqual(backoff["factor"], 2.0)
        self.assertEqual(backoff["jitter"], 0.2)
        self.assertEqual(backoff["max_delay"], 60.0)
        self.assertEqual(policy_module.UPLOAD_RETRY_MAX_DELAY_SECONDS, 60.0)
        self.assertEqual(policy_module.UPLOAD_DATA_PUT_MIN_INTERVAL, 0.1)
        self.assertEqual(policy_module.UPLOAD_BREAKER_DEFAULT_THRESHOLD, 5)
        self.assertEqual(
            policy_module.UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS, 300.0
        )

    def test_retry_delay_exponential_without_jitter(self):
        # rand 恒返回 0，退避就是精确的 base * factor ** (attempt - 1)。
        delay = lambda stage, attempt, **kw: policy_module.retry_delay(
            stage, attempt, rand=lambda a, b: 0.0, **kw
        )
        self.assertEqual(delay("init", 1), 2.0)
        self.assertEqual(delay("init", 2), 4.0)
        self.assertEqual(delay("init", 3), 8.0)
        self.assertEqual(delay("part", 1), 1.0)
        self.assertEqual(delay("part", 2), 2.0)
        self.assertEqual(delay("file", 1), 2.0)
        self.assertEqual(delay("file", 2), 4.0)
        self.assertEqual(delay("range", 1), 0.5)
        self.assertEqual(delay("range", 2), 1.0)
        # 未知阶段按 part 计。
        self.assertEqual(delay("no-such-stage", 1), 1.0)

    def test_retry_delay_429_uses_429_base(self):
        delay = lambda stage, attempt: policy_module.retry_delay(
            stage, attempt, rate_limited=True, rand=lambda a, b: 0.0
        )
        self.assertEqual(delay("part", 1), 5.0)
        self.assertEqual(delay("part", 2), 10.0)
        self.assertEqual(delay("init", 1), 5.0)
        self.assertEqual(delay("file", 1), 5.0)

    def test_retry_delay_capped_at_60s(self):
        delay = policy_module.retry_delay(
            "file", 10, rand=lambda a, b: 0.0
        )
        self.assertEqual(delay, 60.0)
        delay_429 = policy_module.retry_delay(
            "file", 10, rate_limited=True, rand=lambda a, b: 0.0
        )
        self.assertEqual(delay_429, 60.0)

    def test_retry_delay_jitter_bounds(self):
        # 真实随机：首次退避落在 base ±20% 内，且不为负。
        for _ in range(20):
            value = policy_module.retry_delay("init", 1)
            self.assertGreaterEqual(value, 1.6)
            self.assertLessEqual(value, 2.4)
        for _ in range(20):
            value = policy_module.retry_delay("part", 1, rate_limited=True)
            self.assertGreaterEqual(value, 4.0)
            self.assertLessEqual(value, 6.0)


class IsRateLimitErrorTests(unittest.TestCase):
    def test_text_429(self):
        self.assertTrue(
            failure_module.is_rate_limit_error(OSError("HTTP 429 Too Many Requests"))
        )
        self.assertTrue(
            failure_module.is_rate_limit_error(OSError("请求返回 429"))
        )
        self.assertTrue(
            failure_module.is_rate_limit_error(
                RuntimeError("rate limit exceeded, retry later")
            )
        )
        self.assertTrue(
            failure_module.is_rate_limit_error(OSError("操作太频繁，请稍后再试"))
        )
        self.assertTrue(
            failure_module.is_rate_limit_error(OSError("账号触发风控"))
        )

    def test_numeric_429_status_code_errno(self):
        for attr in ("status", "code", "errno", "errcode", "status_code"):
            error = SimpleNamespace()
            setattr(error, attr, 429)
            self.assertTrue(
                failure_module.is_rate_limit_error(error), attr
            )
            error_str = SimpleNamespace()
            setattr(error_str, attr, "429")
            self.assertTrue(
                failure_module.is_rate_limit_error(error_str), attr
            )

    def test_numeric_429_in_dict_arg(self):
        error = RuntimeError("oss error")
        error.args = ({"status": 429},)
        self.assertTrue(failure_module.is_rate_limit_error(error))

    def test_numeric_429_in_cause_chain(self):
        oss_error = OssServerError(
            429, {}, "", {"Code": "TooManyRequests", "Message": "slow down"}
        )
        wrapped = RuntimeError("115 分片上传失败")
        wrapped.__cause__ = oss_error
        self.assertTrue(failure_module.is_rate_limit_error(wrapped))
        # oss2 的 429 错误本身（status 属性即 429）。
        self.assertTrue(failure_module.is_rate_limit_error(oss_error))

    def test_non_429_is_false(self):
        self.assertFalse(failure_module.is_rate_limit_error(OSError("抖动")))
        self.assertFalse(failure_module.is_rate_limit_error(OSError("timeout")))
        self.assertFalse(failure_module.is_rate_limit_error(OSError(5, "boom")))
        self.assertFalse(failure_module.is_rate_limit_error(None))
        self.assertFalse(failure_module.is_rate_limit_error(""))
        # 500 不是 429。
        error = SimpleNamespace(status=500)
        self.assertFalse(failure_module.is_rate_limit_error(error))


class DataRateLimiterAcquireTests(unittest.TestCase):
    def test_acquire_enforces_interval_without_executing(self):
        limiter = common_module.DriveRateLimiter(
            min_interval=0.05, jitter_ratio=0
        )
        limiter.acquire()
        started = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - started
        # 第二次 acquire 至少等够 0.05 秒（留 20% 余量防 CI 抖动）。
        self.assertGreaterEqual(elapsed, 0.04)

    def test_acquire_returns_none(self):
        limiter = common_module.DriveRateLimiter(
            min_interval=0.0, jitter_ratio=0
        )
        self.assertIsNone(limiter.acquire())


if __name__ == "__main__":
    unittest.main()
