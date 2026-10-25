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


class RunUpload:
    def __init__(self, target):
        self.target = target
        self.upload_file_calls = []
        self.progressive_calls = []
        self.from_link_calls = []

    def upload_file(self, local_path, save_path, target_name="", **kwargs):
        size = os.path.getsize(local_path)
        self.upload_file_calls.append((save_path, target_name, size))
        self.target.add_file(save_path, FakeFile(
            file_id="uploaded", name=target_name, size=size
        ))
        return True

    def upload_progressive(
            self, local_path, save_path, target_name, expected_size,
            algorithm, checksum, wait_for_range, progress_callback, stop_requested,
    ):
        self.progressive_calls.append((save_path, target_name, expected_size))
        wait_for_range(0, expected_size - 1)
        self.target.add_file(save_path, FakeFile(
            file_id="progressive", name=target_name, size=expected_size,
            sha1=checksum if algorithm == "sha1" else "",
            md5=checksum if algorithm == "md5" else "",
        ))
        return True

    def upload_from_link(
            self, download_url, download_headers, save_path, target_name,
            size, algorithm, checksum, progress_callback, stop_requested,
            refresh_link=None,
    ):
        self.from_link_calls.append((save_path, target_name, size))
        self.target.add_file(save_path, FakeFile(
            file_id="range", name=target_name, size=size,
            sha1=checksum if algorithm == "sha1" else "",
        ))
        return True


class RunTarget(FakeTarget):
    def __init__(self):
        super().__init__()
        self.upload = RunUpload(self)

    def supports(self, capability):
        return capability in {
            CloudDriveCapability.FILE_QUERY,
            CloudDriveCapability.LOCAL_UPLOAD,
        }

    def require(self, capability):
        if capability == CloudDriveCapability.LOCAL_UPLOAD:
            return self.upload
        return super().require(capability)


class RunSource:
    def __init__(self, name="quark"):
        self.name = name
        self.download_calls = []
        self.link_calls = []

    def supports(self, capability):
        return capability == CloudDriveCapability.FILE_DOWNLOAD

    def require(self, capability):
        if capability == CloudDriveCapability.FILE_DOWNLOAD:
            return self
        raise AssertionError(f"unexpected capability {capability}")

    def resolve_download_link(self, file_item):
        self.link_calls.append(file_item)
        return "https://fake.example/file", {}

    def download_file(
            self, file_item, local_path, progress_callback=None,
            stop_requested=None, preserve_partial=False, download_threads=5,
    ):
        size = int(file_item.size or 0)
        with open(local_path, "wb") as handle:
            handle.write(b"x" * size)
        self.download_calls.append((local_path, size))
        if progress_callback:
            progress_callback(size, size)
        return str(local_path)


def make_run_manager(target, source=None, download_path=""):
    def resolve(key):
        return target if key == "115" else source

    return CrossTransferTaskManager(
        resolve, download_path=download_path, max_concurrent=2,
    )


def make_run_task(
        target_path="/target", name="a.mkv", total=1024,
        algorithm="", checksum="", source_path="", source_file=None,
):
    return {
        "id": "task1",
        "source_provider": "quark",
        "source_file": source_file,
        "source_path": source_path,
        "source_file_name": name,
        "title": name,
        "total": total,
        "target_provider": "115",
        "target_path": target_path,
        "algorithm": algorithm,
        "fallback": True,
        "stop_event": Event(),
        "status": "pending",
    }


def run_task(manager, task, checksum):
    manager._tasks["task1"] = task
    manager._run("task1", checksum)
    return task


def test_progressive_reuses_existing_target_without_download_or_upload(tmp_path):
    checksum = "A" * 32
    target = RunTarget()
    target.add_file("/progressive", FakeFile(
        file_id="existing", name="a.mkv", size=1024, md5=checksum
    ))
    source = RunSource()
    manager = make_run_manager(target, source, str(tmp_path))
    task = make_run_task(
        target_path="/progressive", total=1024,
        algorithm="md5", checksum=checksum,
        source_file=FakeFile(name="a.mkv", size=1024, sha1="", md5=checksum),
    )
    run_task(manager, task, checksum)
    assert task["status"] == "success"
    assert task["method"] == "reuse"
    assert task["result_file_id"] == "existing"
    assert task["result_file_name"] == "a.mkv"
    assert source.download_calls == []
    assert target.upload.progressive_calls == []


