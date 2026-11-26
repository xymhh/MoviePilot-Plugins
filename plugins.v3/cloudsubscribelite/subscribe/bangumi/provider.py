"""Bangumi 自动订阅渠道（对接 MoviePilot 平台推荐功能）。"""
from __future__ import annotations

from typing import Iterator, Optional

from app.chain.recommend import RecommendChain
from app.log import logger

from ...core.subscribe import MediaCandidate, SubscribeContext
from ...core.subscribe.provider import RankingProvider, ranking_scan_limit
from ...core.subscribe.registry import register


@register
class BangumiSubscribeProvider(RankingProvider):
    provider_id = "bangumi"
    provider_name = "Bangumi榜单"

    def __init__(self, chain: Optional[RecommendChain] = None) -> None:
        self._chain = chain or RecommendChain()

    def spec(self) -> dict:
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "default_cron": "0 8 * * *",
        }

    def has_listening(self, options: dict) -> bool:
        return bool(options.get("enabled", True))

    def fetch(self, options: dict, context: SubscribeContext) -> Iterator[MediaCandidate]:
        min_vote = float(options.get("min_vote") or 0.0)
        min_year = int(options.get("min_year") or 0)
        limit = int(options.get("limit") or 50)
        scan_limit = ranking_scan_limit(options)

        logger.info("开始抓取Bangumi每日放送...")
        method = getattr(self._chain, "bangumi_calendar", None)
        if not method:
            logger.warning("MoviePilot平台推荐链中不存在方法：RecommendChain.bangumi_calendar")
            return

        rank_label = "Bangumi每日放送"
        try:
            items = self._paginate(
                lambda page: method(page=page, count=limit),
                scan_limit,
                context,
                rank_label,
                "bangumi_id",
            )
            if items is None:
                return
        except Exception as error:
            logger.error(f"抓取{rank_label}失败：{error}")
            return

        logger.info(f"{rank_label}抓取成功：返回 {len(items)} 条数据")
        seen_keys: set[str] = set()

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
                rank="每日放送",
                rank_key=None,
                id_fields=(("bangumi_id", "int"),),
                unique_key_fields=("bangumi_id",),
                meta_extra={"air_date": data.get("air_date")},
            )
            if candidate is None:
                continue
            yield candidate
