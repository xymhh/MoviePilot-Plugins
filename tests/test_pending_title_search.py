import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import OwnerDelegator, load_module, register_mock, set_media_type

# 构造 mock 的 app 模块依赖
mock_logger = MagicMock()

register_mock("app")
register_mock("app.log", logger=mock_logger)
register_mock("app.chain.subscribe", SubscribeChain=MagicMock())
register_mock("app.db.subscribe_oper", SubscribeOper=MagicMock())

# 构造 mock 的 cloudsubscribe 包结构
register_mock("cloudsubscribe")
register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.core.hook")

pending_module = load_module(
    "cloudsubscribe.core.pending_titles",
    "plugins.v2/cloudsubscribe/core/pending_titles.py",
)
PendingTitleStore = pending_module.PendingTitleStore
normalize_title = pending_module.normalize_title
SubscriptionSearchHook = load_module(
    "cloudsubscribe.core.hook.subscription",
    "plugins.v2/cloudsubscribe/core/hook/subscription.py",
).SubscriptionSearchHook

MediaType = set_media_type()


class FakeMediaInfo:
    """搜索识别结果的最小替身。"""

    def __init__(self, title="仙逆", year="2023", mtype="tv", tmdbid=223911):
        self.title = title
        self.year = year
        self.type = mtype
        self.tmdbid = tmdbid


def fake_id_search(chain, media_source, media_id, mtype=None, area="title", season=None):
    """模拟 SearchChain.search_by_id 的签名。"""
    return []


def fake_title_search(chain, title, page=0, sites=None, mtype=None):
    """模拟 SearchChain.search_by_title 的签名。"""
    return []


def fake_kwargs_search(chain, **kwargs):
    """模拟接收 meta/mediainfo 关键字的搜索方法。"""
    return []


class TestPendingTitleStore(unittest.TestCase):
    """片名登记表单元测试。"""

    def test_normalize_title_drops_punctuation_and_case(self):
        self.assertEqual(normalize_title("  仙 逆 "), "仙逆")
        self.assertEqual(normalize_title("The.Bear"), "thebear")
        self.assertEqual(normalize_title("仙逆 (2023)"), "仙逆2023")
        self.assertEqual(normalize_title(None), "")

    def test_consume_matches_normalized_title_once(self):
        store = PendingTitleStore()
        store.remember(text="仙 逆", userid="1")
        record = store.consume(titles=["仙逆"])
        self.assertIsNotNone(record)
        self.assertEqual(record["text"], "仙 逆")
        # 命中即消费，第二次不再返回
        self.assertIsNone(store.consume(titles=["仙逆"]))
        self.assertEqual(len(store), 0)

    def test_consume_prefers_identity_match(self):
        store = PendingTitleStore()
        store.remember(text="仙逆", tmdbid=223911, mtype="tv")
        store.remember(text="仙逆", tmdbid=999, mtype="tv")
        record = store.consume(titles=["仙逆"], tmdbid=223911, mtype="tv")
        self.assertEqual(record["tmdbid"], 223911)

    def test_consume_respects_media_type(self):
        store = PendingTitleStore()
        store.remember(text="仙逆", mtype="tv")
        self.assertIsNone(store.consume(titles=["仙逆"], mtype="movie"))
        self.assertIsNotNone(store.consume(titles=["仙逆"], mtype="tv"))

    def test_consume_requires_exact_title(self):
        store = PendingTitleStore()
        store.remember(text="仙逆")
        self.assertIsNone(store.consume(titles=["仙逆 第二季"]))
        self.assertIsNone(store.consume(titles=[]))
        self.assertEqual(len(store), 1)

    def test_expired_record_is_not_consumed(self):
        store = PendingTitleStore(ttl=0)
        store.remember(text="仙逆")
        time.sleep(0.01)
        self.assertIsNone(store.consume(titles=["仙逆"]))
        self.assertEqual(len(store), 0)

    def test_max_records_evicts_oldest(self):
        store = PendingTitleStore(max_records=2)
        store.remember(text="一")
        store.remember(text="二")
        store.remember(text="三")
        texts = [item["text"] for item in store.peek()]
        self.assertEqual(texts, ["二", "三"])

    def test_enrich_adds_recognized_title_to_keys(self):
        store = PendingTitleStore()
        record = store.remember(text="仙逆 第二季")
        store.enrich(record["id"], title="仙逆", year="2023", mtype="tv", season=2, tmdbid=223911)
        # 识别出的规范标题也能命中
        hit = store.consume(titles=["仙逆"], mtype="tv")
        self.assertEqual(hit["tmdbid"], 223911)
        self.assertEqual(hit["season"], 2)

    def test_drop_and_clear(self):
        store = PendingTitleStore()
        record = store.remember(text="仙逆")
        store.drop(record["id"])
        self.assertEqual(len(store), 0)
        store.remember(text="仙逆")
        store.clear()
        self.assertEqual(len(store), 0)


