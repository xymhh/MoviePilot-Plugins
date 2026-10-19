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


    def test_real_channel_titles_auto_match(self):
        """真实频道标题里的画质/演员残留不应把精确匹配降成前缀匹配。"""
        cases = [
            (
                "名称：玉簟秋(2026)web-4k H265 ACC简体字幕 第15集",
                {"id": 137, "name": "玉簟秋", "season": 1, "type": "电视剧"},
            ),
            (
                "名称：《玉簟秋》2026 更新至 - 第 15 集 - [1080p] [AAC 2.0] "
                "[丞磊 徐若晗 王丽坤] [民国 虐恋 家国]",
                {"id": 137, "name": "玉簟秋", "season": 1, "type": "电视剧"},
            ),
            (
                "名称：嫁金钗(2026)4k WEB-DL.DoVi 2160p 杜比视界 简体字幕 第10集",
                {"id": 158, "name": "嫁金钗", "season": 1, "type": "电视剧"},
            ),
        ]
        for title, sub in cases:
            parsed = matcher.parse_message_title(title)
            self.assertEqual(
                parsed["normalized_title"], matcher.normalize_title(sub["name"])
            )
            result = matcher.match_item({"title": title}, [sub], allowed_ids=[sub["id"]])
            self.assertIsNotNone(result["matched"], title)
            self.assertEqual(result["matched"]["id"], sub["id"])
            self.assertGreaterEqual(result["matched"]["score"], 90)

    def test_update_count_and_credits_are_stripped(self):
        """真实频道里的“更10集，演员/演员”不能残留成 88 分后缀。"""
        cases = [
            (
                "名称：伟大的长征(2026)4K 10bit 60FPS FLAC无损HiFi声 更至EP20",
                {"id": 10, "name": "伟大的长征", "season": 1, "type": "电视剧"},
            ),
            (
                "名称：伟大的长征‎ (2026) 4K SDR HDR 60帧 高码率 "
                "FLAC2.0+DDP2.0+HiFi 中字 更20集，于和伟/曹磊",
                {"id": 10, "name": "伟大的长征", "season": 1, "type": "电视剧"},
            ),
            (
                "名称：嫁金钗‎ (2026) 4K HDR SDR DV杜比视界 高码率 "
                "AAC2.0 中字 更10集，田曦薇/闫桉",
                {"id": 158, "name": "嫁金钗", "season": 1, "type": "电视剧"},
            ),
            (
                "名称：美人余‎ (2026) 4K SDR HDR DV杜比视界 高码率 "
                "DDP2.0+HiFi 中字 更23集，李一桐/王佳佳",
                {"id": 83, "name": "美人余", "season": 1, "type": "电视剧"},
            ),
            (
                "名称：把黑夜点燃‎ (2026) 4K HDR 高码率 DDP5.1+AAC5.1 "
                "中字 更08集，蒋欣/陈哲远",
                {"id": 184, "name": "把黑夜点燃", "season": 1, "type": "电视剧"},
            ),
        ]
        for title, sub in cases:
            parsed = matcher.parse_message_title(title)
            self.assertEqual(
                parsed["normalized_title"], matcher.normalize_title(sub["name"])
            )
            result = matcher.match_item({"title": title}, [sub], allowed_ids=[sub["id"]])
            self.assertIsNotNone(result["matched"], title)
            self.assertEqual(result["matched"]["id"], sub["id"])
            self.assertGreaterEqual(result["matched"]["score"], 90)

    def test_chinese_short_suffix_matches_actor_or_subtitle(self):
        cases = [
            ("美人余 剧情 李一桐 4KHDR60FPS 更新23集", 83, "美人余"),
            ("把黑夜点燃 陈家往事 (2026) 更08集", 184, "把黑夜点燃"),
            ("现在就出发 第四季 现在就出发4 (2026) 更新1018期", 113, "现在就出发"),
        ]
        for title, sub_id, name in cases:
            result = matcher.match_item(
                {"title": title},
                [{"id": sub_id, "name": name, "season": 4 if sub_id == 113 else 1,
                  "type": "电视剧"}],
                allowed_ids=[sub_id],
            )
            self.assertIsNotNone(result["matched"], title)
            self.assertEqual(result["matched"]["id"], sub_id)
            self.assertGreaterEqual(result["matched"]["score"], 90)

    def test_ambiguous_short_suffix_stays_pending(self):
        cases = [
            ("盗墓笔记 重启 (2026)", 1, "盗墓笔记"),
            ("鬼吹灯 之精绝古城 (2026)", 2, "鬼吹灯"),
            ("把黑夜点燃 番外 (2026)", 3, "把黑夜点燃"),
        ]
        for title, sub_id, name in cases:
            result = matcher.match_item(
                {"title": title},
                [{"id": sub_id, "name": name, "season": 1, "type": "电视剧"}],
                allowed_ids=[sub_id],
            )
            self.assertIsNone(result["matched"], title)
            self.assertIn("匹配度过低", result["reason"])

    def test_season_conflict_reports_reason(self):
        result = matcher.match_item(
            {"title": "名称：诛仙 第四季(2026)【更11集】【4K.SDR】"},
            [{"id": 200, "name": "诛仙", "season": 3, "type": "动漫"}],
            allowed_ids=[200],
        )
        self.assertIsNone(result["matched"])
        self.assertIn("季号不符", result["reason"])
        self.assertIn("S04", result["reason"])


if __name__ == "__main__":
    unittest.main()
