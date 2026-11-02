"""媒体库刷新与消息通知（旁路能力）。

从 handlers/sync/service.py 拆出，纯搬家：方法体逐字未改。
校验：scripts/verify_split_equivalence.py --before <拆分前> --after HEAD
"""

from typing import Any, Dict, List

from app.log import logger
from app.schemas.types import NotificationType

from ..notification import MediaServerNotifier, MediaServerResolver
from ...core import OwnerDelegator
from ...core.bypass_guard import bypass_guard


class MediaRefreshMixin(OwnerDelegator):
    """媒体服务器刷新 + 通知聚合回调。"""

    def update_notification_config(
            self,
            notify: bool,
            notification_type: NotificationType,
            media_server_refresh_enabled: bool,
            media_servers: List[str],
            media_server_path_mappings: str,
            media_server_refresh_delay: int,
            emby_mediainfo_enabled: bool,
    ) -> None:
        self._notify = bool(notify)
        self._notification_type = notification_type
        self._notification_delay_seconds = max(
            0, int(media_server_refresh_delay or 0)
        )
        old_notifier = self._media_server_notifier
        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        old_notifier.close(flush=True)

    def begin_notification_batch(self) -> bool:
        """开始一次同步任务的媒体目录通知聚合。"""
        return self._media_server_notifier.begin_task_batch()

    def finish_notification_batch(self) -> bool:
        """同步任务收尾后统一提交媒体目录通知（旁路：失败不影响主链路）。"""
        return bool(bypass_guard.call(
            "notify",
            self._media_server_notifier.finish_task_batch,
            fallback=False,
        ))

    def refresh_media_servers(self) -> Dict[str, Any]:
        """让媒体服务器重新扫描库（旁路：失败只丢掉刷新，不影响转存结果）。"""
        return bypass_guard.call(
            "media_server",
            self._refresh_media_servers_impl,
            fallback={},
        )

    def _refresh_media_servers_impl(self) -> Dict[str, Any]:
        """媒体服务器刷新实现：逐台独立 try/except，全挂也不会外抛。"""
        results: Dict[str, Any] = {}
        try:
            services = MediaServerResolver._services() or {}
        except Exception as error:  # noqa: BLE001 - 媒体服务器不可用不该阻断主流程
            logger.warning(f"媒体服务器列表获取失败：{error}")
            services = {}
        for name, service in services.items():
            try:
                instance = getattr(service, "instance", None)
                if service.type == "emby":
                    if not self._refresh_emby(instance):
                        results[name] = "unsupported"
                        continue
                elif hasattr(instance, "refresh_library_by_items"):
                    instance.refresh_library_by_items([])
                else:
                    results[name] = "unsupported"
                    continue
                results[name] = "ok"
                logger.info(f"媒体库刷新已提交：{name}")
            except Exception as error:  # noqa: BLE001
                results[name] = str(error)
                logger.warning(f"媒体库刷新失败：{name}（{error}）")
        if not results:
            results.update(self._refresh_emby_from_settings())
        return results

    @staticmethod
    def _refresh_emby(instance: Any) -> bool:
        """对 Emby 触发全库刷新（MP 的 Emby 客户端无 post，直接走 HTTP）。"""
        host = str(getattr(instance, "host", "") or getattr(instance, "_host", "") or "")
        apikey = str(
            getattr(instance, "apikey", "") or getattr(instance, "_apikey", "") or ""
        )
        return MediaRefreshMixin._emby_library_refresh(host, apikey)

    @staticmethod
    def _emby_library_refresh(host: str, apikey: str) -> bool:
        host = str(host or "").rstrip("/")
        apikey = str(apikey or "")
        if not host or not apikey:
            return False
        import httpx

        response = httpx.post(
            f"{host}/Library/Refresh", params={"api_key": apikey}, timeout=30,
        )
        return response.status_code in (200, 204)

    def _refresh_emby_from_settings(self) -> Dict[str, Any]:
        """兜底：从 MP 系统设置里读 Emby 连接信息直接请求。"""
        results: Dict[str, Any] = {}
        try:
            from app.core.config import settings

            servers = (
                getattr(settings, "MEDIASSERVERS", None)
                or getattr(settings, "MEDIASSERVER", None)
                or []
            )
        except Exception:  # noqa: BLE001
            return results
        for server in servers or []:
            if str(server.get("type") or "") != "emby" or not server.get("enabled"):
                continue
            config = server.get("config") or {}
            name = str(server.get("name") or "emby")
            try:
                ok = self._emby_library_refresh(
                    config.get("host", ""), config.get("apikey", ""),
                )
                results[name] = "ok" if ok else "failed"
            except Exception as error:  # noqa: BLE001
                results[name] = str(error)
        return results
