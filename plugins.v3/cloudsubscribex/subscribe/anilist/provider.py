"""AniList 自动订阅渠道（对接 MoviePilot 平台 AniList 功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.anilist import AniListChain
from app.log import logger

from ...core.subscribe import MediaCandidate, SubscribeContext
from ...core.subscribe.provider import RankingProvider, ranking_scan_limit
from ...core.subscribe.registry import register

ANILIST_METHODS: dict[str, tuple[str, str]] = {
    "popular": ("popular_this_season", "AniList本季热门"),
    "popular_this_season": ("popular_this_season", "AniList本季热门"),
    "trending": ("trending", "AniList流行趋势"),
}


@register
class AnilistSubscribeProvider(RankingProvider):
    provider_id = "anilist"
    provider_name = "AniList榜单"

    def __init__(self, chain: Optional[AniListChain] = None) -> None:
        self._chain = chain or AniListChain()

    def spec(self) -> dict:
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "default_cron": "0 8 * * *",
        }

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("ranks"))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        ranks = list(options.get("ranks") or ["popular_this_season", "trending"])
        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        limit = int(options.get("limit") or 30)
        scan_limit = ranking_scan_limit(options)

        logger.info(f"开始抓取AniList榜单：选中榜单={ranks}")
        seen_keys: set[str] = set()

        for rank_key in ranks:
            if context.stopped():
                return
            if rank_key not in ANILIST_METHODS:
                continue

            method_name, rank_name = ANILIST_METHODS[rank_key]
            method = getattr(self._chain, method_name, None)
            if not method:
                logger.warning(f"MoviePilot平台链中不存在方法：AniListChain.{method_name}")
                continue

            rank_label = f"AniList榜单[{rank_name}]"
            try:
                items = self._paginate(
                    lambda page: method(page=page, count=limit),
                    scan_limit,
                    context,
                    rank_label,
                    "anilist_id",
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
                candidate = self._candidate_from_dict(
                    data,
                    min_year=min_year,
                    min_vote=min_vote,
                    seen_keys=seen_keys,
                    media_type="tv",
                    source=self.provider_id,
                    rank=rank_name,
                    rank_key=rank_key,
                    id_fields=(("anilist_id", "int"), ("tmdb_id", "int")),
                    unique_key_fields=("anilist_id", "tmdb_id"),
                )
                if candidate is None:
                    continue
                yield candidate
