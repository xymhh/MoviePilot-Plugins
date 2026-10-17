"""跨盘传输：115 秒传未命中 → 深圳中继接管的接缝测试。

固定契约：
1. 中继成功 → 返回 True、method="relay"、继续查回目标文件；
2. 中继失败/不可达 → 默认回落本机（返回 False）；
3. relay_fallback_local=False → 直接抛错，不静默降级。
"""

import os
import sys
from threading import Event
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
relay_module = load_module(
    "cloudsubscribex.core.relay", "plugins.v2/cloudsubscribex/core/relay.py"
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)

CloudDriveCapability = cloud_module.CloudDriveCapability
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager
RelayError = relay_module.RelayError


class FakeFile:
    id = "f1"
    name = "a.mkv"
    size = 1024
    sha1 = "AB"
    md5 = ""


class FakeQuery:
    def find_file(self, path, name):
        return FakeFile()


class FakeTarget:
    def __init__(self):
        self.query = FakeQuery()

    def supports(self, capability):
        return capability == CloudDriveCapability.FILE_QUERY

    def require(self, capability):
        if capability == CloudDriveCapability.FILE_QUERY:
            return self.query
        raise AssertionError(f"unexpected capability {capability}")


class FakeRelay:
    enabled = True

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.submitted = []
        self.cancelled = []

    def submit(self, payload):
        self.submitted.append(payload)
        return {"id": "job1"}

    def get(self, job_id):
        return self.statuses.pop(0) if self.statuses else {"status": "running"}

    def cancel(self, job_id):
        self.cancelled.append(job_id)


def make_manager(relay, fallback=True):
    manager = CrossTransferTaskManager(
        lambda key: None, relay_client=relay,
        relay_max_bytes=10 * 1024 ** 3,
        relay_timeout_seconds=60,
        relay_fallback_local=fallback,
    )
    manager._update = lambda *args, **kwargs: None
    return manager


def make_task():
    return {
        "source_provider": "quark",
        "source_file": FakeFile(),
        "source_path": "",
        "title": "a.mkv",
        "total": 1024,
        "target_provider": "115",
        "target_path": "/整理/待整理",
    }


def test_relay_success_returns_true_and_marks_success():
    relay = FakeRelay([
        {"status": "running", "phase": "download", "progress": 40,
         "transferred": 400, "message": "下载中"},
        {"status": "success", "phase": "done", "progress": 100,
         "result": {"file_name": "a.mkv", "size": 1024, "sha1": "AB"}},
    ])
    manager = make_manager(relay)
    updates = []
    manager._update = lambda *args, **kwargs: updates.append(kwargs)
    ok = manager._relay_transfer(
        make_task(), "task1", FakeTarget(), "/整理/待整理", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is True
    assert relay.submitted[0]["file_id"] == "f1"
    assert relay.submitted[0]["target"]["name"] == "a.mkv"
    assert any(item.get("method") == "relay" for item in updates)


def test_relay_failure_falls_back():
    relay = FakeRelay([{"status": "failed", "error": "boom"}])
    manager = make_manager(relay)
    ok = manager._relay_transfer(
        make_task(), "task1", FakeTarget(), "/x", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is False


def test_relay_submit_error_falls_back():
    class BrokenRelay(FakeRelay):
        def submit(self, payload):
            raise RelayError("down")

    manager = make_manager(BrokenRelay([]))
    ok = manager._relay_transfer(
        make_task(), "task1", FakeTarget(), "/x", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is False


def test_relay_submit_error_raises_without_fallback():
    class BrokenRelay(FakeRelay):
        def submit(self, payload):
            raise RelayError("down")

    manager = make_manager(BrokenRelay([]), fallback=False)
    try:
        manager._relay_transfer(
            make_task(), "task1", FakeTarget(), "/x", "a.mkv",
            "115", "AB", 1024, Event(),
        )
    except RuntimeError as error:
        assert "深圳中继不可用" in str(error)
    else:
        raise AssertionError("expected RuntimeError")
