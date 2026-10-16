"""盘链网页登录、资源查询与分享链接解析。"""

import base64
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from app.log import logger

from .captcha import PinglianCaptchaRecognizer
from ..http_client import (
    AccountActionGate,
    RequestGate,
    gated_idempotent_request,
    gated_request,
    normalize_proxies,
    request_error_summary,
    requests,
)


def _format_datetime(value: Any) -> str:
    """将时间字符串格式化为可读时间（YYYY-MM-DD HH:MM:SS），不做二次时区偏移。"""
    if not value:
        return ""
    val_str = str(value).strip()
    if not val_str:
        return ""
    if "T" in val_str:
        val_str = val_str.replace("T", " ")
    if val_str.endswith("Z"):
        val_str = val_str[:-1].strip()
    if "." in val_str:
        val_str = val_str.split(".")[0].strip()
    return val_str


class PinglianError(RuntimeError):
    """盘链登录、查询或链接解析失败。"""

    def __init__(self, message: str, code: str = "pinglian_error"):
        super().__init__(message)
        self.code = code


class PinglianClient:
    BASE_URL = "https://pinglian.lol"
    _SESSION_DATA_KEY = "pinglian_auth_session"
    _LOGIN_LOCK = threading.RLock()
    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    def __init__(
            self,
            username: str,
            password: str,
            base_url: str = BASE_URL,
            proxy: Any = None,
            request_timeout: int = 30,
            request_interval: float = 1.0,
            unlocks_per_minute: int = 5,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 30), 120))
        self._session = self._create_session()
        self._request_gate = RequestGate.shared(
            "盘链",
            f"{self.base_url}|{self.username.casefold()}|{self._proxies}",
            request_interval=request_interval, minimum_interval=0.5
        )
        self._unlocks_per_minute = max(1, min(int(unlocks_per_minute or 5), 20))
        self._unlock_gate = AccountActionGate.shared(
            "盘链 解锁接口",
            f"pinglian:{self.username.casefold()}",
            max_actions=self._unlocks_per_minute,
            maximum_actions=20,
        )
        self._account_disabled = False
        self._disabled_reason = ""
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._authenticated = False
        self._captcha = PinglianCaptchaRecognizer()
        self._restore_session()
    @property
    def _timeout(self) -> tuple[int, int]:
        return min(15, self._request_timeout), self._request_timeout

    @classmethod
    def _create_session(cls):
        session = requests.Session(impersonate="chrome")
        session.headers.update(cls._HEADERS)
        return session

    def _session_request(self, *args, **kwargs):
        return self._session.request(*args, **kwargs)

    def _reset_transport(self, error: BaseException, attempt: int) -> None:
        cookies = self._session.cookies.get_dict()
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._create_session()
        for name, value in cookies.items():
            self._session.cookies.set(name, value)
        logger.debug(
            f"盘链连接异常后重建 HTTP 会话："
            f"{type(error).__name__}，重试={attempt}"
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    @staticmethod
    def _is_json(response) -> bool:
        return "application/json" in str(
            response.headers.get("content-type") or ""
        ).casefold()

    def _restore_session(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
            ):
                return
            cookies = data.get("cookies") or {}
            if isinstance(cookies, dict):
                for name, value in cookies.items():
                    if str(name or "").strip() and str(value or ""):
                        self._session.cookies.set(str(name), str(value))
                self._authenticated = bool(cookies)
                if cookies:
                    logger.debug("盘链已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"盘链恢复持久化登录状态失败：{error}")

    def _save_session(self) -> None:
        if not self._save_data_func:
            return
        try:
            cookies = self._session.cookies.get_dict()
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "cookies": cookies,
                    "updated_at": int(time.time()),
                } if cookies else {},
            )
        except Exception as error:
            logger.debug(f"盘链持久化登录状态失败：{error}")

    def _clear_session(self) -> None:
        self._authenticated = False
        self._session.cookies.clear()
        self._save_session()

    def _check_auth_status(self) -> bool:
        """检查当前已保存的会话状态是否仍然有效。"""
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session_request,
                "GET",
                f"{self.base_url}/api/auth/status",
                headers={
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/",
                },
                proxies=self._proxies,
                timeout=self._timeout,
            )
            if response.status_code == 200 and self._is_json(response):
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else {}
                if isinstance(data, dict):
                    if data.get("is_admin") or (
                        data.get("role") == "user"
                        and int(data.get("user_id") or 0) > 0
                    ):
                        return True
        except Exception as error:
            logger.debug(f"盘链鉴权状态检查异常：{error}")
        return False

    def _login(self, force: bool = False) -> None:
        if self._account_disabled:
            raise PinglianError(self._disabled_reason or "盘链账号已被禁用，请联系管理员", "ACCOUNT_DISABLED")
        if not self.is_configured:
            raise PinglianError("盘链账号或密码未配置", "pinglian_not_configured")
        if self._authenticated and not force:
            return
        with self._LOGIN_LOCK:
            if self._account_disabled:
                raise PinglianError(self._disabled_reason or "盘链账号已被禁用，请联系管理员", "ACCOUNT_DISABLED")
            if self._authenticated and not force:
                return
            if force:
                self._clear_session()
            elif self._authenticated and self._check_auth_status():
                return

            max_captcha_attempts = 3
            for attempt in range(1, max_captcha_attempts + 1):
                cid, code = self._fetch_captcha()
                try:
                    response = gated_request(
                        self._request_gate,
                        self._session_request,
                        "POST",
                        f"{self.base_url}/api/auth/login",
                        data={
                            "username": self.username,
                            "password": self.password,
                            "remember": "1",
                            "captcha_id": cid,
                            "captcha_code": code,
                        },
                        headers={
                            "Origin": self.base_url,
                            "Referer": f"{self.base_url}/login",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        proxies=self._proxies,
                        timeout=self._timeout,
                    )
                except requests.exceptions.RequestException as error:
                    raise PinglianError(
                        f"盘链登录失败：{request_error_summary(error)}",
                        "pinglian_login_failed",
                    ) from error

                payload = {}
                if self._is_json(response):
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {}
                elif response.status_code != 200:
                    raise PinglianError(
                        f"盘链登录失败（HTTP {response.status_code}）",
                        "pinglian_login_failed",
                    )

                if response.status_code != 200 or not isinstance(payload, dict) or not payload.get("success"):
                    details = (payload or {}).get("details") or {}
                    if isinstance(details, dict) and details.get("captcha_error"):
                        logger.warning(
                            f"盘链验证码识别错误，重试第 {attempt}/{max_captcha_attempts} 次..."
                        )
                        time.sleep(0.3)
                        continue
                    error_msg = str(
                        (payload or {}).get("message")
                        or f"盘链登录失败（HTTP {response.status_code}）"
                    )
                    if "已被禁用" in error_msg or "禁用" in error_msg:
                        self._account_disabled = True
                        self._disabled_reason = error_msg
                        raise PinglianError(error_msg, "ACCOUNT_DISABLED")
                    raise PinglianError(error_msg, "pinglian_login_failed")
                self._authenticated = True
                self._save_session()
                logger.info("盘链登录成功并已更新会话")
                return

            raise PinglianError("盘链验证码连续识别失败，请检查网络或重试", "pinglian_captcha_failed")

    def _fetch_captcha(self) -> tuple[str, str]:
        """获取并自动识别盘链图形验证码，返回 (captcha_id, captcha_code)。"""
        try:
            response = gated_request(
                self._request_gate,
                self._session_request,
                "GET",
                f"{self.base_url}/api/auth/captcha",
                headers={
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/login",
                },
                proxies=self._proxies,
                timeout=self._timeout,
            )
            if response.status_code != 200 or not self._is_json(response):
                raise PinglianError(
                    f"获取盘链验证码失败（HTTP {response.status_code}）",
                    "pinglian_captcha_failed",
                )
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else {}
            cid = str(data.get("id") or "").strip()
            raw_b64 = str(data.get("image") or "").split(",", 1)[-1]
            if not cid or not raw_b64:
                raise PinglianError("盘链验证码数据缺失", "pinglian_captcha_failed")
            img_bytes = base64.b64decode(raw_b64)
            code, confidence = self._captcha.recognize(img_bytes)
            logger.debug(f"盘链验证码自动识别：[{code}]（置信度 {confidence:.2f}）")
            return cid, code
        except Exception as error:
            if isinstance(error, PinglianError):
                raise
            raise PinglianError(
                f"获取并识别盘链验证码异常：{error}", "pinglian_captcha_failed"
            ) from error


    def _request(
            self,
            method: str,
            path: str,
            retry_auth: bool = True,
            **kwargs,
    ):
        if self._account_disabled:
            raise PinglianError(self._disabled_reason or "盘链账号已被禁用，请联系管理员", "ACCOUNT_DISABLED")
        if not path.startswith("/api/auth/login"):
            self._login()
        req_headers = dict(kwargs.pop("headers", None) or {})
        req_headers.setdefault("Origin", self.base_url)
        req_headers.setdefault("Referer", f"{self.base_url}/")
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session_request,
                method,
                f"{self.base_url}{path}",
                on_retry=self._reset_transport,
                headers=req_headers,
                proxies=self._proxies,
                timeout=self._timeout,
                **kwargs,
            )
        except requests.exceptions.RequestException as error:
            raise PinglianError(
                f"盘链请求失败：{request_error_summary(error)}",
                "pinglian_request_failed",
            ) from error

        auth_failed = response.status_code in (401, 403)
        payload = None
        if self._is_json(response):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                error_type = str(payload.get("error_type") or "").strip()
                message = str(payload.get("message") or "").strip()
                if (
                    error_type in ("ADMIN_AUTH_REQUIRED", "AUTH_REQUIRED")
                    or "请先登录" in message
                    or str(payload.get("code") or "") == "-1"
                ):
                    auth_failed = True
        else:
            response_path = str(urlparse(str(response.url or "")).path or "")
            auth_failed = auth_failed or "/login" in response_path

        if auth_failed and retry_auth:
            self._clear_session()
            self._login(force=True)
            return self._request(method, path, retry_auth=False, **kwargs)

        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            try:
                cooldown = max(30, min(120, int(float(retry_after))))
            except (TypeError, ValueError):
                cooldown = 30
            self._request_gate.activate_cooldown(
                cooldown, status=429, reason="盘链 HTTP 429"
            )
            raise PinglianError("盘链请求过于频繁，请稍后重试", "pinglian_rate_limited")

        if response.status_code >= 400:
            raise PinglianError(
                f"盘链请求失败（HTTP {response.status_code}）",
                "pinglian_request_failed",
            )
        return response, payload

    def request_json(
            self,
            path: str,
            method: str = "GET",
            params: Optional[Dict[str, Any]] = None,
            **kwargs,
    ) -> Dict[str, Any]:
        response, payload = self._request(
            method, path, params=params, **kwargs
        )
        if not self._is_json(response) or not isinstance(payload, dict):
            raise PinglianError(
                "盘链返回了非 JSON 页面，接口可能已改版", "pinglian_schema_changed"
            )
        if payload.get("success") is False:
            message = str(payload.get("message") or "盘链接口调用失败")
            error_type = str(payload.get("error_type") or "pinglian_api_error")
            if "已被禁用" in message or "禁用" in message:
                self._account_disabled = True
                self._disabled_reason = message
                raise PinglianError(message, "ACCOUNT_DISABLED")
            raise PinglianError(message, error_type)
        return payload


    def get_account_info(self) -> Dict[str, Any]:
        """从新版个人中心及配额接口读取账户、会员与配额信息。"""
        profile_payload = self.request_json("/api/me/profile")
        profile = profile_payload.get("data") if isinstance(profile_payload, dict) else {}
        if not isinstance(profile, dict):
            raise PinglianError("盘链个人中心数据格式异常", "pinglian_schema_changed")

        name = str(profile.get("username") or self.username).strip()
        vip_level = profile.get("vip_level")
        level_str = f"VIP{vip_level}" if vip_level else "普通用户"

        quota = {}
        try:
            quota_payload = self.request_json("/api/videos/link-quota")
            quota = quota_payload.get("data") if isinstance(quota_payload, dict) else {}
        except Exception as error:
            logger.debug(f"盘链读取配额信息失败：{error}")

        if isinstance(quota, dict) and quota.get("unlimited"):
            remaining_text = "不限次数"
            quota_text = "今日解锁查看不限次数"
        elif isinstance(quota, dict) and quota.get("remaining") is not None:
            rem = int(quota.get("remaining", 0) or 0)
            remaining_text = f"{rem} 次"
            if quota.get("limit") is not None:
                limit = int(quota.get("limit") or 0)
                used = int(quota.get("used", 0) or 0)
                quota_text = f"{used}/{limit} 次"
            else:
                quota_text = f"{rem} 次"
        elif isinstance(quota, dict) and quota.get("limit") is not None:
            limit = int(quota.get("limit") or 0)
            used = int(quota.get("used", 0) or 0)
            rem = max(0, limit - used)
            remaining_text = f"{rem} 次"
            quota_text = f"{used}/{limit} 次"
        else:
            remaining_text = "—"
            quota_text = "—"
        task_data = self.get_task_center()
        checkin_info = (task_data.get("checkin") or {}) if isinstance(task_data, dict) else {}
        total_days = checkin_info.get("total_days")
        try:
            signin_days = int(total_days) if total_days is not None else None
        except (TypeError, ValueError):
            signin_days = None

        details: Dict[str, str] = {}
        details["quota"] = quota_text
        details["remaining_quota"] = remaining_text
        if signin_days is not None:
            details["signin_days"] = f"{signin_days}"
        if profile.get("created_at"):
            details["created_at"] = _format_datetime(profile.get("created_at"))
        if profile.get("vip_expires_at"):
            details["vip_expires_at"] = _format_datetime(profile.get("vip_expires_at"))
        if profile.get("account_count") is not None:
            details["account_count"] = f"{int(profile.get('account_count') or 0)}"
        return {
            "name": name,
            "email": str(profile.get("email") or ""),
            "level": level_str,
            "points": remaining_text,
            "quota_text": remaining_text,
            "quota_usage": quota_text,
            "signin_days": signin_days,
            "expires_at": _format_datetime(profile.get("vip_expires_at")),
            "registered_at": _format_datetime(profile.get("created_at")),
            "invite_count": "",
            "details": details,
        }

    def get_task_center(self) -> Dict[str, Any]:
        """读取盘链任务中心状态（包含今日签到状态与配额）。"""
        try:
            payload = self.request_json("/api/tasks")
            data = payload.get("data") if isinstance(payload, dict) else {}
            return data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})
        except Exception as error:
            logger.debug(f"读取盘链任务中心状态失败：{error}")
            return {}

    def checkin(self, mode: str = "normal") -> Dict[str, Any]:
        before_profile = self.get_account_info()
        task_data = self.get_task_center()
        checkin_info = (task_data.get("checkin") or {}) if isinstance(task_data, dict) else {}
        already_checked_in = bool(checkin_info.get("done"))

        payload: Dict[str, Any] = {}
        if not already_checked_in:
            try:
                payload = self.request_json("/api/tasks/checkin", method="POST", json={})
            except Exception as error:
                refreshed_tasks = self.get_task_center()
                refreshed_checkin = (refreshed_tasks.get("checkin") or {}) if isinstance(refreshed_tasks, dict) else {}
                if not refreshed_checkin.get("done"):
                    raise PinglianError(f"盘链签到失败：{error}", "pinglian_checkin_failed") from error
                already_checked_in = True

        after_profile = self.get_account_info()
        after_tasks = self.get_task_center()
        checkin_info = (after_tasks.get("checkin") or {}) if isinstance(after_tasks, dict) else {}
        total_days = checkin_info.get("total_days")
        try:
            signin_days = int(total_days) if total_days is not None else None
        except (TypeError, ValueError):
            signin_days = None

        success = bool(already_checked_in or payload.get("success", True))
        status_text = "今日已签到" if already_checked_in else ("签到成功" if success else "签到失败")

        quota_msg = after_profile.get("quota_text") or ""
        msg_parts = [status_text]
        if payload.get("message") and str(payload.get("message")) not in (status_text, "success"):
            msg_parts.append(str(payload.get("message")))
        if quota_msg:
            msg_parts.append(f"今日剩余配额: {quota_msg}")

        message = "，".join(msg_parts)
        return {
            "success": success,
            "already_checked_in": already_checked_in,
            "status": status_text,
            "message": message,
            "mode": "normal",
            "signin_days": signin_days,
            "points_before": before_profile.get("points") or 0,
            "points_after": after_profile.get("points") or 0,
            "points_change": 0,
            "details": {
                "status": status_text,
                "today_quota": quota_msg,
                "remaining_quota": after_profile.get("points") or "",
                "quota_usage": after_profile.get("quota_usage") or "",
                "signin_days": signin_days,
                "vip_level": after_profile.get("level", ""),
            },
        }
    def clear_cache(self) -> Dict[str, int]:
        return {"session": int(self._authenticated)}

    def close(self) -> None:
        with self._lock:
            self._session.close()
