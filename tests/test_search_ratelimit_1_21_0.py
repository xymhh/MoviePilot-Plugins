"""搜索源瘦身 v1.21.0：UnlockLimiter 合并后两种策略的行为。

覆盖：hdhaven 模式（纯窗口计数、钳制上限 5、空 key 兜底 "default"、
不碰 random、不写 _READY_ATS、只查冷却）与 hdhive 模式（最小间隔、
首解锁延迟、钳制上限 3、空 key 兜底 ""、先查停止标志再查冷却），
以及 namespace 隔离（同名 session_key 互不干扰）。
"""

import sys
import unittest

from plugin_env import (
    install_app_mocks,
    load_module,
)

install_app_mocks(
    "app.log",
    "cloudsubscribex",
    "cloudsubscribex.search",
)

_SAVED_MODULES = {}
for _name in ("cloudsubscribex.search.ratelimit",):
    _SAVED_MODULES[_name] = sys.modules.get(_name)


def tearDownModule():
    for _name, _mod in _SAVED_MODULES.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


ratelimit = load_module(
    "cloudsubscribex.search.ratelimit",
    "plugins.v2/cloudsubscribex/search/ratelimit.py",
)
UnlockLimiter = ratelimit.UnlockLimiter


class FakeError(Exception):
    def __init__(self, message, code="", status_code=0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class FakeClock:
    """可控时钟：sleep 只推进时间，不真等。"""

    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRandom:
    """uniform 取中点，保证可复现；调用全记录。"""

    def __init__(self):
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return (a + b) / 2


class RatelimitTests(unittest.TestCase):

    def setUp(self):
        self._saved_time = ratelimit.time
        self._saved_random = ratelimit.random
        self.clock = FakeClock()
        self.random = FakeRandom()
        ratelimit.time = self.clock
        ratelimit.random = self.random

    def tearDown(self):
        ratelimit.time = self._saved_time
        ratelimit.random = self._saved_random

    def _ns(self):
        return f"test-{self._testMethodName}"

    def _hdhaven(self, per_window=5, **kwargs):
        kwargs.setdefault("namespace", self._ns())
        return UnlockLimiter(
            "s1", per_window,
            error_factory=FakeError,
            cooldown_message="HDHaven 处于风控冷却期，跳过解锁",
            log_prefix="HDHaven 解锁按节奏等待",
            **kwargs,
        )

    def _hdhive(self, per_window=3, **kwargs):
        kwargs.setdefault("namespace", self._ns())
        kwargs.setdefault("max_per_window", 3)
        kwargs.setdefault("key_default", "")
        kwargs.setdefault("min_interval", True)
        kwargs.setdefault("first_delay", (3.0, 8.0))
        kwargs.setdefault("stopped_message", "HDHive 解锁等待已停止")
        return UnlockLimiter(
            "s1", per_window,
            error_factory=FakeError,
            cooldown_message="HDHive WebAPI 处于风控冷却期，跳过解锁",
            log_prefix="HDHive 解锁接口按节奏等待",
            **kwargs,
        )

    def test_hdhaven_clamps_per_window_to_5(self):
        self.assertEqual(self._hdhaven(per_window=99).per_window, 5)
        self.assertEqual(self._hdhaven(per_window=2).per_window, 2)
        self.assertEqual(self._hdhaven(per_window=1).per_window, 1)

    def test_hdhaven_empty_key_defaults_to_default(self):
        limiter = self._hdhaven()
        empty = UnlockLimiter(
            "", 5, namespace=self._ns(), error_factory=FakeError,
            cooldown_message="x",
        )
        self.assertEqual(empty._key, f"{self._ns()}:default")
        self.assertNotEqual(limiter._key, empty._key)

    def test_hdhaven_window_cap_waits_for_oldest_to_slide_out(self):
        limiter = self._hdhaven(per_window=5)
        for _ in range(5):
            limiter.record()
        limiter.wait_for_slot(cooldown_remaining=lambda: 0.0)
        self.assertAlmostEqual(sum(self.clock.sleeps), 60.0)
        # 窗口滑出后立即放行，不再等待。
        self.clock.sleeps.clear()
        limiter.wait_for_slot(cooldown_remaining=lambda: 0.0)
        self.assertEqual(self.clock.sleeps, [])

    def test_hdhaven_cooldown_raises(self):
        limiter = self._hdhaven()
        with self.assertRaises(FakeError) as ctx:
            limiter.wait_for_slot(cooldown_remaining=lambda: 30.0)
        self.assertIn("HDHaven 处于风控冷却期", ctx.exception.message)
        self.assertIn("30", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_hdhaven_never_touches_random_or_ready_ats(self):
        limiter = self._hdhaven()
        limiter.wait_for_slot(cooldown_remaining=lambda: 0.0)
        limiter.record()
        limiter.wait_for_slot(cooldown_remaining=lambda: 0.0)
        self.assertEqual(self.random.calls, [])
        self.assertNotIn(limiter._key, UnlockLimiter._READY_ATS)

    def test_hdhive_clamps_per_window_to_3(self):
        self.assertEqual(self._hdhive(per_window=99).per_window, 3)

    def test_hdhive_empty_key_defaults_to_empty(self):
        limiter = self._hdhive()
        empty = UnlockLimiter(
            "", 3, namespace=self._ns(), max_per_window=3, key_default="",
            error_factory=FakeError, cooldown_message="x",
        )
        self.assertEqual(empty._key, f"{self._ns()}:")
        self.assertNotEqual(limiter._key, empty._key)

    def test_hdhive_first_delay_then_min_interval(self):
        limiter = self._hdhive(per_window=3)
        limiter.wait_for_slot(
            stop_requested=lambda: False, cooldown_remaining=lambda: 0.0
        )
        # 首解锁延迟 uniform(3, 8) 取中点 5.5。
        self.assertAlmostEqual(sum(self.clock.sleeps), 5.5)
        self.assertIn(limiter._key, UnlockLimiter._READY_ATS)
        limiter.record()
        self.clock.sleeps.clear()
        limiter.wait_for_slot(
            stop_requested=lambda: False, cooldown_remaining=lambda: 0.0
        )
        # 最小间隔 60/3 + uniform(1, 4) 中点 2.5 = 22.5。
        self.assertAlmostEqual(sum(self.clock.sleeps), 22.5)

    def test_hdhive_stop_checked_before_cooldown(self):
        limiter = self._hdhive()
        with self.assertRaises(FakeError) as ctx:
            limiter.wait_for_slot(
                stop_requested=lambda: True,
                cooldown_remaining=lambda: 99.0,
            )
        self.assertEqual(ctx.exception.message, "HDHive 解锁等待已停止")
        self.assertEqual(ctx.exception.code, "stopped")

    def test_hdhive_cooldown_raises(self):
        limiter = self._hdhive()
        with self.assertRaises(FakeError) as ctx:
            limiter.wait_for_slot(
                stop_requested=lambda: False,
                cooldown_remaining=lambda: 45.0,
            )
        self.assertIn("HDHive WebAPI 处于风控冷却期", ctx.exception.message)
        self.assertIn("45", ctx.exception.message)

    def test_namespace_isolation_same_session_key(self):
        haven = self._hdhaven(per_window=5, namespace="iso-haven")
        hive = self._hdhive(
            per_window=3, namespace="iso-hive", first_delay=(0.0, 0.0),
            min_interval=False,
        )
        for _ in range(5):
            haven.record()
        # hdhive 同名 key 的历史是空的，立即放行。
        hive.wait_for_slot(
            stop_requested=lambda: False, cooldown_remaining=lambda: 0.0
        )
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("iso-haven:s1", UnlockLimiter._HISTORIES)
        self.assertIn("iso-hive:s1", UnlockLimiter._HISTORIES)
        self.assertEqual(len(UnlockLimiter._HISTORIES["iso-haven:s1"]), 5)
        self.assertEqual(len(UnlockLimiter._HISTORIES["iso-hive:s1"]), 0)


if __name__ == "__main__":
    unittest.main()
