"""1.7.95 S1：分享根目录守卫改正向 + 真机样本回归。

样本来自 2026-10-28 线上《尚公主》4K 源（只存文件名/大小快照，不存链接）：
4K 源普遍用反爬/异体根目录（``S）尚丨G丨玊（2026）``、``S 尙伀主``、
``S【上@供#住】``），旧守卫按「名字不像本剧」整包拦下，导致 4.7-4.9GB/集
的资源永远进不来，库里只剩 1.xGB 的 1080p 版本。
"""

import json
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
FileMatcher = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
).FileMatcher

FIXTURE = os.path.join(
    current_dir, "fixtures", "shares", "title_guard_2026-10-28.json"
)


def _load_cases():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


class TitleGuardRealShapesTests(unittest.TestCase):
    """真机样本：守卫必须放行本剧 4K 反爬名，仍拦真正的另一部剧。"""

    @classmethod
    def setUpClass(cls):
        payload = _load_cases()
        cls.media = SimpleNamespace(
            title=payload["media"]["title"],
            en_title=None,
            original_title=None,
            original_name=None,
            names=[],
            year=payload["media"]["year"],
            type=payload["media"]["type"],
        )
        cls.cases = {case["id"]: case for case in payload["cases"]}

    def test_all_cases_present(self):
        self.assertEqual(
            set(self.cases),
            {
                "4k_60fps_variant_2026",
                "4k_variant_chars",
                "4k_obfuscated_at_hash",
                "ads_mixed_with_episodes",
                "code_prefix_episode_names",
                "true_other_show_plain",
            },
        )

    def test_mismatch_matches_expectation(self):
        for case_id, case in self.cases.items():
            with self.subTest(case=case_id):
                self.assertIs(
                    FileMatcher.share_title_mismatch(
                        case["files"],
                        self.media,
                        case["resource_title"],
                        1,
                    ),
                    case["expect_mismatch"],
                    msg=case["why"],
                )

    def test_season_gate_is_deliberate(self):
        """没有 season 时不软放行：4K 变体名仍拦，证明软放行只对剧集季生效。"""
        for case_id, case in self.cases.items():
            if "expect_mismatch_without_season" not in case:
                continue
            with self.subTest(case=case_id):
                self.assertIs(
                    FileMatcher.share_title_mismatch(
                        case["files"], self.media, case["resource_title"]
                    ),
                    case["expect_mismatch_without_season"],
                )

    def test_evidence_roots_point_at_real_culprit(self):
        for case_id, case in self.cases.items():
            expected = case.get("expect_evidence_roots")
            if expected is None:
                continue
            with self.subTest(case=case_id):
                self.assertEqual(
                    FileMatcher.mismatch_evidence_roots(
                        case["files"], self.media, case["resource_title"]
                    ),
                    expected,
                )

    def test_ad_and_code_prefix_never_count_as_title_evidence(self):
        for name in (
            "基德盘搜安卓app.apk",
            "汁源分享.jpg",
            "07.mkv",
            "SGZBRMY-07.mkv",
            "S01E11_4K_HDR_60fps.mkv",
        ):
            with self.subTest(root=name):
                self.assertFalse(FileMatcher._is_title_evidence_root(name))


if __name__ == "__main__":
    unittest.main()
