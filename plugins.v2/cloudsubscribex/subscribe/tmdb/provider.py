"""TMDB 自动订阅渠道（对接 MoviePilot 平台推荐功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.recommend import RecommendChain
from app.log import logger

from ...core.subscribe import MediaCandidate, SubscribeContext
from ...core.subscribe.provider import RankingProvider, ranking_scan_limit
from ...core.subscribe.registry import register

TMDB_METHODS: dict[str, tuple[str, str, str]] = {
    "trending": ("tmdb_trending", "TMDB流行趋势", "movie"),
    "movies": ("tmdb_movies", "TMDB热门电影", "movie"),
    "tvs": ("tmdb_tvs", "TMDB热门电视剧", "tv"),
}


@register
class TmdbSubscribeProvider(RankingProvider):
    provider_id = "tmdb"
    provider_name = "TMDB榜单"

    def __init__(self, chain: Optional[RecommendChain] = None) -> None:
        self._chain = chain or RecommendChain()

    def spec(self) -> dict:
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "default_cron": "0 8 * * *",
        }

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("ranks"))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        ranks = list(options.get("ranks") or ["trending", "movies", "tvs"])
        if not ranks:
            return

        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        scan_limit = ranking_scan_limit(options)
        limit_type = str(options.get("media_type") or "").strip().lower()
        raw_types = options.get("media_types")
        if raw_types:
            if isinstance(raw_types, (list, tuple, set)):
                selected_types = {str(t).strip().lower() for t in raw_types if str(t).strip()}
            else:
                selected_types = {str(raw_types).strip().lower()}
        else:
            selected_types = set()

        logger.info(f"开始抓取TMDB榜单：选中榜单={ranks}")
        seen_keys: set[str] = set()

        for rank_key in ranks:
            if context.stopped():
                return
            if rank_key not in TMDB_METHODS:
                logger.warning(f"未知TMDB榜单键：{rank_key}")
                continue

            method_name, rank_name, default_type = TMDB_METHODS[rank_key]
            method = getattr(self._chain, method_name, None)
            if not method:
                logger.warning(f"MoviePilot平台推荐链中不存在方法：RecommendChain.{method_name}")
                continue

            rank_label = f"TMDB榜单[{rank_name}]"
            try:
                items = self._paginate(
                    lambda page: method(page=page),
                    scan_limit,
                    context,
                    rank_label,
                    "tmdb_id",
                )
                if items is None:
                    return
            except Exception as error:
                logger.error(f"抓取{rank_label}失败：{error}")
                continue

            logger.info(f"{rank_label}抓取成功：返回 {len(items)} 条数据")

            for item in items:
                if context.stopped():
                    return
                data = item.to_dict() if hasattr(item, "to_dict") else dict(item or {})
                mtype = self._resolve_media_type(data, default_type, selected_types, limit_type)
                if mtype is None:
                    continue
                candidate = self._candidate_from_dict(
                    data,
                    min_year=min_year,
                    min_vote=min_vote,
                    seen_keys=seen_keys,
                    media_type=mtype,
                    source=self.provider_id,
                    rank=rank_name,
                    rank_key=rank_key,
                    id_fields=(("tmdb_id", "int"),),
                    unique_key_fields=("tmdb_id",),
                )
                if candidate is None:
                    continue
                yield candidate
