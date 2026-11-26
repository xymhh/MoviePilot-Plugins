"""搜索服务共享基类。

两个家族，各留一个基类：

* ``BaseMagnetSearchService``——简单 magnet 源（piratebay / uindex）：统一的
  ``search()`` 循环（关键词上限 3 个 → 逐关键词检索 → info_hash 去重 →
  季号/年份/标题过滤 → 上限截断 → ``normalize_magnets``），子类只留
  ``_keywords``、``_client_search``、``_on_client_error`` 三个钩子与来源 key。
* ``DelegatingSearchService``——薄门面源（juying / woniu）：检索与解析全部
  委托给资源服务，自身只做参数组装与归一化；子类只留来源 key 与
  ``_extra_resource_kwargs``（聚影多传 tmdb/douban/imdb 三个 id）。

片库（pianku）不在此列：它的 ``search()`` 是整 query 透传
``resources.search(query)`` 再 ``resources.normalize(...)``，``resolve()``
另有一套 provider_data/detail_url 参数，实测与上列两家不等价，留在原文件。
"""

from typing import Any, Dict, Iterable, List, Optional

from app.schemas.types import MediaType

from .magnet import clear_cache, media_titles, normalize_magnets
from .matching import extract_season, extract_year, resource_title_matches
from ..core.search import SearchQuery


class BaseMagnetSearchService:
    """简单 magnet 源的统一 search() 循环。"""

    #: normalize_magnets 用的来源 key。
    source_key = ""
    #: 客户端检索抛出的异常类型；search() 只接住它，其余异常继续上抛。
    client_error: type = Exception

    def __init__(self, client: Any, result_limit: int = 20):
        self._client = client
        self._result_limit = result_limit

    @staticmethod
    def _keywords(
            mediainfo: Any, media_type: MediaType, season: Optional[int]
    ) -> List[str]:
        """按媒体信息生成检索关键词；每源各留自己的实现。"""
        raise NotImplementedError

    def _client_search(self, keyword: str) -> List[Dict[str, Any]]:
        """调客户端检索一个关键词；海盗湾 override 成带 cat=200 的调用。"""
        return self._client.search(keyword)

    def _on_client_error(self, keyword: str, err: Exception) -> None:
        """单关键词检索失败：默认静默跳过（海盗湾行为）。"""

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        mediainfo = query.mediainfo
        if not mediainfo:
            return []

        keywords = self._keywords(mediainfo, query.media_type, query.season)
        if not keywords:
            return []

        limit = query.result_limit or self._result_limit
        exp_titles = media_titles(mediainfo)
        expected_year = extract_year(getattr(mediainfo, "year", None))
        collected = []
        seen_hashes = set()

        for kw in keywords[:3]:
            try:
                entries = self._client_search(kw)
            except self.client_error as err:
                self._on_client_error(kw, err)
                continue

            for item in entries:
                h = item.get("info_hash")
                if not h or h in seen_hashes:
                    continue

                # 季号匹配
                cand_season = extract_season(item.get("title"))
                if query.media_type == MediaType.TV and query.season:
                    if cand_season is not None and cand_season != query.season:
                        continue
                elif query.media_type == MediaType.MOVIE:
                    if cand_season is not None:
                        continue

                # 电影年份检查（如果资源标题中包含4位年份，且与mediainfo年份不符，跳过）
                if query.media_type == MediaType.MOVIE:
                    cand_year = extract_year(item.get("title"))
                    if expected_year and cand_year and cand_year != expected_year:
                        continue

                # 标题校验：资源标题必须包含媒体目标标题中的至少一个
                if exp_titles and not resource_title_matches(
                        item.get("title"), exp_titles, expected_year=expected_year
                ):
                    continue
                seen_hashes.add(h)
                collected.append(item)
                if len(collected) >= limit:
                    break

            if len(collected) >= limit:
                break

        return normalize_magnets(collected, self.source_key)

    def clear_cache(self) -> int:
        return clear_cache(self._client)


class DelegatingSearchService:
    """薄门面源：检索与解析委托给资源服务，自身只做参数组装与归一化。"""

    #: normalize_magnets 用的来源 key。
    source_key = ""

    def __init__(
            self,
            client: Any,
            resources: Any,
            resource_types: Iterable[str],
            result_limit: int,
    ):
        self._client = client
        self._resources = resources
        self._resource_types = tuple(resource_types)
        self._result_limit = result_limit

    def _extra_resource_kwargs(self, query: SearchQuery) -> Dict[str, Any]:
        """资源服务 search() 的额外参数；聚影 override 成 tmdb/douban/imdb。"""
        return {}

    def search(self, query: SearchQuery):
        mediainfo = query.mediainfo
        titles = media_titles(mediainfo)
        resources = self._resources.search(
            title=titles[0] if titles else "",
            alternative_titles=titles[1:],
            year=getattr(mediainfo, "year", None),
            media_type=(
                "tv" if query.media_type == MediaType.TV else "movie"
            ),
            season=query.season,
            resource_type_order=self._resource_types,
            limit=(
                query.result_limit or self._result_limit
                if query.resource_list_mode else self._result_limit
            ),
            resource_list_mode=query.resource_list_mode,
            **self._extra_resource_kwargs(query),
        )
        return normalize_magnets(resources, self.source_key)

    def resolve(self, **kwargs):
        return self._resources.resolve_resource(
            str(kwargs.get("resource_id") or "")
        )

    def clear_cache(self) -> int:
        return clear_cache(self._resources)
