"""RankingProvider 模板基类回归测试（v1.22.0）。

覆盖 core/subscribe/provider.py 的 _paginate / _resolve_media_type /
_candidate_from_dict，以及 anilist / bangumi / douban / tmdb 四家
fetch() 在固定 fake 数据下的精确产出（与重构前逐行核对过的旧逻辑
一致：标题/年份/评分过滤、id 解析、unique_key 去重、source_meta、
media_type 解析）。
"""
import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    REPO_ROOT,
    install_app_mocks,
    load_module,
    register_package,
)

install_app_mocks(
    "app.chain",
    "app.chain.anilist",
    "app.chain.recommend",
    "app.chain.download",
    "app.chain.media",
    "app.chain.subscribe",
    "app.helper",
    "app.helper.mediaserver",
    "app.application",
    "app.application.mediaserver",
    "app.db",
    "app.db.site_oper",
    "app.db.subscribe_oper",
    "app.db.systemconfig_oper",
    "app.core.config",
    "app.core.context",
    "app.core.metainfo",
)

register_package("cloudsubscribex", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex")])
register_package("cloudsubscribex.core", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core")])
register_package("cloudsubscribex.core.subscribe",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core/subscribe")])
register_package("cloudsubscribex.subscribe",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe")])
register_package("cloudsubscribex.subscribe.anilist",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe/anilist")])
register_package("cloudsubscribex.subscribe.bangumi",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe/bangumi")])
register_package("cloudsubscribex.subscribe.douban",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe/douban")])
register_package("cloudsubscribex.subscribe.tmdb",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe/tmdb")])
register_package("cloudsubscribex.search", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/search")])
register_package("cloudsubscribex.utils", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/utils")])

_models_module = load_module(
    "cloudsubscribex.core.subscribe.models", "plugins.v2/cloudsubscribex/core/subscribe/models.py"
)
_provider_module = load_module(
    "cloudsubscribex.core.subscribe.provider", "plugins.v2/cloudsubscribex/core/subscribe/provider.py"
)
load_module("cloudsubscribex.core.subscribe.registry", "plugins.v2/cloudsubscribex/core/subscribe/registry.py")

# 各 provider 用 `from ...core.subscribe import MediaCandidate, ...` 取父包
# 属性；父包是 AutoMockModule，不贴回真实类会拿到 MagicMock。这里按
# 既定做法把已加载的真实符号贴回父包上。
_subscribe_pkg = sys.modules["cloudsubscribex.core.subscribe"]
_subscribe_pkg.MediaCandidate = _models_module.MediaCandidate
_subscribe_pkg.SubscribeContext = _provider_module.SubscribeContext
_subscribe_pkg.SubscribeProvider = _provider_module.SubscribeProvider
_subscribe_pkg.RankingProvider = _provider_module.RankingProvider

RankingProvider = _provider_module.RankingProvider
SubscribeContext = _provider_module.SubscribeContext

# _resolve_media_type 用的 MediaType 桩：只在基类模块命名空间里替换，
# 不动 sys.modules 里的 app.schemas.types。
_MediaTypeStub = SimpleNamespace(
    MOVIE=SimpleNamespace(value="movie"),
    TV=SimpleNamespace(value="tv"),
)
_provider_module.MediaType = _MediaTypeStub

_anilist_module = load_module(
    "cloudsubscribex.subscribe.anilist.provider",
    "plugins.v2/cloudsubscribex/subscribe/anilist/provider.py",
)
_bangumi_module = load_module(
    "cloudsubscribex.subscribe.bangumi.provider",
    "plugins.v2/cloudsubscribex/subscribe/bangumi/provider.py",
)
_douban_module = load_module(
    "cloudsubscribex.subscribe.douban.provider",
    "plugins.v2/cloudsubscribex/subscribe/douban/provider.py",
)
_tmdb_module = load_module(
    "cloudsubscribex.subscribe.tmdb.provider",
    "plugins.v2/cloudsubscribex/subscribe/tmdb/provider.py",
)


class _ConcreteRanking(RankingProvider):
    def spec(self):
        return {"id": "test"}

    def fetch(self, options, context):
        return iter(())


def _context(stopped=False):
    event = threading.Event()
    if stopped:
        event.set()
    return SubscribeContext(event=event)


class PaginateTests(unittest.TestCase):
    def setUp(self):
        self.provider = _ConcreteRanking()
        self.context = _context()

    def test_first_page_failure_reraises(self):
        def fetch_page(page):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.provider._paginate(fetch_page, 10, self.context, "榜单[X]", "tmdb_id")

    def test_later_page_failure_keeps_items(self):
        pages = {1: [{"tmdb_id": 1, "title": "A"}], 2: RuntimeError("boom")}

        def fetch_page(page):
            value = pages.get(page, [])
            if isinstance(value, Exception):
                raise value
            return value

        items = self.provider._paginate(fetch_page, 10, self.context, "榜单[X]", "tmdb_id")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "A")

    def test_empty_page_stops(self):
        calls = []

        def fetch_page(page):
            calls.append(page)
            return [{"tmdb_id": 1, "title": "A"}] if page == 1 else []

        items = self.provider._paginate(fetch_page, 10, self.context, "榜单[X]", "tmdb_id")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(items), 1)

    def test_repeated_page_keys_stop_loop(self):
        page_items = [{"tmdb_id": 1, "title": "A"}, {"tmdb_id": 2, "title": "B"}]
        calls = []

        def fetch_page(page):
            calls.append(page)
            return list(page_items)

        items = self.provider._paginate(fetch_page, 10, self.context, "榜单[X]", "tmdb_id")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(items), 2)

    def test_scan_limit_truncation(self):
        def fetch_page(page):
            return [{"tmdb_id": i, "title": f"T{i}"} for i in range((page - 1) * 5, page * 5)]

        items = self.provider._paginate(fetch_page, 7, self.context, "榜单[X]", "tmdb_id")
        self.assertEqual(len(items), 7)
        self.assertEqual(items[-1]["title"], "T6")

    def test_stopped_context_returns_none(self):
        def fetch_page(page):
            return [{"tmdb_id": 1, "title": "A"}]

        self.assertIsNone(self.provider._paginate(fetch_page, 10, _context(stopped=True), "榜单[X]", "tmdb_id"))

    def test_stopped_mid_loop_returns_none(self):
        event = threading.Event()
        context = SubscribeContext(event=event)

        def fetch_page(page):
            event.set()
            return [{"tmdb_id": page, "title": f"T{page}"}]

        self.assertIsNone(self.provider._paginate(fetch_page, 10, context, "榜单[X]", "tmdb_id"))


class ResolveMediaTypeTests(unittest.TestCase):
    def test_movie_and_tv_sets(self):
        self.assertEqual(
            RankingProvider._resolve_media_type({"type": "movie"}, "tv", set(), ""), "movie"
        )
        self.assertEqual(
            RankingProvider._resolve_media_type({"type": "电视剧"}, "movie", set(), ""), "tv"
        )
        self.assertEqual(
            RankingProvider._resolve_media_type({}, "movie", set(), ""), "movie"
        )
        self.assertEqual(
            RankingProvider._resolve_media_type({"type": "未知"}, "tv", set(), ""), "tv"
        )

    def test_selected_types_filter(self):
        self.assertIsNone(
            RankingProvider._resolve_media_type({"type": "movie"}, "movie", {"tv"}, "")
        )
        self.assertEqual(
            RankingProvider._resolve_media_type({"type": "movie"}, "movie", {"movie"}, ""),
            "movie",
        )
        # 含 all 时不按 selected_types 过滤
        self.assertEqual(
            RankingProvider._resolve_media_type({"type": "movie"}, "movie", {"all"}, ""),
            "movie",
        )

    def test_limit_type_filter(self):
        self.assertIsNone(
            RankingProvider._resolve_media_type({"type": "movie"}, "movie", set(), "tv")
        )
        self.assertEqual(
            RankingProvider._resolve_media_type({"type": "movie"}, "movie", set(), "all"),
            "movie",
        )


class CandidateFromDictTests(unittest.TestCase):
    def setUp(self):
        self.provider = _ConcreteRanking()
        self.seen: set = set()

    def _call(self, data, **kwargs):
        params = dict(
            min_year=0,
            min_vote=0.0,
            seen_keys=self.seen,
            media_type="tv",
            source="anilist",
            rank="本季热门",
            rank_key="popular_this_season",
            id_fields=(("anilist_id", "int"), ("tmdb_id", "int")),
            unique_key_fields=("anilist_id", "tmdb_id"),
        )
        params.update(kwargs)
        return self.provider._candidate_from_dict(data, **params)

    def test_empty_title_skipped(self):
        self.assertIsNone(self._call({"title": "   ", "year": "2024"}))
        self.assertIsNone(self._call({"name": "", "year": "2024"}))

    def test_year_filter_and_bad_year_kept(self):
        self.assertIsNone(self._call({"title": "A", "year": "2020"}, min_year=2021))
        kept = self._call({"title": "B", "year": "not-a-year"}, min_year=2021)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.year, "not-a-year")

    def test_vote_filter_and_bad_vote_zero(self):
        self.assertIsNone(self._call({"title": "A", "year": "2024", "vote_average": 5.0}, min_vote=7.0))
        kept = self._call({"title": "B", "year": "2024", "vote": "bad"})
        self.assertIsNotNone(kept)
        self.assertEqual(kept.vote_average, 0.0)

    def test_id_parse_failure_and_unique_key_dedup(self):
        first = self._call({"title": "A", "year": "2024", "anilist_id": "bad", "tmdb_id": "xyz"})
        self.assertIsNotNone(first)
        self.assertIsNone(first.tmdb_id)
        self.assertEqual(first.source_meta["anilist_id"], None)
        self.assertEqual(first.unique_seed, "A:2024")
        # 同标题同年份同 id 再次出现即去重（unique_key 含 id，换 id 即不同键）
        self.assertIsNone(self._call({"title": "A", "year": "2024", "anilist_id": "bad", "tmdb_id": "xyz"}))

    def test_douban_str_id_and_meta_order(self):
        candidate = self._call(
            {"title": "D", "year": "2023", "douban_id": "  12345 ", "tmdb_id": 77},
            id_fields=(("douban_id", "str"), ("tmdb_id", "int")),
            unique_key_fields=("douban_id", "tmdb_id"),
            source="douban",
        )
        self.assertEqual(candidate.douban_id, "12345")
        self.assertEqual(candidate.tmdb_id, 77)
        self.assertEqual(candidate.unique_seed, "12345:2023")
        self.assertEqual(candidate.source_meta["rank_key"], "popular_this_season")


class _FakeChain:
    """按方法名返回固定分页数据；page 从 1 开始。"""

    def __init__(self, pages_by_method):
        self._pages = pages_by_method
        self.calls = []

    def __getattr__(self, name):
        def _method(**kwargs):
            self.calls.append((name, kwargs))
            pages = self._pages.get(name, [])
            page = kwargs.get("page", 1)
            if page > len(pages):
                return []
            return list(pages[page - 1])

        return _method


def _candidate_tuple(candidate):
    return (
        candidate.title,
        candidate.year,
        candidate.media_type,
        candidate.tmdb_id,
        candidate.douban_id,
        candidate.bangumi_id,
        candidate.unique_seed,
        candidate.source_meta,
        candidate.vote_average,
    )


class ProviderFetchTests(unittest.TestCase):
    def test_anilist_fetch_exact_candidates(self):
        chain = _FakeChain({
            "popular_this_season": [
                [
                    {"title": "Show A", "year": "2024", "anilist_id": 11, "tmdb_id": 101,
                    "vote_average": 8.5, "release_date": "2024-01-01", "first_air_date": "2024-01-01"},
                    {"title": "Show B", "year": "2020", "anilist_id": 12, "tmdb_id": 102,
                    "vote_average": 5.0},
                    {"title": "Show A", "year": "2024", "anilist_id": 11, "tmdb_id": 101,
                    "vote_average": 8.5},
                    {"title": "   ", "year": "2024", "anilist_id": 13},
                    {"title": "Show C", "year": "not-a-year", "anilist_id": "bad",
                    "tmdb_id": "xyz", "vote": "bad"},
                ],
                [],
            ],
            "trending": [
                [
                    {"title": "Show D", "year": "2023", "anilist_id": 14, "tmdb_id": 104,
                    "vote_average": 9.0},
                ],
            ],
        })
        provider = _anilist_module.AnilistSubscribeProvider(chain=chain)
        results = list(provider.fetch(
            {"ranks": ["popular_this_season", "trending"], "min_vote": 7.0, "min_year": 2021, "limit": 30},
            _context(),
        ))
        self.assertEqual([c.title for c in results], ["Show A", "Show C", "Show D"])
        first = results[0]
        self.assertEqual(first.tmdb_id, 101)
        self.assertEqual(first.unique_seed, "11:2024")
        self.assertEqual(first.source_meta, {
            "rank": "AniList本季热门",
            "rank_key": "popular_this_season",
            "release_date": "2024-01-01",
            "first_air_date": "2024-01-01",
            "anilist_id": 11,
        })
        self.assertEqual(first.vote_average, 8.5)
        # 旧逻辑里 anilist_id 解析失败、年份非法、评分非法都保留，id 为 None
        third = results[1]
        self.assertIsNone(third.tmdb_id)
        self.assertEqual(third.unique_seed, "Show C:not-a-year")
        self.assertEqual(third.vote_average, 0.0)
        # 分页调用带 count=limit
        self.assertIn(("popular_this_season", {"page": 1, "count": 30}), chain.calls)

    def test_bangumi_fetch_exact_candidates(self):
        chain = _FakeChain({
            "bangumi_calendar": [
                [
                    {"title": "番 A", "year": "2024", "bangumi_id": 21, "vote_average": 8.0,
                    "air_date": "2024-04-01"},
                    {"title": "番 B", "year": "2019", "bangumi_id": 22, "vote_average": 9.0},
                ],
                [
                    {"title": "番 A", "year": "2024", "bangumi_id": 21, "vote_average": 8.0},
                ],
            ],
        })
        provider = _bangumi_module.BangumiSubscribeProvider(chain=chain)
        results = list(provider.fetch(
            {"min_vote": 0.0, "min_year": 2021, "limit": 50}, _context(),
        ))
        self.assertEqual([c.title for c in results], ["番 A"])
        first = results[0]
        self.assertEqual(first.bangumi_id, 21)
        self.assertEqual(first.unique_seed, "21:2024")
        self.assertEqual(first.source_meta["rank"], "每日放送")
        self.assertEqual(first.source_meta["air_date"], "2024-04-01")
        self.assertNotIn("rank_key", first.source_meta)

    def test_douban_fetch_media_type_and_str_id(self):
        chain = _FakeChain({
            "douban_movie_showing": [
                [
                    {"title": "电影甲", "year": "2024", "type": "movie", "douban_id": "1001",
                    "tmdb_id": 201, "vote_average": 8.0},
                    {"title": "剧甲", "year": "2024", "type": "tv", "douban_id": "1002",
                    "tmdb_id": 202, "vote_average": 8.0},
                    {"title": "电影乙", "year": "2024", "type": "电影", "douban_id": 1003,
                    "tmdb_id": 203, "vote_average": 8.0},
                ],
            ],
            "douban_tv_hot": [
                [
                    {"title": "剧乙", "year": "2024", "type": "teleplay", "douban_id": "1004",
                    "tmdb_id": 204, "vote_average": 8.0},
                ],
            ],
        })
        provider = _douban_module.DoubanSubscribeProvider(chain=chain)
        # 只勾选电影：剧集被类型过滤拦掉；douban_id 是字符串
        results = list(provider.fetch(
            {"ranks": ["movie-showing", "tv-hot"], "media_types": ["movie"], "limit": 30},
            _context(),
        ))
        self.assertEqual([c.title for c in results], ["电影甲", "电影乙"])
        self.assertTrue(all(c.media_type == "movie" for c in results))
        self.assertEqual(results[0].douban_id, "1001")
        self.assertEqual(results[0].unique_seed, "1001:2024")
        self.assertEqual(results[1].douban_id, "1003")
        # 不限类型：四部全出
        results_all = list(provider.fetch(
            {"ranks": ["movie-showing", "tv-hot"], "limit": 30}, _context(),
        ))
        self.assertEqual([c.title for c in results_all], ["电影甲", "剧甲", "电影乙", "剧乙"])
        self.assertEqual(results_all[1].media_type, "tv")
        self.assertEqual(results_all[3].media_type, "tv")

    def test_tmdb_fetch_no_count_kwarg_and_type_fallback(self):
        chain = _FakeChain({
            "tmdb_trending": [
                [
                    {"title": "Film X", "year": "2024", "type": "movie", "tmdb_id": 301,
                    "vote_average": 7.5},
                    {"title": "Show Y", "year": "2024", "tmdb_id": 302, "vote_average": 7.5},
                ],
            ],
        })
        provider = _tmdb_module.TmdbSubscribeProvider(chain=chain)
        results = list(provider.fetch(
            {"ranks": ["trending"], "limit": 20}, _context(),
        ))
        self.assertEqual([c.title for c in results], ["Film X", "Show Y"])
        self.assertEqual(results[0].media_type, "movie")
        # 无 type 时回落到榜单默认类型 movie（trending 的 default_type）
        self.assertEqual(results[1].media_type, "movie")
        self.assertEqual(results[1].unique_seed, "302:2024")
        # TMDB 分页调用不带 count
        for _, kwargs in chain.calls:
            self.assertNotIn("count", kwargs)
            self.assertIn("page", kwargs)

    def test_unknown_rank_and_missing_method_skipped(self):
        chain = _FakeChain({})
        provider = _douban_module.DoubanSubscribeProvider(chain=chain)
        results = list(provider.fetch({"ranks": ["no-such-rank"], "limit": 30}, _context()))
        self.assertEqual(results, [])
        # 方法不存在时跳过该榜单
        provider2 = _tmdb_module.TmdbSubscribeProvider(chain=MagicMock())
        provider2._chain = SimpleNamespace()
        results2 = list(provider2.fetch({"ranks": ["trending"], "limit": 20}, _context()))
        self.assertEqual(results2, [])


if __name__ == "__main__":
    unittest.main()
