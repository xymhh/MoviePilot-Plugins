"""标题里的季号 / 集号拆解（纯函数，便于单测）。

追更抽屉默认关键词是「剧名 S01」、按集搜是「剧名 S01E07」。后端若把它当纯
标题再拼一次季号，关键词会变成「谷雨 S01 (2026) S01」，线上实测 PanSou 与
SeedHub 都返回 0 条。
"""

import re

_TITLE_SEASON_EPISODE_PATTERN = re.compile(
    r"\s*(?:S(\d{1,2})(?:E\d{1,3})?|第\s*(\d{1,2})\s*季|Season\s*(\d{1,2}))\s*$",
    re.IGNORECASE,
)


def split_title_season(title: str) -> tuple:
    """把「剧名 S01」「剧名 S01E07」「剧名 第1季」拆成 (剧名, 季号)。"""
    text = str(title or "").strip()
    match = _TITLE_SEASON_EPISODE_PATTERN.search(text)
    if not match:
        return text, None
    season_text = match.group(1) or match.group(2) or match.group(3)
    try:
        season = int(season_text) if season_text else None
    except (TypeError, ValueError):
        season = None
    stripped = text[: match.start()].strip()
    if not stripped:
        return text, None
    return stripped, season
