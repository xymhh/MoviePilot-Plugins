"""盘链作品匹配与资源候选构造。"""

import re
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.log import logger

from .client import PinglianClient, PinglianError
from .resource import PinglianResourceService
from ..budget import PointBudgetStatus
from ..magnet import clear_cache, media_titles
from ..matching import extract_year, title_matches, unique_texts
from ..types import (
    RESOURCE_TYPE_ORDER,
    SUPPORTED_RESOURCE_TYPES,
    normalize_resource_type,
)
from ...core.search import SearchQuery, format_search_log_prefix


class PinglianQuotaBudget:
    """盘链每日链接解锁配额预算适配器，接入 CloudSubscribe 统一预算协议。"""

    def __init__(self, service: Any, client: PinglianClient):
        self._service = service
        self._client = client
        self.lock = threading.RLock()

    def cached_url(self, key: str) -> str:
        return self._service.resources.cached_url(key)

    def record_result(self, key: str, url: str, points: int = 1) -> None:
        if url:
            self._service.resources.record_unlocked(key, url)

    def clear_cached_urls(self) -> int:
        return self._service.resources.clear_cache()

    def has_budget(self, points: Any) -> bool:
        return self._service.resources.has_quota()

    def status(self, points: Any) -> PointBudgetStatus:
        quota = self._service.resources.get_link_quota()
        unlimited = bool(quota.get("unlimited"))
        limit = 9999 if unlimited else (int(quota.get("limit")) if quota.get("limit") is not None else 0)
        used = 0 if unlimited else max(0, int(quota.get("used") or 0))
        req = max(1, int(points or 1))
        return PointBudgetStatus(
            requested=req,
            task_spent=used,
            subscribe_spent=0,
            task_limit=limit,
            subscribe_limit=limit,
        )

    def reset_task(self) -> None:
        pass

    def reset_subscription(self, key: str = "") -> None:
        pass

    def clear_subscription(self, key: str) -> None:
        pass

    def clear_history(self) -> int:
        return 0

    def configure_storage(self, get_data: Callable, save_data: Callable) -> None:
        pass


