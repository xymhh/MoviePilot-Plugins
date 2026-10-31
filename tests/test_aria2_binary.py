"""内嵌 aria2c 二进制落地：平台判定、压缩包缺失、校验失败、幂等不重写。"""

import hashlib
import lzma
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugin_env import load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

binary_module = load_module(
    "aria2_binary_under_test",
    "plugins.v2/cloudsubscribex/core/aria2/binary.py",
)


class Aria2BinaryTest(unittest.TestCase):
    def test_unsupported_platform(self):
        with TemporaryDirectory() as directory, patch.object(
                binary_module.platform, "machine", return_value="aarch64"
        ):
            path, reason = binary_module.ensure_binary(Path(directory))
        self.assertIsNone(path)
        self.assertEqual(reason, "unsupported-platform")

    def test_missing_archive(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.xz"
            with patch.object(binary_module, "_archive_path", return_value=missing):
                path, reason = binary_module.ensure_binary(Path(directory) / "bin")
        self.assertIsNone(path)
        self.assertEqual(reason, "archive-missing")

    def test_checksum_mismatch(self):
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "aria2c.xz"
            archive.write_bytes(lzma.compress(b"not the aria2 binary"))
            with patch.object(binary_module, "_archive_path", return_value=archive):
                path, reason = binary_module.ensure_binary(Path(directory) / "bin")
        self.assertIsNone(path)
        self.assertEqual(reason, "checksum-mismatch")

    def test_existing_binary_is_not_rewritten(self):
        payload = b"fake aria2 binary"
        digest = hashlib.sha256(payload).hexdigest()
        with TemporaryDirectory() as directory:
            target_dir = Path(directory) / "bin"
            target_dir.mkdir(parents=True)
            target = target_dir / "aria2c"
            target.write_bytes(payload)
            before = target.stat().st_mtime_ns

            class Probe:
                returncode = 0
                stdout = b"aria2 version 1.37.0"

            with patch.object(binary_module, "BINARY_SHA256", digest), \
                    patch.object(
                        binary_module, "_archive_path",
                        return_value=Path(directory) / "absent.xz",
                    ), \
                    patch.object(binary_module.lzma, "decompress") as decompress, \
                    patch.object(binary_module.subprocess, "run", return_value=Probe()):
                path, reason = binary_module.ensure_binary(target_dir)

            self.assertEqual((path, reason), (target, ""))
            self.assertEqual(target.stat().st_mtime_ns, before)
            decompress.assert_not_called()


if __name__ == "__main__":
    unittest.main()
