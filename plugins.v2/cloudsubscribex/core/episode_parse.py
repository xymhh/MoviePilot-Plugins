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


def parse_season_episode(
    stem: str,
    custom_words=None,
    season: int | None = None,
    meta_factory=None,
    episode_fallback=None,
) -> tuple:
    """用 MetaInfo 解析文件名主干，返回 ``(season, episodes)``。

    四处季集解析的唯一入口（episode_dedup / naming / metadata 的
    MetaInfo 解析 + share_recognize 的单文件兜底都收进这里）：
    先走 MetaInfo（``episode_list``，无则 ``begin_episode``）；
    认不出集号时再走单文件兜底（开头数字 + 画质词，如
    ``14 DV.mkv → E14``，见 ``utils.share_recognize.episode_from_name``）。
    ``season`` 只作季号缺省，不参与集号判定；``meta_factory`` /
    ``episode_fallback`` 只为单测打桩，生产调用方不传。
    """
    text = str(stem or "").strip()
    if not text:
        return season, []
    factory = meta_factory
    if factory is None:
        try:
            from app.core.metainfo import MetaInfo

            factory = lambda value, words: MetaInfo(
                value, custom_words=words
            )
        except Exception:
            factory = None
    meta = None
    if factory is not None:
        try:
            meta = factory(text, custom_words)
        except Exception:
            meta = None
    found_season = None
    episodes: list = []
    if meta is not None:
        found_season = getattr(meta, "begin_season", None)
        episodes = list(getattr(meta, "episode_list", None) or [])
        if not episodes and getattr(meta, "begin_episode", None):
            episodes = [meta.begin_episode]
    if not episodes:
        fallback = episode_fallback
        if fallback is None:
            try:
                from ..utils.share_recognize import episode_from_name

                fallback = episode_from_name
            except Exception:
                fallback = None
        if fallback is not None:
            try:
                single = fallback(text, found_season or season)
            except Exception:
                single = None
            if single is not None:
                episodes = [single]
    try:
        parsed = sorted({int(episode) for episode in episodes if int(episode) > 0})
    except (TypeError, ValueError):
        parsed = []
    return (found_season or season), parsed


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
