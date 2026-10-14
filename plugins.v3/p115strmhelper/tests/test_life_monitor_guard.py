"""
生活事件监控停摆看门狗测试模块（任务书 2.8.78 G）

覆盖 6 类用例：
1. 心跳超阈值 ⇒ 判停摆、触发重启、栈被写出；
2. 整理等待期间心跳持续更新 ⇒ 不误判；
3. 看门狗重启后起始时间 = 持久化游标 - grace（含 grace=0、游标缺失）；
4. 单轮全量抽干不截断（截断会永久丢事件）、逐条心跳、游标已推进；
5. 整理等待超过绝对上限 ⇒ 返回并记 WARNING；
6. 配置边界：阈值 0（关闭）、最小值钳制、负数/超大值。

被测模块只依赖标准库，按文件路径加载，不联网、不碰真机。
"""

import importlib.util
import io
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import MagicMock, call, patch


def _load_module(module_name: str, relative: str) -> Any:
    """
    按文件路径加载插件内的纯标准库模块

    :param module_name (str): 加载后的模块名（避免与包内模块冲突）
    :param relative (str): 相对插件根目录的路径

    :return Any: 已加载模块
    """
    module_path = Path(__file__).resolve().parents[1] / relative
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_events(count: int, start_id: int = 301) -> list:
    """
    构造一批生活事件（新→旧，与 115 事件流顺序一致）

    :param count (int): 条数
    :param start_id (int): 最旧一条的 ID

    :return list: 事件列表
    """
    events = []
    for index in range(count):
        events.append(
            {
                "id": start_id + index,
                "update_time": 100 * (index + 1),
            }
        )
    # 反转成「新 → 旧」
    events.reverse()
    return events


