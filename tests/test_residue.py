import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from plugin_env import ensure_package, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]

residue_module = load_module(
    "cloudsubscribex.core.residue",
    "plugins.v2/cloudsubscribex/core/residue.py",
)
scan_residue = residue_module.scan_residue

KEY = "cloud-transfer-" + "a" * 32
OTHER = "cloud-transfer-" + "b" * 32
ACTIVE = "cloud-transfer-" + "c" * 32


class ResidueScanTest(unittest.TestCase):
    def _touch(self, root: Path, name: str, age_days: float, size: int = 10) -> Path:
        path = root / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_only_expired_orphans_are_deleted(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_bin = self._touch(root, f"{KEY}.bin", 3, 100)
            old_meta = self._touch(root, f"{KEY}.bin.meta.json", 3, 5)
            dup_bin = self._touch(root, f"{KEY} (1).bin", 3, 50)
            self._touch(root, f"{OTHER}.bin", 0.1, 20)
            active = self._touch(root, f"{ACTIVE}.bin", 3, 20)
            keep = self._touch(root, "movie.mkv", 5, 20)

            result = scan_residue(root, active_names=[f"{ACTIVE}.bin"])

            self.assertEqual(result["deleted_files"], 3)
            self.assertEqual(result["freed_bytes"], 155)
            self.assertEqual(result["skipped_recent"], 1)
            self.assertEqual(result["skipped_active"], 1)
            self.assertFalse(old_bin.exists())
            self.assertFalse(old_meta.exists())
            self.assertFalse(dup_bin.exists())
            self.assertTrue(active.exists())
            self.assertTrue(keep.exists())
            self.assertEqual(result["errors"], [])

    def test_missing_dir_is_reported(self):
        result = scan_residue("/nonexistent/for/test")
        self.assertEqual(result["deleted_files"], 0)
        self.assertTrue(result["errors"])

    def test_unrelated_names_never_match(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            decoy = self._touch(root, "cloud-transfer-not-a-hash.bin", 5)
            short = self._touch(root, "cloud-transfer-abc.bin", 5)
            result = scan_residue(root, now=time.time())
            self.assertEqual(result["deleted_files"], 0)
            self.assertTrue(decoy.exists())
            self.assertTrue(short.exists())

    def test_default_retention_is_six_hours(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            expired = self._touch(root, f"{KEY}.bin", 7 / 24)
            fresh = self._touch(root, f"{OTHER}.bin", 5 / 24)

            result = scan_residue(root, now=time.time())

            self.assertFalse(expired.exists())
            self.assertTrue(fresh.exists())
            self.assertEqual(result["deleted_files"], 1)
            self.assertEqual(result["skipped_recent"], 1)


class _Usage:
    def __init__(self, total: int, free: int):
        self.total = total
        self.free = free
        self.used = total - free


class ResidueDiskPressureTest(unittest.TestCase):
    def _touch(self, root: Path, name: str, age_hours: float, size: int = 10) -> Path:
        path = root / name
        path.write_bytes(b"x" * size)
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    def test_pressure_deletes_oldest_first_until_target(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            oldest = self._touch(root, f"{KEY}.bin", 4, 100)
            newer = self._touch(root, f"{OTHER}.bin", 2, 200)
            # 第一次读数低于阈值；删掉最老一组后回到阈值以上
            usages = [_Usage(1000, 50), _Usage(1000, 150)]

            with mock.patch.object(
                    residue_module.shutil, "disk_usage",
                    side_effect=lambda *a, **k: usages.pop(0)):
                result = scan_residue(
                    root,
                    now=time.time(),
                    disk_free_ratio=0.10,
                    protect_recent_seconds=0,
                )

            self.assertFalse(oldest.exists())
            self.assertTrue(newer.exists())
            self.assertEqual(result["pressure_deleted_files"], 1)
            self.assertEqual(result["pressure_freed_bytes"], 100)
            self.assertEqual(result["deleted_files"], 1)

    def test_pressure_keeps_freshly_written_groups(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloading = self._touch(root, f"{KEY}.bin", 0.05, 500)
            stale = self._touch(root, f"{OTHER}.bin", 3, 100)
            usages = [_Usage(1000, 10), _Usage(1000, 500)]

            with mock.patch.object(
                    residue_module.shutil, "disk_usage",
                    side_effect=lambda *a, **k: usages.pop(0)):
                result = scan_residue(
                    root,
                    now=time.time(),
                    disk_free_ratio=0.10,
                    protect_recent_seconds=600,
                )

            self.assertTrue(downloading.exists())
            self.assertFalse(stale.exists())
            self.assertEqual(result["skipped_protected"], 1)

    def test_pressure_disabled_keeps_unexpired_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._touch(root, f"{KEY}.bin", 1, 100)
            with mock.patch.object(
                    residue_module.shutil, "disk_usage",
                    return_value=_Usage(1000, 10)):
                result = scan_residue(root, now=time.time(), disk_free_ratio=0)

            self.assertTrue(path.exists())
            self.assertEqual(result["pressure_deleted_files"], 0)


if __name__ == "__main__":
    unittest.main()
