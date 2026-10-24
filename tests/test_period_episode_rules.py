"""期集换算规则：综艺「第N期上/下」→ 集号（可配置，不写死）。"""

import os
import sys
import unittest
from types import SimpleNamespace

from plugin_env import install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks("app.core.metainfo", "app.log")
install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.utils", "cloudsubscribex.search",
)

load_module(
    "cloudsubscribex.utils.file_parser",
    "plugins.v2/cloudsubscribex/utils/file_parser.py",
)
load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
FileMatcher = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
).FileMatcher


def _media(title, **kwargs):
    return SimpleNamespace(title=title, **kwargs)


class PeriodEpisodeOverrideTest(unittest.TestCase):
    def tearDown(self):
        FileMatcher.period_episode_rules = {}

    def test_no_rules_keeps_original_behaviour(self):
        FileMatcher.period_episode_rules = {}
        self.assertIsNone(
            FileMatcher.period_episode_override("第3期上.mp4", _media("现在就出发"))
        )

    def test_configured_programme_maps_period_and_segment(self):
        FileMatcher.period_episode_rules = {"现在就出发": 2}
        media = _media("现在就出发")
        cases = {
            "第1期上.mp4": 1,
            "第1期下.mp4": 2,
            "第2期上.mp4": 3,
            "第2期下.mp4": 4,
            "第3期上.mp4": 5,
            "第3期下.mp4": 6,
        }
        for name, expected in cases.items():
            self.assertEqual(
                FileMatcher.period_episode_override(name, media), expected, name
            )

    def test_period_without_segment_marker_is_first_half(self):
        FileMatcher.period_episode_rules = {"现在就出发": 2}
        self.assertEqual(
            FileMatcher.period_episode_override("第4期.mp4", _media("现在就出发")), 7
        )

    def test_one_episode_per_period_matches_period_number(self):
        FileMatcher.period_episode_rules = {"现在就出发": 1}
        self.assertEqual(
            FileMatcher.period_episode_override("第7期.mp4", _media("现在就出发")), 7
        )

    def test_other_programme_is_not_converted(self):
        FileMatcher.period_episode_rules = {"现在就出发": 2}
        self.assertIsNone(
            FileMatcher.period_episode_override("第3期上.mp4", _media("奔跑吧"))
        )

    def test_explicit_episode_marker_wins(self):
        FileMatcher.period_episode_rules = {"现在就出发": 2}
        self.assertIsNone(
            FileMatcher.period_episode_override(
                "现在就出发.S04E05.第3期上.mkv", _media("现在就出发")
            )
        )

    def test_rule_matching_ignores_season_and_year_noise(self):
        FileMatcher.period_episode_rules = {"现在就出发": 2}
        media = _media("现在就出发 (2023)", original_title="现在就出发 第四季")
        self.assertEqual(
            FileMatcher.period_episode_override("现在就出发 第四季 第3期上.mp4", media), 5
        )

    def test_unparseable_size_is_ignored(self):
        FileMatcher.period_episode_rules = {"现在就出发": 0}
        self.assertIsNone(
            FileMatcher.period_episode_override("第3期上.mp4", _media("现在就出发"))
        )

    def test_episode_from_file_honours_rule(self):
        """集成：episode_from_file 对配置节目返回换算后的集号。"""
        FileMatcher.period_episode_rules = {"现在就出发": 2}
        media = _media("现在就出发")
        episode = FileMatcher.episode_from_file(
            {"name": "第3期上.mp4"}, 4, media
        )
        self.assertEqual(episode, 5)


if __name__ == "__main__":
    unittest.main()
