"""深度删除事件去重单测：同一目标在窗口内只放行一次。"""

import os
import sys
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

from plugin_env import ensure_package, install_app_mocks, load_module  # noqa: E402

install_app_mocks()
package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
utils_package = ensure_package("cloudsubscribex.utils")
utils_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "utils")]

module = load_module(
    "cloudsubscribex.utils.deep_delete_dedupe",
    "plugins.v2/cloudsubscribex/utils/deep_delete_dedupe.py",
)
DeepDeleteDeduplicator = module.DeepDeleteDeduplicator

TARGET = "/media/strm/115网盘/剧集/国漫/武神主宰 (2020) {tmdb-110181}"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DeepDeleteDeduplicatorTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.dedup = DeepDeleteDeduplicator(window_seconds=120, clock=self.clock)

    def test_same_target_only_accepted_once(self):
        fresh, dup = self.dedup.claim([TARGET])
        self.assertEqual(fresh, [TARGET])
        self.assertEqual(dup, [])

        fresh, dup = self.dedup.claim([TARGET])
        self.assertEqual(fresh, [])
        self.assertEqual(dup, [TARGET])

    def test_window_expiry_allows_retry(self):
        self.dedup.claim([TARGET])
        self.clock.now += 121
        fresh, dup = self.dedup.claim([TARGET])
        self.assertEqual(fresh, [TARGET])
        self.assertEqual(dup, [])

    def test_trailing_slash_is_same_target(self):
        self.dedup.claim([TARGET])
        fresh, dup = self.dedup.claim([TARGET + "/"])
        self.assertEqual(fresh, [])
        self.assertEqual(dup, [TARGET])

    def test_multiple_targets_partial_duplicate(self):
        other = TARGET + " 2"
        self.dedup.claim([TARGET])
        fresh, dup = self.dedup.claim([TARGET, other])
        self.assertEqual(fresh, [other])
        self.assertEqual(dup, [TARGET])

    def test_release_allows_immediate_retry(self):
        self.dedup.claim([TARGET])
        self.dedup.release([TARGET])
        fresh, dup = self.dedup.claim([TARGET])
        self.assertEqual(fresh, [TARGET])
        self.assertEqual(dup, [])

    def test_prune_drops_expired_entries(self):
        self.dedup.claim([TARGET])
        self.clock.now += 500
        self.assertEqual(self.dedup.pending(), 0)


if __name__ == "__main__":
    unittest.main()
