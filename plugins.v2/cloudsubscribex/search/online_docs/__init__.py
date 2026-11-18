"""在线文档搜索渠道。"""

from .client import OnlineDocumentClient, is_online_document_url, parse_online_document
from .service import OnlineDocumentSearchService

__all__ = [
    "OnlineDocumentClient",
    "OnlineDocumentSearchService",
    "is_online_document_url",
    "parse_online_document",
]
