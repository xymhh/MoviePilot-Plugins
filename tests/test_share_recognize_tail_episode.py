"""紧贴数字命名的兜底识别（黑色五叶草078.mp4 → 第 78 集）。"""

import os
import sys
import unittest

from plugin_env import ensure_package, install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
utils = ensure_package("cloudsubscribex.utils")
utils.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "utils")]
search = ensure_package("cloudsubscribex.search")
search.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "search")]

module = load_module(
    "cloudsubscribex.utils.share_recognize",
    "plugins.v2/cloudsubscribex/utils/share_recognize.py",
)
build_plan = module.build_plan
READY, REVIEW = module.READY, module.REVIEW
NO_EPISODES = module.NO_EPISODES


def entries(*names, size=800 * 1024 * 1024):
    return [{"path": n, "size": size, "is_dir": False} for n in names]


class TailEpisodeRescueTest(unittest.TestCase):
    def test_glued_numbers_rescued(self):
        names = [f"【全170集】黑色五叶草 (2017)/黑色五叶草{i:03d}.mp4" for i in range(1, 171)]
        plan = build_plan(entries(*names), work_id=73223, season=1, season_total=170)
        self.assertEqual(plan["status"], READY)
        self.assertEqual(len(plan["selected"]), 170)
        picked = [str(item) for item in plan["selected"]]
        self.assertTrue(any("黑色五叶草001.mp4" in p for p in picked))
        self.assertTrue(any("黑色五叶草170.mp4" in p for p in picked))

    def test_too_few_hits_not_trusted(self):
        plan = build_plan(
            entries("黑色五叶草078.mp4", "黑色五叶草079.mp4"),
            work_id=73223, season=1,
        )
        self.assertEqual(plan["status"], REVIEW)
        self.assertEqual(plan["reason"], NO_EPISODES)

    def test_year_like_number_not_rescued(self):
        plan = build_plan(entries("黑色五叶草2017.mp4"), work_id=73223, season=1)
        self.assertEqual(plan["reason"], NO_EPISODES)

    def test_version_suffix_not_mistaken(self):
        names = [f"黑色五叶草{i:03d}v2.mp4" for i in range(1, 21)]
        plan = build_plan(entries(*names), work_id=73223, season=1)
        self.assertEqual(plan["reason"], NO_EPISODES)

    def test_normal_naming_still_wins(self):
        names = [f"黑色五叶草 S01E{i:02d}.mp4" for i in range(1, 11)]
        plan = build_plan(entries(*names), work_id=73223, season=1, season_total=10)
        self.assertEqual(plan["status"], READY)
        self.assertEqual(len(plan["selected"]), 10)


if __name__ == "__main__":
    unittest.main()
