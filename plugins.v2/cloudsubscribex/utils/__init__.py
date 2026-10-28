"""工具模块。"""
from .cache import (
    cached_resource_call,
    create_platform_ttl_cache,
    normalize_platform_cache_key,
)
from .file_matcher import FileMatcher
from .file_parser import MediaFileParser
from .magnet import (
    DEFAULT_METADATA_URL_TEMPLATE,
    clear_magnet_metadata_cache,
    configure_magnet_metadata_url,
    parse_magnet_metadata,
)
from .strm import (
    StrmGenerator,
    StrmTemplateError,
    plan_share_strm_files as plan_share_strm_files,
)
from .strm_scan import rebuild_strm_tree
from .strm_export import rebuild_strm_by_export_tree
from ..core.media import media_identity

__all__ = [
    "FileMatcher",
    "MediaFileParser",
    "StrmGenerator",
    "StrmTemplateError",
    "plan_share_strm_files",
    "rebuild_strm_tree",
    "rebuild_strm_by_export_tree",
    "parse_magnet_metadata",
    "DEFAULT_METADATA_URL_TEMPLATE",
    "configure_magnet_metadata_url",
    "clear_magnet_metadata_cache",
    "cached_resource_call",
    "create_platform_ttl_cache",
    "normalize_platform_cache_key",
    "media_identity",
]
