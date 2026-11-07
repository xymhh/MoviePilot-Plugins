"""分享识别 v2（M1 只读）回归测试。

方案见 ``docs/plan/2026-11-07-分享识别重构-方案.md``，样本来自真实夸克分享的
完整目录树 dump（``tests/fixtures/share_scan/*.json``，只读列举，不含凭证）。

运行：``cd <repo> && python -m pytest tests/test_share_recognize.py -q``
"""

import json
import os
import unittest

from plugin_env import REPO_ROOT, load_module, register_package

MODULE_PATH = "plugins.v2/cloudsubscribex/utils/share_recognize.py"
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "share_scan")


def _load():
    register_package("cloudsubscribex", [os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")])
    register_package("cloudsubscribex.utils",
                     [os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex", "utils")])
    return load_module("cloudsubscribex.utils.share_recognize", MODULE_PATH)


sr = _load()


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)["entries"]


def _entries(*pairs):
    return [{"path": path, "size": size} for path, size in pairs]


class JiaJinChaiTest(unittest.TestCase):
    """嫁金钗：DV 27 集为主版本，28–30 从散文件组补齐。"""

    def setUp(self):
        self.plan = sr.build_plan(_fixture("jiajinchai.json"),
                                  work_id=300146, season=1, season_total=30)

    def test_ready_with_full_coverage(self):
        self.assertEqual(self.plan["status"], sr.READY)
        self.assertEqual(self.plan["missing"], [])
        media = [p for p in self.plan["selected"] if p.endswith(".mkv")]
        self.assertEqual(len(media), 30)

    def test_primary_is_largest_median_group(self):
        self.assertEqual(self.plan["group"], "4K DV杜比视界 高码率")

    def test_fill_episodes_28_to_30_and_mark_mixed(self):
        self.assertEqual(sorted(self.plan["filled_from"]), [28, 29, 30])
        self.assertTrue(self.plan["mixed_quality"])
        for path in self.plan["selected"]:
            if path.endswith(("28 HDR.mkv", "29 HDR.mkv", "30 HDR.mkv")):
                self.assertFalse(path.startswith("4K DV"))

    def test_no_donor_kept_when_complete(self):
        # 补齐后 4K/ 组（30 集、1.2GB/集）不得顶替主版本
        self.assertFalse(any(p.startswith("4K/") for p in self.plan["selected"]))


class MeiRenYuTest(unittest.TestCase):
    """美人余：三个版本目录全是 zip 分卷 → 一个媒体文件都没有。"""

    def test_skip_no_media(self):
        plan = sr.build_plan(_fixture("meirenyu.json"),
                             work_id=294446, season=1, season_total=28)
        self.assertEqual(plan["status"], sr.SKIP)
        self.assertEqual(plan["reason"], sr.NO_MEDIA)
        self.assertEqual(plan["selected"], [])


class InvariantTest(unittest.TestCase):
    """方案 §6 的三条不变量。"""

    def test_message_text_cannot_reach_plan(self):
        # R4：消息只给片名，任何「更99集」都不该有入口
        with self.assertRaises(TypeError):
            sr.build_plan(_fixture("jiajinchai.json"), work_id=1,
                          season=1, season_total=30, message="更99集")

    def test_sidecar_never_counts_as_episode_but_follows_media(self):
        plan = sr.build_plan(_entries(("G/01.mkv", 5 << 30), ("G/01.srt", 4096)),
                             work_id=1, season=1, season_total=1)
        self.assertEqual(plan["status"], sr.READY)
        self.assertIn("G/01.srt", plan["selected"])
        self.assertEqual([p for p in plan["selected"] if p.endswith(".mkv")],
                         ["G/01.mkv"])
        self.assertEqual(plan["missing"], [])

    def test_pure_subtitle_share_is_no_media(self):
        plan = sr.build_plan(_entries(("G/01.srt", 4096), ("G/02.ass", 2048)),
                             work_id=1, season=1, season_total=2)
        self.assertEqual(plan["status"], sr.SKIP)
        self.assertEqual(plan["reason"], sr.NO_MEDIA)

    def test_unknown_identity_cannot_be_ready(self):
        plan = sr.build_plan(_fixture("jiajinchai.json"),
                             work_id=None, season=1, season_total=30)
        self.assertEqual(plan["status"], sr.REVIEW)
        self.assertEqual(plan["reason"], sr.NO_IDENTITY)

    def test_no_episode_marks_review(self):
        plan = sr.build_plan(_entries(("G/花絮.mkv", 1 << 30)), work_id=1, season=1)
        self.assertEqual(plan["status"], sr.REVIEW)
        self.assertEqual(plan["reason"], sr.NO_EPISODES)

    def test_iso_switch_defaults_off(self):
        entries = _entries(("G/01.iso", 8 << 30), ("G/02.iso", 8 << 30))
        self.assertEqual(sr.build_plan(entries, work_id=1, season=1)["reason"],
                         sr.NO_MEDIA)
        plan = sr.build_plan(entries, work_id=1, season=1, treat_iso=True)
        self.assertEqual(plan["status"], sr.READY)


class QualityFirstTest(unittest.TestCase):
    """质量优先 + 缺集补齐：主版本取体积中位数最大组，补集只能来自更小的组。"""

    def test_fill_only_from_smaller_group(self):
        plan = sr.build_plan(_entries(
            ("BIG/01.mkv", 6 << 30), ("BIG/02.mkv", 6 << 30),
            ("SMALL/01.mkv", 1 << 30), ("SMALL/02.mkv", 1 << 30),
            ("SMALL/03.mkv", 1 << 30), ("SMALL/04.mkv", 1 << 30),
        ), work_id=1, season=1, season_total=4)
        self.assertEqual(plan["group"], "BIG")
        self.assertEqual(sorted(plan["filled_from"]), [3, 4])
        self.assertEqual(plan["filled_from"][3], "SMALL")
        self.assertTrue(plan["mixed_quality"])

    def test_complete_primary_is_not_upgraded(self):
        # 主版本已完整 → 不为了「更高画质」洗版
        plan = sr.build_plan(_entries(
            ("A/01.mkv", 6 << 30), ("A/02.mkv", 6 << 30),
            ("B/01.mkv", 1 << 30), ("B/02.mkv", 1 << 30),
        ), work_id=1, season=1, season_total=2)
        self.assertFalse(plan["mixed_quality"])
        self.assertEqual(plan["filled_from"], {})


if __name__ == "__main__":
    unittest.main()
