"""标题季号拆解回归测试（1.9.65）。

背景：追更抽屉默认关键词是「剧名 S01」、按集搜是「剧名 S01E07」，
后端把它当纯标题再拼一次季号，最终关键词变成「谷雨 S01 (2026) S01」，
线上实测 PanSou / SeedHub 都返回 0 条。
"""

import os
import unittest

from plugin_env import load_module

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_title_parse = load_module(
    "cs_title_parse",
    os.path.join(_project_root, "plugins.v2/cloudsubscribex/core/api/title_parse.py"),
)


class SplitTitleSeasonTest(unittest.TestCase):
    def test_season_suffix(self):
        self.assertEqual(_title_parse.split_title_season("谷雨 S01"), ("谷雨", 1))
        self.assertEqual(_title_parse.split_title_season("谷雨 s1"), ("谷雨", 1))

    def test_episode_suffix(self):
        self.assertEqual(_title_parse.split_title_season("谷雨 S01E07"), ("谷雨", 1))
        self.assertEqual(_title_parse.split_title_season("谷雨 S01E7"), ("谷雨", 1))

    def test_chinese_season(self):
        self.assertEqual(_title_parse.split_title_season("谷雨 第2季"), ("谷雨", 2))
        self.assertEqual(_title_parse.split_title_season("谷雨 Season 3"), ("谷雨", 3))

    def test_plain_title_untouched(self):
        self.assertEqual(_title_parse.split_title_season("谷雨"), ("谷雨", None))
        self.assertEqual(_title_parse.split_title_season("S.W.A.T."), ("S.W.A.T.", None))

    def test_year_not_stripped(self):
        self.assertEqual(
            _title_parse.split_title_season("流浪地球 2019"), ("流浪地球 2019", None)
        )

    def test_only_token_kept(self):
        # 整个标题就是个季号时不清空，避免拿空标题去搜
        self.assertEqual(_title_parse.split_title_season("S01"), ("S01", None))


if __name__ == "__main__":
    unittest.main()
