"""115 上传数据面（OSS）的代理注入。

背景（2026-11-12 真机核实）：
- `p115client` 的 API 请求走 `urllib3_future_request` 的**模块级** `request`，
  `p115oss` 的 OSS 请求也自己 `from urllib3_future_request import request`，
  两者都不经过 `P115Client.request`，所以包装实例方法无法覆盖数据上传。
- 但 `urllib3_future_request.request_sync(session=...)` 接受一个 PoolManager，
  换成 `ProxyManager` 就能让该请求走代理（`ProxyManager` 是 `PoolManager` 子类）。
- 因此正确做法：给每次 OSS 调用显式传 `session=<ProxyManager>`，115 API 保持直连
  （避免 cookie/风控受影响），只有分片 PUT 与 OSS 元数据请求走代理。

本模块只负责「造一个带代理的 session」，构造失败一律返回 None 表示直连。
"""
from typing import Any, Dict, Optional

from app.log import logger

#: 缓存：代理 URL -> ProxyManager，避免每次分片都新建连接池
_SESSIONS: Dict[str, Any] = {}
_PROXY_MANAGER_UNAVAILABLE = False


def _cookie_jar():
    from http.cookiejar import CookieJar

    return CookieJar()


def proxy_manager(proxy_url: str) -> Optional[Any]:
    """按代理 URL 造一个 urllib3_future ProxyManager；失败返回 None（直连）。"""
    global _PROXY_MANAGER_UNAVAILABLE
    url = str(proxy_url or "").strip()
    if not url or _PROXY_MANAGER_UNAVAILABLE:
        return None
    cached = _SESSIONS.get(url)
    if cached is not None:
        return cached
    try:
        from urllib3_future import ProxyManager
    except ImportError:
        _PROXY_MANAGER_UNAVAILABLE = True
        logger.warning("115 上传代理未生效：urllib3_future 不可用，保持直连")
        return None
    try:
        pool = ProxyManager(url, num_pools=16, maxsize=64)
        # request_sync 会读 session.cookies 复用 cookie jar
        setattr(pool, "cookies", _cookie_jar())
        _SESSIONS[url] = pool
        logger.info("115 上传数据面已启用代理（OSS 分片走代理，API 直连）")
        return pool
    except Exception as error:  # noqa: BLE001 - 代理不可用时必须能退回直连
        logger.warning(f"115 上传代理不可用，保持直连：{error}")
        return None


def oss_request_kwargs(proxy_url: str) -> Dict[str, Any]:
    """给 p115oss 各函数用的额外 kwargs；无代理时返回空字典。"""
    pool = proxy_manager(proxy_url)
    return {"session": pool} if pool is not None else {}


def reset_cache() -> None:
    """测试/配置变更用：清空连接池缓存。"""
    _SESSIONS.clear()
