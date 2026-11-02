"""后端 TaskContext 与结构化异常体系单元测试。"""

import threading
import time
import unittest

from plugin_env import load_module

_exceptions = load_module(
    "cloudsubscribex.core.exceptions",
    "plugins.v2/cloudsubscribex/core/exceptions.py",
)
_context = load_module(
    "cloudsubscribex.core.context",
    "plugins.v2/cloudsubscribex/core/context.py",
)


class ExceptionsHierarchyTest(unittest.TestCase):
    def test_network_error_properties(self):
        err = _exceptions.NetworkError("连接超时", detail="timeout after 10s", retry_after=2.5)
        self.assertTrue(err.retryable)
        self.assertEqual(err.retry_after, 2.5)
        self.assertIn("连接超时 (timeout after 10s)", str(err))

    def test_auth_expired_error(self):
        err = _exceptions.AuthExpiredError(provider="115网盘")
        self.assertEqual(err.provider, "115网盘")
        self.assertIn("[115网盘]", str(err))

    def test_resource_invalid_error(self):
        err = _exceptions.ResourceInvalidError("https://pan.quark.cn/s/dead", reason="文件已被删除")
        self.assertEqual(err.share_url, "https://pan.quark.cn/s/dead")
        self.assertEqual(err.reason, "文件已被删除")

    def test_task_aborted_error(self):
        err = _exceptions.TaskAbortedError()
        self.assertIsInstance(err, _exceptions.CloudSubscribeError)


class TaskContextLifecycleTest(unittest.TestCase):
    def test_context_initial_state(self):
        ctx = _context.TaskContext(scope="manual", timeout_budget=120)
        self.assertEqual(ctx.scope, "manual")
        self.assertFalse(ctx.is_cancelled)
        self.assertGreater(len(ctx.run_id), 8)
        # 健康检查应顺利通过
        ctx.check_alive()

    def test_context_cancel_signal_triggers_aborted(self):
        cancel_evt = threading.Event()
        ctx = _context.TaskContext(cancel_event=cancel_evt)
        self.assertFalse(ctx.is_cancelled)
        
        cancel_evt.set()
        self.assertTrue(ctx.is_cancelled)
        with self.assertRaises(_exceptions.TaskAbortedError):
            ctx.check_alive()

    def test_context_timeout_triggers_timeout_error(self):
        ctx = _context.TaskContext(timeout_budget=0.05)
        time.sleep(0.08)
        with self.assertRaises(_exceptions.TaskTimeoutError):
            ctx.check_alive()

    def test_context_progress_reporting(self):
        reports = []
        ctx = _context.TaskContext(progress_callback=lambda snap: reports.append(snap))
        ctx.set_progress(current=5, total=10, transferred=2, phase="正在下载")
        
        self.assertEqual(len(reports), 1)
        snap = reports[0]
        self.assertEqual(snap["current"], 5)
        self.assertEqual(snap["total"], 10)
        self.assertEqual(snap["transferred"], 2)
        self.assertEqual(snap["phase"], "正在下载")


if __name__ == "__main__":
    unittest.main()
