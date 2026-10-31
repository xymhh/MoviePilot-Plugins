"""aria2 下载服务语义：完成校验、换链、取消清理和结果清理。"""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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

load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
load_module(
    "cloudsubscribex.core.target_writer",
    "plugins.v2/cloudsubscribex/core/target_writer.py",
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer",
    "plugins.v2/cloudsubscribex/core/transfer.py",
)

aria2_module = sys.modules["cloudsubscribex.core.aria2"]
Aria2FileDownloadService = aria2_module.Aria2FileDownloadService


class FakeFile:
    def __init__(self, size, name="a.mkv"):
        self.id = "f1"
        self.name = name
        self.size = size
        self.sha1 = ""
        self.md5 = ""


class FakeRpc:
    def __init__(self, target, statuses=None):
        self.target = target
        self.statuses = list(statuses or [])
        self.added = []
        self.changes = []
        self.paused = []
        self.removed = []
        self.results = []
        self.gid = "gid-1"

    def add_uri(self, uris, options):
        self.added.append((list(uris), dict(options)))
        return self.gid

    def tell_status(self, gid, keys=None):
        status = (
            self.statuses.pop(0) if self.statuses
            else {"status": "complete", "completedLength": "0", "errorCode": "0"}
        )
        if status.get("status") == "complete":
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(
                b"x" * int(status.get("completedLength") or 0)
            )
        return status

    def change_uri(self, gid, uris):
        self.changes.append((gid, list(uris)))
        return [0]

    def force_pause(self, gid):
        self.paused.append(gid)
        return gid

    def remove(self, gid):
        self.removed.append(gid)
        return gid

    def remove_download_result(self, gid):
        self.results.append(gid)
        return "OK"

    def shutdown(self):
        return "OK"


class FakeEngine:
    def __init__(self, rpc):
        self._rpc = rpc

    def rpc(self):
        return self._rpc


class Aria2ServiceTest(unittest.TestCase):
    def setUp(self):
        # 其它测试文件可能把 transfer/aria2 换成 MagicMock；这里恢复真实模块，
        # 因为本服务在 download_file 内才延迟导入 _LinkRefresher/_ProgressGate。
        sys.modules["cloudsubscribex.core.transfer"] = transfer_module
        sys.modules["cloudsubscribex.core.aria2"] = aria2_module
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        aria2_module.configure_aria2(
            enabled=True, connections=32, data_dir=Path(self._tmp.name) / "data"
        )
        self.addCleanup(lambda: aria2_module.configure_aria2(enabled=False))

    def _service(self, urls, rpc):
        self._urls = list(urls)
        self._resolver_calls = 0

        def resolver(_file):
            index = min(self._resolver_calls, len(self._urls) - 1)
            self._resolver_calls += 1
            return self._urls[index], {"Cookie": "c=1"}

        service = Aria2FileDownloadService(resolver, link_refresh_interval=0.0)
        service._engine = FakeEngine(rpc)
        service._poll_interval = 0
        return service

    def _target(self, size):
        path = Path(self._tmp.name) / "a.mkv"
        path.write_bytes(b"x" * size)
        path.with_name(path.name + ".aria2").write_text("control")
        return path

    def test_complete_returns_path_and_removes_control(self):
        target = self._target(8)
        rpc = FakeRpc(target, [
            {"status": "active", "completedLength": "4", "errorCode": "0"},
            {"status": "complete", "completedLength": "8", "errorCode": "0"},
        ])
        service = self._service(["http://example/a.mkv"], rpc)
        result = service.download_file(FakeFile(8), str(target))
        self.assertEqual(result, str(target))
        self.assertFalse(target.with_name(target.name + ".aria2").exists())
        options = rpc.added[0][1]
        self.assertEqual(options["split"], "32")
        self.assertEqual(options["max-connection-per-server"], "32")
        self.assertEqual(options["header"], ["Cookie: c=1", "Accept-Encoding: identity"])
        self.assertEqual(rpc.results, [rpc.gid])

    def test_size_mismatch_raises(self):
        target = self._target(8)
        rpc = FakeRpc(target, [
            {"status": "complete", "completedLength": "7", "errorCode": "0"},
        ])
        service = self._service(["http://example/a.mkv"], rpc)
        with self.assertRaises(RuntimeError):
            service.download_file(FakeFile(8), str(target))
        self.assertEqual(rpc.results, [rpc.gid])

    def test_authorization_error_readds_with_fresh_uri(self):
        """失败任务不能 changeUri（真实 aria2 会回 400），必须清掉重下。"""
        target = self._target(8)
        rpc = FakeRpc(target, [
            {"status": "error", "completedLength": "0", "errorCode": "24"},
            {"status": "active", "completedLength": "4", "errorCode": "0"},
            {"status": "complete", "completedLength": "8", "errorCode": "0"},
        ])
        service = self._service(
            ["http://example/old.mkv", "http://example/new.mkv"], rpc
        )
        result = service.download_file(FakeFile(8), str(target))
        self.assertEqual(result, str(target))
        self.assertEqual([uris for uris, _ in rpc.added],
                         [["http://example/old.mkv"], ["http://example/new.mkv"]])
        self.assertIn(rpc.gid, rpc.removed)
        self.assertEqual(rpc.changes, [])

    def test_stop_requests_pause_remove_and_preserve_partial(self):
        target = self._target(8)
        rpc = FakeRpc(target)
        service = self._service(["http://example/a.mkv"], rpc)
        with self.assertRaises(InterruptedError):
            service.download_file(
                FakeFile(8), str(target), stop_requested=lambda: True,
                preserve_partial=True,
            )
        self.assertEqual(rpc.paused, [rpc.gid])
        self.assertEqual(rpc.removed, [rpc.gid])
        self.assertIn(rpc.gid, rpc.results)
        self.assertTrue(target.exists())
        self.assertFalse(target.with_name(target.name + ".aria2").exists())

        target.write_bytes(b"x" * 8)
        target.with_name(target.name + ".aria2").write_text("control")
        rpc = FakeRpc(target)
        service = self._service(["http://example/a.mkv"], rpc)
        with self.assertRaises(InterruptedError):
            service.download_file(
                FakeFile(8), str(target), stop_requested=lambda: True,
                preserve_partial=False,
            )
        self.assertFalse(target.exists())

    def test_failure_always_removes_download_result(self):
        target = self._target(8)
        rpc = FakeRpc(target, [
            {"status": "error", "completedLength": "0", "errorCode": "24"},
            {"status": "error", "completedLength": "0", "errorCode": "24"},
            {"status": "error", "completedLength": "0", "errorCode": "24"},
        ])
        service = self._service(
            ["http://example/1.mkv", "http://example/2.mkv",
             "http://example/3.mkv"],
            rpc,
        )
        with self.assertRaises(RuntimeError):
            service.download_file(FakeFile(8), str(target))
        # 首次 + 两次换链重下
        self.assertEqual(len(rpc.added), 3)
        self.assertIn(rpc.gid, rpc.results)


if __name__ == "__main__":
    unittest.main()
