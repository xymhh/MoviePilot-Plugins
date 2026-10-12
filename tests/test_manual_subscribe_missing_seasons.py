"""手动订阅按媒体库缺集补齐所有季（S00 除外）的用例。

本文件用**真实**的 ``core/subscribe/seasons.py`` 逻辑 + 假媒体库
（``DownloadChain.get_no_exists_info``）验证挂钩行为，调用签名严格对齐
MoviePilot 的 ``SubscribeChain.add``（片名选定路径只传 media_source / media_id，
不传 mediainfo）。
"""
import asyncio
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

MediaType = set_media_type()

SHOW_SEASONS = {0: [1, 2], 1: [1, 2, 3], 2: [1, 2, 3], 3: [1, 2]}


class NotExist:
    def __init__(self, season, episodes):
        self.season = season
        self.episodes = episodes


class MetaInfoStub:
    def __init__(self, title=""):
        self.title = title
        self.year = None
        self.type = None
        self.begin_season = None


class MediaStub:
    """最小 MediaInfo 替身：季集映射 + 媒体身份。"""

    def __init__(self, seasons, media_type=None, media_source="themoviedb", media_id="42"):
        self.type = media_type or MediaType.TV
        self.title = "仙逆"
        self.year = "2023"
        self.media_source = media_source
        self.media_id = media_id
        self.seasons = dict(seasons)
        self.season = None


class FakeDownloadChain:
    """用 ``library`` 夹具模拟媒体库：季 -> 已有集号集合。"""

    library = {}

    def get_no_exists_info(self, meta=None, mediainfo=None, **kwargs):
        season = int(getattr(mediainfo, "season", 0) or 0)
        total = sorted(dict(getattr(mediainfo, "seasons", {}) or {}).get(season, []))
        if season not in self.library:
            # MoviePilot 对「媒体库完全没有该季」返回空集号列表
            return False, {"themoviedb:42": {season: NotExist(season, [])}}
        missing = [episode for episode in total if episode not in set(self.library[season])]
        if not missing:
            return True, {}
        return False, {"themoviedb:42": {season: NotExist(season, missing)}}


class FakeMediaChain:
    seasons = {}
    calls = 0

    def recognize_media(self, mtype=None, media_source=None, media_id=None, cache=True, **kwargs):
        type(self).calls += 1
        if not media_source or not media_id or not self.seasons:
            return None
        return MediaStub(self.seasons)


register_mock("app")
register_mock("app.log", logger=MagicMock())
register_module("app.core.metainfo", MetaInfo=MetaInfoStub)
register_module("app.chain.download", DownloadChain=FakeDownloadChain)
register_module("app.chain.media", MediaChain=FakeMediaChain)

CALLS = []


class FakeSubscribeChain:
    """签名对齐 MoviePilot v3 的 ``SubscribeChain.add``。"""

    def add(
        self,
        title,
        year="",
        mtype=None,
        episode_group=None,
        season=None,
        channel=None,
        source=None,
        userid=None,
        username=None,
        message=True,
        exist_ok=False,
        media_source=None,
        media_id=None,
        **kwargs,
    ):
        CALLS.append(
            {
                "title": title,
                "mtype": mtype,
                "season": season,
                "message": message,
                "media_source": media_source,
                "media_id": media_id,
                "kwargs": dict(kwargs),
            }
        )
        return (len(CALLS), "ok")

    async def async_add(self, title, year="", mtype=None, **kwargs):
        return self.add(title=title, year=year, mtype=mtype, **kwargs)


register_mock("app.chain.subscribe", SubscribeChain=FakeSubscribeChain)
register_mock("app.db.subscribe_oper", SubscribeOper=MagicMock())
register_mock("cloudsubscribe")
register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.core.hook")

load_module(
    "cloudsubscribe.core.pending_titles",
    "plugins.v2/cloudsubscribe/core/pending_titles.py",
)
seasons_module = load_module(
    "cloudsubscribe.core.subscribe.seasons",
    "plugins.v2/cloudsubscribe/core/subscribe/seasons.py",
)
hook_module = load_module(
    "cloudsubscribe.core.hook.subscription",
    "plugins.v2/cloudsubscribe/core/hook/subscription.py",
)


