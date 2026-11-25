"""TG 频道监控 API：状态、资源列表、扫描、登录、转存。"""

from typing import Any, Dict, List

from app.db.subscribe_oper import SubscribeOper
from app.log import logger

from ..delegation import OwnerDelegator


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
        """频道监控「转存」按钮：单条（id）或多条（ids）补转错过的资源。

        v1.28.0：不再直接调手动同步并立刻把条目标成「已转存」（那会把只是
        提交成功、实际 0 转存的条目也算成已转存，且不带 TG 来源与目标集）。
        改为把条目放回频道监控转存队列，由 worker 走与自动转存同一条路
        （_transfer_group → submit_candidate → _apply_group_outcome），
        结果与真实原因写回条目，前端轮询刷新。
        """
        payload = payload or {}
        item_ids = self._ids(payload)
        if not item_ids:
            return {"success": False, "message": "缺少资源 ID"}
        subscribe_id = self._subscribe_id_of(payload.get("subscribe_id"))
        try:
            outcome = self._monitor().manual_transfer_items(
                item_ids, subscribe_id=subscribe_id,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"TG 频道手动转存提交失败：{error}")
            return {"success": False, "message": f"提交转存失败：{error}"}
        results = list(outcome.get("results") or [])
        submitted = int(outcome.get("submitted") or 0)
        rejected = [row for row in results if not row.get("ok")]
        if not submitted:
            first = rejected[0] if rejected else {}
            if len(item_ids) == 1 and first.get("reason") == "no_subscribe":
                # 前端据此弹出订阅选择框。
                message = "请选择要转存到的订阅"
            else:
                message = str(first.get("message") or "没有可转存的条目")
                if len(rejected) > 1:
                    message = f"{len(rejected)} 条都未提交：{message}"
            return {"success": False, "message": message, "data": outcome}
        message = f"已加入转存队列 {submitted} 条，完成后状态会自动更新"
        if rejected:
            reasons: Dict[str, int] = {}
            for row in rejected:
                text = str(row.get("message") or "")
                reasons[text] = reasons.get(text, 0) + 1
            message += "；未提交 " + "、".join(
                f"{count} 条{text}" for text, count in reasons.items()
            )
        return {"success": True, "message": message, "data": outcome}

    def api_vue_tg_retry_failed(self, payload: Dict[str, Any] = None) -> dict:
        """1.7.114：把失败条目重新排队（优先沿用扫描匹配到的订阅）。"""
        payload = payload or {}
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        stats = self._monitor().retry_failed_items(limit=limit or 50)
        if not stats.get("submitted"):
            if stats.get("skipped_no_subscribe"):
                message = (
                    f"没有可重试的条目：{stats['skipped_no_subscribe']} 条失败条目"
                    "没有匹配到订阅"
                )
            elif stats.get("skipped_queue_full"):
                message = "转存队列已满，请稍后再试"
            else:
                message = "没有失败条目需要重试"
        else:
            message = f"已重新排队 {stats['submitted']} 条"
            if stats.get("skipped_no_subscribe"):
                message += f"；{stats['skipped_no_subscribe']} 条无匹配订阅已跳过"
            if stats.get("skipped_queue_full"):
                message += f"；{stats['skipped_queue_full']} 条因队列已满未入队"
        return {"success": True, "message": message, "data": stats}

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