def test_progressive_writes_once_and_backfills_real_name(tmp_path):
    checksum = "B" * 32
    target = RunTarget()
    source = RunSource()
    manager = make_run_manager(target, source, str(tmp_path))
    task = make_run_task(
        target_path="/progressive-write", total=1024,
        algorithm="md5", checksum=checksum,
        source_file=FakeFile(name="a.mkv", size=1024, sha1="", md5=""),
    )
    run_task(manager, task, checksum)
    assert task["status"] == "success"
    assert task["method"] == "progressive"
    assert task["result_file_id"] == "progressive"
    assert task["result_file_name"] == "a.mkv"
    assert len(target.upload.progressive_calls) == 1
    assert len(source.download_calls) == 1


def test_local_upload_reuses_existing_target_without_transfer(tmp_path):
    local_path = tmp_path / "source.bin"
    local_path.write_bytes(b"y" * 1024)
    target = RunTarget()
    target.add_file("/local", FakeFile(
        file_id="existing", name="movie.mkv", size=1024
    ))
    manager = make_run_manager(target, None, str(tmp_path))
    task = make_run_task(
        target_path="/local", name="movie.mkv", total=1024,
        source_path=str(local_path),
    )
    run_task(manager, task, "")
    assert task["status"] == "success"
    assert task["method"] == "reuse"
    assert task["result_file_id"] == "existing"
    assert task["result_file_name"] == "movie.mkv"
    assert target.upload.upload_file_calls == []


def test_local_upload_writes_once_and_backfills_real_name(tmp_path):
    local_path = tmp_path / "source.bin"
    local_path.write_bytes(b"z" * 1024)
    target = RunTarget()
    manager = make_run_manager(target, None, str(tmp_path))
    task = make_run_task(
        target_path="/local-write", name="movie.mkv", total=1024,
        source_path=str(local_path),
    )
    run_task(manager, task, "")
    assert task["status"] == "success"
    assert task["method"] == "fallback"
    assert task["result_file_id"] == "uploaded"
    assert task["result_file_name"] == "movie.mkv"
    assert len(target.upload.upload_file_calls) == 1


def test_local_upload_conflict_fails_without_transfer(tmp_path):
    local_path = tmp_path / "source.bin"
    local_path.write_bytes(b"w" * 1024)
    target = RunTarget()
    target.add_file("/local-conflict", FakeFile(
        file_id="existing", name="movie.mkv", size=2048
    ))
    manager = make_run_manager(target, None, str(tmp_path))
    task = make_run_task(
        target_path="/local-conflict", name="movie.mkv", total=1024,
        source_path=str(local_path),
    )
    run_task(manager, task, "")
    assert task["status"] == "failed"
    assert target.upload.upload_file_calls == []
    assert "result_file_id" not in task


def test_range_reuses_existing_target_without_upload_from_link(tmp_path):
    checksum = "C" * 40
    target = RunTarget()
    target.add_file("/range", FakeFile(
        file_id="existing", name="a.mkv", size=1024, sha1=checksum
    ))
    source = RunSource()
    manager = make_run_manager(target, source, str(tmp_path))
    task = make_run_task(
        target_path="/range", total=1024,
        algorithm="sha1", checksum=checksum,
        source_file=FakeFile(name="a.mkv", size=1024, sha1=checksum),
    )
    run_task(manager, task, checksum)
    assert task["status"] == "success"
    assert task["method"] == "reuse"
    assert task["result_file_id"] == "existing"
    assert task["result_file_name"] == "a.mkv"
    assert target.upload.from_link_calls == []
    assert source.link_calls == []


def test_local_upload_renamed_target_is_verified_by_writer(tmp_path):
    local_path = tmp_path / "source.bin"
    local_path.write_bytes(b"r" * 1024)
    checksum = "D" * 40
    target = RunTarget()

    def upload_file(_local_path, save_path, target_name="", **_kwargs):
        size = os.path.getsize(_local_path)
        target.upload.upload_file_calls.append((save_path, target_name, size))
        target.add_file(save_path, FakeFile(
            file_id="renamed", name="movie (2).mkv", size=size,
            sha1=checksum,
        ))
        return True

    target.upload.upload_file = upload_file
    manager = make_run_manager(target, None, str(tmp_path))
    task = make_run_task(
        target_path="/local-renamed", name="movie.mkv", total=1024,
        algorithm="sha1", checksum=checksum, source_path=str(local_path),
    )
    run_task(manager, task, checksum)
    assert task["status"] == "success"
    assert task["method"] == "fallback"
    assert task["result_file_name"] == "movie (2).mkv"
    assert task["result_file_id"] == "renamed"
    assert len(target.upload.upload_file_calls) == 1


