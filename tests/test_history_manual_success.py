"""历史记录批量手动标记成功（1.6.54）。

覆盖：待确认/处理中/下载中 → 成功、终态跳过、manual_success 阻止自动终态回写、
空标识与不存在记录报错。
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


def _record(time_value, status, name="f1.mkv", **extra):
    return {
        "time": time_value,
        "share_url": "https://pan.example.com/s/" + time_value.upper(),
        "file_name": name,
        "status": status,
        **extra,
    }


class _ManualSuccessService(HistoryService):
    """内存历史存储，隔离数据库与锁依赖。"""

    def __init__(self, records, pending=None):
        super().__init__(None)
        object.__setattr__(self, "_store", {
            "history": [dict(record) for record in records],
        })
        if pending is not None:
            self._store["offline_pending"] = pending
        object.__setattr__(self, "_offline_pending_lock", threading.RLock())
        object.__setattr__(self, "save_count", 0)
        object.__setattr__(self, "verify_calls", 0)

    def _get_data(self, key):
        return self._store.get(key)

    def _save_data(self, key, value):
        self._store[key] = value
        object.__setattr__(self, "save_count", self.save_count + 1)

    # 落盘自检桩：任何调用都判「查无实据」，用来验证手动成功不会被翻回待确认。
    def verify_landed_records(self, records):
        object.__setattr__(self, "verify_calls", self.verify_calls + 1)
        return {id(record): {"landed": False, "reason": "missing_in_dir"} for record in records}

    def _record_platform_transfer_histories(self, records):
        return None


class TestMarkHistoryRecordsSuccess(unittest.TestCase):
    """批量手动标记成功的主路径与守卫。"""

    def test_marks_pending_and_unverified_only(self):
        """待确认/处理中/下载中 → 成功；成功/失败/跳过 → 跳过。"""
        service = _ManualSuccessService([
            _record("t1", "待确认"),
            _record("t2", "处理中"),
            _record("t3", "下载中"),
            _record("t4", "成功"),
            _record("t5", "失败"),
            _record("t6", "跳过"),
        ])
        result = service.mark_history_records_success(
            list(service._store["history"])
        )
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["skipped"], 3)
        self.assertEqual(result["selected"], 6)

        by_time = {item["time"]: item for item in service._store["history"]}
        for time_value, origin in (("t1", "待确认"), ("t2", "处理中"), ("t3", "下载中")):
            record = by_time[time_value]
            self.assertEqual(record["status"], "成功")
            self.assertTrue(record["manual_success"])
            self.assertEqual(record["manual_success_from"], origin)
            self.assertIn("manual_success_at", record)
        self.assertEqual(by_time["t4"]["status"], "成功")
        self.assertNotIn("manual_success", by_time["t4"])
        self.assertEqual(by_time["t5"]["status"], "失败")
        self.assertEqual(by_time["t6"]["status"], "跳过")

    def test_manual_success_blocks_auto_terminal_status(self):
        """标记后自动终态（失败/成功）都不得再覆盖该记录。"""
        service = _ManualSuccessService(
            [_record("t1", "处理中", finalize_key="k1")],
            pending={"k1": {"file_name": "f1.mkv", "cloud_dir": "/媒体/剧集/Show/Season 01"}},
        )
        service.mark_history_records_success([service._store["history"][0]])
        record = service._store["history"][0]
        self.assertEqual(record["status"], "成功")

        matched = service._mark_offline_history_status_batch({"k1"}, "失败", "boom")
        self.assertEqual(matched, 1)
        self.assertEqual(record["status"], "成功")
        self.assertNotIn("failure_reason", record)

        matched = service._mark_offline_history_status_batch({"k1"}, "成功")
        self.assertEqual(matched, 1)
        self.assertEqual(record["status"], "成功")
        # 手动成功不触发落盘自检（否则会被判「待确认」）
        self.assertEqual(service.verify_calls, 0)

    def test_reselect_is_idempotent(self):
        """重复标记同一批：第二次全部跳过，不重复写盘。"""
        service = _ManualSuccessService([_record("t1", "待确认")])
        identities = [dict(service._store["history"][0])]
        first = service.mark_history_records_success(identities)
        saves = service.save_count
        second = service.mark_history_records_success(identities)
        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(service.save_count, saves)

    def test_unknown_record_raises(self):
        service = _ManualSuccessService([_record("t1", "待确认")])
        with self.assertRaises(LookupError):
            service.mark_history_records_success(
                [{"share_url": "https://pan.example.com/s/NOPE", "file_name": "nope.mkv"}]
            )

    def test_empty_identity_raises(self):
        service = _ManualSuccessService([_record("t1", "待确认")])
        with self.assertRaises(ValueError):
            service.mark_history_records_success([{"time": "", "file_name": ""}])


if __name__ == "__main__":
    unittest.main()
