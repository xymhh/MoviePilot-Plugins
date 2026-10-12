import unittest
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, set_media_type

# 构造 mock 的 app 基础依赖
mock_logger = MagicMock()


class FakeTorrentInfo:
    """最小可用的 TorrentInfo 桩：只保留字段过滤与属性赋值语义。"""

    model_fields = {
        "site": None,
        "site_name": None,
        "title": None,
        "description": None,
        "enclosure": None,
        "page_url": None,
        "size": None,
        "seeders": None,
        "peers": None,
        "grabs": None,
        "pubdate": None,
        "category": None,
        "downloadvolumefactor": None,
        "uploadvolumefactor": None,
        "labels": None,
        "media_source": None,
        "media_id": None,
    }

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


register_mock("app")
register_mock("app.log", logger=mock_logger)
register_mock("app.schemas", TorrentInfo=FakeTorrentInfo)
set_media_type("movie", "tv")
register_mock("cloudsubscribe")
register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.core.media", coerce_media_source=lambda value: value)
register_mock("cloudsubscribe.core.hook")

MoviePilotSearchModule = load_module(
    "cloudsubscribe.core.hook.mp_module",
    "plugins.v2/cloudsubscribe/core/hook/mp_module.py",
).MoviePilotSearchModule


class FakeCache:
    """记录式缓存桩，替代平台的 TTL 缓存。"""

    def __init__(self, payload=None):
        self.payload = payload

    def get(self, key, **kwargs):
        if not isinstance(self.payload, dict):
            return self.payload
        if key in self.payload:
            return self.payload[key]
        # 允许直接注入单个搜索会话的资源表，测试不必复刻平台的会话键规则
        if "resources" in self.payload:
            return self.payload
        return None


def make_media_type(name: str):
    """返回一个带 value 的宿主媒体类型桩。"""

    class _Member:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

    return _Member(name)


MOVIE = make_media_type("电影")
TV = make_media_type("电视剧")


