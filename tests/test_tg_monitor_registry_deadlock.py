"""TG 监控实例注册表的死锁回归测试（1.7.32）。

回归背景：2026-10-24 线上把插件更新到 1.7.31 后，MP 界面长期停在「插件等待加载」，
点卡片、调 ``POST /api/v1/plugin/reload/...`` 都没有任何反应（reload 请求 100s 无响应）。
py-spy 抓到卡住的线程：

    stop (core/tg_monitor.py:623)   ← with monitors_lock:
    start (core/tg_monitor.py:603)  ← 持同一把锁调用 other.stop()

``_shared_monitor_registry()`` 给的是 ``threading.Lock``（不可重入）。旧版本
``start()`` 在 ``with monitors_lock`` 内部遍历注册表并调用各旧实例的 ``stop()``，
而 ``stop()`` 一进来又要拿同一把锁——同线程二次获取即永久阻塞，把 MP 的
``_lifecycle_lock`` 一起拖死，于是之后所有插件加载/卸载都静默排队。

本用例锁住：注册表里已存在其它实例时，``start()`` 必须能返回。
"""

import os
import pathlib
import tempfile
import threading
import unittest

from plugin_env import install_app_mocks, load_module, register_package

install_app_mocks()

# tg_monitor 用相对导入引用同包模块，必须把真实的包路径挂上
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "plugins.v2", "cloudsubscribex")
register_package("cloudsubscribex", paths=[_PKG])
register_package("cloudsubscribex.core", paths=[os.path.join(_PKG, "core")])
register_package("cloudsubscribex.search", paths=[os.path.join(_PKG, "search")])

tg = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)


class _Owner:
    def __init__(self, path):
        self._path = path

    def get_data_path(self):
        return self._path


class TgMonitorRegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = pathlib.Path(tempfile.mkdtemp())
        self._monitors, _lock = tg._shared_monitor_registry()
        self._seed = set(self._monitors)

    def tearDown(self):
        # 注册表挂在 sys 上跨用例共享，恢复现场免得污染其它测试
        self._monitors.clear()
        for item in self._seed:
            self._monitors.add(item)

    def test_start_returns_when_other_instance_registered(self):
        old = tg.TgChannelMonitor(_Owner(self._tmp))
        old._enabled = False
        self._monitors.add(old)

        new = tg.TgChannelMonitor(_Owner(self._tmp))
        new._enabled = False
        done = threading.Event()

        def _run():
            new.start()
            done.set()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        self.assertTrue(
            done.wait(3.0),
            "start() 在持锁调用 other.stop() 时死锁（mprunners 会永久等待）",
        )
        new.stop()
        self.assertNotIn(new, self._monitors)

    def test_start_stop_roundtrip_is_reentrant_safe(self):
        """同一实例重复 start/stop 不应阻塞，也不应留下活线程。"""
        monitor = tg.TgChannelMonitor(_Owner(self._tmp))
        monitor._enabled = False
        for _ in range(3):
            done = threading.Event()

            def _run(m=monitor, d=done):
                m.start()
                m.stop()
                d.set()

            threading.Thread(target=_run, daemon=True).start()
            self.assertTrue(done.wait(3.0), "重复 start/stop 出现阻塞")
        self.assertNotIn(monitor, self._monitors)


if __name__ == "__main__":
    unittest.main()
