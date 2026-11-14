"""1.9.86：任务进度脱离自有 UI，改走下载管理页独立标签。

覆盖 core/downloader_view.py 的纯投影逻辑：任务 hash、状态映射、
字段投影、查询过滤、文件清单、下载器条目登记/移除。
投影层不依赖 MoviePilot 运行时，桩 DownloaderTorrent/DownloaderFile
直接传参注入。
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


SYNC_TV = {
    "id": "subscribe:123",
    "title": "谷雨",
    "media_type": "电视剧",
    "year": "2026",
    "season": 1,
    "target_episodes": [21, 22],
    "status": "transferring",
    "phase": "跨盘转存 2 个文件",
    "progress": 45,
    "transferred": 450,
    "total": 1000,
    "speed_bytes_per_second": 2097152,
    "current_file": "",
    "queued_at": 1000,
    "started_at": 1010,
}

SYNC_MOVIE = {
    "id": "media:tmdb:999",
    "title": "天使与恶徒",
    "media_type": "电影",
    "year": "2026",
    "status": "downloading",
    "progress": 10,
    "transferred": 0,
    "total": 0,
    "speed_bytes_per_second": 0,
}

QUEUED = {
    "id": "subscribe:124",
    "title": "折月亮",
    "media_type": "电视剧",
    "year": "2026",
    "season": 1,
    "target_episodes": [1],
    "status": "queued",
    "progress": 0,
}

STOPPING = {
    "id": "subscribe:125",
    "title": "走马上任",
    "media_type": "电视剧",
    "year": "2026",
    "season": 1,
    "target_episodes": [28],
    "status": "stopping",
    "progress": 80,
}

CROSS = {
    "id": "cross:abcdef123456",
    "task_kind": "cross_transfer",
    "parent_task_id": "",
    "title": "谷雨 S01E21.mkv",
    "source_provider": "115",
    "target_provider": "115",
    "target_path": "/转存/电视剧/谷雨 (2026)/Season 1/谷雨 - S01E21.mkv",
    "status": "running",
    "phase": "running",
    "progress": 60,
    "transferred": 600,
    "total": 1000,
    "speed_bytes_per_second": 1048576,
    "message": "正在跨盘转存",
}


def _tasks():
    return [dict(SYNC_TV), dict(SYNC_MOVIE), dict(QUEUED), dict(STOPPING), dict(CROSS)]


class TestTaskHash(unittest.TestCase):
    def test_hash_is_40_hex_and_stable(self):
        first = _view.task_hash("subscribe:123")
        self.assertEqual(len(first), 40)
        int(first, 16)
        self.assertEqual(first, _view.task_hash("subscribe:123"))
        self.assertNotEqual(first, _view.task_hash("subscribe:124"))

    def test_resolve_round_trip(self):
        tasks = _tasks()
        task_id = _view.resolve_task_id(tasks, _view.task_hash("subscribe:123"))
        self.assertEqual(task_id, "subscribe:123")
        self.assertEqual(_view.resolve_task_id(tasks, "0" * 40), "")
        self.assertEqual(_view.resolve_task_id(tasks, ""), "")


class TestMapTaskState(unittest.TestCase):
    def test_downloading_statuses(self):
        for status in (
            "queued", "running", "downloading",
            "transferring", "postprocessing", "pending",
        ):
            self.assertEqual(_view.map_task_state(status), "downloading", status)

    def test_completed_statuses(self):
        for status in ("completed", "success"):
            self.assertEqual(_view.map_task_state(status), "completed", status)

    def test_paused_statuses(self):
        for status in ("stopping", "failed", "stopped", "unknown", "", None):
            self.assertEqual(_view.map_task_state(status), "paused", status)


class TestTaskToTorrent(unittest.TestCase):
    def test_sync_tv_fields(self):
        torrent = _view.task_to_torrent(SYNC_TV, torrent_cls=_Torrent)
        self.assertEqual(torrent.downloader, "网盘订阅助手")
        self.assertEqual(torrent.hash, _view.task_hash("subscribe:123"))
        self.assertEqual(torrent.title, "谷雨")
        self.assertEqual(torrent.name, "谷雨 S01E21-E22")
        self.assertEqual(torrent.year, "2026")
        self.assertEqual(torrent.season_episode, "S01E21-E22")
        self.assertEqual(torrent.size, 1000)
        self.assertEqual(torrent.progress, 45)
        self.assertEqual(torrent.state, "downloading")
        self.assertEqual(torrent.dlspeed, "2.0 MB/s")

    def test_movie_has_no_season_episode(self):
        torrent = _view.task_to_torrent(SYNC_MOVIE, torrent_cls=_Torrent)
        self.assertEqual(torrent.name, "天使与恶徒")
        self.assertIsNone(torrent.season_episode)
        self.assertIsNone(torrent.dlspeed)

    def test_speed_format(self):
        self.assertEqual(_view.format_speed(1048576), "1.0 MB/s")
        self.assertIsNone(_view.format_speed(0))
        self.assertIsNone(_view.format_speed(None))
        self.assertIsNone(_view.format_speed("bad"))

    def test_progress_clamped(self):
        torrent = _view.task_to_torrent(
            {**SYNC_TV, "progress": 150}, torrent_cls=_Torrent
        )
        self.assertEqual(torrent.progress, 100.0)
        torrent = _view.task_to_torrent(
            {**SYNC_TV, "progress": -5}, torrent_cls=_Torrent
        )
        self.assertEqual(torrent.progress, 0.0)


class TestProjectTasks(unittest.TestCase):
    def test_all_returns_everything(self):
        rows = _view.project_tasks(_tasks(), torrent_cls=_Torrent)
        self.assertEqual(len(rows), 5)

    def test_downloading_filter(self):
        rows = _view.project_tasks(
            _tasks(), status="downloading", torrent_cls=_Torrent
        )
        self.assertEqual(
            {row.title for row in rows},
            {"谷雨", "天使与恶徒", "折月亮", "谷雨 S01E21.mkv"},
        )

    def test_paused_filter(self):
        rows = _view.project_tasks(_tasks(), status="paused", torrent_cls=_Torrent)
        self.assertEqual([row.title for row in rows], ["走马上任"])

    def test_completed_filter_is_empty_for_live_tasks(self):
        rows = _view.project_tasks(_tasks(), status="completed", torrent_cls=_Torrent)
        self.assertEqual(rows, [])

    def test_completed_task_shows_under_completed(self):
        done = {**SYNC_TV, "status": "completed", "progress": 100}
        rows = _view.project_tasks([done], status="completed", torrent_cls=_Torrent)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "completed")
        rows = _view.project_tasks([done], status="downloading", torrent_cls=_Torrent)
        self.assertEqual(rows, [])

    def test_transfer_query_needs_full_progress(self):
        done = {**SYNC_TV, "status": "completed", "progress": 100}
        half = {**SYNC_TV, "status": "completed", "progress": 50}
        rows = _view.project_tasks(
            [done, half], status="transfer", torrent_cls=_Torrent
        )
        self.assertEqual(len(rows), 1)

    def test_other_downloader_returns_empty(self):
        rows = _view.project_tasks(
            _tasks(), downloader="网盘直链", torrent_cls=_Torrent
        )
        self.assertEqual(rows, [])

    def test_hash_filter(self):
        wanted = _view.task_hash("subscribe:124")
        rows = _view.project_tasks(_tasks(), hashs=[wanted], torrent_cls=_Torrent)
        self.assertEqual([row.title for row in rows], ["折月亮"])
        rows = _view.project_tasks(_tasks(), hashs=wanted, torrent_cls=_Torrent)
        self.assertEqual([row.title for row in rows], ["折月亮"])


class TestTaskToFiles(unittest.TestCase):
    def test_cross_transfer_is_one_file(self):
        files = _view.task_to_files(CROSS, file_cls=_File)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "谷雨 S01E21.mkv")
        self.assertEqual(files[0].size, 1000)
        self.assertEqual(files[0].progress, 60)

    def test_sync_task_with_current_file(self):
        task = {**SYNC_TV, "current_file": "谷雨 S01E21.mkv", "progress": 45}
        files = _view.task_to_files(task, file_cls=_File)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "谷雨 S01E21.mkv")
        self.assertEqual(files[0].progress, 45)

    def test_sync_task_without_current_file(self):
        self.assertEqual(_view.task_to_files(SYNC_TV, file_cls=_File), [])


class TestDownloaderEntry(unittest.TestCase):
    def test_ensure_adds_new_entry(self):
        items = [{"name": "Qbittorrent", "type": "Qbittorrent", "enabled": True}]
        updated = _view.ensure_downloader_entry(items)
        self.assertEqual(len(updated), 2)
        entry = updated[-1]
        self.assertEqual(entry["name"], "网盘订阅助手")
        self.assertEqual(entry["type"], "CloudSubscribeX")
        self.assertTrue(entry["enabled"])
        self.assertFalse(entry["default"])

    def test_ensure_enables_existing_disabled_entry(self):
        items = [{
            "name": "网盘订阅助手", "type": "CloudSubscribeX", "enabled": False,
        }]
        updated = _view.ensure_downloader_entry(items)
        self.assertEqual(len(updated), 1)
        self.assertTrue(updated[0]["enabled"])

    def test_ensure_is_idempotent(self):
        items = [{
            "name": "网盘订阅助手", "type": "CloudSubscribeX", "enabled": True,
        }]
        self.assertEqual(_view.ensure_downloader_entry(items), items)

    def test_remove_entry(self):
        items = [
            {"name": "网盘订阅助手", "type": "CloudSubscribeX", "enabled": True},
            {"name": "Qbittorrent", "type": "Qbittorrent", "enabled": True},
        ]
        kept = _view.remove_downloader_entry(items)
        self.assertEqual(
            [item["name"] for item in kept], ["Qbittorrent"]
        )

    def test_remove_keeps_same_name_other_type(self):
        items = [
            {"name": "网盘订阅助手", "type": "SomethingElse", "enabled": True},
        ]
        self.assertEqual(_view.remove_downloader_entry(items), items)


if __name__ == "__main__":
    unittest.main()
