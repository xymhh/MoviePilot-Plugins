"""智能体搜索与媒体识别（v1.24.0 从 platform.py 拆出）。

PlatformIntegrationService 的智能体侧 mixin：订阅/标题匹配、媒体识别
（经 core/services/media_resolve.py 收敛）、候选搜索与选择。
方法体逐行原样搬移，行为不变。"""

import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType
from app.utils.string import StringUtils
from ...core.media import (
    get_subscribe_by_media,
    legacy_media_ids,
    media_identity,
    recognize_media,
)
from .media_resolve import build_recognition_meta, recognize_once


class PlatformAgentMixin:
    @staticmethod
    def _agent_cache_key(session_id: str, search_id: str) -> str:
        return f"{str(session_id or 'unknown')}:{str(search_id or '').strip()}"

    @staticmethod
    def _normalize_agent_title(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    @staticmethod
    def _strip_leading_decorations(value: Any) -> str:
        """去掉标题开头的装饰符号/emoji（如 🗄 📁），保留剧名本身。

        开头的 emoji 会毒化 TMDB 标题搜索（带 🗄 的标题查不到候选），
        订阅标题匹配也会因此失败；去掉后为空则保留原串。
        """
        text = str(value or "")
        stripped = re.sub(r"^[^\w]+", "", text)
        return stripped if stripped.strip() else text

    def _candidate_for_tmdb_id(self, tmdb_id: Any, media_type: str,
                               title: str) -> Optional[Dict[str, Any]]:
        """按明确 TMDB ID 直接取媒体候选，不走标题搜索。

        供 MP 搜索下载用：搜索时已按关键词唯一命中 TMDB，提交时直接
        信它，跳过标题搜索、多媒体检测与分享预览（那些分支都在
        ``selected_candidate is None`` 时才跑）。识别失败或返回的
        TMDB ID 不符时返回 None，调用方走原标题流程。
        """
        try:
            tmdb_id = int(tmdb_id or 0)
        except (TypeError, ValueError):
            return None
        if tmdb_id <= 0:
            return None
        requested_type = str(media_type or "").strip().lower()
        mtype = MediaType.MOVIE if requested_type == "movie" else MediaType.TV
        clean_title = self._strip_leading_decorations(title)
        try:
            mediainfo = recognize_media(
                self.chain,
                meta=MetaInfo(clean_title or str(tmdb_id)),
                mtype=mtype,
                tmdb_id=tmdb_id,
                cache=True,
            )
        except Exception as error:  # noqa: BLE001 - 识别失败走原标题流程
            logger.debug(f"按 TMDB ID 识别媒体失败：{error}", exc_info=True)
            return None
        if not mediainfo:
            return None
        try:
            resolved_id = int(getattr(mediainfo, "tmdb_id", 0) or 0)
        except (TypeError, ValueError):
            resolved_id = 0
        if resolved_id != tmdb_id:
            return None
        resolved_type = getattr(mediainfo, "type", None)
        candidate_type = (
            "movie" if resolved_type == MediaType.MOVIE
            else "tv" if resolved_type == MediaType.TV
            else requested_type or "tv"
        )
        return {
            "tmdb_id": tmdb_id,
            "media_type": candidate_type,
            "title": str(getattr(mediainfo, "title", None) or clean_title or ""),
            "original_title": str(getattr(mediainfo, "original_title", "") or ""),
            "year": getattr(mediainfo, "year", None),
        }

    @staticmethod
    def _agent_media_type(value: Any) -> Optional[MediaType]:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        if normalized in {"movie", MediaType.MOVIE.value.lower()}:
            return MediaType.MOVIE
        if normalized in {"tv", MediaType.TV.value.lower()}:
            return MediaType.TV
        return None

    @staticmethod
    def _subscribe_identity(subscribe: Any) -> Tuple[Any, ...]:
        source = str(getattr(subscribe, "media_source", "") or "").strip()
        media_id = str(getattr(subscribe, "media_id", "") or "").strip()
        if source and media_id:
            return "source", source, media_id
        for field in ("tmdbid", "doubanid", "bangumiid", "anilistid"):
            value = getattr(subscribe, field, None)
            if value not in (None, ""):
                return field, str(value)
        return (
            "title",
            PlatformAgentMixin._normalize_agent_title(
                getattr(subscribe, "name", "")
            ),
            str(getattr(subscribe, "year", "") or ""),
        )

    def _match_agent_subscribe(
            self,
            subscribe_id: Optional[int],
            title: str,
            media_type: Optional[MediaType],
            season: Optional[int],
            latest_season: bool,
    ) -> Tuple[Any, Optional[str]]:
        oper = SubscribeOper()
        subscribe = oper.get(int(subscribe_id)) if subscribe_id else None
        if subscribe_id and not subscribe:
            return None, f"未找到订阅 ID {subscribe_id}"

        normalized_title = self._normalize_agent_title(title)
        if subscribe and normalized_title and normalized_title != self._normalize_agent_title(
                getattr(subscribe, "name", "")
        ):
            return None, "媒体名称与指定订阅不一致"
        if subscribe and media_type and self._agent_media_type(
                getattr(subscribe, "type", None)
        ) != media_type:
            return None, "媒体类型与指定订阅不一致"
        if subscribe and season is not None:
            if int(getattr(subscribe, "season", 0) or 0) == int(season):
                return subscribe, None
            return None, f"指定订阅不对应第 {season} 季"
        if subscribe:
            return subscribe, None

        subscribes = []
        if normalized_title:
            subscribes = [
                item for item in (oper.list() or [])
                if self._normalize_agent_title(getattr(item, "name", ""))
                   == normalized_title
                   and (
                           not media_type
                           or self._agent_media_type(getattr(item, "type", None)) == media_type
                   )
            ]
            identities = {self._subscribe_identity(item) for item in subscribes}
            if len(identities) > 1:
                subscribes = []

        if not subscribes:
            return subscribe, None
        if season is not None:
            matched = [
                item for item in subscribes
                if int(getattr(item, "season", 0) or 0) == int(season)
            ]
            if matched:
                return matched[0], None
            return None, None
        return subscribes[0], None

    @staticmethod
    def _latest_media_season(mediainfo: Any) -> int:
        seasons = []
        for value in (getattr(mediainfo, "seasons", None) or {}).keys():
            try:
                season = int(value)
            except (TypeError, ValueError):
                continue
            if season > 0:
                seasons.append(season)
        if seasons:
            return max(seasons)
        return max(1, int(getattr(mediainfo, "number_of_seasons", 0) or 1))

    def _match_recognized_subscribe(
            self,
            mediainfo: Any,
            media_type: MediaType,
            season: Optional[int],
    ) -> Any:
        return get_subscribe_by_media(
            SubscribeOper(),
            media_type=media_type.value,
            season=season if media_type == MediaType.TV else None,
            media=mediainfo,
        )

    def _recognize_subscribe_media(self, subscribe: Any):
        media_type = (
            MediaType.MOVIE
            if str(getattr(subscribe, "type", "")) == MediaType.MOVIE.value
            else MediaType.TV
        )
        season = int(getattr(subscribe, "season", 0) or 1) if media_type == MediaType.TV else None
        meta = build_recognition_meta(
            str(getattr(subscribe, "name", "") or ""),
            media_type,
            year=getattr(subscribe, "year", None),
        )
        source, media_id = media_identity(subscribe)
        legacy_ids = legacy_media_ids(subscribe)
        mediainfo = recognize_once(
            self._sync_handler._recognize_media_once,
            getattr(self, "chain", None),
            (
                "agent",
                media_type.value,
                source,
                media_id,
                getattr(subscribe, "name", None),
                getattr(subscribe, "year", None),
                season or 0,
            ),
            meta=meta,
            mtype=media_type,
            media_source=source,
            media_id=media_id,
            **legacy_ids,
            cache=True,
        )
        return mediainfo, media_type, season

    def _recognize_agent_media(
            self,
            subscribe: Any,
            title: str,
            media_type: Optional[MediaType],
            season: Optional[int],
            latest_season: bool,
    ) -> Tuple[Any, Optional[MediaType], Optional[int]]:
        if subscribe:
            mediainfo, resolved_type, resolved_season = (
                self._recognize_subscribe_media(subscribe)
            )
            if mediainfo and resolved_type == MediaType.TV:
                if latest_season:
                    resolved_season = self._latest_media_season(mediainfo)
                elif season is not None:
                    resolved_season = season
            return mediainfo, resolved_type, resolved_season

        meta = build_recognition_meta(title)
        if media_type:
            meta.type = media_type
        if season is not None:
            meta.begin_season = season
        mediainfo = recognize_once(
            self._sync_handler._recognize_media_once,
            getattr(self, "chain", None),
            ("agent", media_type.value if media_type else "", title, season or 0),
            meta=meta,
            mtype=media_type,
            cache=True,
        )
        if not mediainfo:
            return None, media_type, season
        resolved_type = self._agent_media_type(getattr(mediainfo, "type", None))
        if not resolved_type:
            return mediainfo, None, season
        if media_type and resolved_type != media_type:
            return None, resolved_type, season
        if resolved_type == MediaType.MOVIE:
            return mediainfo, resolved_type, None
        resolved_season = season
        if latest_season:
            resolved_season = self._latest_media_season(mediainfo)
        if resolved_season is None:
            resolved_season = int(
                getattr(meta, "begin_season", 0)
                or getattr(mediainfo, "season", 0)
                or 1
            )
        return mediainfo, resolved_type, resolved_season

    @staticmethod
    def _candidate_reason(resource: Dict[str, Any], index: int) -> List[str]:
        reasons = []
        if index == 0:
            reasons.append("当前平台规则排序第一")
        source_priority = int(resource.get("source_priority") or 0)
        if source_priority:
            reasons.append(f"搜索源优先级第 {source_priority} 位")
        if resource.get("is_official"):
            reasons.append("官方或官组资源")
        priority = int(resource.get("platform_priority") or 0)
        if priority:
            reasons.append(f"规则优先级 {priority}")
        if not resource.get("need_unlock"):
            reasons.append("无需积分解锁")
        elif int(resource.get("unlock_points") or 0) > 0:
            reasons.append(f"需要 {int(resource.get('unlock_points') or 0)} 积分解锁")
        definition = str(
            resource.get("resolution") or resource.get("quality") or ""
        ).strip()
        if definition:
            reasons.append(f"清晰度 {definition}")
        if resource.get("update_time"):
            reasons.append(f"更新时间 {resource.get('update_time')}")
        return reasons or ["已通过订阅规则筛选"]

    def search_platform_resources(
            self,
            session_id: str,
            subscribe_id: Optional[int] = None,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            latest_season: bool = False,
            limit: int = 20,
    ) -> Dict[str, Any]:
        """按订阅或媒体名称搜索候选，并保存完整结果供智能体分步选择。"""
        started = time.monotonic()
        title = str(title or "").strip()
        suffix = re.search(r"\s*最新(?:一)?季\s*$", title)
        if suffix:
            latest_season = True
            title = title[:suffix.start()].strip()
        if not subscribe_id and not title:
            return {"success": False, "message": "请提供媒体名称或订阅 ID"}
        if season is not None and latest_season:
            return {"success": False, "message": "指定季号与最新季不能同时使用"}
        requested_type = self._agent_media_type(media_type)
        if media_type and not requested_type:
            return {
                "success": False,
                "message": "媒体类型仅支持 movie（电影）或 tv（电视剧）",
            }
        if not requested_type and (season is not None or latest_season):
            requested_type = MediaType.TV
        try:
            if not self._search_handler:
                return {"success": False, "message": "搜索服务尚未就绪"}
            subscribe, match_error = self._match_agent_subscribe(
                subscribe_id=subscribe_id,
                title=title,
                media_type=requested_type,
                season=season,
                latest_season=latest_season,
            )
            if match_error:
                return {"success": False, "message": match_error}
            if subscribe and not title:
                title = str(getattr(subscribe, "name", "") or "").strip()
            mediainfo, resolved_type, resolved_season = self._recognize_agent_media(
                subscribe=subscribe,
                title=title,
                media_type=requested_type,
                season=season,
                latest_season=latest_season,
            )
            if not mediainfo:
                return {"success": False, "message": f"无法识别媒体：{title}"}
            if resolved_type not in {MediaType.MOVIE, MediaType.TV}:
                return {"success": False, "message": "仅支持电影或电视剧资源搜索"}
            if resolved_type == MediaType.MOVIE and (season or latest_season):
                return {"success": False, "message": "电影不支持季号或最新季参数"}
            subscribe_type = self._agent_media_type(
                getattr(subscribe, "type", None)
            ) if subscribe else None
            subscribe_season = int(
                getattr(subscribe, "season", 0) or 0
            ) if subscribe_type == MediaType.TV else None
            if (
                    subscribe_type != resolved_type
                    or subscribe_season != resolved_season
            ):
                subscribe = self._match_recognized_subscribe(
                    mediainfo,
                    resolved_type,
                    resolved_season,
                )
            sources = self._search_handler.get_enabled_sources()
            if not sources:
                return {"success": False, "message": "没有可用的搜索源"}
            source_results = self._search_handler.search_sources(
                sources=sources,
                mediainfo=mediainfo,
                media_type=resolved_type,
                season=resolved_season,
                subscribe=subscribe,
            )
            resources = []
            for source_priority, source in enumerate(sources, start=1):
                for source_position, resource in enumerate(
                        source_results.get(source) or [], start=1
                ):
                    item = dict(resource)
                    item.setdefault("source", source)
                    item["source_priority"] = source_priority
                    item["source_position"] = source_position
                    resources.append(item)
            resources = resources[: max(1, min(int(limit or 20), 50))]
        except Exception as error:
            logger.error(f"智能体搜索网盘资源失败：{error}")
            return {"success": False, "message": "搜索资源失败，请检查媒体信息和搜索源配置"}

        candidates = []
        cached_resources = {}
        bound_subscribe_id = int(getattr(subscribe, "id", 0) or 0) or None
        available_count = 0
        transferable_count = 0
        unlock_count = 0
        official_count = 0
        free_count = 0
        total_size = 0
        latest_update_time = ""
        source_counts: Counter = Counter()
        resource_type_counts: Counter = Counter()
        for index, resource in enumerate(resources):
            candidate_id = f"r{index + 1:03d}"
            item = dict(resource)
            item["candidate_id"] = candidate_id
            cached_resources[candidate_id] = item
            available = bool(
                str(item.get("url") or "").strip()
                or (
                        item.get("need_unlock")
                        and item.get("resource_ref")
                )
            )
            candidate = {
                "candidate_id": candidate_id,
                "title": str(item.get("title") or "未知资源"),
                "source": str(item.get("source") or "unknown"),
                "rank": index + 1,
                "source_priority": int(item.get("source_priority") or 0),
                "source_position": int(item.get("source_position") or 0),
                "resource_type": str(
                    item.get("resource_type") or item.get("pan_type") or "unknown"
                ).lower(),
                "size": item.get("size") or 0,
                "resolution": item.get("resolution") or "",
                "quality": item.get("quality") or "",
                "update_time": item.get("update_time") or "",
                "platform_priority": int(item.get("platform_priority") or 0),
                "is_official": bool(item.get("is_official")),
                "need_unlock": bool(item.get("need_unlock")),
                "unlock_points": int(item.get("unlock_points") or 0),
                "available": available,
                "transferable": bool(bound_subscribe_id and available),
                "recommendation_reasons": self._candidate_reason(item, index),
            }
            candidates.append(candidate)
            available_count += available
            transferable_count += candidate["transferable"]
            unlock_count += candidate["need_unlock"]
            official_count += candidate["is_official"]
            free_count += not candidate["need_unlock"]
            total_size += max(
                0, int(StringUtils.num_filesize(candidate["size"]) or 0)
            )
            latest_update_time = max(
                latest_update_time, str(candidate.get("update_time") or "")
            )
            source_counts[candidate["source"]] += 1
            resource_type_counts[candidate["resource_type"]] += 1

        search_id = uuid4().hex[:12]
        media = {
            "subscribe_id": bound_subscribe_id,
            "title": str(getattr(mediainfo, "title", "") or title),
            "year": getattr(mediainfo, "year", None),
            "type": resolved_type.value,
            "season": resolved_season,
        }
        cache_key = self._agent_cache_key(session_id, search_id)
        self._agent_resource_cache[cache_key] = {
            "subscribe_id": bound_subscribe_id,
            "media": media,
            "resources": cached_resources,
        }
        summary = {
            "total": len(candidates),
            "available": available_count,
            "transferable": transferable_count,
            "need_unlock": unlock_count,
            "official": official_count,
            "free": free_count,
            "total_size_bytes": total_size,
            "average_size_bytes": total_size // len(candidates) if candidates else 0,
            "latest_update_time": latest_update_time,
            "by_source": dict(source_counts),
            "by_resource_type": dict(resource_type_counts),
        }
        logger.info(
            f"智能体资源搜索完成：{media['title']}"
            f"{f' S{resolved_season:02d}' if resolved_season else ''}，"
            f"订阅 {bound_subscribe_id or '未绑定'}，候选 {len(candidates)} 个，"
            f"耗时 {int((time.monotonic() - started) * 1000)} ms"
        )
        return {
            "success": True,
            "message": "资源搜索完成",
            "search_id": search_id,
            "media": media,
            "summary": summary,
            "source_priority_order": sources,
            "sort_rule": (
                "先按配置的搜索源优先级，再按资源类型优先级、MoviePilot规则优先级、"
                "官组、可直接访问状态、更新时间和解锁积分保持稳定顺序"
            ),
            "recommended_candidate_ids": [
                item["candidate_id"] for item in available[:3]
            ],
            "candidates": candidates,
            "next_step": (
                    "请使用中文汇总候选，并结合规则优先级、官组、清晰度、"
                    "资源大小、更新时间和解锁成本说明推荐理由。"
                    + (
                        "用户需要手动选择时，优先调用 ask_user_choice 展示候选 ID；"
                        "收到选择后使用 search_id 调用 cloudsubscribe_select_resources。"
                        if bound_subscribe_id else
                        "本次搜索未绑定现有订阅，只能展示和推荐；需要转存时先创建或选择订阅，"
                        "再使用订阅 ID 重新搜索。"
                    )
                    + "不要自行构造或改写资源链接。"
            ),
        }

    def select_platform_resources(
            self,
            session_id: str,
            search_id: str,
            candidate_ids: List[str],
    ) -> Dict[str, Any]:
        """只从当前会话缓存取回已搜索链接并提交，禁止模型直接拼接链接。"""
        cache_key = self._agent_cache_key(session_id, search_id)
        cached = self._agent_resource_cache.get(cache_key)
        if not isinstance(cached, dict):
            return {"success": False, "message": "候选资源已过期，请重新搜索"}
        subscribe_id = int(cached.get("subscribe_id") or 0)
        if subscribe_id <= 0:
            return {
                "success": False,
                "message": "本次搜索未关联现有订阅，请先创建或选择订阅后重新搜索",
            }
        resources = cached.get("resources") or {}
        selected = []
        invalid = []
        for candidate_id in dict.fromkeys(str(value).strip() for value in candidate_ids):
            resource = resources.get(candidate_id)
            usable = bool(
                str((resource or {}).get("url") or "").strip()
                or (
                        (resource or {}).get("need_unlock")
                        and (resource or {}).get("resource_ref")
                )
            )
            if not usable:
                invalid.append(candidate_id)
                continue
            selected.append(dict(resource))
        if invalid:
            return {
                "success": False,
                "message": f"候选不可用或不可直接提交：{', '.join(invalid)}",
            }
        if not selected:
            return {"success": False, "message": "没有选择可提交的候选资源"}
        result = dict(self.start_selected_resources(int(subscribe_id), selected))
        data = dict(result.get("data") or {})
        data["candidate_ids"] = list(dict.fromkeys(candidate_ids))
        result["data"] = data
        return result
