"""豆瓣自动订阅渠道（全面对接 MoviePilot 平台原生推荐功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.recommend import RecommendChain
from app.log import logger

from ...core.subscribe import MediaCandidate, SubscribeContext
from ...core.subscribe.provider import RankingProvider, ranking_scan_limit
from ...core.subscribe.registry import register

DOUBAN_METHODS: dict[str, tuple[str, str, str]] = {
    "movie-showing": ("douban_movie_showing", "豆瓣正在热映", "movie"),
    "movie-hot": ("douban_movie_hot", "豆瓣热门电影", "movie"),
    "tv-hot": ("douban_tv_hot", "豆瓣热门电视剧", "tv"),
    "tv-weekly-chinese": ("douban_tv_weekly_chinese", "豆瓣国产剧集周榜", "tv"),
    "tv-weekly-global": ("douban_tv_weekly_global", "豆瓣全球剧集周榜", "tv"),
    "tv-animation": ("douban_tv_animation", "豆瓣动画剧集", "tv"),
    "movie-latest": ("douban_movies", "豆瓣最新电影", "movie"),
    "tv-latest": ("douban_tvs", "豆瓣最新剧集", "tv"),
}


@register
class DoubanSubscribeProvider(RankingProvider):
    provider_id = "douban"
    provider_name = "豆瓣榜单"

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
        ranks = list(options.get("ranks") or ["movie-showing", "tv-hot"])
        if not ranks:
            return

        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        limit = int(options.get("limit") or 30)
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

        logger.info(f"开始抓取豆瓣榜单：选中榜单={ranks}")
        seen_keys: set[str] = set()

        for rank_key in ranks:
            if context.stopped():
                return
            if rank_key not in DOUBAN_METHODS:
                logger.warning(f"未知豆瓣榜单键：{rank_key}")
                continue

            method_name, rank_name, default_type = DOUBAN_METHODS[rank_key]
            method = getattr(self._chain, method_name, None)
            if not method:
                logger.warning(f"MoviePilot平台推荐链中不存在方法：RecommendChain.{method_name}")
                continue

            rank_label = f"豆瓣榜单[{rank_name}]"
            try:
                items = self._paginate(
                    lambda page: method(page=page, count=limit),
                    scan_limit,
                    context,
                    rank_label,
                    "douban_id",
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
                    id_fields=(("douban_id", "str"), ("tmdb_id", "int")),
                    unique_key_fields=("douban_id", "tmdb_id"),
                )
                if candidate is None:
                    continue
                yield candidate
