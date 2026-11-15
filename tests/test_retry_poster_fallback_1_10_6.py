"""1.10.6：重试任务下载页无海报——存量海报兜底。

复现：LOST10 S01E05 历史重试，下载页任务有标题/季集（LOST10
2026 · S01E05）但整张卡没有海报。根因不在重试链路（1.10.2 已把
media 透传到底），而在海报解析那一步：记录的 tmdb_id 是正确的
329490，但 MP 自身的媒体详情页 tmdb:329490 都 500（“未识别到媒体
信息”）——实时查 TMDB 这条路对这部剧是断的；与此同时 MP 订阅页
LOST10 卡片是有海报的，说明海报在 MP 的存量数据里是有的。

本版三处改动：
- _normalize_task_media：保留 media 里的 poster（也认 image 键），
  只认 http(s) 绝对地址。
- _task_media：实时解析不到海报时，用任务自带的存量 poster 兜底。
- 三个 media 透传点都带上存量海报：重试用记录的 image、同步用
  mediainfo.get_poster_image()、字幕同理；重试的 tmdb_id 另用
  记录的 tmdb_id 兜底（现认不到时记录里的才是权威的）。
"""

import os
import re
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


def _make_manager():
    manager = CrossTransferTaskManager(
        lambda key: MagicMock(),
        download_path=tempfile.mkdtemp(prefix="csx-poster-fallback-test-"),
    )
    manager._notify_change = lambda: None
    manager._run = lambda *args, **kwargs: None
    return manager


def _make_source_file():
    return CloudFile(
        id="f1", name="05.mkv", is_directory=False,
        size=3000, sha1="aabbcc", md5="ddeeff",
    )


def _read_source(*parts):
    with open(_plugin_dir(*parts), encoding="utf-8") as handle:
        return handle.read()


class TestNormalizeTaskMediaPoster(unittest.TestCase):
    def test_keeps_http_poster(self):
        result = transfer_module._normalize_task_media({
            "tmdb_id": 329490, "media_type": "TV",
            "poster": "https://image.tmdb.org/t/p/w500/abc.jpg",
        })
        self.assertEqual(
            result.get("poster"), "https://image.tmdb.org/t/p/w500/abc.jpg"
        )

    def test_accepts_image_key(self):
        result = transfer_module._normalize_task_media({
            "media_type": "TV",
            "image": "https://image.tmdb.org/t/p/w500/abc.jpg",
        })
        self.assertEqual(
            result.get("poster"), "https://image.tmdb.org/t/p/w500/abc.jpg"
        )

    def test_drops_non_http_poster(self):
        for bad in ("", "  ", "/posters/abc.jpg", "ftp://x/y.jpg",
                    "javascript:alert(1)"):
            with self.subTest(bad=bad):
                result = transfer_module._normalize_task_media(
                    {"media_type": "TV", "poster": bad}
                )
                self.assertNotIn("poster", result)

    def test_poster_survives_with_other_fields(self):
        result = transfer_module._normalize_task_media({
            "tmdb_id": 329490, "media_type": "TV", "season": 1,
            "media_title": "LOST10",
            "poster": "https://image.tmdb.org/t/p/w500/abc.jpg",
            "target_episodes": [5],
        })
        self.assertEqual(result["tmdb_id"], 329490)
        self.assertEqual(result["media_type"], "电视剧")
        self.assertEqual(
            result["poster"], "https://image.tmdb.org/t/p/w500/abc.jpg"
        )


class TestCreateStoresPoster(unittest.TestCase):
    def test_create_from_cloud_file_forwards_poster(self):
        manager = _make_manager()
        task = manager.create_from_cloud_file(
            "quark", _make_source_file(), "u115", "/downloads", "05.mkv",
            media={
                "tmdb_id": 329490, "media_type": "TV", "season": 1,
                "media_title": "LOST10", "target_episodes": [5],
                "poster": "https://image.tmdb.org/t/p/w500/abc.jpg",
            },
        )
        self.assertEqual(
            task.get("poster"), "https://image.tmdb.org/t/p/w500/abc.jpg"
        )
        self.assertEqual(task.get("tmdb_id"), 329490)


