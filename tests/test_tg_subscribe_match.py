"""TG 频道消息 → 订阅白名单匹配单测。"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATCHER_PATH = os.path.join(
    ROOT, "plugins.v2", "cloudsubscribex", "core", "tg_match.py",
)
spec = importlib.util.spec_from_file_location("tg_match_under_test", MATCHER_PATH)
matcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matcher)


class TgMatchTest(unittest.TestCase):
    def test_variety_title_season_episode(self):
        parsed = matcher.parse_message_title(
            "名称：现在就出发 第四季(2026) 第4集 [旅行/综艺]"
        )
        self.assertEqual(parsed["title"], "现在就出发")
        self.assertEqual(parsed["season"], 4)
        self.assertEqual(parsed["episode"], 4)
        self.assertEqual(parsed["year"], 2026)
        self.assertEqual(parsed["media_type"], "tv")

    def test_chinese_episode_range(self):
        parsed = matcher.parse_message_title("🎬 嫁金钗 (2026) 第1-8集")
        self.assertEqual(parsed["title"], "嫁金钗")
        self.assertEqual(parsed["episode"], 1)

    def test_bare_episode_range(self):
        parsed = matcher.parse_message_title("🎬 嫁金钗 (2026) 1-8")
        self.assertEqual(parsed["title"], "嫁金钗")

    def test_ddp_audio_suffix_stripped(self):
        parsed = matcher.parse_message_title(
            "📺 美人余 (2026) S01E20 ✨4K WEB-DL DDP 5 1"
        )
        self.assertEqual(parsed["title"], "美人余")
        self.assertEqual(parsed["season"], 1)
        self.assertEqual(parsed["episode"], 20)

    def test_english_release_title(self):
        parsed = matcher.parse_message_title("Some.Show.S02E03.1080p.WEB-DL")
        self.assertEqual(parsed["normalized_title"], "someshow")
        self.assertEqual(parsed["season"], 2)
        self.assertEqual(parsed["episode"], 3)

    def test_exact_title_and_season_matches(self):
        result = matcher.match_item(
            {"title": "现在就出发 第四季(2026) 第4集 [旅行/综艺]"},
            [{"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"}],
            allowed_ids=[113],
        )
        self.assertEqual(result["matched"]["id"], 113)
        self.assertIn("S04", result["matched"]["reason"])

    def test_season_mismatch_rejected(self):
        result = matcher.match_item(
            {"title": "现在就出发 第三季 第4集"},
            [{"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"}],
            allowed_ids=[113],
        )
        self.assertIsNone(result["matched"])
        self.assertEqual(result["candidates"], [])

    def test_type_mismatch_rejected(self):
        result = matcher.match_item(
            {"title": "沙丘2 (2024) [电影]"},
            [{"id": 1, "name": "沙丘2", "season": 1, "type": "电视剧"}],
            allowed_ids=[1],
        )
        self.assertIsNone(result["matched"])

    def test_movie_year_mismatch_rejected(self):
        result = matcher.match_item(
            {"title": "沙丘2 (2024) [电影]"},
            [{"id": 1, "name": "沙丘2", "season": 0, "type": "电影", "year": "2020"}],
            allowed_ids=[1],
        )
        self.assertIsNone(result["matched"])

    def test_multiple_seasons_without_item_season_pending(self):
        result = matcher.match_item(
            {"title": "现在就出发 第4集"},
            [
                {"id": 1, "name": "现在就出发", "season": 3, "type": "电视剧"},
                {"id": 2, "name": "现在就出发", "season": 4, "type": "电视剧"},
            ],
            allowed_ids=[1, 2],
        )
        self.assertIsNone(result["matched"])
        self.assertIn("多个相近订阅", result["reason"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_empty_whitelist_matches_nothing(self):
        result = matcher.match_item(
            {"title": "现在就出发 第四季"},
            [{"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"}],
            allowed_ids=[],
        )
        self.assertIsNone(result["matched"])
        self.assertEqual(result["candidates"], [])

    def test_prefix_is_candidate_not_auto_match(self):
        result = matcher.match_item(
            {"title": "Doctor Who S01E01"},
            [{"id": 1, "name": "Doctor", "season": 1, "type": "电视剧"}],
            allowed_ids=[1],
        )
        self.assertIsNone(result["matched"])
        self.assertEqual(result["candidates"][0]["id"], 1)
        self.assertIn("匹配度过低", result["reason"])

    def test_whitelist_excludes_other_subscriptions(self):
        result = matcher.match_item(
            {"title": "沙丘2 (2024) [电影]"},
            [
                {"id": 1, "name": "沙丘2", "season": 0, "type": "电影", "year": "2024"},
                {"id": 2, "name": "沙丘2", "season": 0, "type": "电影", "year": "2024"},
            ],
            allowed_ids=[1],
        )
        self.assertEqual(result["matched"]["id"], 1)


if __name__ == "__main__":
    unittest.main()
