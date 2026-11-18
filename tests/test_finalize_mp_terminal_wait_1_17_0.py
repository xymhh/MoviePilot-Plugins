"""Phase 4 第 6 项：finalize 必须等 MP 终态再判失败。

覆盖：
- 同盘批量触发 MP 整理成功时，MP 整理身份写进处理项（与跨盘路一致），
  随后可进 pending 记录、供 finalize 轮询；
- already_in_library（根本没触发整理）与触发失败时不写身份——写了
  反而会被判“MP 整理未受理”；
- 带身份的项在 MP 排队中时不 abort、不计 fail（2026-11-18 当天 7 条
  “网盘文件后处理失败”通知里 6 条是这种误报：定位 2 次就 abort，
  而 MP 整理随后自己完成）。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from test_finalize_mp_wait_1_12_6 import (
        DummyMpWaitService,
        FakeTracker,
        _ctx,
        _item,
        _stub,
        set_media_type,
    )
    from test_mp_organize_1_11_0 import (
        _install_stubs,
        _owner,
    )
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_finalize_mp_wait_1_12_6 import (
        DummyMpWaitService,
        FakeTracker,
        _ctx,
        _item,
        _stub,
        set_media_type,
    )
    from test_mp_organize_1_11_0 import (
        _install_stubs,
        _owner,
    )

from plugin_env import load_module  # noqa: E402

mp_organize = load_module(
    "cloudsubscribex.handlers.sync.mp_organize",
    "plugins.v2/cloudsubscribex/handlers/sync/mp_organize.py",
)
MpOrganizeMixin = mp_organize.MpOrganizeMixin

NOW = 1_700_000_000.0
INFO = {
    "src_path": "/downloads/凡人修仙传 - S01E200 - 第200集.mkv",
    "src_storage": "local",
    "target_dir": "/媒体库/剧集",
    "target_storage": "u115cs",
    "target_name": "剧集",
    "file_name": "凡人修仙传 - S01E200 - 第200集.mkv",
    "triggered_at": NOW,
}


class BatchStampsIdentityTests(unittest.TestCase):
    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        _install_stubs()
        self.mediainfo = MagicMock()
        self.mediainfo.type = MediaType.TV
        self.mediainfo.category = "国漫"
        self.mixin = MpOrganizeMixin(_owner())

    def _batch(self, trigger_result, success_ids=("ep1", "ep2")):
        object.__setattr__(
            self.mixin, "trigger_mp_organize",
            MagicMock(return_value=trigger_result),
        )
        items = [
            {"file": {"id": "ep1"}, "target_name": "a - S01E01.mkv"},
            {"file": {"id": "ep2"}, "target_name": "b - S01E02.mkv"},
            {"file": {"id": "ep3"}, "target_name": "c - S01E03.mkv"},
        ]
        accepted = self.mixin._trigger_mp_organize_for_batch(
            items, set(success_ids),
            mediainfo=self.mediainfo, season=1,
        )
        return items, accepted

    def test_trigger_success_stamps_identity_on_items(self):
        items, accepted = self._batch((True, dict(INFO)))
        self.assertEqual(accepted, {"ep1", "ep2"})
        for item in items[:2]:
            self.assertEqual(item.get("mp_organize"), INFO)
        # 不在成功集里的项不触发、不写身份。
        self.assertNotIn("mp_organize", items[2])

    def test_already_in_library_accepts_without_identity(self):
        """已在库：算已处理（不用再触发），但不写身份——这次根本没触发
        MP 整理，finalize 没有可轮询的对象。"""
        items, accepted = self._batch(
            (False, {"reason": "already_in_library"})
        )
        self.assertEqual(accepted, {"ep1", "ep2"})
        for item in items[:2]:
            self.assertNotIn("mp_organize", item)

    def test_trigger_failure_stamps_nothing(self):
        items, accepted = self._batch(
            (False, {"reason": "not_accepted", "message": "MP 整理未接受"})
        )
        self.assertEqual(accepted, set())
        for item in items:
            self.assertNotIn("mp_organize", item)


class BatchIdentityWaitsForMpTerminalTests(unittest.TestCase):
    """带身份的项：MP 排队中不 abort、不计 fail，等 MP 终态。"""

    def setUp(self):
        set_media_type()
        self.service = DummyMpWaitService()
        self.abort_calls = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason}
            ),
        )

    def test_queued_identity_item_not_aborted_past_120s(self):
        """2026-11-18 误报场景：下载完成 5 分钟后 MP 仍在排队——
        定位 120 秒规则不能约束 MP 整理等待，只复查、不 abort。"""
        tracker = FakeTracker(("queued", "MP 整理排队中", None))
        _stub(self.service, "_mp_organize_tracker", lambda: tracker)
        item = _item(NOW - 300)
        pending = {"key1": item}
        ctx = _ctx(pending, now=NOW)
        self.service._organize_and_finalize_single_item(item, "key1", ctx)
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(item.get("fail_count") or 0), 0)
        self.assertIn("key1", pending)
        self.assertEqual(len(tracker.polls), 1)
        self.assertEqual(
            tracker.polls[0]["src_path"], item["mp_organize"]["src_path"]
        )


if __name__ == "__main__":
    unittest.main()
