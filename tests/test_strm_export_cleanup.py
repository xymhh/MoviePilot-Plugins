"""导出目录树残留清理单测：只删超龄的 `*_目录树.txt`，且不影响导出主流程。"""

import importlib.util
import os
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import ensure_package

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")

ensure_package("app")
ensure_package("app.log").logger = MagicMock()

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
drive_package = ensure_package("cloudsubscribex.drive")
drive_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "drive")]
ensure_package("cloudsubscribex.drive.common").DriveRateLimiter = object
ensure_package("cloudsubscribex.drive.p115").__path__ = [
    os.path.join(plugins_v2_path, "cloudsubscribex", "drive", "p115")
]

module_name = "cloudsubscribex.drive.p115.strm_export"
if module_name not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        module_name,
        os.path.join(plugins_v2_path, "cloudsubscribex", "drive", "p115", "strm_export.py"),
    )
    export_module = importlib.util.module_from_spec(spec)
    export_module.__package__ = "cloudsubscribex.drive.p115"
    sys.modules[module_name] = export_module
    spec.loader.exec_module(export_module)
else:
    export_module = sys.modules[module_name]


class _FakeClient:
    def __init__(self, files):
        self.files = files
        self.deleted = []
        self.listed = []

    def fs_files(self, payload):
        self.listed.append(payload)
        return {"state": True, "data": list(self.files)}

    def fs_delete(self, payload):
        self.deleted.append(payload)
        return {"state": True}


def _call(func, *args, **kwargs):
    return func(*args, **kwargs)


def _entry(name, fid, mtime):
    return {"n": name, "fid": fid, "te": mtime}


class CleanupStaleExportsTest(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def test_deletes_only_stale_tree_files(self):
        client = _FakeClient([
            _entry("moviepilot20261026080553_目录树.txt", 111, self.now - 3 * 3600),
            _entry("moviepilot20261026060532_目录树.txt", 222, self.now - 5 * 3600),
            _entry("moviepilot20261026120500_目录树.txt", 333, self.now - 60),
            _entry("某个视频.mkv", 444, self.now - 9 * 3600),
            _entry("Alist", 555, self.now - 9 * 3600),
        ])
        export_module.P115StrmExport._cleanup_stale_exports(client, _call)

        self.assertEqual(client.deleted, [[111, 222]])
        self.assertEqual(client.listed[0]["cid"], 0)

    def test_keeps_recent_export_to_avoid_race(self):
        client = _FakeClient([
            _entry("moviepilot20261026120500_目录树.txt", 333, self.now - 120),
        ])
        export_module.P115StrmExport._cleanup_stale_exports(client, _call)
        self.assertEqual(client.deleted, [])

    def test_missing_mtime_is_treated_as_stale(self):
        client = _FakeClient([
            {"n": "moviepilot_目录树.txt", "fid": 777},
        ])
        export_module.P115StrmExport._cleanup_stale_exports(client, _call)
        self.assertEqual(client.deleted, [[777]])

    def test_cleanup_failure_is_swallowed(self):
        client = _FakeClient([])
        client.fs_files = MagicMock(side_effect=RuntimeError("boom"))
        # 不抛异常即通过
        export_module.P115StrmExport._cleanup_stale_exports(client, _call)
        self.assertEqual(client.deleted, [])


class ExportCallsCleanupTest(unittest.TestCase):
    def test_cleanup_runs_before_export_start(self):
        order = []
        client = _FakeClient([])
        client.fs_files = MagicMock(
            side_effect=lambda payload: order.append("list") or {"data": []},
        )

        fake_export = types.ModuleType("p115client.tool.export_dir")
        fake_export.export_dir_start = MagicMock(
            side_effect=lambda *a, **k: order.append("start") or 42,
        )
        fake_export.export_dir_status = MagicMock(return_value={"file_id": 999})
        fake_export.export_dir_parse_iter = MagicMock(
            return_value=iter(["/某剧/E01.mkv"]),
        )
        p115_client = types.ModuleType("p115client")
        p115_tool = types.ModuleType("p115client.tool")
        p115_client.__path__ = []
        p115_tool.__path__ = []
        p115_tool.export_dir = fake_export
        p115_client.tool = p115_tool
        stubs = {
            "p115client": p115_client,
            "p115client.tool": p115_tool,
            "p115client.tool.export_dir": fake_export,
        }

        with patch.dict(sys.modules, stubs):
            manager = MagicMock()
            manager.client = client
            manager.rate_limiter = None
            exporter = export_module.P115StrmExport(manager)
            paths = list(exporter.export_directory_tree("123", timeout_seconds=5))

        self.assertEqual(paths, ["/某剧/E01.mkv"])
        self.assertEqual(order[0], "list")
        self.assertEqual(order[1], "start")


if __name__ == "__main__":
    unittest.main()
