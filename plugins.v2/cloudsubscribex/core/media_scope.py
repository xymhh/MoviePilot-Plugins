"""媒体库范围过滤：把指定路径下的媒体库条目排除在插件的入库判断之外。

115 STRM 插件的「分享 STRM」通常单独建一个 Emby 媒体库，路径与插件自己
转存生成的媒体库不同。MoviePilot 的 ``media_exists`` 会直接搜索 Emby 全库，
并不会遵守 ``sync_libraries``，因此只靠平台配置无法排除分享库；这里按路径
前缀在插件侧统一过滤。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Set

from .media import (
    TMDB_SOURCE,
    media_identity,
    media_server_tmdb_filters,
)

_IGNORE_PREFIXES: tuple[str, ...] = ()


def normalize_media_path(path: Any) -> str:
    """统一媒体服务器路径：反斜杠转正斜杠、合并重复分隔符、去尾斜杠。"""
    value = str(path or "").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def configure_media_path_ignores(value: Any) -> None:
    """从配置文本（每行一个路径）初始化忽略前缀。"""
    global _IGNORE_PREFIXES
    if isinstance(value, (list, tuple, set)):
        raw_lines = list(value)
    else:
        raw_lines = str(value or "").splitlines()
    prefixes = []
    seen = set()
    for raw_line in raw_lines:
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        prefix = normalize_media_path(line).casefold()
        if not prefix or prefix == "/" or prefix in seen:
            continue
        seen.add(prefix)
        prefixes.append(prefix)
    _IGNORE_PREFIXES = tuple(sorted(prefixes, key=len, reverse=True))


def media_path_ignores() -> tuple[str, ...]:
    """返回当前生效的忽略前缀（测试与诊断用）。"""
    return _IGNORE_PREFIXES


def is_ignored_media_path(path: Any) -> bool:
    """判断路径是否落在忽略前缀下；仅匹配整段路径前缀。"""
    if not _IGNORE_PREFIXES:
        return False
    normalized = normalize_media_path(path).casefold()
    if not normalized:
        return False
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in _IGNORE_PREFIXES
    )


def filter_media_rows(rows: Iterable[Any]) -> List[Any]:
    """过滤掉路径命中忽略前缀的媒体服务器条目。"""
    return [
        row for row in rows
        if not is_ignored_media_path(getattr(row, "path", ""))
    ]


def _row_media_type(row: Any) -> str:
    text = str(getattr(row, "item_type", "") or "").strip().casefold()
    if text in {"movie", "film", "电影"}:
        return "movie"
    if text in {"tv", "series", "show", "television", "电视剧", "剧集"}:
        return "tv"
    return ""


def _identity_filters(mediainfo: Any) -> Optional[List[Any]]:
    from app.db.models.mediaserver import MediaServerItem

    source, media_id = media_identity(mediainfo)
    if not source or not media_id:
        return None
    if source == TMDB_SOURCE:
        try:
            tmdb_id = int(media_id)
        except (TypeError, ValueError):
            return None
        if tmdb_id <= 0:
            return None
        return media_server_tmdb_filters(MediaServerItem, [tmdb_id])
    if not hasattr(MediaServerItem, "media_source"):
        return None
    from sqlalchemy import func

    return [
        func.lower(MediaServerItem.media_source) == source,
        MediaServerItem.media_id == str(media_id),
    ]


def _scoped_rows(mediainfo: Any) -> Optional[List[Any]]:
    """返回忽略前缀生效时的非忽略条目；未配置过滤时返回 None。"""
    if not _IGNORE_PREFIXES:
        return None
    filters = _identity_filters(mediainfo)
    if filters is None:
        return None
    try:
        from app.db import SessionFactory
        from app.db.models.mediaserver import MediaServerItem

        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(*filters).all()
    except Exception:
        # 数据库不可用或模型字段不兼容时交回平台逻辑，避免误判整季缺失。
        return None
    return filter_media_rows(rows)


def season_episodes_in_scope(mediainfo: Any, season: int) -> Optional[Set[int]]:
    """返回忽略前缀生效时目标季实际存在的集号；未启用过滤时返回 None。"""
    rows = _scoped_rows(mediainfo)
    if rows is None:
        return None
    target = int(season)
    episodes: Set[int] = set()
    for row in rows:
        if _row_media_type(row) != "tv":
            continue
        seasoninfo = getattr(row, "seasoninfo", None) or {}
        if not isinstance(seasoninfo, dict):
            continue
        values = seasoninfo.get(target) or seasoninfo.get(str(target)) or []
        for value in values or []:
            try:
                episode = int(value)
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.add(episode)
    return episodes


def movie_in_scope(mediainfo: Any) -> Optional[bool]:
    """返回忽略前缀生效时电影是否存在；未启用过滤时返回 None。"""
    rows = _scoped_rows(mediainfo)
    if rows is None:
        return None
    return any(_row_media_type(row) == "movie" for row in rows)
