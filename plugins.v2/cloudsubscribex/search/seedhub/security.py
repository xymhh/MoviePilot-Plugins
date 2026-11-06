"""SeedHub 反盾协议：直连优先，命中 Cloudflare 盾时用平台浏览器仿真。

SeedHub 会对频繁请求返回 Cloudflare 质询；本模块负责：

* 维护浏览器仿真得到的 User-Agent / Cookie，并在后续直连中复用；
* 识别质询响应并按需调起平台浏览器快照（含失效后的冷却）；
* 统一的文本获取入口：重试、盾回退与错误归类。
"""

import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from ..cloudflare import (
    browser_proxy,
    get_cloudflare_solver,
    is_cloudflare_challenge,
    playwright_snapshot,
)
from ..http_client import RequestGate, gated_request, requests
from app.log import logger


class SeedHubSecurity:
    """SeedHub 请求协议：直连 + 浏览器仿真回退，并复用仿真出的指纹。

    ``error_type`` 由调用方注入渠道错误类型，避免为拆分再造一套异常体系。
    """

    def __init__(
            self,
            *,
            base_url: str,
            proxy: Any = None,
            request_timeout: int = 20,
            headers: Optional[Dict[str, str]] = None,
            proxies: Optional[Any] = None,
            error_type: Callable[..., BaseException],
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self._browser_proxy = browser_proxy(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        self._headers = dict(headers or {})
        self._proxies = proxies
        self._error_type = error_type
        self._gate: Optional[RequestGate] = None
        self._lock = threading.RLock()
        self._state_version = 0
        self._cookie_header = ""
        self._user_agent = ""

    def bind_gate(self, gate: RequestGate) -> None:
        """绑定共享请求门控（用于浏览器快照与风控冷却）。"""
        self._gate = gate

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Any = None,
            request_timeout: Optional[int] = None,
            proxies: Optional[Any] = None,
    ) -> None:
        if base_url is not None:
            self.base_url = str(base_url or "").rstrip("/")
        if proxy is not None:
            self._browser_proxy = browser_proxy(proxy)
        if request_timeout is not None:
            self._request_timeout = max(5, min(int(request_timeout or 20), 60))
        if proxies is not None:
            self._proxies = proxies

    @staticmethod
    def is_challenge(response: Any) -> bool:
        """统一判断响应是否命中 Cloudflare 质询页（可直接用作门控挑战识别器）。"""
        if response is None:
            return False
        return is_cloudflare_challenge(
            str(getattr(response, "text", "") or ""),
            int(getattr(response, "status_code", 0) or 0),
            getattr(response, "headers", None) or {},
        )

    @property
    def state_version(self) -> int:
        with self._lock:
            return self._state_version

    def request_headers(self) -> Dict[str, str]:
        """基础请求头 + 浏览器仿真得到的 UA / Cookie。"""
        with self._lock:
            headers = dict(self._headers)
            if self._user_agent:
                headers["User-Agent"] = self._user_agent
            if self._cookie_header:
                headers["Cookie"] = self._cookie_header
            return headers

    def request_once(self, url: str):
        return gated_request(
            self._gate,
            requests.get,
            url,
            impersonate="chrome",
            headers=self.request_headers(),
            proxies=self._proxies,
            timeout=(8, self._request_timeout),
            allow_redirects=True,
        )

    def get_text(self, url: str) -> str:
        """带重试与盾回退的页面文本获取。"""
        last_error = ""
        for attempt in range(2):
            try:
                observed_version = self.state_version
                response = self.request_once(url)
                text = response.text or ""
                if self.is_challenge(response):
                    browser_text = self._browser_text(url, observed_version)
                    if browser_text:
                        return browser_text
                    raise self._error_type(
                        "SeedHub 被 Cloudflare 人机验证拦截（浏览器自动过盾失败）；"
                        "可在插件设置里给 SeedHub 配置代理或稍后重试"
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                response.raise_for_status()
                return text
            except self._error_type:
                raise
            except requests.exceptions.RequestException as error:
                last_error = type(error).__name__
                if attempt == 0:
                    time.sleep(0.3)
                    continue
        raise self._error_type(f"SeedHub 请求失败：{last_error or '未知错误'}")

    def _browser_text(self, url: str, observed_version: int) -> str:
        """用平台浏览器仿真获取页面，并把指纹写回后续直连。"""
        with self._lock:
            if self._state_version != observed_version:
                # 其它线程已刷新过指纹，先按新指纹重试一次直连。
                try:
                    response = self.request_once(url)
                    text = response.text or ""
                    if response.ok and not self.is_challenge(response):
                        return text
                except requests.exceptions.RequestException:
                    logger.debug("_browser_text 分支忽略异常", exc_info=True)

            # 站点 2026-11 起改用交互式 Turnstile：一次性快照拿到的仍是质询页。
            # 先用「会点验证框并等待 cf_clearance」的常驻浏览器过盾，失败再退回
            # 原来的单次快照（老环境没有 cloakbrowser 时 solver 会返回 None）。
            solved = self._solve_with_browser(url)
            if solved:
                return solved

            result = playwright_snapshot(
                url, self._browser_proxy, max(30, self._request_timeout), self._gate
            )
            if not isinstance(result, dict):
                return ""
            text = str(result.get("text") or "")
            if not text or is_cloudflare_challenge(text):
                self._gate.activate_cooldown(30, reason="SeedHub 浏览器验证")
                return ""
            self._apply_fingerprint(result.get("cookies"), result.get("user_agent"))
            return text

    def _solve_with_browser(self, url: str) -> str:
        """用常驻反盾浏览器（会点验证框）过 Cloudflare 并返回页面 HTML。"""
        try:
            solver = get_cloudflare_solver()
        except Exception:
            logger.debug("SeedHub 反盾浏览器初始化失败", exc_info=True)
            return ""
        if solver is None:
            return ""
        try:
            result = solver.solve(
                url, proxy=self._browser_proxy, timeout=max(35, self._request_timeout)
            )
        except Exception:
            logger.debug("SeedHub 反盾浏览器过盾异常", exc_info=True)
            return ""
        if not isinstance(result, dict):
            return ""
        text = str(result.get("html") or "")
        if not text or is_cloudflare_challenge(text):
            return ""
        self._apply_fingerprint(result.get("cookies"), result.get("user_agent"))
        logger.info("SeedHub 已通过 Cloudflare 人机验证（常驻浏览器），复用其指纹继续直连")
        return text

    def _apply_fingerprint(self, cookies: Any, user_agent: Any) -> None:
        """保存浏览器会话的 Cookie / UA；域名不匹配本站的一律丢弃。"""
        host = str(urlparse(self.base_url).hostname or "").lower()
        parts = []
        for cookie in cookies or []:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if domain and host != domain and not host.endswith(f".{domain}"):
                # 站点迁移后可能停在跳转域名上，同站别名（sidhub/seedhub）放行
                if not domain.split(".")[0].endswith(("seedhub", "sidhub")):
                    continue
            parts.append(f"{name}={value}")
        self._cookie_header = "; ".join(parts)
        self._user_agent = str(user_agent or "")
        self._state_version += 1
