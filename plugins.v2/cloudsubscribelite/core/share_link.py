"""分享短链规范化：把中转域名展开成目标网盘的真实分享链接。

线上背景（2026-11-02 账本实测）：1588 条账本里 44 条 ``参数错误。``，全部来自
``pan.qoark.cn``。该域名是夸克的中转短链（实测 302 → ``pan.quark.cn/s/<真实ID>``），
但插件不认识它：

1. ``resource_type_from_url`` 查不到域名 → 类型为空；
2. ``_resource_provider_for_url`` 拿不到 provider → 回退默认 115 分享服务；
3. 115 的接口解析夸克链接 → 返回「参数错误。」，账本里只留一句没有任何信息量的话。

本模块在资源进入校验/转存前把短链换成真实链接，只对白名单域名发请求，失败原样
返回（宁可走旧路径报错，也不能让短链解析把转存流程卡住）。

缓存刻意用进程内字典：短链映射是稳定的，热重载丢一次只多一次 HEAD 请求。
"""

import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Tuple
from urllib.parse import urlparse

#: 已知的中转短链域名。新增前先实测 302 目标是哪个网盘。
SHORT_LINK_HOSTS = frozenset({"qoark.cn"})

_SHORT_LINK_TTL = 24 * 60 * 60
_SHORT_LINK_MAXSIZE = 2000
_SHORT_LINK_TIMEOUT = 6
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cache: Dict[str, Tuple[str, float]] = {}


def _host_of(url: str) -> str:
    try:
        return str(urlparse(str(url or "").strip()).hostname or "").casefold()
    except ValueError:
        return ""


def is_short_link(url: str) -> bool:
    """该链接是否属于需要展开的中转短链域名。"""
    host = _host_of(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in SHORT_LINK_HOSTS)


def _cache_get(key: str) -> str:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return ""
        target, expires_at = hit
        if expires_at <= now:
            _cache.pop(key, None)
            return ""
        return target


def _cache_put(key: str, target: str) -> None:
    with _cache_lock:
        if len(_cache) >= _SHORT_LINK_MAXSIZE:
            for stale in [item for item, (_, exp) in _cache.items() if exp <= time.time()]:
                _cache.pop(stale, None)
        _cache[key] = (target, time.time() + _SHORT_LINK_TTL)


def _resolve(url: str) -> str:
    """跟随 302 拿到最终地址；先 HEAD，失败再 GET（只读响应头即关闭）。"""
    request = urllib.request.Request(url, method="HEAD")
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=_SHORT_LINK_TIMEOUT) as response:
            final = str(response.geturl() or "").strip()
            if final and final != url:
                return final
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
        logger.debug("短链 HEAD 展开失败，改走 GET：%s（%s）", url, error, exc_info=True)
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=_SHORT_LINK_TIMEOUT) as response:
            return str(response.geturl() or "").strip() or url
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as error:
        logger.debug("短链 GET 展开失败，保持原链接：%s（%s）", url, error)
        return url


def canonicalize_share_url(url: str) -> str:
    """把中转短链换成真实分享链接；非短链、解析失败或目标仍是短链时原样返回。"""
    raw = str(url or "").strip()
    if not is_short_link(raw):
        return raw
    cached = _cache_get(raw)
    if cached:
        return cached
    resolved = _resolve(raw)
    if not resolved or resolved == raw or _host_of(resolved) == _host_of(raw):
        # 域名没变说明根本没跳转（或跳回了自己），按解析失败处理
        return raw
    _cache_put(raw, resolved)
    return resolved
