"""1.9.93：下载页任务投影 media（海报/季/集）与文件名集号推导。

覆盖 core/downloader_view.py：
* 任务带 tmdb_id 时 media 含海报/类型/标题/季/集（海报由注入的解析器提供，
  按 tmdb_id 缓存，重复投影不再调解析器）；
* 海报取不到时 media 其它字段照常填；
* target_episodes 为空时从跨盘子任务文件名推导集数（真机复现：手动转存
  subscribe:35 律界战争，target_episodes 为空，集号只在传输中的文件名里）；
* 文件名季不符时不采纳，回退 target_episodes / 仅季；
* 电影不带季集；中文「第 N 集」回退。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import load_module

_view = load_module(
    "cloudsubscribex.core.downloader_view",
    "plugins.v2/cloudsubscribex/core/downloader_view.py",
)


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _File:
    """桩 DownloaderFile：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _PosterResolver:
    """计数型海报解析器：返回固定格式 URL。"""

    def __init__(self):
        self.calls = []

    def __call__(self, tmdb_id, media_type):
        self.calls.append((tmdb_id, media_type))
        return f"https://image.tmdb.org/t/p/w500/poster{tmdb_id}.jpg"


TV_TASK = {
    "id": "subscribe:35",
    "title": "律界战争",
    "media_type": "电视剧",
    "year": "2025",
    "season": 1,
    "tmdb_id": 123456,
    "target_episodes": [],
    "status": "transferring",
    "phase": "正在从夸克网盘下载到本地缓存",
    "progress": 35,
    "transferred": 350,
    "total": 1000,
    "speed_bytes_per_second": 1048576,
    "current_file": "",
    "transfer_file_count": 1,
    "transfer_file_names": ["律界战争 S01E05.mkv"],
    "transfer_task_ids": ["cross:3ad2a315dddc43b8baf1e4c0cfc7bc3e"],
}


class TestMediaProjection(unittest.TestCase):
    def setUp(self):
        _view.clear_poster_cache()

    def test_tv_media_with_poster_and_episode(self):
        resolver = _PosterResolver()
        torrent = _view.task_to_torrent(
            TV_TASK, torrent_cls=_Torrent, poster_resolver=resolver
        )
        media = torrent.media
        self.assertEqual(media["type"], "电视剧")
        self.assertEqual(media["title"], "律界战争")
        self.assertEqual(media["season"], 1)
        self.assertEqual(media["episode"], 5)
        self.assertEqual(
            media["poster"], "https://image.tmdb.org/t/p/w500/poster123456.jpg"
        )
        self.assertEqual(media["media_source"], "themoviedb")
        self.assertEqual(media["media_id"], "123456")
        self.assertEqual(torrent.season_episode, "S01E05")
        self.assertEqual(torrent.name, "律界战争 S01E05")

    def test_poster_is_cached_per_tmdb_id(self):
        resolver = _PosterResolver()
        _view.task_to_torrent(TV_TASK, torrent_cls=_Torrent, poster_resolver=resolver)
        _view.task_to_torrent(TV_TASK, torrent_cls=_Torrent, poster_resolver=resolver)
        _view.task_to_torrent(
            {**TV_TASK, "id": "subscribe:36", "title": "另一部剧"},
            torrent_cls=_Torrent,
            poster_resolver=resolver,
        )
        self.assertEqual(resolver.calls, [(123456, "电视剧")])
        _view.clear_poster_cache()
        _view.task_to_torrent(TV_TASK, torrent_cls=_Torrent, poster_resolver=resolver)
        self.assertEqual(len(resolver.calls), 2)

    def test_poster_miss_keeps_other_media_fields(self):
        def _missing(tmdb_id, media_type):
            return ""

        torrent = _view.task_to_torrent(
            TV_TASK, torrent_cls=_Torrent, poster_resolver=_missing
        )
        media = torrent.media
        self.assertIsNone(media["poster"])
        self.assertEqual(media["title"], "律界战争")
        self.assertEqual(media["season"], 1)
        self.assertEqual(media["episode"], 5)
        self.assertEqual(torrent.season_episode, "S01E05")

    def test_poster_resolver_exception_is_a_miss(self):
        def _boom(tmdb_id, media_type):
            raise RuntimeError("tmdb down")

        torrent = _view.task_to_torrent(
            TV_TASK, torrent_cls=_Torrent, poster_resolver=_boom
        )
        self.assertIsNone(torrent.media["poster"])
        self.assertEqual(torrent.media["episode"], 5)

    def test_no_tmdb_id_means_no_poster_but_media_present(self):
        task = {key: value for key, value in TV_TASK.items() if key != "tmdb_id"}
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent, poster_resolver=_PosterResolver()
        )
        self.assertIsNone(torrent.media["poster"])
        self.assertNotIn("media_source", torrent.media)
        self.assertEqual(torrent.media["episode"], 5)

    def test_movie_media_has_no_season_episode(self):
        task = {
            "id": "media:tmdb:999",
            "title": "天使与恶徒",
            "media_type": "电影",
            "year": "2026",
            "tmdb_id": 999,
            "status": "downloading",
            "progress": 10,
            "total": 0,
        }
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent, poster_resolver=_PosterResolver()
        )
        media = torrent.media
        self.assertEqual(media["type"], "电影")
        self.assertIsNone(media["season"])
        self.assertIsNone(media["episode"])
        self.assertIsNone(torrent.season_episode)
        self.assertEqual(torrent.name, "天使与恶徒")

    def test_cross_task_without_media_type_has_no_media(self):
        task = {
            "id": "cross:abcdef123456",
            "task_kind": "cross_transfer",
            "parent_task_id": "",
            "title": "谷雨 S01E21.mkv",
            "status": "running",
            "progress": 60,
            "total": 1000,
        }
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent, poster_resolver=_PosterResolver()
        )
        self.assertIsNone(torrent.media)
        self.assertIsNone(torrent.season_episode)


