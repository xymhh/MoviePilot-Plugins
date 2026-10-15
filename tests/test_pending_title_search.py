import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

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
subscription_module = load_module(
    "cloudsubscribe.core.hook.subscription",
    "plugins.v2/cloudsubscribe/core/hook/subscription.py",
)
SubscriptionSearchHook = subscription_module.SubscriptionSearchHook

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


def fake_process_search(chain, mediainfo=None, keyword=None, no_exists=None, **kwargs):
    """模拟 SearchChain.process 的签名（平台点选候选后走的入口）。"""
    return ["contexts"]


def fake_search_media_resources(chain_self, request=None, mediainfo=None, *args, **kwargs):
    """模拟 InteractionChain._search_media_resources 的签名。"""
    return "platform-search"


class FakeMediaInteractionChain:
    """模拟平台 v3.1.x 改名后的类：app.chain.interaction.MediaInteractionChain。"""

    def _search_media_resources(self, request=None, mediainfo=None, *args, **kwargs):
        return "platform-search"


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

    # ------------------------------------------- 1.6.28 选定结果对齐用模糊同名兜底
    def test_consume_matches_title_with_one_char_difference(self):
        """用户输入比正式片名少一个字时也要接上（机智医生生活 / 机智的医生生活）。"""
        store = PendingTitleStore()
        store.remember(text="机智医生生活")
        hit = store.consume(titles=["机智的医生生活"], mtype="tv")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["match"], "fuzzy")
        self.assertGreaterEqual(hit["match_ratio"], 0.85)
        self.assertEqual(len(store), 0)

    def test_fuzzy_skips_short_titles(self):
        """两字片名不参与模糊匹配，避免「仙逆」撞上「仙逆传」。"""
        store = PendingTitleStore()
        store.remember(text="仙逆")
        self.assertIsNone(store.consume(titles=["仙逆传"], mtype="tv"))
        self.assertEqual(len(store), 1)

    def test_fuzzy_keeps_record_when_not_similar_enough(self):
        """部分同名（相似度不到阈值）不消费记录，宁可漏也不错建。"""
        store = PendingTitleStore()
        store.remember(text="机智医生生活")
        self.assertIsNone(store.consume(titles=["机智医生"], mtype="tv"))
        self.assertEqual(len(store), 1)

    def test_fuzzy_skips_incompatible_type(self):
        """类型不相容（登记为电影、点选为剧集）不命中。"""
        store = PendingTitleStore()
        store.remember(text="机智医生生活", mtype="movie")
        self.assertIsNone(store.consume(titles=["机智的医生生活"], mtype="tv"))

    # ------------------------------------------- 1.6.33 点选兜底（allow_sole）
    def test_sole_fallback_consumes_record_with_unrelated_title(self):
        """点选时片名完全对不上（画江湖 → 画江湖之不良人）也按点选接入。"""
        store = PendingTitleStore()
        store.remember(text="画江湖", userid="1")
        hit = store.consume(titles=["画江湖之不良人"], tmdbid=83463, mtype="tv", allow_sole=True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["text"], "画江湖")
        self.assertEqual(hit["match"], "sole")
        self.assertEqual(hit["match_candidates"], 1)
        self.assertEqual(len(store), 0)

    def test_sole_fallback_is_opt_in(self):
        """默认（订阅搜索等路径）仍要求片名对得上，记录保留。"""
        store = PendingTitleStore()
        store.remember(text="画江湖", mtype="tv")
        self.assertIsNone(store.consume(titles=["画江湖之不良人"], tmdbid=83463, mtype="tv"))
        self.assertEqual(len(store), 1)

    def test_sole_fallback_takes_latest_record(self):
        """多条挂起记录时取最近一条，并留下同表候选条数。"""
        store = PendingTitleStore()
        store.remember(text="仙逆", mtype="tv")
        store.remember(text="画江湖", mtype="tv")
        hit = store.consume(titles=["画江湖之不良人"], mtype="tv", allow_sole=True)
        self.assertEqual(hit["text"], "画江湖")
        self.assertEqual(hit["match"], "sole")
        self.assertEqual(hit["match_candidates"], 2)
        self.assertEqual([item["text"] for item in store.peek()], ["仙逆"])

    def test_sole_fallback_prefers_compatible_type(self):
        """点选兜底优先取类型相容的记录，别被更近的不相容记录抢走。"""
        store = PendingTitleStore()
        store.remember(text="洛基", mtype="movie")
        store.remember(text="画江湖", mtype="tv")
        hit = store.consume(titles=["画江湖之不良人"], mtype="tv", allow_sole=True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["text"], "画江湖")
        self.assertEqual(hit["match"], "sole")
        self.assertEqual(hit["match_candidates"], 1)

    def test_sole_fallback_accepts_incompatible_type(self):
        """1.6.42：登记被后台识别成电影、用户点选的是电视剧时，点选兜底仍要接上。

        线上案例（2026-10-15 11:58）：发「洛基」→ 后台 TMDB 把登记记录补成
        电影 洛基 (2021) 636901，用户点选的是 电视剧 洛基 (2021) 84958 →
        类型硬过滤让四级匹配全不中 → 订阅没建、搜索落到平台、回「未搜索到资源」。
        """
        store = PendingTitleStore()
        store.remember(text="洛基", mtype="movie", tmdbid=636901)
        hit = store.consume(titles=["洛基"], tmdbid=84958, mtype="tv", allow_sole=True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["text"], "洛基")
        self.assertEqual(hit["match"], "sole")
        self.assertEqual(hit["match_candidates"], 1)
        self.assertEqual(len(store), 0)

    def test_incompatible_type_still_keeps_record_for_other_callers(self):
        """非点选路径（订阅搜索、扫描）不变：类型不相容就不消费，记录留到过期。"""
        store = PendingTitleStore()
        store.remember(text="洛基", mtype="movie", tmdbid=636901)
        self.assertIsNone(store.consume(titles=["洛基"], tmdbid=84958, mtype="tv"))
        self.assertEqual(len(store), 1)

    def test_sole_fallback_requires_identity_signal(self):
        """点选项没有任何身份信号时不消费，避免空手接入。"""
        store = PendingTitleStore()
        store.remember(text="画江湖", mtype="tv")
        self.assertIsNone(store.consume(titles=[], tmdbid=None, allow_sole=True))
        self.assertEqual(len(store), 1)

    def test_identity_and_title_still_take_precedence(self):
        """点选兜底只是兜底：身份/同名仍优先，别把另一条记录吃掉。"""
        store = PendingTitleStore()
        store.remember(text="画江湖", mtype="tv")
        store.remember(text="仙逆", tmdbid=223911, mtype="tv")
        hit = store.consume(titles=["仙逆"], tmdbid=223911, mtype="tv", allow_sole=True)
        self.assertEqual(hit["match"], "tmdbid")
        self.assertEqual([item["text"] for item in store.peek()], ["画江湖"])

    def test_sole_fallback_skips_expired_record(self):
        store = PendingTitleStore(ttl=0)
        store.remember(text="画江湖", mtype="tv")
        time.sleep(0.01)
        self.assertIsNone(store.consume(titles=["画江湖之不良人"], mtype="tv", allow_sole=True))


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


    # ------------------------------------------------- 点选候选入口（1.6.28）
    def test_process_wrapper_passes_through_without_pending_hit(self):
        """没有登记候选时，process 原样透传（订阅搜索等调用不受影响）。"""
        self._blocked()
        wrapper = self.hook._create_platform_search_wrapper(
            "process", "pending", fake_process_search
        )
        result = wrapper(object(), mediainfo=FakeMediaInfo(title="别的片子", mtype="tv"))
        self.assertEqual(result, ["contexts"])
        self.assertEqual(self.selected, [])

    def test_process_wrapper_blocks_clicked_candidate(self):
        """点选的那一项（机智的医生生活）要能接上「机智医生生活」这条登记。"""
        self._blocked()
        self.store.remember(text="机智医生生活", mtype="tv")
        wrapper = self.hook._create_platform_search_wrapper(
            "process", "pending", fake_process_search
        )
        self.assertEqual(
            wrapper(
                object(),
                mediainfo=FakeMediaInfo(title="机智的医生生活", mtype="tv", tmdbid=96102),
            ),
            [],
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0][0]["match"], "fuzzy")
        self.assertEqual(len(self.store), 0)

    def test_pending_async_wrapper_blocks(self):
        self._blocked()
        self.store.remember(text="仙逆", mtype="tv")

        async def original(chain, mediainfo=None, **kwargs):
            return ["contexts"]

        wrapper = self.hook._create_platform_search_wrapper(
            "async_process", "pending_async", original
        )
        result = asyncio.run(
            wrapper(object(), mediainfo=FakeMediaInfo(title="仙逆", mtype="tv"))
        )
        self.assertEqual(result, [])
        self._wait_async()
        self.assertEqual(len(self.selected), 1)

    def test_pending_stream_wrapper_blocks(self):
        self._blocked()
        self.store.remember(text="仙逆", mtype="tv")

        async def original(chain, mediainfo=None, **kwargs):
            yield {"type": "done", "items": ["contexts"]}

        wrapper = self.hook._create_platform_search_wrapper(
            "async_process_stream", "pending_stream", original
        )

        async def collect():
            return [event async for event in wrapper(object(), mediainfo=FakeMediaInfo(mtype="tv"))]

        events = asyncio.run(collect())
        self.assertEqual(events[0]["items"], [])
        self._wait_async()
        self.assertEqual(len(self.selected), 1)

    def test_interaction_wrapper_skips_platform_search(self):
        """媒体点选接管器：命中登记就直接接管，不落到平台搜索、也不再回「未搜索到」。"""
        self._blocked()
        self.store.remember(text="机智医生生活", mtype="tv")
        calls = []

        def original(chain_self, request=None, mediainfo=None, *args, **kwargs):
            calls.append(mediainfo)
            return "platform-search"

        wrapper = self.hook._create_interaction_media_wrapper(original)
        request = MagicMock()
        request.meta = None
        result = wrapper(
            object(),
            request=request,
            mediainfo=FakeMediaInfo(title="机智的医生生活", mtype="tv", tmdbid=96102),
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0][1]["tmdbid"], 96102)
        self.assertEqual(len(self.store), 0)

    def test_interaction_wrapper_uses_request_keyword(self):
        """用户原始输入（request.keyword）能直接对上登记，判级为同名。"""
        self._blocked()
        self.store.remember(text="机智医生生活", mtype="tv")
        wrapper = self.hook._create_interaction_media_wrapper(fake_search_media_resources)
        request = MagicMock()
        request.meta = None
        request.keyword = "机智医生生活"
        self.assertIsNone(
            wrapper(
                object(),
                request=request,
                mediainfo=FakeMediaInfo(title="机智的医生生活", mtype="tv", tmdbid=96102),
            )
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0][0]["match"], "title")

    def test_interaction_wrapper_passes_through_other_searches(self):
        self._blocked()
        wrapper = self.hook._create_interaction_media_wrapper(fake_search_media_resources)
        request = MagicMock()
        request.meta = None
        self.assertEqual(
            wrapper(
                object(),
                request=request,
                mediainfo=FakeMediaInfo(title="别的片子", mtype="tv"),
            ),
            "platform-search",
        )
        self.assertEqual(self.selected, [])

    # --------------------------------------- 点选兜底（1.6.33）
    def test_interaction_wrapper_uses_sole_fallback(self):
        """登记名与点选项对不上（画江湖 → 画江湖之不良人）时，按点选项接入。"""
        self._blocked()
        self.store.remember(text="画江湖")
        wrapper = self.hook._create_interaction_media_wrapper(fake_search_media_resources)
        request = MagicMock()
        request.meta = None
        self.assertIsNone(
            wrapper(
                object(),
                request=request,
                mediainfo=FakeMediaInfo(title="画江湖之不良人", mtype="tv", tmdbid=83463),
            )
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        record, identity = self.selected[0]
        self.assertEqual(record["text"], "画江湖")
        self.assertEqual(record["match"], "sole")
        # 订阅用的是「点选的那一项」的信息
        self.assertEqual(identity["tmdbid"], 83463)
        self.assertEqual(identity["title"], "画江湖之不良人")
        self.assertEqual(len(self.store), 0)

    def test_interaction_wrapper_handles_type_mismatched_registration(self):
        """1.6.42：登记被后台识别成电影、点选电视剧时，点选接入器仍要建订阅。

        复刻 2026-10-15 11:58 的线上案例（洛基）：登记记录 enrich 成
        电影 洛基 (2021) 636901，用户点选 电视剧 洛基 (2021) 84958。
        """
        self._blocked()
        record = self.store.remember(text="洛基")
        self.store.enrich(
            record["id"], title="洛基", year="2021", mtype="movie", tmdbid=636901
        )
        wrapper = self.hook._create_interaction_media_wrapper(fake_search_media_resources)
        request = MagicMock()
        request.meta = None
        request.keyword = "洛基"
        self.assertIsNone(
            wrapper(
                object(),
                request=request,
                mediainfo=FakeMediaInfo(
                    title="洛基", year="2021", mtype="tv", tmdbid=84958
                ),
            )
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        record, identity = self.selected[0]
        self.assertEqual(record["match"], "sole")
        # 订阅按「点选的那一项」建（电视剧 84958），登记记录只决定回执渠道
        self.assertEqual(identity["tmdbid"], 84958)
        self.assertEqual(identity["title"], "洛基")
        self.assertEqual(len(self.store), 0)

    def test_interaction_wrapper_sole_fallback_needs_record(self):
        """登记表为空时不猜、不接（点选照旧落到平台搜索）。"""
        self._blocked()
        wrapper = self.hook._create_interaction_media_wrapper(fake_search_media_resources)
        request = MagicMock()
        request.meta = None
        self.assertEqual(
            wrapper(
                object(),
                request=request,
                mediainfo=FakeMediaInfo(title="画江湖之不良人", mtype="tv", tmdbid=83463),
            ),
            "platform-search",
        )
        self.assertEqual(self.selected, [])

    def test_process_wrapper_keeps_record_without_title_match(self):
        """process 系列（订阅搜索/扫描也走）不参与点选兜底，避免误建订阅。"""
        self._blocked()
        self.store.remember(text="画江湖", mtype="tv")
        wrapper = self.hook._create_platform_search_wrapper(
            "process", "pending", fake_process_search
        )
        result = wrapper(
            object(),
            mediainfo=FakeMediaInfo(title="画江湖之不良人", mtype="tv", tmdbid=83463),
        )
        self.assertEqual(result, ["contexts"])
        self.assertEqual(self.selected, [])
        self.assertEqual(len(self.store), 1)

    # ------------------------- 点选调用兜底：认参数形状（1.6.34）
    def test_process_wrapper_treats_selection_call_as_click(self):
        """点选那次 process 调用（只有 mediainfo + no_exists）允许走点选兜底。

        线上事故复现：平台点选后是 InteractionChain._search_media_resources →
        SearchChain().process(mediainfo=…, no_exists=…)，点选接入器一旦没装上，
        这条调用就是唯一的落点。
        """
        self._blocked()
        self.store.remember(text="画江湖")
        wrapper = self.hook._create_platform_search_wrapper(
            "process", "pending", fake_process_search
        )
        result = wrapper(
            object(),
            mediainfo=FakeMediaInfo(title="画江湖之不良人", mtype="tv", tmdbid=83463),
            no_exists={"S1": {1: None}},
        )
        self.assertEqual(result, [])
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        record, identity = self.selected[0]
        self.assertEqual(record["text"], "画江湖")
        self.assertEqual(record["match"], "sole")
        self.assertEqual(identity["tmdbid"], 83463)
        self.assertEqual(len(self.store), 0)

    def test_process_wrapper_keeps_subscription_search_strict(self):
        """订阅搜索的 process 调用带 keyword/sites，仍要求片名对得上。"""
        self._blocked()
        self.store.remember(text="画江湖", mtype="tv")
        wrapper = self.hook._create_platform_search_wrapper(
            "process", "pending", fake_process_search
        )
        result = wrapper(
            object(),
            mediainfo=FakeMediaInfo(title="画江湖之不良人", mtype="tv", tmdbid=83463),
            keyword="画江湖之不良人",
            sites=[1],
            rule_groups=["高清"],
            no_exists={"S1": {1: None}},
        )
        self.assertEqual(result, ["contexts"])
        self.assertEqual(self.selected, [])
        self.assertEqual(len(self.store), 1)

    def test_selection_call_shape_detection(self):
        """参数形状判定：只有 no_exists 才算点选，缺 no_exists 一律不算。"""
        self.assertTrue(
            self.hook._looks_like_media_selection({"mediainfo": object(), "no_exists": {"S1": {}}})
        )
        self.assertFalse(self.hook._looks_like_media_selection({"mediainfo": object()}))
        self.assertFalse(
            self.hook._looks_like_media_selection(
                {"mediainfo": object(), "no_exists": {"S1": {}}, "sites": [1]}
            )
        )

    # ------------------------- 点选接入器挂载（1.6.34）
    def test_interaction_hook_binds_renamed_platform_class(self):
        """平台把 InteractionChain 改名成 MediaInteractionChain 后，点选接入器照样装上。"""
        self._blocked()
        register_module(
            "app.chain.interaction", MediaInteractionChain=FakeMediaInteractionChain
        )
        self.hook._install_interaction_media_hook()
        self.assertIs(self.hook._interaction_chain_class, FakeMediaInteractionChain)
        self.assertIs(
            FakeMediaInteractionChain._search_media_resources.__cloudsubscribe_owner__,
            self.hook,
        )
        # 装上之后，点选（登记名与点选项不一致）直接接入订阅，不再落到平台搜索
        self.store.remember(text="画江湖")
        request = MagicMock()
        request.meta = None
        self.assertIsNone(
            FakeMediaInteractionChain()._search_media_resources(
                request=request,
                mediainfo=FakeMediaInfo(title="画江湖之不良人", mtype="tv", tmdbid=83463),
            )
        )
        self._wait_async()
        self.assertEqual(len(self.selected), 1)
        self.assertEqual(self.selected[0][0]["match"], "sole")

    def test_interaction_hook_prefers_renamed_class(self):
        """两个名字都在时，优先用平台当前的名字（MediaInteractionChain）。"""
        register_module(
            "app.chain.interaction",
            MediaInteractionChain=FakeMediaInteractionChain,
            InteractionChain=type("InteractionChain", (), {}),
        )
        target, reason = subscription_module.resolve_interaction_chain_class()
        self.assertIs(target, FakeMediaInteractionChain)
        self.assertEqual(reason, "MediaInteractionChain")

    def test_interaction_hook_reports_missing_platform_class(self):
        """找不到承载点选的类时，返回原因且安装不抛异常（日志里有 WARNING）。"""
        register_module("app.chain.interaction", OtherChain=type("OtherChain", (), {}))
        target, reason = subscription_module.resolve_interaction_chain_class()
        self.assertIsNone(target)
        self.assertIn("_search_media_resources", reason)
        self.hook._install_interaction_media_hook()

    def test_interaction_hook_scans_module_for_unknown_class_name(self):
        """平台再次改名时按方法扫描兜底，不会因为名字变了就静默失效。"""
        register_module(
            "app.chain.interaction",
            WhateverInteractionChain=type(
                "WhateverInteractionChain",
                (),
                {"_search_media_resources": lambda self, **kwargs: None},
            ),
        )
        target, reason = subscription_module.resolve_interaction_chain_class()
        self.assertIsNotNone(target)
        self.assertEqual(target.__name__, "WhateverInteractionChain")
        self.assertIn("扫描", reason)


if __name__ == "__main__":
    unittest.main()
