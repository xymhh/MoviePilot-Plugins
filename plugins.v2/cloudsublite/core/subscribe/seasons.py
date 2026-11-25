"""按媒体库进度解析需要创建订阅的季。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfo
from app.schemas.types import MediaType

try:
    from ..media_scope import season_episodes_in_scope
except (ImportError, AttributeError):  # 单测/精简环境没有媒体库过滤模块
    def season_episodes_in_scope(mediainfo: Any, season: int):
        return None


@dataclass
class _SeasonProgress:
    complete: bool = False
    missing_episodes: List[int] = field(default_factory=list)
    total_episode: int = 0


def _season_map(media: Any) -> dict:
    """取出媒体的季集映射（季号 -> 集号列表）。"""
    seasons = getattr(media, "seasons", None)
    return dict(seasons) if isinstance(seasons, dict) else {}


def _season_episodes(seasons: dict, season: int) -> List[int]:
    """取出目标季的集号列表（兼容 int / str 两种季号键）。"""
    episodes = seasons.get(season) or seasons.get(str(season)) or []
    try:
        return sorted({int(episode) for episode in episodes})
    except (TypeError, ValueError):
        return []


def probe_season_progress(
    mediainfo: Any,
    season: int,
    download_chain: Any = None,
) -> _SeasonProgress:
    """检查目标季在媒体库是否完整。

    无法检查（缺媒体身份 / 宿主异常）时按「整季缺失」处理，与 MoviePilot
    ``get_no_exists_info`` 拿不到身份时的返回值语义保持一致。
    """
    season = int(season)
    episodes = _season_episodes(_season_map(mediainfo), season)
    if not episodes:
        return _SeasonProgress()
    unknown = _SeasonProgress(
        missing_episodes=list(episodes), total_episode=max(episodes)
    )
    if not getattr(mediainfo, "media_source", None) or not getattr(mediainfo, "media_id", None):
        return unknown
    scoped_episodes = season_episodes_in_scope(mediainfo, season)
    if scoped_episodes is not None:
        missing = sorted(set(episodes) - scoped_episodes)
        return _SeasonProgress(
            complete=not missing,
            missing_episodes=missing,
            total_episode=max(episodes),
        )
    try:
        media = copy.deepcopy(mediainfo)
        meta = MetaInfo(getattr(media, "title", ""))
        meta.year = getattr(media, "year", None)
        meta.type = getattr(media, "type", None)
        meta.begin_season = season
        media.season = season
        media.seasons = {season: episodes}
        chain = download_chain or DownloadChain()
        exists, no_exists = chain.get_no_exists_info(meta=meta, mediainfo=media)
        missing: set = set()
        for season_map in (no_exists or {}).values():
            for info in (season_map or {}).values():
                if getattr(info, "season", None) != season:
                    continue
                absent = getattr(info, "episodes", None) or []
                missing.update(episodes if not absent else (int(e) for e in absent))
        if not exists and not missing:
            missing.update(episodes)
        return _SeasonProgress(
            complete=bool(exists) and not missing,
            missing_episodes=sorted(missing),
            total_episode=max(episodes),
        )
    except Exception:
        return unknown


def _recognize_media(media_source: Any, media_id: Any, mtype: Any) -> Optional[Any]:
    """按媒体身份重新识别，取回季集信息；身份不全或识别失败时返回 None。"""
    if not media_source or not media_id:
        return None
    try:
        return MediaChain().recognize_media(
            mtype=mtype or MediaType.TV,
            media_source=media_source,
            media_id=media_id,
            cache=False,
        )
    except Exception:
        return None


def resolve_subscribe_seasons(
    mediainfo: Any = None,
    *,
    media_source: Any = None,
    media_id: Any = None,
    mtype: Any = None,
    skip_season_zero: bool = True,
    limit: int = 0,
    download_chain: Any = None,
) -> List[int]:
    """
    解析需要建立订阅的缺集季，按季号升序返回（默认排除第 0 季/特辑）。

    ``SubscribeChain.add`` 不接收 mediainfo（MoviePilot 片名选定路径只传
    media_source / media_id），因此 mediainfo 缺失或没有季集信息时，
    用身份字段重新识别媒体。
    """
    media = mediainfo
    seasons = _season_map(media)
    if media is None or not seasons:
        recognized = _recognize_media(
            media_source or getattr(media, "media_source", None),
            media_id or getattr(media, "media_id", None),
            mtype,
        )
        if recognized is not None:
            media = recognized
            seasons = _season_map(recognized)
    if media is None or not seasons:
        return []
    try:
        numbers = sorted({int(number) for number in seasons})
    except (TypeError, ValueError):
        return []
    missing_seasons: List[int] = []
    for number in numbers:
        if skip_season_zero and number <= 0:
            continue
        if not _season_episodes(seasons, number):
            continue
        if not probe_season_progress(media, number, download_chain).complete:
            missing_seasons.append(number)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    return missing_seasons[:limit] if limit else missing_seasons
