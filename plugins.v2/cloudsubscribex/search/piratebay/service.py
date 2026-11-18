"""海盗湾搜索服务实现。"""

from typing import Any, Dict, List, Optional

from app.schemas.types import MediaType

from .client import PirateBayClient, PirateBayError
from ..base import BaseMagnetSearchService
from ..matching import extract_year, unique_texts


class PirateBaySearchService(BaseMagnetSearchService):
    source_key = "piratebay"
    client_error = PirateBayError

    def __init__(self, client: PirateBayClient, result_limit: int = 20):
        super().__init__(client, result_limit)

    @staticmethod
    def _keywords(mediainfo: Any, media_type: MediaType, season: Optional[int]) -> List[str]:
        titles = []
        # 英文/原名优先，因为海盗湾主要是英文标题
        for attr in ("original_title", "original_name", "title"):
            val = str(getattr(mediainfo, attr, "") or "").strip()
            if val and val not in titles:
                titles.append(val)

        year = extract_year(getattr(mediainfo, "year", None))
        keywords = []
        for t in titles:
            if media_type == MediaType.TV and season:
                keywords.append(f"{t} S{season:02d}")
                keywords.append(f"{t} Season {season}")
            elif year:
                keywords.append(f"{t} {year}")
            keywords.append(t)
        valid_keywords = [
            kw for kw in unique_texts(keywords)
            if any("a" <= c.lower() <= "z" for c in kw)
        ]
        return valid_keywords

    def _client_search(self, keyword: str) -> List[Dict[str, Any]]:
        # 200 is Video category in PirateBay
        return self._client.search(keyword, cat=200)