class TestStallWatchdog(TestCase):
    """
    用例 1：心跳超阈值 ⇒ 判停摆、触发重启、栈被写出
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载被测模块
        """
        cls.guard = _load_module(
            "life_monitor_guard_under_test", "helper/life/monitor_guard.py"
        )

    def _start_blocked_thread(self):
        """
        启动一个卡在指定行的线程，便于取到真实调用栈

        :return Tuple[threading.Thread, threading.Event]: (线程, 释放事件)
        """
        ready = threading.Event()
        release = threading.Event()

        def target():
            ready.set()
            release.wait(10)

        thread = threading.Thread(
            target=target, name="P115StrmHelper-MonitorLife", daemon=True
        )
        thread.start()
        self.assertTrue(ready.wait(5))
        # addCleanup 是 LIFO：先放 join，再放 release，清理时先松开线程再等待退出
        self.addCleanup(lambda: thread.join(5))
        self.addCleanup(release.set)
        return thread, release

    def test_stalled_thread_restarts_and_dumps_stack(self) -> None:
        """
        心跳超过阈值时：WARNING（含分钟数/线程名/ident）→ 打栈 → 通知 → 停止 → 重启
        """
        thread, _ = self._start_blocked_thread()
        log = MagicMock(spec=["debug", "info", "warning", "error"])
        notify = MagicMock()
        stop = MagicMock()
        start = MagicMock()
        manager = MagicMock()
        manager.attach_mock(notify, "notify")
        manager.attach_mock(stop, "stop")
        manager.attach_mock(start, "start")

        stalled = self.guard.detect_and_handle_stall(
            last_progress_ts=1000.0,
            now=1000.0 + 16 * 60,
            timeout_minutes=15,
            thread=thread,
            log=log,
            notify=notify,
            stop=stop,
            start=start,
        )

        self.assertAlmostEqual(stalled, 16.0)
        log.warning.assert_called_once()
        warning_text = log.warning.call_args.args[0]
        self.assertIn("16.0", warning_text)
        self.assertIn("P115StrmHelper-MonitorLife", warning_text)
        self.assertIn(str(thread.ident), warning_text)
        self.assertIn("停摆", warning_text)

        # 拿不到日志句柄时回落 logger.error，栈里必须有本测试文件的调用行
        log.error.assert_called_once()
        stack_text = log.error.call_args.args[0]
        self.assertIn("test_life_monitor_guard.py", stack_text)
        self.assertIn('File "', stack_text)
        self.assertIn("release.wait", stack_text)

        # 动作顺序：通知 → 停止 → 回放重启（WARNING/打栈在其之前，见上方断言）
        self.assertEqual(
            manager.mock_calls,
            [call.notify(16.0), call.stop(), call.start()],
        )

    def test_stack_written_into_log_stream(self) -> None:
        """
        能拿到日志句柄时，调用栈直接写进句柄
        """
        thread, _ = self._start_blocked_thread()
        stream = io.StringIO()
        log = SimpleNamespace(
            handlers=[SimpleNamespace(stream=stream)],
            warning=MagicMock(),
            error=MagicMock(),
        )

        stack = self.guard.write_thread_stack(log, thread)

        self.assertIn("test_life_monitor_guard.py", stack)
        self.assertIn("test_life_monitor_guard.py", stream.getvalue())
        self.assertIn("停摆线程调用栈", stream.getvalue())
        log.error.assert_not_called()

    def test_healthy_thread_is_not_restarted(self) -> None:
        """
        心跳在阈值内不判停摆，不停不启
        """
        log = MagicMock(spec=["debug", "info", "warning", "error"])
        notify = MagicMock()
        stop = MagicMock()
        start = MagicMock()

        stalled = self.guard.detect_and_handle_stall(
            last_progress_ts=1000.0,
            now=1000.0 + 14 * 60,
            timeout_minutes=15,
            thread=None,
            log=log,
            notify=notify,
            stop=stop,
            start=start,
        )

        self.assertIsNone(stalled)
        log.warning.assert_not_called()
        notify.assert_not_called()
        stop.assert_not_called()
        start.assert_not_called()

    def test_watchdog_disabled_never_fires(self) -> None:
        """
        阈值 0 = 关闭看门狗，哪怕心跳早已过期也不处置
        """
        log = MagicMock(spec=["debug", "info", "warning", "error"])

        stalled = self.guard.detect_and_handle_stall(
            last_progress_ts=1000.0,
            now=1000.0 + 10 * 60 * 60,
            timeout_minutes=0,
            thread=None,
            log=log,
            stop=MagicMock(),
            start=MagicMock(),
        )

        self.assertIsNone(stalled)
        log.warning.assert_not_called()


class TestTransferWaitHeartbeat(TestCase):
    """
    用例 2：整理等待期间心跳持续更新 ⇒ 不误判
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载被测模块
        """
        cls.guard = _load_module(
            "life_monitor_guard_heartbeat_under_test", "helper/life/monitor_guard.py"
        )
        cls.wait = _load_module(
            "life_transfer_wait_heartbeat_under_test", "helper/life/transfer_wait.py"
        )

    def test_wait_feeds_heartbeat_every_round(self) -> None:
        """
        等待整理期间每轮喂心跳 ⇒ 判定为有进度，不停摆
        """
        clock = {"now": 100.0}
        heartbeat = {"ts": 100.0}
        queue_side_effect = [["task-1"], ["task-1"], ["task-1"], []]
        get_queue_tasks = MagicMock(side_effect=queue_side_effect)
        log = MagicMock()
        progress = MagicMock()

        def fake_time() -> float:
            clock["now"] += 600
            return clock["now"]

        def on_progress() -> None:
            progress()
            heartbeat["ts"] = clock["now"]

        with (
            patch.object(self.wait, "time", side_effect=fake_time),
            patch.object(self.wait, "sleep"),
        ):
            stopped = self.wait.wait_for_transfer_complete(
                get_queue_tasks=get_queue_tasks,
                stall_timeout_minutes=60,
                stop_event=None,
                log=log,
                on_progress=on_progress,
            )

        self.assertFalse(stopped)
        self.assertEqual(progress.call_count, 3)
        self.assertEqual(heartbeat["ts"], 1900.0)

        # 等待结束时立刻体检：心跳刚刷新 ⇒ 不判停摆
        self.assertIsNone(
            self.guard.evaluate_stall(
                heartbeat["ts"], clock["now"], timeout_minutes=15
            )
        )

        # 对照组：若这 3 轮不喂心跳，停在进入等待前的时间戳上就是停摆 30 分钟
        stalled = self.guard.evaluate_stall(100.0, clock["now"], timeout_minutes=15)
        self.assertIsNotNone(stalled)
        self.assertAlmostEqual(stalled, 30.0)

    def test_wait_without_heartbeat_callback_still_returns(self) -> None:
        """
        不传心跳回调也不影响原有等待语义（兼容旧调用方）
        """
        get_queue_tasks = MagicMock(side_effect=[["task-1"], []])
        log = MagicMock()

        with (
            patch.object(self.wait, "time", side_effect=[0]),
            patch.object(self.wait, "sleep"),
        ):
            stopped = self.wait.wait_for_transfer_complete(
                get_queue_tasks=get_queue_tasks,
                stall_timeout_minutes=60,
                stop_event=None,
                log=log,
            )

        self.assertFalse(stopped)
        log.warning.assert_not_called()


