"""历史失败记录批量重试测试（1.6.46）。

覆盖：混合批次计数、单次 20 条上限、串行限流（整批在飞不超过 2）、
单条 submit_history_retry 行为不变。
"""

import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_package,
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

_retry = load_module(
    "cloudsubscribex.handlers.sync.retry",
    "plugins.v2/cloudsubscribex/handlers/sync/retry.py",
)
HistoryRetryService = _retry.HistoryRetryService


def _failed(time_value, share_url, file_name):
    return {
        "time": time_value, "share_url": share_url, "file_name": file_name,
        "status": "失败",
    }


class _BaseRetryService(HistoryRetryService):
    """内存历史存储 + 可控重试线程的测试基类。"""

    def __init__(self, records):
        super().__init__(None)
        object.__setattr__(self, "_store", {"history": list(records)})
        object.__setattr__(self, "_history_changed", None)
        object.__setattr__(self, "_ran", [])
        object.__setattr__(
            self, "_history_retry_state", HistoryService._history_retry_state
        )

    def _get_data(self, key):
        return self._store.get(key)

    def _find_history_record(self, record_time, share_url, file_name):
        return HistoryRetryService._find_history_record(
            self, record_time, share_url, file_name
        )

    @staticmethod
    def _history_retry_key(record_time, share_url, file_name):
        return HistoryRetryService._history_retry_key(
            record_time, share_url, file_name
        )

    def _is_ed2k_url(self, url):
        return False

    def append_history_records(self, records, reopen_terminal=True):
        for record in records:
            existing = next(
                (
                    item for item in self._store["history"]
                    if str(item.get("time") or "") == str(record.get("time") or "")
                       and str(item.get("share_url") or "") == str(record.get("share_url") or "")
                       and str(item.get("file_name") or "") == str(record.get("file_name") or "")
                ),
                None,
            )
            if existing is not None:
                existing.clear()
                existing.update(record)
            else:
                self._store["history"].append(record)

    def _history_changed(self):
        pass


class SingleRetryDummy(_BaseRetryService):
    """使用真实 submit_history_retry，仅把后台执行换成桩。"""

    def _run_history_retry(self, key, record_time, share_url, file_name):
        self._ran.append(key)
        with _retry._retry_inflight_lock:
            _retry._retry_inflight_keys.discard(key)


class BatchOnlyDummy(_BaseRetryService):
    """批量分类测试：只统计候选，不起真实提交线程。"""

    def _run_history_retry_batch(self, candidates):
        object.__setattr__(self, "_batch_candidates", list(candidates))


class RateLimitDummy(_BaseRetryService):
    """限流测试：submit 桩把 key 记入模块在飞集合，稍后释放。"""

    def __init__(self, records):
        super().__init__(records)
        object.__setattr__(self, "_observed", [])
        object.__setattr__(self, "_lock", threading.Lock())

    def submit_history_retry(self, record_time, share_url, file_name):
        key = self._history_retry_key(record_time, share_url, file_name)
        with self._lock:
            _retry._retry_inflight_keys.add(key)
            # 记录当前在飞总数（本批桩实现即模块集合大小）
            self._observed.append(len(_retry._retry_inflight_keys))

        def _release():
            time.sleep(0.05)
            with _retry._retry_inflight_lock:
                _retry._retry_inflight_keys.discard(key)

        threading.Timer(0.05, _release).start()
        return {"status": "处理中"}


