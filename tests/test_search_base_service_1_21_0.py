"""搜索源瘦身 v1.21.0：BaseMagnetSearchService 与 DelegatingSearchService。

用假客户端覆盖基类 search() 循环：关键词上限 3、info_hash 去重、季号/
年份/标题过滤、上限截断、piratebay 传 cat=200、uindex 出错记 warning 后
继续、clear_cache 委托；以及薄门面源的委托：juying 多传 tmdb/douban/imdb，
woniu 不传，resolve/clear_cache 均委托给资源服务。
"""

import re
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugin_env import (
    install_app_mocks,
    install_real_ttl_cache,
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

install_app_mocks(
    "app.log",
    "app.core",
    "app.core.config",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.search",
    "cloudsubscribex.utils",
)
MediaType = set_media_type(movie="movie", tv="tv")


def _test_metainfo(title=None, *args, **kwargs):
    """与真实 utils/magnet 兼容的 MetaInfo 桩：接受 title= 关键字。"""
    text = str(title if title is not None else (args[0] if args else "") or "")
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    season = int(match.group(1)) if match else None
    return SimpleNamespace(
        begin_season=season,
        season_list=[season] if season is not None else [],
        episode_list=[int(match.group(2))] if match else [],
    )

_SAVED_MODULES = {}
for _name in (
    "cloudsubscribex.core.search",
    "cloudsubscribex.search.matching",
    "cloudsubscribex.search.magnet",
    "cloudsubscribex.utils.magnet",
    "cloudsubscribex.search.base",
    "cloudsubscribex.search.piratebay.client",
    "cloudsubscribex.search.piratebay.service",
    "cloudsubscribex.search.uindex.client",
    "cloudsubscribex.search.uindex.service",
    "cloudsubscribex.search.juying.service",
    "cloudsubscribex.search.woniu.service",
    "cloudsubscribex.core.media",
    "app.core.metainfo",
    "torf",
):
    _SAVED_MODULES[_name] = sys.modules.get(_name)

# utils/magnet 需要真实的 app.core.cache.TTLCache（不能是 MagicMock 基类）
# 与真实的 torf.Magnet：更早收集的文件会把 torf 注册成 mock，
# utils/magnet 在导入时就会绑走 mock Magnet；这里先换成真实库。
install_real_ttl_cache()
sys.modules.pop("torf", None)
import torf  # noqa: F401  (真实库；tearDownModule 恢复之前那个 mock）


# 基线文件的假 MetaInfo 不接受 title= 关键字，与真实 utils/magnet 不兼容；
# 这里先换成兼容桩，tearDownModule 再恢复。
register_module("app.core.metainfo", MetaInfo=_test_metainfo)


def tearDownModule():
    for _name, _mod in _SAVED_MODULES.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


class PirateBayError(RuntimeError):
    pass


class UIndexError(RuntimeError):
    pass


class _FakePirateBayClientModule(types.ModuleType):
    pass


_pb_client = types.ModuleType("cloudsubscribex.search.piratebay.client")
_pb_client.PirateBayClient = type("PirateBayClient", (), {})
_pb_client.PirateBayError = PirateBayError
sys.modules["cloudsubscribex.search.piratebay.client"] = _pb_client
_ui_client = types.ModuleType("cloudsubscribex.search.uindex.client")
_ui_client.UIndexClient = type("UIndexClient", (), {})
_ui_client.UIndexError = UIndexError
sys.modules["cloudsubscribex.search.uindex.client"] = _ui_client

register_mock(
    "cloudsubscribex.core.media",
    media_id_of=lambda *args: "",
    tmdb_id_of=lambda *args: "",
)

core_search = load_module(
    "cloudsubscribex.core.search",
    "plugins.v2/cloudsubscribex/core/search.py",
)
SearchQuery = core_search.SearchQuery
load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
utils_magnet = load_module(
    "cloudsubscribex.utils.magnet",
    "plugins.v2/cloudsubscribex/utils/magnet.py",
)
# 直接钉住模块内绑定的 MetaInfo：之后再有文件重注册 app.core.metainfo
# 也不会影响本文件已绑定的名字。
utils_magnet.MetaInfo = _test_metainfo
load_module(
    "cloudsubscribex.search.magnet",
    "plugins.v2/cloudsubscribex/search/magnet.py",
)
base_module = load_module(
    "cloudsubscribex.search.base",
    "plugins.v2/cloudsubscribex/search/base.py",
)
PirateBaySearchService = load_module(
    "cloudsubscribex.search.piratebay.service",
    "plugins.v2/cloudsubscribex/search/piratebay/service.py",
).PirateBaySearchService
uindex_service_module = load_module(
    "cloudsubscribex.search.uindex.service",
    "plugins.v2/cloudsubscribex/search/uindex/service.py",
)
UIndexSearchService = uindex_service_module.UIndexSearchService
JuyingSearchService = load_module(
    "cloudsubscribex.search.juying.service",
    "plugins.v2/cloudsubscribex/search/juying/service.py",
).JuyingSearchService
WoniuSearchService = load_module(
    "cloudsubscribex.search.woniu.service",
    "plugins.v2/cloudsubscribex/search/woniu/service.py",
).WoniuSearchService


def _magnet_entry(info_hash, title):
    return {
        "info_hash": info_hash,
        "title": title,
        "url": f"magnet:?xt=urn:btih:{info_hash}",
    }


class FakeMagnetClient:
    """记录调用的 magnet 客户端替身；error_cls 决定抛出的异常类型。"""

    error_cls = RuntimeError

    def __init__(self, results_by_keyword=None, error_keywords=()):
        self.calls = []
        self.results_by_keyword = results_by_keyword or {}
        self.error_keywords = set(error_keywords)

    def search(self, keyword, **kwargs):
        self.calls.append((keyword, dict(kwargs)))
        if keyword in self.error_keywords:
            raise self.error_cls(f"boom {keyword}")
        return list(self.results_by_keyword.get(keyword, []))

    def clear_cache(self):
        return {"a": 2, "b": 3}


class FakePirateBayClient(FakeMagnetClient):
    error_cls = PirateBayError


class FakeUIndexClient(FakeMagnetClient):
    error_cls = UIndexError


class MediaInfo:
    def __init__(self, title="", original_title="", original_name="",
            year=None, tmdb_id=None, douban_id=None, imdb_id=None,
            en_name=""):
        self.title = title
        self.original_title = original_title
        self.original_name = original_name
        self.year = year
        self.tmdb_id = tmdb_id
        self.douban_id = douban_id
        self.imdb_id = imdb_id
        self.en_name = en_name


class BaseMagnetLoopTests(unittest.TestCase):

    def _movie_info(self):
        return MediaInfo(
            title="Test Show", original_title="Test Show Original", year="2024"
        )

    def _movie_query(self, info, result_limit=None):
        return SearchQuery(
            mediainfo=info, media_type=MediaType.MOVIE, result_limit=result_limit
        )

    def test_keyword_cap_three(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        self.assertGreater(len(keywords), 3)
        client = FakePirateBayClient()
        service = PirateBaySearchService(client)
        service.search(self._movie_query(info))
        self.assertEqual([kw for kw, _ in client.calls], keywords[:3])

    def test_dedup_by_info_hash(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        h = "a" * 40
        entry = _magnet_entry(h, "Test Show 2024 1080p")
        client = FakePirateBayClient({keywords[0]: [entry], keywords[1]: [entry]})
        service = PirateBaySearchService(client)
        results = service.search(self._movie_query(info))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["info_hash"], h.upper())
        self.assertEqual(results[0]["source"], "piratebay")

    def test_tv_season_filter(self):
        info = MediaInfo(title="Test Show", original_title="Test Show Original", year="2024")
        query = SearchQuery(mediainfo=info, media_type=MediaType.TV, season=2)
        keywords = PirateBaySearchService._keywords(info, MediaType.TV, 2)
        h_ok, h_wrong, h_none = "a" * 40, "b" * 40, "c" * 40
        entries = [
            _magnet_entry(h_ok, "Test Show S02 1080p"),
            _magnet_entry(h_wrong, "Test Show S01 1080p"),
            _magnet_entry(h_none, "Test Show Complete 1080p"),
        ]
        client = FakePirateBayClient({keywords[0]: entries})
        service = PirateBaySearchService(client)
        results = service.search(query)
        self.assertEqual(
            {r["info_hash"] for r in results}, {h_ok.upper(), h_none.upper()}
        )

    def test_movie_season_dropped(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        h_season, h_plain = "a" * 40, "b" * 40
        entries = [
            _magnet_entry(h_season, "Test Show S01 1080p"),
            _magnet_entry(h_plain, "Test Show 2024 1080p"),
        ]
        client = FakePirateBayClient({keywords[0]: entries})
        service = PirateBaySearchService(client)
        results = service.search(self._movie_query(info))
        self.assertEqual([r["info_hash"] for r in results], [h_plain.upper()])

    def test_movie_year_filter(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        h_old, h_ok, h_noyear = "a" * 40, "b" * 40, "c" * 40
        entries = [
            _magnet_entry(h_old, "Test Show 2023 1080p"),
            _magnet_entry(h_ok, "Test Show 2024 1080p"),
            _magnet_entry(h_noyear, "Test Show 1080p"),
        ]
        client = FakePirateBayClient({keywords[0]: entries})
        service = PirateBaySearchService(client)
        results = service.search(self._movie_query(info))
        self.assertEqual(
            {r["info_hash"] for r in results}, {h_ok.upper(), h_noyear.upper()}
        )

    def test_title_filter(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        h_bad, h_ok = "a" * 40, "b" * 40
        entries = [
            _magnet_entry(h_bad, "Something Else 2024 1080p"),
            _magnet_entry(h_ok, "Test Show 2024 1080p"),
        ]
        client = FakePirateBayClient({keywords[0]: entries})
        service = PirateBaySearchService(client)
        results = service.search(self._movie_query(info))
        self.assertEqual([r["info_hash"] for r in results], [h_ok.upper()])

    def test_limit(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        entries = [_magnet_entry(f"{i:040x}", f"Test Show 2024 Part{i}") for i in range(1, 6)]
        client = FakePirateBayClient({keywords[0]: entries})
        service = PirateBaySearchService(client, result_limit=20)
        results = service.search(self._movie_query(info, result_limit=2))
        self.assertEqual(len(results), 2)

    def test_piratebay_passes_cat_200(self):
        info = self._movie_info()
        client = FakePirateBayClient()
        service = PirateBaySearchService(client)
        service.search(self._movie_query(info))
        self.assertTrue(client.calls)
        for _, kwargs in client.calls:
            self.assertEqual(kwargs, {"cat": 200})

    def test_uindex_passes_no_extra_kwargs(self):
        info = self._movie_info()
        client = FakeUIndexClient()
        service = UIndexSearchService(client)
        service.search(self._movie_query(info))
        self.assertTrue(client.calls)
        for _, kwargs in client.calls:
            self.assertEqual(kwargs, {})

    def test_uindex_error_logs_warning_and_continues(self):
        info = self._movie_info()
        keywords = UIndexSearchService._keywords(info, MediaType.MOVIE, None)
        h = "a" * 40
        client = FakeUIndexClient(
            {keywords[1]: [_magnet_entry(h, "Test Show 2024 1080p")]},
            error_keywords={keywords[0]},
        )
        service = UIndexSearchService(client)
        # 直接 patch service 模块上绑定的 logger：sys.modules["app.log"] 在全量
        # 套件里可能被其它文件换掉，与 service 加载时绑定的不是同一对象。
        with patch.object(uindex_service_module, "logger") as mock_logger:
            results = service.search(self._movie_query(info))
        self.assertEqual([r["info_hash"] for r in results], [h.upper()])
        self.assertEqual([r["source"] for r in results], ["uindex"])
        self.assertTrue(mock_logger.warning.called)
        self.assertIn(keywords[0], str(mock_logger.warning.call_args))

    def test_piratebay_error_silent_continue(self):
        info = self._movie_info()
        keywords = PirateBaySearchService._keywords(info, MediaType.MOVIE, None)
        h = "a" * 40
        client = FakePirateBayClient(
            {keywords[1]: [_magnet_entry(h, "Test Show 2024 1080p")]},
            error_keywords={keywords[0]},
        )
        service = PirateBaySearchService(client)
        logger = sys.modules["app.log"].logger
        logger.warning.reset_mock()
        results = service.search(self._movie_query(info))
        self.assertEqual([r["info_hash"] for r in results], [h.upper()])
        self.assertFalse(logger.warning.called)

    def test_clear_cache_delegates_to_client(self):
        self.assertEqual(PirateBaySearchService(FakePirateBayClient()).clear_cache(), 5)
        self.assertEqual(UIndexSearchService(FakeUIndexClient()).clear_cache(), 5)

    def test_no_mediainfo_returns_empty(self):
        query = SearchQuery(mediainfo=None, media_type=MediaType.MOVIE)
        self.assertEqual(PirateBaySearchService(FakePirateBayClient()).search(query), [])

    def test_base_search_requires_keywords(self):
        # 基类 _keywords 未实现时直接报 NotImplementedError，不静默返回空。
        service = base_module.BaseMagnetSearchService(FakePirateBayClient())
        with self.assertRaises(NotImplementedError):
            service.search(self._movie_query(self._movie_info()))


class FakeResourceService:
    """记录 search()/resolve_resource() 调用的资源服务替身。"""

    def __init__(self, results=None):
        self.results = results or []
        self.search_calls = []
        self.resolve_calls = []

    def search(self, **kwargs):
        self.search_calls.append(dict(kwargs))
        return list(self.results)

    def resolve_resource(self, reference):
        self.resolve_calls.append(reference)
        return {"reference": reference}

    def clear_cache(self):
        return {"x": 4}


class DelegatingServiceTests(unittest.TestCase):

    def _query(self, info, resource_list_mode=False, result_limit=None):
        return SearchQuery(
            mediainfo=info,
            media_type=MediaType.TV,
            season=1,
            resource_list_mode=resource_list_mode,
            result_limit=result_limit,
        )

    def test_woniu_search_has_no_id_kwargs(self):
        info = MediaInfo(title="Test Show", year="2024")
        resources = FakeResourceService([_magnet_entry("a" * 40, "Test Show S01E01")])
        service = WoniuSearchService(object(), resources, ("magnet", "115"), 10)
        results = service.search(self._query(info))
        self.assertEqual(len(resources.search_calls), 1)
        kwargs = resources.search_calls[0]
        self.assertNotIn("tmdb_id", kwargs)
        self.assertNotIn("douban_id", kwargs)
        self.assertNotIn("imdb_id", kwargs)
        self.assertEqual(kwargs["title"], "Test Show")
        self.assertEqual(kwargs["media_type"], "tv")
        self.assertEqual(kwargs["season"], 1)
        self.assertEqual(results[0]["source"], "woniu")

    def test_juying_search_passes_ids(self):
        info = MediaInfo(title="Test Show", year="2024", tmdb_id=123)
        resources = FakeResourceService([_magnet_entry("a" * 40, "Test Show S01E01")])
        service = JuyingSearchService(object(), resources, ("magnet", "115"), 10)
        results = service.search(self._query(info))
        kwargs = resources.search_calls[0]
        self.assertEqual(kwargs["tmdb_id"], 123)
        self.assertEqual(kwargs["douban_id"], "")
        self.assertEqual(kwargs["imdb_id"], "")
        self.assertEqual(results[0]["source"], "juying")

    def test_delegating_limit_modes(self):
        info = MediaInfo(title="Test Show", year="2024")
        resources = FakeResourceService()
        service = WoniuSearchService(object(), resources, ("magnet",), 7)
        service.search(self._query(info, resource_list_mode=False, result_limit=99))
        self.assertEqual(resources.search_calls[0]["limit"], 7)
        service.search(self._query(info, resource_list_mode=True, result_limit=3))
        self.assertEqual(resources.search_calls[1]["limit"], 3)
        service.search(self._query(info, resource_list_mode=True, result_limit=None))
        self.assertEqual(resources.search_calls[2]["limit"], 7)

    def test_delegating_resolve_and_clear_cache(self):
        resources = FakeResourceService()
        service = WoniuSearchService(object(), resources, ("magnet",), 10)
        self.assertEqual(service.resolve(resource_id="abc"), {"reference": "abc"})
        self.assertEqual(resources.resolve_calls, ["abc"])
        self.assertEqual(service.clear_cache(), 4)


if __name__ == "__main__":
    unittest.main()
