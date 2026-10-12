"""MoviePilot 插件模块声明：把网盘候选资源接入平台搜索链路。

平台调用链（MoviePilot v3）：

* ``SearchChain._search_all_sites`` -> ``search_plugin_torrents``：标题/手动搜索；
* ``SearchScan.fetch`` 的 ``plugin`` 来源：订阅补集搜索；
* ``async_search_plugin_torrents``：智能体与异步搜索链路。

模块返回的 ``TorrentInfo`` 统一以 :attr:`MoviePilotSearchModule.SITE_NAME_PREFIX`
作为站点名前缀，``ResourceDownload`` 事件据此识别「网盘候选」，
并把平台下载交回本插件的链接转存链路（见 :meth:`is_cloud_resource`）。
"""

import asyncio
import functools
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.log import logger
from app.schemas import TorrentInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import coerce_media_source


class MoviePilotSearchModule(OwnerDelegator):
    """以 MoviePilot ``search_torrents`` 模块暴露网盘搜索能力。"""

    # 平台模块方法名（同步/异步共用同一契约）
    MODULE_METHOD = "search_torrents"
    ASYNC_MODULE_METHOD = "async_search_torrents"
    # 站点名统一前缀：既是 UI 上的来源标识，也是下载接管时的识别标记
    SITE_NAME_PREFIX = "网盘订阅助手｜"
    # 单个关键词最多回填给平台的候选数（平台侧还要做规则过滤）
    DEFAULT_LIMIT = 20
    # 网盘源只有一页，平台侧对插件来源也按「不分页」处理
    MAX_PAGE = 0

    _SEASON_PATTERNS = (
        re.compile(r"^(?P<title>.*?)\s*[Ss](?P<season>\d{1,2})\s*$"),
        re.compile(r"^(?P<title>.*?)\s*第\s*(?P<season>\d{1,2})\s*季\s*$"),
    )
    _SIZE_UNITS = {
        "": 1,
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "KIB": 1024,
        "M": 1024 ** 2,
        "MB": 1024 ** 2,
        "MIB": 1024 ** 2,
        "G": 1024 ** 3,
        "GB": 1024 ** 3,
        "GIB": 1024 ** 3,
        "T": 1024 ** 4,
        "TB": 1024 ** 4,
        "TIB": 1024 ** 4,
    }
    _SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Z]*)$")

    # ---------------------------------------------------------------- 宿主声明
    def get_module(self) -> Optional[Dict[str, Any]]:
        """声明 ``search_torrents`` 模块；关闭开关时返回 None 保持原行为。"""
        if not self._module_enabled():
            return None
        return {
            self.MODULE_METHOD: self.search_torrents,
            self.ASYNC_MODULE_METHOD: self.async_search_torrents,
        }

    @classmethod
    def is_cloud_resource(cls, torrent: Any) -> bool:
        """判断平台资源是否来自本插件的网盘资源源。"""
        site_name = str(getattr(torrent, "site_name", "") or "")
        return site_name.startswith(cls.SITE_NAME_PREFIX)

    # ---------------------------------------------------------------- 模块实现
    def search_torrents(
            self,
            site: Optional[Dict[str, Any]] = None,
            keyword: str = "",
            mtype: Optional[MediaType] = None,
            page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """同步模块实现：平台会在独立线程中调用，失败一律降级为空结果。"""
        return self._search_torrents(keyword=keyword, mtype=mtype, page=page)

    async def async_search_torrents(
            self,
            site: Optional[Dict[str, Any]] = None,
            keyword: str = "",
            mtype: Optional[MediaType] = None,
            page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """异步模块实现：复用同步搜索，避免阻塞平台事件循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                self._search_torrents,
                keyword=keyword,
                mtype=mtype,
                page=page,
            ),
        )

    # ---------------------------------------------------------------- 内部实现
    def _module_enabled(self) -> bool:
        if not bool(getattr(self, "_enabled", False)):
            return False
        value = getattr(self, "_mp_search_module_enabled", None)
        return True if value is None else bool(value)

    def _module_limit(self) -> int:
        try:
            limit = int(getattr(self, "_mp_search_module_limit", None) or self.DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = self.DEFAULT_LIMIT
        return max(1, min(limit, 50))

    def _search_torrents(
            self,
            keyword: str = "",
            mtype: Optional[MediaType] = None,
            page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """按平台传入的关键词做一轮网盘搜索，并翻译为平台资源对象。"""
        if not self._module_enabled():
            return []
        try:
            page_number = int(page or 0)
        except (TypeError, ValueError):
            page_number = 0
        if page_number > self.MAX_PAGE:
            return []
        title, season = self.split_title_and_season(keyword, mtype)
        if not title:
            return []
        if not getattr(self, "_search_handler", None):
            logger.debug("网盘资源源搜索跳过：搜索服务尚未就绪")
            return []

        session_id = f"mp-module-{uuid4().hex[:8]}"
        try:
            result = self.search_platform_resources(
                session_id=session_id,
                subscribe_id=None,
                title=title,
                media_type=self.media_type_key(mtype),
                season=season,
                latest_season=False,
                limit=self._module_limit(),
            )
        except Exception as error:
            logger.error(f"网盘资源源搜索失败：{title}，{error}", exc_info=True)
            return []
        if not isinstance(result, dict) or not result.get("success"):
            message = (result or {}).get("message") if isinstance(result, dict) else ""
            logger.debug(f"网盘资源源未返回候选：{title}（{message or '无结果'}）")
            return []

        candidates = list(result.get("candidates") or [])
        resources = self._module_resources(session_id, str(result.get("search_id") or ""))
        media = dict(result.get("media") or {})
        torrents: List[TorrentInfo] = []
        skipped = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            item = resources.get(str(candidate.get("candidate_id") or "")) or {}
            torrent = self.build_torrent_info(candidate, item, media)
            if torrent is None:
                skipped += 1
                continue
            torrents.append(torrent)
        logger.info(
            f"网盘资源源回填平台：{media.get('title') or title}"
            f"{f' S{int(season):02d}' if season else ''}，"
            f"候选 {len(torrents)} 个（跳过不可直接转存 {skipped} 个）"
        )
        return torrents

    def _module_resources(self, session_id: str, search_id: str) -> Dict[str, Any]:
        """取回上一轮搜索缓存的原始候选（含真实链接）。"""
        cache = getattr(self, "_agent_resource_cache", None)
        if not cache or not search_id:
            return {}
        try:
            cached = cache.get(self._agent_cache_key(session_id, search_id)) or {}
        except Exception as error:
            logger.debug(f"读取网盘候选缓存失败：{error}")
            return {}
        resources = cached.get("resources") if isinstance(cached, dict) else None
        return dict(resources or {})

    def build_torrent_info(
            self,
            candidate: Dict[str, Any],
            resource: Optional[Dict[str, Any]] = None,
            media: Optional[Dict[str, Any]] = None,
    ) -> Optional[TorrentInfo]:
        """把一条网盘候选翻译成平台资源对象；无法直接转存时返回 None。"""
        resource = dict(resource or {})
        media = dict(media or {})
        link = self.resource_link(resource)
        if not link:
            return None
        source = str(candidate.get("source") or resource.get("source") or "unknown").strip()
        resource_type = str(
            candidate.get("resource_type") or resource.get("resource_type") or ""
        ).strip().lower()
        title = str(candidate.get("title") or resource.get("title") or "").strip()
        if not title:
            title = "网盘资源"
        media_source, media_id = self._media_identity(media)
        return self._new_torrent_info(
            site=None,
            site_name=f"{self.SITE_NAME_PREFIX}{source or 'unknown'}",
            title=title,
            description=self._description(candidate, resource, resource_type),
            enclosure=link,
            page_url=str(
                resource.get("media_page_url")
                or resource.get("source_url")
                or link
            ),
            size=self.parse_size(candidate.get("size") or resource.get("size")),
            seeders=0,
            peers=0,
            grabs=0,
            pubdate=str(candidate.get("update_time") or resource.get("update_time") or "") or None,
            category=str(media.get("type") or "") or None,
            downloadvolumefactor=0.0,
            uploadvolumefactor=0.0,
            labels=[value for value in ("网盘", source, resource_type) if value],
            media_source=media_source,
            media_id=media_id,
        )

    @staticmethod
    def resource_link(resource: Dict[str, Any]) -> str:
        """取候选的可直接转存链接；需要解锁或缺失链接时返回空串。"""
        raw = resource.get("url")
        if isinstance(raw, (list, tuple)):
            links = [str(value).strip() for value in raw if str(value or "").strip()]
            return links[0] if links else ""
        return str(raw or "").strip()

    @staticmethod
    def _description(
            candidate: Dict[str, Any],
            resource: Dict[str, Any],
            resource_type: str,
    ) -> str:
        parts = [
            str(candidate.get("resolution") or resource.get("resolution") or "").strip(),
            str(candidate.get("quality") or resource.get("quality") or "").strip(),
            resource_type.upper() if resource_type else "",
            str(candidate.get("source") or resource.get("source") or "").strip(),
        ]
        if candidate.get("need_unlock"):
            points = int(candidate.get("unlock_points") or 0)
            parts.append(f"需 {points} 积分解锁" if points else "需解锁")
        if resource.get("description"):
            parts.append(str(resource.get("description") or "").strip())
        return " ｜ ".join(value for value in parts if value)

    @staticmethod
    def _media_identity(media: Dict[str, Any]) -> tuple[Any, Optional[str]]:
        tmdb_id = str(media.get("tmdb_id") or "").strip()
        if not tmdb_id:
            return None, None
        return coerce_media_source("tmdb"), tmdb_id

    @staticmethod
    def _new_torrent_info(**fields: Any) -> TorrentInfo:
        """按当前宿主支持的字段构造资源对象（v2/v3 字段集不同）。"""
        allowed = (
            getattr(TorrentInfo, "model_fields", None)
            or getattr(TorrentInfo, "__fields__", None)
            or {}
        )
        if allowed:
            fields = {
                key: value for key, value in fields.items() if key in allowed
            }
        return TorrentInfo(**fields)

    @classmethod
    def split_title_and_season(
            cls,
            keyword: Any,
            mtype: Optional[MediaType] = None,
    ) -> tuple[str, Optional[int]]:
        """从平台关键词里拆出媒体名与季号（``某某 S02`` / ``某某 第2季``）。"""
        text = str(keyword or "").strip()
        if not text:
            return "", None
        if cls.media_type_key(mtype) != "tv":
            return text, None
        for pattern in cls._SEASON_PATTERNS:
            match = pattern.match(text)
            if not match:
                continue
            title = match.group("title").strip()
            if not title:
                break
            return title, int(match.group("season"))
        return text, None

    @staticmethod
    def media_type_key(mtype: Any) -> str:
        """把宿主媒体类型归一化为插件内部的 ``movie`` / ``tv`` 取值。"""
        value = str(getattr(mtype, "value", mtype) or "").strip().casefold()
        if value in {"movie", "电影"}:
            return "movie"
        if value in {"tv", "电视剧"}:
            return "tv"
        return ""

    @classmethod
    def parse_size(cls, value: Any) -> float:
        """把候选里的体积描述解析为字节数，无法解析时返回 0。"""
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip().upper().replace(" ", "")
        if not text:
            return 0.0
        match = cls._SIZE_PATTERN.match(text)
        if not match:
            return 0.0
        number = float(match.group(1))
        unit = match.group(2)
        if unit not in cls._SIZE_UNITS:
            return 0.0
        return number * cls._SIZE_UNITS[unit]
