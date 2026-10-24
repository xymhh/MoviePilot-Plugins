"""Emby 302 反代纯逻辑单测：URL 分流、改写、STRM 解析、头处理与降级。"""

import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

from plugin_env import ensure_package, install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()
package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core_package = ensure_package("cloudsubscribex.core")
core_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
api_package = ensure_package("cloudsubscribex.core.api")
api_package.__path__ = [
    os.path.join(plugins_v2_path, "cloudsubscribex", "core", "api")
]

rules = load_module(
    "cloudsubscribex.core.api.emby_proxy_rules",
    "plugins.v2/cloudsubscribex/core/api/emby_proxy_rules.py",
)
api_package.emby_proxy_rules = rules
main = load_module(
    "cloudsubscribex.core.api.emby_proxy",
    "plugins.v2/cloudsubscribex/core/api/emby_proxy.py",
)

PICKCODE = "akoocdeuj01e3hhcu"
DIRECT_URL = "https://cdnfhnfile.115cdn.net/example.mkv?t=1"


class ClassifyRequestTest(unittest.TestCase):
    """任务书 §3 的分流顺序。"""

    def test_root_redirect(self):
        self.assertEqual(rules.classify_request("/"), rules.INDEX_REDIRECT)

    def test_web_is_passthrough(self):
        self.assertEqual(
            rules.classify_request("/web/index.html"), rules.PASSTHROUGH
        )

    def test_playback_info(self):
        self.assertEqual(
            rules.classify_request("/Items/123/PlaybackInfo"),
            rules.PLAYBACK_INFO,
        )

    def test_emby_prefixed_playback_info(self):
        self.assertEqual(
            rules.classify_request("/emby/Items/123/PlaybackInfo"),
            rules.PLAYBACK_INFO,
        )

    def test_video_stream(self):
        self.assertEqual(
            rules.classify_request("/Videos/1/stream"),
            rules.STREAM_REDIRECT,
        )

    def test_video_stream_with_extension(self):
        self.assertEqual(
            rules.classify_request("/Videos/1/stream.mkv"),
            rules.STREAM_REDIRECT,
        )

    def test_audio_universal(self):
        self.assertEqual(
            rules.classify_request("/Audio/1/universal"),
            rules.STREAM_REDIRECT,
        )

    def test_fake_direct_stream(self):
        self.assertEqual(
            rules.classify_request(
                "/emby/http", "MediaSourceId=1&FakeDirectStream=true"
            ),
            rules.STREAM_REDIRECT,
        )

    def test_other_path_passthrough(self):
        self.assertEqual(
            rules.classify_request("/Users/abc/Items"), rules.PASSTHROUGH
        )

    def test_websocket_rejected(self):
        self.assertEqual(
            rules.classify_request("/embywebsocket"),
            rules.WEBSOCKET_REJECT,
        )

    def test_transcode_params_detected(self):
        self.assertTrue(
            rules.is_transcode_request("VideoCodec=h264&MaxStreamingBitrate=1")
        )
        self.assertFalse(
            rules.is_transcode_request("Static=true&FakeDirectStream=true")
        )


