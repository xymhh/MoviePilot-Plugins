"""v1.28.0：频道监控手动转存（错过的条目可补转）+ 详情/按钮 UI。

后端用真实 TgChannelMonitor（真实存储、真实队列、真实 _transfer_group /
_apply_group_outcome）；只有插件注入的转存处理器（submit_candidate 边界）
用记录型函数代替。前端按已接受的静态接线断言做法校验。
"""

import os
import threading
import time
import unittest
from pathlib import Path
import tempfile

from test_tg_interrupt_retry import FakeOwner, TgChannelMonitor, tg_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(
    ROOT, "frontend", "cloudsubscribex", "src", "components", "dashboard",
)


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


def _row(item_id, status, **extra):
    row = {
        "id": item_id,
        "title": f"多喜一家人 S01E09 {item_id}",
        "links": [f"https://115cdn.com/s/{item_id}"],
        "status": status,
        "matched_subscribe_id": 420,
        "created_at": 1,
    }
    row.update(extra)
    return row


class _MonitorCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.monitor = TgChannelMonitor(FakeOwner(Path(self.tempdir.name)))
        self.worker_starts = 0

        def _fake_start():
            self.worker_starts += 1

        # 默认不起真实线程：入队后由用例手动驱动 worker 那一步。
        self.monitor._start_transfer_worker = _fake_start

    def tearDown(self):
        try:
            self.monitor._stop_transfer_worker()
        finally:
            self.tempdir.cleanup()

    def seed(self, rows):
        self.monitor._store.save(tg_module.ITEMS_KEY, rows)

    def rows(self):
        return {row["id"]: row for row in self.monitor._load_items()}

    def drain(self):
        """模拟 worker 取一批：与 _transfer_loop 同一入口 _process_transfer_items。"""
        ids = []
        while not self.monitor._transfer_queue.empty():
            ids.append(self.monitor._queue_entry_id(self.monitor._transfer_queue.get_nowait()))
        if ids:
            self.monitor._process_transfer_items(ids, threading.Event())
        return ids


class ManualTransferStateRulesTest(_MonitorCase):
    def test_allowed_statuses_are_requeued(self):
        self.seed([
            _row("s", "skipped", skip_reason="本轮无新集", auto_transfer_error="未转存：本轮无新集"),
            _row("f", "failed", auto_transfer_error="链接已失效", failure_kind="permanent"),
            _row("i", "ignored"),
            _row("n", "new"),
        ])
        outcome = self.monitor.manual_transfer_items(["s", "f", "i", "n"])
        self.assertEqual(outcome["submitted"], 4)
        rows = self.rows()
        for item_id in "sfin":
            self.assertEqual(rows[item_id]["status"], "queued")
            self.assertEqual(rows[item_id]["auto_transfer_error"], "")
            self.assertIn("manual_transfer_at", rows[item_id])
        self.assertEqual(self.monitor._transfer_queue.qsize(), 4)

    def test_rejected_statuses_return_reasons(self):
        self.seed([
            _row("q", "queued"),
            _row("t", "transferring"),
            _row("p", "processing"),
            _row("d", "transferred"),
            _row("x", "weird"),
            _row("l", "skipped", links=[]),
        ])
        outcome = self.monitor.manual_transfer_items(["q", "t", "p", "d", "x", "l", "gone"])
        self.assertEqual(outcome["submitted"], 0)
        reasons = {row["id"]: row["reason"] for row in outcome["results"]}
        self.assertEqual(reasons, {
            "q": "queued", "t": "in_flight", "p": "in_flight", "d": "transferred",
            "x": "not_allowed", "l": "no_links", "gone": "not_found",
        })
        for row in outcome["results"]:
            self.assertTrue(row["message"])
        # 拒绝的条目一个字段都不动。
        self.assertEqual(self.rows()["d"]["status"], "transferred")
        self.assertEqual(self.monitor._transfer_queue.qsize(), 0)

    def test_no_subscribe_is_explicit_and_override_fixes_it(self):
        self.seed([_row("s", "skipped", matched_subscribe_id=0)])
        outcome = self.monitor.manual_transfer_items(["s"])
        self.assertEqual(outcome["results"][0]["reason"], "no_subscribe")
        self.assertIn("请选择要转存到的订阅", outcome["results"][0]["message"])
        self.assertEqual(self.rows()["s"]["status"], "skipped")
        outcome = self.monitor.manual_transfer_items(["s"], subscribe_id=77)
        self.assertEqual(outcome["submitted"], 1)
        self.assertEqual(self.rows()["s"]["manual_subscribe_id"], 77)

    def test_double_click_is_rejected_as_queued(self):
        self.seed([_row("s", "skipped")])
        first = self.monitor.manual_transfer_items(["s"])
        second = self.monitor.manual_transfer_items(["s"])
        self.assertEqual(first["submitted"], 1)
        self.assertEqual(second["submitted"], 0)
        self.assertEqual(second["results"][0]["reason"], "queued")
        self.assertEqual(self.monitor._transfer_queue.qsize(), 1)

    def test_concurrent_requests_submit_once(self):
        self.seed([_row("s", "skipped")])
        barrier = threading.Barrier(4)
        outcomes = []

        def _hit():
            barrier.wait()
            outcomes.append(self.monitor.manual_transfer_items(["s"]))

        threads = [threading.Thread(target=_hit) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(out["submitted"] for out in outcomes), 1)
        self.assertEqual(self.monitor._transfer_queue.qsize(), 1)

    def test_queue_full_restores_previous_state(self):
        self.seed([_row("s", "skipped", skip_reason="本轮无新集", auto_transfer_error="未转存：本轮无新集")])
        original = self.monitor._enqueue_item
        self.monitor._enqueue_item = lambda item: False
        try:
            outcome = self.monitor.manual_transfer_items(["s"])
        finally:
            self.monitor._enqueue_item = original
        self.assertEqual(outcome["results"][0]["reason"], "queue_full")
        row = self.rows()["s"]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["auto_transfer_error"], "未转存：本轮无新集")

    def test_worker_started_only_with_handlers(self):
        self.seed([_row("s", "skipped"), _row("t", "skipped")])
        self.monitor.manual_transfer_items(["s"])
        self.assertEqual(self.worker_starts, 0)
        self.monitor._auto_transfer_handler = lambda item, sid: {"success": True}
        self.monitor.manual_transfer_items(["t"])
        self.assertEqual(self.worker_starts, 1)


