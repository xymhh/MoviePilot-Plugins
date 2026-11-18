"""聚影搜索服务。"""

from typing import Any, Dict

from ..base import DelegatingSearchService
from ...core.media import media_id_of, tmdb_id_of
from ...core.search import SearchQuery


class JuyingSearchService(DelegatingSearchService):
    source_key = "juying"

    def _extra_resource_kwargs(self, query: SearchQuery) -> Dict[str, Any]:
        mediainfo = query.mediainfo
        subscribe = query.subscribe
        return {
            "tmdb_id": (
                    getattr(mediainfo, "tmdb_id", None)
                    or tmdb_id_of(subscribe)
            ),
            "douban_id": (
                    getattr(mediainfo, "douban_id", None)
                    or media_id_of(subscribe, "douban")
            ),
            "imdb_id": (
                    getattr(mediainfo, "imdb_id", None)
                    or media_id_of(subscribe, "imdb")
            ),
        }
