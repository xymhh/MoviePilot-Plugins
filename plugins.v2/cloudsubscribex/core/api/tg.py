"""TG 频道监控 API：状态、资源列表、扫描、登录、转存。"""

from typing import Any, Dict, List

from app.db.subscribe_oper import SubscribeOper
from app.log import logger

from .. import OwnerDelegator
from .sync import SyncApi


class TgMonitorApi(OwnerDelegator):
    def _monitor(self):
        from ..tg_monitor import get_channel_monitor
        return get_channel_monitor(self._owner)

    @staticmethod
    def _ids(payload: Dict[str, Any]) -> List[str]:
        raw = (payload or {}).get("ids")
        if raw is None:
            single = (payload or {}).get("id")
            raw = [single] if single else []
        if isinstance(raw, str):
            raw = [raw]
        return [str(item) for item in (raw or []) if str(item).strip()]

    def api_vue_tg_status(self) -> dict:
        return {"success": True, "data": self._monitor().status()}

    def api_vue_tg_items(self, status: str = "") -> dict:
        monitor = self._monitor()
        return {
            "success": True,
            "data": {
                "items": monitor.list_items(status=str(status or "")),
                "status": monitor.status(),
            },
        }

    def api_vue_tg_scan(self, payload: Dict[str, Any] = None) -> dict:
        payload = payload or {}
        try:
            result = self._monitor().scan(
                trigger="手动", full=bool(payload.get("full")),
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"TG 频道手动扫描失败：{error}")
            return {"success": False, "message": f"扫描失败：{error}"}
        if not result.get("ok"):
            return {"success": False, "message": str(result.get("message") or "扫描失败")}
        message = f"扫描完成：新增 {result.get('added', 0)} 条"
        if result.get("errors"):
            message += f"；{len(result['errors'])} 个频道异常"
        return {"success": True, "message": message, "data": result}

    def api_vue_tg_ignore(self, payload: Dict[str, Any]) -> dict:
        touched = self._monitor().update_items(self._ids(payload), status="ignored")
        return {"success": True, "message": f"已忽略 {touched} 条", "data": {"touched": touched}}

    def api_vue_tg_delete(self, payload: Dict[str, Any]) -> dict:
        removed = self._monitor().delete_items(self._ids(payload))
        return {"success": True, "message": f"已删除 {removed} 条", "data": {"removed": removed}}

    def api_vue_tg_subscribes(self) -> dict:
        rows: List[dict] = []
        try:
            for sub in SubscribeOper().list():
                rows.append({
                    "id": int(getattr(sub, "id", 0) or 0),
                    "name": str(getattr(sub, "name", "") or ""),
                    "season": int(getattr(sub, "season", 0) or 0),
                    "tmdbid": int(getattr(sub, "tmdbid", 0) or 0),
                    "type": str(getattr(sub, "type", "") or ""),
                    "state": str(getattr(sub, "state", "") or ""),
                })
        except Exception as error:  # noqa: BLE001
            logger.warning(f"读取订阅列表失败：{error}")
        rows.sort(key=lambda row: row["id"], reverse=True)
        return {"success": True, "data": {"subscribes": rows}}

    @staticmethod
    def _subscribe_id_of(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def api_vue_tg_transfer(self, payload: Dict[str, Any]) -> dict:
        payload = payload or {}
        item_id = str(payload.get("id") or "")
        subscribe_id = self._subscribe_id_of(payload.get("subscribe_id"))
        if not item_id:
            return {"success": False, "message": "缺少资源 ID"}
        monitor = self._monitor()
        item = next(
            (row for row in monitor.list_items() if str(row.get("id")) == item_id),
            None,
        )
        if not item:
            return {"success": False, "message": "资源不存在，可能已被清理"}
        # 1.7.113：扫描阶段已经把匹配结果写在条目上（matched_subscribe_id），
        # 手动转存没传订阅时直接沿用，不再让用户重选一遍。
        if subscribe_id <= 0:
            subscribe_id = self._subscribe_id_of(item.get("matched_subscribe_id"))
        if subscribe_id <= 0:
            return {"success": False, "message": "请选择要转存到的订阅"}
        links = list(item.get("links") or [])
        if not links:
            return {"success": False, "message": "该消息没有可转存的网盘链接"}
        payload = {
            "subscribe_id": subscribe_id,
            "resource_links": links,
        }
        title = str(item.get("title") or "").strip()
        if title:
            payload["resource_titles"] = {link: title for link in links}
        result = SyncApi(self._owner).api_vue_start_manual_sync(payload)
        if result.get("success"):
            monitor.update_items([item_id], status="transferred", subscribe_id=subscribe_id)
        return result

    # -------------------------------------------------------------- 登录
    def api_vue_tg_login_start(self, payload: Dict[str, Any]) -> dict:
        try:
            return {"success": True, "data": self._monitor().login_send_code(
                str((payload or {}).get("phone") or "")
            )}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "message": f"发送验证码失败：{error}"}

    def api_vue_tg_login_code(self, payload: Dict[str, Any]) -> dict:
        payload = payload or {}
        try:
            password = str(payload.get("password") or "")
            if password:
                return {"success": True, "data": self._monitor().login_submit_password(password)}
            return {"success": True, "data": self._monitor().login_submit_code(
                str(payload.get("code") or "")
            )}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "message": f"登录失败：{error}"}

    def api_vue_tg_logout(self) -> dict:
        try:
            return {"success": True, "data": self._monitor().logout()}
        except Exception as error:  # noqa: BLE001
            return {"success": False, "message": f"退出登录失败：{error}"}
