"""TG 频道监控作为「推送型」搜索渠道的自描述规范。

频道监控与其它搜索渠道在**产品层**并列：同一份渠道目录、同一套开关与统计；
但它是常驻推送流（游标、flood wait、验证码机器人都在 ``core/tg_monitor.py``），
不是「按关键词拉取」的查询源，因此：

* ``capabilities`` 里只有 ``push`` / ``notify``，没有 ``query``；
* ``create_provider`` 返回 ``None``——搜索组装会跳过它（``search/registry.py``）；
* 配置表单继续由插件原有的「频道监控」分组提供，这里不重复声明字段，
  避免同一份 ``tg_*`` 配置被两处表单写坏。

架构见 ``docs/plan/2026-10-28-频道监控并入搜索渠道-架构.md``。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

from ...core.definitions import GroupSpec, SearchSourceDefinition


class TgChannelsSourceDefinition(SearchSourceDefinition):
    """频道监控渠道：推送发现，不提供关键词搜索。"""

    id = "tgchannels"
    name = "频道监控"
    icon = "mdi-telegram"
    color = "light-blue"
    order = 90
    capabilities: FrozenSet[str] = frozenset({"push", "notify"})

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        """不声明字段：配置仍由插件「频道监控」分组维护。"""
        return []

    @classmethod
    def create_provider(
            cls,
            service: Any,
            client: Any,
            config: Dict[str, Any],
            context: Optional[Any] = None,
    ) -> Any:
        """推送型渠道没有 SearchProvider，返回 None 让搜索组装跳过。"""
        return None
