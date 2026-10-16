"""HDHaven 网页接口必须带浏览器 Fetch Metadata 头（Sec-Fetch-*）。

背景：2026-10-12 起 hdhaven.com 的网页接口按 Fetch Metadata 判定来源，缺少
``Sec-Fetch-Site`` 的请求直接返回 ``403 {"code": "BROWSER_REQUIRED"}``，表现为
插件取不到 Cloudflare site key、签到失败。本文件锁死该行为。
"""

import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

import requests as real_requests

from plugin_env import OwnerDelegator, register_mock

register_mock("app")
register_mock("app.log", logger=MagicMock())
register_mock("app.core")
register_mock("app.core.config", settings=MagicMock(TZ="Asia/Shanghai"))

register_mock("cloudsubscribex")
register_mock("cloudsubscribex.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribex.core.checkin_manager")
register_mock("cloudsubscribex.core.definitions")
register_mock("cloudsubscribex.core.search")
register_mock("cloudsubscribex.core.services")
register_mock("cloudsubscribex.search")
register_mock("cloudsubscribex.search.hdhaven")
register_mock("cloudsubscribex.search.hdhaven.security")
register_mock("cloudsubscribex.search.cloudflare")
# http_client 必须暴露真实 requests：本文件断言的是真实的 Session 头合并结果
register_mock(
    "cloudsubscribex.search.http_client",
    requests=real_requests,
    RequestGate=MagicMock(),
    RequestGateCancelled=type("RequestGateCancelled", (Exception,), {}),
    RequestGateCooldown=type("RequestGateCooldown", (Exception,), {}),
    gated_idempotent_request=MagicMock(),
    normalize_proxies=MagicMock(return_value=None),
)
register_mock("cloudsubscribex.utils")
register_mock("cloudsubscribex.utils.cache")

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")

hdhaven_client_path = os.path.join(plugins_v2_path, "cloudsubscribex/search/hdhaven/client.py")
spec_h = importlib.util.spec_from_file_location("cloudsubscribex.search.hdhaven.client", hdhaven_client_path)
mod_h = importlib.util.module_from_spec(spec_h)
mod_h.__package__ = "cloudsubscribex.search.hdhaven"
sys.modules["cloudsubscribex.search.hdhaven.client"] = mod_h
spec_h.loader.exec_module(mod_h)
HDHavenClient = mod_h.HDHavenClient

EXPECTED_FETCH_METADATA = {
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}


def _json_response(status_code=200, payload=None, text="{}"):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.headers = {}
    response.json.return_value = payload if payload is not None else {}
    return response


class TestHDHavenBrowserHeaders(unittest.TestCase):
    """会话默认头必须包含浏览器 Fetch Metadata 三元组。"""

    def setUp(self):
        self.client = HDHavenClient(
            username="testuser",
            password="testpassword",
            base_url="https://hdhaven.com",
        )

    def test_session_headers_include_fetch_metadata(self):
        headers = self.client._session.headers
        for name, value in EXPECTED_FETCH_METADATA.items():
            self.assertEqual(
                headers.get(name),
                value,
                f"HDHaven 会话头缺少 {name}（站点会以 403 BROWSER_REQUIRED 拒绝）",
            )

    def test_session_headers_keep_origin_and_referer(self):
        headers = self.client._session.headers
        self.assertEqual(headers.get("Origin"), "https://hdhaven.com")
        self.assertEqual(headers.get("Referer"), "https://hdhaven.com/")
        self.assertTrue(headers.get("User-Agent", "").startswith("Mozilla/5.0"))

    def test_prepared_request_merges_fetch_metadata(self):
        """真实 requests 会话合并后的出站头必须带齐 Fetch Metadata。"""
        request = real_requests.Request(
            "GET", "https://hdhaven.com/api/auth/registration-config"
        )
        prepared = self.client._session.prepare_request(request)
        for name, value in EXPECTED_FETCH_METADATA.items():
            self.assertEqual(prepared.headers.get(name), value, f"出站请求未携带 {name}")
        self.assertTrue(prepared.headers.get("User-Agent", "").startswith("Mozilla/5.0"))


class TestHDHavenLoginSiteKey(unittest.TestCase):
    """登录流程取 Cloudflare site key 的路径回归（403 BROWSER_REQUIRED 的现场根因）。"""

    def setUp(self):
        self.client = HDHavenClient(
            username="testuser",
            password="testpassword",
            base_url="https://hdhaven.com",
        )

    def test_login_uses_registration_config_site_key(self):
        conf = _json_response(payload={"success": True, "data": {"turnstile_site_key": "0x4AAAAAAA_test_key"}})
        login_ok = _json_response(payload={"success": True, "data": {"user": {"id": "u1"}}})
        solver = MagicMock()
        solver.token.return_value = "cf-token"

        with patch.object(self.client, "request", return_value=conf) as req, \
                patch.object(mod_h, "gated_idempotent_request", return_value=login_ok), \
                patch.object(mod_h, "is_cloudflare_challenge", return_value=False), \
                patch.object(mod_h, "HDHavenTurnstile", return_value=solver), \
                patch.object(HDHavenClient, "cooldown_remaining", new_callable=PropertyMock, return_value=0.0):
            token = self.client.login()

        self.assertEqual(self.client._turnstile_site_key, "0x4AAAAAAA_test_key")
        self.assertTrue(token)
        self.assertFalse(
            req.call_args.kwargs.get("auto_login", True),
            "取 site key 必须 auto_login=False，否则登录会递归",
        )

    def test_login_without_site_key_raises(self):
        conf = _json_response(
            status_code=403,
            payload={"code": "BROWSER_REQUIRED", "message": "网页接口仅供浏览器访问，程序接入请使用 OPENAPI 授权"},
            text='{"code": "BROWSER_REQUIRED"}',
        )

        with patch.object(self.client, "request", return_value=conf), \
                patch.object(HDHavenClient, "cooldown_remaining", new_callable=PropertyMock, return_value=0.0):
            with self.assertRaises(mod_h.HDHavenError) as ctx:
                self.client.login()

        self.assertIn("Cloudflare site key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
