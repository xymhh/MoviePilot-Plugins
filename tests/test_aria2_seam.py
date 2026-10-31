"""HttpFileDownloadService 接缝：默认不走 aria2，启用后委派且只回落一次。"""

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
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)

aria2_module = sys.modules["cloudsubscribex.core.aria2"]
HttpFileDownloadService = transfer_module.HttpFileDownloadService
Aria2FileDownloadService = aria2_module.Aria2FileDownloadService


class FakeFile:
    def __init__(self, size, name="a.mkv"):
        self.id = "f1"
        self.name = name
        self.size = size
        self.sha1 = ""
        self.md5 = ""


class Aria2SeamTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: aria2_module.configure_aria2(enabled=False))
        self.target = Path(self._tmp.name) / "a.mkv"

    def _resolver(self, _file):
        return "http://example/a.mkv", {"Cookie": "c=1"}

    def test_disabled_has_no_delegate(self):
        aria2_module.configure_aria2(enabled=False)
        service = HttpFileDownloadService(self._resolver)
        self.assertIsNone(service._delegate)

    def test_enabled_delegates_without_builtin_download(self):
        aria2_module.configure_aria2(
            enabled=True, data_dir=Path(self._tmp.name) / "data"
        )
        service = HttpFileDownloadService(self._resolver)
        self.assertIsInstance(service._delegate, Aria2FileDownloadService)

        delegate = MagicMock()
        delegate.download_file.return_value = str(self.target)
        service._delegate = delegate
        service._download_parallel = MagicMock(return_value=str(self.target))
        result = service.download_file(FakeFile(32 * 1024 * 1024), str(self.target))
        self.assertEqual(result, str(self.target))
        delegate.download_file.assert_called_once()
        service._download_parallel.assert_not_called()

    def test_delegate_failure_falls_back_only_once(self):
        aria2_module.configure_aria2(
            enabled=True, data_dir=Path(self._tmp.name) / "data"
        )
        service = HttpFileDownloadService(self._resolver)
        delegate = MagicMock()
        delegate.download_file.side_effect = RuntimeError("boom")
        service._delegate = delegate
        service._download_parallel = MagicMock(return_value=str(self.target))

        first = service.download_file(FakeFile(32 * 1024 * 1024), str(self.target))
        second = service.download_file(FakeFile(32 * 1024 * 1024), str(self.target))

        self.assertEqual(first, str(self.target))
        self.assertEqual(second, str(self.target))
        self.assertEqual(delegate.download_file.call_count, 1)
        self.assertEqual(service._download_parallel.call_count, 2)
        self.assertIsNone(service._delegate)

    def test_delegate_cancel_propagates_without_fallback(self):
        """用户取消不能被当成失败回落重下：InterruptedError 必须原样上抛。"""
        aria2_module.configure_aria2(
            enabled=True, data_dir=Path(self._tmp.name) / "data"
        )
        service = HttpFileDownloadService(self._resolver)
        delegate = MagicMock()
        delegate.download_file.side_effect = InterruptedError
        service._delegate = delegate
        service._download_parallel = MagicMock(return_value=str(self.target))
        service._download_serial = MagicMock(return_value=str(self.target))

        with self.assertRaises(InterruptedError):
            service.download_file(FakeFile(32 * 1024 * 1024), str(self.target))

        service._download_parallel.assert_not_called()
        service._download_serial.assert_not_called()
        self.assertIsNotNone(service._delegate)


if __name__ == "__main__":
    unittest.main()
