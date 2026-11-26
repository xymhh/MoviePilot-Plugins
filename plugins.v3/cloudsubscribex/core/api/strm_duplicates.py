"""重复扫描 API：扫描（纯本地）/ 核对大小 / 删除 / 状态保存与读取（v1.31.2）。

端点都是普通实例方法（MP 按 query 参数以 kwargs 调用，不传实例；
staticmethod 会丢 self，见 MP 端点分发规则）。
"""

from typing import Any, Dict

from ..delegation import OwnerDelegator


class StrmDuplicatesApi(OwnerDelegator):
    def _sync_handler_or_error(self, *method_names: str):
        return self._require_sync_handler(*method_names)

    def api_vue_duplicates_scan(self) -> dict:
        """递归走 .strm 根目录，只读本地 .strm，零 115 调用。"""
        handler, failure = self._sync_handler_or_error("duplicates_scan")
        if handler is None:
            return failure
        try:
            return handler.duplicates_scan()
        except Exception as error:  # noqa: BLE001 - 扫描异常返回失败
            return {"success": False, "message": f"重复扫描失败：{error}"}

    def api_vue_duplicates_enrich(self, payload: Dict[str, Any] = None) -> dict:
        """按 115 列表核对各份视频实际大小，给出留大建议。"""
        handler, failure = self._sync_handler_or_error("duplicates_enrich")
        if handler is None:
            return failure
        try:
            return handler.duplicates_enrich((payload or {}).get("groups"))
        except Exception as error:  # noqa: BLE001 - 核对异常返回失败
            return {"success": False, "message": f"核对视频大小失败：{error}"}

    def api_vue_duplicates_delete(self, payload: Dict[str, Any] = None) -> dict:
        """按用户确认删一组重复（视频 + STRM + NFO，不可逆）。"""
        handler, failure = self._sync_handler_or_error("duplicates_delete")
        if handler is None:
            return failure
        payload = payload or {}
        try:
            return handler.duplicates_delete(
                payload.get("group"), payload.get("keep_index"),
            )
        except Exception as error:  # noqa: BLE001 - 删除异常返回失败
            return {"success": False, "message": f"删除重复失败：{error}"}

    def api_vue_duplicates_delete_batch(self, payload: Dict[str, Any] = None) -> dict:
        """批量删除重复组（v1.31.3）：逐组复用单组删除校验，单组失败不中断整批。"""
        handler, failure = self._sync_handler_or_error("duplicates_delete_batch")
        if handler is None:
            return failure
        try:
            return handler.duplicates_delete_batch((payload or {}).get("items"))
        except Exception as error:  # noqa: BLE001 - 批量删除异常返回失败
            return {"success": False, "message": f"批量删除重复失败：{error}"}

    def api_vue_duplicates_save_state(self, payload: Dict[str, Any] = None) -> dict:
        """保存重复扫描状态（扫描结果 + 核对进度 + 保留选择）。"""
        handler, failure = self._sync_handler_or_error("duplicates_save_state")
        if handler is None:
            return failure
        try:
            return handler.duplicates_save_state((payload or {}).get("state"))
        except Exception as error:  # noqa: BLE001 - 保存异常返回失败
            return {"success": False, "message": f"保存扫描状态失败：{error}"}

    def api_vue_duplicates_load_state(self) -> dict:
        """读取上次保存的重复扫描状态（无保存时返回空，不报错）。"""
        handler, failure = self._sync_handler_or_error("duplicates_load_state")
        if handler is None:
            return failure
        try:
            return handler.duplicates_load_state()
        except Exception as error:  # noqa: BLE001 - 读取异常返回失败
            return {"success": False, "message": f"读取扫描状态失败：{error}"}