class PlaybackInfoTest(unittest.TestCase):
    """任务书 §4 的字段改写与降级。"""

    def _payload(self):
        return {
            "ItemId": "item-1",
            "MediaSources": [
                {
                    "Id": "source-strm",
                    "ItemId": "item-1",
                    "Path": "/strm/剧集/Show S01E01.strm",
                },
                {
                    "Id": "source-file",
                    "ItemId": "item-1",
                    "Path": "/strm/剧集/Show S01E01.mkv",
                },
            ],
        }

    def test_strm_source_rewritten(self):
        payload = rules.rewrite_playback_info(
            self._payload(), "http://proxy:3010"
        )
        source = payload["MediaSources"][0]
        self.assertEqual(
            source["DirectStreamUrl"],
            "http://proxy:3010/Videos/item-1/stream"
            "?MediaSourceId=source-strm&Static=true&FakeDirectStream=true",
        )
        self.assertFalse(source["SupportsTranscoding"])
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertEqual(source["Protocol"], "Http")

    def test_non_strm_source_untouched(self):
        payload = rules.rewrite_playback_info(
            self._payload(), "http://proxy:3010"
        )
        self.assertNotIn("DirectStreamUrl", payload["MediaSources"][1])

    def test_cannot_resolve_keeps_payload(self):
        payload = rules.rewrite_playback_info(
            self._payload(),
            "http://proxy:3010",
            can_resolve=lambda _source: False,
        )
        self.assertNotIn("DirectStreamUrl", payload["MediaSources"][0])
        self.assertNotIn("SupportsTranscoding", payload["MediaSources"][0])

    def test_only_200_json_is_rewritable(self):
        self.assertFalse(
            rules.is_rewritable_playback_response(500, b"{}")
        )
        self.assertFalse(
            rules.is_rewritable_playback_response(200, b"not-json")
        )
        self.assertTrue(
            rules.is_rewritable_playback_response(200, b'{"MediaSources":[]}')
        )

    def test_extract_media_path_by_source(self):
        payload = {
            "Items": [{
                "Id": "item-1",
                "MediaSources": [
                    {"Id": "other", "Path": "/strm/other.strm"},
                    {"Id": "wanted", "Path": "/strm/wanted.strm"},
                ],
            }]
        }
        self.assertEqual(
            rules.extract_media_path(payload, "wanted"),
            "/strm/wanted.strm",
        )

    def test_build_origin_from_forwarded_headers(self):
        scope = {"scheme": "http", "server": ("127.0.0.1", 3010)}
        origin = rules.build_origin(
            scope,
            {
                "host": "127.0.0.1:3010",
                "x-forwarded-proto": "https",
                "x-forwarded-host": "emby.example.com",
            },
        )
        self.assertEqual(origin, "https://emby.example.com")


class StrmParseTest(unittest.TestCase):
    """任务书 §5：STRM 里的 pickcode / 直链 / 空文件 / 不存在。"""

    def test_pickcode_url(self):
        link = rules.parse_strm_content(
            f"http://127.0.0.1:9527/d/{PICKCODE}?/剧集 S01E01.mkv"
        )
        self.assertEqual(link.kind, "pickcode")
        self.assertEqual(link.value, PICKCODE)
        self.assertEqual(link.file_name, "剧集 S01E01.mkv")

    def test_query_pickcode_url(self):
        link = rules.parse_strm_content(
            f"http://127.0.0.1:9527/redirect?pickcode={PICKCODE}"
        )
        self.assertEqual(link.kind, "pickcode")
        self.assertEqual(link.value, PICKCODE)

    def test_direct_115_url(self):
        link = rules.parse_strm_content(f"{DIRECT_URL}\n")
        self.assertEqual(link.kind, "direct")
        self.assertEqual(link.value, DIRECT_URL)

    def test_empty_file(self):
        self.assertIsNone(rules.parse_strm_content("   \n"))

    def test_missing_file(self):
        cache = rules.StrmFileCache()
        path = os.path.join(tempfile.gettempdir(), "no-such-file.strm")
        self.assertIsNone(cache.get(path))

    def test_cache_reads_once_for_same_path(self):
        calls = []

        def reader(path):
            calls.append(path)
            return f"http://127.0.0.1:9527/d/{PICKCODE}?/x.mkv"

        cache = rules.StrmFileCache(reader=reader)
        with tempfile.NamedTemporaryFile(
                "w", suffix=".strm", delete=False
        ) as handle:
            handle.write("placeholder")
            path = handle.name
        try:
            first = cache.get(path)
            second = cache.get(path)
        finally:
            os.unlink(path)
        self.assertEqual(first.kind, "pickcode")
        self.assertEqual(second.value, PICKCODE)
        self.assertEqual(len(calls), 1)


