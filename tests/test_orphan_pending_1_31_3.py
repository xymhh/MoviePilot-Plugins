"""v1.31.3 漏斗孤儿待终态记录修复 + 入队失败清 funnel_route。

覆盖：
- A1：_fail_row 转 held 时弹出待终态记录并通知刷新；重试分支不弹；
- A2：启动扫孤儿——行 held/done/不存在 → 弹；行 queued/failed → 留；
  非漏斗记录不动；阶段 1 关闭时不扫；幂等；
- A3：入队失败时 pending 记录的 funnel_route 被清掉（走旧流水线）；
  入队成功时保留；
- A4（简报三处之外，同一类口子）：租约回收转 held 时也弹出记录。

harness：复用 test_funnel_stage1_1_29_7 的 _DbMixin / SyncHandler /
funnel_module；A3 用 SyncHandler.__new__ + 桩 helper。
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_funnel_stage1_1_29_7 import (  # noqa: E402
    SyncHandler,
    _DbMixin,
    _payload,
    _tv_media,
    funnel_module,
)

FUNNEL_RETRY_DELAYS = funnel_module.FUNNEL_RETRY_DELAYS


class FailRowHeldPopsPendingTests(_DbMixin):
    """A1：挂起即终态——转 held 时弹出待终态记录并通知刷新。"""

    def _attach_notify(self):
        calls = []
        self.owner._notify_offline_pending_changed = lambda count: calls.append(count)
        self.owner.count_offline_monitor_pending = lambda pending: sum(
            1 for item in (pending or {}).values()
            if isinstance(item, dict) and not item.get("funnel_route")
        )
        return calls

    def test_held_pops_pending_record_and_notifies(self):
        """5 次失败转 held：记录弹出，通知带刷新后计数 0。"""
        calls = self._attach_notify()
        self._pending_item("pk-tv-09", funnel_route=True)
        row = self._enqueue_tv()
        for _ in range(4):
            current = self.repo.get_by_id(row["id"])
            self.assertEqual(self.service._fail_row(self.repo, current, "boom"), "failed")
            self.assertIn("pk-tv-09", self.owner._pending)
        current = self.repo.get_by_id(row["id"])
        self.assertEqual(self.service._fail_row(self.repo, current, "boom"), "held")
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")
        self.assertNotIn("pk-tv-09", self.owner._pending)
        self.assertEqual(calls, [0])

    def test_retry_branch_keeps_pending_record(self):
        """重试分支（attempts 未满）：记录保留，不通知。"""
        calls = self._attach_notify()
        self._pending_item("pk-tv-09", funnel_route=True)
        row = self._enqueue_tv()
        current = self.repo.get_by_id(row["id"])
        self.assertEqual(self.service._fail_row(self.repo, current, "boom"), "failed")
        self.assertIn("pk-tv-09", self.owner._pending)
        self.assertEqual(calls, [])

    def test_held_without_pending_record_is_noop(self):
        """行已无待终态记录时转 held：不抛错，行照样 held。"""
        row = self._enqueue_tv()
        for _ in range(4):
            current = self.repo.get_by_id(row["id"])
            self.service._fail_row(self.repo, current, "boom")
        current = self.repo.get_by_id(row["id"])
        self.assertEqual(self.service._fail_row(self.repo, current, "boom"), "held")
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")


class RepairOrphanPendingTests(_DbMixin):
    """A2：启动扫孤儿——只弹行已终态/不存在的漏斗记录。"""

    def _setup_rows(self):
        held = self.repo.enqueue(_payload(pending_key="pk-held", episode=9))[0]
        self.repo.mark_held(held["id"], "人工挂起")
        done = self.repo.enqueue(_payload(pending_key="pk-done", episode=10))[0]
        self.repo.mark_done(done["id"], reason="已完成")
        queued = self.repo.enqueue(_payload(pending_key="pk-queued", episode=11))[0]
        failed = self.repo.enqueue(_payload(pending_key="pk-failed", episode=12))[0]
        self.repo.mark_failed(failed["id"], int(time.time()) + 60, "boom")
        self._pending_item("pk-held", funnel_route=True)
        self._pending_item("pk-done", funnel_route=True)
        self._pending_item("pk-queued", funnel_route=True)
        self._pending_item("pk-failed", funnel_route=True)
        self._pending_item("pk-missing", funnel_route=True)
        self._pending_item("pk-plain")
        return held, done, queued, failed

    def test_sweep_pops_held_done_missing_only(self):
        """held/done/无行 → 弹；queued/failed/非漏斗 → 留。"""
        self._setup_rows()
        removed = self.service.repair_orphan_pending_records()
        self.assertEqual(removed, 3)
        self.assertNotIn("pk-held", self.owner._pending)
        self.assertNotIn("pk-done", self.owner._pending)
        self.assertNotIn("pk-missing", self.owner._pending)
        self.assertIn("pk-queued", self.owner._pending)
        self.assertIn("pk-failed", self.owner._pending)
        self.assertIn("pk-plain", self.owner._pending)

    def test_sweep_is_idempotent(self):
        """第二遍扫不到孤儿，返回 0。"""
        self._setup_rows()
        self.assertEqual(self.service.repair_orphan_pending_records(), 3)
        self.assertEqual(self.service.repair_orphan_pending_records(), 0)

    def test_sweep_skipped_when_phase1_off(self):
        """阶段 1 关闭时漏斗记录归监控驱动，不是孤儿，不扫。"""
        self._setup_rows()
        self.owner._funnel_phase1_enabled = False
        self.assertEqual(self.service.repair_orphan_pending_records(), 0)
        self.assertIn("pk-held", self.owner._pending)
        self.assertIn("pk-missing", self.owner._pending)

    def test_sweep_empty_pending_returns_zero(self):
        """没有待终态记录时返回 0，不报错。"""
        self.assertEqual(self.service.repair_orphan_pending_records(), 0)


class EnqueueFailureClearsFunnelRouteTests(unittest.TestCase):
    """A3：入队失败走旧流水线——pending 记录的 funnel_route 被清掉。"""

    def _batch_handler(self, enqueue_side_effect):
        handler = SyncHandler.__new__(SyncHandler)
        pending = {}
        saved = {}
        notified = []
        object.__setattr__(handler, "_OFFLINE_PENDING_KEY", "pending_offline_strm")
        object.__setattr__(handler, "_get_data", lambda key: pending)
        object.__setattr__(handler, "_offline_pending_lock", threading.RLock())
        object.__setattr__(
            handler, "_serialize_mediainfo",
            lambda mediainfo: {"tmdb_id": 1, "type": "电视剧", "title": "x"},
        )
        object.__setattr__(
            handler, "_pending_identity",
            lambda *args: ("pk-test-1", "share", ""),
        )
        object.__setattr__(
            handler, "_build_pending_record",
            lambda **kwargs: {
                "funnel_route": bool(kwargs.get("funnel_route")),
                "file_name": "f.mkv",
            },
        )
        object.__setattr__(
            handler, "enqueue_funnel_from_pending", enqueue_side_effect,
        )
        object.__setattr__(
            handler, "_save_offline_pending", lambda data: saved.update(data),
        )
        object.__setattr__(
            handler, "count_offline_monitor_pending",
            lambda data: sum(
                1 for item in (data or {}).values()
                if isinstance(item, dict) and not item.get("funnel_route")
            ),
        )
        object.__setattr__(
            handler, "_notify_offline_pending_changed", notified.append,
        )
        handler._saved_store = saved
        handler._notified = notified
        return handler

    def _item(self):
        return {
            "share_url": "https://example.invalid/share-1",
            "cloud_dir": "/dir",
            "file_name": "f.mkv",
            "staging_dir": "/dir",
            "staging_name": "f.mkv",
            "result_key": "f-1",
            "funnel_route": True,
            "file_size": 100,
        }

    def test_enqueue_failure_clears_funnel_route(self):
        """入队抛错：记录留着但 funnel_route=False，离线监控接手（计数 1）。"""
        def _raise(*args, **kwargs):
            raise RuntimeError("db down")

        handler = self._batch_handler(_raise)
        result = handler._queue_file_finalize_batch(
            items=[self._item()], mediainfo=object(),
        )
        self.assertEqual(result, {"f-1": "pk-test-1"})
        # 注意：_queue_file_finalize_batch 里 pending = _get_data(...) or {}
        # ——空 dict 是 falsy，方法往新 dict 里写、再经 _save_offline_pending
        # 持久化；断言读持久化后的 store（生产上 _save_data 就是这么存的）。
        record = handler._saved_store.get("pk-test-1")
        self.assertIsNotNone(record)
        self.assertFalse(record.get("funnel_route"))
        self.assertEqual(handler._notified, [1])

    def test_enqueue_success_keeps_funnel_route(self):
        """入队成功：funnel_route 保留，离线监控不计入（计数 0）。"""
        handler = self._batch_handler(lambda *args, **kwargs: ({"id": 1}, True))
        result = handler._queue_file_finalize_batch(
            items=[self._item()], mediainfo=object(),
        )
        self.assertEqual(result, {"f-1": "pk-test-1"})
        self.assertTrue(handler._saved_store["pk-test-1"].get("funnel_route"))
        self.assertEqual(handler._notified, [0])

    def test_enqueue_returns_no_row_clears_funnel_route(self):
        """存储不可用返回 (None, False)：没建成行，同样清 funnel_route。"""
        handler = self._batch_handler(lambda *args, **kwargs: (None, False))
        result = handler._queue_file_finalize_batch(
            items=[self._item()], mediainfo=object(),
        )
        self.assertEqual(result, {"f-1": "pk-test-1"})
        record = handler._saved_store.get("pk-test-1")
        self.assertIsNotNone(record)
        self.assertFalse(record.get("funnel_route"))
        self.assertEqual(handler._notified, [1])


class ReclaimHeldPopsPendingTests(_DbMixin):
    """A4（简报三处之外，同一类口子）：租约回收转 held 时也弹出记录。"""

    def _expired_processing_row(self, pending_key="pk-tv-09", **item_extra):
        item = self._pending_item(pending_key, **item_extra)
        row, _ = self.service.enqueue_from_pending(item, pending_key, _tv_media())
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now - 5, now=now))
        return row, item, now

    def test_reclaim_tracker_unavailable_holds_and_pops(self):
        """MP 整理状态查不到 → held，待终态记录弹出。"""
        row, _item, now = self._expired_processing_row(
            "pk-tv-09", mp_organize={"task_id": "trk-1"},
        )
        self.assertIn("pk-tv-09", self.owner._pending)
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")
        self.assertNotIn("pk-tv-09", self.owner._pending)

    def test_reclaim_not_landed_requeues_and_keeps_record(self):
        """无 MP 整理身份 + 落盘实查未落盘 → 重回队列，记录保留。"""
        row, _item, now = self._expired_processing_row("pk-tv-09")
        self.assertIn("pk-tv-09", self.owner._pending)
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 1, "held": 0})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")
        self.assertIn("pk-tv-09", self.owner._pending)

    def test_reclaim_missing_pending_item_holds_without_error(self):
        """记录已不在 → held（无记录可弹，不报错）。"""
        row, _item, now = self._expired_processing_row("pk-tv-09")
        self.owner._pending.pop("pk-tv-09", None)
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")


if __name__ == "__main__":
    unittest.main()
