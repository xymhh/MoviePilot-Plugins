"""短链规范化单测：白名单、302 展开、缓存、失败回退。

真机依据（2026-11-02 账本）：44 条「参数错误。」全部来自 ``pan.qoark.cn``，
实测 302 → ``pan.quark.cn/s/<真实ID>``。这里用本地 stub 复刻同样的跳转。
"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plugin_env import ensure_package, load_module

ensure_package("cloudsubscribex")
ensure_package("cloudsubscribex.core")
ensure_package("cloudsubscribex.search")
share_link = load_module(
    "cloudsubscribex.core.share_link",
    "plugins.v2/cloudsubscribex/core/share_link.py",
)
search_types = load_module(
    "cloudsubscribex.search.types",
    "plugins.v2/cloudsubscribex/search/types.py",
)


class _RedirectState:
    def __init__(self) -> None:
        self.hits = 0
        self.target = "/s/5eef7f324c9d"
        self.status = 302
        self.fail = False


STATE = _RedirectState()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802
        STATE.hits += 1
        if STATE.fail:
            self.send_response(500)
            self.end_headers()
            return
        if STATE.status == 302:
            self.send_response(302)
            self.send_header("Location", STATE.target)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        STATE.hits += 1
        if STATE.fail:
            self.send_response(500)
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ShareLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._hosts = share_link.SHORT_LINK_HOSTS

    @classmethod
    def tearDownClass(cls) -> None:
        share_link.SHORT_LINK_HOSTS = cls._hosts
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        STATE.hits = 0
        STATE.target = f"http://localhost:{self.port}/s/5eef7f324c9d"
        STATE.status = 302
        STATE.fail = False
        share_link.SHORT_LINK_HOSTS = frozenset({"qoark.cn", "127.0.0.1"})
        share_link._cache.clear()
        self.base = f"http://127.0.0.1:{self.port}"

    def test_qoark_host_is_short_link(self):
        self.assertTrue(share_link.is_short_link("https://pan.qoark.cn/s/vkuabikk4n"))
        self.assertTrue(share_link.is_short_link("https://qoark.cn/s/x"))
        self.assertFalse(share_link.is_short_link("https://pan.quark.cn/s/abc"))

    def test_lookalike_host_rejected(self):
        self.assertFalse(share_link.is_short_link("https://evil-qoark.cn/s/x"))
        self.assertFalse(share_link.is_short_link("https://qoark.cn.evil.com/s/x"))

    def test_non_short_link_untouched_without_request(self):
        url = "https://pan.quark.cn/s/5eef7f324c9d"
        self.assertEqual(share_link.canonicalize_share_url(url), url)
        self.assertEqual(STATE.hits, 0)

    def test_redirect_expanded(self):
        url = f"{self.base}/s/vkuabikk4n"
        self.assertEqual(share_link.canonicalize_share_url(url), STATE.target)
        self.assertGreaterEqual(STATE.hits, 1)

    def test_cache_hit_skips_request(self):
        url = f"{self.base}/s/vkuabikk4n"
        share_link.canonicalize_share_url(url)
        first = STATE.hits
        share_link.canonicalize_share_url(url)
        self.assertEqual(STATE.hits, first)

    def test_failure_returns_original(self):
        STATE.fail = True
        url = f"{self.base}/s/vkuabikk4n"
        self.assertEqual(share_link.canonicalize_share_url(url), url)
        self.assertGreaterEqual(STATE.hits, 1)

    def test_unreachable_returns_original(self):
        url = "http://127.0.0.1:1/s/x"
        self.assertEqual(share_link.canonicalize_share_url(url), url)

    def test_no_redirect_returns_original(self):
        STATE.status = 200
        url = f"{self.base}/s/vkuabikk4n"
        self.assertEqual(share_link.canonicalize_share_url(url), url)

    def test_cache_expiry_refetches(self):
        url = f"{self.base}/s/vkuabikk4n"
        share_link.canonicalize_share_url(url)
        with share_link._cache_lock:
            target, _ = share_link._cache[url]
            share_link._cache[url] = (target, 0)
        before = STATE.hits
        share_link.canonicalize_share_url(url)
        self.assertGreater(STATE.hits, before)

    def test_empty_input(self):
        self.assertEqual(share_link.canonicalize_share_url(""), "")
        self.assertEqual(share_link.canonicalize_share_url(None), "")


class QoarkTypeTests(unittest.TestCase):
    def test_qoark_resolves_to_quark(self):
        self.assertEqual(
            search_types.resource_type_from_url("https://pan.qoark.cn/s/vkuabikk4n"),
            "quark",
        )

    def test_quark_still_quark(self):
        self.assertEqual(
            search_types.resource_type_from_url("https://pan.quark.cn/s/abc"), "quark"
        )

    def test_unknown_host_stays_unknown(self):
        self.assertEqual(search_types.resource_type_from_url("https://pan.unknown.cn/s/x"), "")


if __name__ == "__main__":
    unittest.main()