class ManualSubscribeExpansionTest(unittest.TestCase):
    def setUp(self):
        CALLS.clear()
        FakeMediaChain.calls = 0
        FakeMediaChain.seasons = dict(SHOW_SEASONS)
        # S1 完整、S2 缺 2-3 集、S3 媒体库完全没有
        FakeDownloadChain.library = {1: {1, 2, 3}, 2: {1}}
        self.owner = MagicMock()
        self.owner._subscribe_all_missing_seasons = True
        self.owner._subscribe_skip_season_zero = True
        self.owner._subscribe_season_limit = 0
        self.hook = hook_module.SubscriptionSearchHook(self.owner)
        self.hook._subscribe_chain_originals = {}
        self.hook._install_subscribe_add_expansion()
        self.addCleanup(self.hook._restore_subscribe_chain_takeover)

    def call(self, **overrides):
        params = {
            "title": "仙逆",
            "year": "2023",
            "mtype": MediaType.TV,
            "media_source": "themoviedb",
            "media_id": "42",
            "season": None,
            "channel": None,
            "source": "",
            "userid": "1",
            "username": "me",
            "state": "R",
            "note": None,
            "music_type": None,
        }
        params.update(overrides)
        return FakeSubscribeChain().add(**params)

    # 1) 真实签名（无 mediainfo）：按缺集季建多条订阅，后续季不再重复通知
    def test_expands_missing_seasons_and_silences_followups(self):
        self.assertEqual(self.call(), (1, "ok"))
        self.assertEqual([call["season"] for call in CALLS], [2, 3])
        self.assertEqual([call["message"] for call in CALLS], [True, False])
        self.assertEqual(FakeMediaChain.calls, 1)

    # 2) 关掉「跳过第 0 季」时把特辑也算进来（媒体库缺 S00）
    def test_skip_season_zero_switch_can_include_zero(self):
        self.owner._subscribe_skip_season_zero = False
        FakeDownloadChain.library = {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {1, 2}}
        self.call()
        self.assertEqual([call["season"] for call in CALLS], [0])

    # 3) 调用方显式指定季号时尊重原参数
    def test_explicit_season_is_respected(self):
        self.call(season=2)
        self.assertEqual([call["season"] for call in CALLS], [2])
        self.assertEqual(FakeMediaChain.calls, 0)

    # 4) 电影不展开
    def test_movie_is_not_expanded(self):
        self.call(mtype=MediaType.MOVIE)
        self.assertEqual(len(CALLS), 1)
        self.assertIsNone(CALLS[0]["season"])

    # 5) 拿不到季集信息时按原样透传（只有一次调用，且不抛异常）
    def test_missing_season_info_falls_back_to_single_call(self):
        FakeMediaChain.seasons = {}
        self.call()
        self.assertEqual(len(CALLS), 1)
        self.assertIsNone(CALLS[0]["season"])

    # 6) 开关关闭 → 保持原行为
    def test_switch_off_disables_expansion(self):
        self.owner._subscribe_all_missing_seasons = False
        self.call()
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(FakeMediaChain.calls, 0)

    # 7) 季数上限
    def test_limit_caps_expanded_seasons(self):
        self.owner._subscribe_season_limit = 1
        self.call()
        self.assertEqual([call["season"] for call in CALLS], [2])

    # 8) 抑制态（插件自身榜单订阅）内不二次展开
    def test_suppressed_context_disables_expansion(self):
        with hook_module.suppress_season_expansion():
            self.call()
        self.assertEqual(len(CALLS), 1)
        self.assertIsNone(CALLS[0]["season"])

    # 9) 所有季都完整 → 不展开，保持原调用一次
    def test_complete_library_keeps_original_call(self):
        FakeDownloadChain.library = {0: {1, 2}, 1: {1, 2, 3}, 2: {1, 2, 3}, 3: {1, 2}}
        result = self.call()
        self.assertEqual(result, (1, "ok"))
        self.assertEqual(len(CALLS), 1)
        self.assertIsNone(CALLS[0]["season"])

    # 10) 兼容带 mediainfo 的内部调用（不重复识别）
    def test_mediainfo_argument_skips_recognition(self):
        self.call(mediainfo=MediaStub(SHOW_SEASONS))
        self.assertEqual([call["season"] for call in CALLS], [2, 3])
        self.assertEqual(FakeMediaChain.calls, 0)

    # 11) 异步入口同样展开
    def test_async_add_expands(self):
        result = asyncio.run(
            FakeSubscribeChain().async_add(
                title="仙逆",
                year="2023",
                mtype=MediaType.TV,
                media_source="themoviedb",
                media_id="42",
                season=None,
            )
        )
        self.assertEqual(result, (1, "ok"))
        self.assertEqual([call["season"] for call in CALLS], [2, 3])


class ResolveSubscribeSeasonsTest(unittest.TestCase):
    def setUp(self):
        FakeDownloadChain.library = {1: {1, 2, 3}, 2: {1}}
        FakeMediaChain.seasons = dict(SHOW_SEASONS)
        FakeMediaChain.calls = 0

    def test_resolve_returns_empty_without_identity(self):
        self.assertEqual(seasons_module.resolve_subscribe_seasons(None), [])

    def test_resolve_recognizes_identity_and_filters_complete_seasons(self):
        result = seasons_module.resolve_subscribe_seasons(
            None, media_source="themoviedb", media_id="42"
        )
        self.assertEqual(result, [2, 3])
        self.assertEqual(FakeMediaChain.calls, 1)

    def test_resolve_skips_season_zero_by_default(self):
        FakeDownloadChain.library = {}
        result = seasons_module.resolve_subscribe_seasons(
            MediaStub(SHOW_SEASONS)
        )
        self.assertEqual(result, [1, 2, 3])
        self.assertNotIn(0, result)


if __name__ == "__main__":
    unittest.main()
