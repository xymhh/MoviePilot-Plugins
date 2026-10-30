"""TG 转存队列优先级：订阅还缺集的先补集，洗版排后面。

线上背景：频道监控一次扫描几十条命中，全按入队顺序串行跑，
洗版任务（体积大、耗时、还要下 4K 源）经常挡在补集任务前面。
"""

import os
import queue
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


class FakeOwner:
    def __init__(self, root):
        self.root = root

    def get_data_path(self):
        return self.root

    def _persist_config_values(self, **updates):
        pass


class QueuePriorityTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))
        self.monitor = TgChannelMonitor(self.owner)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_missing_episodes_pop_before_upgrade(self):
        """后入队的缺集条目先出队；同优先级仍按入队顺序。"""
        self.monitor._enqueue_item({"id": "up-1", "queue_priority": 1})
        self.monitor._enqueue_item({"id": "up-2", "queue_priority": 1})
        self.monitor._enqueue_item({"id": "miss-1", "queue_priority": 0})
        self.monitor._enqueue_item({"id": "miss-2", "queue_priority": 0})
        order = []
        while not self.monitor._transfer_queue.empty():
            entry = self.monitor._transfer_queue.get_nowait()
            order.append(self.monitor._queue_entry_id(entry))
        self.assertEqual(order, ["miss-1", "miss-2", "up-1", "up-2"])

    def test_legacy_item_without_priority_goes_last(self):
        self.monitor._enqueue_item({"id": "legacy"})
        self.monitor._enqueue_item({"id": "miss", "queue_priority": 0})
        entry = self.monitor._transfer_queue.get_nowait()
        self.assertEqual(entry[0], tg_module.QUEUE_PRIORITY_MISSING)
        self.assertEqual(self.monitor._queue_entry_id(entry), "miss")

    def test_match_sets_priority_by_lack(self):
        self.monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            auto_transfer_handler=lambda item, subscribe_id: {},
            subscribe_provider=lambda: [
                {"id": 7, "name": "尚公主", "season": 1, "type": "电视剧",
                 "lack": 4},
                {"id": 9, "name": "玉簟秋", "season": 1, "type": "电视剧",
                 "lack": 0},
            ],
        )
        rule = {"channel": "@a"}
        missing = {
            "id": "m1", "links": ["https://pan.quark.cn/s/1"],
            "title": "尚公主 第5集", "status": "new",
        }
        upgrade = {
            "id": "u1", "links": ["https://pan.quark.cn/s/2"],
            "title": "玉簟秋 第3集", "status": "new",
        }
        stats, to_queue = self.monitor._match_and_enqueue(
            [(missing, rule), (upgrade, rule)]
        )
        self.assertEqual(stats["queued"], 2)
        by_id = {item["id"]: item for item in to_queue}
        self.assertEqual(
            by_id["m1"]["queue_priority"], tg_module.QUEUE_PRIORITY_MISSING
        )
        self.assertIn("缺 4 集", by_id["m1"]["queue_priority_reason"])
        self.assertEqual(
            by_id["u1"]["queue_priority"], tg_module.QUEUE_PRIORITY_UPGRADE
        )
        self.assertIn("仅洗版", by_id["u1"]["queue_priority_reason"])

    def test_same_subscribe_season_merges_into_one_unit(self):
        """同一订阅同一季在一批扫描里只入队一个单元，不同集的链接合并进去。"""
        self.monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            auto_transfer_handler=lambda item, subscribe_id: {},
            subscribe_provider=lambda: [
                {"id": 7, "name": "尚公主", "season": 1, "type": "电视剧",
                 "lack_episode": 0},
            ],
        )
        rule = {"channel": "@a"}
        low = {"id": "low", "links": ["https://pan.quark.cn/s/1"],
               "title": "尚公主 第5集 720p", "status": "new"}
        high = {"id": "high", "links": ["https://pan.quark.cn/s/2"],
                "title": "尚公主 第6集 2160p", "status": "new"}
        stats, to_queue = self.monitor._match_and_enqueue(
            [(low, rule), (high, rule)]
        )
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["merged"], 1)
        self.assertEqual(len(to_queue), 1)
        unit = to_queue[0]
        self.assertEqual(
            unit["links"],
            ["https://pan.quark.cn/s/1", "https://pan.quark.cn/s/2"],
        )
        self.assertEqual(unit["target_episodes"], [5, 6])
        self.assertEqual(high["match_status"], "merged")

    def test_candidate_merged_into_queued_season_unit(self):
        """跨批次合并：同订阅同季已有条目排队时，新候选并进那条而不是另排。"""
        self.monitor._store.save(
            tg_module.ITEMS_KEY,
            [{
                "id": "queued-1", "status": "queued",
                "matched_subscribe_id": 7,
                "matched_dedupe_key": [7, "season", 1, 0],
                "links": ["https://pan.quark.cn/s/old"],
                "target_episodes": [3],
                "queue_priority": 1,
            }],
        )
        self.monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            auto_transfer_handler=lambda item, subscribe_id: {},
            subscribe_provider=lambda: [
                {"id": 7, "name": "尚公主", "season": 1, "type": "电视剧",
                 "lack_episode": 2},
            ],
        )
        item = {"id": "dup", "links": ["https://pan.quark.cn/s/3"],
                "title": "尚公主 第5集 2160p", "status": "new"}
        stats, to_queue = self.monitor._match_and_enqueue(
            [(item, {"channel": "@a"})]
        )
        self.assertEqual(to_queue, [])
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["merged"], 1)
        self.assertEqual(item["match_status"], "merged")
        self.assertIn("queued-1", item["match_reason"])
        stored = self.monitor._store.load(tg_module.ITEMS_KEY, [])
        unit = stored[0]
        self.assertEqual(
            unit["links"],
            ["https://pan.quark.cn/s/old", "https://pan.quark.cn/s/3"],
        )
        self.assertEqual(unit["target_episodes"], [3, 5])
        # 合并后优先级取更急的一档：订阅缺集 → 先补集
        self.assertEqual(unit["queue_priority"], tg_module.QUEUE_PRIORITY_MISSING)

    def test_recovery_keeps_persisted_priority(self):
        persisted = [
            {"id": "old-upgrade", "status": "queued",
             "matched_subscribe_id": 9, "queue_priority": 1},
            {"id": "old-missing", "status": "queued",
             "matched_subscribe_id": 7, "queue_priority": 0},
        ]
        self.monitor._load_items = lambda: [dict(row) for row in persisted]
        self.monitor._recover_queued_items()
        first = self.monitor._queue_entry_id(
            self.monitor._transfer_queue.get_nowait()
        )
        self.assertEqual(first, "old-missing")

    def test_plain_string_entries_still_work(self):
        """旧测试与老代码可能塞纯字符串，worker 不能崩。"""
        self.monitor._transfer_queue = queue.Queue()
        self.monitor._transfer_queue.put("plain-id")
        entry = self.monitor._transfer_queue.get_nowait()
        self.assertEqual(self.monitor._queue_entry_id(entry), "plain-id")
