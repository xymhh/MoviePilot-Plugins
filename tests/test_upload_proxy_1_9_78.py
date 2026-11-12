"""1.9.78 上传代理真机修复：OSS 数据面显式注入 ProxyManager。

背景：1.9.75/1.9.76 包装 p115client 的 request 层在真机上无效 ——
p115client 与 p115oss 都用模块级 urllib3_future_request.request，
不经过实例方法，mihomo 里看不到任何来自 MP 的连接。
"""
import os
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)

UPLOAD = "plugins.v2/cloudsubscribex/drive/p115/upload.py"
PROXY = "plugins.v2/cloudsubscribex/drive/p115/upload_proxy.py"
CLIENT = "plugins.v2/cloudsubscribex/drive/p115/client.py"
INIT = "plugins.v2/cloudsubscribex/__init__.py"


def read(rel):
    with open(os.path.join(project_root, rel), encoding="utf-8") as handle:
        return handle.read()


class TestUploadProxyModule(unittest.TestCase):
    """把模块源码里的 app.log 依赖换掉后直接 import，做行为断言。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        import tempfile

        source = read(PROXY)
        source = source.replace(
            "from app.log import logger",
            "class _L:\n"
            "    @staticmethod\n"
            "    def info(*a, **k): pass\n"
            "    @staticmethod\n"
            "    def warning(*a, **k): pass\n"
            "logger = _L()",
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            path = handle.name
        spec = importlib.util.spec_from_file_location("upload_proxy_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.module = module

    def test_empty_proxy_returns_none(self):
        self.assertIsNone(self.module.proxy_manager(""))

    def test_invalid_proxy_falls_back_to_direct(self):
        # 非法代理 URL：ProxyManager 构造失败 → 返回 None（直连），不抛异常
        self.assertIsNone(self.module.proxy_manager("::not-a-url::"))

    def test_oss_request_kwargs_empty_without_proxy(self):
        self.assertEqual(self.module.oss_request_kwargs(""), {})


class TestOssRequestKwargs(unittest.TestCase):
    """oss_request_kwargs 是薄封装，直接按源码语义断言。"""

    def setUp(self):
        self.source = read(PROXY)

    def test_oss_request_kwargs_only_adds_session(self):
        self.assertIn('return {"session": pool} if pool is not None else {}', self.source)

    def test_no_proxy_returns_empty(self):
        # 空 proxy → proxy_manager 返回 None → kwargs 为空字典
        self.assertIn('url = str(proxy_url or "").strip()', self.source)
        self.assertIn("if not url or _PROXY_MANAGER_UNAVAILABLE:", self.source)


class TestUploadWiring(unittest.TestCase):
    def setUp(self):
        self.upload = read(UPLOAD)
        self.client = read(CLIENT)
        self.init = read(INIT)

    def test_oss_calls_carry_proxy_kwargs(self):
        for call in (
            "oss_multipart_upload_init(target, **proxy_kwargs)",
            "oss_multipart_upload_complete(",
            "oss_multipart_upload_cancel(",
        ):
            self.assertIn(call, self.upload)
        self.assertIn("**proxy_kwargs,", self.upload)
        self.assertGreaterEqual(self.upload.count("**proxy_kwargs"), 4)

    def test_upload_file_main_path_carries_proxy(self):
        """1.9.79：线上实际走的 p115oss.upload_file 主路径也必须带代理。

        1.9.78 只覆盖了插件自写的 oss_multipart_* 分片；p115oss 缺失或走顺序上传时
        用的是 self.client.upload_file，那条路不传 session 就仍直连。
        注意调用形态是 `self._rate_limited_call(self.client.upload_file, ...)`，
        所以 upload_file 出现在**参数**位置，按 Call 找会漏。
        """
        import ast

        tree = ast.parse(self.upload)
        call_sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if (
                    isinstance(arg, ast.Attribute)
                    and arg.attr == "upload_file"
                    and isinstance(arg.value, ast.Attribute)
                    and arg.value.attr == "client"
                ):
                    call_sites.append(node)
                    break
        self.assertEqual(
            len(call_sites), 3, f"预期 3 处 upload_file 调用，实际 {len(call_sites)}"
        )
        # 收集函数内所有「变量名 -> 赋值表达式源码」，用于追溯 splat 的来源
        assignments = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    assignments[target.id] = ast.unparse(node.value)
        for call in call_sites:
            splats = [kw for kw in call.keywords if kw.arg is None]
            self.assertTrue(
                splats,
                f"第 {call.lineno} 行的 upload_file 调用没有 **kwargs 透传代理",
            )
            traced = False
            for kw in splats:
                source = ast.unparse(kw.value)
                if "_oss_proxy_kwargs" in source:
                    traced = True
                    break
                # **upload_kwargs 这类，回溯变量赋值来源
                if isinstance(kw.value, ast.Name):
                    assigned = assignments.get(kw.value.id, "")
                    if "_oss_proxy_kwargs" in assigned:
                        traced = True
                        break
            self.assertTrue(
                traced,
                f"第 {call.lineno} 行的 upload_file 调用**未**透传代理参数"
                f"（splat 来源里没有 _oss_proxy_kwargs）",
            )

    def test_helper_reads_owner_proxy(self):
        self.assertIn("def _oss_proxy_kwargs(self)", self.upload)
        self.assertIn("_cross_transfer_upload_proxy", self.upload)
        self.assertIn("from .upload_proxy import oss_request_kwargs", self.upload)

    def test_fake_success_log_removed(self):
        """1.9.76 那句假的「已生效」日志必须消失。"""
        self.assertNotIn("115 上传代理已生效（request 层注入", self.client)

    def test_mp_proxy_host_fallback(self):
        self.assertIn("PROXY_HOST", self.init)
        self.assertIn("插件没填时回落到 MP", self.init)

    def test_version(self):
        import re

        match = re.search(r'plugin_version = "(\d+)\.(\d+)\.(\d+)"', self.init)
        self.assertIsNotNone(match, "未找到 plugin_version")
        self.assertGreaterEqual(
            tuple(int(part) for part in match.groups()), (1, 9, 78)
        )


if __name__ == "__main__":
    unittest.main()