class TestPendingTitleSearchHook(unittest.TestCase):
    """搜索拦截命中片名候选的单元测试。"""

    def setUp(self):
        self.store = pending_module.reset_pending_title_store()
        self.owner = MagicMock()
        self.hook = SubscriptionSearchHook(self.owner)
        self.selected = []
        # 宿主（插件）上的落地点：搜索钩子通过职责委托调用它
        self.owner._handle_selected_title = (
            lambda record, identity: self.selected.append((record, identity))
        )
        # 媒体身份工具打桩，避免依赖真实 v2/v3 兼容层
        for name, value in (
            ("_tmdb_id_of", lambda media: getattr(media, "tmdbid", None)),
            ("_is_tmdb_source", lambda source: str(source or "").lower() in {"themoviedb", "tmdb"}),
        ):
            patcher = patch.object(SubscriptionSearchHook, name, staticmethod(value))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _wait_async(self, seconds=2.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.02)
            if self.selected:
                return
        return

    # ------------------------------------------------------------ 身份解析
    def test_identity_from_id_search(self):
        identity = self.hook._search_identity(
            fake_id_search, (object(), "themoviedb", "223911"), {"mtype": MediaType.TV}
        )
        self.assertEqual(identity["tmdbid"], "223911")
        self.assertEqual(identity["media_id"], "223911")
        self.assertEqual(identity["mtype"], "tv")
        self.assertEqual(identity["titles"], [])

    def test_identity_from_title_search(self):
        identity = self.hook._search_identity(
            fake_title_search, (object(), "仙逆"), {"mtype": MediaType.TV, "page": 1}
        )
        self.assertEqual(identity["titles"], ["仙逆"])
        self.assertIsNone(identity["tmdbid"])
        self.assertEqual(identity["mtype"], "tv")

    def test_identity_from_meta_object(self):
        meta = MagicMock()
        meta.name = "仙逆"
        meta.type = MediaType.TV
        meta.begin_season = 2
        identity = self.hook._search_identity(fake_kwargs_search, (object(),), {"meta": meta})
        self.assertIn("仙逆", identity["titles"])
        self.assertEqual(identity["mtype"], "tv")
        self.assertEqual(identity["season"], 2)

    def test_identity_none_without_any_signal(self):
        self.assertIsNone(
            self.hook._search_identity(fake_title_search, (object,), {})
        )

    # --------------------------------------------------------------- 消费
    def test_tmdb_identity_hit_dispatches_subscribe(self):
        self.store.remember(text="仙逆", tmdbid=223911, mtype="tv")
        self.hook._dispatch_pending_title_search(
            fake_id_search, (object(), "themoviedb", "223911"), {"mtype": MediaType.TV}
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0][0]["text"], "仙逆")
        self.assertEqual(len(self.store), 0)

    def test_title_hit_dispatches_subscribe(self):
        self.store.remember(text="仙 逆", mtype="tv")
        self.hook._dispatch_pending_title_search(
            fake_title_search, (object(), "仙逆"), {"mtype": MediaType.TV}
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(len(self.store), 0)

    def test_unrelated_search_keeps_record(self):
        self.store.remember(text="仙逆", mtype="tv")
        self.hook._dispatch_pending_title_search(
            fake_title_search, (object(), "别的片子"), {"mtype": MediaType.TV}
        )
        self.assertEqual(self.selected, [])
        self.assertEqual(len(self.store), 1)

    def test_empty_store_short_circuits(self):
        self.hook._dispatch_pending_title_search(
            fake_title_search, (object(), "仙逆"), {}
        )
        self.assertEqual(self.selected, [])

    def test_id_only_search_falls_back_to_recognition(self):
        self.store.remember(text="仙逆", mtype="tv")
        self.hook._sync_handler._recognize_media_once = MagicMock(
            return_value=FakeMediaInfo()
        )
        identity = self.hook._search_identity(
            fake_id_search, (object(), "themoviedb", "223911"), {"mtype": MediaType.TV}
        )
        self.hook._recognize_selected_title(identity)
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0][1]["title"], "仙逆")
        self.assertEqual(len(self.store), 0)

    def test_recognition_failure_is_silent(self):
        self.store.remember(text="仙逆", mtype="tv")
        self.hook._sync_handler._recognize_media_once = MagicMock(
            side_effect=RuntimeError("tmdb 超时")
        )
        identity = self.hook._search_identity(
            fake_id_search, (object(), "themoviedb", "223911"), {"mtype": MediaType.TV}
        )
        self.hook._recognize_selected_title(identity)
        self.assertEqual(self.selected, [])
        # 识别失败不消费记录，用户正常搜索不受影响
        self.assertEqual(len(self.store), 1)

    # ------------------------------------------------------------- 包装器
    def _blocked(self):
        patcher = patch.object(
            SubscriptionSearchHook, "_is_platform_search_blocked", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sync_wrapper_blocks_and_dispatches(self):
        self._blocked()
        self.store.remember(text="仙逆", mtype="tv")
        wrapper = self.hook._create_platform_search_wrapper(
            "search_by_title", "sync", fake_title_search
        )
        self.assertEqual(wrapper(object(), "仙逆", mtype=MediaType.TV), [])
        self._wait_async()
        self.assertEqual(len(self.selected), 1)

    def test_sync_wrapper_passes_through_when_not_blocked(self):
        self.base = patch.object(
            SubscriptionSearchHook, "_is_platform_search_blocked", return_value=False
        )
        self.base.start()
        self.addCleanup(self.base.stop)
        calls = []

        def original(chain, title, page=0):
            calls.append(title)
            return ["ok"]

        wrapper = self.hook._create_platform_search_wrapper(
            "search_by_title", "sync", original
        )
        self.assertEqual(wrapper(object(), "仙逆"), ["ok"])
        self.assertEqual(calls, ["仙逆"])

    def test_async_wrapper_blocks(self):
        self._blocked()

        async def original(chain, title, page=0):
            return ["ok"]

        wrapper = self.hook._create_platform_search_wrapper(
            "async_search_by_title", "async", original
        )
        self.assertEqual(asyncio.run(wrapper(object(), "仙逆")), [])

    def test_stream_wrapper_blocks(self):
        self._blocked()
        self.store.remember(text="仙逆", mtype="tv")

        async def original(chain, title, page=0):
            yield {"type": "done"}

        wrapper = self.hook._create_platform_search_wrapper(
            "async_search_by_title_stream", "stream", original
        )

        async def collect():
            return [event async for event in wrapper(object(), "仙逆")]

        events = asyncio.run(collect())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["items"], [])
        self._wait_async()
        self.assertEqual(len(self.selected), 1)


if __name__ == "__main__":
    unittest.main()
