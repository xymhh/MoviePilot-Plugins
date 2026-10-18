import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
