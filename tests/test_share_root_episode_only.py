"""散装集数文件不应被判「分享根目录与订阅标题不符」（1.7.68）。

回归背景（线上 2026-10-25 11:03）：尚公主缺 E01-E04，搜到 4 个夸克候选，
根目录分别是 `01-4K.mp4`、`S01E01.mkv`、`6.mkv`、`01.mkv` —— 全是直接摊在
根目录的视频文件（没有剧名目录），被当成「另一部剧的剧名」整批跳过。
"""

import os
import re
import sys
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
file_matcher_module = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
)
FileMatcher = file_matcher_module.FileMatcher


class _Media:
    title = "尚公主"
    year = "2026"
    cn_name = "尚公主"

    def get_name(self):
        return "尚公主"


def _files(*names):
    return [{"name": n, "_relative_path": n} for n in names]


def test_episode_only_files_are_not_mismatch():
    for names in (
        ("01-4K.mp4", "02-4K.mp4", "03-4K.mp4"),
        ("S01E01.mkv", "S01E02.mkv", "S01E03.mkv"),
        ("01.mkv", "02.mkv", "03.mkv"),
        ("6.mkv",),
    ):
        assert FileMatcher.share_title_mismatch(
            _files(*names), _Media(), "【尚公主】更新至第6集"
        ) is False, names


def test_real_other_drama_dir_still_blocked():
    files = [
        {"name": "01.mkv", "_relative_path": "私.行/01.mkv"},
        {"name": "02.mkv", "_relative_path": "私.行/02.mkv"},
    ]
    # 资源标题写的是目标剧，分享里却是另一部剧的目录 → 必须拦。
    assert FileMatcher.share_title_mismatch(
        files, _Media(), "尚公主 更新至第04集"
    ) is True


def test_matching_title_dir_allowed():
    files = [{"name": "01.mkv", "_relative_path": "尚公主/Season 1/01.mkv"}]
    assert FileMatcher.share_title_mismatch(files, _Media(), "尚公主") is False