class PathAndHeaderTest(unittest.TestCase):
    """路径映射与转发头（纯函数级）。"""

    def test_parse_mapping_longest_first(self):
        mappings = rules.parse_path_mappings(
            "/media=/mnt\n/media/strm=/strm\n# 注释行"
        )
        self.assertEqual(mappings[0].source, "/media/strm")
        self.assertEqual(mappings[1].source, "/media")

    def test_map_path_translates_prefix(self):
        self.assertEqual(
            rules.map_path(
                "/media/strm/剧集/a.strm",
                "/media/strm=/strm",
            ),
            "/strm/剧集/a.strm",
        )

    def test_map_path_without_mapping_is_identity(self):
        self.assertEqual(
            rules.map_path("/media/strm/a.strm", ""),
            "/media/strm/a.strm",
        )

    def test_request_headers_host_replaced_and_range_kept(self):
        headers = rules.filter_request_headers(
            {
                "Host": "proxy:3010",
                "Range": "bytes=0-1023",
                "Connection": "keep-alive",
                "User-Agent": "Infuse",
            },
            "embyserver:8096",
        )
        self.assertEqual(headers["Host"], "embyserver:8096")
        self.assertEqual(headers["Range"], "bytes=0-1023")
        self.assertNotIn("Connection", headers)
        # Emby 会无视 identity 强制压缩，只声明 httpx 能解压的编码
        self.assertEqual(headers["Accept-Encoding"], "gzip, deflate")

    def test_response_headers_drop_content_encoding(self):
        headers = dict(rules.filter_response_headers({
            "Content-Type": "video/mp4",
            "Content-Encoding": "gzip",
            "Transfer-Encoding": "chunked",
            "Connection": "keep-alive",
        }))
        self.assertNotIn("content-encoding", {k.lower() for k in headers})
        self.assertNotIn("transfer-encoding", {k.lower() for k in headers})
        self.assertEqual(headers["Content-Type"], "video/mp4")

    def test_response_headers_keep_duplicate_set_cookie(self):
        class MultiHeaders:
            def multi_items(self):
                return [
                    ("Set-Cookie", "a=1; Path=/"),
                    ("Set-Cookie", "b=2; Path=/"),
                    ("Content-Encoding", "br"),
                ]

        headers = rules.filter_response_headers(MultiHeaders())
        self.assertEqual(
            [v for k, v in headers if k.lower() == "set-cookie"],
            ["a=1; Path=/", "b=2; Path=/"],
        )
        self.assertNotIn("content-encoding", {k.lower() for k, _ in headers})

    def test_allowed_clients(self):
        self.assertTrue(rules.allowed_client("anything", ""))
        self.assertTrue(rules.allowed_client("Infuse/7.0", "infuse, vlc"))
        self.assertFalse(rules.allowed_client("Chrome", "infuse"))


class ServiceFallbackTest(unittest.TestCase):
    """失败必须返回 None/空串触发透传，不能把异常抛给插件。"""

    def test_missing_strm_returns_empty(self):
        owner = SimpleNamespace(_cloud_drive=None)
        service = main.EmbyProxyService(owner)
        result = asyncio.run(
            service._resolve_strm_url("/tmp/no-such-file.strm")
        )
        self.assertEqual(result, "")

    def test_link_unavailable(self):
        owner = SimpleNamespace(_cloud_drive=None)
        service = main.EmbyProxyService(owner)
        self.assertFalse(service._link_available())

    def test_disabled_service_does_not_start(self):
        owner = SimpleNamespace(_emby_proxy_enabled=False)
        status = main.start_emby_proxy(owner)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])

    def test_status_contract(self):
        owner = SimpleNamespace(
            _emby_proxy_enabled=False,
            _emby_proxy_port=3010,
            _emby_proxy_upstream="http://embyserver:8096",
        )
        status = main.emby_proxy_status(owner)
        self.assertEqual(status["port"], 3010)
        self.assertEqual(status["upstream"], "http://embyserver:8096")
        self.assertEqual(
            set(status["stats"]), {"hit", "miss", "fallback", "error"}
        )


class WebsocketProxyTest(unittest.TestCase):
    """WebSocket 必须转发给上游；上游缺失时安全关闭，不影响 REST。"""

    def test_websocket_closes_when_upstream_missing(self):
        owner = SimpleNamespace(_cloud_drive=None)
        service = main.EmbyProxyService(owner)
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "websocket",
            "path": "/embywebsocket",
            "query_string": b"api_key=x&deviceId=y",
            "headers": [],
        }
        asyncio.run(main.EmbyProxyApp(service)(scope, receive, send))
        self.assertEqual(sent[-1]["type"], "websocket.close")
        self.assertEqual(sent[-1]["code"], 1011)

    def test_ws_connect_falls_back_to_extra_headers(self):
        calls = []

        class FakeModule:
            @staticmethod
            def connect(url, **kwargs):
                calls.append(kwargs)
                if "additional_headers" in kwargs:
                    raise TypeError("unexpected keyword argument")

                async def _ok():
                    return "conn"

                return _ok()

        conn = asyncio.run(
            main.EmbyProxyService._ws_connect(
                FakeModule, "ws://embyserver:8096/embywebsocket", {"A": "b"}
            )
        )
        self.assertEqual(conn, "conn")
        self.assertEqual(len(calls), 2)
        self.assertIn("additional_headers", calls[0])
        self.assertIn("extra_headers", calls[1])


if __name__ == "__main__":
    unittest.main()
