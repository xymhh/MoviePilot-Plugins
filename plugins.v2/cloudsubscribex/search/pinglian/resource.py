from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from app.log import logger

from .client import PinglianClient, PinglianError
from ..types import append_share_password, normalize_resource_type, resource_type_from_url
from ...utils.cache import create_platform_ttl_cache, normalize_platform_cache_key


class PinglianResourceService:
    """负责盘链资源详情接口、存活检测、按需解锁与配额状态管理。"""

    _DETAIL_CACHE_TTL = 10 * 60  # 详情缓存 10 分钟
    _DETAIL_CACHE_LIMIT = 512

    def __init__(self, client: PinglianClient):
        self._client = client
        self._detail_cache = create_platform_ttl_cache(
            "pinglian:video_details",
            str(getattr(client, "username", "") or "default").casefold(),
            maxsize=self._DETAIL_CACHE_LIMIT,
            ttl=self._DETAIL_CACHE_TTL,
        )
        self._unlocked_cache = create_platform_ttl_cache(
            "pinglian:unlocked_urls",
            str(getattr(client, "username", "") or "default").casefold(),
            maxsize=512,
            ttl=30 * 60,
        )
        self._quota_cache: Dict[str, Any] = {}
        self._quota_cache_time: float = 0.0
        self._lock = threading.RLock()

    def clear_cache(self) -> int:
        with self._lock:
            count = len(list(self._detail_cache.items()))
            self._detail_cache.clear()
            self._unlocked_cache.clear()
            return count

    def get_link_quota(self, force: bool = False) -> Dict[str, Any]:
        """读取盘链链接解锁配额，包含 60s 短时缓存。"""
        now = time.time()
        with self._lock:
            if not force and self._quota_cache and (now - self._quota_cache_time) < 60:
                return dict(self._quota_cache)

        try:
            quota_payload = self._client.request_json("/api/videos/link-quota")
            quota = (
                quota_payload.get("data")
                if isinstance(quota_payload, dict)
                else {}
            )
            if isinstance(quota, dict) and quota:
                with self._lock:
                    self._quota_cache = dict(quota)
                    self._quota_cache_time = now
                return quota
        except Exception as error:
            logger.debug(f"盘链读取配额信息失败：{error}")

        with self._lock:
            return dict(self._quota_cache or {})

    def has_quota(self) -> bool:
        """检查今日是否仍有可用链接解锁配额。"""
        quota = self.get_link_quota()
        if not quota:
            return True
        if quota.get("unlimited"):
            return True
        remaining = quota.get("remaining")
        if remaining is not None:
            return int(remaining) > 0
        limit = quota.get("limit")
        used = quota.get("used")
        if limit is not None and used is not None:
            return int(used) < int(limit)
        return True

    def format_quota_text(self) -> str:
        """格式化今日剩余配额显示文本（例如 '50 次'）。"""
        quota = self.get_link_quota()
        if not quota:
            return "—"
        if quota.get("unlimited"):
            return "不限次数"
        remaining = quota.get("remaining")
        if remaining is not None:
            return f"{int(remaining)} 次"
        limit = quota.get("limit")
        used = quota.get("used")
        if limit is not None:
            rem = max(0, int(limit) - int(used or 0))
            return f"{rem} 次"
        return "—"

    def format_used_quota_text(self) -> str:
        """格式化今日已用配额显示文本（例如 '0/50 次'）。"""
        quota = self.get_link_quota()
        if not quota:
            return "—"
        if quota.get("unlimited"):
            return "今日解锁查看不限次数"
        used = int(quota.get("used", 0) or 0)
        limit = quota.get("limit")
        if limit is not None:
            return f"{used}/{int(limit)} 次"
        return f"{used} 次"

    def sync_quota_from_data(self, quota_data: Dict[str, Any]) -> None:
        """从 link-ticket 等接口返回的实时数据同步配额缓存。"""
        if isinstance(quota_data, dict) and quota_data:
            with self._lock:
                self._quota_cache.update(quota_data)
                self._quota_cache_time = time.time()

    def record_unlocked(self, link_id: str, url: str) -> None:
        """请求 link-open 成功即计入解锁，更新本地配额并缓存。"""
        target_id = str(link_id or "").strip()
        with self._lock:
            if url and target_id:
                self._unlocked_cache.set(target_id, str(url).strip())
            if self._quota_cache and not self._quota_cache.get("unlimited"):
                used = int(self._quota_cache.get("used", 0) or 0) + 1
                limit = self._quota_cache.get("limit")
                remaining = self._quota_cache.get("remaining")
                if remaining is not None:
                    remaining = max(0, int(remaining) - 1)
                elif limit is not None:
                    remaining = max(0, int(limit) - used)
                self._quota_cache["used"] = used
                if remaining is not None:
                    self._quota_cache["remaining"] = remaining
                self._quota_cache_time = time.time()

    def mark_quota_exhausted(self) -> None:
        """标记今日配额已耗尽。"""
        with self._lock:
            if self._quota_cache:
                limit = self._quota_cache.get("limit")
                if limit is not None:
                    self._quota_cache["used"] = int(limit)
                self._quota_cache["remaining"] = 0
            else:
                self._quota_cache = {
                    "unlimited": False,
                    "remaining": 0,
                }
            self._quota_cache_time = time.time()

    def cached_url(self, link_id: str) -> str:
        """读取已解锁直链缓存。"""
        target_id = str(link_id or "").strip()
        if not target_id:
            return ""
        return str(self._unlocked_cache.get(target_id) or "").strip()

    def get_video_detail(
            self, vod_id: Any, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """获取影视详情及包含的全部网盘链接列表，带平台缓存。"""
        target_id = str(vod_id or "").strip()
        if not target_id:
            return {}

        cache_key = normalize_platform_cache_key(("pinglian:detail", target_id))
        if not force_refresh:
            cached = self._detail_cache.get(cache_key)
            if cached is not None:
                return dict(cached)

        try:
            payload = self._client.request_json(f"/api/videos/{target_id}")
            data = payload.get("data") if isinstance(payload, dict) else {}
            result = dict(data) if isinstance(data, dict) else {}
            if result:
                self._detail_cache.set(cache_key, result)
            return result
        except Exception as error:
            logger.warning(f"获取盘链影视详情失败（id={target_id}）：{error}")
            return {}

    def check_link_status(self, link_id: Any) -> Dict[str, Any]:
        """调用 POST /api/videos/link-check 探测链接真实存活状态。"""
        target_id = str(link_id or "").strip()
        if not target_id:
            return {"status": "invalid", "message": "无效链接ID"}

        try:
            payload = self._client.request_json(
                "/api/videos/link-check",
                method="POST",
                json={"link_id": int(target_id) if target_id.isdigit() else target_id},
            )
            data = payload.get("data") if isinstance(payload, dict) else {}
            return dict(data) if isinstance(data, dict) else {}
        except Exception as error:
            logger.debug(f"盘链链接状态检测失败（link_id={target_id}）：{error}")
            return {}

    def resolve_link(
            self,
            link_id: str | int,
            resource_type: str = "",
            password: str = "",
            **kwargs,
    ) -> Dict[str, str | bytes]:
        """按两步解锁流程换取盘链真实网盘直链"""
        target_id = str(link_id or "").strip()
        expected_type = normalize_resource_type(resource_type)
        if not target_id:
            raise PinglianError("盘链资源标识无效", "pinglian_invalid_token")

        if not self.has_quota():
            raise PinglianError(
                f"今日盘链链接解锁配额已用尽（{self.format_quota_text()}）",
                "pinglian_quota_exceeded",
            )

        # 第一步：获取解锁凭证 (link-ticket)，受每分钟解锁频次门控保护
        def _fetch_ticket():
            return self._client.request_json(
                "/api/videos/link-ticket",
                method="POST",
                json={"link_id": int(target_id) if target_id.isdigit() else target_id},
            )

        try:
            if hasattr(self._client, "_unlock_gate") and self._client._unlock_gate:
                ticket_payload = self._client._unlock_gate.run(_fetch_ticket)
            else:
                ticket_payload = _fetch_ticket()
        except Exception as error:
            if any(w in str(error).lower() for w in ("配额", "上限", "quota", "limit")):
                self.mark_quota_exhausted()
            raise PinglianError(
                f"获取盘链资源解锁凭证失败：{error}", "pinglian_ticket_failed"
            ) from error
        ticket_data = ticket_payload.get("data") if isinstance(ticket_payload, dict) else {}
        ticket = str((ticket_data or {}).get("ticket") or "").strip()
        ticket_code = str((ticket_data or {}).get("code") or "").strip()
        if not ticket:
            raise PinglianError("盘链未返回有效解锁凭证", "pinglian_ticket_empty")

        # 同步服务端下发的最新实时配额数据
        if isinstance(ticket_data.get("quota"), dict):
            self.sync_quota_from_data(ticket_data["quota"])

        # 第二步：使用凭证换取实际网盘链接 (link-open)
        try:
            open_payload = self._client.request_json(
                f"/api/videos/link-open/{target_id}",
                method="GET",
                params={"t": ticket},
            )
        except Exception as error:
            raise PinglianError(
                f"打开盘链真实链接失败：{error}", "pinglian_open_failed"
            ) from error

        open_data = open_payload.get("data") if isinstance(open_payload, dict) else {}
        target_url = str((open_data or {}).get("url") or "").strip()
        if not target_url:
            raise PinglianError("盘链未返回有效分享链接", "pinglian_empty_link")

        # 记录已解锁与扣减配额
        self.record_unlocked(target_id, target_url)

        actual_type = resource_type_from_url(target_url)
        final_type = actual_type or expected_type
        resolved_pwd = str(
            (open_data or {}).get("password")
            or (open_data or {}).get("code")
            or ticket_code
            or password
            or ""
        ).strip()

        return {
            "url": append_share_password(final_type, target_url, resolved_pwd),
            "resource_type": final_type,
        }

    def unlock_resource(
            self,
            link_id: Any,
            resource_type: str = "",
            password: str = "",
            budget: Optional[Any] = None,
            check_first: bool = True,
            search_label: str = "",
    ) -> Optional[str]:
        """按需单条解锁盘链链接。"""
        target_id = str(link_id or "").strip()
        if not target_id:
            return None

        prefix = f"[{search_label}][PINGLIAN]" if search_label else "[PINGLIAN]"

        # 1. 检查已解锁缓存 (0配额消耗)
        cached = self.cached_url(target_id) or (
            budget.cached_url(target_id) if budget and hasattr(budget, "cached_url") else "")
        if cached:
            logger.debug(f"{prefix} 命中已解锁盘链直链缓存：link_id={target_id}，无需重复解锁")
            return cached

        # 2. 链接存活预检（防止对已取消分享的死链浪费配额）
        if check_first:
            check_info = self.check_link_status(target_id)
            status = str(check_info.get("status") or "").lower()
            if status in {"gone", "deleted", "invalid", "expired"}:
                msg = check_info.get("message") or "分享链接已失效或已取消"
                logger.warning(f"{prefix} 盘链链接探测已失效（link_id={target_id}，状态={status}）：{msg}")
                return None

        # 3. 检查当日配额
        if not self.has_quota():
            logger.warning(
                f"{prefix} 解锁被拦截：今日链接解锁配额已用尽（{self.format_quota_text()}）"
            )
            return None
        # 4. 执行两步解锁 (link-ticket + link-open)
        try:
            resolved = self.resolve_link(
                link_id=target_id,
                resource_type=resource_type,
                password=password,
            )
            target_url = str(resolved.get("url") or "").strip()
            if not target_url:
                logger.error(f"{prefix} 盘链未返回有效分享链接：link_id={target_id}")
                return None

            if budget and hasattr(budget, "record_result"):
                budget.record_result(target_id, target_url, 1)

            logger.info(
                f"{prefix} 成功按需解锁盘链链接：link_id={target_id}，"
                f"今日解锁配额已更新为: {self.format_quota_text()}"
            )
            return target_url
        except PinglianError as error:
            logger.error(f"{prefix} 解锁盘链资源失败（link_id={target_id}）：{error}")
            return None
