"""1.15.0 Phase 3a 第 7 项：季集解析合一。

``parse_season_episode(stem, custom_words)`` 是四处 MetaInfo 季集解析的
唯一入口：MetaInfo 口径（``episode_list``，无则 ``begin_episode``）+
share_recognize 单文件兜底（开头数字 + 画质词，如“14 DV.mkv → E14”）。
"""
import os
import sys
import unittest
from types import SimpleNamespace

from plugin_env import ensure_package, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

ensure_package("cloudsubscribex")
ensure_package("cloudsubscribex.core")
ensure_package("cloudsubscribex.utils")
episode_parse = load_module(
    "cloudsubscribex.core.episode_parse",
    "plugins.v2/cloudsubscribex/core/episode_parse.py",
)
parse_season_episode = episode_parse.parse_season_episode


def _meta(begin_season=None, episode_list=None, begin_episode=None):
    return SimpleNamespace(
        begin_season=begin_season,
        episode_list=list(episode_list or []),
        begin_episode=begin_episode,
    )


class ParseSeasonEpisodeTests(unittest.TestCase):
    def test_metainfo_episode_list(self):
        season, episodes = parse_season_episode(
            "凡人修仙传 - S01E200 - 第200集",
            meta_factory=lambda stem, cw: _meta(1, [200]),
        )
        self.assertEqual((season, episodes), (1, [200]))

    def test_metainfo_begin_episode_fallback(self):
        season, episodes = parse_season_episode(
            "第 5 集", meta_factory=lambda stem, cw: _meta(1, [], 5)
        )
        self.assertEqual((season, episodes), (1, [5]))

    def test_metainfo_season_missing_uses_default(self):
        season, episodes = parse_season_episode(
            "E07", season=2, meta_factory=lambda stem, cw: _meta(None, [7])
        )
        self.assertEqual((season, episodes), (2, [7]))

    def test_empty_stem(self):
        self.assertEqual(
            parse_season_episode("  ", meta_factory=lambda stem, cw: _meta()),
            (None, []),
        )

    def test_fallback_single_file_rule(self):
        # MetaInfo 认不出时走单文件兜底：开头数字 + 画质词。
        season, episodes = parse_season_episode(
            "14 DV",
            season=1,
            meta_factory=lambda stem, cw: _meta(1, []),
            episode_fallback=lambda name, season: 14,
        )
        self.assertEqual((season, episodes), (1, [14]))

    def test_no_fallback_when_metainfo_found(self):
        calls = []

        def fallback(name, season):
            calls.append(name)
            return 99

        season, episodes = parse_season_episode(
            "S01E03",
            meta_factory=lambda stem, cw: _meta(1, [3]),
            episode_fallback=fallback,
        )
        self.assertEqual((season, episodes), (1, [3]))
        self.assertEqual(calls, [])

    def test_episode_zero_filtered(self):
        season, episodes = parse_season_episode(
            "x", meta_factory=lambda stem, cw: _meta(1, [0, 3])
        )
        self.assertEqual((season, episodes), (1, [3]))

    def test_meta_factory_exception_falls_back(self):
        def boom(stem, cw):
            raise RuntimeError("no metainfo")

        season, episodes = parse_season_episode(
            "14 DV",
            season=1,
            meta_factory=boom,
            episode_fallback=lambda name, season: 14,
        )
        self.assertEqual((season, episodes), (1, [14]))


if __name__ == "__main__":
    unittest.main()
