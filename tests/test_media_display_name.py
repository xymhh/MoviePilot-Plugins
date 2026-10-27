"""深度删除通知里的媒体名：去 {tmdb-*} 标签（1.7.86）。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_mock

install_app_mocks("app", "app.log")
register_mock("app.log", logger=MagicMock())

_m = load_module(
    "cloudsubscribex.utils.deep_delete_match",
    "plugins.v2/cloudsubscribex/utils/deep_delete_match.py",
)


class MediaDisplayNameTest(unittest.TestCase):
    def test_strip_tmdb_tag(self):
        self.assertEqual(
            _m.media_display_name("/media/strm/115网盘/剧集/国漫/百日成王 (2026) {tmdb-326844}"),
            "百日成王 (2026)",
        )

    def test_strip_uppercase_and_trailing_slash(self):
        self.assertEqual(
            _m.media_display_name("武神主宰 (2020) {TMDB-110181}/"),
            "武神主宰 (2020)",
        )

    def test_keep_name_without_tag(self):
        self.assertEqual(_m.media_display_name("/strm/115网盘/剧集/国漫/仙逆 (2023)"), "仙逆 (2023)")

    def test_windows_separator(self):
        self.assertEqual(
            _m.media_display_name("\\\\media\\\\strm\\\\剧集\\\\牧神记 (2024) {tmdb-1}"),
            "牧神记 (2024)",
        )

    def test_empty_and_none(self):
        self.assertEqual(_m.media_display_name(""), "")
        self.assertEqual(_m.media_display_name(None), "")

    def test_tag_only_name_degrades_to_empty_not_crash(self):
        self.assertEqual(_m.strip_media_tag("{tmdb-123}"), "")


if __name__ == "__main__":
    unittest.main()
