"""aria2 工作目录与日志：跟随跨盘缓存目录、日志限级并在启动前轮转。"""

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
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))

import importlib  # noqa: E402 - 依赖上面的包路径注册

aria2_module = importlib.import_module("cloudsubscribex.core.aria2")
engine_module = importlib.import_module("cloudsubscribex.core.aria2.engine")
residue_module = load_module(
    "cloudsubscribex.core.residue",
    "plugins.v2/cloudsubscribex/core/residue.py",
)

resolve_data_dir = aria2_module.resolve_data_dir
resolve_roots = residue_module.resolve_roots
Aria2Settings = aria2_module.Aria2Settings
Aria2Engine = engine_module.Aria2Engine


class ResolveDataDirTest(unittest.TestCase):
    def test_follows_cross_transfer_directory(self):
        data_dir = resolve_data_dir("/downloads", "/config/plugins/x")
        self.assertEqual(Path("/downloads/.aria2"), data_dir)

    def test_falls_back_to_plugin_data_dir(self):
        for empty in ("", "   ", None):
            with self.subTest(empty=empty):
                self.assertEqual(
                    Path("/config/plugins/x"),
                    resolve_data_dir(empty, "/config/plugins/x"),
                )

    def test_falls_back_to_tmp_without_plugin_data_dir(self):
        self.assertEqual(
            Path("/tmp/cloudsubscribex-aria2"), resolve_data_dir("", "")
        )


class BuildCommandTest(unittest.TestCase):
    def _command(self, data_dir):
        settings = Aria2Settings(enabled=True, data_dir=Path(data_dir))
        engine = Aria2Engine(settings)
        return engine._build_command(
            Path(data_dir) / "bin" / "aria2c", 6800, "secret", Path(data_dir)
        )

    def test_log_level_is_warning(self):
        command = self._command("/downloads/.aria2")
        self.assertIn("--log-level=warn", command)

    def test_dir_points_at_aria2_data_dir(self):
        command = self._command("/downloads/.aria2")
        self.assertIn("--dir=/downloads/.aria2", command)
        self.assertIn("--log=/downloads/.aria2/aria2.log", command)


class RotateLogTest(unittest.TestCase):
    def test_under_limit_kept(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "aria2.log"
            log.write_bytes(b"x" * 1024)
            Aria2Engine._rotate_logs(Path(tmp))
            self.assertTrue(log.exists())
            self.assertFalse((Path(tmp) / "aria2.log.1").exists())

    def test_over_limit_rotated(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "aria2.log"
            log.write_bytes(b"x" * 1024)
            with unittest.mock.patch.object(Aria2Engine, "LOG_LIMIT_BYTES", 512):
                Aria2Engine._rotate_logs(Path(tmp))
            self.assertFalse(log.exists())
            self.assertEqual(1024, (Path(tmp) / "aria2.log.1").stat().st_size)

    def test_rotation_replaces_previous_backup(self):
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "aria2.log"
            backup = Path(tmp) / "aria2.log.1"
            log.write_bytes(b"y" * 1024)
            backup.write_bytes(b"z" * 10)
            with unittest.mock.patch.object(Aria2Engine, "LOG_LIMIT_BYTES", 512):
                Aria2Engine._rotate_logs(Path(tmp))
            self.assertFalse(log.exists())
            self.assertEqual(1024, backup.stat().st_size)

    def test_missing_log_is_noop(self):
        with TemporaryDirectory() as tmp:
            Aria2Engine._rotate_logs(Path(tmp))
            self.assertEqual([], [p.name for p in Path(tmp).iterdir()])


class ResolveRootsTest(unittest.TestCase):
    def test_dedupes_and_skips_empty(self):
        with TemporaryDirectory() as tmp:
            nested = Path(tmp) / "cache"
            nested.mkdir()
            roots = resolve_roots(
                str(nested), "", None, str(nested) + "/", str(Path(tmp) / "other")
            )
            self.assertEqual([str(nested), str(Path(tmp) / "other")], roots)

    def test_resolve_handles_dot_segments(self):
        with TemporaryDirectory() as tmp:
            nested = Path(tmp) / "cache"
            nested.mkdir()
            dotted = str(nested) + "/./"
            self.assertEqual([dotted], resolve_roots(dotted, str(nested)))


if __name__ == "__main__":
    unittest.main()
