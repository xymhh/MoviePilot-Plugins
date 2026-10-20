"""同一集存在多条历史时的重试定位与展示选择（1.7.22）。"""

import sys
import threading
import unittest

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
core_pkg.CloudFile = object

_history = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = _history.HistoryService


def _record(record_id, status, file_name, **extra):
    return {
        "time": "2026-10-19 23:15:44",
        "share_url": "https://pan.quark.cn/s/example",
        "file_name": file_name,
        "status": status,
        "record_id": record_id,
        "type": "电视剧",
        "tmdb_id": 304452,
        "season": 1,
        "episode": 7,
        "transfer_mode": "cross",
        "task_types": ["cross_transfer"],
        **extra,
    }


class _HistoryStore(HistoryService):
    """内存历史存储，隔离数据库、锁和平台历史写入。"""

    def __init__(self, records):
        super().__init__(None)
        object.__setattr__(self, "_store", {
            "history": [dict(record) for record in records],
        })
        object.__setattr__(self, "_offline_pending_lock", threading.RLock())

    def _get_data(self, key):
        return self._store.get(key)

    def _save_data(self, key, value):
        self._store[key] = value

    def _record_platform_transfer_histories(self, records):
        return None

    def verify_landed_records(self, records):
        return {}


class TestRetryUpdatesExactRecord(unittest.TestCase):
    """重试改了扩展名，也必须按 record_id 更新原失败记录。"""

    def test_prefers_record_id_over_changed_file_name_identity(self):
        failed = _record(
            "failed-record", "失败", "玉簟秋 - S01E07 - 第7集.mkv",
            failure_reason="夸克网盘分享链接无效：好友已取消了分享",
        )
        success = _record(
            "success-record", "成功", "玉簟秋 - S01E07 - 第7集.iso",
            landed=True, landed_reason="name_match",
        )
        service = _HistoryStore([failed, success])
        incoming = dict(failed)
        incoming.update({
            "file_name": "玉簟秋 - S01E07 - 第7集.iso",
            "status": "处理中",
            "retry_reused_existing": True,
        })
        incoming.pop("failure_reason", None)

        service.append_history_records([incoming], reopen_terminal=True)

        by_id = {
            record["record_id"]: record
            for record in service._store["history"]
        }
        self.assertEqual(by_id["failed-record"]["status"], "处理中")
        self.assertEqual(
            by_id["failed-record"]["file_name"],
            "玉簟秋 - S01E07 - 第7集.iso",
        )
        self.assertNotIn("failure_reason", by_id["failed-record"])
        # 同一集的另一条成功记录不能被这次重试改写。
        self.assertEqual(by_id["success-record"]["status"], "成功")
        self.assertEqual(
            by_id["success-record"]["file_name"],
            "玉簟秋 - S01E07 - 第7集.iso",
        )


class TestHistoryGroupDisplay(unittest.TestCase):
    """页面同集去重时，成功记录应压过旧的失败记录。"""

    @staticmethod
    def _group(records):
        return {"type": "电视剧", "records": records}

    def test_success_hides_older_failed_duplicate(self):
        failed = _record(
            "failed-record", "失败", "玉簟秋 - S01E07 - 第7集.mkv",
            failure_reason="夸克网盘分享链接无效：好友已取消了分享",
        )
        success = _record(
            "success-record", "成功", "玉簟秋 - S01E07 - 第7集.iso",
            landed=True,
        )
        prepared = HistoryService.prepare_history_group(
            self._group([failed, success])
        )
        self.assertEqual(len(prepared["records"]), 1)
        self.assertEqual(prepared["records"][0]["status"], "成功")
        self.assertEqual(prepared["records"][0]["file_name"], success["file_name"])
        self.assertEqual(prepared["success_count"], 1)
        self.assertEqual(prepared["failed_count"], 0)

    def test_processing_hides_older_failed_duplicate(self):
        failed = _record(
            "failed-record", "失败", "玉簟秋 - S01E07 - 第7集.mkv",
        )
        processing = _record(
            "retry-record", "处理中", "玉簟秋 - S01E07 - 第7集.iso",
        )
        prepared = HistoryService.prepare_history_group(
            self._group([failed, processing])
        )
        self.assertEqual(len(prepared["records"]), 1)
        self.assertEqual(prepared["records"][0]["status"], "处理中")
        self.assertEqual(prepared["pending_count"], 1)
        self.assertEqual(prepared["failed_count"], 0)


if __name__ == "__main__":
    unittest.main()
