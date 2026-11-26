"""在线文档搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import OnlineDocumentClient
from .service import OnlineDocumentSearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition
from ..provider import MAGNET_CAPABILITIES, build_search_provider


class OnlineDocsSourceDefinition(SearchSourceDefinition):
    """在线文档搜索渠道规范。"""

    id = "online_docs"
    name = "在线文档"
    icon = "mdi-file-document-outline"
    order = 110

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        return OnlineDocumentClient(
            documents=cls.config_value(config, "online_docs", []) or [],
            resource_types=cls.config_value(config, "online_docs_resource_types", []) or [],
            proxy=(context or {}).get("proxy"),
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="online_docs",
                title="在线文档",
                icon="mdi-file-document-outline",
                fields=[
                    FieldSpec(
                        key="online_docs",
                        label="在线文档",
                        type="online-documents",
                        default=[{"url": "", "resource_types": []}],
                        hint="每个文档可分别选择一个或多个资源类型。",
                        cols=12,
                    ),
                    FieldSpec(
                        key="test_online_docs",
                        label="测试搜索",
                        type="test-source",
                        source="online_docs",
                        cols=12,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_service(cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        if not client:
            return None
        return OnlineDocumentSearchService(client)

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        resource_types = ctx.get("resource_types", ())
        if not service or not resource_types:
            return None
        return build_search_provider(
            key="online_docs",
            name="在线文档",
            resource_types=resource_types,
            service=service,
            capabilities=MAGNET_CAPABILITIES,
        )
