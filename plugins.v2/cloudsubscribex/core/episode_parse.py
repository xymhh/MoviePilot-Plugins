"""文件名集号解析：SxxExx 标签与中文「第 N 集」。

本模块是集号解析的唯一实现（标准库依赖，便于无宿主环境的纯投影层复用）：
``core/api/page.py`` 的本地媒体库扫描与 ``core/downloader_view.py`` 的
下载页投影共用同一套规则——只认目标季的 SxxExx 标签，完全没有 SxxExx
时才回退中文集号，避免把描述性文字误判成集号。
"""

import os
import re
from typing import Iterable, Set

EPISODE_TAG_PATTERN = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,4})")
CN_EPISODE_PATTERN = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
VIDEO_SUFFIXES = {
    ".strm", ".mp4", ".mkv", ".ts", ".avi", ".mov",
    ".wmv", ".flv", ".m2ts", ".iso",
}


def parse_episodes_from_names(
    file_names: Iterable[str], season: int
) -> Set[int]:
    """从文件名解析目标季的集号；只认已存在的文件，多版本同集只算一次。"""
    episodes: Set[int] = set()
    for raw_name in file_names:
        name = str(raw_name or "")
        if not name:
            continue
        suffix = os.path.splitext(name)[1].lower()
        if suffix and suffix not in VIDEO_SUFFIXES:
            continue
        tagged = False
        for tag in EPISODE_TAG_PATTERN.finditer(name):
            tagged = True
            try:
                file_season = int(tag.group(1))
                episode = int(tag.group(2))
            except (TypeError, ValueError):
                continue
            if file_season != season or episode <= 0:
                continue
            episodes.add(episode)
        # 只有完全没有 SxxExx 标签时才回退中文集号，避免把「第2集」这类
        # 描述性文字误判成集号（例如 S01E02 与 S02E01 混在同一目录时）
        if tagged:
            continue
        for tag in CN_EPISODE_PATTERN.finditer(name):
            try:
                episode = int(tag.group(1))
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.add(episode)
    return episodes
