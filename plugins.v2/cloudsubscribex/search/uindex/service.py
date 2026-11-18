"""UIndex 搜索服务实现。"""

from typing import Any, List, Optional

from app.log import logger
from app.schemas.types import MediaType

from .client import UIndexClient, UIndexError
from ..base import BaseMagnetSearchService
from ..magnet import media_titles
from ..matching import extract_year, unique_texts


class UIndexSearchService(BaseMagnetSearchService):
    source_key = "uindex"
    client_error = UIndexError

    def __init__(self, client: UIndexClient, result_limit: int = 20):
        super().__init__(client, result_limit)

    @classmethod
    def _is_mostly_ascii(cls, text: str) -> bool:
        """检查文本是否主要由 ASCII 字符构成（英文/外文标题）。"""
        if not text:
            return False
        stripped = "".join(ch for ch in text if ch.isalnum())
        if not stripped:
            return False
        ascii_count = sum(1 for ch in stripped if ord(ch) < 128)
        return (ascii_count / len(stripped)) >= 0.5

    @classmethod
    def _keywords(cls, mediainfo: Any, media_type: MediaType, season: Optional[int]) -> List[str]:
        raw_titles = media_titles(mediainfo)
        year = extract_year(getattr(mediainfo, "year", None))

        # 优先提取英文/原名标题（UIndex 为全外文BT索引库，优先使用原名）
        orig_title = str(
            getattr(mediainfo, "original_title", "")
            or getattr(mediainfo, "original_name", "")
            or getattr(mediainfo, "en_name", "")
            or ""
        ).strip()

        candidate_titles = []
        if orig_title and cls._is_mostly_ascii(orig_title):
            candidate_titles.append(orig_title)

        for t in raw_titles:
            if cls._is_mostly_ascii(t) and t not in candidate_titles:
                candidate_titles.append(t)

        # 若没有任何英文原名，兜底保留原始标题
        if not candidate_titles:
            candidate_titles = [t for t in raw_titles if t]

        keywords = []
        for t in candidate_titles:
            if media_type == MediaType.TV and season:
                keywords.append(f"{t} S{season:02d}")
            elif year:
                keywords.append(f"{t} {year}")
            keywords.append(t)
        return unique_texts(keywords)

    def _on_client_error(self, keyword: str, err: Exception) -> None:
        logger.warning(f"UIndex 检索关键词 '{keyword}' 失败：{err}")
