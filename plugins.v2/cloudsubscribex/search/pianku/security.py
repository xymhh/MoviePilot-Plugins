"""片库风控与前端资源协议：人机验证（Cloudflare Turnstile）及资源列表获取。

片库的详情页资源列表由 ``fetch.php`` 流式下发，且必须先完成 Cloudflare Turnstile
人机验证；本模块把这一层协议集中在一处：

* 复用公共反盾会话 (:class:`~..cloudflare.BrowserPageSession`) 与验证点击工具；
* 一次过盾、多次复用，按详情页依次渲染并等待推流结束再交付资源行；
* 会话失效时可由调用方传入同一页面重新过盾，避免嵌套会话调用。
"""

import time
from typing import Any, Callable, Dict, List, Optional

from app.log import logger

from ..cloudflare import (
    BrowserPageSession,
    mount_turnstile_page,
    turnstile_page_token,
)


class PiankuGateError(RuntimeError):
    """片库人机验证或资源获取失败。"""


_GATE_STATUS_JS = """
() => fetch('/openapi/resources/turnstile_gate.php', {cache: 'no-store', credentials: 'same-origin'})
    .then(response => response.json())
    .catch(() => ({}))
"""

_GATE_SUBMIT_JS = """
async (token) => {
    const response = await fetch('/openapi/resources/turnstile_gate.php', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token})
    });
    return await response.json().catch(() => ({}));
}
"""

_EXTRACT_ROWS_JS = """
() => Array.from(document.querySelectorAll('#resContainer .res-row')).map(row => {
    const link = row.querySelector('a.res-title');
    const pwd = row.querySelector('.res-pwd');
    return {
        href: link ? (link.getAttribute('href') || '') : '',
        title: link ? (link.textContent || '').trim() : '',
        platform: link ? (link.getAttribute('data-pkres-platform') || '') : '',
        password: pwd ? (pwd.getAttribute('data-pwd') || '') : '',
    };
})
"""

_RESOURCE_STATE_JS = """
() => {
    const button = document.querySelector('#btnFetchRes');
    const hint = document.querySelector('#resHint');
    return {
        rows: document.querySelectorAll('#resContainer .res-row').length,
        busy: button ? (button.textContent || '').indexOf('获取中') >= 0 : false,
        hint: hint ? (hint.textContent || '').trim() : '',
    };
}
"""


