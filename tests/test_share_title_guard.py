"""1.7.9 分享根目录标题守卫单测。"""

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
    text = str(name or "").strip()
    for ext in (".mkv", ".mp4", ".avi", ".ts"):
        if text.lower().endswith(ext):
            text = text[: -len(ext)]
            break
    season = episode = year = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        text = text[: match.start()] + " " + text[match.end():]
    year_match = re.search(r"(?:19|20)\d{2}", text)
    if year_match:
        year = year_match.group(0)
        text = text[: year_match.start()] + " " + text[year_match.end():]
    text = re.sub(
        r"(?i)\b(?:4k|2160p|1080p|720p|web[- .]?dl|hdr10?|dv|hevc|h265|h264|avc|aac|dts|remux|bluray)\b.*$",
        " ",
        text,
    )
    text = re.sub(r"[ ._~\-]+", " ", text).strip()
    return SimpleNamespace(
        cn_name=text or None,
        en_name=None,
        original_name=None,
        year=year,
        type="tv",
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
file_matcher_module = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
)
FileMatcher = file_matcher_module.FileMatcher

MEDIA = SimpleNamespace(
    title="伟大的长征",
    en_title="The Great Long March",
    original_title=None,
    original_name=None,
    names=[],
    year="2026",
    type="tv",
)


def _share(root, name="01.mp4"):
    return [{"name": name, "_relative_path": f"{root}/{name}"}]


class ShareTitleGuardTests(unittest.TestCase):
    def test_rejects_other_show_folder(self):
        self.assertTrue(
            FileMatcher.share_title_mismatch(_share("私.行"), MEDIA, "伟大的长征")
        )

    def test_rejects_obfuscated_other_show_folder(self):
        self.assertTrue(
            FileMatcher.share_title_mismatch(_share("玉-癫-囚 (2026)"), MEDIA, "玉簟秋")
        )

    def test_accepts_matching_folder_with_tags(self):
        self.assertFalse(
            FileMatcher.share_title_mismatch(_share("伟大的长征 4K"), MEDIA, "伟大的长征")
        )

    def test_skips_generic_folder(self):
        self.assertFalse(
            FileMatcher.share_title_mismatch(_share("国剧"), MEDIA, "伟大的长征")
        )
        self.assertFalse(
            FileMatcher.share_title_mismatch(_share("更新至20集"), MEDIA, "伟大的长征")
        )

    def test_accepts_when_one_root_matches(self):
        files = _share("私.行") + _share("伟大的长征")
        self.assertFalse(FileMatcher.share_title_mismatch(files, MEDIA, "伟大的长征"))

    def test_single_file_share_uses_filename(self):
        files = [{"name": "伟大的长征.S01E20.2160p.mkv"}]
        self.assertFalse(FileMatcher.share_title_mismatch(files, MEDIA, "伟大的长征"))


if __name__ == "__main__":
    unittest.main()
