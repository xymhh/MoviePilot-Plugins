"""TMDB 媒体候选查询的类型兜底测试。

前端「一键转存」在**没有选中媒体上下文**时，会默认按电视剧反查 TMDB
（``media_type=tv``）。此前 ``api_vue_search_tmdb_candidates`` 会严格按类型过滤
候选，电影因此被全部挡掉 → 前端拿不到 tmdb_id → 提交后后端返回
「请选择订阅或有效的 TMDB 媒体」。

本测试固定新行为：按类型查不到时不直接返回空，而是放宽类型返回候选
（若用户所需类型有命中，仍只返回该类型，保持原语义）。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.config", global_vars=MagicMock(), settings=MagicMock())
register_mock("app.core.context")
register_mock("app.core.metainfo")
register_module("app.db", package=True, SessionFactory=MagicMock())
register_mock("app.db.subscribe_oper")
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True, MediaInfo=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string")

MEDIA_TYPE = set_media_type()

# ---- cloudsubscribe.* 占位依赖 ----
register_module("cloudsubscribe", package=True)
register_module(
    "cloudsubscribe.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    SearchCapability=MagicMock(),
    CloudDriveCapability=MagicMock(),
)
register_module("cloudsubscribe.core.cloud", CloudDriveCapability=MagicMock())
register_module(
    "cloudsubscribe.core.api.page",
    _platform_image_url=MagicMock(return_value=""),
)
register_module(
    "cloudsubscribe.core.media",
    apply_media_identity=MagicMock(),
    legacy_media_ids=MagicMock(),
    media_identity=MagicMock(),
    recognize_media=MagicMock(),
    search_medias=MagicMock(),
    tmdb_id_of=MagicMock(),
    tmdb_identity_update=MagicMock(),
)
register_module("cloudsubscribe.core.config", UIConfig=MagicMock())
register_module("cloudsubscribe.handlers.search", SearchHandler=MagicMock())
register_module("cloudsubscribe.search.hdhive", HDHIVE_DETAIL_RESOURCE_TYPES=set())
register_module("cloudsubscribe.search.magnet", parse_size_str=MagicMock())
register_module("cloudsubscribe.search.matching", extract_resource_tags=MagicMock())
register_module("cloudsubscribe.search.scanner", SearchSourceRegistry=MagicMock())
register_module(
    "cloudsubscribe.search.types",
    PREVIEW_PROVIDER_KEYS=set(),
    PREVIEW_RESOURCE_TYPES=set(),
    RESOURCE_TYPE_ORDER=[],
    RESOURCE_TYPE_PRIORITY={},
    SUPPORTED_RESOURCE_TYPES=set(),
    normalize_resource_type=MagicMock(),
    resource_type_from_url=MagicMock(),
    resource_type_name=MagicMock(),
)
register_module("cloudsubscribe.utils", parse_magnet_metadata=MagicMock())
register_module(
    "cloudsubscribe.utils.http_client",
    build_proxy_url=MagicMock(),
    normalize_proxies=MagicMock(),
    request_error_summary=MagicMock(),
    requests=MagicMock(),
    validate_proxy_address=MagicMock(),
)

search_api_module = load_module(
    "cloudsubscribe.core.api.search",
    "plugins.v2/cloudsubscribe/core/api/search.py",
)


def _candidate(media_type, tmdb_id, title, year=2026):
    return SimpleNamespace(
        type=media_type,
        tmdb_id=tmdb_id,
        imdb_id=None,
        tvdb_id=None,
        douban_id=None,
        bangumi_id=None,
        anilist_id=None,
        anidb_id=None,
        title=title,
        original_title=title,
        year=year,
        poster_path="",
        vote_average=7.0,
    )


class TestTmdbCandidateTypeFallback(unittest.TestCase):
    def setUp(self):
        self.api = search_api_module.SearchApi(None)
        self.movie = _candidate(MEDIA_TYPE.MOVIE, 900001, "某电影")
        self.tv = _candidate(MEDIA_TYPE.TV, 900002, "某剧集")

    def test_movie_returned_when_frontend_asks_tv(self):
        """前端默认按电视剧反查、实际是电影时，应放宽类型返回电影候选。"""
        search_api_module.search_medias = lambda *args, **kwargs: [self.movie]
        result = self.api.api_vue_search_tmdb_candidates(
            {"title": "某电影", "media_type": "tv"}
        )
        self.assertTrue(result["success"])
        items = result["data"]["items"]
        self.assertEqual(len(items), 1, "电影候选不应被类型过滤挡掉")
        self.assertEqual(items[0]["tmdb_id"], 900001)
        self.assertEqual(items[0]["media_type"], "movie")

    def test_tv_only_when_tv_candidates_exist(self):
        """按类型有命中时保持原语义：只返回该类型，不放宽。"""
        search_api_module.search_medias = lambda *args, **kwargs: [self.tv, self.movie]
        result = self.api.api_vue_search_tmdb_candidates(
            {"title": "某剧集", "media_type": "tv"}
        )
        items = result["data"]["items"]
        self.assertEqual([item["media_type"] for item in items], ["tv"])
        self.assertEqual(items[0]["tmdb_id"], 900002)

    def test_movie_type_request_keeps_movie(self):
        """明确要求电影时不触发兜底逻辑，行为与之前一致。"""
        search_api_module.search_medias = lambda *args, **kwargs: [self.tv, self.movie]
        result = self.api.api_vue_search_tmdb_candidates(
            {"title": "某电影", "media_type": "movie"}
        )
        items = result["data"]["items"]
        self.assertEqual([item["media_type"] for item in items], ["movie"])
        self.assertEqual(items[0]["tmdb_id"], 900001)

    def test_tmdb_id_filter_still_applies(self):
        """指定 tmdb_id 时仍需精确命中，不能因放宽类型而返回别的媒体。"""
        search_api_module.search_medias = lambda *args, **kwargs: [
            self.tv,
            _candidate(MEDIA_TYPE.MOVIE, 900003, "另一部电影"),
        ]
        result = self.api.api_vue_search_tmdb_candidates(
            {"title": "某电影", "media_type": "tv", "tmdb_id": 111111}
        )
        self.assertEqual(result["data"]["items"], [])


if __name__ == "__main__":
    unittest.main()