class PiankuGate:
    """片库反盾协议：常驻会话内完成人机验证、列表推流等待与资源行提取。"""

    #: 过盾后等待资源列表推流完成的额外窗口（秒）。
    STREAM_TIMEOUT = 30.0
    #: 页面内 Turnstile 状态变量名。
    TURNSTILE_STATE = "piankuVerification"

    def __init__(
            self,
            base_url: str,
            proxy: Any = None,
            timeout: int = 30,
            gate_timeout: int = 45,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.proxy = proxy
        self.timeout = max(5, int(timeout or 30))
        self.gate_timeout = max(15, min(int(gate_timeout or 45), 120))
        self._session = BrowserPageSession(
            "Pianku", proxy=proxy, timeout=self.timeout
        )
        self._last_detail_url = ""
        self._material: Optional[Dict[str, Any]] = None

    def update_config(
            self,
            base_url: Optional[str] = None,
            proxy: Any = None,
            timeout: Optional[int] = None,
            gate_timeout: Optional[int] = None,
    ) -> None:
        if base_url is not None:
            self.base_url = str(base_url or "").rstrip("/")
        if proxy is not None:
            self.proxy = proxy
        if timeout is not None:
            self.timeout = max(5, int(timeout or 30))
        if gate_timeout is not None:
            self.gate_timeout = max(15, min(int(gate_timeout or 45), 120))

    def session_material(self, ttl: float = 600.0) -> Dict[str, Any]:
        """返回当前已过盾会话的 Cookie 与 UA，供并发请求复用（带 TTL 缓存）。"""
        cached = self._material
        if cached and time.time() - float(cached.get("obtained_at") or 0) < ttl:
            return cached

        def _grab(page) -> Dict[str, Any]:
            self.ensure_verified(page)
            cookies = {
                str(item.get("name")): str(item.get("value"))
                for item in (page.context.cookies() or [])
                if item.get("name")
            }
            try:
                user_agent = str(page.evaluate("navigator.userAgent") or "")
            except Exception:
                user_agent = ""
            return {"cookies": cookies, "user_agent": user_agent}

        material = self.run_with_page(_grab) or {}
        material["obtained_at"] = time.time()
        self._material = material
        return material

    def clear_material(self) -> None:
        """凭据失效（如 403）时丢弃缓存，下次重新取用。"""
        self._material = None

    def run_with_page(self, callback: Callable[[Any], Any], wait_timeout: float = 0) -> Any:
        """在常驻反盾会话的页面线程内执行 ``callback(page)``。"""
        return self._session.run(
            callback,
            wait_timeout=wait_timeout or (self.gate_timeout + self.STREAM_TIMEOUT + 30),
        )

    def fetch_resources(self, detail_url: str) -> List[Dict[str, Any]]:
        """打开详情页、完成人机验证，返回完整推流后的资源行。"""
        target = str(detail_url or "").strip()
        if not target:
            return []

        def _flow(page) -> List[Dict[str, Any]]:
            page.goto(target, wait_until="domcontentloaded")
            self._last_detail_url = target
            self.ensure_gate(page)
            return page.evaluate(_EXTRACT_ROWS_JS) or []

        try:
            rows = self.run_with_page(_flow)
        except PiankuGateError:
            raise
        except Exception as error:
            raise PiankuGateError(f"片库详情渲染失败：{error}") from error
        return self.normalize_rows(rows)

    def ensure_gate(self, page, detail_url: Optional[str] = None) -> None:
        """确保会话已过盾；给定详情页时再渲染并等待资源列表推流完成。"""
        self.ensure_verified(page)

        target = str(detail_url or self._last_detail_url or "").strip()
        current = str(getattr(page, "url", "") or "")
        if target and current != target and not current.startswith(target):
            try:
                page.goto(target, wait_until="domcontentloaded")
            except Exception as error:
                raise PiankuGateError(f"片库详情页打开失败：{error}") from error
            self._last_detail_url = target
        elif not target:
            return

        state = self._resource_state(page)
        if state["rows"] and not state["busy"]:
            return

        if not state["busy"]:
            button = page.query_selector("#btnFetchRes")
            if button is None:
                raise PiankuGateError("片库详情页缺少资源获取入口，站点结构可能已变更")
            try:
                button.click()
            except Exception:
                pass

        gate_deadline = time.monotonic() + self.gate_timeout
        stream_deadline = 0.0
        while True:
            state = self._resource_state(page)
            if self._resource_complete(state):
                return
            now = time.monotonic()
            if state["rows"] and not stream_deadline:
                # 过盾已通过、列表仍在推流：给推流预留独立窗口，避免被过盾预算截断。
                stream_deadline = now + self.STREAM_TIMEOUT
            if now >= max(gate_deadline, stream_deadline):
                break
            page.wait_for_timeout(500)
        logger.warning(
            "片库：资源列表未在 %ss 内推流完成，本次跳过该详情页资源",
            self.gate_timeout,
        )
        raise PiankuGateError("片库资源列表获取超时")

    def ensure_verified(self, page) -> None:
        """确保当前会话已通过人机验证；未通过时用官方接口铸造并提交 token。

        站点自身的校验流程就是「拿 sitekey 铸 token -> POST turnstile_gate.php」，
        因此不再依赖某个详情页，冷启动（测试搜索/资源列表直接解析）也能过盾。
        """
        if self._gate_status(page).get("verified"):
            return
        if not str(getattr(page, "url", "") or "").startswith(self.base_url):
            try:
                page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
            except Exception as error:
                raise PiankuGateError(f"片库站点访问失败：{error}") from error

        status = self._gate_status(page)
        if status.get("verified"):
            return
        site_key = str(status.get("sitekey") or "").strip()
        action = str(status.get("action") or "").strip()
        if not site_key:
            raise PiankuGateError("片库人机验证参数缺失，站点结构可能已变更")

        try:
            mount_turnstile_page(page, f"{self.base_url}/__gate")
            token = turnstile_page_token(
                page,
                site_key,
                action,
                state_name=self.TURNSTILE_STATE,
                label="Pianku",
                deadline=float(self.gate_timeout),
                click_challenge=self._session.click_challenge,
            )
        except Exception as error:
            raise PiankuGateError(f"片库人机验证失败：{error}") from error
        if not token:
            raise PiankuGateError("片库人机验证未返回 token")
        page.evaluate(_GATE_SUBMIT_JS, token)
        if not self._gate_status(page).get("verified"):
            raise PiankuGateError("片库人机验证未通过")

    def _gate_status(self, page) -> Dict[str, Any]:
        try:
            status = page.evaluate(_GATE_STATUS_JS) or {}
        except Exception:
            return {}
        return status if isinstance(status, dict) else {}

    def _resource_state(self, page) -> Dict[str, Any]:
        """读取资源面板状态：已渲染条数、是否仍在获取、提示文案。"""
        try:
            state = page.evaluate(_RESOURCE_STATE_JS) or {}
        except Exception:
            return {"rows": 0, "busy": True, "hint": ""}
        return {
            "rows": int(state.get("rows") or 0),
            "busy": bool(state.get("busy")),
            "hint": str(state.get("hint") or "").strip(),
        }

    @staticmethod
    def _resource_complete(state: Dict[str, Any]) -> bool:
        """站点在推流结束时会改写提示文案并复位按钮，据此判断列表已完整。"""
        if state["busy"]:
            return False
        if state["rows"]:
            return True
        hint = state["hint"]
        return hint.startswith("共 ") or "暂未找到" in hint

    def normalize_rows(self, rows: Any) -> List[Dict[str, Any]]:
        """把页面行规范化为绝对 ``href`` 与 ``title/platform/password``。"""
        items: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            href = str(row.get("href") or "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = "".join((self.base_url, href)) if href.startswith("/") else href
            items.append({
                "href": href,
                "title": str(row.get("title") or "").strip(),
                "platform": str(row.get("platform") or "").strip().lower(),
                "password": str(row.get("password") or "").strip(),
            })
        return items

    def close(self) -> None:
        self._last_detail_url = ""
        self._material = None
        self._session.close()