def test_progressive_renamed_target_is_verified_by_writer(tmp_path):
    checksum = "E" * 32
    target = RunTarget()
    source = RunSource()

    def upload_progressive(
            _local_path, save_path, target_name, expected_size,
            algorithm, _checksum, wait_for_range, _progress_callback,
            _stop_requested,
    ):
        target.upload.progressive_calls.append(
            (save_path, target_name, expected_size)
        )
        wait_for_range(0, expected_size - 1)
        target.add_file(save_path, FakeFile(
            file_id="renamed-progressive", name="a (2).mkv",
            size=expected_size,
            md5=_checksum if algorithm == "md5" else "",
        ))
        return True

    target.upload.upload_progressive = upload_progressive
    manager = make_run_manager(target, source, str(tmp_path))
    task = make_run_task(
        target_path="/progressive-renamed", total=1024,
        algorithm="md5", checksum=checksum,
        source_file=FakeFile(name="a.mkv", size=1024, sha1="", md5=""),
    )
    run_task(manager, task, checksum)
    assert task["status"] == "success"
    assert task["method"] == "progressive"
    assert task["result_file_name"] == "a (2).mkv"
    assert task["result_file_id"] == "renamed-progressive"
    assert len(target.upload.progressive_calls) == 1
    assert len(source.download_calls) == 1


def test_local_upload_source_sha1_conflict_fails_without_transfer(tmp_path):
    local_path = tmp_path / "source.bin"
    local_path.write_bytes(b"s" * 1024)
    target = RunTarget()
    target.add_file("/local-sha-conflict", FakeFile(
        file_id="existing", name="movie.mkv", size=1024, sha1="B" * 40
    ))
    manager = make_run_manager(target, None, str(tmp_path))
    task = make_run_task(
        target_path="/local-sha-conflict", name="movie.mkv", total=1024,
        source_path=str(local_path),
        source_file=FakeFile(name="movie.mkv", size=1024,
                             sha1="A" * 40, md5=""),
    )
    run_task(manager, task, "")
    assert task["status"] == "failed"
    assert target.upload.upload_file_calls == []


def test_local_upload_source_sha1_match_reuses_without_transfer(tmp_path):
    local_path = tmp_path / "source.bin"
    local_path.write_bytes(b"t" * 1024)
    target = RunTarget()
    target.add_file("/local-sha-reuse", FakeFile(
        file_id="existing", name="movie.mkv", size=1024, sha1="A" * 40
    ))
    manager = make_run_manager(target, None, str(tmp_path))
    task = make_run_task(
        target_path="/local-sha-reuse", name="movie.mkv", total=1024,
        source_path=str(local_path),
        source_file=FakeFile(name="movie.mkv", size=1024,
                             sha1="A" * 40, md5=""),
    )
    run_task(manager, task, "")
    assert task["status"] == "success"
    assert task["method"] == "reuse"
    assert task["result_file_id"] == "existing"
    assert target.upload.upload_file_calls == []


def test_relay_always_is_stored_and_defaults_off():
    manager = make_manager(FakeRelay([]))
    assert manager._relay_always is False

    manager = CrossTransferTaskManager(
        lambda key: FakeTarget(),
        relay_client=FakeRelay([]),
        relay_max_bytes=10 * 1024 ** 3,
        relay_timeout_seconds=60,
        relay_fallback_local=True,
        relay_always=True,
    )
    assert manager._relay_always is True


def test_relay_always_takes_over_without_rapid_probe(monkeypatch):
    """一律走中继：不探 115 秒传、不下载，直接提交中继任务。"""
    target = FakeTarget()
    relay = FakeRelay(
        [_success_status()],
        on_success=lambda _s: target.add_file("/整理/待整理", FakeFile()),
    )
    manager = make_manager(relay, target=target)
    manager._relay_always = True
    manager._rapid_miss_action = "fallback"
    updates = []
    manager._update = lambda *args, **kwargs: updates.append(kwargs)

    called = {"download": 0}

    def _boom(*args, **kwargs):
        called["download"] += 1
        raise AssertionError("一律走中继时不应触发本机下载")

    monkeypatch.setattr(manager, "_download_quark_file", _boom, raising=False)

    task = make_task()
    task.update({
        "id": "task-always",
        "algorithm": "SHA1",
        "cache_key": "",
        "stop_event": Event(),
        "status": "queued",
        "fallback": False,
        "image": "",
        "description": "",
        "source_file": FakeFile(),
    })
    manager._tasks["task-always"] = task
    manager._run_locked("task-always", "AB", "")
    assert relay.submitted, "中继应收到任务"
    assert called["download"] == 0
