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
TargetConflictError = sys.modules[
    "cloudsubscribex.core.target_writer"
].TargetConflictError



class FakeFile:
    def __init__(self, file_id="f1", name="a.mkv", size=1024, sha1="AB", md5=""):
        self.id = file_id
        self.name = name
        self.size = size
        self.sha1 = sha1
        self.md5 = md5


class FakeQuery:
    def __init__(self):
        self.files = {}

    def find_file(self, path, name):
        return self.files.get((path, name))

    def list_files_recursive(self, path):
        return [file for (file_path, _name), file in self.files.items()
                if file_path == path]


class FakeTarget:
    def __init__(self):
        self.query = FakeQuery()

    def add_file(self, path, file):
        self.query.files[(path, file.name)] = file

    def supports(self, capability):
        return capability == CloudDriveCapability.FILE_QUERY

    def require(self, capability):
        if capability == CloudDriveCapability.FILE_QUERY:
            return self.query
        raise AssertionError(f"unexpected capability {capability}")


class FakeRelay:
    enabled = True

    def __init__(self, statuses, on_success=None):
        self.statuses = list(statuses)
        self.submitted = []
        self.cancelled = []
        self.on_success = on_success

    def submit(self, payload):
        self.submitted.append(payload)
        return {"id": "job1"}

    def get(self, job_id):
        current = self.statuses.pop(0) if self.statuses else {"status": "running"}
        if current.get("status") == "success" and self.on_success:
            callback, self.on_success = self.on_success, None
            callback(current)
        return current

    def cancel(self, job_id):
        self.cancelled.append(job_id)


