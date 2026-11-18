"""搜索渠道注册表组装。"""

from typing import Any

from app.log import logger

from .scanner import SearchSourceRegistry
from ..core.search import SearchRegistry


def create_search_registry(
        owner: Any,
        **extra_context: Any,
) -> SearchRegistry:
    """根据当前配置自动扫描并组装可用搜索渠道。"""
    registry = SearchRegistry()
    config = {}
    plugin = getattr(owner, "_owner", None) or getattr(owner, "_plugin", None) or getattr(owner, "plugin", None)
    if plugin:
        if hasattr(plugin, "get_config"):
            try:
                config = dict(plugin.get_config() or {})
            except Exception:
                logger.debug("create_search_registry 分支忽略异常", exc_info=True)
        if not config and hasattr(plugin, "_applied_config"):
            config = dict(getattr(plugin, "_applied_config", None) or {})
        if not config and hasattr(plugin, "_config"):
            config = dict(getattr(plugin, "_config", None) or {})
    if not config and isinstance(owner, dict):
        config = dict(owner)
    elif not config:
        config = dict(getattr(owner, "__dict__", {}) or {})

    resource_types = tuple(getattr(owner, "_resource_type_order_config", ()) or config.get("resource_type_order", ()))
    context = {
        "owner": owner,
        "storage_owner": plugin or getattr(owner, "_plugin", None) or owner,
        "resource_types": resource_types,
        "proxy": getattr(owner, "_search_proxy", None) or config.get("search_proxy"),
        **extra_context,
    }

    skipped = {}
    outcome = {}
    for def_cls in SearchSourceRegistry.get_definitions():
        has_capability = getattr(def_cls, "has_capability", None)
        if callable(has_capability) and not has_capability("query"):
            # 推送型渠道（如频道监控）只走侧车，不参与搜索组装与超时预算。
            skipped[def_cls.id] = "no-query-capability"
            continue
        try:
            client = def_cls.create_client(config, context)
            service = def_cls.create_service(client, config, context)
            provider = def_cls.create_provider(service, client, config, context)
            if provider:
                registry.register(provider, replace=True)
                outcome[def_cls.id] = None
            else:
                skipped[def_cls.id] = "not-configured"
        except Exception as _err:
            logger.debug(f"搜索渠道 [{def_cls.id}] 注册失败：{_err}", exc_info=True)
            skipped[def_cls.id] = "registration-failed"

    if skipped:
        details = "、".join(
            f"{source_id}（{reason}）" for source_id, reason in skipped.items()
        )
        logger.info(f"搜索渠道已跳过：{details}")
    SearchSourceRegistry.record_registry_outcome({**outcome, **skipped})
    return registry
