"""Phase 4 第 7 项：finalize 终态回写频道监控 + 失败分阶段。

覆盖 TgChannelMonitor.apply_finalize_outcome 的各分支与
_apply_group_outcome / 转存异常路径的 failure_stage 标记：
- transferred 后 finalize 失败 → failed（postprocess 阶段）；
- postprocess 失败后 finalize 成功 → transferred；
- 订阅阶段失败不受 finalize 终态影响（成功失败都不动）；
- transferred 后 finalize 成功只补终态字段；
- postprocess 失败重复失败只更新原因与时间；
- share_url 不匹配 / subscribe_id 不一致 → 不写。
"""

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

URL = "https://115.com/s/abc123"
OTHER_URL = "https://pan.quark.cn/s/xyz789"


class FakeOwner:
    """最小插件替身：提供数据目录和配置持久化。"""

    def __init__(self, root):
        self.root = root
        self.persisted = {}

    def get_data_path(self):
        return self.root

    def _persist_config_values(self, **updates):
        self.persisted.update(updates)


def _item(item_id="item-1", **overrides):
    item = {
        "id": item_id,
        "title": "测试剧 S01E01",
        "links": [URL],
        "subscribe_id": 101,
        "status": "transferred",
        "match_status": "transferred",
    }
    item.update(overrides)
    return item


class ApplyFinalizeOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))
        self.monitor = TgChannelMonitor(self.owner)

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self, rows):
        self.monitor._store.save(tg_module.ITEMS_KEY, rows)

    def _load(self):
        return self.monitor._store.load(tg_module.ITEMS_KEY, [])

    def test_transferred_then_finalize_failed(self):
        """已转存 + 后处理失败 → failed，阶段为 postprocess，原因带前缀。"""
        self._seed([_item()])
        touched = self.monitor.apply_finalize_outcome(
            URL, 101, success=False, reason="网盘文件定位失败",
        )
        self.assertEqual(touched, 1)
        item = self._load()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["failure_stage"], "postprocess")
        self.assertEqual(
            item["auto_transfer_error"], "后处理失败：网盘文件定位失败"
        )
        self.assertEqual(item["finalize_status"], "failed")
        self.assertIn("finalize_failed_at", item)

    def test_postprocess_failed_then_finalize_succeeded(self):
        """postprocess 失败 + 后来入库成功 → 翻回 transferred，阶段清掉。"""
        self._seed([_item(
            status="failed", match_status="failed",
            failure_kind="permanent", failure_stage="postprocess",
            auto_transfer_error="后处理失败：网盘文件定位失败",
        )])
        touched = self.monitor.apply_finalize_outcome(URL, 101, success=True)
        self.assertEqual(touched, 1)
        item = self._load()[0]
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(item["failure_stage"], "")
        self.assertEqual(item["auto_transfer_error"], "")
        self.assertEqual(item["finalize_status"], "landed")
        self.assertIn("finalize_completed_at", item)

    def test_subscribe_failed_untouched_by_finalize(self):
        """订阅阶段失败：finalize 成功失败都不动（那不是这条后处理的终态）。"""
        self._seed([_item(
            status="failed", match_status="failed",
            failure_kind="permanent", failure_stage="subscribe",
            auto_transfer_error="订阅搜索完成，但有 1 个订阅处理失败",
        )])
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 101, success=True), 0
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome(
                URL, 101, success=False, reason="网盘文件定位失败",
            ),
            0,
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["failure_stage"], "subscribe")
        self.assertEqual(
            item["auto_transfer_error"], "订阅搜索完成，但有 1 个订阅处理失败"
        )

    def test_transferred_then_finalize_succeeded_annotates_only(self):
        """已转存 + 入库成功：只补终态字段，状态不动。"""
        self._seed([_item()])
        touched = self.monitor.apply_finalize_outcome(URL, 101, success=True)
        self.assertEqual(touched, 1)
        item = self._load()[0]
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(item["finalize_status"], "landed")
        self.assertIn("finalize_completed_at", item)

    def test_postprocess_failed_then_failed_again_updates_reason(self):
        """postprocess 失败 + 再失败：更新原因与时间，仍是失败。"""
        self._seed([_item(
            status="failed", match_status="failed",
            failure_kind="permanent", failure_stage="postprocess",
            auto_transfer_error="后处理失败：旧原因",
            finalize_failed_at=1000,
        )])
        touched = self.monitor.apply_finalize_outcome(
            URL, 101, success=False, reason="新原因",
        )
        self.assertEqual(touched, 1)
        item = self._load()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["failure_stage"], "postprocess")
        self.assertEqual(item["auto_transfer_error"], "后处理失败：新原因")
        self.assertNotEqual(item["finalize_failed_at"], 1000)

    def test_unknown_url_and_subscribe_mismatch_untouched(self):
        """URL 对不上 / subscribe_id 不一致 → 不写。"""
        self._seed([_item()])
        self.assertEqual(
            self.monitor.apply_finalize_outcome(OTHER_URL, 101, success=False),
            0,
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 999, success=False),
            0,
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome("", 101, success=False),
            0,
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "transferred")
        self.assertNotIn("finalize_status", item)

    def test_skipped_untouched_by_finalize(self):
        """skipped 条目不受 finalize 终态影响。"""
        self._seed([_item(status="skipped", match_status="skipped")])
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 101, success=True), 0
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 101, success=False),
            0,
        )
        self.assertEqual(self._load()[0]["status"], "skipped")


class FailureStageTests(unittest.TestCase):
    """失败分阶段：订阅处理阶段失败标 subscribe，成功/跳过清掉。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))
        self.monitor = TgChannelMonitor(self.owner)

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self, rows):
        self.monitor._store.save(tg_module.ITEMS_KEY, rows)

    def _load(self):
        return self.monitor._store.load(tg_module.ITEMS_KEY, [])

    def test_group_outcome_failed_marks_subscribe_stage(self):
        self._seed([_item(status="queued", match_status="queued")])
        self.monitor._apply_group_outcome(
            ["item-1"], 101, "failed", "订阅搜索完成，但有 1 个订阅处理失败",
            [{"title": "测试剧"}],
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["failure_stage"], "subscribe")

    def test_group_outcome_transferred_clears_stage(self):
        self._seed([_item(
            status="failed", failure_stage="subscribe",
            auto_transfer_error="旧原因",
        )])
        self.monitor._apply_group_outcome(
            ["item-1"], 101, "transferred", "", [{"title": "测试剧"}],
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(item["failure_stage"], "")

    def test_group_outcome_skipped_clears_stage(self):
        self._seed([_item(
            status="failed", failure_stage="subscribe",
            auto_transfer_error="旧原因",
        )])
        self.monitor._apply_group_outcome(
            ["item-1"], 101, "skipped", "已入库",
            [{"title": "测试剧", "season": 1, "episode": 1}],
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["failure_stage"], "")


if __name__ == "__main__":
    unittest.main()
