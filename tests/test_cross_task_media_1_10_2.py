"""1.10.2：独立跨盘任务带媒体身份，下载页能画出海报。

此前跨盘任务字典里没有任何媒体信息：有归属订阅的任务靠父任务
带 tmdb_id，独立任务（手动下载、历史重试、无订阅）就只有文件名，
下载页投影拿不到 tmdb_id/媒体类型，海报与季集都出不来。
本版起 create/create_from_cloud_file 接受 media（tmdb_id/media_type/
season/year/media_title），_transfer_file 链路从 mediainfo 透传；
投影层用 media_title 做媒体标题与卡片标题。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import load_module, register_mock, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager
CloudFile = transfer_module.CloudFile

_view = load_module(
    "cloudsubscribex.core.downloader_view",
    "plugins.v2/cloudsubscribex/core/downloader_view.py",
)


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeMediaType:
    """桩 MediaType 枚举：只有 name。"""

    def __init__(self, name):
        self.name = name


def _make_manager():
    manager = CrossTransferTaskManager(
        lambda key: MagicMock(),
        download_path=tempfile.mkdtemp(prefix="csx-media-test-"),
    )
    manager._notify_change = lambda: None
    # 不真正跑传输：只验证任务字典建时带上的媒体字段。
    manager._run = lambda *args, **kwargs: None
    return manager


def _make_source_file():
    return CloudFile(
        id="f1", name="18.mp4", is_directory=False,
        size=1000, sha1="aabbcc", md5="ddeeff",
    )


class TestNormalizeTaskMedia(unittest.TestCase):
    def test_media_type_forms(self):
        for raw, expected in (
            ("TV", "电视剧"), ("MOVIE", "电影"),
            ("tv", "电视剧"), ("movie", "电影"),
            ("电视剧", "电视剧"), ("电影", "电影"),
            ("MediaType.TV", "电视剧"), ("MediaType.MOVIE", "电影"),
            (_FakeMediaType("TV"), "电视剧"),
            (_FakeMediaType("MOVIE"), "电影"),
        ):
            with self.subTest(raw=raw):
                result = transfer_module._normalize_task_media(
                    {"media_type": raw}
                )
                self.assertEqual(result.get("media_type"), expected)

    def test_unknown_media_type_dropped(self):
        result = transfer_module._normalize_task_media({"media_type": "music"})
        self.assertNotIn("media_type", result)

    def test_tmdb_id_season_year_coercion(self):
        result = transfer_module._normalize_task_media({
            "tmdb_id": "326140", "season": "1", "year": 2026,
            "target_episodes": [18, 18, 0, "bad"],
        })
        self.assertEqual(result, {
            "tmdb_id": 326140, "season": 1, "year": "2026",
            "target_episodes": [18],
        })

    def test_invalid_values_dropped(self):
        result = transfer_module._normalize_task_media({
            "tmdb_id": 0, "season": 0, "year": "bad",
            "media_title": "  ",
        })
        self.assertEqual(result, {})

    def test_media_title_prefers_media_title(self):
        result = transfer_module._normalize_task_media({
            "media_title": "今天是太阳雨", "title": "18.mp4",
        })
        self.assertEqual(result.get("media_title"), "今天是太阳雨")

    def test_non_dict_returns_empty(self):
        self.assertEqual(transfer_module._normalize_task_media(None), {})
        self.assertEqual(transfer_module._normalize_task_media("x"), {})


class TestCreateStoresMedia(unittest.TestCase):
    def test_create_stores_media_fields(self):
        manager = _make_manager()
        task = manager.create(
            target_key="115", save_path="/转存", target_name="18.mp4",
            source_key="quark", source_file=_make_source_file(),
            media={
                "tmdb_id": 326140, "media_type": "TV", "season": 1,
                "year": 2026, "media_title": "今天是太阳雨",
                "target_episodes": [18],
            },
        )
        self.assertEqual(task.get("tmdb_id"), 326140)
        self.assertEqual(task.get("media_type"), "电视剧")
        self.assertEqual(task.get("season"), 1)
        self.assertEqual(task.get("year"), "2026")
        self.assertEqual(task.get("media_title"), "今天是太阳雨")
        self.assertEqual(task.get("target_episodes"), [18])
        self.assertEqual(task.get("title"), "18.mp4")

    def test_create_without_media_has_no_media_keys(self):
        manager = _make_manager()
        task = manager.create(
            target_key="115", save_path="/转存", target_name="18.mp4",
            source_key="quark", source_file=_make_source_file(),
        )
        for key in ("tmdb_id", "media_type", "season", "year", "media_title"):
            self.assertNotIn(key, task)

    def test_create_from_cloud_file_forwards_media(self):
        manager = _make_manager()
        task = manager.create_from_cloud_file(
            "quark", _make_source_file(), "115", "/转存", "18.mp4",
            algorithm="sha1",
            media={"tmdb_id": 326140, "media_type": "电视剧", "season": 1},
        )
        self.assertEqual(task.get("tmdb_id"), 326140)
        self.assertEqual(task.get("media_type"), "电视剧")
        self.assertEqual(task.get("season"), 1)


class TestStandaloneCrossTaskPoster(unittest.TestCase):
    def _task(self, **overrides):
        task = {
            "id": "cross:abcdef123456",
            "task_kind": "cross_transfer",
            "parent_task_id": "",
            "title": "18.mp4",
            "status": "running",
            "phase": "running",
            "progress": 30,
            "transferred": 300,
            "total": 1000,
            "speed_bytes_per_second": 1048576,
        }
        task.update(overrides)
        return task

    def test_media_with_poster_and_show_title(self):
        task = self._task(
            tmdb_id=326140, media_type="电视剧", season=1, year="2026",
            media_title="今天是太阳雨", target_episodes=[18],
        )
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "https://img/poster.jpg",
        )
        self.assertIsNotNone(torrent.media)
        self.assertEqual(torrent.media["poster"], "https://img/poster.jpg")
        self.assertEqual(torrent.media["title"], "今天是太阳雨")
        self.assertEqual(torrent.media["season"], 1)
        self.assertEqual(torrent.media["episode"], 18)
        self.assertEqual(torrent.name, "今天是太阳雨 S01E18")

    def test_no_media_without_media_identity(self):
        torrent = _view.task_to_torrent(self._task(), torrent_cls=_Torrent)
        self.assertIsNone(torrent.media)
        self.assertEqual(torrent.name, "18.mp4")

    def test_media_title_falls_back_to_file_title(self):
        task = self._task(
            tmdb_id=326140, media_type="电视剧", season=1, target_episodes=[18]
        )
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "https://img/poster.jpg",
        )
        self.assertEqual(torrent.media["title"], "18.mp4")
        self.assertEqual(torrent.name, "18.mp4 S01E18")


if __name__ == "__main__":
    unittest.main()
