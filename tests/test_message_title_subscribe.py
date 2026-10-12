import time
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import OwnerDelegator, load_module, register_mock

# 构造 mock 的 app 模块依赖
mock_message_chain = MagicMock()
mock_logger = MagicMock()

register_mock("app")
register_mock("app.log", logger=mock_logger)
register_mock("app.chain.message", MessageChain=mock_message_chain)

# 构造 mock 的 cloudsubscribe 包结构
register_mock("cloudsubscribe")
register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.core.delegation", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.search")
search_types = register_mock("cloudsubscribe.search.types")
search_types.resource_type_from_url = lambda url: "115" if "115.com" in url else ""

MessageRoutingHook = load_module(
    "cloudsubscribe.core.hook.message",
    "plugins.v2/cloudsubscribe/core/hook/message.py",
).MessageRoutingHook


class FakeMediaInfo:
    """仅提供片名订阅路径用到的字段。"""

    title = "仙逆"
    year = "2023"
    type = MagicMock()


class TestTitleSubscribeHook(unittest.TestCase):
    """
    消息纯片名自动订阅（title_subscribe_enabled）单元测试
    """

    def setUp(self):
        self.owner = MagicMock()
        self.hook = MessageRoutingHook(self.owner)
        self.hook._enabled = True
        self.hook._direct_transfer_enabled = True
        self.hook._title_subscribe_enabled = True
        self.hook.extract_resource_links = lambda text: [
            w for w in text.split() if "http" in w or w.startswith("magnet:?")
        ]

        self.searches = []
        self.posts = []
        self._stub("_post_command_message", lambda *a, **k: self.posts.append(a))
        self._stub(
            "_dispatch_title_search",
            lambda subscribe_id, state: self.searches.append((subscribe_id, state)),
        )

    def _stub(self, name, value):
        """替换方法为桩函数：类上有定义就改类，否则改宿主（属性读写会委托过去）。"""
        if hasattr(MessageRoutingHook, name):
            patcher = patch.object(MessageRoutingHook, name, staticmethod(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        else:
            setattr(self.owner, name, value)

    # ------------------------------------------------------------------ 判定
    def test_title_payload_accepts_plain_title(self):
        payload = self.hook._title_payload({"text": "  仙逆  ", "userid": "1"})
        self.assertIsNotNone(payload)
        self.assertEqual(payload["text"], "仙逆")

    def test_title_payload_rejects_commands_callbacks_and_links(self):
        for arguments in (
                {"text": "/search 仙逆"},
                {"text": "CALLBACK:select"},
                {"text": "仙逆", "callback_data": "select:1"},
                {"text": "https://115.com/s/123"},
                {"text": "magnet:?xt=urn:btih:abc"},
                {"text": "仙逆 有吗？"},
                {"text": "仙逆", "images": ["a.jpg"]},
                {"text": "仙逆", "files": ["a.zip"]},
                {"text": "仙逆", "has_audio_input": True},
                {"text": "1 2 3 4"},
                {"text": "x" * 41},
                {"text": "a\nb"},
                {"text": "仙逆/第01集"},
        ):
            self.assertIsNone(self.hook._title_payload(arguments), arguments)

    def test_title_payload_disabled(self):
        self.hook._title_subscribe_enabled = False
        self.assertIsNone(self.hook._title_payload({"text": "仙逆"}))

    # --------------------------------------------------------------- 业务流
    def _patch_resolution(self, subscribe=None, created=(42, "")):
        mediainfo = FakeMediaInfo()
        self._stub("_resolve_title_media", lambda text: (mediainfo, MagicMock(), 1))
        self._stub("_find_title_subscribe", lambda *a: subscribe)
        self._stub("_create_title_subscribe", lambda *a: created)

    def test_creates_subscribe_and_triggers_search(self):
        self._patch_resolution()
        self.hook._handle_title_subscribe(
            {"text": "仙逆", "channel": "telegram", "userid": "1"}
        )
        self.assertEqual(self.searches, [(42, "N")])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("已创建订阅", self.posts[0][1])
        self.assertIn("仙逆", self.posts[0][2])

    def test_existing_subscribe_only_triggers_search(self):
        self._patch_resolution(subscribe=MagicMock(id=7))
        self.hook._handle_title_subscribe({"text": "仙逆", "userid": "1"})
        self.assertEqual(self.searches, [(7, "R")])
        self.assertIn("已在订阅中", self.posts[0][1])

    def test_unrecognized_title_stays_silent(self):
        self._stub("_resolve_title_media", lambda text: None)
        self.hook._handle_title_subscribe({"text": "在吗", "userid": "1"})
        self.assertEqual(self.searches, [])
        self.assertEqual(self.posts, [])

    def test_create_failure_reports_reason(self):
        self._patch_resolution(created=(None, "订阅已存在"))
        self.hook._handle_title_subscribe({"text": "仙逆", "userid": "1"})
        self.assertEqual(self.searches, [])
        self.assertIn("创建订阅失败", self.posts[0][1])
        self.assertIn("订阅已存在", self.posts[0][2])

    def test_recognize_exception_is_swallowed(self):
        def boom(text):
            raise RuntimeError("tmdb 超时")

        self._stub("_resolve_title_media", boom)
        self.hook._handle_title_subscribe({"text": "仙逆", "userid": "1"})
        self.assertEqual(self.posts, [])

    # ------------------------------------------------------------- 路由行为
    def test_try_handle_consumes_title_message(self):
        self._patch_resolution()
        self.assertTrue(self.hook._try_handle({"text": "仙逆", "userid": "1"}))
        for _ in range(40):
            if self.searches:
                break
            time.sleep(0.05)
        self.assertEqual(self.searches, [(42, "N")])

    def test_try_handle_ignores_non_title_message(self):
        self._patch_resolution()
        # 命令消息交回平台
        self.assertFalse(self.hook._try_handle({"text": "/search 仙逆"}))
        # 链接消息仍走链接直达转存，不进入片名订阅路径
        self.assertTrue(self.hook._try_handle({"text": "https://115.com/s/1"}))
        self.assertEqual(self.searches, [])

    def test_install_disabled_when_both_switches_off(self):
        orig_func = MagicMock()
        mock_message_chain._handle_message_core = orig_func
        self.hook._title_subscribe_enabled = False
        self.hook._direct_transfer_enabled = False
        self.hook.install()
        self.assertIs(mock_message_chain._handle_message_core, orig_func)
        self.hook._title_subscribe_enabled = True
        self.hook.install()
        self.assertIsNot(mock_message_chain._handle_message_core, orig_func)
        self.hook.close()
        self.assertIs(mock_message_chain._handle_message_core, orig_func)


if __name__ == "__main__":
    unittest.main()
