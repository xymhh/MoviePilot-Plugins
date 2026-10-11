"""HDHaven Turnstile 求解：复用统一的常驻反盾浏览器会话。"""

from typing import Any

from ..cloudflare import (
    BrowserPageSession,
    mint_turnstile_token,
    mount_turnstile_page,
)


class HDHavenTurnstile:
    """复用轻量浏览器，仅生成 HDHaven 接口使用的一次性 Turnstile token。"""

    _STATE_NAME = "hdhavenVerification"

    def __init__(self, base_url: str = "https://hdhaven.com", proxy: Any = None):
        self._base_url = str(base_url or "").rstrip("/")
        self._session = BrowserPageSession(
            "HDHaven-Turnstile",
            proxy=proxy,
            timeout=30,
            prepare=lambda page: mount_turnstile_page(
                page, f"{self._base_url}/login"
            ),
        )

    def token(self, site_key: str, action: str = "login", timeout: int = 30) -> str:
        if not site_key:
            raise ValueError("HDHaven 求解 Turnstile 需要有效的 site_key")
        deadline = max(5.0, float(timeout or 45))
        return mint_turnstile_token(
            self._session,
            str(site_key).strip(),
            action,
            state_name=self._STATE_NAME,
            label="HDHaven",
            deadline=deadline,
        )

    def close(self) -> None:
        """关闭求解器与浏览器进程。"""
        self._session.close()
