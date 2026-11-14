"""1.9.84 上传代理未生效真机修复。

事故（2026-11-14 01:49 / 02:46 两次真实上传）：插件配置里已填
`cross_transfer_upload_proxy = http://mihomo:7890`，但 mihomo 里来自
moviepilot-v3（172.18.0.5）的连接为 0，日志里一条「115 上传数据面已启用代理」都没有。

根因：`P115UploadService` 继承 `OwnerDelegator`，其 `_owner` 是 **`P115ClientManager`**，
不是插件实例；而 `P115ClientManager.__getattr__` 只解析职责组件（`_COMPONENT_TYPES`），
**不会**转发到插件。旧写法
`getattr(self._owner, "_cross_transfer_upload_proxy", "")`
恒拿到默认空串 → `oss_request_kwargs("")` → `{}` → 代理从未注入。

修复：直接读 manager 在 `create_client` 时存好的 `_upload_proxies`
（`normalize_proxies` 的结果 `{"http": ..., "https": ...}`），保留旧属性作为兼容回退。
"""
import ast
import os
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
PKG = os.path.join(project_root, "plugins.v2", "cloudsubscribex")
UPLOAD = os.path.join(PKG, "drive", "p115", "upload.py")
CLIENT = os.path.join(PKG, "drive", "p115", "client.py")
DEFINITION = os.path.join(PKG, "drive", "p115", "definition.py")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestOwnerChain(unittest.TestCase):
    """用真实类验证 owner 链：P115UploadService._owner 是 P115ClientManager。"""

    def test_client_manager_getattr_does_not_forward_to_plugin(self):
        """核心事实：P115ClientManager.__getattr__ 只解析组件，不转发插件属性。

        这条断言锁住根因。若将来有人给它加上插件转发，这个测试会失败并提醒
        重新评估 `_oss_proxy_kwargs` 的读取路径。
        """
        source = read(CLIENT)
        tree = ast.parse(source)
        manager = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "P115ClientManager"
        )
        getattr_fn = next(
            node for node in manager.body
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        )
        body_source = ast.unparse(getattr_fn)
        self.assertIn("resolve_component", body_source)
        self.assertNotIn("_plugin", body_source)
        self.assertNotIn("_cross_transfer_upload_proxy", body_source)

    def test_manager_stores_upload_proxies(self):
        source = read(CLIENT)
        self.assertIn("self._upload_proxies = proxies or None", source)

    def test_create_client_passes_normalized_proxies(self):
        source = read(DEFINITION)
        self.assertIn(
            'proxies = normalize_proxies(config.get("_cross_transfer_upload_proxy"))',
            source,
        )
        self.assertIn("proxies=proxies,", source)

    def test_drive_manager_passes_plugin_dict(self):
        source = read(os.path.join(PKG, "core", "drive_manager.py"))
        self.assertIn("def_cls.create_client(", source)
        self.assertIn("self._plugin.__dict__", source)


class TestOssProxyKwargsReadsManager(unittest.TestCase):
    """执行真实方法体：owner 是 manager（带 _upload_proxies）时必须返回 session。"""

    @classmethod
    def setUpClass(cls):
        source = read(UPLOAD)
        tree = ast.parse(source)
        service = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "P115UploadService"
        )
        fn = next(
            node for node in service.body
            if isinstance(node, ast.FunctionDef) and node.name == "_oss_proxy_kwargs"
        )
        module = ast.Module(body=[fn], type_ignores=[])

        class FakeLogger:
            def __init__(self):
                self.infos = []

            def info(self, message, *a, **k):
                self.infos.append(str(message))

            def warning(self, message, *a, **k):
                pass

        calls = []

        def fake_oss_request_kwargs(proxy_url):
            calls.append(proxy_url)
            return {"session": f"pool:{proxy_url}"} if proxy_url else {}

        namespace = {
            "Dict": dict,
            "Any": object,
            "logger": FakeLogger(),
            "oss_request_kwargs": fake_oss_request_kwargs,
        }
        ast.fix_missing_locations(module)
        exec(compile(module, "<upload._oss_proxy_kwargs>", "exec"), namespace)
        # 注意：普通函数赋给类属性会变成方法（self 会错位），必须用 staticmethod 包一层
        cls.fn = staticmethod(namespace["_oss_proxy_kwargs"])
        cls.logger = namespace["logger"]
        cls.calls = calls

    def _service(self, owner):
        """返回一个只带 `_owner` 的轻量实例。

        AST 抽出的函数签名是 `def _oss_proxy_kwargs(self)`，直接调用并显式传实例即可；
        若再用 MethodType 绑定会多出一个位置参数。
        """
        class Service:
            pass

        instance = Service()
        instance._owner = owner
        return instance

    def test_manager_with_upload_proxies_returns_session(self):
        """真机路径：manager 上存的是 normalize_proxies 的结果。"""
        manager = type("Manager", (), {
            "_upload_proxies": {"http": "http://mihomo:7890", "https": "http://mihomo:7890"},
        })()
        result = self.fn(self._service(manager))
        self.assertEqual(result, {"session": "pool:http://mihomo:7890"})
        self.assertIn("http://mihomo:7890", self.calls)

    def test_manager_without_proxies_returns_empty(self):
        manager = type("Manager", (), {"_upload_proxies": None})()
        self.assertEqual(self.fn(self._service(manager)), {})

    def test_legacy_attribute_fallback_still_works(self):
        """兼容回退：owner 直接挂插件属性时也能取到。"""
        owner = type("Owner", (), {
            "_upload_proxies": None,
            "_cross_transfer_upload_proxy": "http://mihomo:7890",
        })()
        self.assertEqual(
            self.fn(self._service(owner)),
            {"session": "pool:http://mihomo:7890"},
        )

    def test_manager_regression_old_read_path_returns_empty(self):
        """反证根因：manager 没有插件属性时，旧读取路径必然拿空。"""
        manager = type("Manager", (), {"_upload_proxies": None})()
        legacy = getattr(manager, "_cross_transfer_upload_proxy", "")
        self.assertEqual(legacy, "", "manager 上确实没有该属性，旧实现必然拿到空串")

    def test_logged_once(self):
        manager = type("Manager", (), {
            "_upload_proxies": {"https": "http://mihomo:7890"},
        })()
        service = self._service(manager)
        before = len(self.logger.infos)
        self.fn(service)
        self.fn(service)
        after = len(self.logger.infos)
        self.assertEqual(after - before, 1, "「已启用代理」只应打一次")


if __name__ == "__main__":
    unittest.main()