class TestReplayStartCursor(TestCase):
    """
    用例 3：看门狗重启后起始时间 = 持久化游标 - grace
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载被测模块
        """
        cls.guard = _load_module(
            "life_monitor_guard_replay_under_test", "helper/life/monitor_guard.py"
        )

    def _resolve(self, **kwargs):
        """
        以回放模式解析起始游标
        """
        payload = {
            "replay": True,
            "pull_mode": "latest",
            "persisted": None,
            "now": 10_000.0,
            "grace_minutes": 10,
        }
        payload.update(kwargs)
        return self.guard.resolve_monitor_life_start(**payload)

    def test_replay_subtracts_grace_from_persisted_cursor(self) -> None:
        """
        回放起始时间 = min(持久化游标, now) - grace，且不带旧 from_id
        """
        from_time, from_id = self._resolve(
            persisted={"from_time": 5000.0, "from_id": 42}
        )
        self.assertEqual(from_time, 5000.0 - 600)
        self.assertEqual(from_id, 0)

    def test_replay_uses_now_when_cursor_in_future(self) -> None:
        """
        游标超前于当前时间（时钟回拨）时按 now 起算
        """
        from_time, _ = self._resolve(persisted={"from_time": 99_000.0, "from_id": 7})
        self.assertEqual(from_time, 10_000.0 - 600)

    def test_replay_grace_zero_keeps_cursor_and_id(self) -> None:
        """
        grace=0（不回放）：直接用持久化游标，from_id 一并保留
        """
        from_time, from_id = self._resolve(
            persisted={"from_time": 5000.0, "from_id": 42},
            grace_minutes=0,
        )
        self.assertEqual(from_time, 5000.0)
        self.assertEqual(from_id, 42)

    def test_replay_falls_back_to_now_when_cursor_missing(self) -> None:
        """
        游标缺失：以 now 为基点再减 grace，保证最近一个宽限窗口仍被回放
        """
        for persisted in (None, {}, {"from_time": None}, {"from_time": -1}):
            with self.subTest(persisted=persisted):
                from_time, from_id = self._resolve(persisted=persisted)
                self.assertEqual(from_time, 10_000.0 - 600)
                self.assertEqual(from_id, 0)

    def test_manual_reload_keeps_first_pull_mode_semantics(self) -> None:
        """
        非回放（用户手工重载/改配置）必须保持 latest/all/last 老语义
        """
        cursor = {"from_time": 5000.0, "from_id": 42}

        self.assertEqual(
            self._resolve(replay=False, pull_mode="latest", now=10_000.0),
            (10_000.0, 0),
        )
        self.assertEqual(
            self._resolve(replay=False, pull_mode="all", now=10_000.0),
            (0.0, 0),
        )
        self.assertEqual(
            self._resolve(
                replay=False, pull_mode="last", persisted=cursor, now=10_000.0
            ),
            (5000.0, 42),
        )
        # last 但游标缺失：与旧实现一样回落到 latest
        self.assertEqual(
            self._resolve(replay=False, pull_mode="last", persisted=None, now=10_000.0),
            (10_000.0, 0),
        )

    def test_replay_grace_is_clamped(self) -> None:
        """
        grace 非法（负数）回落默认 10 分钟，超大钳到 24 小时
        """
        from_time, _ = self._resolve(
            persisted={"from_time": 10_000.0, "from_id": 1}, grace_minutes=-5
        )
        self.assertEqual(from_time, 10_000.0 - 10 * 60)

        from_time, _ = self._resolve(
            persisted={"from_time": 10_000.0, "from_id": 1}, grace_minutes=99_999
        )
        self.assertEqual(from_time, max(0.0, 10_000.0 - 1440 * 60))


