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

# 注意：这里**不能**写 `from ..core.media import media_identity`。
# core/__init__.py 会 import utils（notify_template），而 core.api.sync 又
# `from .. import ...` 回到 core 包——若 utils 再反向 import core.media，
# 同一线程递归拿同一把模块锁，MP 热重载时报
# `deadlock detected by _ModuleLock('app.plugins.cloudsubscribex.core.api.sync')`
# （2026-11-12 真机 13:51 / 16:38 两次复现，导致插件更新失败）。
# 需要 media_identity 的模块一律直接 `from ...core.media import media_identity`。

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
]