class TestTaskMediaPosterFallback(unittest.TestCase):
    def _task(self, **overrides):
        task = {
            "id": "cross:abcdef123456",
            "task_kind": "cross_transfer",
            "parent_task_id": "",
            "title": "05.mkv",
            "status": "running",
            "phase": "running",
            "progress": 79,
            "transferred": 790,
            "total": 1000,
            "speed_bytes_per_second": 15 * 1024 * 1024,
        }
        task.update(overrides)
        return task

    def test_falls_back_to_stored_poster_when_resolver_misses(self):
        # LOST10 复现：tmdb_id 正确，但实时查 TMDB 返回空串。
        task = self._task(
            tmdb_id=329490, media_type="电视剧", season=1, year="2026",
            media_title="LOST10", target_episodes=[5],
            poster="https://image.tmdb.org/t/p/w500/abc.jpg",
        )
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "",
        )
        self.assertIsNotNone(torrent.media)
        self.assertEqual(
            torrent.media["poster"], "https://image.tmdb.org/t/p/w500/abc.jpg"
        )
        self.assertEqual(torrent.media["title"], "LOST10")
        self.assertEqual(torrent.name, "LOST10 S01E05")

    def test_resolver_result_wins_over_stored_poster(self):
        # 注意：同一 tmdb_id 在 600 秒内有过一次解析未命中会被短路，
        # 所以这里用另一个 tmdb_id，避免被前一个用例的 miss 缓存干扰。
        task = self._task(
            tmdb_id=326140, media_type="电视剧", season=1,
            media_title="今天是太阳雨", target_episodes=[18],
            poster="https://image.tmdb.org/t/p/w500/stored.jpg",
        )
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "https://img/live.jpg",
        )
        self.assertEqual(torrent.media["poster"], "https://img/live.jpg")

    def test_stored_poster_without_tmdb_id(self):
        # 现认不到 tmdb_id 时也能靠存量海报出图（标题/季集照常）。
        task = self._task(
            media_type="电视剧", season=1, media_title="LOST10",
            target_episodes=[5],
            poster="https://image.tmdb.org/t/p/w500/abc.jpg",
        )
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "",
        )
        self.assertIsNotNone(torrent.media)
        self.assertEqual(
            torrent.media["poster"], "https://image.tmdb.org/t/p/w500/abc.jpg"
        )

    def test_no_poster_anywhere_stays_none(self):
        task = self._task(
            tmdb_id=329490, media_type="电视剧", season=1,
            media_title="LOST10", target_episodes=[5],
        )
        torrent = _view.task_to_torrent(
            task, torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "",
        )
        self.assertIsNotNone(torrent.media)
        self.assertIsNone(torrent.media["poster"])


class TestRetryPassesStoredPoster(unittest.TestCase):
    def test_retry_media_uses_record_tmdb_id_and_image(self):
        source = _read_source("handlers", "sync", "retry.py")
        # tmdb_id：mediainfo 现认不到时用记录的兜底。
        self.assertIn(
            'getattr(mediainfo, "tmdb_id", None) or record.get("tmdb_id")',
            source,
        )
        # poster：记录建时存的 image 进任务。
        self.assertIn('"poster": str(record.get("image") or "")', source)

    def test_sync_and_subtitle_paths_pass_poster(self):
        sync_source = _read_source("handlers", "sync", "transfer_ops.py")
        self.assertIn("mediainfo.get_poster_image()", sync_source)
        subtitle_source = _read_source("handlers", "sync", "subtitles.py")
        self.assertIn("subtitle_mediainfo.get_poster_image()", subtitle_source)
        # 三个透传点的 media 字典都带 poster 键。
        for text in (sync_source, subtitle_source):
            self.assertTrue(
                re.search(r'"poster":\s*\(', text),
                "media dict should carry a poster entry",
            )


if __name__ == "__main__":
    unittest.main()
