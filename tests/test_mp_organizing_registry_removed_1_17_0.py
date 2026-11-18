"""Phase 4 第 3 项：_MP_ORGANIZING 内存登记表已删，下载页“整理中”态
改从持久化 pending 记录推导。

覆盖：
- tracker 模块不再导出登记表函数（mark/list/clear/update/clear_by_src）；
- _organizing_task_id 与 _postprocess_task_id 同口径：订阅项取
  subscribe_id，非订阅项取 sub_key；绝不用记录里的 task_id（磁力
  项的 task_id 是离线任务哈希 info_hash）；
- _organizing_entries_from_pending 只收带 mp_organize 身份且尚未
  moved_at 的项；停滞文案取 pending 项的 mp_organize_status_text；
- _sync_handler 未就绪或 pending 存储不可读时返回空字典，不抛异常。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from test_download_organizing_phase_1_12_6 import (
        FakeOwner,
        SyncRuntimeService,
    )
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_download_organizing_phase_1_12_6 import (
        FakeOwner,
        SyncRuntimeService,
    )

from plugin_env import load_module  # noqa: E402

runtime_mod = load_module(
    "cloudsubscribex.core.services.runtime",
    "plugins.v2/cloudsubscribex/core/services/runtime.py",
)
tracker_mod = load_module(
    "cloudsubscribex.core.services.mp_organize_tracker",
    "plugins.v2/cloudsubscribex/core/services/mp_organize_tracker.py",
)
_organizing_task_id = runtime_mod._organizing_task_id

INFO = {
    "src_path": "/downloads/另一个妈妈.mkv",
    "src_storage": "local",
    "target_dir": "/媒体库/剧集",
    "target_storage": "u115cs",
}


class RegistryRemovedTests(unittest.TestCase):
    def test_tracker_module_has_no_registry_functions(self):
        for name in (
            "mark_mp_organizing",
            "clear_mp_organizing",
            "list_mp_organizing",
            "update_mp_organizing_by_src",
            "clear_mp_organizing_by_src",
        ):
            self.assertFalse(
                hasattr(tracker_mod, name), f"{name} 应已随登记表删除"
            )
        self.assertFalse(hasattr(tracker_mod, "_MP_ORGANIZING"))

    def test_tracker_poller_still_available(self):
        # 轮询器本身保留：它直接查 MP 历史/待整理登记/队列，不用登记表。
        self.assertTrue(hasattr(tracker_mod, "MpOrganizeTracker"))
        self.assertEqual(tracker_mod.MP_ORGANIZE_WAIT_TIMEOUT_SECONDS, 90 * 60)


class OrganizingTaskIdTests(unittest.TestCase):
    def test_subscribe_id_wins(self):
        self.assertEqual(
            _organizing_task_id({"subscribe_id": 5, "sub_key": "abc"}),
            "subscribe:5",
        )

    def test_sub_key_fallback(self):
        self.assertEqual(
            _organizing_task_id({"subscribe_id": 0, "sub_key": "abc"}),
            "media:abc",
        )

    def test_empty_identity_returns_empty(self):
        self.assertEqual(_organizing_task_id({"subscribe_id": 0}), "")
        self.assertEqual(_organizing_task_id({}), "")

    def test_offline_task_id_hash_is_never_used(self):
        """磁力/ed2k 项的 task_id 是离线任务哈希，不能当父任务 id。"""
        self.assertEqual(
            _organizing_task_id({
                "task_id": "ac62c099abcdef",
                "subscribe_id": 0,
                "sub_key": "",
            }),
            "",
        )


class OrganizingEntriesFromPendingTests(unittest.TestCase):
    def _service(self, pending_items, sync_handler=None):
        owner = FakeOwner([], [], pending_items)
        if sync_handler is not None:
            owner._sync_handler = sync_handler
        return SyncRuntimeService(owner=owner)

    def test_identity_pending_item_becomes_organizing(self):
        service = self._service([{
            "subscribe_id": 1, "mp_organize": dict(INFO),
        }])
        entries = service._organizing_entries_from_pending()
        self.assertEqual(list(entries), ["subscribe:1"])
        self.assertEqual(entries["subscribe:1"]["mp_organize"], INFO)
        self.assertEqual(entries["subscribe:1"]["status_text"], "")

    def test_stall_text_carried_from_pending_item(self):
        service = self._service([{
            "subscribe_id": 1, "mp_organize": dict(INFO),
            "mp_organize_status_text": "整理停滞（队头阻塞，后续任务排队中）",
        }])
        entries = service._organizing_entries_from_pending()
        self.assertEqual(
            entries["subscribe:1"]["status_text"],
            "整理停滞（队头阻塞，后续任务排队中）",
        )

    def test_no_identity_or_moved_items_skipped(self):
        service = self._service([
            {"subscribe_id": 1},  # 无身份：还在等离线下载，不是整理中
            {"subscribe_id": 2, "mp_organize": {}},  # 空身份
            {
                "subscribe_id": 3, "mp_organize": dict(INFO),
                "moved_at": 1_700_000_000.0,  # 已移入媒体库，终态已过
            },
        ])
        self.assertEqual(service._organizing_entries_from_pending(), {})

    def test_media_key_item_grouped_by_sub_key(self):
        service = self._service([{
            "sub_key": "movie:1359916", "mp_organize": dict(INFO),
        }])
        entries = service._organizing_entries_from_pending()
        self.assertEqual(list(entries), ["media:movie:1359916"])

    def test_missing_sync_handler_returns_empty(self):
        service = self._service([])
        service._sync_handler = None
        self.assertEqual(service._organizing_entries_from_pending(), {})

    def test_pending_store_error_returns_empty(self):
        class _RaisingHandler:
            def get_pending_finalize_tasks(self):
                raise RuntimeError("存储不可读")

        service = self._service([], sync_handler=_RaisingHandler())
        self.assertEqual(service._organizing_entries_from_pending(), {})


if __name__ == "__main__":
    unittest.main()
