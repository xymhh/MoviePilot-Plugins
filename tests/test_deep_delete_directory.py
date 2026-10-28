"""深度删除联动：目录级（整剧/整季）目标应命中其下所有历史记录。"""

import os
import sys
import unittest

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

from plugin_env import load_module  # noqa: E402

match_module = load_module(
    "cloudsubscribex.utils.deep_delete_match",
    "plugins.v2/cloudsubscribex/utils/deep_delete_match.py",
)
match = match_module.match_history_record_to_targets


def _targets(paths):
    filenames = set()
    stems = set()
    normalized = []
    for path in paths:
        clean = str(path).strip().replace("\\", "/").rstrip("/")
        normalized.append(clean)
        filenames.add(os.path.basename(clean).casefold())
        stems.add(os.path.splitext(os.path.basename(clean))[0].casefold())
    return filenames, stems, normalized


class DeepDeleteDirectoryMatchTest(unittest.TestCase):
    def test_series_directory_target_matches_episode(self):
        filenames, stems, normalized = _targets(
            ["/media/strm/115网盘/剧集/国产剧/尚公主 (2026) {tmdb-286171}"]
        )
        self.assertTrue(
            match(
                "尚公主 - S01E01.mkv",
                "/media/strm/115网盘/剧集/国产剧/尚公主 (2026) {tmdb-286171}"
                "/Season 1/尚公主 - S01E01.strm",
                filenames, stems, normalized,
            )
        )

    def test_series_directory_target_matches_all_episodes(self):
        filenames, stems, normalized = _targets(
            ["/media/strm/115网盘/剧集/国产剧/尚公主 (2026) {tmdb-286171}"]
        )
        for episode in ("S01E01", "S01E02", "S02E10"):
            self.assertTrue(
                match(
                    f"尚公主 - {episode}.mkv",
                    "/media/strm/115网盘/剧集/国产剧/尚公主 (2026) {tmdb-286171}"
                    f"/Season {episode[1:3].lstrip('0')}/尚公主 - {episode}.strm",
                    filenames, stems, normalized,
                )
            )

    def test_unrelated_series_not_matched(self):
        filenames, stems, normalized = _targets(
            ["/media/strm/115网盘/剧集/国产剧/尚公主 (2026) {tmdb-286171}"]
        )
        self.assertFalse(
            match(
                "别的剧 - S01E01.mkv",
                "/media/strm/115网盘/剧集/国产剧/别的剧 (2025) {tmdb-1}"
                "/Season 1/别的剧 - S01E01.strm",
                filenames, stems, normalized,
            )
        )

    def test_similar_prefix_directory_not_matched(self):
        """尚公主2 不能因为前缀相同被误删。"""
        filenames, stems, normalized = _targets(
            ["/media/strm/115网盘/剧集/国产剧/尚公主 (2026) {tmdb-286171}"]
        )
        self.assertFalse(
            match(
                "尚公主2 - S01E01.mkv",
                "/media/strm/115网盘/剧集/国产剧/尚公主2 (2027) {tmdb-2}"
                "/Season 1/尚公主2 - S01E01.strm",
                filenames, stems, normalized,
            )
        )

    def test_single_episode_file_target_still_matches(self):
        filenames, stems, normalized = _targets(
            [
                "/media/strm/115网盘/剧集/国漫/弹珠传说 (2010) {tmdb-155953}"
                "/Season 1/弹珠传说 - S01E01.strm"
            ]
        )
        self.assertTrue(
            match(
                "弹珠传说 - S01E01.mkv",
                "/media/strm/115网盘/剧集/国漫/弹珠传说 (2010) {tmdb-155953}"
                "/Season 1/弹珠传说 - S01E01.strm",
                filenames, stems, normalized,
            )
        )


if __name__ == "__main__":
    unittest.main()
