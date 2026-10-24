"""频道匹配加固回归（1.7.33）。

样本全部取自 2026-10-24 线上 `tg/items` 的真实消息，逐条锁住当时被误判的情形：

1. 「同名重复订阅」被判多候选 —— #68/#207 都是「Doctor X：白色黑手党时代」S01，
   5 条新番资源因此永久挂起；
2. 「标题 + 原名」只给 88 分（< 90 门槛）—— 某暗部的少女共栖 / FX战士久留美；
3. 归一化残留 —— 「伟大的长征60帧率」留下「率」、「金银岛AMZN单集4.8GB」留下
   「amzn单集48gb」，把精确匹配降成前缀匹配。
"""

import os
import unittest

from plugin_env import install_app_mocks, load_module, register_package

install_app_mocks()

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "plugins.v2", "cloudsubscribex")
register_package("cloudsubscribex", paths=[_PKG])
register_package("cloudsubscribex.core", paths=[os.path.join(_PKG, "core")])
register_package("cloudsubscribex.search", paths=[os.path.join(_PKG, "search")])

tg = load_module(
    "cloudsubscribex.core.tg_match",
    "plugins.v2/cloudsubscribex/core/tg_match.py",
)


def _sub(**kwargs):
    row = {"id": 1, "name": "", "season": 1, "type": "电视剧", "state": "R"}
    row.update(kwargs)
    return row


class NormalizeResidueTest(unittest.TestCase):
    """线上真实标题的归一化结果。"""

    def test_frame_rate_suffix_removed(self):
        parsed = tg.parse_message_title(
            "名称：伟大的长征 (2026) 4K HDR 高码率 60帧率 国语中字【更新EP22】"
        )
        self.assertEqual(parsed["normalized_title"], "伟大的长征")

    def test_platform_and_size_removed(self):
        parsed = tg.parse_message_title(
            "名称：金银岛 (2026) 2160p.AMZN.WEB-DL.DDP5.1.H.265 E01-E02 单集4.8GB"
        )
        self.assertEqual(parsed["normalized_title"], "金银岛")

    def test_douban_style_suffix_removed(self):
        parsed = tg.parse_message_title(
            "🎬 Doctor X：白色黑手党时代 (2026) 1080.SDR.NF高码版 "
            "内封精修简韩双语＆简中特效sup字幕"
        )
        self.assertEqual(parsed["normalized_title"], "doctorx白色黑手党时代")


class AliasSuffixScoreTest(unittest.TestCase):
    """原名/别名后缀 ≥90 分，可直接自动转存。"""

    def _score(self, title, sub_name, sub_season=None):
        parsed = tg.parse_message_title(title)
        sub = _sub(name=sub_name, season=sub_season if sub_season else 1)
        scored = tg.score_subscription(parsed, sub)
        return scored["score"] if scored else None

    def test_japanese_original_title_suffix(self):
        score = self._score(
            "名称：某暗部的少女共栖 とある暗部の少女共棲 (2026) [1080P] "
            "[内嵌繁中] [更至3集]",
            "某暗部的少女共栖",
        )
        self.assertGreaterEqual(score, 90)

    def test_katakana_suffix(self):
        score = self._score(
            "名称：FX战士久留美 FX戦士くるみちゃん (2026) [1080P] [内封简繁] [更至4集]",
            "FX战士久留美",
        )
        self.assertGreaterEqual(score, 90)

    def test_doctor_who_is_not_swallowed(self):
        """纯英文订阅名 + 独立英文单词后缀 → 保持 88，不自动吞掉。"""
        parsed = tg.parse_message_title("Doctor Who S01E05 1080p")
        scored = tg.score_subscription(parsed, _sub(name="Doctor", season=1))
        self.assertIsNotNone(scored)
        self.assertLess(scored["score"], 90)

    def test_plain_prefix_still_low(self):
        """无明确别名后缀的普通前缀仍停在 88，避免扩大误伤面。"""
        parsed = tg.parse_message_title("某某某 1080p 中字")
        scored = tg.score_subscription(parsed, _sub(name="某某", season=1))
        self.assertIsNotNone(scored)
        self.assertLess(scored["score"], 90)


class DuplicateSubscriptionTest(unittest.TestCase):
    """同名同季同类型的重复登记不再算多候选。"""

    def test_same_name_duplicates_pick_newer(self):
        subs = [
            _sub(id=68, name="Doctor X：白色黑手党时代", season=1),
            _sub(id=207, name="Doctor X：白色黑手党时代", season=1),
        ]
        result = tg.match_item(
            {"title": "名称：Doctor X：白色黑手党时代(2026)NF 1080p 内封简中 S01E01-E05"},
            subs,
        )
        self.assertIsNotNone(result["matched"], result["reason"])
        self.assertEqual(result["matched"]["id"], 207)

    def test_different_season_still_ambiguous(self):
        """季号不同是真歧义，必须保留多候选。"""
        deduped = tg.dedupe_duplicate_subscriptions([
            {"id": 68, "name": "Doctor X：白色黑手党时代", "season": 1,
             "type": "电视剧", "score": 100},
            {"id": 207, "name": "Doctor X：白色黑手党时代", "season": 1,
             "type": "电视剧", "score": 100},
            {"id": 99, "name": "Doctor X：白色黑手党时代", "season": 2,
             "type": "电视剧", "score": 100},
        ])
        self.assertEqual(sorted(row["id"] for row in deduped), [99, 207])

    def test_real_season_conflict_reported(self):
        """冰之城墙 S02 消息 vs S01 订阅 —— 保持拦截并说明原因。"""
        result = tg.match_item(
            {"title": "名称：冰之城墙 第2季 氷の城壁 第2期 (2026) [1080P] [更至4集]"},
            [_sub(id=73, name="冰之城墙", season=1)],
        )
        self.assertIsNone(result["matched"])
        self.assertIn("季号不符", result["reason"])


if __name__ == "__main__":
    unittest.main()
