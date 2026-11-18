"""1.12.17：每集入库阶段耗时埋点（Phase 0-10，速度面验收的观测手段）。

覆盖：
- 提交成功终态记一行：结果=成功，总/下载/整理等待三段 + 累计快照；
- abort 终态记一行失败（重复 abort 不再由终态写入返回值守门）；
- 辅助函数容忍缺失时间戳，不抛错。
"""
import sys
import unittest

from plugin_env import load_module

from test_finalize_mp_wait_1_12_6 import (
    NOW,
    DummyMpWaitService,
    _ctx,
    _item,
    _postprocess,
    _stub,
)

telemetry_module = sys.modules.get("cloudsubscribex.core.telemetry")
if telemetry_module is None:
    telemetry_module = load_module(
        "cloudsubscribex.core.telemetry",
        "plugins.v2/cloudsubscribex/core/telemetry.py",
    )
api_meter = telemetry_module.api_meter


def _telemetry_lines(before):
    # 用 postprocess 模块绑定的 logger 读：全量套件里 app.log 会被别的
    # 用例重注册，sys.modules 里的 logger 未必是 postprocess 用的那只。
    logger = _postprocess.logger
    return [
        str(call.args[0])
        for call in logger.info.call_args_list[before:]
        if call.args and "集耗时" in str(call.args[0])
    ]


class EpisodeTelemetryTests(unittest.TestCase):
    def setUp(self):
        api_meter.reset()
        self.service = DummyMpWaitService()
        self.logger = _postprocess.logger

    def _stub_commit_path(self, resolve_result=True):
        _stub(self.service, "_update_postprocess_progress", lambda *a, **k: None)
        _stub(self.service, "_write_terminal_state", lambda *a, **k: resolve_result)
        _stub(self.service, "_queue_subscription_completion", lambda *a, **k: None)
        _stub(self.service, "_notify_pending_file_finalized", lambda *a, **k: None)
        _stub(self.service, "_postprocess_task_id", lambda item: None)

    def test_commit_logs_success_once_with_stages(self):
        self._stub_commit_path()
        api_meter.record("oss2:part", bytes_=10 * 1024 * 1024)
        item = _item(
            NOW - 300, season=1, episode=234, size=1572864000,
        )
        ctx = _ctx({"key1": item})
        before = len(self.logger.info.call_args_list)
        self.service._commit_single_item(item, "key1", None, None, {}, ctx)
        lines = _telemetry_lines(before)
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("S01E234", line)
        self.assertIn("1500.00MB", line)
        self.assertIn("结果=成功", line)
        self.assertIn("总=1000.0s", line)
        self.assertIn("下载=0.0s", line)
        self.assertIn("整理等待=300.0s", line)
        self.assertIn("oss2:part=1次/10.0MB", line)

    def test_abort_logs_failure_once(self):
        """abort 终态记一行失败。

        v1.13.3 §4.4：结算不再因历史记录缺失而提前返回，重复 abort 的
        结构保证是 pending 项只处理一次——处理完即从 ctx.pending 弹出，
        不再由终态写入的返回值守门；旧的「第二次 abort 静默」用例随
        _resolve_history_record 一起删除。
        """
        _stub(self.service, "_update_postprocess_progress", lambda *a, **k: None)
        _stub(self.service, "_write_terminal_state", lambda *a, **k: True)
        _stub(self.service, "_notify_finalize_dead", lambda *a, **k: None)
        _stub(self.service, "_postprocess_task_id", lambda item: None)
        item = _item(NOW - 300)
        ctx = _ctx({"key1": item})
        before = len(self.logger.info.call_args_list)
        self.service._abort_single_item(item, "key1", "MP 整理未受理", ctx)
        lines = _telemetry_lines(before)
        self.assertEqual(len(lines), 1)
        self.assertIn("结果=失败", lines[0])
        self.assertIn("整理等待=300.0s", lines[0])

    def test_helper_tolerates_missing_timestamps(self):
        before = len(self.logger.info.call_args_list)
        self.service._log_episode_telemetry({}, "key1", "成功", NOW)
        lines = _telemetry_lines(before)
        self.assertEqual(len(lines), 1)
        self.assertIn("key1", lines[0])
        self.assertIn("总=0.0s", lines[0])
        self.assertNotIn("下载=", lines[0])
        self.assertNotIn("整理等待=", lines[0])