class ManualTransferUsesWorkerPathTest(_MonitorCase):
    """入队后走 _process_transfer_items → _transfer_group → _apply_group_outcome。"""

    def setUp(self):
        super().setUp()
        self.calls = []

    def _single(self, result):
        def handler(item, subscribe_id):
            self.calls.append(("single", item["id"], subscribe_id))
            return result
        self.monitor._auto_transfer_handler = handler

    def test_transferred_outcome_written_back(self):
        self.seed([_row("s", "skipped", skip_reason="本轮无新集")])
        self._single({"success": True, "data": {"transferred": 2}})
        self.monitor.manual_transfer_items(["s"])
        self.assertEqual(self.drain(), ["s"])
        self.assertEqual(self.calls, [("single", "s", 420)])
        row = self.rows()["s"]
        self.assertEqual(row["status"], "transferred")
        self.assertEqual(row["subscribe_id"], 420)

    def test_skip_reason_written_back_through_short_rules(self):
        self.seed([_row("s", "failed")])
        self._single({
            "success": True,
            "data": {"transferred": 0, "skip_reason": "缺集尚未播出，等待更新"},
        })
        self.monitor.manual_transfer_items(["s"])
        self.drain()
        row = self.rows()["s"]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "缺集尚未播出，等待更新")
        self.assertEqual(row["auto_transfer_error"], "未转存：缺集尚未播出，等待更新")

    def test_failure_written_back(self):
        self.seed([_row("s", "skipped")])
        self._single({"success": False, "message": "分享地址已失效"})
        self.monitor.manual_transfer_items(["s"])
        self.drain()
        row = self.rows()["s"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure_stage"], "subscribe")

    def test_override_subscribe_wins_over_match(self):
        self.seed([_row("s", "skipped", matched_subscribe_id=420)])
        self._single({"success": True, "data": {"transferred": 1}})
        self.monitor.manual_transfer_items(["s"], subscribe_id=99)
        self.drain()
        self.assertEqual(self.calls, [("single", "s", 99)])
        self.assertEqual(self.rows()["s"]["subscribe_id"], 99)

    def test_same_subscribe_rows_use_batch_handler(self):
        self.seed([_row("a", "skipped"), _row("b", "skipped")])
        batches = []

        def batch(rows, subscribe_id):
            batches.append(([row["id"] for row in rows], subscribe_id))
            return {"success": True, "data": {"transferred": 2}}

        self.monitor._auto_transfer_batch_handler = batch
        self.monitor.manual_transfer_items(["a", "b"])
        self.drain()
        self.assertEqual(batches, [(["a", "b"], 420)])
        self.assertEqual({row["status"] for row in self.rows().values()}, {"transferred"})


class ManualTransferRealWorkerTest(unittest.TestCase):
    """真实转存 worker 线程端到端：入队 → 线程取走 → 结果写回。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.monitor = TgChannelMonitor(FakeOwner(Path(self.tempdir.name)))

    def tearDown(self):
        try:
            self.monitor._stop_transfer_worker()
        finally:
            self.tempdir.cleanup()

    def test_real_worker_processes_manual_item(self):
        self.monitor._store.save(tg_module.ITEMS_KEY, [_row("s", "skipped")])
        done = threading.Event()

        def handler(item, subscribe_id):
            done.set()
            return {"success": True, "data": {"transferred": 1}}

        self.monitor._auto_transfer_handler = handler
        outcome = self.monitor.manual_transfer_items(["s"])
        self.assertEqual(outcome["submitted"], 1)
        self.assertTrue(done.wait(10))
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.monitor._load_items()[0]["status"] == "transferred":
                break
            time.sleep(0.05)
        self.assertEqual(self.monitor._load_items()[0]["status"], "transferred")


class RetryFailedSharesRequeueCoreTest(_MonitorCase):
    def test_retry_failed_uses_shared_core(self):
        self.seed([
            _row("a", "failed"),
            _row("b", "failed", matched_subscribe_id=0),
            _row("c", "skipped"),
        ])
        seen = []
        original = self.monitor._requeue_items

        def spy(ids, **kwargs):
            seen.append((list(ids), tuple(sorted(kwargs["allowed_statuses"]))))
            return original(ids, **kwargs)

        self.monitor._requeue_items = spy
        stats = self.monitor.retry_failed_items(limit=10)
        self.assertEqual(seen, [(["a"], ("failed",))])
        self.assertEqual(stats["submitted"], 1)
        self.assertEqual(stats["skipped_no_subscribe"], 1)
        self.assertEqual(self.rows()["c"]["status"], "skipped")

    def test_retry_failed_queue_full_keeps_failed_with_message(self):
        self.seed([_row("a", "failed", auto_transfer_error="链接已失效")])
        self.monitor._enqueue_item = lambda item: False
        stats = self.monitor.retry_failed_items(limit=10)
        self.assertEqual(stats["skipped_queue_full"], 1)
        row = self.rows()["a"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["auto_transfer_error"], "转存队列已满，稍后再试")


class ApiRegistrationStaticTest(unittest.TestCase):
    def test_transfer_endpoint_is_instance_method_and_registered(self):
        tg_source = _read(ROOT, "plugins.v2", "cloudsubscribex", "core", "api", "tg.py")
        self.assertIn("def api_vue_tg_transfer(self, payload: Dict[str, Any]) -> dict:", tg_source)
        self.assertIn("manual_transfer_items(", tg_source)
        self.assertNotIn("api_vue_start_manual_sync", tg_source)
        registration = _read(ROOT, "plugins.v2", "cloudsubscribex", "core", "api", "registration.py")
        self.assertIn('"path": "/tg/transfer"', registration)
        self.assertIn('"endpoint": self.api_vue_tg_transfer', registration)


class FrontendWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.monitor = _read(DASHBOARD, "TgMonitor.vue")
        cls.detail = _read(DASHBOARD, "TgItemDetail.vue")

    def test_can_transfer_matches_backend_statuses(self):
        self.assertIn(
            'const MANUAL_TRANSFER_STATUSES = ["new", "pending", "skipped", "failed", "ignored"]',
            self.monitor,
        )
        backend = _read(ROOT, "plugins.v2", "cloudsubscribex", "core", "tg_monitor.py")
        self.assertIn(
            'MANUAL_TRANSFER_STATUSES = frozenset({"new", "pending", "skipped", "failed", "ignored"})',
            backend,
        )
        self.assertIn("function transferBlockReason(item)", self.monitor)

    def test_skipped_group_rows_have_transfer_and_detail(self):
        group = self.monitor[self.monitor.index('class="tg-group-body"'):self.monitor.index('class="tg-radar-stream')]
        self.assertIn('@click="toggleDetail(item.id)"', group)
        self.assertIn('@click.stop="openTransfer(item)"', group)
        self.assertIn("<TgItemDetail", group)
        self.assertIn("selectGroup(group)", group)

    def test_list_rows_use_shared_detail(self):
        stream = self.monitor[self.monitor.index('class="tg-radar-stream'):]
        self.assertIn("<TgItemDetail", stream)
        self.assertIn(':loading="isBusy(item)"', stream)
        self.assertIn('import TgItemDetail from "./TgItemDetail.vue"', self.monitor)

    def test_transfer_posts_ids_and_polls(self):
        self.assertIn("plugin/${props.pluginId}/tg/transfer", self.monitor)
        self.assertIn("{ ids: wanted }", self.monitor)
        self.assertIn("startWatching(", self.monitor)
        self.assertIn("function announceOutcome(rows)", self.monitor)
        self.assertIn("transferSelected", self.monitor)
        self.assertIn("stopWatching()", self.monitor)

    def test_detail_shows_full_context(self):
        for text in ("复制链接", "打开链接", "TG 原消息", "匹配订阅", "消息时间", "原因", "频道"):
            self.assertIn(text, self.detail)
        self.assertIn("$emit('transfer', item)", self.detail)
        self.assertIn("$emit('copy', link)", self.detail)


if __name__ == "__main__":
    unittest.main()
