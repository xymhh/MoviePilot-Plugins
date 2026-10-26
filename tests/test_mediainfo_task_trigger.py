"""媒体信息提取改造单测（1.7.74）。

覆盖 docs/plan/2026-10-26-媒体信息提取改走神医任务.md：

1. Emby 连接异常时 ``_emby_request`` 带出真实原因，不再被 ``with None`` 的
   ``TypeError`` 吞掉，并按退避重排；
2. 命中媒体项后触发 Emby 计划任务 ``Extract MediaInfo``（POST Running/{id}）；
3. 触发有 5 分钟冷却，同批次不会反复拉全库任务；
4. 任务不存在时给明确告警且不报错；
5. 任务已在运行时视为成功、不重复 POST。
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


class _FakeRequestUtils:
    """可编程的 RequestUtils 替身：按 URL 返回预设响应或抛异常。"""

    responses = {}
    raised = {}
    calls = []

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def request(self, method="get", url="", params=None, **kwargs):
        key = (method, url)
        _FakeRequestUtils.calls.append((method, url, dict(params or {})))
        if key in _FakeRequestUtils.raised:
            raise _FakeRequestUtils.raised[key]
        return _FakeRequestUtils.responses.get(key)


def _response(payload, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload,
    )


install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.utils",
    "app.utils.http", "app.utils.string", "app.utils.path",
)
register_module("app.utils.http", RequestUtils=_FakeRequestUtils)
register_module("app.chain.mediaserver", MediaServerChain=MagicMock)
register_module("app.helper.mediaserver", MediaServerHelper=MagicMock)
register_module("app.schemas", MediaInfo=SimpleNamespace, RefreshMediaItem=SimpleNamespace)
register_module("app.log", logger=MagicMock())

ensure_package("cloudsubscribex")
sys.modules["cloudsubscribex"].__path__ = [
    os.path.join(plugins_v2_path, "cloudsubscribex")
]
ensure_package("cloudsubscribex.handlers")
ensure_package("cloudsubscribex.handlers.notification")
ensure_package("cloudsubscribex.utils")
ensure_package("cloudsubscribex.core")
load_module(
    "cloudsubscribex.utils.path_map",
    "plugins.v2/cloudsubscribex/utils/path_map.py",
)
media_server_module = load_module(
    "cloudsubscribex.handlers.notification.media_server",
    "plugins.v2/cloudsubscribex/handlers/notification/media_server.py",
)
MediaServerNotifier = media_server_module.MediaServerNotifier
logger_mock = media_server_module.logger

HOST = "http://emby.local:8096"


class _MediainfoTriggerTests(unittest.TestCase):
    def setUp(self):
        _FakeRequestUtils.responses = {}
        _FakeRequestUtils.raised = {}
        _FakeRequestUtils.calls = []
        self.handler = MediaServerNotifier(
            enabled=True,
            mediaservers=["Emby"],
            path_mappings="/media/strm=/strm",
            emby_mediainfo_enabled=True,
        )
        self.handler._emby_connection = lambda name: (HOST, "key", "user")
        self.scheduled = []
        self.handler._schedule_emby_mediainfo = (
            lambda name, path, attempt: self.scheduled.append(attempt)
        )
        self.path = Path("/strm/115网盘/剧/剧 S01E01.mkv")

    def _queue_items(self, item_id="9527"):
        _FakeRequestUtils.responses[("get", f"{HOST}/emby/Items")] = _response(
            {"Items": [{"Id": item_id, "Path": "/media/strm/115网盘/剧/剧 S01E01.mkv"}]}
        )

    def _queue_tasks(self, tasks):
        _FakeRequestUtils.responses[
            ("get", f"{HOST}/emby/ScheduledTasks")
        ] = _response(tasks)
        _FakeRequestUtils.responses[
            ("post", f"{HOST}/emby/ScheduledTasks/Running/task-1")
        ] = _response(None, status_code=204)

    def test_1_connection_error_reports_real_reason(self):
        """1. 连接异常 → 日志带真实原因，不是 TypeError，且退避重排。"""
        _FakeRequestUtils.raised[("get", f"{HOST}/emby/Items")] = ConnectionError(
            "Connection refused"
        )
        self.handler._trigger_emby_mediainfo("Emby", self.path, attempt=1)
        self.assertEqual(self.scheduled, [2])
        messages = [str(call.args[0]) for call in logger_mock.warning.call_args_list]
        self.assertTrue(any("Connection refused" in message for message in messages))
        self.assertFalse(any("context manager protocol" in message for message in messages))

    def test_2_item_found_triggers_extract_task(self):
        """2. 命中媒体项 → 触发 Extract MediaInfo 计划任务。"""
        self._queue_items()
        self._queue_tasks([
            {"Id": "task-1", "Name": "Extract MediaInfo", "State": "Idle"},
        ])
        self.handler._trigger_emby_mediainfo("Emby", self.path, attempt=1)
        posts = [call for call in _FakeRequestUtils.calls if call[0] == "post"]
        self.assertEqual(
            [call[1] for call in posts],
            [f"{HOST}/emby/ScheduledTasks/Running/task-1"],
        )
        self.assertIn("Emby", self.handler._mediainfo_task_recent)

    def test_3_task_trigger_cooldown(self):
        """3. 冷却期内不再查任务、不再触发。"""
        self._queue_items()
        self._queue_tasks([
            {"Id": "task-1", "Name": "Extract MediaInfo", "State": "Idle"},
        ])
        self.handler._trigger_emby_mediainfo("Emby", self.path, attempt=1)
        before = len(_FakeRequestUtils.calls)
        self.handler._trigger_emby_mediainfo("Emby", self.path, attempt=1)
        self.assertEqual(len(_FakeRequestUtils.calls), before + 1)  # 只多一次 Items 查询

    def test_4_missing_task_warns_and_skips(self):
        """4. 没有该计划任务 → 告警且返回 False，不抛异常。"""
        self._queue_items()
        self._queue_tasks([{"Id": "task-9", "Name": "Scan media library", "State": "Idle"}])
        self.assertFalse(self.handler._trigger_mediainfo_task("Emby", HOST, "key"))
        messages = [str(call.args[0]) for call in logger_mock.warning.call_args_list]
        self.assertTrue(any("Extract MediaInfo" in message for message in messages))

    def test_5_running_task_not_reposted(self):
        """5. 任务已在运行 → 视为成功、不重复 POST。"""
        self._queue_tasks([
            {"Id": "task-1", "Name": "Extract MediaInfo", "State": "Running"},
        ])
        self.assertTrue(self.handler._trigger_mediainfo_task("Emby", HOST, "key"))
        self.assertFalse([call for call in _FakeRequestUtils.calls if call[0] == "post"])

    def test_6_item_missing_reschedules(self):
        """6. 查不到媒体项 → 退避重排（保留原行为）。"""
        _FakeRequestUtils.responses[("get", f"{HOST}/emby/Items")] = _response(
            {"Items": []}
        )
        self.handler._trigger_emby_mediainfo("Emby", self.path, attempt=1)
        self.assertEqual(self.scheduled, [2])


if __name__ == "__main__":
    unittest.main()
