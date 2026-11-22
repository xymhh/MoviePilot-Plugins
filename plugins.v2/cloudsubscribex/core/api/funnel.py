"""整理队列 API：队列列表、状态、手动触发、停止、放回、删除（v1.29.7 阶段 1）。

端点都是普通实例方法（MP 按 query 参数以 kwargs 调用，不传实例；
staticmethod 会丢 self，见 MP 端点分发规则）。
"""

from typing import Any, Dict, List

from app.log import logger

from ..delegation import OwnerDelegator


class FunnelApi(OwnerDelegator):
    @staticmethod
    def _row_ids(payload: Dict[str, Any]) -> List[int]:
        raw = (payload or {}).get("ids")
        if raw is None:
            single = (payload or {}).get("id")
            raw = [single] if single is not None else []
        if isinstance(raw, (int, str)):
            raw = [raw]
        ids = []
        for value in (raw or []):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                ids.append(number)
        return ids

    @staticmethod
    def _row_view(row: Dict[str, Any]) -> Dict[str, Any]:
        media_type = str(row.get("media_type") or "").strip().lower()
        season = int(row.get("season") or 0)
        episode = int(row.get("episode") or 0)
        if media_type == "tv" and episode > 0:
            season_episode = f"S{season:02d}E{episode:02d}"
        elif media_type == "tv":
            season_episode = f"S{season:02d}"
        else:
            season_episode = ""
        reason = str(row.get("skip_reason") or row.get("last_error") or "").strip()
        return {
            "id": int(row.get("id") or 0),
            "title": str(row.get("title") or row.get("file_name") or ""),
            "media_type": media_type,
            "tmdb_id": row.get("tmdb_id"),
            "season": season,
            "episode": episode,
            "season_episode": season_episode,
            "file_name": str(row.get("file_name") or ""),
            "file_size": int(row.get("file_size") or 0),
            "source": str(row.get("source") or ""),
            "status": str(row.get("status") or ""),
            "attempts": int(row.get("attempts") or 0),
            "next_retry_at": int(row.get("next_retry_at") or 0),
            "reason": reason,
            "is_upgrade": bool(row.get("is_upgrade")),
            "created_at": int(row.get("created_at") or 0),
            "updated_at": int(row.get("updated_at") or 0),
        }

    def _sync_handler_or_error(self, *method_names: str):
        return self._require_sync_handler(*method_names)

    def api_vue_funnel_rows(self) -> dict:
        handler, failure = self._sync_handler_or_error(
            "funnel_list_rows", "funnel_queue_counts"
        )
        if handler is None:
            return failure
        try:
            counts = handler.funnel_queue_counts()
            groups = {}
            for key, statuses in (
                ("queued", ("queued", "failed")),
                ("processing", ("processing",)),
                ("held", ("held",)),
            ):
                groups[key] = [
                    self._row_view(row)
                    for row in handler.funnel_list_rows(list(statuses), limit=200)
                ]
            return {
                "success": True,
                "data": {
                    "counts": counts,
                    "groups": groups,
                    "phase1_enabled": bool(
                        getattr(self, "_funnel_phase1_enabled", False)
                    ),
                    "phase2_enabled": bool(
                        getattr(self, "_funnel_phase2_enabled", False)
                    ),
                    "target_dir": str(
                        getattr(self, "_funnel_target_dir", "") or ""
                    ),
                },
            }
        except Exception as error:  # noqa: BLE001 - 列表异常返回失败
            logger.warning(f"读取整理队列失败：{error}")
            return {"success": False, "message": f"读取整理队列失败：{error}"}

    def api_vue_funnel_status(self) -> dict:
        handler, failure = self._sync_handler_or_error("funnel_queue_counts")
        if handler is None:
            return failure
        worker_running = False
        try:
            registry = self._scheduler_registry()
            worker_running = bool(
                registry is not None
                and registry.get_job("CloudSubscribe_FunnelWorker") is not None
            )
        except Exception as error:  # noqa: BLE001 - 查不到按未运行处理
            logger.debug(f"查询漏斗 worker 状态失败：{error}", exc_info=True)
        return {
            "success": True,
            "data": {
                "counts": handler.funnel_queue_counts(),
                "worker_running": worker_running,
                "phase1_enabled": bool(
                    getattr(self, "_funnel_phase1_enabled", False)
                ),
                "phase2_enabled": bool(
                    getattr(self, "_funnel_phase2_enabled", False)
                ),
                "target_dir": str(getattr(self, "_funnel_target_dir", "") or ""),
            },
        }

    def api_vue_funnel_run(self, payload: Dict[str, Any] = None) -> dict:
        handler, failure = self._sync_handler_or_error("funnel_run_now")
        if handler is None:
            return failure
        try:
            result = handler.funnel_run_now()
        except Exception as error:  # noqa: BLE001 - 手动触发异常返回失败
            logger.warning(f"手动触发整理队列失败：{error}")
            return {"success": False, "message": f"触发失败：{error}"}
        stats = (result or {}).get("data") or {}
        return {
            "success": True,
            "message": (
                f"整理一轮完成：处理 {stats.get('processed', 0)} 行，"
                f"完成 {stats.get('done', 0)} 行，"
                f"失败 {stats.get('failed', 0)} 行，"
                f"挂起 {stats.get('held', 0)} 行"
            ),
            "data": stats,
        }

    def api_vue_funnel_stop(self, payload: Dict[str, Any] = None) -> dict:
        handler, failure = self._sync_handler_or_error("funnel_request_stop")
        if handler is None:
            return failure
        return handler.funnel_request_stop()

    def api_vue_funnel_requeue(self, payload: Dict[str, Any] = None) -> dict:
        handler, failure = self._sync_handler_or_error(
            "funnel_requeue_rows", "funnel_ensure_worker"
        )
        if handler is None:
            return failure
        ids = self._row_ids(payload or {})
        if not ids:
            return {"success": False, "message": "未选择可放回的行"}
        count = handler.funnel_requeue_rows(ids)
        if count:
            handler.funnel_ensure_worker()
        return {
            "success": True,
            "message": f"已放回队列 {count} 行",
            "data": {"requeued": count},
        }

    def api_vue_funnel_delete(self, payload: Dict[str, Any] = None) -> dict:
        handler, failure = self._sync_handler_or_error("funnel_delete_rows")
        if handler is None:
            return failure
        ids = self._row_ids(payload or {})
        if not ids:
            return {"success": False, "message": "未选择可删除的行"}
        count = handler.funnel_delete_rows(ids)
        return {
            "success": True,
            "message": f"已删除 {count} 行（pending 记录与网盘文件均保留）",
            "data": {"deleted": count},
        }
