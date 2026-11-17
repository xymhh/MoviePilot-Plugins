"""运行状态、离线任务与历史操作 API。"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

from app.core.config import global_vars, settings
from app.log import logger
from fastapi import Request
from fastapi.responses import StreamingResponse

from .page import clear_ui_options_cache
from ..cloud import CloudDriveCapability
from ..delegation import OwnerDelegator
from ..retry_policy import is_dead_letter
from ..sync_pause import set_paused, snapshot
from ..task_center import collect_tasks
from ...utils import clear_magnet_metadata_cache


class RuntimeApi(OwnerDelegator):
    def api_vue_stop_sync(self) -> dict:
        return self.api_stop_sync(settings.API_TOKEN)

    def api_vue_stop_sync_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_stop_sync_task(
            settings.API_TOKEN,
            str((payload or {}).get("task_id") or ""),
        )

    def api_vue_runtime_status(self) -> dict:
        return self.api_runtime_status(settings.API_TOKEN)

    async def api_vue_runtime_stream(self, request: Request) -> StreamingResponse:
        """以单条 SSE 连接推送运行态，合并高频进度变化。"""

        async def event_generator():
            last_revision = -1
            last_heartbeat = time.monotonic()
            yield "retry: 3000\n\n"
            try:
                while not global_vars.is_system_stopped:
                    if await request.is_disconnected():
                        break
                    revision = self._runtime_revision_value()
                    now = time.monotonic()
                    if revision != last_revision:
                        snapshot = self._runtime_snapshot()
                        last_revision = int(snapshot.get("revision") or 0)
                        payload = json.dumps(
                            snapshot, ensure_ascii=False, separators=(",", ":")
                        )
                        yield f"data: {payload}\n\n"
                        last_heartbeat = now
                    elif now - last_heartbeat >= 15:
                        yield ": keepalive\n\n"
                        last_heartbeat = now
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def api_vue_tasks(self, state: str = "all") -> dict:
        """统一任务中心：只读聚合同步作业与非终态历史。"""
        try:
            store = self._get_data_store()
            store.initialize()
            with store.manager.session(write=False) as db:
                tasks = collect_tasks(db, state_filter=state)
            return {
                "success": True,
                "data": tasks,
                "total": len(tasks),
                "state": str(state or "all"),
                "paused": snapshot(self._sync_pause_repository()),
            }
        except Exception as error:  # noqa: BLE001 - API 失败返回结构统一
            logger.error(f"读取任务中心失败：{error}", exc_info=True)
            return {"success": False, "message": str(error), "data": []}

    def api_vue_sync_pause(self) -> dict:
        """读取全局转存暂停开关。"""
        return {
            "success": True,
            "data": snapshot(self._sync_pause_repository()),
        }

    def api_vue_set_sync_pause(self, payload: Dict[str, Any]) -> dict:
        """切换全局暂停：暂停后不再认领新作业，正在写的文件不打断。"""
        data = payload if isinstance(payload, dict) else {}
        if "paused" not in data:
            return {"success": False, "message": "缺少 paused 参数"}
        try:
            state = set_paused(
                bool(data.get("paused")),
                actor="任务中心",
                repository=self._sync_pause_repository(),
            )
        except Exception as error:  # noqa: BLE001 - API 失败返回结构统一
            logger.error(f"切换转存暂停失败：{error}", exc_info=True)
            return {"success": False, "message": str(error)}
        self._sync_pause_cache = None
        message = "已暂停转存，新作业不再认领" if state["paused"] else "已恢复转存"
        return {"success": True, "message": message, "data": state}

    def api_vue_pause_task(self, payload: Dict[str, Any]) -> dict:
        """暂停单个排队中/等待重试的作业（不消耗重试次数）。"""
        return self._task_pause_action(payload, pause=True)

    def api_vue_resume_task(self, payload: Dict[str, Any]) -> dict:
        """恢复暂停的作业，重新排队。"""
        return self._task_pause_action(payload, pause=False)

    def _task_pause_action(self, payload: Dict[str, Any], pause: bool) -> dict:
        data = payload if isinstance(payload, dict) else {}
        job_id = str(data.get("job_id") or "").strip()
        job_key = str(data.get("job_key") or "").strip()
        if not job_id and not job_key:
            return {"success": False, "message": "缺少 task 标识"}
        action = "暂停" if pause else "恢复"
        try:
            store = self._get_data_store()
            store.initialize()
            repo = store.repositories.sync_jobs
            row = repo.get_by_id(job_id) if job_id else repo.get(job_key)
            if row is None:
                return {"success": False, "message": "未找到对应任务"}
            if pause:
                changed = repo.pause(row.job_key, "用户在任务中心暂停")
            else:
                changed = repo.resume(row.job_key, "用户在任务中心恢复")
            if not changed:
                return {
                    "success": False,
                    "message": (
                        f"当前状态无法{action}（只有排队中/等待重试的任务能暂停）"
                        if pause else "该任务不是暂停状态，无需恢复"
                    ),
                }
            logger.info(
                f"任务中心{action}作业：{row.target_path}/{row.target_name}"
            )
            return {"success": True, "message": f"已{action}"}
        except Exception as error:  # noqa: BLE001 - API 失败返回结构统一
            logger.error(f"任务中心{action}失败：{error}", exc_info=True)
            return {"success": False, "message": str(error)}

    def api_vue_retry_task(self, payload: Dict[str, Any]) -> dict:
        """任务中心显式重试：只复活死信作业，次数与退避清零后重新入队。"""
        data = payload if isinstance(payload, dict) else {}
        job_id = str(data.get("job_id") or "").strip()
        job_key = str(data.get("job_key") or "").strip()
        if not job_id and not job_key:
            return {"success": False, "message": "缺少 task 标识"}
        try:
            store = self._get_data_store()
            store.initialize()
            repo = store.repositories.sync_jobs
            row = (
                repo.get_by_id(job_id) if job_id else repo.get(job_key)
            )
            if row is None:
                return {"success": False, "message": "未找到对应任务"}
            if not is_dead_letter(
                    row.state, row.last_error, row.attempt, row.max_attempts,
            ):
                return {
                    "success": False,
                    "message": "该任务不在死信状态，无需手动重试",
                }
            reopened = repo.reopen_dead_letter(
                row.job_key, "用户在任务中心手动重试",
            )
            if not reopened:
                return {"success": False, "message": "重试未能入队，请刷新后重看"}
            logger.info(
                f"任务中心手动重试死信：{row.target_path}/{row.target_name}"
            )
            return {"success": True, "message": "死信已重新入队，最多再试 3 次"}
        except Exception as error:  # noqa: BLE001 - API 失败返回结构统一
            logger.error(f"任务中心重试失败：{error}", exc_info=True)
            return {"success": False, "message": str(error)}

    def _sync_pause_repository(self) -> Any:
        """暂停开关用的仓储；库没就绪时返回 None（读按未暂停、写明确报错）。"""
        try:
            return self._get_data_store().repositories.runtime
        except Exception as error:  # noqa: BLE001 - 暂停开关故障不能拖垮调用方
            logger.warning(f"获取转存暂停开关仓储失败：{error}")
            return None

    def api_vue_offline_tasks(self, refresh: bool = False) -> dict:
        return self.api_offline_tasks(settings.API_TOKEN, refresh)

    def api_vue_refresh_offline_tasks(self) -> dict:
        """显式刷新离线任务；周期读取保持只读缓存语义。"""
        return self.api_offline_tasks(settings.API_TOKEN, refresh=True)

    def api_vue_delete_offline_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_offline_task(
            settings.API_TOKEN,
            str((payload or {}).get("task_id") or ""),
            str((payload or {}).get("pending_key") or ""),
            delete_source_file=bool((payload or {}).get("delete_source_file", False)),
        )

    def api_vue_delete_offline_tasks(self, payload: Dict[str, Any]) -> dict:
        task_ids = [
            str(value).strip()
            for value in ((payload or {}).get("task_ids") or [])
            if str(value).strip()
        ]
        pending_keys = [
            str(value).strip()
            for value in ((payload or {}).get("pending_keys") or [])
            if str(value).strip()
        ]
        return self.api_delete_offline_tasks(
            settings.API_TOKEN,
            task_ids,
            pending_keys,
            delete_source_file=bool((payload or {}).get("delete_source_file", False)),
        )

    def api_vue_retry_offline_tasks(self, payload: Dict[str, Any]) -> dict:
        pending_keys = [
            str(value).strip()
            for value in ((payload or {}).get("pending_keys") or [])
            if str(value).strip()
        ]
        task_ids = [
            str(value).strip()
            for value in ((payload or {}).get("task_ids") or [])
            if str(value).strip()
        ]
        return self.api_retry_offline_tasks(
            settings.API_TOKEN,
            pending_keys,
            task_ids,
        )

    def api_vue_clear_history(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        return self.api_clear_history(
            settings.API_TOKEN,
            force=(payload or {}).get("force") is True,
            clear_points_history=(payload or {}).get("clear_points_history") is True,
        )

    def api_vue_delete_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_history(settings.API_TOKEN, payload or {})

    def api_vue_delete_history_batch(self, payload: Dict[str, Any]) -> dict:
        data = payload or {}
        return self.api_delete_history_batch(
            settings.API_TOKEN,
            identities=data.get("records") or [],
            delete_linked_files=data.get("delete_linked_files") is True,
        )

    def api_vue_upgrade_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_upgrade_history(settings.API_TOKEN, payload or {})

    def api_vue_notify_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_notify_history(settings.API_TOKEN, payload or {})

    def api_vue_retry_history(self, payload: Dict[str, Any]) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            result = self._sync_handler.submit_history_retry(
                record_time=str((payload or {}).get("time") or ""),
                share_url=str((payload or {}).get("share_url") or ""),
                file_name=str((payload or {}).get("file_name") or ""),
            )
            return {
                "success": True,
                "message": "已提交重试任务，正在后台处理",
                "data": result,
            }
        except Exception as error:
            logger.error(f"提交历史记录重试异常：{error}")
            return {"success": False, "message": str(error)}

    def api_vue_retry_history_batch(self, payload: Dict[str, Any]) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            result = self._sync_handler.submit_history_retry_batch(
                (payload or {}).get("records") or []
            )
            return {
                "success": True,
                "message": (
                    f"已提交 {result.get('submitted', 0)} 条，"
                    f"跳过 {result.get('skipped', 0)} 条"
                ),
                "data": result,
            }
        except Exception as error:
            logger.error(f"提交批量历史重试异常：{error}")
            return {"success": False, "message": str(error)}

    def api_vue_mark_history_success(self, payload: Dict[str, Any]) -> dict:
        return self.api_mark_history_success(
            settings.API_TOKEN,
            identities=(payload or {}).get("records") or [],
        )

    def api_vue_backfill_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_backfill_history(
            settings.API_TOKEN,
            keyword=str((payload or {}).get("keyword") or ""),
        )

    _CACHE_CATEGORIES = frozenset({
        "search", "cloud", "sync", "interface", "platform",
    })





    def api_vue_strm_full_scan(
            self, payload: Optional[Dict[str, Any]] = None
    ) -> dict:
        """手动触发 STRM 全量校准（后台线程执行）。"""
        del payload
        result = self._owner.run_strm_full_scan("手动")
        return {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or ""),
        }

    def api_vue_cleanup_residue(
            self, payload: Optional[Dict[str, Any]] = None
    ) -> dict:
        """手动清理 cloud-transfer-* 残留（跨盘传输半截文件）。"""
        del payload
        try:
            result = self._owner.cleanup_transfer_residue("手动")
            freed = int(result.get("freed_bytes") or 0) / (1024 * 1024)
            message = (
                f"已清理 {result.get('deleted_files')} 个残留文件，"
                f"释放 {freed:.1f} MB"
                if result.get("deleted_files")
                else "没有可清理的残留文件"
            )
            if result.get("skipped_recent") or result.get("skipped_active"):
                message += (
                    f"（跳过未过期 {result.get('skipped_recent')} 组、"
                    f"任务中 {result.get('skipped_active')} 组）"
                )
            return {"success": True, "message": message, "data": result}
        except Exception as error:  # noqa: BLE001
            logger.error(f"清理跨盘缓存残留失败：{error}")
            return {"success": False, "message": str(error)}

    def api_vue_clear_cache(
            self, payload: Optional[Dict[str, Any]] = None
    ) -> dict:
        requested = (payload or {}).get("categories")
        categories = (
            self._CACHE_CATEGORIES
            if requested is None
            else {
                str(value).strip().lower()
                for value in requested
                if str(value).strip().lower() in self._CACHE_CATEGORIES
            }
        )
        if not categories:
            return {"success": False, "message": "请至少选择一项缓存内容"}
        counts: Dict[str, int] = {}
        try:
            if "search" in categories:
                if self._search_handler:
                    counts.update(self._search_handler.clear_search_cache())
                counts["magnet_metadata"] = clear_magnet_metadata_cache()
            if "cloud" in categories and (
                    self._cloud_drive and self._cloud_drive.supports(
                    CloudDriveCapability.CACHE_MAINTENANCE
            )
            ):
                cache_service = self._cloud_drive.require(
                    CloudDriveCapability.CACHE_MAINTENANCE
                )
                counts.update(cache_service.clear_cache())
            if "sync" in categories and self._sync_handler:
                counts.update(self._sync_handler.clear_runtime_cache())
            if "interface" in categories:
                counts["ui_options"] = clear_ui_options_cache()
            if "platform" in categories:
                counts.update(self.clear_platform_cache())
            total = sum(int(value or 0) for value in counts.values())
            logger.info(
                f"插件缓存已清理：{total} 项，分类={','.join(sorted(categories))}"
            )
            return {
                "success": True,
                "message": f"缓存已清理，共移除 {total} 项",
                "data": {
                    "categories": sorted(categories),
                    "counts": counts,
                },
            }
        except Exception as error:
            logger.error(f"清理插件缓存失败：{error}")
            return {"success": False, "message": str(error)}
