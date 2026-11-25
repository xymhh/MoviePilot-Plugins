"""身份层（M2）：作品 → 季 → 季集表，供识别层算「缺哪几集」。

规则收得很紧（方案 R5）：TMDB 只提供**集数**，不用来猜作品，也不参与版本选择。
无季号的分享只在「某一季的集数区间能完整容纳全部集号」时才反推季，否则返回
``None`` 交给上层判 ``review: NO_IDENTITY``。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

EpisodeLookup = Callable[[int, int], List[Any]]


def _episode_number(item: Any) -> Optional[int]:
    raw = (
        item.get("episode_number")
        if isinstance(item, dict)
        else getattr(item, "episode_number", None)
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def default_lookup(tmdb_id: int, season: int) -> List[Any]:
    """默认走 MoviePilot 的 ``TmdbChain``（与既有追更逻辑同源）。"""
    try:
        from app.chain.tmdb import TmdbChain
        return TmdbChain().tmdb_episodes(tmdbid=int(tmdb_id), season=int(season)) or []
    except Exception:  # noqa: BLE001 - 拿不到就当没有季集表
        return []


def season_episodes(
        tmdb_id: Optional[int],
        season: int,
        lookup: Optional[EpisodeLookup] = None,
) -> List[int]:
    """该季 TMDB 的集号列表（升序）；拿不到返回空列表。"""
    try:
        tmdb_id = int(tmdb_id or 0)
    except (TypeError, ValueError):
        return []
    if tmdb_id <= 0:
        return []
    rows = (lookup or default_lookup)(tmdb_id, int(season)) or []
    numbers = sorted({ep for ep in (_episode_number(row) for row in rows) if ep})
    return numbers


def season_total(
        tmdb_id: Optional[int],
        season: int,
        lookup: Optional[EpisodeLookup] = None,
) -> Optional[int]:
    """该季集数；TMDB 没数据时返回 ``None``（不拿「最大集号」冒充集数）。"""
    numbers = season_episodes(tmdb_id, season, lookup)
    return numbers[-1] if numbers else None


def season_totals(
        tmdb_id: Optional[int],
        seasons: Iterable[int],
        lookup: Optional[EpisodeLookup] = None,
) -> Dict[int, int]:
    """批量取多季集数，只保留拿得到数据的季。"""
    totals: Dict[int, int] = {}
    for season in seasons or []:
        total = season_total(tmdb_id, int(season), lookup)
        if total:
            totals[int(season)] = total
    return totals


def plan_for_share(
        entries: List[Dict[str, Any]],
        work_id: Optional[int] = None,
        season: Optional[int] = None,
        season_total_hint: Optional[int] = None,
        lookup: Optional[EpisodeLookup] = None,
        treat_iso: bool = False,
) -> Dict[str, Any]:
    """把「枚举结果 + 作品」合成识别计划（M2 组合层）。

    季号优先用调用方给的（订阅季），其次用整棵树里唯一的显式季号；
    集数优先问 TMDB，TMDB 没有时才用调用方给的 ``season_total_hint``。
    """
    from .share_recognize import build_plan

    resolved_season = season
    if resolved_season is None:
        seasons_in_tree = {
            int(f["season"]) for f in (
                entry_season(e) for e in entries or [] if not e.get("is_dir")
            ) if f
        }
        if len(seasons_in_tree) == 1:
            resolved_season = seasons_in_tree.pop()
    total = None
    if work_id and resolved_season:
        total = season_total(work_id, int(resolved_season), lookup)
    if not total:
        total = season_total_hint
    plan = build_plan(
        entries,
        work_id=work_id,
        season=resolved_season,
        season_total=total,
        treat_iso=treat_iso,
    )
    return plan


def entry_season(entry: Dict[str, Any]) -> Optional[int]:
    """单条文件路径里的显式季号（文件名优先，其次目录）。"""
    from .file_parser import MediaFileParser

    path = str(entry.get("path") or "")
    name = path.rsplit("/", 1)[-1]
    season = MediaFileParser.path_season(name)
    if season is None and "/" in path:
        season = MediaFileParser.path_season(path.rsplit("/", 1)[0])
    return season


def infer_season(
        episodes: Iterable[int],
        totals: Dict[int, int],
        explicit: Optional[int] = None,
) -> Optional[int]:
    """无季号时按集号区间反推季。

    只有一个季的区间能**完整容纳**全部集号才算数；能容纳的季不止一个时，
    取集数最小的那个（短季更可能是它自己的集号），拿不准仍返回 ``None``。
    """
    if explicit:
        return int(explicit)
    eps = {int(ep) for ep in (episodes or []) if ep}
    if not eps or not totals:
        return None
    low, high = min(eps), max(eps)
    fits = [
        (total, season) for season, total in totals.items()
        if total and low >= 1 and high <= int(total)
    ]
    if not fits:
        return None
    fits.sort()
    if len(fits) > 1 and fits[0][0] == fits[1][0]:
        return None
    return int(fits[0][1])
