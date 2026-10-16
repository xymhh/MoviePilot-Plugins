"""片库搜索客户端：列表检索、资源缓存与直链解析。

过盾与人机验证协议统一由 :mod:`.security` 提供，本模块只负责 HTTP 检索、
缓存与链接解析，并复用共享请求门控做频率控制。
"""

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

from app.log import logger

from .security import PiankuGate, PiankuGateError
from ..http_client import (
    RequestGate,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)
from ...utils.cache import create_platform_ttl_cache


class PiankuError(RuntimeError):
    """片库请求、解析或过盾失败。"""


#: 并发解析的请求并发度与分批间隔（秒）：open.php 是站点自身的轻量跳转接口。
RESOLVE_CONCURRENCY = 5
RESOLVE_INTERVAL = 0.15


class PiankuClient:
    """片库搜索客户端。"""

    DEFAULT_BASE_URL = "https://4k.pianku.online"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    _ITEM_RE = re.compile(
        r'<a[^>]+href="(?:https?://[^"]*)?/voddetail/(?P<id>\d+)\.html"'
        r'[^>]*title="(?P<title>[^"]*)"[^>]*>(?P<body>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    _REMARKS_RE = re.compile(r'class="remarks"[^>]*>(?P<value>[^<]*)<', re.IGNORECASE)
    _SUBTITLE_RE = re.compile(r'class="subtitle"[^>]*>(?P<value>[^<]*)<', re.IGNORECASE)
    _OPEN_REDIRECT_RE = re.compile(
        r'location\.href\s*=\s*["\'](?P<target>[^"\']+)["\']', re.IGNORECASE
    )

    def __init__(
            self,
            base_url: str = DEFAULT_BASE_URL,
            proxy: Optional[str] = None,
            timeout: int = 30,
            request_interval: float = 1.5,
            detail_interval: float = 2.0,
            gate_timeout: int = 45,
    ) -> None:
        self.base_url = (
                str(base_url or self.DEFAULT_BASE_URL).strip().rstrip("/")
                or self.DEFAULT_BASE_URL
        )
        self._proxy = str(proxy or "").strip()
        self.timeout = max(5, int(timeout or 30))
        self.request_interval = max(0.5, float(request_interval or 1.5))
        self.detail_interval = max(0.2, float(detail_interval or 2.0))
        self.gate_timeout = max(15, int(gate_timeout or 45))
        #: 解析真实链接之间的最小间隔（不再占用公共请求门控的 ≥1s 队列）。
        self.resolve_interval = RESOLVE_INTERVAL
        self._request_gate = self._build_request_gate()
        self._page_cache = create_platform_ttl_cache("pianku_search", ttl=1800, maxsize=200)
        self._resource_cache = create_platform_ttl_cache("pianku_resources", ttl=6 * 3600, maxsize=200)
        self._link_cache = create_platform_ttl_cache("pianku_links", ttl=6 * 3600, maxsize=800)
        self._lock = threading.RLock()
        self._gate_protocol: Optional[PiankuGate] = None

    @property
    def proxy(self) -> str:
        return self._proxy

    def _build_request_gate(self) -> RequestGate:
        """复用共享门控：串行、按间隔限速，代理或地址变更时重建。"""
        return RequestGate.shared(
            "Pianku",
            f"{self.base_url}|{self._proxy}",
            request_interval=self.request_interval,
            minimum_interval=0.5,
            serial_requests=True,
        )

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Optional[str] = None,
            timeout: Optional[int] = None,
            request_interval: Optional[float] = None,
            detail_interval: Optional[float] = None,
            gate_timeout: Optional[int] = None,
    ) -> None:
        if base_url is not None:
            self.base_url = (
                    str(base_url).strip().rstrip("/") or self.DEFAULT_BASE_URL
            )
        if proxy is not None:
            self._proxy = str(proxy or "").strip()
        if timeout is not None:
            self.timeout = max(5, int(timeout or 30))
        if request_interval is not None:
            self.request_interval = max(0.5, float(request_interval or 1.5))
        if detail_interval is not None:
            self.detail_interval = max(0.2, float(detail_interval or 2.0))
        if gate_timeout is not None:
            self.gate_timeout = max(15, int(gate_timeout or 45))
        self._request_gate = self._build_request_gate()
        if self._gate_protocol is not None:
            self._gate_protocol.update_config(
                base_url=self.base_url,
                proxy=self._proxy,
                timeout=self.timeout,
                gate_timeout=self.gate_timeout,
            )

    def _security(self) -> PiankuGate:
        """按需创建过盾协议对象；会话与代理复用由 :class:`PiankuGate` 维护。"""
        if self._gate_protocol is None:
            self._gate_protocol = PiankuGate(
                self.base_url,
                proxy=self._proxy,
                timeout=self.timeout,
                gate_timeout=self.gate_timeout,
            )
        return self._gate_protocol

    def search_titles(self, keyword: str) -> List[Dict[str, Any]]:
        """按关键词检索详情条目（标题 / 年份 / 备注 / 详情链接）。"""
        text = str(keyword or "").strip()
        if not text:
            return []
        cache_key = text.casefold()
        with self._lock:
            cached = self._page_cache.get(cache_key)
        if cached is not None:
            return [dict(item) for item in cached]

        url = f"{self.base_url}/vodsearch/{quote(text)}----------1---.html"
        proxies = normalize_proxies(self._proxy)

        def _requester() -> Any:
            return requests.get(
                url,
                headers=self._HEADERS,
                proxies=proxies,
                timeout=self.timeout,
                allow_redirects=True,
            )

        try:
            response = gated_request(
                self._request_gate,
                _requester,
                retry_exceptions=(
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                ),
                max_retries=2,
                initial_delay=1.5,
            )
        except Exception as error:
            raise PiankuError(f"片库列表请求失败：{request_error_summary(error)}") from error
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            raise PiankuError(f"片库列表请求异常：HTTP {status}")

        items = self._parse_search_page(getattr(response, "text", "") or "")
        with self._lock:
            self._page_cache[cache_key] = items
        return [dict(item) for item in items]

    def _parse_search_page(self, page_html: str) -> List[Dict[str, Any]]:
        """解析搜索结果列表中的详情条目。"""
        results: List[Dict[str, Any]] = []
        seen = set()
        if not page_html:
            return results
        for match in self._ITEM_RE.finditer(page_html):
            vod_id = str(match.group("id") or "").strip()
            if not vod_id or vod_id in seen:
                continue
            seen.add(vod_id)
            body = match.group("body") or ""
            remarks = self._first_group(self._REMARKS_RE, body)
            subtitle = self._first_group(self._SUBTITLE_RE, body)
            results.append({
                "id": vod_id,
                "title": html.unescape(str(match.group("title") or "")).strip(),
                "remarks": html.unescape(remarks).strip(),
                "subtitle": html.unescape(subtitle).strip(),
                "url": f"{self.base_url}/voddetail/{vod_id}.html",
            })
        return results

    @staticmethod
    def _first_group(pattern: re.Pattern, text: str) -> str:
        matched = pattern.search(text or "")
        return matched.group("value") if matched else ""

    def fetch_resources(self, vod_id: Any) -> List[Dict[str, Any]]:
        """读取某个详情页的资源列表（磁力 / 网盘分享）。"""
        key = str(vod_id or "").strip()
        if not key:
            return []
        with self._lock:
            cached = self._resource_cache.get(key)
        if cached is not None:
            return [dict(item) for item in cached]

        try:
            items = self._security().fetch_resources(
                f"{self.base_url}/voddetail/{key}.html"
            )
        except PiankuGateError as error:
            raise PiankuError(str(error)) from error
        finally:
            self._sleep(self.detail_interval)

        with self._lock:
            self._resource_cache[key] = items
        return [dict(item) for item in items]

    def resolve_link(self, href: str, detail_url: str = "") -> str:
        """解析单个 open.php 跳转，返回真实磁力或分享链接。"""
        target_url = str(href or "").strip()
        if not target_url:
            return ""
        return self.resolve_links([target_url], detail_url).get(target_url, "")

    def resolve_links(
            self, hrefs: List[str], detail_url: str = ""
    ) -> Dict[str, str]:
        """批量解析 open.php：缓存命中 -> 会话凭据并发请求 -> 必要时回退浏览器会话。"""
        targets = list(dict.fromkeys(
            str(value or "").strip() for value in hrefs if str(value or "").strip()
        ))
        if not targets:
            return {}
        resolved_map: Dict[str, str] = {}
        pending: List[str] = []
        with self._lock:
            for target in targets:
                cached = self._link_cache.get(target)
                if cached:
                    resolved_map[target] = str(cached)
                else:
                    pending.append(target)
        if not pending:
            return resolved_map

        material = self._session_material()
        remaining = list(pending)
        if material:
            workers = max(1, min(RESOLVE_CONCURRENCY, len(pending)))
            remaining = []
            for offset in range(0, len(pending), workers):
                chunk = pending[offset:offset + workers]
                with ThreadPoolExecutor(
                        max_workers=workers,
                        thread_name_prefix="cloudsubscribe-pianku-resolve",
                ) as executor:
                    for target, link in zip(chunk, executor.map(
                            lambda href: self._fetch_link(href, material), chunk
                    )):
                        if link:
                            resolved_map[target] = link
                        else:
                            remaining.append(target)
                if offset + workers < len(pending) and self.resolve_interval:
                    self._sleep(self.resolve_interval)
            if resolved_map:
                with self._lock:
                    for target, link in resolved_map.items():
                        self._link_cache[target] = link

        if remaining:
            # 凭据不可用或被站点拒绝时，回退到浏览器会话内解析（含重新过盾）。
            fallback = self._resolve_in_browser(remaining, detail_url)
            resolved_map.update(fallback)
        return resolved_map

    def _session_material(self) -> Dict[str, Any]:
        """已过盾会话凭据（Cookie/UA），按 TTL 复用。"""
        try:
            return self._security().session_material()
        except Exception as error:
            logger.debug(f"片库会话凭据获取失败：{error}")
            return {}

    def _fetch_link(self, target: str, material: Dict[str, Any]) -> str:
        """宿主侧请求 open.php 并解析真实链接；凭据失效返回空串。"""
        try:
            response = requests.get(
                target,
                headers=self._resolve_headers(material),
                cookies=dict(material.get("cookies") or {}),
                timeout=self.timeout,
                allow_redirects=False,
                impersonate="chrome",
            )
        except Exception as error:
            logger.debug(f"片库资源解析请求异常：{error}")
            return ""
        status = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", None) or {}
        location = ""
        try:
            location = str(headers.get("location") or "").strip()
        except Exception:
            location = ""
        body = ""
        if status == 200 or (not location and status < 400):
            try:
                body = str(getattr(response, "text", "") or "")
            except Exception:
                body = ""
        if status in {401, 403}:
            # 凭据失效：丢弃缓存让下次重新过盾。
            self._security().clear_material()
        return self._extract_link(status, location, body, target)

    def _resolve_headers(self, material: Dict[str, Any]) -> Dict[str, str]:
        headers = dict(self._HEADERS)
        user_agent = str(material.get("user_agent") or "").strip()
        if user_agent:
            headers["User-Agent"] = user_agent
        headers["Referer"] = f"{self.base_url}/"
        return headers

    def _resolve_in_browser(
            self, targets: List[str], detail_url: str = ""
    ) -> Dict[str, str]:
        """浏览器会话内串行解析（兜底路径，同时负责会话失效后的重新过盾）。"""
        protocol = self._security()

        def _flow(page) -> Dict[str, str]:
            result: Dict[str, str] = {}
            verified = False
            for index, target in enumerate(targets):
                if index and self.resolve_interval:
                    page.wait_for_timeout(int(self.resolve_interval * 1000))
                link = ""
                try:
                    response = page.request.get(
                        target, max_redirects=0, timeout=self._request_timeout_ms
                    )
                    link = self._extract_resolved_link(response, target)
                except Exception as error:
                    logger.debug(f"片库资源解析异常：{error}")
                if not link and not verified:
                    try:
                        protocol.ensure_gate(page, detail_url or None)
                        protocol.clear_material()
                        verified = True
                    except PiankuGateError as error:
                        logger.warning(f"片库重新过盾失败，放弃本次资源解析：{error}")
                        break
                    try:
                        response = page.request.get(
                            target, max_redirects=0, timeout=self._request_timeout_ms
                        )
                        link = self._extract_resolved_link(response, target)
                    except Exception as error:
                        logger.debug(f"片库资源重试解析异常：{error}")
                if link:
                    result[target] = link
            return result

        try:
            fresh = protocol.run_with_page(_flow) or {}
        except PiankuGateError as error:
            raise PiankuError(str(error)) from error
        except Exception as error:
            raise PiankuError(f"片库资源解析失败：{error}") from error
        if fresh:
            with self._lock:
                for target, link in fresh.items():
                    self._link_cache[target] = link
        return fresh

    def _extract_resolved_link(self, response: Any, target_url: str) -> str:
        """从浏览器响应中取出真实资源链接。"""
        if response is None:
            return ""
        headers = getattr(response, "headers", None) or {}
        try:
            location = str(headers.get("location") or "").strip()
        except Exception:
            location = ""
        body = ""
        if not 300 <= int(getattr(response, "status", 0) or 0) < 400:
            try:
                body = response.text() or ""
            except Exception:
                body = ""
        return self._extract_link(
            int(getattr(response, "status", 0) or 0), location, body, target_url
        )

    def _extract_link(self, status: int, location: str, body: str, target_url: str) -> str:
        """统一解析：优先 Location（302 跳转），其次页面内联跳转脚本。"""
        if 300 <= int(status or 0) < 400:
            return urljoin(target_url, location) if location else ""
        resolved = self._parse_open_redirect(body)
        if resolved:
            return resolved
        return urljoin(target_url, location) if location else ""

    def _parse_open_redirect(self, body: str) -> str:
        matched = self._OPEN_REDIRECT_RE.search(body or "")
        if matched:
            return html.unescape(matched.group("target")).strip()
        return ""

    @property
    def _request_timeout_ms(self) -> int:
        return max(5000, int(self.timeout or 30) * 1000)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
        while time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        protocol, self._gate_protocol = self._gate_protocol, None
        if protocol is not None:
            protocol.close()

    def clear_cache(self) -> int:
        """清空检索/资源列表缓存；已解析出的真实链接仍按 TTL 保留。"""
        with self._lock:
            count = len(self._page_cache) + len(self._resource_cache)
            self._page_cache.clear()
            self._resource_cache.clear()
        return count
