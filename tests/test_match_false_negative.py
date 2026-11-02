"""「内容不匹配」误判修复的回归测试（1.9.29）。

三个真实样本来自线上账本（2026-11-02，718 条 mismatch 里抽出来的）：

1. 《邻人可疑》：分享根目录就是订阅名，文件是「上集/中集/下集」——
   根目录守卫把**文件名**当成了根目录，整包判成另一部剧；
2. 《最后生还者 S01》：文件 `01 官中最新4K高清完整版.mp4`，父目录
   `S01最新4K高清完整版` 写着季号，但解析只看文件名 → 判「未被识别」；
3. 《冰之城墙 S01》：`[LoliHouse] Koori no Jouheki - 07 [...]` 罗马音命名。

固定契约：
- ``iter_files`` 对扁平列表沿用条目自带的 ``relative_path``；
- ``episode_from_file`` 在文件名没有季集号时，用父目录季号兜底；
- 目录写了别的季、或没写季号却要 S2 时，仍然拒绝；
- 1080p/2160p 这类分辨率数字不得被当成集号。
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

# MetaInfo 桩：只认 SxxExx 与「第N集」，不认裸数字（复现线上「解析不出季集」）。
def _fake_metainfo(name):
    text = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", str(name or "").strip())
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[ ._-]*[Ee](?:[Pp])?(\d{1,4})", text)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        text = text[: match.start()] + " " + text[match.end():]
    else:
        match = re.search(r"第\s*(\d{1,4})\s*[集话]", text)
        if match:
            episode = int(match.group(1))
            text = text[: match.start()] + " " + text[match.end():]
    residual = re.sub(r"[ ._~-]+", " ", text).strip()
    return SimpleNamespace(
        cn_name=residual or None,
        en_name=None,
        original_name=None,
        begin_season=season,
        begin_episode=episode,
        year=None,
        type=None,
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


def _flat(name, relative_path):
    """夸克/115 list_share_files 的扁平条目形态。"""
    return {
        "name": name,
        "is_dir": False,
        "size": 1024,
        "relative_path": relative_path,
    }


NEIGHBOR_ROOT = "邻人可疑"
NEIGHBOR_FILES = [
    _flat("上集：喜迁新居，竟遇“诡”邻 [322155_maxplus_50fps_tv_6.72GB].mkv",
          f"{NEIGHBOR_ROOT}/上集：喜迁新居，竟遇“诡”邻 [322155_maxplus_50fps_tv_6.72GB].mkv"),
    _flat("中集：双面丈夫，究竟谁在说谎？ [322155_maxplus_50fps_tv_6.60GB].mkv",
          f"{NEIGHBOR_ROOT}/中集：双面丈夫，究竟谁在说谎？ [322155_maxplus_50fps_tv_6.60GB].mkv"),
    _flat("下集：终极反转！全员恶人互搏 [322155_maxplus_50fps_tv_6.45GB].mkv",
          f"{NEIGHBOR_ROOT}/下集：终极反转！全员恶人互搏 [322155_maxplus_50fps_tv_6.45GB].mkv"),
]

LAST_OF_US_DIR = "最后生还者 第一季 The Last of Us Season 1超清4K未删减杜比视界"
LAST_OF_US_S01 = f"{LAST_OF_US_DIR}/S01最新4K高清完整版"


class TestIterFilesKeepsSharePath(unittest.TestCase):
    """1. 扁平列表不得把自带 relative_path 覆盖成文件名。"""

    def test_flat_list_keeps_relative_path(self):
        files = list(MediaFileParser.iter_files(NEIGHBOR_FILES))
        self.assertEqual(len(files), 3)
        self.assertEqual(
            {item["_relative_path"] for item in files},
            {item["relative_path"] for item in NEIGHBOR_FILES},
        )

    def test_share_root_names_uses_directory(self):
        files = list(MediaFileParser.iter_files(NEIGHBOR_FILES))
        self.assertEqual(FileMatcher.share_root_names(files), {NEIGHBOR_ROOT})

    def test_nested_children_still_prefixed_by_parent(self):
        files = [{
            "name": "S01最新4K高清完整版",
            "is_dir": True,
            "children": [{"name": "01 官中最新4K高清完整版.mp4", "is_dir": False}],
        }]
        walked = list(MediaFileParser.iter_files(files))
        self.assertEqual(len(walked), 1)
        self.assertEqual(
            walked[0]["_relative_path"],
            "S01最新4K高清完整版/01 官中最新4K高清完整版.mp4",
        )


class TestEpisodeFromPathEvidence(unittest.TestCase):
    """2. 文件名没有季集号时，用父目录季号兜底。"""

    def test_bare_number_in_season_dir(self):
        item = _flat("01 官中最新4K高清完整版.mp4",
                     f"{LAST_OF_US_S01}/01 官中最新4K高清完整版.mp4")
        self.assertEqual(FileMatcher.episode_from_file(item, 1), 1)

    def test_bare_number_season_two_dir(self):
        item = _flat("07 最新4K高清完整版.mp4",
                     f"{LAST_OF_US_DIR}/S02最新4K高清完整版/07 最新4K高清完整版.mp4")
        self.assertEqual(FileMatcher.episode_from_file(item, 2), 7)

    def test_romaji_dash_episode(self):
        item = _flat(
            "[LoliHouse] Koori no Jouheki - 07 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
            "B 冰之城墙 全2季 1080P/冰之城墙 S01/[LoliHouse] 冰之城墙 [01-14 合集]/"
            "[LoliHouse] Koori no Jouheki - 07 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
        )
        self.assertEqual(FileMatcher.episode_from_file(item, 1), 7)

    def test_part_markers_map_to_episodes(self):
        expected = {"上集": 1, "中集": 2, "下集": 3}
        for item in NEIGHBOR_FILES:
            name = item["name"]
            marker = name[:2]
            self.assertEqual(
                FileMatcher.episode_from_file(item, 1),
                expected[marker],
                name,
            )

    def test_explicit_sxxexx_still_wins(self):
        item = _flat("S01E247.mp4", "吞噬/S01E247.mp4")
        self.assertEqual(FileMatcher.episode_from_file(item, 1), 247)

    def test_bare_episode_marker(self):
        item = _flat("【tvzongheba】E01.mkv", "我的有罪之人/【tvzongheba】E01.mkv")
        self.assertEqual(FileMatcher.episode_from_file(item, 1), 1)

    def test_plain_number_file(self):
        item = _flat("243.mp4", "吞噬/243.mp4")
        self.assertEqual(FileMatcher.episode_from_file(item, 1), 243)

    def test_conflicting_dir_season_rejected(self):
        item = _flat("01 官中最新4K高清完整版.mp4",
                     "某剧 S02/01 官中最新4K高清完整版.mp4")
        self.assertIsNone(FileMatcher.episode_from_file(item, 1))

    def test_no_season_dir_rejects_other_seasons(self):
        item = _flat("05 最新4K高清完整版.mp4", "某剧/05 最新4K高清完整版.mp4")
        self.assertIsNone(FileMatcher.episode_from_file(item, 2))

    def test_resolution_is_not_episode(self):
        item = _flat("1080p 花絮.mp4", "某剧/1080p 花絮.mp4")
        self.assertIsNone(FileMatcher.episode_from_file(item, 1))


class TestShareTitleGuardWithRealShapes(unittest.TestCase):
    """3. 根目录就是订阅名时，不得判成「另一部剧」。"""

    def test_neighbor_share_passes_guard(self):
        mediainfo = SimpleNamespace(title="邻人可疑", year="2026", type=None)
        files = list(MediaFileParser.iter_files(NEIGHBOR_FILES))
        self.assertFalse(
            FileMatcher.share_title_mismatch(files, mediainfo, "邻人可疑", 1)
        )
        self.assertEqual(
            FileMatcher.mismatch_evidence_roots(files, mediainfo, "邻人可疑"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
