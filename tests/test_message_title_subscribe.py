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

pending_module = load_module(
    "cloudsubscribe.core.pending_titles",
    "plugins.v2/cloudsubscribe/core/pending_titles.py",
)
MessageRoutingHook = load_module(
    "cloudsubscribe.core.hook.message",
    "plugins.v2/cloudsubscribe/core/hook/message.py",
).MessageRoutingHook


class FakeMediaInfo:
    """仅提供片名订阅路径用到的字段。"""

    title = "仙逆"
    year = "2023"
    type = MagicMock()
    tmdbid = 223911


class TestTitleSubscribeHook(unittest.TestCase):
    """
    片名候选登记（1.6.27：登记不消费）与选定后创建订阅的单元测试
    """

    def setUp(self):
        self.store = pending_module.reset_pending_title_store()
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
        self.links = []
        self.ensured = []
        self._stub("_post_command_message", lambda *a, **k: self.posts.append(a))
        self._stub(
            "_dispatch_title_search",
            lambda subscribe_id, state: self.searches.append((subscribe_id, state)),
        )
        self._stub("handle_telegram_links", lambda payload: self.links.append(payload))
        # 身份补齐只验证链路，不依赖真实媒体兼容层
        self._stub("_tmdb_id_of", lambda media: 223911 if media is not None else None)

    def _stub(self, name, value):
        """替换方法为桩函数：类上有定义就改类，否则改宿主（属性读写会委托过去）。"""
        if hasattr(MessageRoutingHook, name):
            patcher = patch.object(MessageRoutingHook, name, staticmethod(value))
            patcher.start()
            self.addCleanup(patcher.stop)
        else:
            setattr(self.owner, name, value)

    def _wait_for(self, predicate, seconds=2.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

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

    # ------------------------------------------------------------- 登记候选
    def test_try_handle_registers_candidate_without_consuming(self):
        self._stub("_resolve_title_media", lambda text: (FakeMediaInfo(), "tv", 1))
        # 返回 False = 不消费消息，交回平台给出候选列表
        self.assertFalse(self.hook._try_handle({"text": "仙逆", "userid": "1"}))
        records = self.store.peek()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "仙逆")
        # 不再直接建订阅/回执
        self.assertEqual(self.posts, [])
        self.assertEqual(self.searches, [])

    def test_candidate_is_enriched_with_tmdb_identity(self):
        self._stub("_resolve_title_media", lambda text: (FakeMediaInfo(), "tv", 1))
        self.assertFalse(self.hook._try_handle({"text": "仙逆", "userid": "1"}))
        self.assertTrue(
            self._wait_for(lambda: self.store.peek()[0].get("tmdbid") == 223911)
        )
        record = self.store.peek()[0]
        self.assertEqual(record["title"], "仙逆")
        self.assertEqual(str(record["year"]), "2023")

    def test_enrich_failure_keeps_raw_candidate(self):
        def boom(text):
            raise RuntimeError("tmdb 超时")

        self._stub("_resolve_title_media", boom)
        self.assertFalse(self.hook._try_handle({"text": "仙逆", "userid": "1"}))
        time.sleep(0.1)
        records = self.store.peek()
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["tmdbid"])

    def test_try_handle_ignores_non_title_message(self):
        self._stub("_resolve_title_media", lambda text: (FakeMediaInfo(), "tv", 1))
        # 命令消息交回平台，且不登记
        self.assertFalse(self.hook._try_handle({"text": "/search 仙逆"}))
        self.assertEqual(self.store.peek(), [])
        # 链接消息仍走链接直达转存，消息被消费
        self.assertTrue(self.hook._try_handle({"text": "https://115.com/s/1"}))
        self.assertEqual(len(self.links), 1)
        self.assertEqual(self.store.peek(), [])

    # ------------------------------------------------------------- 选定接入
    def _patch_ensure(self):
        """把「建订阅」这一步换成记录桩，便于单独校验选定后的调用参数。"""
        self._stub(
            "_ensure_title_subscribe",
            lambda mediainfo, media_type, season, title, payload: self.ensured.append(
                (mediainfo, media_type, season, title, payload)
            ),
        )

    def _patch_resolution(self, subscribe=None, created=(42, "")):
        self._stub("_resolve_title_media", lambda text: (FakeMediaInfo(), "tv", 1))
        self._stub("_find_title_subscribe", lambda *a: subscribe)
        self._stub("_create_title_subscribe", lambda *a: created)

    def test_creates_subscribe_and_triggers_search(self):
        self._patch_resolution()
        self.hook._ensure_title_subscribe(FakeMediaInfo(), MagicMock(), 1, "仙逆", {})
        self.assertEqual(self.searches, [(42, "N")])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("已创建订阅", self.posts[0][1])
        self.assertIn("仙逆", self.posts[0][2])

    def test_existing_subscribe_only_triggers_search(self):
        self._patch_resolution(subscribe=MagicMock(id=7))
        self.hook._ensure_title_subscribe(FakeMediaInfo(), MagicMock(), 1, "仙逆", {})
        self.assertEqual(self.searches, [(7, "R")])
        self.assertIn("已在订阅中", self.posts[0][1])

    def test_create_failure_reports_reason(self):
        self._patch_resolution(created=(None, "订阅已存在"))
        self.hook._ensure_title_subscribe(FakeMediaInfo(), MagicMock(), 1, "仙逆", {})
        self.assertEqual(self.searches, [])
        self.assertIn("创建订阅失败", self.posts[0][1])
        self.assertIn("订阅已存在", self.posts[0][2])

    def test_handle_selected_title_prefers_selected_identity(self):
        self._patch_ensure()
        self._stub(
            "_resolve_selected_media",
            lambda identity: (FakeMediaInfo(), MagicMock(), 2),
        )
        record = self.store.remember(text="仙逆", userid="1", channel="telegram")
        self.hook._handle_selected_title(record, {"titles": ["仙逆"], "season": 2})
        self.assertEqual(len(self.ensured), 1)
        self.assertEqual(self.ensured[0][3], "仙逆")
        self.assertEqual(self.ensured[0][4]["userid"], "1")

    def test_handle_selected_title_falls_back_to_title_recognition(self):
        self._patch_ensure()
        self._stub("_resolve_selected_media", lambda identity: None)
        self._stub("_resolve_title_media", lambda text: (FakeMediaInfo(), "tv", 1))
        record = self.store.remember(text="仙逆")
        self.hook._handle_selected_title(record, {"titles": ["仙逆"]})
        self.assertEqual(len(self.ensured), 1)

    def test_handle_selected_title_unrecognized_stays_silent(self):
        self._patch_ensure()
        self._stub("_resolve_selected_media", lambda identity: None)
        self._stub("_resolve_title_media", lambda text: None)
        record = self.store.remember(text="在吗")
        self.hook._handle_selected_title(record, {"titles": ["在吗"]})
        self.assertEqual(self.ensured, [])
        self.assertEqual(self.posts, [])

    def test_handle_selected_title_swallows_resolution_error(self):
        self._patch_ensure()
        def boom(identity):
            raise RuntimeError("识别超时")

        self._stub("_resolve_selected_media", boom)
        record = self.store.remember(text="仙逆")
        self.hook._handle_selected_title(record, {"titles": ["仙逆"]})
        self.assertEqual(self.ensured, [])

    # ------------------------------------------------------------- 生命周期
    def test_close_clears_candidates(self):
        self._stub("_resolve_title_media", lambda text: (FakeMediaInfo(), "tv", 1))
        self.assertFalse(self.hook._try_handle({"text": "仙逆", "userid": "1"}))
        self.assertEqual(len(self.store), 1)
        self.hook.close()
        self.assertEqual(len(self.store), 0)

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
