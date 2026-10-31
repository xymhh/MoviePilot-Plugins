"""频道监控「中断≠失败」与失败重排单测（1.7.114）。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from plugin_env import ensure_package, install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
search = ensure_package("cloudsubscribex.search")
search.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "search")]

tg_module = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)
TgChannelMonitor = tg_module.TgChannelMonitor

INTERRUPTED = (
    "提交转存任务失败：SyncExecutionService.sync_subscribes() got an "
    "unexpected keyword argument 'target_episodes'"
)


class FakeOwner:
    def __init__(self, root):
        self.root = root
        self.persisted = {}

    def get_data_path(self):
        return self.root

    def _persist_config_values(self, **updates):
        self.persisted.update(updates)


class InterruptClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))
        self.monitor = TgChannelMonitor(self.owner)

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self, **extra):
        item = {
            "id": "chan:1",
            "title": "小城良方 S01E05",
            "links": ["https://pan.quark.cn/s/abcdef123456"],
            "status": "transferring",
            "matched_subscribe_id": 201,
        }
        item.update(extra)
        self.monitor._save_items([item])
        return item

    def test_interrupt_returns_to_queue(self):
        item = self._seed()
        self.monitor._apply_group_outcome(
            ["chan:1"], 201, "failed", INTERRUPTED, [item],
        )
        row = self.monitor._load_items()[0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["match_status"], "interrupted")
        self.assertEqual(row["failure_kind"], "interrupted")
        self.assertEqual(row["retry_count"], 1)
        self.assertIn("重启后自动续跑", row["auto_transfer_error"])

    def test_stopped_search_counts_as_interrupt(self):
        item = self._seed()
        self.monitor._apply_group_outcome(
            ["chan:1"], 201, "failed", "订阅搜索已停止", [item],
        )
        row = self.monitor._load_items()[0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["failure_kind"], "interrupted")

    def test_permanent_error_stays_failed(self):
        item = self._seed()
        self.monitor._apply_group_outcome(
            ["chan:1"], 201, "failed", "链接已失效", [item],
        )
        row = self.monitor._load_items()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure_kind"], "permanent")

    def test_interrupt_retry_limit(self):
        item = self._seed(retry_count=3)
        self.monitor._apply_group_outcome(
            ["chan:1"], 201, "failed", INTERRUPTED, [item],
        )
        row = self.monitor._load_items()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["retry_count"], 4)
        self.assertIn("连续 4 次被中断", row["auto_transfer_error"])

    def test_success_resets_retry_count(self):
        item = self._seed(status="transferring", retry_count=2,
                          failure_kind="interrupted")
        self.monitor._apply_group_outcome(
            ["chan:1"], 201, "transferred", "", [item],
        )
        row = self.monitor._load_items()[0]
        self.assertEqual(row["status"], "transferred")
        self.assertEqual(row["retry_count"], 0)
        self.assertEqual(row["failure_kind"], "")


class RetryFailedItemsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))
        self.monitor = TgChannelMonitor(self.owner)

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self, rows):
        self.monitor._save_items(rows)

    def test_only_items_with_subscribe_are_requeued(self):
        self._seed([
            {
                "id": "a", "status": "failed", "matched_subscribe_id": 201,
                "auto_transfer_error": INTERRUPTED, "created_at": 3,
                "links": ["https://pan.quark.cn/s/a"],
            },
            {
                "id": "b", "status": "failed", "matched_subscribe_id": 0,
                "auto_transfer_error": "缺少目标订阅", "created_at": 2,
                "links": ["https://pan.quark.cn/s/b"],
            },
            {
                "id": "c", "status": "failed", "subscribe_id": 158,
                "auto_transfer_error": "链接已失效", "created_at": 1,
                "links": ["https://pan.quark.cn/s/c"],
            },
        ])
        stats = self.monitor.retry_failed_items(limit=10)
        self.assertEqual(stats["submitted"], 2)
        self.assertEqual(stats["skipped_no_subscribe"], 1)
        rows = {row["id"]: row for row in self.monitor._load_items()}
        self.assertEqual(rows["a"]["status"], "queued")
        self.assertEqual(rows["c"]["status"], "queued")
        self.assertEqual(rows["b"]["status"], "failed")
        self.assertEqual(rows["a"]["retry_count"], 0)
        self.assertEqual(rows["a"]["auto_transfer_error"], "")

    def test_limit_caps_batch(self):
        self._seed([
            {
                "id": f"x{index}", "status": "failed", "matched_subscribe_id": 201,
                "created_at": index, "links": ["https://pan.quark.cn/s/x"],
            }
            for index in range(3)
        ])
        stats = self.monitor.retry_failed_items(limit=2)
        self.assertEqual(stats["submitted"], 2)
        statuses = sorted(row["status"] for row in self.monitor._load_items())
        self.assertEqual(statuses, ["failed", "queued", "queued"])

    def test_status_reports_failed_and_restart_flag(self):
        self._seed([
            {"id": "z", "status": "failed", "auto_transfer_error": "链接已失效"},
        ])
        status = self.monitor.status()
        self.assertEqual(status["failed_count"], 1)
        # 同进程里别的用例可能已经触发过签名裁剪，这里只校验字段类型。
        self.assertIsInstance(status["restart_required"], bool)


if __name__ == "__main__":
    unittest.main()
