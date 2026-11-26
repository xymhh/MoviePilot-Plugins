import html
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

from app.log import logger

from .security import UIndexSecurity, UIndexSecurityError
from ..base_client import BaseSearchClient
from ..http_client import (
    gated_idempotent_request,
    normalize_proxies,
    request_error_summary,
    requests,
)
from ..magnet import _HASH_REGEX, _MAGNET_REGEX, parse_size_str


class UIndexError(RuntimeError):
    """UIndex 请求或解析失败。"""


class UIndexClient(BaseSearchClient):
    """通过 UIndex 检索磁力资源。"""

    DEFAULT_BASE_URL = "https://uindex.org"
    _GATE_NAME = "UIndex"
    _CACHE_REGION = "uindex_search"
    _PROXY_STYLE = "raw"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    def __init__(
            self,
            base_url: str = DEFAULT_BASE_URL,
            proxy: Optional[str] = None,
            timeout: int = 20,
            request_interval: float = 1.0,
            request_timeout: Optional[int] = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            proxy=proxy,
            timeout=timeout,
            request_interval=request_interval,
            request_timeout=request_timeout,
        )
        self._security = UIndexSecurity(self._proxy)

    def _on_config_updated(self) -> None:
        self._security.update_config(proxy=self._proxy)

    def search(self, query: str) -> List[Dict[str, Any]]:
        keyword = str(query or "").strip()
        if not keyword:
            return []
        cache_key = keyword.casefold()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]

        url = f"{self.base_url}/search.php?search={quote(keyword)}"
        proxies = normalize_proxies(self._proxy)

        # 1. 优先执行标准 HTTP 快速请求（不需要盾时零浏览器开销）
        response = None
        request_error = None
        try:
            response = gated_idempotent_request(
                self._gate,
                requests.request,
                "GET",
                url,
                headers=self._HEADERS,
                proxies=proxies,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as error:
            logger.debug(
                f"UIndex 直连请求异常：{request_error_summary(error)}，准备尝试浏览器渲染"
            )
            request_error = error

        # 2. 仅在命中 Cloudflare 盾或直连异常时，才按需调起公共反盾浏览器
        if request_error is not None or self._security.is_challenge(response):
            logger.debug(f"UIndex 检测到 Cloudflare 盾防护，按需调用 CloakBrowser 浏览器过盾渲染：{keyword}")
        try:
            page_html = self._security.resolve_page(url, response, request_error)
        except UIndexSecurityError as error:
            raise UIndexError(str(error)) from error

        results = self._parse_search_page(page_html)
        with self._cache_lock:
            self._cache[cache_key] = results
        return [dict(item) for item in results]

    def _parse_search_page(self, page_html: str) -> List[Dict[str, Any]]:
        """解析搜索列表 HTML。"""
        results = []
        if not page_html:
            return results

        # 匹配每一行 <tr>...</tr>
        row_regex = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
        for row_match in row_regex.finditer(page_html):
            row_content = row_match.group(1)
            # 必须包含磁力链接或带有 btih 的内容
            mag_match = _MAGNET_REGEX.search(row_content)
            if not mag_match:
                continue

            magnet_url = html.unescape(mag_match.group(0))
            hash_match = _HASH_REGEX.search(magnet_url)
            if not hash_match:
                continue
            info_hash = hash_match.group(1).upper()

            # 提取标题
            # 优先从详情链接文本或 title 属性提取
            title = ""
            title_match = re.search(r"<a[^>]+href=[\"'][^\"']*details\.php[^\"']*[\"'][^>]*>(.*?)</a>", row_content,
                                    re.IGNORECASE | re.DOTALL)
            if title_match:
                title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
            if not title:
                # 尝试从 magnet 的 dn 参数提取
                dn_match = re.search(r"dn=([^&]+)", magnet_url)
                if dn_match:
                    title = unquote(dn_match.group(1))

            if not title:
                title = f"UIndex 资源 {info_hash[:8]}"

            # 提取文件大小
            size_bytes = parse_size_str(row_content)

            # 提取做种数
            seeders = 0
            seed_match = re.search(r"class=[\"'][^\"']*seeders?[^\"']*[\"'][^>]*>(\d+)</td>", row_content,
                                   re.IGNORECASE)
            if seed_match:
                seeders = int(seed_match.group(1))

            results.append({
                "url": magnet_url,
                "title": html.unescape(title).strip(),
                "size": size_bytes,
                "seeders": seeders,
                "info_hash": info_hash,
                "resource_type": "magnet",
                "source": "uindex",
                "source_url": f"{self.base_url}/search.php",
            })

        return results

    def clear_cache(self) -> int:
        with self._cache_lock:
            count = len(self._cache)
            self._cache.clear()
            return count