class TestPullBatchLimit(TestCase):
    """
    用例 4：单轮拉取**全量抽干、不截断**（截断会永久丢事件）、心跳正常、游标已推进
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载被测模块
        """
        cls.guard = _load_module(
            "life_monitor_guard_pull_under_test", "helper/life/monitor_guard.py"
        )

    def test_full_drain_without_truncation(self) -> None:
        """
        事件数远超「告警阈值」也全部取出：游标推进到本批最新一条，不丢事件
        """
        events = _make_events(5)
        iterator = iter(events)
        first_event = next(iterator)
        progress = MagicMock()

        batch = self.guard.drain_events_batch(
            first_event,
            iterator,
            on_progress=progress,
        )

        # 全部 5 条都要在，且顺序与事件流一致（新 → 旧）
        self.assertEqual([e["id"] for e in batch], [305, 304, 303, 302, 301])
        self.assertGreaterEqual(progress.call_count, 5)

        from_time, from_id = self.guard.advance_cursor_from_events(100, 1, batch)
        self.assertEqual(from_time, 500)
        self.assertEqual(from_id, 305)

    def test_heartbeats_on_every_collected_event(self) -> None:
        """
        逐条迭代时每取到一条就打一次心跳（首条也要打）
        """
        events = _make_events(4)
        iterator = iter(events)
        first_event = next(iterator)
        progress = MagicMock()

        batch = self.guard.drain_events_batch(
            first_event, iterator, on_progress=progress
        )

        self.assertEqual(len(batch), 4)
        self.assertEqual(progress.call_count, 4)

    def test_count_callback_for_visibility(self) -> None:
        """
        每累计 progress_every 条回调一次，用于长窗口的日志可见性
        """
        events = _make_events(5)
        iterator = iter(events)
        first_event = next(iterator)
        on_count = MagicMock()

        batch = self.guard.drain_events_batch(
            first_event, iterator, on_count=on_count, progress_every=2
        )

        self.assertEqual(len(batch), 5)
        self.assertEqual([args.args[0] for args in on_count.call_args_list], [2, 4])

    def test_empty_batch_keeps_cursor(self) -> None:
        """
        没有事件时游标原地不动
        """
        self.assertEqual(self.guard.advance_cursor_from_events(7, 8, []), (7, 8))


