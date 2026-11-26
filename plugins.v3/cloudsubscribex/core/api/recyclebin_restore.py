"""115 回收站恢复 API：列出候选 / 执行还原（v1.31.6）。

端点都是普通实例方法（MP 按 query 参数以 kwargs 调用，不传实例；
staticmethod 会丢 self，见 MP 端点分发规则）。

还原端点只接受前端确认后发来的 rid 清单，不接受“全部还原”这种无清单
操作；还原后服务端逐项重查回收站核对，见 handlers/sync/recyclebin_restore。
"""

from typing import Any, Dict

from ..delegation import OwnerDelegator


class RecyclebinRestoreApi(OwnerDelegator):
    def _sync_handler_or_error(self, *method_names: str):
        return self._require_sync_handler(*method_names)

    def api_vue_recyclebin_list(self, payload: Dict[str, Any] = None) -> dict:
        """按删除时间窗口列出回收站里可还原的记录（只读，不还原）。"""
        handler, failure = self._sync_handler_or_error(
            "restore_list_candidates",
        )
        if handler is None:
            return failure
        payload = payload or {}
        try:
            return handler.restore_list_candidates(
                payload.get("window_start"), payload.get("window_end"),
            )
        except Exception as error:  # noqa: BLE001 - 列表异常返回失败
            return {"success": False, "message": f"列出回收站记录失败：{error}"}

    def api_vue_recyclebin_revert(self, payload: Dict[str, Any] = None) -> dict:
        """只还原前端确认过的 rid 清单，逐项核对后返回结果。"""
        handler, failure = self._sync_handler_or_error("restore_revert")
        if handler is None:
            return failure
        payload = payload or {}
        items = payload.get("items")
        if items is None:
            items = payload.get("rids")
        if not items:
            return {"success": False, "message": "未指定要还原的回收站记录"}
        try:
            return handler.restore_revert(
                items, payload.get("window_start"), payload.get("window_end"),
            )
        except Exception as error:  # noqa: BLE001 - 还原异常返回失败
            return {"success": False, "message": f"还原回收站失败：{error}"}