class TestMoviePilotSearchModule(unittest.TestCase):
    """平台资源源（search_torrents 模块）单元测试。"""

    def setUp(self):
        self.owner = MagicMock()
        self.owner._enabled = True
        self.owner._mp_search_module_enabled = True
        self.owner._mp_search_module_limit = 20
        self.owner._search_handler = object()
        self.owner._agent_cache_key = lambda session_id, search_id: f"{session_id}:{search_id}"
        self.owner._agent_resource_cache = FakeCache()
        self.module = MoviePilotSearchModule(self.owner)

    # ------------------------------------------------------------ 声明与识别
    def test_get_module_declares_sync_and_async(self):
        module = self.module.get_module()
        self.assertEqual(
            set(module.keys()),
            {"search_torrents", "async_search_torrents"},
        )
        self.assertEqual(module["search_torrents"].__func__, self.module.search_torrents.__func__)

    def test_get_module_disabled_returns_none(self):
        self.owner._mp_search_module_enabled = False
        self.assertIsNone(self.module.get_module())

    def test_is_cloud_resource_matches_prefix_only(self):
        torrent = FakeTorrentInfo(site_name="网盘订阅助手｜pansou")
        self.assertTrue(MoviePilotSearchModule.is_cloud_resource(torrent))
        self.assertFalse(
            MoviePilotSearchModule.is_cloud_resource(FakeTorrentInfo(site_name="馒头"))
        )
        self.assertFalse(MoviePilotSearchModule.is_cloud_resource(None))

    # ------------------------------------------------------------ 关键词解析
    def test_split_title_and_season_for_tv(self):
        self.assertEqual(
            self.module.split_title_and_season("仙逆 S02", TV),
            ("仙逆", 2),
        )
        self.assertEqual(
            self.module.split_title_and_season("仙逆 第3季", TV),
            ("仙逆", 3),
        )
        self.assertEqual(
            self.module.split_title_and_season("仙逆", TV),
            ("仙逆", None),
        )

    def test_split_title_keeps_movie_keyword(self):
        self.assertEqual(
            self.module.split_title_and_season("环形使者 S01", MOVIE),
            ("环形使者 S01", None),
        )

    def test_media_type_key(self):
        self.assertEqual(self.module.media_type_key(MOVIE), "movie")
        self.assertEqual(self.module.media_type_key(TV), "tv")
        self.assertEqual(self.module.media_type_key("movie"), "movie")
        self.assertEqual(self.module.media_type_key(None), "")

    def test_parse_size(self):
        self.assertEqual(self.module.parse_size("2.5GB"), int(2.5 * 1024 ** 3))
        self.assertEqual(self.module.parse_size("700 MB"), 700 * 1024 ** 2)
        self.assertEqual(self.module.parse_size(1024), 1024.0)
        self.assertEqual(self.module.parse_size("未知"), 0.0)
        self.assertEqual(self.module.parse_size(None), 0.0)

    # ------------------------------------------------------------ 候选翻译
    def test_build_torrent_info_maps_candidate(self):
        candidate = {
            "candidate_id": "r001",
            "title": "环形使者 2012 1080p 蓝光原盘",
            "source": "pansou",
            "resource_type": "115",
            "size": "2.5GB",
            "resolution": "1080p",
            "update_time": "2026-10-01 10:00:00",
            "need_unlock": False,
            "unlock_points": 0,
        }
        resource = {
            "url": "https://115.com/s/abcdef",
            "source_url": "https://pansou.example/item/1",
        }
        media = {"title": "环形使者", "type": "电影", "tmdb_id": 59967}

        torrent = self.module.build_torrent_info(candidate, resource, media)

        self.assertEqual(torrent.site_name, "网盘订阅助手｜pansou")
        self.assertEqual(torrent.enclosure, "https://115.com/s/abcdef")
        self.assertEqual(torrent.size, int(2.5 * 1024 ** 3))
        self.assertEqual(torrent.category, "电影")
        self.assertEqual(torrent.media_id, "59967")
        self.assertEqual(torrent.media_source, "tmdb")
        self.assertEqual(torrent.downloadvolumefactor, 0.0)
        self.assertIn("1080p", torrent.description)
        self.assertIn("115", torrent.labels)

    def test_build_torrent_info_skips_unresolved_link(self):
        candidate = {"candidate_id": "r002", "title": "需解锁资源", "need_unlock": True}
        resource = {"url": "", "resource_ref": "hdhive:123"}
        self.assertIsNone(self.module.build_torrent_info(candidate, resource, {}))

    def test_build_torrent_info_accepts_link_list(self):
        candidate = {"candidate_id": "r003", "title": "多链接资源"}
        resource = {"url": ["", "magnet:?xt=urn:btih:abc"]}
        torrent = self.module.build_torrent_info(candidate, resource, {})
        self.assertEqual(torrent.enclosure, "magnet:?xt=urn:btih:abc")

    # ------------------------------------------------------------ 搜索链路
    def _stub_search(self, candidates, resources, media=None):
        self.owner.search_platform_resources = lambda **kwargs: {
            "success": True,
            "search_id": "abc123",
            "media": media or {"title": "环形使者", "type": "电影", "tmdb_id": 59967},
            "candidates": candidates,
        }
        self.owner._agent_resource_cache = FakeCache({"resources": resources})

    def test_search_torrents_returns_mapped_items(self):
        self._stub_search(
            candidates=[
                {"candidate_id": "r001", "title": "环形使者", "source": "pansou", "size": "1GB"},
                {"candidate_id": "r002", "title": "无链接", "source": "pansou", "size": "1GB"},
            ],
            resources={
                "r001": {"url": "https://115.com/s/abcdef"},
                "r002": {"url": ""},
            },
        )
        torrents = self.module.search_torrents(keyword="环形使者", mtype=MOVIE)
        self.assertEqual(len(torrents), 1)
        self.assertEqual(torrents[0].enclosure, "https://115.com/s/abcdef")

    def test_search_torrents_second_page_is_empty(self):
        self._stub_search(candidates=[], resources={})
        self.assertEqual(self.module.search_torrents(keyword="环形使者", page=1), [])

    def test_search_torrents_disabled_returns_empty(self):
        self.owner._mp_search_module_enabled = False
        self.assertEqual(self.module.search_torrents(keyword="环形使者"), [])

    def test_search_torrents_without_search_service_returns_empty(self):
        self.owner._search_handler = None
        self.assertEqual(self.module.search_torrents(keyword="环形使者"), [])

    def test_search_torrents_swallows_failure(self):
        def boom(**kwargs):
            raise RuntimeError("boom")

        self.owner.search_platform_resources = boom
        self.assertEqual(self.module.search_torrents(keyword="环形使者"), [])

    def test_search_torrents_skips_when_platform_reports_failure(self):
        self.owner.search_platform_resources = lambda **kwargs: {
            "success": False,
            "message": "无法识别媒体",
        }
        self.assertEqual(self.module.search_torrents(keyword="未知片名"), [])

    def test_search_torrents_passes_tv_season(self):
        captured = {}

        def fake_search(**kwargs):
            captured.update(kwargs)
            return {"success": True, "search_id": "abc123", "media": {}, "candidates": []}

        self.owner.search_platform_resources = fake_search
        self.module.search_torrents(keyword="仙逆 S02", mtype=TV)
        self.assertEqual(captured.get("title"), "仙逆")
        self.assertEqual(captured.get("season"), 2)
        self.assertEqual(captured.get("media_type"), "tv")


if __name__ == "__main__":
    unittest.main()
