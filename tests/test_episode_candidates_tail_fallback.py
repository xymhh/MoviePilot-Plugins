"""转存匹配阶段的集号兜底（黑色五叶草001.mp4 这类标题与数字紧贴的命名）。

识别阶段早已有 ``rescue_tail_episodes_from_names``，但真正挑文件走
``FileMatcher.episode_candidates``，平台解析不出集号时一律 continue，
导致计划 ready 却「集号不符」整批跳过。这里补同门槛的兜底：

1. 整批都解析不出集号、且末尾数字命中率 >=80% 时才采信；
2. 命中 <3 个或集号去重 <3 个时放弃（宁可漏转不错转）；
3. 平台能解析（SxxExx / 第 N 集）时行为不变；
4. 4 位数字（1080p / 年份）不参与兜底。
"""

import os
import re
import sys
import unittest
from types import SimpleNamespace

from plugin_env import install_app_mocks, load_module, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks("app.core.metainfo", "app.log")


def _fake_metainfo(name):
    """平台桩：认 SxxExx，其余一律无季集号（模拟「标题+紧贴数字」解析失败）。"""
    text = str(name or "").strip()
    lowered = text.lower()
    for ext in (".mkv", ".mp4", ".avi", ".ts"):
        if lowered.endswith(ext):
            text = text[: -len(ext)]
            break
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        text = text[: match.start()] + " " + text[match.end():]
    text = re.sub(r"[ ._~-]+", " ", text).strip()
    return SimpleNamespace(
        cn_name=text or None,
        en_name=None,
        original_name=None,
        begin_season=season,
        begin_episode=episode,
    )


register_module("app.core.metainfo", MetaInfo=_fake_metainfo)
install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.utils", "cloudsubscribex.search",
)

load_module(
    "cloudsubscribex.utils.file_parser",
    "plugins.v2/cloudsubscribex/utils/file_parser.py",
)
load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
load_module(
    "cloudsubscribex.utils.share_recognize",
    "plugins.v2/cloudsubscribex/utils/share_recognize.py",
)
file_matcher_module = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
)
FileMatcher = file_matcher_module.FileMatcher


def _files(*names):
    return [{"name": name, "size": 800 * 1024 * 1024} for name in names]


class EpisodeCandidatesTailFallbackTest(unittest.TestCase):
    def test_glued_numbers_matched(self):
        names = [f"黑色五叶草{i:03d}.mp4" for i in range(1, 11)]
        result = FileMatcher.episode_candidates(_files(*names), 1, list(range(1, 11)))
        for episode in range(1, 11):
            self.assertEqual(len(result[episode]), 1, f"E{episode:02d} 应命中 1 个文件")
        self.assertTrue(result[3][0]["name"].endswith("003.mp4"))

    def test_only_missing_episodes_filled(self):
        """只补缺集：目标集号之外的兜底结果不参与匹配。"""
        names = [f"黑色五叶草{i:03d}.mp4" for i in range(1, 11)]
        result = FileMatcher.episode_candidates(_files(*names), 1, [3, 7])
        self.assertEqual(len(result[3]), 1)
        self.assertEqual(len(result[7]), 1)

    def test_low_hit_rate_rejected(self):
        """命中率 <80%：宁可留空也不猜。"""
        names = [
            "黑色五叶草001.mp4",
            "黑色五叶草002.mp4",
            "黑色五叶草003.mp4",
            "无编号宣传片.mp4",
            "无编号花絮.mp4",
        ]
        result = FileMatcher.episode_candidates(_files(*names), 1, [1, 2, 3])
        self.assertEqual(result, {1: [], 2: [], 3: []})

    def test_too_few_episodes_rejected(self):
        names = ["黑色五叶草001.mp4", "黑色五叶草001.mp4"]
        result = FileMatcher.episode_candidates(_files(*names), 1, [1])
        self.assertEqual(result, {1: []})

    def test_platform_parsed_names_unchanged(self):
        names = [f"黑色五叶草.S01E{i:02d}.mp4" for i in range(1, 6)]
        result = FileMatcher.episode_candidates(_files(*names), 1, [1, 2, 3, 4, 5])
        for episode in range(1, 6):
            self.assertEqual(len(result[episode]), 1)

    def test_resolution_suffix_ignored(self):
        """4 位分辨率不是集号：整批都不该命中。"""
        names = ["黑色五叶草1080.mp4" for _ in range(5)]
        result = FileMatcher.episode_candidates(_files(*names), 1, [1080])
        self.assertEqual(result, {1080: []})


if __name__ == "__main__":
    unittest.main()
