"""链接提交与工作流（v1.24.0 从 platform.py 拆出）。

PlatformIntegrationService 的链接侧 mixin：手动链接识别、链接提交
（含多媒体分发与 TMDB 候选选择）、链接媒体预览/推断、工作流入口。
方法体逐行原样搬移，行为不变。"""

import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType
from ...core.media import (
    tmdb_id_of,
)
from ...search.types import (
    PREVIEW_PROVIDER_KEYS,
    resource_type_from_url,
    resource_type_name,
)
from .platform_agent import PlatformAgentMixin


class PlatformLinkMixin:
    def api_vue_resolve_manual_links(self, payload: Dict[str, Any]) -> dict:
        """只读识别手动资源，统一返回订阅、TMDB 候选与资源季。"""
        payload = dict(payload or {})
        links = self.extract_resource_links(payload.get("resource_links"))
        cloud_path = str(payload.get("cloud_path") or "").strip()
        if not links and not cloud_path:
            return {"success": False, "message": "请提供有效资源链接或网盘路径"}
        title = str(payload.get("title") or "").strip()
        requested_type = str(payload.get("media_type") or "").strip().lower()
        try:
            requested_tmdb_id = int(payload.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "TMDB ID 格式错误"}
        preview = self._preview_link_media(links) if links else {}
        recognized_title = (
                title
                or str(preview.get("title") or "").strip()
                or self._link_media_title(links)
        )
        if not recognized_title:
            return {"success": False, "message": "未能从分享内容识别媒体名称"}
        media_type = requested_type or str(preview.get("media_type") or "").strip().lower()
        if media_type and media_type not in {"movie", "tv"}:
            return {"success": False, "message": "媒体类型仅支持 movie 或 tv"}
        season_values = set(self._link_title_seasons(recognized_title))
        season_values.update({
            int(value) for value in preview.get("seasons") or []
            if int(value) > 0
        })
        for link in links:
            season_values.update(self._link_title_seasons(self._link_media_title([link])))
        seasons = sorted(season_values)
        matched = (
            self._find_link_subscribe(
                recognized_title,
                media_type,
                seasons[0] if len(seasons) == 1 else None,
                requested_tmdb_id or None,
            )
            if len(seasons) <= 1 else None
        )
        candidates = []
        if not matched:
            search_result = self.api_vue_search_tmdb_candidates({
                "title": recognized_title,
                "tmdb_id": requested_tmdb_id or None,
                "media_type": media_type or None,
            })
            if not search_result.get("success"):
                return search_result
            candidates = list((search_result.get("data") or {}).get("items") or [])
            if media_type:
                candidates = [item for item in candidates if item.get("media_type") == media_type]
        resolved_media = (
            self._link_subscribe_media(matched)
            if matched else candidates[0] if len(candidates) == 1 else None
        )
        available_seasons = []
        if resolved_media and resolved_media.get("media_type") == "tv":
            detail_result = self.api_vue_search_tmdb_candidates({
                "title": resolved_media.get("title") or recognized_title,
                "original_title": resolved_media.get("original_title") or "",
                "year": resolved_media.get("year"),
                "tmdb_id": resolved_media.get("tmdb_id"),
                "media_type": "tv",
            })
            detail_data = detail_result.get("data") or {}
            available_seasons = list(detail_data.get("seasons") or [])
            if not matched and detail_data.get("items"):
                candidates = list(detail_data["items"])
        selected_seasons = (
            [value for value in seasons if value in set(available_seasons)]
            if available_seasons else seasons
        )
        return {
            "success": True,
            "message": "已定位订阅" if matched else "已定位 TMDB 候选",
            "data": {
                "title": recognized_title,
                "media_type": media_type,
                "seasons": selected_seasons,
                "detected_seasons": seasons,
                "available_seasons": available_seasons,
                "subscribe_id": int(getattr(matched, "id", 0) or 0) if matched else None,
                "candidates": candidates,
            },
        }

    def submit_platform_links(
            self,
            subscribe_id: Optional[int] = None,
            resource_links: Any = None,
            wait: bool = False,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            seasons: Optional[List[int]] = None,
            selection_id: str = "",
            tmdb_id: Optional[int] = None,
            selection_scope: str = "",
            detect_multiple: bool = True,
    ) -> Dict[str, Any]:
        """提交链接；无有效订阅时先完成 TMDB 快速识别与选择。"""
        try:
            normalized_seasons = sorted({
                int(value) for value in (seasons or [])
                if int(value) > 0
            })
            if season is not None and not normalized_seasons:
                normalized_seasons = [int(season)]
        except (TypeError, ValueError):
            return {"success": False, "message": "季数格式错误"}
        season = normalized_seasons[0] if normalized_seasons else season
        try:
            normalized_subscribe_id = int(subscribe_id or 0)
        except (TypeError, ValueError):
            normalized_subscribe_id = 0
        if normalized_subscribe_id > 0:
            subscribe = SubscribeOper().get(normalized_subscribe_id)
            if subscribe:
                return self.api_vue_start_manual_sync({
                    "subscribe_id": normalized_subscribe_id,
                    "resource_links": self.extract_resource_links(resource_links),
                }, wait=wait)
            if not str(title or "").strip():
                title = str(normalized_subscribe_id)

        normalized_selection_id = str(selection_id or "").strip()
        requested_selection_type = str(media_type or "").strip().lower()
        selected_candidate = None
        if normalized_selection_id:
            cached = self._link_selection_cache.get(normalized_selection_id)
            if not isinstance(cached, dict):
                return {"success": False, "message": "TMDB 候选已过期，请重新提交链接识别"}
            cached_scope = str(cached.get("scope") or "")
            if cached_scope and cached_scope != str(selection_scope or ""):
                return {"success": False, "message": "TMDB 候选不属于当前会话"}
            try:
                selected_tmdb_id = int(tmdb_id or 0)
            except (TypeError, ValueError):
                selected_tmdb_id = 0
            if selected_tmdb_id <= 0:
                return {"success": False, "message": "请选择有效的 TMDB ID"}
            matched_candidates = [
                item for item in cached.get("candidates", [])
                if int(item.get("tmdb_id") or 0) == selected_tmdb_id
                   and (
                           not requested_selection_type
                           or item.get("media_type") == requested_selection_type
                   )
            ]
            if len(matched_candidates) > 1:
                return {"success": False, "message": "TMDB ID 同时匹配电影和电视剧，请指定媒体类型"}
            selected_candidate = matched_candidates[0] if matched_candidates else None
            if selected_candidate is None:
                return {"success": False, "message": "所选 TMDB ID 不在待选候选中"}
            links = list(cached.get("links") or [])
            title = str(cached.get("title") or "")
            media_type = str(cached.get("media_type") or "")
            season = cached.get("season")
            normalized_seasons = [
                int(value) for value in cached.get("seasons") or []
            ]
            if season is not None and not normalized_seasons:
                normalized_seasons = [int(season)]
        else:
            links = self.extract_resource_links(resource_links)
        if not links:
            return {"success": False, "message": "请至少提供一个有效资源链接"}

        # 调用方给了明确 TMDB ID（MP 搜索下载：搜索时已按关键词唯一命中），
        # 直接按它取候选，不走标题搜索。候选一定下来，多媒体检测、分享
        # 预览与标题搜索都在 selected_candidate is None 的分支里，全部跳过。
        # 识别失败时 selected_candidate 仍为空，走原标题流程。
        if selected_candidate is None:
            try:
                requested_tmdb_id = int(tmdb_id or 0)
            except (TypeError, ValueError):
                requested_tmdb_id = 0
            if requested_tmdb_id > 0:
                selected_candidate = self._candidate_for_tmdb_id(
                    requested_tmdb_id, media_type, title
                )

        # 一个分享可能包含多个媒体。无显式标题时按文件名分组，分别走现有
        # TMDB/订阅匹配和整理流程，避免把整包错误归到出现次数最多的媒体。
        detected = (
            self._preview_link_media_candidates(links)
            if selected_candidate is None and detect_multiple else []
        )
        title_key = self._normalize_agent_title(title)
        detected_keys = {
            self._normalize_agent_title(item.get("title")) for item in detected
        }
        if (
                selected_candidate is None
                and len(detected) > 1
                and (not title_key or title_key not in detected_keys)
        ):
            results = []
            for item in detected:
                item_result = self.submit_platform_links(
                    subscribe_id=subscribe_id,
                    resource_links=links,
                    wait=wait,
                    title=str(item.get("title") or "").strip(),
                    media_type=str(item.get("media_type") or "").strip(),
                    seasons=list(item.get("seasons") or []),
                    selection_scope=selection_scope,
                    detect_multiple=False,
                )
                results.append(item_result)
            success = all(bool(item.get("success")) for item in results)
            messages = [str(item.get("message") or "") for item in results]
            return {
                "success": success,
                "message": "；".join(value for value in messages if value)
                           or ("多媒体资源已提交" if success else "部分媒体提交失败"),
                "data": {
                    "media": [
                        dict((item.get("data") or {}).get("media") or {})
                        for item in results
                    ],
                    "results": results,
                },
            }

        if selected_candidate is None:
            preview = detected[0] if detected else self._preview_link_media(links)
            link_title = str(preview.get("title") or "").strip()
            if link_title:
                recognized_title = link_title
                if preview.get("year") and str(preview["year"]) not in recognized_title:
                    recognized_title = f"{recognized_title} ({preview['year']})"
                if not media_type:
                    media_type = str(preview.get("media_type") or "")
                if season is None:
                    season = preview.get("season")
                    if season is None and preview.get("seasons"):
                        try:
                            season = next(iter(sorted(preview["seasons"])))
                        except (TypeError, ValueError, StopIteration):
                            season = None
                if not normalized_seasons and preview.get("seasons"):
                    normalized_seasons = sorted({
                        int(value) for value in preview["seasons"] if int(value) > 0
                    })
                if str(title or "").strip() and str(title or "").strip() != recognized_title:
                    logger.info(
                        f"分享内容识别到有效媒体，优先采用链接内容：'{recognized_title}'"
                        f"（忽略消息附带文本 '{str(title).strip()}'）"
                    )
            else:
                recognized_title = str(title or "").strip() or self._link_media_title(links)

            if not recognized_title:
                return {
                    "success": False,
                    "message": "未能从分享内容识别媒体名称，请在链接前补充名称后重试",
                }
            # 标题开头的装饰符号/emoji 会毒化 TMDB 标题搜索与订阅标题匹配，
            # 先去掉（预览标题与传入标题都可能带，如 🗄）。
            recognized_title = self._strip_leading_decorations(recognized_title)
            title = recognized_title
            normalized_type = str(media_type or "").strip().lower()
            if normalized_type and normalized_type not in {"movie", "tv"}:
                return {"success": False, "message": "媒体类型仅支持 movie 或 tv"}
            matched_subscribe = (
                self._find_link_subscribe(
                    recognized_title,
                    normalized_type,
                    season,
                )
                if len(normalized_seasons) <= 1 else None
            )
            if matched_subscribe:
                result = self.api_vue_start_manual_sync({
                    "subscribe_id": int(getattr(matched_subscribe, "id", 0) or 0),
                    "resource_links": links,
                }, wait=wait)
                data = dict(result.get("data") or {})
                data["matched_subscribe_id"] = int(
                    getattr(matched_subscribe, "id", 0) or 0
                )
                data["media"] = self._link_subscribe_media(matched_subscribe)
                result["data"] = data
                logger.info(
                    f"分享内容已定位订阅：标题={recognized_title}，"
                    f"订阅={getattr(matched_subscribe, 'name', '')}，"
                    f"季={getattr(matched_subscribe, 'season', '') or '电影'}"
                )
                return result
            search_result = self.api_vue_search_tmdb_candidates({"title": recognized_title})
            if not search_result.get("success"):
                return search_result
            candidates = list((search_result.get("data") or {}).get("items") or [])
            if normalized_type:
                candidates = [
                    item for item in candidates
                    if item.get("media_type") == normalized_type
                ]
            if not candidates:
                return {"success": False, "message": f"未识别到“{recognized_title}”的 TMDB 媒体"}
            logger.info(
                f"分享内容 TMDB 匹配：标题={recognized_title}，"
                f"类型={normalized_type or '不限'}，候选={len(candidates)}"
            )
            try:
                selected_tmdb_id = int(tmdb_id or 0)
            except (TypeError, ValueError):
                selected_tmdb_id = 0
            if selected_tmdb_id:
                selected_candidate = next(
                    (
                        item for item in candidates
                        if int(item.get("tmdb_id") or 0) == selected_tmdb_id
                    ),
                    None,
                )
                if not selected_candidate:
                    return {"success": False, "message": "指定 TMDB ID 与识别结果不匹配"}
            elif len(candidates) == 1:
                selected_candidate = candidates[0]
            else:
                next_selection_id = uuid4().hex[:12]
                self._link_selection_cache[next_selection_id] = {
                    "scope": str(selection_scope or ""),
                    "links": links,
                    "title": recognized_title,
                    "media_type": normalized_type,
                    "season": season,
                    "seasons": normalized_seasons,
                    "candidates": candidates,
                }
                return {
                    "success": False,
                    "message": f"“{recognized_title}”匹配到多个 TMDB 媒体，请先选择",
                    "data": {
                        "selection_required": True,
                        "selection_id": next_selection_id,
                        "candidates": candidates,
                        "next_step": "请选择候选的 media_type 与 tmdb_id 后继续提交",
                    },
                }

        matched_subscribe = (
            self._find_link_subscribe(
                str(selected_candidate.get("title") or title),
                str(selected_candidate.get("media_type") or media_type),
                season,
                int(selected_candidate.get("tmdb_id") or 0),
            )
            if not normalized_subscribe_id and len(normalized_seasons) <= 1
            else None
        )
        if matched_subscribe:
            result = self.api_vue_start_manual_sync({
                "subscribe_id": int(getattr(matched_subscribe, "id", 0) or 0),
                "resource_links": links,
            }, wait=wait)
            data = dict(result.get("data") or {})
            data["matched_subscribe_id"] = int(
                getattr(matched_subscribe, "id", 0) or 0
            )
            data["media"] = self._link_subscribe_media(matched_subscribe)
            result["data"] = data
            return result
        media = self._link_media_payload(
            selected_candidate,
            source_title=title,
            season=season,
            seasons=normalized_seasons,
        )
        result = self.api_vue_start_manual_sync({
            "resource_links": links,
            "media": media,
        }, wait=wait)
        logger.info(
            f"分享资源转存提交：媒体={media.get('title') or title}，"
            f"类型={media.get('media_type') or '未知'}，链接数={len(links)}，"
            f"成功={bool(result.get('success'))}"
        )
        if result.get("success") and normalized_selection_id:
            try:
                del self._link_selection_cache[normalized_selection_id]
            except KeyError:
                logger.debug("submit_platform_links 分支忽略异常", exc_info=True)
        data = dict(result.get("data") or {})
        data.setdefault("media", media)
        result["data"] = data
        return result

    @staticmethod
    def _link_media_title(links: List[str]) -> str:
        """从 ED2K 文件名或 Magnet dn 中提取可供平台识别的标题。"""
        for link in links:
            lowered = str(link or "").lower()
            if lowered.startswith("ed2k://|file|"):
                parts = str(link).split("|")
                if len(parts) > 2:
                    return unquote(parts[2]).strip()
            if lowered.startswith("magnet:?"):
                values = parse_qs(urlparse(str(link)).query).get("dn") or []
                if values and str(values[0]).strip():
                    return unquote(str(values[0])).strip()
        return ""

    @staticmethod
    def _link_title_seasons(value: Any) -> List[int]:
        """提取资源标题中的明确季范围，供 TMDB 季列表自动预选。"""
        seasons = set()
        pattern = re.compile(
            r"(?<![A-Za-z0-9])(?:S(?:eason)?\s*|第\s*)0*(\d{1,3})"
            r"(?:\s*(?:[-~～至到])\s*(?:S(?:eason)?\s*|第\s*)?0*(\d{1,3}))?"
            r"(?:\s*季)?(?=$|[\s._/\\\-\[\]()Eｅ集])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(str(value or "")):
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if 0 < start <= end <= 999 and end - start <= 100:
                seasons.update(range(start, end + 1))
        return sorted(seasons)

    def _find_link_subscribe(
            self,
            title: str,
            media_type: str = "",
            season: Optional[int] = None,
            tmdb_id: Optional[int] = None,
    ) -> Any:
        """按分享识别结果只复用唯一明确的订阅，避免多季误配。"""
        normalized_title = self._normalize_agent_title(title)
        if not normalized_title and not tmdb_id:
            return None
        normalized_type = str(media_type or "").strip().lower()
        candidates = []
        for subscribe in SubscribeOper().list() or []:
            subscribe_type = self._agent_media_type(getattr(subscribe, "type", None))
            if normalized_type and self._agent_media_type(normalized_type) != subscribe_type:
                continue
            if tmdb_id:
                try:
                    if tmdb_id_of(subscribe) != int(tmdb_id):
                        continue
                except (TypeError, ValueError):
                    continue
            elif self._normalize_agent_title(getattr(subscribe, "name", "")) != normalized_title:
                continue
            if season is not None and subscribe_type == MediaType.TV:
                if int(getattr(subscribe, "season", 0) or 0) != int(season):
                    continue
            candidates.append(subscribe)
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _link_subscribe_media(subscribe: Any) -> Dict[str, Any]:
        media_type = (
            "movie"
            if str(getattr(subscribe, "type", "")) == MediaType.MOVIE.value
            else "tv"
        )
        media = {
            "tmdb_id": tmdb_id_of(subscribe) or 0,
            "media_type": media_type,
            "title": str(getattr(subscribe, "name", "") or "").strip(),
            "year": getattr(subscribe, "year", None),
        }
        if media_type == "tv":
            media["seasons"] = [int(getattr(subscribe, "season", 1) or 1)]
        return media

    def _preview_link_media(self, links: List[str]) -> Dict[str, Any]:
        """从已配置网盘的分享文件名推断媒体名称和季集范围。"""
        candidates = self._preview_link_media_candidates(links)
        return candidates[0] if candidates else {}

    def _preview_link_media_candidates(self, links: List[str]) -> List[Dict[str, Any]]:
        """从分享文件名识别全部媒体，按文件出现次数降序返回。"""
        if not self._sync_handler:
            return []
        for link in links:
            if resource_type_from_url(link) not in PREVIEW_PROVIDER_KEYS:
                continue
            try:
                files = self._sync_handler.preview_resource_files(link)
            except Exception as error:
                logger.warning(f"分享内容快速识别失败：{error}")
                continue
            resource_type = resource_type_from_url(link)
            logger.info(
                f"分享内容预览：类型={resource_type_name(resource_type, '未知')}，"
                f"文件数={len(files)}"
            )
            inferred = self._infer_link_media_candidates(files)
            if inferred:
                for item in inferred:
                    logger.info(
                        f"分享内容识别完成：标题={item.get('title')}，"
                        f"类型={item.get('media_type') or '待 TMDB 判断'}，"
                        f"季={','.join(str(value) for value in item.get('seasons') or []) or '未指定'}"
                    )
                return inferred
            logger.warning(
                f"分享内容未识别到媒体文件名："
                f"类型={resource_type_name(resource_type, '未知')}"
            )
        return []

    @staticmethod
    def _infer_link_media(files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """兼容旧调用：返回分享中出现频率最高的媒体。"""
        candidates = PlatformLinkMixin._infer_link_media_candidates(files)
        return candidates[0] if candidates else {}

    @staticmethod
    def _infer_link_media_candidates(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """聚合分享中的视频文件名，保留全部不同媒体元数据。"""
        scores: Counter = Counter()
        candidates: Dict[str, Dict[str, Any]] = {}
        media_extensions = {
            str(value).lower() for value in (settings.RMT_MEDIAEXT or [])
        }
        for item in files or []:
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("name")
                or item.get("file_name")
                or item.get("filename")
                or item.get("path")
                or ""
            ).strip()
            if not name:
                continue
            file_name = Path(name).name
            if media_extensions and Path(file_name).suffix.lower() not in media_extensions:
                continue
            try:
                meta = MetaInfo(file_name)
            except Exception as error:
                logger.debug(f"分享文件名解析失败：{file_name}，原因：{error}")
                continue
            parsed_title = str(getattr(meta, "name", "") or "").strip()
            if not parsed_title:
                continue
            key = PlatformAgentMixin._normalize_agent_title(parsed_title)
            if not key:
                continue
            scores[key] += 1
            candidate = candidates.setdefault(key, {
                "title": parsed_title,
                "year": getattr(meta, "year", None),
                "media_type": (
                    "tv"
                    if getattr(meta, "begin_season", None) is not None
                       or getattr(meta, "begin_episode", None) is not None
                    else ""
                ),
                "seasons": set(),
            })
            parsed_season = getattr(meta, "begin_season", None)
            if parsed_season is not None and int(parsed_season) > 0:
                candidate["seasons"].add(int(parsed_season))
            for season_context in (
                    item.get("_relative_path"),
                    item.get("path"),
                    item.get("_cloud_dir"),
            ):
                candidate["seasons"].update(
                    PlatformLinkMixin._link_title_seasons(
                        season_context
                    )
                )
        result = []
        for selected_key, _score in scores.most_common():
            selected = dict(candidates[selected_key])
            selected["seasons"] = sorted(selected.get("seasons") or [])
            result.append(selected)
        return result

    @staticmethod
    def _link_media_payload(
            candidate: Dict[str, Any],
            source_title: str = "",
            season: Optional[int] = None,
            seasons: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        media_type = str(candidate.get("media_type") or "").lower()
        media = {
            "tmdb_id": int(candidate.get("tmdb_id") or 0),
            "media_type": media_type,
        }
        if media_type != "tv":
            return media
        resolved_seasons = sorted({
            int(value) for value in (seasons or [])
            if int(value) > 0
        })
        if not resolved_seasons:
            resolved_season = season
            if resolved_season:
                resolved_seasons = [max(1, int(resolved_season))]
        media["seasons"] = resolved_seasons
        return media

    @staticmethod
    def _set_workflow_output(context: Any, name: str, result: Dict[str, Any]) -> Any:
        outputs = dict(getattr(context, "node_outputs", None) or {})
        outputs[name] = dict(result)
        context.node_outputs = outputs
        return context

    def workflow_start_sync(
            self,
            context: Any,
            subscribe_id: int = 0,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            **kwargs,
    ) -> tuple[bool, Any]:
        requested_ids: List[int] = []
        raw_ids: Any = subscribe_ids
        if raw_ids is None and subscribe_id:
            raw_ids = [subscribe_id]
        if raw_ids is None:
            context_subscribes = list(getattr(context, "subscribes", None) or [])
            raw_ids = [getattr(item, "id", 0) for item in context_subscribes] or None
        if raw_ids is not None:
            if not isinstance(raw_ids, (list, tuple, set)):
                raw_ids = [raw_ids]
            try:
                normalized_ids = {int(value) for value in raw_ids}
            except (TypeError, ValueError):
                result = {"success": False, "message": "订阅 ID 参数格式错误"}
                return False, self._set_workflow_output(
                    context, "cloudsubscribe_sync", result
                )
            requested_ids = sorted(value for value in normalized_ids if value > 0)
            if not requested_ids:
                result = {"success": False, "message": "请提供有效的订阅 ID"}
                return False, self._set_workflow_output(
                    context, "cloudsubscribe_sync", result
                )
            with SessionFactory() as db:
                existing_ids = {
                    int(item.id)
                    for item in db.query(Subscribe).filter(
                        Subscribe.id.in_(requested_ids)
                    ).all()
                    if getattr(item, "id", None) is not None
                }
            missing_ids = [value for value in requested_ids if value not in existing_ids]
            if missing_ids:
                result = {
                    "success": False,
                    "message": f"订阅不存在：{', '.join(map(str, missing_ids))}",
                }
                return False, self._set_workflow_output(
                    context, "cloudsubscribe_sync", result
                )
        result: Dict[str, Any] = {}
        self.sync_subscribes(
            subscribe_ids=requested_ids or None,
            subscribe_states=subscribe_states,
            result=result,
        )
        data = dict(result.get("data") or {})
        data.update({
            "scope": (
                "selected" if requested_ids
                else "states" if subscribe_states
                else "all"
            ),
            "subscribe_ids": requested_ids,
            "subscribe_count": len(requested_ids),
            "subscribe_states": subscribe_states,
        })
        result["data"] = data
        return bool(result.get("success")), self._set_workflow_output(
            context, "cloudsubscribe_sync", result
        )

    def workflow_process_links(
            self,
            context: Any,
            subscribe_id: int = 0,
            resource_links: Any = None,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            seasons: Optional[List[int]] = None,
            selection_id: str = "",
            tmdb_id: Optional[int] = None,
            **kwargs,
    ) -> tuple[bool, Any]:
        if not subscribe_id:
            subscribes = list(getattr(context, "subscribes", None) or [])
            if len(subscribes) == 1:
                subscribe_id = int(getattr(subscribes[0], "id", 0) or 0)
        raw_content = (
            resource_links
            if resource_links is not None else getattr(context, "content", "")
        )
        links = self.extract_resource_links(raw_content)
        recognized_title = str(title or "").strip()
        if not recognized_title and resource_links is None:
            recognized_title = str(raw_content or "")
            for link in links:
                recognized_title = recognized_title.replace(link, " ")
            recognized_title = " ".join(recognized_title.split())
        result = self.submit_platform_links(
            subscribe_id=subscribe_id,
            resource_links=links,
            wait=True,
            title=recognized_title,
            media_type=media_type,
            season=season,
            seasons=seasons,
            selection_id=selection_id,
            tmdb_id=tmdb_id,
            selection_scope="workflow",
        )
        return bool(result.get("success")), self._set_workflow_output(
            context, "cloudsubscribe_links", result
        )
