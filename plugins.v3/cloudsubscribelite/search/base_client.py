"""两个磁力搜索客户端（PirateBay / UIndex）共用的 ``__init__`` 骨架。

14 个客户端的 ``__init__`` 签名互不兼容（参数名/必填项/默认值各不
相同），一次全量统一会改行为；这里只抽 PirateBay 与 UIndex 这一对——
它们的 base_url 归一化、timeout/interval 钳制、门控创建、缓存、锁、
proxy 属性、update_config 除两点外逐字相同。两点分歧参数化：
* proxy 归一化：PirateBay 用 ``normalize_proxy_address``（补 scheme），
  UIndex 只做 ``str`` strip；由 ``_PROXY_STYLE`` 二选一；
* UIndex 有 ``request_timeout`` 覆盖 timeout，且 ``update_config`` 末尾
  要同步 ``_security``；前者是 ``__init__`` / ``update_config`` 的可选
  参数，后者是 ``_on_config_updated`` 钩子。
其余 12 个客户端（SeedHub/AnimeGarden/Mikan/Juying/Woniu/Pianku/
PanSou/OnlineDocs/Dian115/HDHaven/HDHive web+open/Pinglian）分歧更大
（session 创建方式、账号字段、门控 identity 模板、interval 参数名），
本次不动，基类留作以后迁移的落点。

知道的行为变化（仅一处，加法）：``PirateBayClient.update_config``
现在也接受 ``request_timeout`` 关键字（以前不接受），不传时行为不变；
没有调用方传它。
"""

from __future__ import annotations

import threading
from typing import Optional

from .http_client import RequestGate, normalize_proxy_address
from ..utils.cache import create_platform_ttl_cache


class BaseSearchClient:
    """PirateBay / UIndex 共用的客户端初始化骨架。"""

    DEFAULT_BASE_URL = ""
    _GATE_NAME = ""
    _CACHE_REGION = ""
    _PROXY_STYLE = "address"  # "address" | "raw"

    def __init__(
        self,
        *,
        base_url=None,
        proxy=None,
        timeout: int = 20,
        request_interval: float = 1.0,
        request_timeout: Optional[int] = None,
    ) -> None:
        self._raw_base_url = str(base_url or self.DEFAULT_BASE_URL).strip()
        self.base_url = self._raw_base_url.rstrip("/") or self.DEFAULT_BASE_URL
        self._proxy_style = self._PROXY_STYLE
        self._proxy = self._normalize_proxy(proxy)
        effective_timeout = (
            request_timeout if request_timeout is not None else timeout
        )
        self.timeout = max(5, int(effective_timeout or 20))
        self.request_interval = max(0.2, float(request_interval or 1.0))
        self._gate = RequestGate.shared(
            self._GATE_NAME,
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )
        self._cache = create_platform_ttl_cache(
            self._CACHE_REGION, ttl=1800, maxsize=500
        )
        self._cache_lock = threading.Lock()

    def _normalize_proxy(self, proxy) -> str:
        if self._proxy_style == "address":
            return normalize_proxy_address(proxy)
        return str(proxy or "").strip()

    @property
    def proxy(self) -> str:
        return self._proxy

    def update_config(
        self,
        base_url: Optional[str] = None,
        proxy: Optional[str] = None,
        timeout: Optional[int] = None,
        request_interval: Optional[float] = None,
        request_timeout: Optional[int] = None,
    ) -> None:
        if base_url is not None:
            raw = str(base_url).strip()
            self._raw_base_url = raw or self.DEFAULT_BASE_URL
            self.base_url = self._raw_base_url.rstrip("/") or self.DEFAULT_BASE_URL
        if proxy is not None:
            self._proxy = self._normalize_proxy(proxy)
        effective_timeout = (
            request_timeout if request_timeout is not None else timeout
        )
        if effective_timeout is not None:
            self.timeout = max(5, int(effective_timeout or 20))
        if request_interval is not None:
            self.request_interval = max(0.2, float(request_interval or 1.0))
        self._gate = RequestGate.shared(
            self._GATE_NAME,
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )
        self._on_config_updated()

    def _on_config_updated(self) -> None:
        """``update_config`` 末尾的钩子：UIndex 在此同步 ``_security``。"""