class TestSubmitHistoryRetryBatch(unittest.TestCase):
    """批量重试：混合计数、上限、限流、单条行为不变。"""

    def setUp(self):
        with _retry._retry_inflight_lock:
            _retry._retry_inflight_keys.clear()

    def tearDown(self):
        with _retry._retry_inflight_lock:
            _retry._retry_inflight_keys.clear()

    def test_mixed_batch_counts(self):
        """可重试 / 不可重试 / 不存在 / 在飞 → submitted/skipped 计数正确。"""
        records = [
            _failed("t1", "https://pan.example.com/s/AAA", "f1.mkv"),
            {
                "time": "t2", "share_url": "https://pan.example.com/s/BBB",
                "file_name": "f2.mkv", "status": "成功",
                "source_file_name": "f2.mkv",
            },
            {
                "time": "t3", "share_url": "https://pan.example.com/s/CCC",
                "file_name": "f3.mkv", "status": "成功",
            },
            _failed("t4", "https://pan.example.com/s/DDD", "f4.mkv"),
        ]
        service = BatchOnlyDummy(records)
        inflight_key = service._history_retry_key(
            "t4", "https://pan.example.com/s/DDD", "f4.mkv"
        )
        with _retry._retry_inflight_lock:
            _retry._retry_inflight_keys.add(inflight_key)

        result = service.submit_history_retry_batch([
            {"time": "t1", "share_url": "https://pan.example.com/s/AAA", "file_name": "f1.mkv"},
            {"time": "t2", "share_url": "https://pan.example.com/s/BBB", "file_name": "f2.mkv"},
            {"time": "t3", "share_url": "https://pan.example.com/s/CCC", "file_name": "f3.mkv"},
            {"time": "t4", "share_url": "https://pan.example.com/s/DDD", "file_name": "f4.mkv"},
            {"time": "t5", "share_url": "https://pan.example.com/s/EEE", "file_name": "f5.mkv"},
        ])

        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["skipped"], 3)
        by_name = {item["file_name"]: item for item in result["details"]}
        self.assertTrue(by_name["f1.mkv"]["ok"])
        self.assertTrue(by_name["f2.mkv"]["ok"])
        self.assertFalse(by_name["f3.mkv"]["ok"])
        self.assertEqual(by_name["f4.mkv"]["message"], "正在重试中")
        self.assertEqual(by_name["f5.mkv"]["message"], "记录不存在")
        candidates = getattr(service, "_batch_candidates", [])
        self.assertEqual(len(candidates), 2)

    def test_more_than_limit_raises(self):
        """超过 20 条直接报错，不静默截断。"""
        service = BatchOnlyDummy([])
        batch = [
            {"time": f"t{index}", "share_url": f"https://x/{index}", "file_name": f"f{index}.mkv"}
            for index in range(21)
        ]
        with self.assertRaises(ValueError) as ctx:
            service.submit_history_retry_batch(batch)
        self.assertIn("20", str(ctx.exception))

    def test_rate_limit_keeps_inflight_at_most_two(self):
        """串行提交 + 限流：整批在飞数量不超过 2。"""
        candidates = [
            {"time": f"t{index}", "share_url": f"https://x/{index}", "file_name": f"f{index}.mkv"}
            for index in range(4)
        ]
        service = RateLimitDummy([])
        sleeps = []
        original_time = _retry.time

        class _TimeStub:
            @staticmethod
            def sleep(seconds):
                sleeps.append(seconds)

        object.__setattr__(_retry, "time", _TimeStub)
        try:
            service._run_history_retry_batch(candidates)
        finally:
            object.__setattr__(_retry, "time", original_time)

        self.assertEqual(len(service._observed), 4)
        self.assertLessEqual(max(service._observed), 2)
        self.assertGreaterEqual(len(sleeps), 3)

    def test_single_retry_behavior_unchanged(self):
        """单条 submit_history_retry 行为不变：入队、置处理中、在飞去重。"""
        record = _failed("t1", "https://pan.example.com/s/AAA", "f1.mkv")
        service = SingleRetryDummy([record])
        key = service._history_retry_key(
            "t1", "https://pan.example.com/s/AAA", "f1.mkv"
        )
        result = service.submit_history_retry(
            "t1", "https://pan.example.com/s/AAA", "f1.mkv"
        )
        self.assertEqual(result, {"status": "处理中"})
        stored = next(
            item for item in service._store["history"]
            if item.get("time") == "t1"
        )
        self.assertEqual(stored["status"], "处理中")
        self.assertEqual(len(service._ran), 1)
        # 后台桩完成后在飞集合已清理（与真实 _run_history_retry 的 finally 语义一致）
        with _retry._retry_inflight_lock:
            self.assertNotIn(key, _retry._retry_inflight_keys)
        # 记录已是「处理中」→ 不可重试（真实语义：处理中禁止重复提交）
        with self.assertRaises(ValueError):
            service.submit_history_retry(
                "t1", "https://pan.example.com/s/AAA", "f1.mkv"
            )

    def test_single_retry_validation_unchanged(self):
        """单条校验不变：找不到记录 / 不可重试 / 仍在飞都要报错。"""
        service = SingleRetryDummy([
            _failed("t1", "https://pan.example.com/s/AAA", "f1.mkv"),
            {
                "time": "t2", "share_url": "https://pan.example.com/s/BBB",
                "file_name": "f2.mkv", "status": "成功",
            },
        ])
        with self.assertRaises(ValueError):
            service.submit_history_retry("missing", "https://x", "f.mkv")
        with self.assertRaises(ValueError):
            service.submit_history_retry("t2", "https://pan.example.com/s/BBB", "f2.mkv")

        key = service._history_retry_key("t1", "https://pan.example.com/s/AAA", "f1.mkv")
        with _retry._retry_inflight_lock:
            _retry._retry_inflight_keys.add(key)
        with self.assertRaises(ValueError):
            service.submit_history_retry("t1", "https://pan.example.com/s/AAA", "f1.mkv")


if __name__ == "__main__":
    unittest.main()
