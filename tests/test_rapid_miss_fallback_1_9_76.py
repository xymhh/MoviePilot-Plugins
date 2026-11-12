"""1.9.76 真机冲突修复：fallback 下负缓存不拦截 / 不自我续期 / 运行期同步 / 代理 request 层注入。

对应 2026-11-12 线上事故：用户把「秒传未命中处理」切到「回落普通上传」并配了代理，
任务仍被「6 分钟前已确认未命中（115），24 小时内不再下载」拦住并走 aborted。
"""
import ast
import os
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

TRANSFER = "plugins.v2/cloudsubscribex/core/transfer.py"
INIT = "plugins.v2/cloudsubscribex/__init__.py"
CLIENT = "plugins.v2/cloudsubscribex/drive/p115/client.py"


def read(rel):
    with open(os.path.join(project_root, rel), encoding="utf-8") as handle:
        return handle.read()


class TestRapidMissAborted(unittest.TestCase):
    """从 AST 单独抽出异常类执行，避免加载整条 transfer 依赖链。"""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(read(TRANSFER))
        node = next(
            n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "RapidMissAborted"
        )
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<ast>", "exec"), namespace)
        cls.exception_cls = namespace["RapidMissAborted"]

    def test_default_from_cache_false(self):
        error = self.exception_cls("探测未命中")
        self.assertFalse(error.from_cache)
        self.assertEqual(str(error), "探测未命中")

    def test_cache_skip_marks_from_cache(self):
        error = self.exception_cls("命中负缓存", from_cache=True)
        self.assertTrue(error.from_cache)


class TestNegativeCacheWiring(unittest.TestCase):
    def setUp(self):
        self.transfer = read(TRANSFER)
        self.init = read(INIT)
        self.client = read(CLIENT)

    def test_negative_cache_guard_requires_abort(self):
        marker = "负缓存只服务 abort 模式"
        self.assertIn(marker, self.transfer)
        block = self.transfer[self.transfer.index(marker):self.transfer.index(marker) + 400]
        self.assertIn('self._rapid_miss_action == "abort"', block)
        self.assertIn("self._rapid_miss_at(target_key, known_sha1)", block)

    def test_cache_skip_raises_with_from_cache(self):
        self.assertIn("from_cache=True", self.transfer)

    def test_except_does_not_refresh_ttl(self):
        self.assertIn("except RapidMissAborted as rapid_miss_error:", self.transfer)
        self.assertIn(
            'not getattr(rapid_miss_error, "from_cache", False)', self.transfer
        )
        self.assertIn('self._rapid_miss_action == "abort"', self.transfer)

    def test_runtime_sync_without_restart(self):
        self.assertIn("def set_rapid_miss_action", self.transfer)
        self.assertIn("setter(self._cross_transfer_rapid_miss_action)", self.init)
        self.assertIn("_cross_transfer_manager", self.init)

    def test_proxy_injected_at_request_layer(self):
        self.assertIn("types.MethodType", self.client)
        self.assertIn('kwargs.setdefault("proxies", proxies)', self.client)
        self.assertIn("115 上传代理已生效", self.client)
        self.assertNotIn("未暴露可注入的会话", self.client)

    def test_version_bumped(self):
        self.assertIn('plugin_version = "1.9.76"', self.init)


if __name__ == "__main__":
    unittest.main()
