"""片库资源列表拉取、候选构造与分享直链解析协议。"""

import threading
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import MediaType

from .client import PiankuClient, PiankuError
from ..magnet import (
    build_magnet_url,
    extract_magnet_hash,
    media_titles,
    normalize_magnets,
)
from ..matching import (
    extract_season,
    extract_year,
    media_aliases,
    resource_title_matches,
    search_keyword_candidates,
    title_matches,
)
from ..types import (
    RESOURCE_TYPE_PRIORITY,
    append_share_password,
    resource_type_from_panel,
    resource_type_from_url,
)
from ...core.search import SearchQuery
from ...utils.cache import create_platform_ttl_cache


class PiankuResourceService:
    """使用片库客户端拉取资源并解析直链，不管理浏览器会话本身。"""

    #: 站点未收录的平台键：只能解析出真实链接后再按链接判定类型。
    _UNKNOWN_PLATFORM = "?"

    def __init__(
            self,
            client: PiankuClient,
            result_limit: int = 20,
            detail_limit: int = 3,
    ) -> None:
        self._client = client
        self._result_limit = max(1, int(result_limit or 20))
        self._detail_limit = max(1, int(detail_limit or 3))
        self._lock = threading.RLock()
        #: 资源身份 -> 已解析真实链接：同一资源在 TTL 内不再重复过盾解析。
        self._link_identity = create_platform_ttl_cache(
            "pianku:links", getattr(client, "base_url", ""), maxsize=2048, ttl=6 * 3600
        )

    @classmethod
    def _keywords(cls, mediainfo: Any, media_type: MediaType, season: Optional[int]) -> List[str]:
        titles = media_titles(mediainfo)
        keywords = search_keyword_candidates(titles, media_aliases(mediainfo))
        year = extract_year(getattr(mediainfo, "year", None))
        expanded: List[str] = []
        for title in keywords:
            expanded.append(title)
            if year:
                expanded.append(f"{title} {year}")
            if media_type == MediaType.TV and season:
                expanded.append(f"{title} 第{season}季")
        return list(dict.fromkeys(text for text in expanded if str(text or "").strip()))

    def _select_details(
            self,
            details: List[Dict[str, Any]],
            exp_titles: List[str],
            expected_year: str,
            media_type: MediaType,
            season: Optional[int],
    ) -> List[Dict[str, Any]]:
        """按标题/年份/季号挑选需要读取资源的详情页。"""
        matched: List[Dict[str, Any]] = []
        fallback: List[Dict[str, Any]] = []
        for detail in details:
            title = str(detail.get("title") or "").strip()
            if not title:
                continue
            if not (
                    title_matches(title, exp_titles)
                    or resource_title_matches(title, exp_titles, expected_year=expected_year)
            ):
                continue
            detail_year = extract_year(f"{title} {detail.get('subtitle') or ''}")
            if media_type == MediaType.MOVIE and expected_year and detail_year and detail_year != expected_year:
                continue
            detail_season = extract_season(f"{title} {detail.get('remarks') or ''}")
            if media_type == MediaType.TV and season and detail_season and detail_season != season:
                continue
            if media_type == MediaType.MOVIE and detail_season:
                continue
            matched.append(detail)
            if not detail_year and not detail_season:
                fallback.append(detail)
        # 年份/季号信息缺失的条目排在后面，优先解析信息更完整的详情页。
        ordered = matched + fallback
        return list({item["id"]: item for item in ordered}.values())[: self._detail_limit]

    @classmethod
    def _resource_type(cls, platform: str) -> str:
        """平台键/展示名映射（公共识别）；站点归到 other 的平台返回空并等待链接判定。"""
        return resource_type_from_panel(platform)

    @classmethod
    def _infer_resource_type(cls, link: str, platform: str = "") -> str:
        """优先用平台键判定，其次按真实链接（磁力/电驴/网盘域名）判定。"""
        resource_type = cls._resource_type(platform)
        if resource_type:
            return resource_type
        return resource_type_from_url(link)

    def _accepts_title(
            self,
            title: str,
            media_type: MediaType,
            season: Optional[int],
            expected_year: str,
            exp_titles: List[str],
    ) -> bool:
        """在解析真实链接前先按标题/年份/季号过滤，避免无谓的解析请求。"""
        if not title:
            return False
        if exp_titles and not resource_title_matches(
                title, exp_titles, expected_year=expected_year, strict_year=bool(expected_year)
        ):
            return False
        candidate_season = extract_season(title)
        if media_type == MediaType.TV and season and candidate_season and candidate_season != season:
            return False
        if media_type == MediaType.MOVIE and candidate_season:
            return False
        return True

    @classmethod
    def build_share_link(
            cls, link: str, resource_type: str, title: str = "", password: str = ""
    ) -> str:
        """把解析结果规范化为可转存链接（磁力补全展示名，网盘附加提取码）。"""
        target = str(link or "").strip()
        if not target:
            return ""
        if resource_type == "magnet":
            info_hash = extract_magnet_hash(target)
            return build_magnet_url(info_hash, title) if info_hash else ""
        if password:
            return append_share_password(resource_type, target, password)
        return target

    @classmethod
    def _build_candidate(
            cls,
            resource: Dict[str, Any],
            resource_type: str,
            link: str,
    ) -> Optional[Dict[str, Any]]:
        title = str(resource.get("title") or "").strip()
        if not title or not resource_type:
            return None
        password = str(resource.get("password") or "").strip()
        resolved = cls.build_share_link(link, resource_type, title, password)
        if not resolved:
            return None
        candidate: Dict[str, Any] = {
            "url": resolved,
            "title": title,
            "resource_type": resource_type,
            "source": "pianku",
            "source_url": str(resource.get("source_url") or ""),
            "resource_ref": str(resource.get("href") or ""),
            "provider_data": {
                "resource_id": str(resource.get("href") or ""),
                "token": str(resource.get("href") or ""),
                "platform": str(resource.get("platform") or ""),
                "password": password,
                "detail_url": str(resource.get("source_url") or ""),
            },
        }
        if resource_type == "magnet":
            candidate["info_hash"] = (extract_magnet_hash(resolved) or "").upper()
        if password:
            candidate["share_password"] = password
        return candidate

    @staticmethod
    def _round_robin(groups: List[List[Dict[str, Any]]], limit: int) -> List[Dict[str, Any]]:
        """按资源类型轮转取候选，避免单一网盘占满候选上限。"""
        ordered: List[Dict[str, Any]] = []
        index = 0
        while len(ordered) < limit and groups:
            progressed = False
            for group in groups:
                if index < len(group):
                    ordered.append(group[index])
                    progressed = True
                    if len(ordered) >= limit:
                        break
            if not progressed:
                break
            index += 1
        return ordered

    @staticmethod
    def _ordered_groups(
            groups: Dict[str, List[Dict[str, Any]]]
    ) -> List[Any]:
        """按「磁力 -> 未知待判定 -> 插件资源类型优先级」返回 (类型, 分组) 序列。

        磁力无需网盘凭证、适用范围最广；未知分组通常是站点归到 other 的 115/ed2k，
        解析出链接后即可判定类型，因此排在其它网盘之前。
        """

        def sort_key(resource_type: str) -> tuple:
            if resource_type == "magnet":
                return (0, 0)
            if resource_type == PiankuResourceService._UNKNOWN_PLATFORM:
                return (1, 0)
            return (2, RESOURCE_TYPE_PRIORITY.get(resource_type, 99))

        return [(key, groups[key]) for key in sorted(groups, key=sort_key)]

    def _load_groups(
            self,
            selected: List[Dict[str, Any]],
            media_type: MediaType,
            season: Optional[int],
            expected_year: str,
            exp_titles: List[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """逐个详情页取资源（必要时过盾），离线预筛后按资源类型分组。"""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        seen_rows = set()
        # 单个详情页通常已包含整季资源；命中足够多候选后不再渲染后续详情页，
        # 控制过盾与渲染开销（详情页上限仅是兜底）。
        enough_rows = max(12, self._result_limit * 3)
        for detail in selected:
            if sum(len(group) for group in groups.values()) >= enough_rows:
                break
            try:
                resources = self._client.fetch_resources(detail.get("id"))
            except PiankuError as error:
                logger.warning(f"片库详情 {detail.get('id')} 资源读取失败：{error}")
                continue
            for resource in resources:
                title = str(resource.get("title") or "").strip()
                if not title:
                    continue
                if not self._accepts_title(
                        title, media_type, season, expected_year, exp_titles
                ):
                    continue
                # 站点未收录的平台（115/ed2k 等会被归为 other）先按未知占位，
                # 解析出真实链接后再判定类型，避免直接丢弃。
                resource_type = (
                        self._resource_type(resource.get("platform"))
                        or self._UNKNOWN_PLATFORM
                )
                row_key = (resource_type, title, str(resource.get("password") or ""))
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                item = dict(resource)
                item["source_url"] = detail.get("url") or ""
                groups.setdefault(resource_type, []).append(item)
        return groups

    def _search_details(self, keywords: List[str]) -> List[Dict[str, Any]]:
        details: List[Dict[str, Any]] = []
        seen_ids = set()
        for keyword in keywords[:3]:
            try:
                entries = self._client.search_titles(keyword)
            except PiankuError as error:
                logger.warning(f"片库检索关键词 '{keyword}' 失败：{error}")
                continue
            for entry in entries:
                vod_id = str(entry.get("id") or "").strip()
                if not vod_id or vod_id in seen_ids:
                    continue
                seen_ids.add(vod_id)
                details.append(entry)
            if details:
                break
        return details

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        mediainfo = query.mediainfo
        if not mediainfo:
            return []

        keywords = self._keywords(mediainfo, query.media_type, query.season)
        if not keywords:
            return []

        exp_titles = media_titles(mediainfo)
        expected_year = extract_year(getattr(mediainfo, "year", None))
        limit = max(1, int(query.result_limit or self._result_limit))

        selected = self._select_details(
            self._search_details(keywords),
            exp_titles,
            expected_year,
            query.media_type,
            query.season,
        )
        if not selected:
            return []

        groups = self._load_groups(
            selected, query.media_type, query.season, expected_year, exp_titles
        )
        if not groups:
            return []

        ordered = self._ordered_groups(groups)

        # 片库的 open.php 直链带一次性签名凭证，只在签发它的会话内有效；因此无论
        # 普通搜索还是网盘资源列表，都必须在同一次会话内解析成真实链接再交付，
        # 否则调用方拿到的引用会以「资源访问凭证已失效」失败。
        # 解析预算：每个候选最多消耗 1 次解析，另留 1 次重试余量。
        budget = limit + 1
        plan = self._round_robin([group for _, group in ordered], budget)

        def identity_of(resource: Dict[str, Any]) -> tuple:
            return (
                str(resource.get("source_url") or ""),
                str(resource.get("platform") or ""),
                str(resource.get("title") or ""),
                str(resource.get("password") or ""),
            )

        links: Dict[str, str] = {}
        pending_hrefs: List[str] = []
        with self._lock:
            for resource in plan:
                identity = identity_of(resource)
                cached = self._link_identity.get(identity)
                if cached:
                    links[str(resource.get("href") or "")] = str(cached)
                    continue
                href = str(resource.get("href") or "")
                if href:
                    pending_hrefs.append(href)
        if pending_hrefs:
            try:
                # 批量解析：同一次会话内连续请求，避免逐条排队公共门控。
                links.update(self._client.resolve_links(pending_hrefs))
            except PiankuError as error:
                logger.warning(f"片库资源批量解析失败：{error}")

        collected: List[Dict[str, Any]] = []
        seen_urls = set()
        for resource in plan:
            href = str(resource.get("href") or "")
            link = links.get(href) or ""
            if not link:
                continue
            resource_type = self._infer_resource_type(link, resource.get("platform"))
            candidate = self._build_candidate(
                resource,
                resource_type or self._resource_type(resource.get("platform")),
                link,
            )
            if not candidate:
                continue
            dedupe_key = candidate.get("info_hash") or candidate["url"]
            if dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)
            with self._lock:
                self._link_identity[identity_of(resource)] = link
            collected.append(candidate)
            if len(collected) >= limit:
                break

        return collected

    def resolve_resource(
            self,
            reference: str,
            resource_type: str = "",
            password: str = "",
            title: str = "",
            detail_url: str = "",
    ) -> Dict[str, Any]:
        """按资源引用过盾换取真实链接（网盘资源列表按需调用）。"""
        href = str(reference or "").strip()
        if not href:
            raise PiankuError("片库资源引用为空")
        with self._lock:
            link = self._client.resolve_link(href, detail_url)
            if not link:
                raise PiankuError("片库资源链接解析失败（可能需要重新完成人机验证）")
            normalized_type = self._resource_type(resource_type) or str(resource_type or "").strip().lower()
            resolved = self.build_share_link(link, normalized_type, title, password)
            if not resolved:
                raise PiankuError("片库资源链接格式无效")
        result: Dict[str, Any] = {
            "url": resolved,
            "resource_type": normalized_type,
            "resource_ref": href,
        }
        if normalized_type == "magnet":
            result["info_hash"] = (extract_magnet_hash(resolved) or "").upper()
        return result

    def normalize(self, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return normalize_magnets(resources, "pianku")

    def clear_cache(self) -> Dict[str, int]:
        """清空检索/资源列表缓存；已解析链接按 TTL 复用，不随清理失效。"""
        return {"resources": int(self._client.clear_cache() or 0)}
