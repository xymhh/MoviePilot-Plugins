"""自动订阅渠道契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable, Iterator, Optional
from app.log import logger
from app.schemas.types import MediaType

from .models import MediaCandidate


def ranking_scan_limit(options: dict[str, Any]) -> int:
    """为榜单过滤预留后续候选，实际新增数量仍由 ``limit`` 控制。"""
    internal_limit = options.get("_candidate_scan_limit")
    if internal_limit is not None:
        try:
            return max(1, min(int(internal_limit), 500))
        except (TypeError, ValueError):
            logger.debug("ranking_scan_limit 分支忽略异常", exc_info=True)
    try:
        limit = max(1, min(int(options.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    return min(500, max(100, limit * 5))


@dataclass
class SubscribeContext:
    owner: Any = None
    event: Optional[Event] = None
    logger: Any = None
    config: dict[str, Any] | None = None
    # 由宿主后端统一构造的搜索代理 URL（含可选认证信息）。
    proxy: Any = None

    def stopped(self) -> bool:
        return bool(self.event and self.event.is_set())

    def proxy_for(self, enabled: Any) -> Any:
        """按渠道开关返回共享搜索代理；未启用时保持直连。"""
        if not bool(enabled):
            return None
        if not self.proxy:
            raise RuntimeError("已启用榜单代理，但搜索渠道代理地址未配置或无效")
        return self.proxy


class SubscribeProvider(ABC):
    provider_id = ""
    provider_name = ""

    @abstractmethod
    def spec(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, options: dict[str, Any], context: SubscribeContext) -> Iterator[Any]:
        raise NotImplementedError

    def has_listening(self, options: dict[str, Any]) -> bool:
        return True


#: 可直接用作 MediaCandidate 关键字的 id 字段；其余解析出的 id 只进 source_meta。
_CANDIDATE_ID_FIELDS = frozenset({"tmdb_id", "douban_id", "bangumi_id", "imdb_id"})


class RankingProvider(SubscribeProvider):
    """榜单类订阅源模板基类：anilist / bangumi / douban / tmdb 共用。

    四家 fetch() 原先各写一份相同的两段代码，这里只抽这两段，榜单遍历
    的外层结构（METHODS 表、缺方法警告、失败继续/返回）仍留给各家：

    * ``_paginate``——while 分页循环：首页失败重抛（由外层 try/except
      记 error 日志后继续/返回），后续页失败记 warning 保留已抓内容，
      空页停、重复页键停、``items[:scan_limit]`` 截断；context 停止时
      返回 None，调用方直接 return。
    * ``_candidate_from_dict``——单条候选解析：标题/年份下限/评分下限、
      id 解析、unique_key 去重、MediaCandidate 组装。
    * ``_resolve_media_type``——豆瓣/TMDB 共用的类型解析与过滤。

    maoyan 与 mikan 不在此列：它们是 service 式 fetch，结构不同。
    """

    def _paginate(
            self,
            fetch_page: Callable[[int], Any],
            scan_limit: int,
            context: SubscribeContext,
            rank_label: str,
            id_attr: str,
    ) -> Optional[list]:
        """分页抓取一个榜单；context 停止时返回 None。"""
        items: list = []
        page = 1
        seen_pages: set = set()
        while len(items) < scan_limit:
            if context.stopped():
                return None
            try:
                page_items = fetch_page(page) or []
            except Exception as error:
                if not items:
                    raise
                logger.warning(f"{rank_label}第 {page} 页抓取失败，保留已抓取内容：{error}")
                break
            if not page_items:
                break
            page_keys = tuple(
                str(getattr(item, id_attr, None) or getattr(item, "title", None) or item)
                for item in page_items
            )
            if page_keys in seen_pages:
                break
            seen_pages.add(page_keys)
            items.extend(page_items)
            page += 1
        return items[:scan_limit]

    @staticmethod
    def _resolve_media_type(
            data: dict[str, Any],
            default_type: str,
            selected_types: set[str],
            limit_type: str,
    ) -> Optional[str]:
        """豆瓣/TMDB 共用的类型解析与过滤；不符合时返回 None。"""
        raw_type = str(data.get("type") or default_type or "").strip().lower()
        movie_types = {"movie", "电影", "film"}
        tv_types = {"tv", "电视剧", "剧集", "teleplay", "series"}
        if hasattr(MediaType, "MOVIE"):
            movie_types.add(str(getattr(MediaType.MOVIE, "value", "")).lower())
        if hasattr(MediaType, "TV"):
            tv_types.add(str(getattr(MediaType.TV, "value", "")).lower())

        mtype = "movie" if raw_type in movie_types else "tv" if raw_type in tv_types else default_type
        if selected_types and "all" not in selected_types:
            if mtype == "movie" and not ({"movie", "anime_movie"} & selected_types):
                return None
            if mtype == "tv" and not ({"tv", "anime_tv"} & selected_types):
                return None
        elif limit_type and limit_type not in ("all", "全部", "any") and limit_type != mtype:
            return None
        return mtype

    def _candidate_from_dict(
            self,
            data: dict[str, Any],
            *,
            min_year: int,
            min_vote: float,
            seen_keys: set[str],
            media_type: str,
            source: str,
            rank: str,
            rank_key: Optional[str],
            id_fields: tuple[tuple[str, str], ...],
            unique_key_fields: tuple[str, ...],
            meta_extra: Optional[dict[str, Any]] = None,
    ) -> Optional[MediaCandidate]:
        """解析单条候选；应跳过时返回 None。"""
        title = str(data.get("title") or data.get("name") or "").strip()
        if not title:
            return None

        year = str(data.get("year") or "").strip()
        if min_year and year:
            try:
                if int(year) < min_year:
                    return None
            except ValueError:
                logger.debug("fetch 分支忽略异常", exc_info=True)

        vote = data.get("vote_average") or data.get("vote") or 0.0
        try:
            vote_float = float(vote)
            if min_vote and vote_float < min_vote:
                return None
        except (TypeError, ValueError):
            vote_float = 0.0

        ids: dict[str, Any] = {}
        for field_name, kind in id_fields:
            raw = data.get(field_name)
            if kind == "int":
                try:
                    ids[field_name] = int(raw) if raw else None
                except (TypeError, ValueError):
                    ids[field_name] = None
            else:
                ids[field_name] = str(raw or "").strip() or None

        unique_key = f"{next((ids[f] for f in unique_key_fields if ids.get(f)), title)}:{year}"
        if unique_key in seen_keys:
            return None
        seen_keys.add(unique_key)

        source_meta: dict[str, Any] = {"rank": rank}
        if rank_key is not None:
            source_meta["rank_key"] = rank_key
        if meta_extra:
            source_meta.update(meta_extra)
        source_meta["release_date"] = data.get("release_date")
        source_meta["first_air_date"] = data.get("first_air_date")
        # 非 MediaCandidate 字段的 id（如 anilist_id）只进 source_meta。
        for field_name, value in ids.items():
            if field_name not in _CANDIDATE_ID_FIELDS:
                source_meta[field_name] = value

        candidate_kwargs = {
            field_name: value
            for field_name, value in ids.items()
            if field_name in _CANDIDATE_ID_FIELDS
        }
        return MediaCandidate(
            title=title,
            year=year or None,
            media_type=media_type,
            source=source,
            source_meta=source_meta,
            vote_average=vote_float,
            unique_seed=unique_key,
            **candidate_kwargs,
        )