class PinglianSearchService:
    def __init__(
            self,
            client: PinglianClient,
            resource_types: Iterable[str],
            result_limit: int,
    ):
        self._client = client
        self._resource_types = tuple(resource_types)
        self._result_limit = result_limit
        self._budget = PinglianQuotaBudget(self, client)
        self._resources = PinglianResourceService(client)

    @property
    def budget(self) -> PinglianQuotaBudget:
        return self._budget

    @property
    def resources(self) -> PinglianResourceService:
        return self._resources

    @staticmethod
    def _video_title(row: Dict[str, Any]) -> str:
        name = str(row.get("title") or row.get("vod_name") or "").strip()
        row_year = extract_year(row.get("year") or row.get("vod_year"))
        if not name or not row_year:
            return name
        normalized = re.sub(
            rf"[\s（(【\[]*{re.escape(row_year)}[）)】\]]*$", "", name
        ).strip()
        return normalized or name

    @classmethod
    def _select_video(
            cls, rows: Iterable[Dict[str, Any]], titles: List[str], year: str
    ) -> Optional[Dict[str, Any]]:
        best = None
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = cls._video_title(row)
            if not title_matches(name, titles):
                continue
            row_year = extract_year(row.get("year") or row.get("vod_year"))
            if year and row_year and row_year != year:
                continue
            exact = any(name.casefold() == title.casefold() for title in titles)
            score = (200 if exact else 100) + (
                50 if year and row_year == year else 0
            )
            ranked = (score, -index, row)
            if best is None or (score, -index) > best[:2]:
                best = ranked
        return best[2] if best is not None else None

    @staticmethod
    def _round_robin(candidates: List[tuple]) -> List[tuple]:
        grouped = {}
        for candidate in candidates:
            grouped.setdefault(candidate[2], []).append(candidate)
        results = []
        offsets = {resource_type: 0 for resource_type in grouped}
        while grouped:
            for resource_type in list(grouped):
                rows = grouped[resource_type]
                offset = offsets[resource_type]
                results.append(rows[offset])
                offset += 1
                offsets[resource_type] = offset
                if offset >= len(rows):
                    grouped.pop(resource_type)
                    offsets.pop(resource_type, None)
        return results

    def _search(
            self,
            titles: List[str],
            year: Any,
            limit: int,
            resource_list_mode: bool,
            log_prefix: str,
    ) -> List[Dict[str, Any]]:
        titles = unique_texts(titles)
        if not titles:
            return []
        allowed = (
            list(RESOURCE_TYPE_ORDER)
            if resource_list_mode else list(dict.fromkeys(
                normalize_resource_type(value) for value in self._resource_types
                if normalize_resource_type(value) in SUPPORTED_RESOURCE_TYPES
            ))
        )
        if not allowed:
            return []
        expected_year = extract_year(year)
        prefix = str(log_prefix or "[PINGLIAN]")
        video = None
        selected_keyword = ""

        # 1. 搜索影视条目
        for keyword in titles:
            try:
                payload = self._client.request_json(
                    "/api/videos",
                    params={
                        "search": keyword,
                        "sort": "year_desc",
                        "page": 1,
                        "page_size": 20,
                    },
                )
            except PinglianError as error:
                if "已被禁用" in str(error) or error.code in ("ACCOUNT_DISABLED", "pinglian_rate_limited",
                                                              "pinglian_auth_failed"):
                    logger.error(f"{prefix} 盘链账号出现致命异常，立即阻断搜索并触发熔断：{error}")
                    raise
                logger.warning(f"{prefix} 搜索影视条目失败（{keyword}）：{error}")
                continue
            except Exception as error:
                logger.warning(f"{prefix} 搜索影视条目失败（{keyword}）：{error}")
                continue

            data = payload.get("data") if isinstance(payload, dict) else {}
            if isinstance(data, dict):
                rows = data.get("list") or []
            elif isinstance(payload, dict):
                rows = payload.get("list") or []
            else:
                rows = []
            rows = rows if isinstance(rows, list) else []

            logger.debug(f"{prefix} 检索作品：关键词={keyword}，条目={len(rows)}")
            video = self._select_video(rows, titles, expected_year)
            if video:
                selected_keyword = keyword
                break

        if not video:
            logger.debug(f"{prefix} 未选中作品：关键词={','.join(titles)}")
            return []

        vod_id = video.get("id") or video.get("vod_id")
        video_name = self._video_title(video)
        logger.debug(
            f"{prefix} 选中作品：id={vod_id}，标题={video_name}"
        )

        # 2. 获取作品详情及网盘链接列表（带 TTL 缓存）
        detail_data = self._resources.get_video_detail(vod_id)
        if not detail_data or not isinstance(detail_data, dict):
            logger.debug(f"{prefix} 获取作品详情失败或格式异常（id={vod_id}）")
            return []

        raw_links = detail_data.get("links") or []
        if not isinstance(raw_links, list):
            logger.debug(f"{prefix} 作品未包含网盘链接列表")
            return []

        type_order = {value: index for index, value in enumerate(allowed)}
        candidates = []
        raw_link_count = 0
        type_counts: Dict[str, int] = {}
        filtered_type_counts: Dict[str, int] = {}

        for row in raw_links:
            if not isinstance(row, dict):
                continue
            raw_link_count += 1
            link_id = str(row.get("id") or "").strip()
            if not link_id:
                continue

            raw_type = (
                "magnet" if row.get("is_magnet")
                else str(row.get("pan_type") or "")
            )
            resource_type = normalize_resource_type(raw_type)
            if resource_type:
                type_counts[resource_type] = type_counts.get(resource_type, 0) + 1

            if resource_type not in type_order:
                continue

            filtered_type_counts[resource_type] = (
                    filtered_type_counts.get(resource_type, 0) + 1
            )
            candidates.append((
                type_order[resource_type], row, resource_type, link_id
            ))

        logger.debug(
            f"{prefix} 链接过滤：原始链接={raw_link_count}，"
            f"类型={'/'.join(f'{k}={v}' for k, v in type_counts.items()) or '无'}，"
            f"已选类型候选={'/'.join(f'{k}={v}' for k, v in filtered_type_counts.items()) or '无'}，"
            f"可用候选={len(candidates)}"
        )

        candidates.sort(key=lambda item: item[0])
        if resource_list_mode:
            candidates = self._round_robin(candidates)

        results = []
        seen = set()
        resolved_count = 0
        resolve_failed_count = 0
        normalized_limit = max(1, min(int(limit or 20), 80))

        source_url = f"{self._client.base_url}/videos/{vod_id}"

        for _, row, resource_type, link_id in candidates:
            if len(results) >= normalized_limit:
                break
            key = (resource_type, str(row.get("title") or "").strip(), link_id)
            if key in seen:
                continue
            seen.add(key)

            title = str(row.get("title") or video_name or "盘链资源").strip()
            desc_parts = []
            if row.get("username"):
                desc_parts.append(f"分享人: {row.get('username')}")
            if row.get("note"):
                desc_parts.append(str(row.get("note")))
            desc = " | ".join(desc_parts)

            update_time = str(
                row.get("updated_at") or row.get("created_at") or ""
            )

            # 候选资源构造：盘链无免费资源，所有链接在未解锁前均为待解锁状态 (need_unlock=True, need_access=True, 1点配额)
            # 命中本地已解锁缓存时方可直接复用直链
            cached_url = self._resources.cached_url(link_id)
            is_unlocked = bool(cached_url)

            results.append({
                "title": title,
                "description": desc,
                "url": cached_url if is_unlocked else "",
                "resource_type": resource_type,
                "update_time": update_time,
                "source_url": source_url,
                "need_unlock": not is_unlocked,
                "need_access": not is_unlocked,
                "unlock_points": 0 if is_unlocked else 1,
                "pending_resolution": not is_unlocked,
                "is_unlocked": is_unlocked,
                "resource_ref": str(link_id),
                "unlock_group": f"pinglian:link:{link_id}",
                "provider_data": {
                    "resource_id": link_id,
                    "link_id": link_id,
                    "token": link_id,
                    "video_id": str(vod_id),
                    "password": str(row.get("password") or ""),
                },
            })
        logger.debug(
            f"{prefix} 候选构造完成：最终输出={len(results)}，"
            f"直接解析={resolved_count}，解析失败={resolve_failed_count}"
        )
        return results

    def search(self, query: SearchQuery):
        prefix = format_search_log_prefix(query, "pinglian")
        # 订阅转存/下载模式下，若今日解锁配额已耗尽，直接跳过搜索以避免无用请求与风控
        if not query.resource_list_mode and not self._resources.has_quota():
            quota_text = self._resources.format_quota_text()
            logger.warning(
                f"{prefix} 今日链接解锁配额已用尽（{quota_text}），"
                "跳过盘链搜索以阻止无用请求并保护配额"
            )
            return []

        mediainfo = query.mediainfo
        titles = media_titles(mediainfo)
        return self._search(
            titles=titles,
            year=getattr(mediainfo, "year", None),
            limit=(
                query.result_limit or self._result_limit
                if query.resource_list_mode else self._result_limit
            ),
            resource_list_mode=query.resource_list_mode,
            log_prefix=prefix,
        )

    def unlock(
            self,
            candidate: Dict[str, Any],
            search_label: str = "",
    ) -> Optional[str]:
        """按需解锁单个盘链资源。"""
        provider_data = candidate.get("provider_data") or {}
        link_id = str(provider_data.get("link_id") or candidate.get("resource_ref") or "")
        resource_type = candidate.get("resource_type") or ""
        password = str(provider_data.get("password") or "")
        return self._resources.unlock_resource(
            link_id=link_id,
            resource_type=resource_type,
            password=password,
            budget=self._budget,
            check_first=True,
            search_label=search_label,
        )

    def resolve(self, **kwargs):
        return self._resources.resolve_link(**kwargs)

    def clear_cache(self) -> int:
        count = self._resources.clear_cache()
        count += clear_cache(self._client)
        count += self._budget.clear_cached_urls()
        return count
