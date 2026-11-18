"""平台入口共享的聚合与业务调用。

v1.24.0 拆为三个 mixin（platform_overview / platform_agent /
platform_links），本文件只留薄壳：类常量、__init__（三份 TTL 缓存）
与跨模块共用的 extract_resource_links。方法名与解析路径不变。
"""

import re
from typing import Any, Iterable, List

from app.schemas.types import MediaType  # noqa: F401  (v1.23.0 模块面：测试从本模块取 MediaType)
from ...core import OwnerDelegator
from ...utils.cache import create_platform_ttl_cache
from .platform_agent import PlatformAgentMixin
from .platform_links import PlatformLinkMixin
from .platform_overview import PlatformOverviewMixin


class PlatformIntegrationService(
        OwnerDelegator,
        PlatformOverviewMixin,
        PlatformAgentMixin,
        PlatformLinkMixin,
):
    """统一服务于页面、仪表盘、命令、工作流与智能体。"""
    _OVERVIEW_TTL_SECONDS = 3.0
    _RESOURCE_LINK_PATTERN = re.compile(
        r"ed2k://\|file\|.*?\|/|magnet:\?\S+|https?://\S+",
        re.IGNORECASE,
    )
    def __init__(self, owner):
        super().__init__(owner)
        self._overview_cache = create_platform_ttl_cache(
            "platform:overview",
            owner,
            maxsize=4,
            ttl=int(self._OVERVIEW_TTL_SECONDS),
        )
        self._agent_resource_cache = create_platform_ttl_cache(
            "platform:agent_resources",
            owner,
            maxsize=256,
            ttl=30 * 60,
        )
        self._link_selection_cache = create_platform_ttl_cache(
            "platform:link_selections",
            owner,
            maxsize=256,
            ttl=30 * 60,
        )

    @staticmethod
    def extract_resource_links(value: Any) -> List[str]:
        if isinstance(value, (list, tuple, set)):
            candidates: Iterable[Any] = value
        else:
            candidates = PlatformIntegrationService._RESOURCE_LINK_PATTERN.findall(
                str(value or "")
            )
        links = []
        seen = set()
        for candidate in candidates:
            link = str(candidate or "").strip().rstrip(",，。;；)")
            if link and link not in seen:
                seen.add(link)
                links.append(link)
        return links[:50]