def make_manager(relay, fallback=True, relay_connections=0, target=None):
    provider = target if target is not None else FakeTarget()
    manager = CrossTransferTaskManager(
        lambda key: provider, relay_client=relay,
        relay_max_bytes=10 * 1024 ** 3,
        relay_timeout_seconds=60,
        relay_fallback_local=fallback,
        relay_connections=relay_connections,
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


def _success_status(file_name="a.mkv", size=1024, sha1="AB"):
    return {
        "status": "success", "phase": "done", "progress": 100,
        "result": {"file_name": file_name, "size": size, "sha1": sha1},
    }


def test_relay_success_returns_true_and_marks_success():
    target = FakeTarget()
    relay = FakeRelay([
        {"status": "running", "phase": "download", "progress": 40,
         "transferred": 400, "message": "下载中"},
        _success_status(),
    ], on_success=lambda _status: target.add_file("/整理/待整理", FakeFile()))
    manager = make_manager(relay, target=target)
    updates = []
    manager._update = lambda *args, **kwargs: updates.append(kwargs)
    ok = manager._relay_transfer(
        make_task(), "task1", target, "/整理/待整理", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is True
    assert relay.submitted[0]["file_id"] == "f1"
    assert relay.submitted[0]["target"]["name"] == "a.mkv"
    assert relay.submitted[0]["connections"] == 0
    success_updates = [item for item in updates if item.get("status") == "success"]
    assert len(success_updates) == 1
    assert success_updates[0]["method"] == "relay"
    assert success_updates[0]["result_file_name"] == "a.mkv"


def test_relay_payload_carries_configured_connections():
    target = FakeTarget()
    relay = FakeRelay([
        _success_status(),
    ], on_success=lambda _status: target.add_file("/整理/待整理", FakeFile()))
    manager = make_manager(relay, relay_connections=12, target=target)
    ok = manager._relay_transfer(
        make_task(), "task1", target, "/整理/待整理", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is True
    assert relay.submitted[0]["connections"] == 12


def test_relay_target_exists_and_matches_reuses_without_submit():
    target = FakeTarget()
    target.add_file("/dedup", FakeFile(file_id="existing", name="dup.mkv",
                                       size=1024, sha1="DUP"))
    relay = FakeRelay([])
    manager = make_manager(relay, target=target)
    updates = []
    manager._update = lambda *args, **kwargs: updates.append(kwargs)
    ok = manager._relay_transfer(
        make_task(), "task1", target, "/dedup", "dup.mkv",
        "115", "DUP", 1024, Event(),
    )
    assert ok is True
    assert relay.submitted == []
    success_updates = [item for item in updates if item.get("status") == "success"]
    assert len(success_updates) == 1
    assert success_updates[0]["method"] == "relay-reuse"
    assert success_updates[0]["result_file_id"] == "existing"
    assert success_updates[0]["result_file_name"] == "dup.mkv"


def test_relay_missing_target_submits_once_and_marks_success():
    target = FakeTarget()
    relay = FakeRelay([
        _success_status(file_name="new.mkv", sha1="NEW"),
    ], on_success=lambda _status: target.add_file(
        "/new", FakeFile(file_id="written", name="new.mkv", size=1024, sha1="NEW")
    ))
    manager = make_manager(relay, target=target)
    updates = []
    manager._update = lambda *args, **kwargs: updates.append(kwargs)
    ok = manager._relay_transfer(
        make_task(), "task1", target, "/new", "new.mkv",
        "115", "NEW", 1024, Event(),
    )
    assert ok is True
    assert len(relay.submitted) == 1
    success_updates = [item for item in updates if item.get("status") == "success"]
    assert success_updates[0]["method"] == "relay"
    assert success_updates[0]["result_file_id"] == "written"
    assert success_updates[0]["result_file_name"] == "new.mkv"


def test_relay_same_target_and_sha1_is_not_submitted_twice():
    target = FakeTarget()
    relay = FakeRelay([_success_status(file_name="dup.mkv", sha1="DUP")],
                      on_success=lambda _status: target.add_file(
                          "/dedup", FakeFile(name="dup.mkv", size=1024, sha1="DUP")
                      ))
    manager = make_manager(relay, target=target)
    first = manager._relay_transfer(
        make_task(), "task1", target, "/dedup", "dup.mkv",
        "115", "DUP", 1024, Event(),
    )
    second = manager._relay_transfer(
        make_task(), "task2", target, "/dedup", "dup.mkv",
        "115", "DUP", 1024, Event(),
    )
    assert first is True
    assert second is True
    assert len(relay.submitted) == 1


def test_relay_same_name_different_content_conflicts_without_submit():
    target = FakeTarget()
    target.add_file("/dedup-upgrade", FakeFile(
        name="upgrade.mkv", size=1024, sha1="OLD"
    ))
    relay = FakeRelay([])
    manager = make_manager(relay, target=target)
    try:
        manager._relay_transfer(
            make_task(), "task1", target, "/dedup-upgrade", "upgrade.mkv",
            "115", "NEW", 2048, Event(),
        )
    except TargetConflictError:
        pass
    else:
        raise AssertionError("expected TargetConflictError")
    assert relay.submitted == []


def test_relay_success_but_missing_target_fails_without_success(monkeypatch):
    target = FakeTarget()
    relay = FakeRelay([_success_status()])
    manager = make_manager(relay, target=target)
    updates = []
    manager._update = lambda *args, **kwargs: updates.append(kwargs)
    monkeypatch.setattr(transfer_module.time, "sleep", lambda _seconds: None)
    try:
        manager._relay_transfer(
            make_task(), "task1", target, "/missing", "a.mkv",
            "115", "AB", 1024, Event(),
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")
    assert not any(item.get("status") == "success" for item in updates)


def test_relay_failure_falls_back():
    target = FakeTarget()
    relay = FakeRelay([{"status": "failed", "error": "boom"}])
    manager = make_manager(relay, target=target)
    ok = manager._relay_transfer(
        make_task(), "task1", target, "/x", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is False


def test_relay_submit_error_falls_back():
    class BrokenRelay(FakeRelay):
        def submit(self, payload):
            raise RelayError("down")

    target = FakeTarget()
    manager = make_manager(BrokenRelay([]), target=target)
    ok = manager._relay_transfer(
        make_task(), "task1", target, "/x", "a.mkv",
        "115", "AB", 1024, Event(),
    )
    assert ok is False


def test_relay_submit_error_raises_without_fallback():
    class BrokenRelay(FakeRelay):
        def submit(self, payload):
            raise RelayError("down")

    target = FakeTarget()
    manager = make_manager(BrokenRelay([]), fallback=False, target=target)
    try:
        manager._relay_transfer(
            make_task(), "task1", target, "/x", "a.mkv",
            "115", "AB", 1024, Event(),
        )
    except RuntimeError as error:
        assert "深圳中继不可用" in str(error)
    else:
        raise AssertionError("expected RuntimeError")
