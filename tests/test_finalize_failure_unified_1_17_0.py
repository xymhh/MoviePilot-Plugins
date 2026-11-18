"""Phase 4 第 2+5 项：fail_count 两路合一、阈值与超时只在一处定义。

覆盖：
- 阈值与超时只在 service.py（SyncHandler）定义：_FINALIZE_MAX_FAILURES=5
  （离线任务）、_FINALIZE_MAX_FAILURES_LOCAL=2（其余）、
  _FINALIZE_LOCATE_TIMEOUT_SECONDS=120.0；postprocess.py 不再有
  _FINALIZE_MAX_FAILURES 类常量；
- _finalize_failure 按任务类型取阈值：普通任务 2 次判死，离线任务 5 次；
- 定位超时（now-ready_at >= timeout）返回 True 但不写 finalize_dead
  （保留原来的超时 abort 语义：只约束插件自己的定位/移动步骤）；
- max_failures 显式传入时覆盖默认阈值；
- 内联定位失败路径调 _finalize_failure（不是自增）：无身份的项定位
  失败 2 次后 abort，且 abort 原因里是“已尝试 2 次”。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from test_finalize_mp_wait_1_12_6 import (
        DummyMpWaitService,
        PostprocessService,
        SyncHandler,
        _ctx,
        _item,
        _stub,
        set_media_type,
    )
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_finalize_mp_wait_1_12_6 import (
        DummyMpWaitService,
        PostprocessService,
        SyncHandler,
        _ctx,
        _item,
        _stub,
        set_media_type,
    )

NOW = 1_700_000_000.0


class ThresholdSingleSourceTests(unittest.TestCase):
    """第 5 项：常量一处定义。"""

    def test_thresholds_defined_once_on_sync_handler(self):
        self.assertEqual(SyncHandler._FINALIZE_MAX_FAILURES, 5)
        self.assertEqual(SyncHandler._FINALIZE_MAX_FAILURES_LOCAL, 2)
        self.assertEqual(SyncHandler._FINALIZE_LOCATE_TIMEOUT_SECONDS, 120.0)

    def test_postprocess_has_no_threshold_class_attr(self):
        # postprocess.py 的 _FINALIZE_MAX_FAILURES 类常量已删，阈值
        # 只能从 SyncHandler（经委托读宿主属性）取，不得各自为政。
        self.assertNotIn("_FINALIZE_MAX_FAILURES", PostprocessService.__dict__)
        self.assertNotIn(
            "_FINALIZE_MAX_FAILURES_LOCAL", PostprocessService.__dict__
        )
        self.assertNotIn(
            "_FINALIZE_LOCATE_TIMEOUT_SECONDS", PostprocessService.__dict__
        )


class FinalizeFailureUnifiedTests(unittest.TestCase):
    """第 2 项：_finalize_failure 是唯一的失败计数入口。"""

    def setUp(self):
        set_media_type()
        self.service = DummyMpWaitService()
        # Dummy 沿用旧值 3；这里改读 SyncHandler 的一处定义（5/2/120），
        # 测的才是线上真实阈值。
        self.service._FINALIZE_MAX_FAILURES = SyncHandler._FINALIZE_MAX_FAILURES
        self.service._FINALIZE_MAX_FAILURES_LOCAL = (
            SyncHandler._FINALIZE_MAX_FAILURES_LOCAL
        )
        self.service._FINALIZE_LOCATE_TIMEOUT_SECONDS = (
            SyncHandler._FINALIZE_LOCATE_TIMEOUT_SECONDS
        )

    def test_share_task_dies_after_two_failures(self):
        item = _item(NOW - 300)
        self.assertFalse(self.service._finalize_failure(item, "key1"))
        self.assertEqual(item["fail_count"], 1)
        self.assertNotIn("finalize_dead", item)
        self.assertTrue(self.service._finalize_failure(item, "key1"))
        self.assertEqual(item["fail_count"], 2)
        self.assertTrue(item["finalize_dead"])
        self.assertEqual(item["next_check_at"], 0.0)

    def test_offline_task_dies_after_five_failures(self):
        item = _item(NOW - 300, task_type="magnet")
        for _ in range(4):
            self.assertFalse(self.service._finalize_failure(item, "key1"))
        self.assertEqual(item["fail_count"], 4)
        self.assertNotIn("finalize_dead", item)
        self.assertTrue(self.service._finalize_failure(item, "key1"))
        self.assertEqual(item["fail_count"], 5)
        self.assertTrue(item["finalize_dead"])

    def test_locate_timeout_returns_true_without_dead_mark(self):
        """定位超时判死但不写 finalize_dead：超时只约束插件自己的定位
        步骤，MP 整理等待不走这里（见 _poll_mp_organize_state）。"""
        item = _item(NOW - 300)
        self.assertTrue(
            self.service._finalize_failure(
                item, "key1", now=NOW, ready_at=NOW - 300,
                timeout_seconds=120.0,
            )
        )
        self.assertEqual(item["fail_count"], 1)
        self.assertNotIn("finalize_dead", item)

    def test_max_failures_overrides_default(self):
        item = _item(NOW - 300)
        self.assertTrue(
            self.service._finalize_failure(item, "key1", max_failures=1)
        )
        self.assertTrue(item["finalize_dead"])

    def test_inline_locate_failure_routes_through_finalize_failure(self):
        """无身份项的内联定位失败走 _finalize_failure：第二次失败 abort，
        原因里是“已尝试 2 次”，且 fail_count 由统一入口递增。"""
        calls = []
        real = self.service._finalize_failure

        def _recording(item, key, **kwargs):
            calls.append(kwargs)
            return real(item, key, **kwargs)

        _stub(self.service, "_finalize_failure", _recording)
        aborts = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx: aborts.append(reason),
        )
        self.service._snapshot_map = {}
        # download_completed_at 在 120 秒内：超时不触发，这次 abort
        # 纯粹是第 2 次失败达到本地阈值。
        item = _item(NOW - 300, download_completed_at=NOW - 60)
        item.pop("mp_organize")
        item["fail_count"] = 1
        pending = {"key1": item}
        ctx = _ctx(pending)
        self.service._organize_and_finalize_single_item(item, "key1", ctx)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout_seconds"], 120.0)
        self.assertEqual(len(aborts), 1)
        self.assertIn("已尝试 2 次", aborts[0])
        self.assertEqual(item["fail_count"], 2)


if __name__ == "__main__":
    unittest.main()