class TestTransferWaitAbsoluteCap(TestCase):
    """
    用例 5：整理等待超过绝对上限 ⇒ 返回并记 WARNING
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载被测模块
        """
        cls.wait = _load_module(
            "life_transfer_wait_cap_under_test", "helper/life/transfer_wait.py"
        )

    def test_absolute_cap_resumes_with_warning(self) -> None:
        """
        队列一直有变化但等待超过绝对上限：记 WARNING 并返回 False 恢复拉取
        """
        tasks = ["task-1"]
        get_queue_tasks = MagicMock(return_value=tasks)
        log = MagicMock()

        with (
            patch.object(self.wait, "time", side_effect=[0, 1800, 4000]),
            patch.object(self.wait, "sleep"),
        ):
            stopped = self.wait.wait_for_transfer_complete(
                get_queue_tasks=get_queue_tasks,
                stall_timeout_minutes=120,
                stop_event=None,
                log=log,
                max_wait_hours=1,
            )

        self.assertFalse(stopped)
        log.warning.assert_called_once()
        args = log.warning.call_args.args
        self.assertIn("绝对上限", args[0])
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], 66)
        self.assertEqual(args[3], len(tasks))

    def test_default_has_no_absolute_cap(self) -> None:
        """
        不传绝对上限时行为与旧实现一致：只受无进展超时约束
        """
        get_queue_tasks = MagicMock(return_value=["task-1"])
        log = MagicMock()

        with (
            patch.object(self.wait, "time", side_effect=[0, 60]),
            patch.object(self.wait, "sleep"),
        ):
            stopped = self.wait.wait_for_transfer_complete(
                get_queue_tasks=get_queue_tasks,
                stall_timeout_minutes=1,
                stop_event=None,
                log=log,
            )

        self.assertFalse(stopped)
        log.warning.assert_called_once()
        self.assertNotIn("绝对上限", log.warning.call_args.args[0])


class TestConfigBounds(TestCase):
    """
    用例 6：配置边界——阈值 0（关闭）、最小值钳制、负数/超大值
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载被测模块
        """
        cls.guard = _load_module(
            "life_monitor_guard_config_under_test", "helper/life/monitor_guard.py"
        )

    def test_stall_timeout_bounds(self) -> None:
        """
        停摆阈值：0 关闭、1~4 钳到 5、负数/非法回落 15、超大钳到 1440
        """
        normalize = self.guard.normalize_stall_timeout_minutes
        cases = {
            0: 0,
            1: 5,
            4: 5,
            5: 5,
            15: 15,
            16: 16,
            -3: 15,
            -0.5: 15,
            None: 15,
            "abc": 15,
            99_999: 1440,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), expected)

    def test_replay_grace_bounds(self) -> None:
        """
        回放宽限：0 = 不回放、负数/非法回落 10、超大钳到 1440
        """
        normalize = self.guard.normalize_replay_grace_minutes
        cases = {0: 0, 1: 1, 10: 10, -5: 10, None: 10, "x": 10, 99_999: 1440}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), expected)

    def test_transfer_wait_max_hours_bounds(self) -> None:
        """
        整理等待绝对上限：小于 1 或非法回落 6、超大钳到 168
        """
        normalize = self.guard.normalize_transfer_wait_max_hours
        cases = {6: 6, 1: 1, 0: 6, -1: 6, None: 6, "3": 3, 9999: 168}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), expected)

    def test_large_window_warn_events_bounds(self) -> None:
        """
        单轮条数告警阈值：小于 1 或非法回落 2000、超大钳到 100000
        """
        normalize = self.guard.normalize_large_window_warn_events
        cases = {
            2000: 2000,
            1: 1,
            0: 2000,
            -10: 2000,
            None: 2000,
            "4000": 4000,
            999_999: 100_000,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), expected)

    def test_evaluate_stall_honours_bounds(self) -> None:
        """
        判定函数遵守归一化后的阈值（0 直接不判、1~4 按 5 分钟判、心跳未知不判）
        """
        evaluate = self.guard.evaluate_stall
        self.assertIsNone(evaluate(1000.0, 10_000.0, 0))
        # 4.5 分钟无进度：阈值 4 被钳到 5，因此不判停摆
        self.assertIsNone(evaluate(9_730.0, 10_000.0, 4))
        # 400 秒无进度：超过钳出来的 5 分钟阈值，判停摆
        self.assertAlmostEqual(
            evaluate(9_600.0, 10_000.0, 4), 400 / 60.0, places=3
        )
        self.assertIsNone(evaluate(None, 10_000.0, 15))
        self.assertIsNone(evaluate(0.0, 10_000.0, 15))
        self.assertIsNone(evaluate(-5.0, 10_000.0, 15))
