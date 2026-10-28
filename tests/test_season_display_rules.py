"""季号展示规则：官方季优先，媒体库残留季不展示（1.7.81）。

复现问题：TMDB 只有第 1 季 260 集的《吞噬星空》，插件详情页却显示第 1-5 季、
缺 217 集。根因是媒体库快照里残留了第 2-5 季（早期按绝对集号拆分），
而旧实现把「官方季」与「媒体库季」并集后全部展示。
"""

import unittest

from plugin_env import load_module

_rules = load_module(
    "cloudsubscribex.core.api.season_rules",
    "plugins.v2/cloudsubscribex/core/api/season_rules.py",
)

_select = _rules._select_season_numbers
_total = _rules._season_total_episodes


class SelectSeasonNumbersTest(unittest.TestCase):
    def test_official_seasons_win_over_library_leftovers(self):
        self.assertEqual(
            _select({1: 260}, {1: set(range(1, 44)), 2: set(range(1, 27)),
                                3: set(range(1, 34)), 4: set(range(1, 121)),
                                5: {30}}),
            [1],
        )

    def test_falls_back_to_library_when_official_missing(self):
        self.assertEqual(_select({}, {1: {1, 2}, 2: {1}}), [1, 2])

    def test_keeps_multiple_official_seasons(self):
        self.assertEqual(_select({1: 10, 2: 8}, {1: {1}, 9: {1}}), [1, 2])

    def test_empty_inputs(self):
        self.assertEqual(_select({}, {}), [])


class SeasonTotalEpisodesTest(unittest.TestCase):
    def test_official_count_wins(self):
        self.assertEqual(_total(260, {1, 2, 180}), 260)

    def test_library_max_when_official_missing(self):
        self.assertEqual(_total(0, {1, 2, 33}), 33)

    def test_no_data(self):
        self.assertEqual(_total(None, set()), 0)


if __name__ == "__main__":
    unittest.main()
