"""搜索源瘦身 v1.21.0：第 5 项——BaseSearchClient。

覆盖：PirateBay / UIndex 迁到共用基类后的构造状态（base_url 归一化、
proxy 归一化两种风格、timeout/interval 钳制、request_timeout 覆盖、
共享门控同一性、update_config 重建门控、UIndex 的 _security 同步）。
其余 12 个客户端本次不动（分歧见 base_client.py 模块注释）。
"""

import sys
import unittest

from plugin_env import (
    install_app_mocks,
    install_real_ttl_cache,
    load_module,
)

install_app_mocks(
    "app.log",
    "app.core",
    "app.core.config",
    "app.core.metainfo",
    "app.helper",
    "app.helper.browser",
    "cloudsubscribex",
    "cloudsubscribex.search",
    "cloudsubscribex.utils",
)
install_real_ttl_cache()

_SAVED_MODULES = {}
for _name in (
    "cloudsubscribex.search.base_client",
    "cloudsubscribex.search.piratebay.client",
    "cloudsubscribex.search.uindex.client",
    "cloudsubscribex.search.uindex.security",
    "cloudsubscribex.search.cloudflare",
    "cloudsubscribex.search.http_client",
    "cloudsubscribex.search.magnet",
    "cloudsubscribex.utils.cache",
    "cloudsubscribex.utils.magnet",
    "torf",
):
    _SAVED_MODULES[_name] = sys.modules.get(_name)


def tearDownModule():
    for _name, _mod in _SAVED_MODULES.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


# utils/magnet 在导入时绑走 torf：更早的文件会把它注册成 mock，先换真实库。
sys.modules.pop("torf", None)
import torf  # noqa: F401,E402

http_client = load_module(
    "cloudsubscribex.search.http_client",
    "plugins.v2/cloudsubscribex/search/http_client.py",
)
RequestGate = http_client.RequestGate
# 更早的文件会把 utils.cache 注册成 mock（create_platform_ttl_cache 是
# MagicMock）；这里先重载真实模块，缓存行为才可断言。
load_module(
    "cloudsubscribex.utils.cache",
    "plugins.v2/cloudsubscribex/utils/cache.py",
)
base_client_module = load_module(
    "cloudsubscribex.search.base_client",
    "plugins.v2/cloudsubscribex/search/base_client.py",
)
BaseSearchClient = base_client_module.BaseSearchClient
PirateBayClient = load_module(
    "cloudsubscribex.search.piratebay.client",
    "plugins.v2/cloudsubscribex/search/piratebay/client.py",
).PirateBayClient
UIndexClient = load_module(
    "cloudsubscribex.search.uindex.client",
    "plugins.v2/cloudsubscribex/search/uindex/client.py",
).UIndexClient


class BaseClientTests(unittest.TestCase):

    def test_subclass_contract(self):
        self.assertTrue(issubclass(PirateBayClient, BaseSearchClient))
        self.assertTrue(issubclass(UIndexClient, BaseSearchClient))
        self.assertEqual(PirateBayClient._GATE_NAME, "PirateBay")
        self.assertEqual(UIndexClient._GATE_NAME, "UIndex")
        self.assertEqual(PirateBayClient._PROXY_STYLE, "address")
        self.assertEqual(UIndexClient._PROXY_STYLE, "raw")

    def test_piratebay_defaults(self):
        client = PirateBayClient()
        self.assertEqual(client.base_url, "https://apibay.org")
        self.assertEqual(client.timeout, 20)
        self.assertEqual(client.request_interval, 1.0)
        self.assertEqual(client.proxy, "")
        self.assertEqual(client._gate.name, "PirateBay")

    def test_uindex_defaults_and_request_timeout(self):
        client = UIndexClient()
        self.assertEqual(client.base_url, "https://uindex.org")
        self.assertEqual(client.timeout, 20)
        self.assertEqual(client._gate.name, "UIndex")
        overridden = UIndexClient(timeout=20, request_timeout=45)
        self.assertEqual(overridden.timeout, 45)

    def test_base_url_normalization(self):
        for raw, expected in [
            ("https://x.org/", "https://x.org"),
            ("  https://x.org/  ", "https://x.org"),
            ("", "https://apibay.org"),
            (None, "https://apibay.org"),
        ]:
            client = PirateBayClient(base_url=raw)
            self.assertEqual(client.base_url, expected, raw)

    def test_proxy_styles_differ(self):
        # PirateBay：normalize_proxy_address 会补 scheme。
        self.assertEqual(
            PirateBayClient(proxy="127.0.0.1:7890").proxy,
            "http://127.0.0.1:7890",
        )
        # UIndex：只 strip，不补 scheme。
        self.assertEqual(
            UIndexClient(proxy="127.0.0.1:7890").proxy, "127.0.0.1:7890"
        )
        self.assertEqual(
            UIndexClient(proxy="  http://h:1  ").proxy, "http://h:1"
        )

    def test_timeout_and_interval_clamps(self):
        self.assertEqual(PirateBayClient(timeout=3).timeout, 5)
        self.assertEqual(PirateBayClient(timeout=0).timeout, 20)
        self.assertEqual(PirateBayClient(timeout=None).timeout, 20)
        self.assertEqual(PirateBayClient(request_interval=0.1).request_interval, 0.2)
        self.assertEqual(PirateBayClient(request_interval=0).request_interval, 1.0)

    def test_gate_is_shared_for_same_args(self):
        first = PirateBayClient(proxy="http://p:1")
        second = PirateBayClient(proxy="http://p:1")
        self.assertIs(first._gate, second._gate)
        self.assertIs(
            first._gate,
            RequestGate.shared(
                "PirateBay", "https://apibay.org|http://p:1",
                request_interval=1.0, minimum_interval=0.2,
                serial_requests=False,
            ),
        )

    def test_update_config_rebuilds_gate(self):
        client = PirateBayClient()
        old_gate = client._gate
        client.update_config(base_url="https://other.example/")
        self.assertEqual(client.base_url, "https://other.example")
        self.assertIsNot(client._gate, old_gate)
        # 同参数重建回到同一共享门控
        client.update_config(base_url="https://apibay.org")
        self.assertIs(client._gate, old_gate)

    def test_update_config_partial_keeps_rest(self):
        client = UIndexClient(timeout=30, request_interval=2.0)
        client.update_config(timeout=45)
        self.assertEqual(client.timeout, 45)
        self.assertEqual(client.request_interval, 2.0)
        self.assertEqual(client.base_url, "https://uindex.org")

    def test_uindex_security_follows_proxy(self):
        client = UIndexClient(proxy="http://a:1")
        self.assertEqual(client._security.proxy, "http://a:1")
        client.update_config(proxy="http://b:2")
        self.assertEqual(client._security.proxy, "http://b:2")

    def test_cache_roundtrip(self):
        client = PirateBayClient()
        client._cache.set("k", "v")
        self.assertEqual(client._cache.get("k"), "v")


if __name__ == "__main__":
    unittest.main()
