"""TargetWriter M1 契约测试：probe/commit/锁/写后核验，全部使用 fake。"""

import os
import sys
import threading
import time
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
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
target_writer_module = load_module(
    "cloudsubscribex.core.target_writer",
    "plugins.v2/cloudsubscribex/core/target_writer.py",
)

CloudDriveCapability = cloud_module.CloudDriveCapability
CloudFile = cloud_module.CloudFile
TargetPlan = target_writer_module.TargetPlan
TargetWriter = target_writer_module.TargetWriter
TargetConflictError = target_writer_module.TargetConflictError
TargetUnknownError = target_writer_module.TargetUnknownError


def make_file(file_id="f1", name="a.mkv", size=1024, sha1="", md5=""):
    return CloudFile(
        id=file_id, name=name, is_directory=False, size=size, sha1=sha1, md5=md5
    )


class FakeQuery:
    def __init__(self):
        self.files = {}
        self.find_error = None
        self.list_error = None
        self.find_calls = []

    def add(self, path, file):
        self.files[(path, file.name)] = file

    def find_file(self, path, name):
        self.find_calls.append((path, name))
        if self.find_error:
            raise self.find_error
        return self.files.get((path, name))

    def list_files_recursive(self, path):
        if self.list_error:
            raise self.list_error
        return [file for (file_path, _name), file in self.files.items()
                if file_path == path]


class FakeProvider:
    def __init__(self, query):
        self.query = query

    def require(self, capability):
        assert capability == CloudDriveCapability.FILE_QUERY
        return self.query


def make_writer(query):
    return TargetWriter(
        lambda key: FakeProvider(query), lock_registry={}
    )


def plan(name="a.mkv", size=1024, sha1="", md5="", path="/target"):
    return TargetPlan(
        provider_key="115", path=path, name=name,
        size=size, sha1=sha1, md5=md5,
    )


def test_probe_missing_returns_not_written():
    query = FakeQuery()
    result = make_writer(query).probe(plan())
    assert result.status == "not_written"
    assert result.file is None


def test_probe_same_name_size_reused_without_write():
    query = FakeQuery()
    query.add("/target", make_file(size=1024))
    called = []

    def write():
        called.append(True)
        return True

    result = make_writer(query).commit(plan(), write)
    assert result.status == "reused"
    assert called == []
    assert result.actual_name == "a.mkv"
    assert result.file.id == "f1"


def test_probe_same_name_size_and_sha1_reused():
    query = FakeQuery()
    query.add("/target", make_file(size=1024, sha1="ABC"))
    result = make_writer(query).probe(plan(sha1="abc"))
    assert result.status == "reused"


def test_probe_same_name_different_size_conflict_no_write():
    query = FakeQuery()
    query.add("/target", make_file(size=2048))
    called = []

    def write():
        called.append(True)
        return True

    writer = make_writer(query)
    result = writer.probe(plan(size=1024))
    assert result.status == "conflict"
    try:
        writer.commit(plan(size=1024), write)
    except TargetConflictError:
        pass
    else:
        raise AssertionError("expected TargetConflictError")
    assert called == []


def test_probe_same_name_size_but_different_sha1_conflict():
    query = FakeQuery()
    query.add("/target", make_file(size=1024, sha1="OLD"))
    result = make_writer(query).probe(plan(sha1="NEW"))
    assert result.status == "conflict"


def test_commit_write_true_verifies_written_file():
    query = FakeQuery()
    writer = make_writer(query)

    def write():
        query.add("/target", make_file(file_id="real-id", size=1024))
        return True

    result = writer.commit(plan(), write)
    assert result.status == "written"
    assert result.actual_name == "a.mkv"
    assert result.file.id == "real-id"
    assert result.actual_size == 1024


def test_commit_write_false_returns_not_written():
    query = FakeQuery()
    result = make_writer(query).commit(plan(), lambda: False)
    assert result.status == "not_written"
    assert result.file is None


def test_commit_write_true_but_missing_after_write_raises_unknown():
    query = FakeQuery()
    writer = make_writer(query)
    try:
        writer.commit(plan(), lambda: True)
    except TargetUnknownError:
        pass
    else:
        raise AssertionError("expected TargetUnknownError")


def test_concurrent_same_target_writes_once():
    query = FakeQuery()
    writer = make_writer(query)
    write_started = threading.Event()
    allow_write = threading.Event()
    write_count = [0]

    def write():
        write_count[0] += 1
        write_started.set()
        assert allow_write.wait(2)
        query.add("/target", make_file(size=1024))
        return True

    results = []

    def run():
        results.append(writer.commit(plan(), write))

    first = threading.Thread(target=run)
    first.start()
    assert write_started.wait(2)
    second = threading.Thread(target=run)
    second.start()
    time.sleep(0.15)
    allow_write.set()
    first.join(2)
    second.join(2)
    assert write_count[0] == 1
    assert sorted(item.status for item in results) == ["reused", "written"]


def test_commit_renamed_file_matches_by_size_and_returns_actual_name():
    query = FakeQuery()
    writer = make_writer(query)
    renamed = make_file(file_id="renamed", name="a (2).mkv", size=1024)

    def write():
        query.add("/target", renamed)
        return True

    result = writer.commit(plan(), write)
    assert result.status == "written"
    assert result.actual_name == "a (2).mkv"
    assert result.file.id == "renamed"


def test_probe_query_error_returns_unknown_without_write():
    query = FakeQuery()
    query.find_error = RuntimeError("directory unavailable")
    called = []

    def write():
        called.append(True)
        return True

    result = make_writer(query).probe(plan())
    assert result.status == "unknown"
    assert called == []
