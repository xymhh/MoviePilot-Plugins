"""按体积/分辨率择优入库的比较器单测（1.7.88）。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_mock

install_app_mocks("app", "app.log")
register_mock("app.log", logger=MagicMock())

_q = load_module(
    "cloudsubscribex.utils.quality_compare",
    "plugins.v2/cloudsubscribex/utils/quality_compare.py",
)

MB = 1024 * 1024
GB = 1024 * MB


class ParseResolutionTest(unittest.TestCase):
    def test_common_tokens(self):
        cases = {
            "某剧.S01E01.2160p.WEB-DL.mkv": 2160,
            "某剧.S01E01.1080p.mkv": 1080,
            "某剧.S01E01.720P.mkv": 720,
            "某剧.S01E01.4K.HDR.mkv": 2160,
            "某剧.S01E01.UHD.mkv": 2160,
            "某剧.S01E01.8K.mkv": 4320,
            "某剧.S01E01.FHD.mkv": 1080,
        }
        for name, expected in cases.items():
            self.assertEqual(_q.parse_resolution(name), expected, name)

    def test_no_resolution(self):
        self.assertEqual(_q.parse_resolution("某剧.S01E01.mkv"), 0)
        self.assertEqual(_q.parse_resolution(""), 0)
        self.assertEqual(_q.parse_resolution(None), 0)

    def test_digits_inside_words_do_not_match(self):
        # 1080i 之外的数字串（如编码器 id/年份）不应被当成分辨率
        self.assertEqual(_q.parse_resolution("某剧.2024.x264-GROUP.mkv"), 0)


class CompareQualityTest(unittest.TestCase):
    def test_resolution_upgrade(self):
        action, reason = _q.compare_quality(
            "某剧.S01E01.2160p.mkv", 2 * GB, [("某剧.S01E01.1080p.mkv", 1 * GB)]
        )
        self.assertEqual(action, "upgrade")
        self.assertIn("1080p → 2160p", reason)

    def test_resolution_downgrade_never_replaces(self):
        action, reason = _q.compare_quality(
            "某剧.S01E01.720p.mkv", 5 * GB, [("某剧.S01E01.2160p.mkv", 1 * GB)]
        )
        self.assertEqual(action, "keep")
        self.assertIn("低于现有 2160p", reason)

    def test_fake_4k_blocked(self):
        # 4K 标签但体积只有旧版一半 → 不换
        action, reason = _q.compare_quality(
            "某剧.S01E01.2160p.mkv", 400 * MB, [("某剧.S01E01.1080p.mkv", 1 * GB)]
        )
        self.assertEqual(action, "keep")
        self.assertIn("疑似伪 4K", reason)

    def test_same_resolution_bigger_enough(self):
        action, reason = _q.compare_quality(
            "某剧.S01E01.1080p.mkv", 1200 * MB, [("某剧.S01E01.1080p.mkv", 1000 * MB)]
        )
        self.assertEqual(action, "upgrade")
        self.assertIn("+20%", reason)

    def test_same_resolution_gain_too_small(self):
        action, reason = _q.compare_quality(
            "某剧.S01E01.1080p.mkv", 1050 * MB, [("某剧.S01E01.1080p.mkv", 1000 * MB)]
        )
        self.assertEqual(action, "keep")
        self.assertIn("体积增益不足", reason)

    def test_min_gain_mb_dominates_for_small_files(self):
        # 小文件：+10% 只有 50 MB，被 200 MB 门槛挡住
        action, _ = _q.compare_quality(
            "某剧.S01E01.1080p.mkv", 550 * MB, [("某剧.S01E01.1080p.mkv", 500 * MB)]
        )
        self.assertEqual(action, "keep")
        action2, _ = _q.compare_quality(
            "某剧.S01E01.1080p.mkv", 750 * MB, [("某剧.S01E01.1080p.mkv", 500 * MB)]
        )
        self.assertEqual(action2, "upgrade")

    def test_unknown_resolution_falls_back_to_size(self):
        action, reason = _q.compare_quality(
            "某剧.S01E01.XViD.avi", 2 * GB, [("某剧.S01E01.avi", 1 * GB)]
        )
        self.assertEqual(action, "upgrade")
        self.assertIn("体积", reason)

    def test_unknown_old_size_upgrades(self):
        action, reason = _q.compare_quality(
            "某剧.S01E01.1080p.mkv", 1 * GB, [("某剧.S01E01.1080p.mkv", 0)]
        )
        self.assertEqual(action, "upgrade")
        self.assertIn("旧文件体积未知", reason)

    def test_both_sizes_unknown_keeps(self):
        action, _ = _q.compare_quality("某剧.S01E01.mkv", 0, [("某剧.S01E01.mkv", 0)])
        self.assertEqual(action, "keep")

    def test_picks_best_among_multiple_old_versions(self):
        # 库里同时有 720p 和 1080p，新的是 1080p 但只比 720p 大 → 应和 1080p 比，不换
        old = [("某剧.S01E01.720p.mkv", 600 * MB), ("某剧.S01E01.1080p.mkv", 2 * GB)]
        action, _ = _q.compare_quality("某剧.S01E01.1080p.mkv", 1200 * MB, old)
        self.assertEqual(action, "keep")

    def test_accepts_dict_versions(self):
        action, _ = _q.compare_quality(
            "某剧.S01E01.1080p.mkv",
            3 * GB,
            [{"file_name": "某剧.S01E01.1080p.mkv", "file_size": 1 * GB}],
        )
        self.assertEqual(action, "upgrade")

    def test_empty_old_versions_upgrades(self):
        action, _ = _q.compare_quality("某剧.S01E01.1080p.mkv", 1 * GB, [])
        self.assertEqual(action, "upgrade")

    def test_custom_thresholds_respected(self):
        old = [("某剧.S01E01.1080p.mkv", 1000 * MB)]
        # 默认 10% + 200MB 不达标；放宽到 2% 则达标
        self.assertEqual(_q.compare_quality("某剧.S01E01.1080p.mkv", 1040 * MB, old)[0], "keep")
        self.assertEqual(
            _q.compare_quality(
                "某剧.S01E01.1080p.mkv", 1100 * MB, old, min_gain_percent=2, min_gain_mb=50
            )[0],
            "upgrade",
        )

    def test_human_size(self):
        self.assertEqual(_q.human_size(0), "0 B")
        self.assertEqual(_q.human_size(1024), "1 KB")
        self.assertEqual(_q.human_size(int(2.5 * GB)), "2.50 GB")


if __name__ == "__main__":
    unittest.main()
