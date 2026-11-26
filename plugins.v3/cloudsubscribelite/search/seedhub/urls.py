"""SeedHub 站点地址规范化（纯函数，便于单测）。

站点 2026-11 起从 seedhub.cc 迁到 sidhub.cc，旧域名整站 302；直接用新域名
能省一跳，也避免 Cloudflare 在跳转途中重新质询。
"""

from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://sidhub.cc"

_LEGACY_HOSTS = {
    "seedhub.cc",
    "www.seedhub.cc",
    "seedhub.top",
    "www.seedhub.top",
    "seedhub.pro",
    "www.seedhub.pro",
    "seedhub.icu",
    "www.seedhub.icu",
}


def normalize_base_url(url: str, fallback: str = DEFAULT_BASE_URL) -> str:
    """旧域名统一换成新域名；其它自定义地址原样返回（去掉尾部斜杠）。"""
    text = str(url or "").strip()
    if not text:
        return fallback
    host = (urlparse(text).hostname or "").strip().lower()
    if host in _LEGACY_HOSTS:
        return fallback
    return text.rstrip("/")
