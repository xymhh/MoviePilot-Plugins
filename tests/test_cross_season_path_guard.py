"""1.6.47 分享季号错配：跨季路径守卫单测（组5）。

固定契约：

1. 文件名自身的其它季号仍走 ``contains_other_season``（未改动）；
2. 目录裸季号（如「21806-极恶老大 S2」）与目标季冲突时 strict 候选拒绝，
   且打 debug 日志（路径 + 目标季 + 识别季号）；
3. 目录无季号或目录季号与目标一致时行为不变（titleless 兜底正常出集）；
4. 守卫开关 ``path_season_guard_enabled=False`` 时回退旧逻辑（放行）；
5. ``episode_candidates``：冲突目录下 strict/loose/loosest 一律不入候选；
6. 基础契约：``path_season``/``path_conflicts_season``/``contains_other_season``。
"""

import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks("app.core.metainfo", "app.log")
logger_ref = sys.modules["app.log"].logger

# 平台 MetaInfo 桩：剥扩展名与 SxxExx/第N季 标记，其余残留为 cn_name；
# 无残留 → cn_name=None（titleless 兜底路径）。已烟测匹配 titleless 流程。
def _fake_metainfo(name):
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
    text = re.sub(r"第\s*\d{1,2}\s*季", " ", text)
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

file_parser_module = load_module(
    "cloudsubscribex.utils.file_parser",
    "plugins.v2/cloudsubscribex/utils/file_parser.py",
)
matching_module = load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
file_matcher_module = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
)

MediaFileParser = file_parser_module.MediaFileParser
FileMatcher = file_matcher_module.FileMatcher

DIR_NAME = "21806-极恶老大 S2"


def _item(name="4.mp4", dir_name=DIR_NAME):
    item = {"name": name}
    if dir_name is not None:
        item["_relative_path"] = f"{dir_name}/{name}"
    return item


class CrossSeasonPathGuardTests(unittest.TestCase):
    def tearDown(self):
        FileMatcher.path_season_guard_enabled = True

    def test_strict_rejects_conflicting_bare_season_dir(self):
        """组5-1：目录裸季号冲突 → strict 拒绝；同季正常出集。"""
        item = _item()
        self.assertIsNone(FileMatcher.episode_from_file(item, 1), "S2 目录对 S1 目标必须拒绝")
        self.assertIsNone(FileMatcher.episode_from_file(item, 3), "S2 目录对 S3 目标必须拒绝")
        self.assertEqual(FileMatcher.episode_from_file(item, 2), 4, "S2 目录对 S2 目标正常匹配第4集")

    def test_no_season_dir_behavior_unchanged(self):
        """组5-2：无季号目录不受守卫影响；文件名自身季号仍走 contains_other_season。"""
        plain = _item(dir_name="极恶老大")
        self.assertEqual(FileMatcher.episode_from_file(plain, 1), 4)
        direct = _item(dir_name=None)
        self.assertEqual(FileMatcher.episode_from_file(direct, 1), 4)
        named = _item(name="S03E04.mkv", dir_name="极恶老大")
        self.assertIsNone(
            FileMatcher.episode_from_file(named, 1),
            "文件名自身 S03 与 S1 冲突必须由原 contains_other_season 拒绝",
        )

    def test_guard_disabled_restores_old_behavior(self):
        """组5-3：开关关闭 → 回退旧逻辑（冲突目录放行 titleless 兜底）。"""
        FileMatcher.path_season_guard_enabled = False
        item = _item()
        self.assertEqual(
            FileMatcher.episode_from_file(item, 1), 4,
            "守卫关闭时必须回退旧行为（不做路径季号检查）",
        )
        FileMatcher.path_season_guard_enabled = True
        self.assertIsNone(FileMatcher.episode_from_file(item, 1))

    def test_rejection_logs_debug_with_paths_and_seasons(self):
        """组5-2b：拒绝时打 debug：路径 + 目标季 + 识别季号。"""
        if isinstance(logger_ref, MagicMock):
            logger_ref.debug.reset_mock()
        FileMatcher.episode_from_file(_item(), 1)
        logged = [
            str(call.args[0]) for call in logger_ref.debug.call_args_list
            if call.args and "分享目录季号与目标季冲突" in str(call.args[0])
        ]
        self.assertEqual(len(logged), 1)
        self.assertIn(DIR_NAME, logged[0])
        self.assertIn("目标=S01", logged[0])
        self.assertIn("识别到=S02", logged[0])

    def test_candidates_exclude_conflicting_dir_entirely(self):
        """组5-5：冲突目录下 strict/loose/loosest 一律不入候选。"""
        files = [{
            "is_dir": True,
            "name": DIR_NAME,
            "children": [{"name": "4.mp4"}],
        }]
        self.assertEqual(
            FileMatcher.episode_candidates(files, 1, [4]), {4: []},
            "S1 目标下 S2 目录候选必须为 0（含 loose 兜底）",
        )
        self.assertEqual(
            FileMatcher.episode_candidates(files, 3, [4]), {4: []},
            "S3 目标下 S2 目录候选必须为 0",
        )
        matched = FileMatcher.episode_candidates(files, 2, [4])
        self.assertEqual(len(matched.get(4, [])), 1, "S2 目标正常匹配 1 个候选")

    def test_path_season_and_conflict_primitives(self):
        """组5-6：path_season/path_conflicts_season/contains_other_season 契约。"""
        path_season = file_parser_module.MediaFileParser.path_season
        conflicts = file_parser_module.MediaFileParser.path_conflicts_season
        self.assertEqual(path_season("Show S02"), 2)
        self.assertEqual(path_season("第2季"), 2)
        self.assertEqual(path_season("Season 2"), 2)
        self.assertEqual(path_season("S2"), 2)
        self.assertEqual(path_season(DIR_NAME), 2)
        self.assertIsNone(path_season("无季号目录"))
        self.assertIsNone(path_season("Show 2024"))
        self.assertTrue(conflicts("Show S02", 1))
        self.assertFalse(conflicts("Show S02", 2))
        self.assertFalse(conflicts("无季号目录", 1))
        self.assertFalse(
            MediaFileParser.contains_other_season(DIR_NAME, 1),
            "裸 S2 仍匹配不到既有三模式（守卫正是补这个缺口，此断言防回归扩散）",
        )


if __name__ == "__main__":
    unittest.main()
