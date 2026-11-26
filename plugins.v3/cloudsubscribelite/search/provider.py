"""搜索渠道 Provider 的唯一组装入口。

14 个搜索源原先各有一份 ``create_<源>_provider`` 工厂，做的都是同一件事：
把 service / client 按能力位装进 ``SearchProvider``。这里只留一个数据驱动的
``build_search_provider``，各源 definition 的 ``create_provider`` 直接调它，
只声明一张能力映射表（能力 → 角色）与一条策略：

* ``"service"``——该能力由传入的 service 提供；
* ``"client"``——该能力由传入的 client 提供；
* ``"budget"``——该能力由 ``service.budget``（积分账本）提供。

dian115 / hdhaven / hdhive 三家共用 ``POINTS_CAPABILITIES``，hdhaven 与
hdhive 在此基础上多加 ``RESOURCE_PREVIEW``；juying / woniu 共用
``DELEGATING_CAPABILITIES``。
"""

from typing import Any, Mapping, Optional

from ..core.search import SearchCapability, SearchPolicy, SearchProvider

#: 能力角色：由传入的 service 提供。
SERVICE_ROLE = "service"
#: 能力角色：由传入的 client 提供。
CLIENT_ROLE = "client"
#: 能力角色：由 service.budget（积分账本）提供。
BUDGET_ROLE = "budget"

#: 纯 magnet 源（piratebay / uindex / animegarden / mikan / pansou / online_docs）。
MAGNET_CAPABILITIES: Mapping[SearchCapability, str] = {
    SearchCapability.RESOURCE_SEARCH: SERVICE_ROLE,
    SearchCapability.CACHE_MAINTENANCE: SERVICE_ROLE,
}

#: 可解析的 magnet 源（seedhub = 纯 magnet + 资源解析）。
RESOLVABLE_MAGNET_CAPABILITIES: Mapping[SearchCapability, str] = {
    **MAGNET_CAPABILITIES,
    SearchCapability.RESOURCE_RESOLVE: SERVICE_ROLE,
}

#: 薄门面源（juying / woniu）：账户、签到、生命周期走客户端。
DELEGATING_CAPABILITIES: Mapping[SearchCapability, str] = {
    SearchCapability.RESOURCE_SEARCH: SERVICE_ROLE,
    SearchCapability.RESOURCE_RESOLVE: SERVICE_ROLE,
    SearchCapability.ACCOUNT: CLIENT_ROLE,
    SearchCapability.CHECKIN: CLIENT_ROLE,
    SearchCapability.CACHE_MAINTENANCE: SERVICE_ROLE,
    SearchCapability.LIFECYCLE: CLIENT_ROLE,
}

#: 片库：生命周期走服务自身（它自己管浏览器会话）。
PIANKU_CAPABILITIES: Mapping[SearchCapability, str] = {
    SearchCapability.RESOURCE_SEARCH: SERVICE_ROLE,
    SearchCapability.RESOURCE_RESOLVE: SERVICE_ROLE,
    SearchCapability.CACHE_MAINTENANCE: SERVICE_ROLE,
    SearchCapability.LIFECYCLE: SERVICE_ROLE,
}

#: 积分三件套共用（dian115 / hdhaven / hdhive）；hdhaven 与 hdhive
#: 在此基础上多加 RESOURCE_PREVIEW。
POINTS_CAPABILITIES: Mapping[SearchCapability, str] = {
    SearchCapability.RESOURCE_SEARCH: SERVICE_ROLE,
    SearchCapability.RESOURCE_UNLOCK: SERVICE_ROLE,
    SearchCapability.ACCOUNT: CLIENT_ROLE,
    SearchCapability.CHECKIN: CLIENT_ROLE,
    SearchCapability.POINT_BUDGET: BUDGET_ROLE,
    SearchCapability.CACHE_MAINTENANCE: SERVICE_ROLE,
    SearchCapability.LIFECYCLE: SERVICE_ROLE,
}

#: 积分三件套中带资源预览的两家（hdhaven / hdhive = 积分表 + 资源预览）。
POINTS_PREVIEW_CAPABILITIES: Mapping[SearchCapability, str] = {
    **POINTS_CAPABILITIES,
    SearchCapability.RESOURCE_PREVIEW: SERVICE_ROLE,
}

#: 盘链：解锁与解析走服务，账户、签到、生命周期走客户端。
PINGLIAN_CAPABILITIES: Mapping[SearchCapability, str] = {
    SearchCapability.RESOURCE_SEARCH: SERVICE_ROLE,
    SearchCapability.RESOURCE_UNLOCK: SERVICE_ROLE,
    SearchCapability.RESOURCE_RESOLVE: SERVICE_ROLE,
    SearchCapability.ACCOUNT: CLIENT_ROLE,
    SearchCapability.CHECKIN: CLIENT_ROLE,
    SearchCapability.POINT_BUDGET: BUDGET_ROLE,
    SearchCapability.CACHE_MAINTENANCE: SERVICE_ROLE,
    SearchCapability.LIFECYCLE: CLIENT_ROLE,
}


def build_search_provider(
        *,
        key: str,
        name: str,
        resource_types: Any,
        service: Any,
        client: Any = None,
        capabilities: Mapping[SearchCapability, str],
        policy: Optional[SearchPolicy] = None,
) -> SearchProvider:
    """按能力映射表组装一个 SearchProvider。

    ``capabilities`` 把每个能力位映射到 ``service`` / ``client`` / ``budget``
    三种角色之一；``policy`` 不传时用默认 ``SearchPolicy()``（在线文档行为）。
    """
    services = {}
    for capability, role in capabilities.items():
        if role == SERVICE_ROLE:
            services[capability] = service
        elif role == CLIENT_ROLE:
            services[capability] = client
        elif role == BUDGET_ROLE:
            services[capability] = service.budget
        else:
            raise ValueError(f"未知的搜索能力角色：{role}")
    return SearchProvider(
        key=key,
        name=name,
        resource_types=frozenset(resource_types),
        services=services,
        policy=policy if policy is not None else SearchPolicy(),
    )
