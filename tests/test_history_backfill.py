"""历史记录补写（1.7.102）。

覆盖：终态缺记录时按落盘事实补写、重复补写跳过、字段不足放弃、
后处理项多集补写。
"""

import sys
import threading
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.context",
    "app.core.metainfo", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.subscribe_oper", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.log",
    "app.utils", "app.utils.string", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.history",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.drive.common", "cloudsubscribex.search",
    "cloudsubscribex.search.types", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
)
set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudDriveCapability = MagicMock()
core_pkg.CloudFile = MagicMock()

_history = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = _history.HistoryService


class _BackfillService(HistoryService):
    """内存历史存储 + 最小可用的历史条目构造器。"""

    def __init__(self, records=None):
        super().__init__(None)
        object.__setattr__(self, "_store", {
            "history": [dict(record) for record in (records or [])],
        })
        object.__setattr__(self, "_offline_pending_lock", threading.RLock())
        object.__setattr__(self, "save_count", 0)

    def _get_data(self, key):
        return self._store.get(key)

    def _save_data(self, key, value):
        self._store[key] = value
        object.__setattr__(self, "save_count", self.save_count + 1)

    def _deserialize_mediainfo(self, media_data):
        if not media_data:
            return None
        return MagicMock(title=media_data.get("title") or "凡人修仙传")

    def _build_transfer_history_item(self, **kwargs):
        return {
            "title": getattr(kwargs.get("mediainfo"), "title", ""),
            "status": kwargs.get("status"),
            "file_name": kwargs.get("file_name"),
            "cloud_dir": kwargs.get("cloud_dir"),
            "season": kwargs.get("season"),
            "episode": kwargs.get("episode"),
            "file_size": kwargs.get("file_size"),
            "backfilled": kwargs.get("backfilled"),
        }


def _mediainfo(title="凡人修仙传"):
    return MagicMock(title=title)


def _subscribe():
    return MagicMock(id=271, name="凡人修仙传", season=1)


class TestAppendBackfillRecord(unittest.TestCase):

    def test_appends_and_saves(self):
        service = _BackfillService()
        record = service._append_backfill_history_record(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            episode=4,
            file_name="凡人修仙传 - S01E04 - 第4集.mp4",
            cloud_dir="/转存/凡人修仙传/Season 1",
            file_size=1261769971,
        )
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "成功")
        self.assertTrue(record["backfilled"])
        self.assertEqual(service.save_count, 1)
        self.assertEqual(len(service._store["history"]), 1)

    def test_same_record_is_not_written_twice(self):
        service = _BackfillService()
        kwargs = dict(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            episode=5,
            file_name="凡人修仙传 - S01E05 - 第5集.mp4",
            cloud_dir="/转存/凡人修仙传/Season 1",
        )
        self.assertIsNotNone(service._append_backfill_history_record(**kwargs))
        self.assertIsNone(service._append_backfill_history_record(**kwargs))
        self.assertEqual(service.save_count, 1)
        self.assertEqual(len(service._store["history"]), 1)

    def test_missing_fields_are_skipped(self):
        service = _BackfillService()
        self.assertIsNone(service._append_backfill_history_record(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            episode=0,
            file_name="x.mp4",
            cloud_dir="/转存/x/Season 1",
        ))
        self.assertIsNone(service._append_backfill_history_record(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            episode=6,
            file_name="",
            cloud_dir="/转存/x/Season 1",
        ))
        self.assertEqual(service.save_count, 0)


class TestEpisodeLevelDedupe(unittest.TestCase):

    def _existing(self, episode):
        return {
            "title": "凡人修仙传",
            "season": 1,
            "episode": episode,
            "status": "成功",
            "file_name": "01.mp4",
            "cloud_dir": "/别的目录/Season 1",
        }

    def test_existing_episode_blocks_backfill_even_from_other_dir(self):
        service = _BackfillService([self._existing(4)])
        record = service._append_backfill_history_record(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            episode=4,
            file_name="凡人修仙传 - S01E04 - 第4集.mp4",
            cloud_dir="/媒体库/凡人修仙传/Season 1",
        )
        self.assertIsNone(record)
        self.assertEqual(service.save_count, 0)

    def test_failed_record_does_not_block_backfill(self):
        failed = self._existing(6)
        failed["status"] = "失败"
        service = _BackfillService([failed])
        record = service._append_backfill_history_record(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            episode=6,
            file_name="凡人修仙传 - S01E06 - 第6集.mp4",
            cloud_dir="/媒体库/凡人修仙传/Season 1",
        )
        self.assertIsNotNone(record)
        self.assertEqual(service.save_count, 1)


class TestBackfillFromPending(unittest.TestCase):

    def test_writes_every_success_episode(self):
        service = _BackfillService()
        item = {
            "mediainfo": {"title": "凡人修仙传"},
            "subscribe_id": 271,
            "season": 1,
            "episode": 35,
            "success_episodes": [34, 35],
            "file_name": "凡人修仙传 - S01E35 - 第35集.mp4",
            "cloud_dir": "/转存/凡人修仙传/Season 1",
            "file_size": 1627858136,
        }
        self.assertEqual(service._backfill_history_from_pending(item), 2)
        self.assertEqual(len(service._store["history"]), 2)

    def test_requires_mediainfo(self):
        service = _BackfillService()
        item = {
            "mediainfo": {},
            "season": 1,
            "episode": 9,
            "file_name": "a.mp4",
            "cloud_dir": "/转存/a/Season 1",
        }
        self.assertEqual(service._backfill_history_from_pending(item), 0)
        self.assertEqual(service.save_count, 0)


if __name__ == "__main__":
    unittest.main()
