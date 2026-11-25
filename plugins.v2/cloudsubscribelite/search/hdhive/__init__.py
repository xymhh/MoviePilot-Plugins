"""HDHive 官方开放 API 与 WebAPI 客户端。"""

from .open import (
    HDHiveOpenAPIClient,
    HDHiveOpenAPIError,
)
from .service import HDHiveSearchService
from .web import (
    HDHIVE_DETAIL_RESOURCE_TYPES,
    HDHIVE_RESOURCE_TYPES,
    HDHiveClient,
    HDHiveResourceService,
    HDHiveWebError,
    valid_share_url,
)

__all__ = [
    "HDHiveClient",
    "HDHiveResourceService",
    "HDHiveWebError",
    "HDHiveOpenAPIClient",
    "HDHiveOpenAPIError",
    "HDHiveSearchService",
    "HDHIVE_DETAIL_RESOURCE_TYPES",
    "HDHIVE_RESOURCE_TYPES",
    "valid_share_url",
]