class TestEpisodeFromFileNames(unittest.TestCase):
    def setUp(self):
        _view.clear_poster_cache()

    def _task(self, **overrides):
        task = {
            "id": "subscribe:35",
            "title": "律界战争",
            "media_type": "电视剧",
            "season": 1,
            "target_episodes": [],
            "status": "transferring",
            "progress": 35,
        }
        task.update(overrides)
        return task

    def test_empty_target_episodes_derive_from_transfer_file_names(self):
        torrent = _view.task_to_torrent(
            self._task(transfer_file_names=["律界战争 S01E05.mkv"]),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E05")
        self.assertEqual(torrent.media["episode"], 5)

    def test_current_file_is_a_source(self):
        torrent = _view.task_to_torrent(
            self._task(current_file="律界战争 S01E06.mkv"),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E06")

    def test_multiple_files_give_range_and_no_single_episode(self):
        torrent = _view.task_to_torrent(
            self._task(transfer_file_names=[
                "律界战争 S01E05.mkv", "律界战争 S01E06.mkv",
            ]),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E05-E06")
        self.assertIsNone(torrent.media["episode"])
        self.assertEqual(torrent.media["season"], 1)

    def test_wrong_season_file_falls_back_to_target_episodes(self):
        torrent = _view.task_to_torrent(
            self._task(
                transfer_file_names=["律界战争 S02E01.mkv"],
                target_episodes=[3],
            ),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E03")

    def test_no_file_names_falls_back_to_target_episodes(self):
        torrent = _view.task_to_torrent(
            self._task(target_episodes=[21, 22]),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E21-E22")

    def test_no_episodes_anywhere_is_season_only(self):
        torrent = _view.task_to_torrent(self._task(), torrent_cls=_Torrent)
        self.assertEqual(torrent.season_episode, "S01")
        self.assertIsNone(torrent.media["episode"])

    def test_chinese_episode_fallback(self):
        torrent = _view.task_to_torrent(
            self._task(transfer_file_names=["律界战争 第7集.mkv"]),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E07")

    def test_file_names_beat_target_episodes(self):
        # 文件名是正在传的真实文件，优先于建任务时的目标集数。
        torrent = _view.task_to_torrent(
            self._task(
                transfer_file_names=["律界战争 S01E05.mkv"],
                target_episodes=[1, 2, 3],
            ),
            torrent_cls=_Torrent,
        )
        self.assertEqual(torrent.season_episode, "S01E05")

    def test_project_tasks_threads_poster_resolver(self):
        resolver = _PosterResolver()
        rows = _view.project_tasks(
            [TV_TASK], torrent_cls=_Torrent, poster_resolver=resolver
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].media["poster"],
            "https://image.tmdb.org/t/p/w500/poster123456.jpg",
        )
        self.assertEqual(resolver.calls, [(123456, "电视剧")])


class TestEpisodeParser(unittest.TestCase):
    def test_parse_shared_module(self):
        from cloudsubscribex.core.episode_parse import (
            parse_episodes_from_names,
        )

        self.assertEqual(
            parse_episodes_from_names(["律界战争 S01E05.mkv"], 1), {5}
        )
        self.assertEqual(
            parse_episodes_from_names(["律界战争 S02E01.mkv"], 1), set()
        )
        self.assertEqual(
            parse_episodes_from_names(["律界战争 第7集.mkv"], 1), {7}
        )
        self.assertEqual(
            parse_episodes_from_names(["readme.txt"], 1), set()
        )


if __name__ == "__main__":
    unittest.main()
