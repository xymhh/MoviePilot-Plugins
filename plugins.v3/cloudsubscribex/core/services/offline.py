"""离线任务生命周期（115 离线下载任务的查看/重试/删除与终态监控）。

v1.24.0 从 ``SyncRuntimeService``（core/services/runtime.py）拆出：
方法体逐行原样搬移，行为不变。Owner 仍是插件实例——``self._sync_handler``、
``self._cloud_drive``、``self._sync_tasks``、``self._offline_monitor_lock``
等运行态都经 OwnerDelegator 委托到插件上，与拆分前解析路径一致。
插件通过 ``_COMPONENT_TYPES`` 按方法名解析到本类（见 __init__.py）。
"""

import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import pytz
from app.core.config import settings
from app.log import logger
from apscheduler.triggers.interval import IntervalTrigger

from ...core import CloudDriveCapability, OwnerDelegator


class OfflineTaskService(OwnerDelegator):
    """管理离线任务的查看、重试、删除与网盘文件终态后处理监控。"""

    @staticmethod
    def _pending_count_or_zero(sync_handler: Any) -> int:
        """取待处理记录数；取不到时返回 0（异常路径的 result 仍要带 pending 数）。"""
        try:
            return len(sync_handler.get_pending_finalize_tasks())
        except Exception:
            return 0

    def _log_monitor_heartbeat(self, result: Dict[str, Any]) -> None:
        """v1.26.0：监控每轮结果记 INFO（节流）。

        成功路径此前全是 DEBUG，任务停跑与正常等待在日志里无法区分；
        只在完成/失败数>0 或待处理数变化时记一行：心跳停 = 监控没在完成轮次。
        """
        completed = int(result.get("completed") or 0)
        failed = int(result.get("failed") or 0)
        pending = int(result.get("pending") or 0)
        last_pending = self.__dict__.get("_monitor_last_pending")
        self.__dict__["_monitor_last_pending"] = pending
        if not completed and not failed and last_pending == pending:
            return
        logger.info(
            f"网盘文件后处理监控：完成 {completed} 个，失败 {failed} 个，待处理 {pending} 个"
        )

    def _monitor_offline_task_groups(
            self,
            sync_handler: Any,
            offline_service: Optional[Any],
            kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            groups = sync_handler.get_due_offline_task_groups(
                force=bool(kwargs.get("force")),
                pending_keys=kwargs.get("pending_keys"),
            )
        except Exception as error:
            # v1.26.0：分组异常不再直抛给调度器（只进主日志、插件侧静默），
            # 在插件日志记 ERROR 并返回带待处理数的 result，监控继续下一轮。
            logger.error(f"媒体后处理分组失败：{error}", exc_info=True)
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": self._pending_count_or_zero(sync_handler),
            }
        with self._sync_tasks_lock:
            stopping_keys = {
                str(pending_key)
                for task in self._sync_tasks.values()
                if task.get("status") == "stopping"
                   and task.get("postprocess_stop_token")
                for pending_key in task.get("postprocess_stop_pending_keys") or set()
            }
        if stopping_keys:
            filtered_groups = []
            for group in groups:
                active_keys = set(group["pending_keys"]) - stopping_keys
                if not active_keys:
                    continue
                filtered_group = dict(group)
                filtered_group["pending_keys"] = active_keys
                filtered_groups.append(filtered_group)
            groups = filtered_groups
        if not groups:
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": len(sync_handler.get_pending_finalize_tasks()),
            }
        shared_kwargs = dict(kwargs)
        if (
                any(group["needs_offline"] for group in groups)
                and offline_service
                and shared_kwargs.get("offline_tasks") is None
        ):
            try:
                snapshot = offline_service.get_offline_task_list_snapshot(
                    force=True,
                )
            except Exception as error:
                # v1.26.0：前置快照失败不阻断本轮——各组在
                # _prepare_postprocess_context 里需要时还会自己拉一次。
                logger.error(f"媒体后处理离线快照失败：{error}", exc_info=True)
            else:
                shared_kwargs["offline_tasks"] = snapshot.get("tasks") or []
                shared_kwargs["offline_tasks_valid"] = bool(snapshot.get("refresh_ok"))

        if len(groups) == 1:
            shared_kwargs["pending_keys"] = set(groups[0]["pending_keys"])
            try:
                return sync_handler.monitor_offline_strm_tasks(**shared_kwargs)
            except Exception as error:
                # v1.26.0：单组路径此前无任何 try/except，异常直抛给调度器、
                # 整批零处理且插件日志零记录；现在记 ERROR 并返回带待处理数的 result。
                logger.error(f"媒体后处理单组执行失败：{error}", exc_info=True)
                return {
                    "checked": 0,
                    "completed": 0,
                    "failed": 0,
                    "pending": self._pending_count_or_zero(sync_handler),
                }

        policy = getattr(getattr(self, "_cloud_drive", None), "policy", None)
        worker_count = min(
            len(groups),
            max(1, int(getattr(policy, "max_concurrency", 1) or 1)),
        )
        logger.debug(
            f"离线后处理并发调度：{len(groups)} 个媒体队列，并发数 {worker_count}"
        )
        totals = {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="cloudsubscribe-offline",
        ) as executor:
            futures = {
                executor.submit(
                    sync_handler.monitor_offline_strm_tasks,
                    **{
                        **shared_kwargs,
                        "pending_keys": set(group["pending_keys"]),
                    },
                ): group
                for group in groups
            }
            for future in as_completed(futures):
                group = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    logger.error(
                        f"媒体后处理队列执行失败："
                        f"{len(group['pending_keys'])} 个文件，{error}"
                    )
                    continue
                for field in ("checked", "completed", "failed"):
                    totals[field] += int(result.get(field) or 0)
        totals["pending"] = len(sync_handler.get_pending_finalize_tasks())
        return totals

    def _run_offline_monitor(self, **kwargs: Any) -> Dict[str, Any]:
        """按媒体队列并发执行网盘文件终态检查。"""
        sync_handler = self._sync_handler
        offline_service = self._offline_task_service()
        if not sync_handler:
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": 0,
            }
        force = bool(kwargs.get("force"))
        lock_acquired = self._offline_monitor_lock.acquire(
            blocking=force, timeout=10.0 if force else -1
        )
        if not lock_acquired:
            pending_count = len(sync_handler.get_pending_finalize_tasks())
            logger.debug(
                f"已有网盘文件后处理正在执行，本轮检查跳过"
                f"（待处理 {pending_count} 个）"
            )
            return {
                "checked": 0,
                "completed": 0,
                "failed": 0,
                "pending": pending_count,
                "deferred": 1,
            }
        try:
            notification_batch_started = sync_handler.begin_notification_batch()
            try:
                result = self._monitor_offline_task_groups(
                    sync_handler, offline_service, kwargs
                )
                self._log_monitor_heartbeat(result)
                return result
            finally:
                if notification_batch_started:
                    sync_handler.finish_notification_batch()
        finally:
            self._offline_monitor_lock.release()

    def api_offline_tasks(self, refresh: bool = False) -> dict:
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务", "data": []}
        snapshot = offline_tasks.get_offline_tasks_snapshot(force=refresh)
        snapshot["provider"] = self._cloud_drive.key
        snapshot["provider_name"] = self._cloud_drive.name
        monitor_result = None
        if refresh and self._sync_handler:
            monitor_result = self._run_offline_monitor(
                force=True,
                offline_tasks=snapshot.get("tasks") or [],
                offline_tasks_valid=bool(snapshot.get("refresh_ok")),
            )
        snapshot = self._merge_pending_finalize_tasks(snapshot)
        if monitor_result is not None:
            snapshot["monitor"] = monitor_result
        return {
            "success": True,
            "message": "已手动刷新离线任务" if refresh else "已返回离线任务缓存",
            "data": snapshot,
        }

    def _merge_pending_finalize_tasks(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(snapshot or {})
        tasks = [dict(item) for item in (result.get("tasks") or [])]
        pending = self._sync_handler.get_pending_finalize_tasks() if self._sync_handler else []
        task_by_id: Dict[str, Any] = {}
        for item in tasks:
            tid = str(item.get("id") or "").upper()
            if tid:
                task_by_id[tid] = item
            nid = str(item.get("native_id") or "").upper()
            if nid and nid not in task_by_id:
                task_by_id[nid] = item
        offline_pending_count = 0
        for item in pending:
            task_type = str(item.get("task_type") or "share").strip().lower()
            is_offline_type = task_type in {"magnet", "ed2k", "offline"}
            task_id = str(item.get("task_id") or "").upper()
            task = task_by_id.get(task_id) if task_id else None
            # v1.26.0：全部待后处理记录都参与合并——有对应离线任务的合
            # 并进该任务，没有的合成 pending: 项（分享类无真实离线任务
            # id，旧守卫会把它们全拦在合成支路之外，界面上完全不可见）。
            offline_pending_count += 1
            pending_meta = {
                "pending_key": item.get("pending_key"),
                "finalize_pending": True,
                "cloud_dir": item.get("cloud_dir"),
                "target_name": item.get("file_name"),
                "size": int(item.get("file_size") or (task or {}).get("size") or 0),
                "postprocess_text": (
                    "等待下载完成后读取真实文件并匹配"
                    if task_type == "magnet"
                    else "等待下载完成或文件处理"
                    if task_type == "ed2k"
                    else "等待系统处理、重命名和STRM"
                ),
            }
            if task:
                task.update(pending_meta)
            else:
                # v1.26.0：分享类 pending 记录也进快照（带 finalize_pending
                # 标记），在 OfflineTasksDialog 展示——此前只有离线类或在
                # 网盘离线任务里有对应 task_id 的才进，分享转存的待处理
                # 记录在界面上完全不可见。合成项不带 id：对话框按 pending_key
                # 选择/重试/删除；带 id 会被当成真实离线任务 id 去重启 115
                # 任务。离线类沿用旧形状（id 即真实离线任务 id）。
                synthetic = {
                    "name": item.get("file_name") or "未命名文件",
                    "state": "processing",
                    "completed": False,
                    "failed": False,
                    "status_text": "系统处理中",
                    "percent": 100,
                    "add_time": int(item.get("created_at") or 0),
                    **pending_meta,
                }
                if is_offline_type:
                    synthetic["id"] = task_id
                tasks.insert(0, synthetic)
        if self._sync_handler:
            pending_task_keys = {
                str(item.get("id") or "").upper()
                for item in tasks
                if item.get("finalize_pending")
            }
            for task in tasks:
                if task.get("finalize_pending"):
                    continue
                task_id = str(task.get("id") or "").strip()
                if not task_id or task_id.upper() in pending_task_keys:
                    continue
                try:
                    finalized = self._sync_handler.is_offline_task_finalized(task_id)
                except Exception as error:
                    logger.debug(f"标注离线任务入库状态失败 {task_id}：{error}")
                    continue
                if not finalized:
                    continue
                # 只影响展示层：state/completed/percent 等判定字段保持不变
                task["finalized"] = True
                task["finalized_text"] = "已入库"
                task["status_text"] = "已完成（已入库）"
        result["tasks"] = tasks
        result["pending_count"] = offline_pending_count
        return result

    def api_retry_offline_tasks(
            self,
            pending_keys: List[str],
            task_ids: Optional[List[str]] = None,
    ) -> dict:
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务"}
        keys = {str(value).strip() for value in pending_keys if str(value).strip()}
        normalized_task_ids = {
            str(value).strip().upper()
            for value in (task_ids or [])
            if str(value).strip()
        }
        if not keys and not normalized_task_ids:
            return {"success": False, "message": "请选择需要重试的任务"}
        restarted = 0
        restart_failed = 0
        for task_id in normalized_task_ids:
            try:
                offline_tasks.restart_offline_task(task_id)
                restarted += 1
                if self._sync_handler:
                    self._sync_handler._remove_offline_blacklist(task_id)
            except Exception as error:
                restart_failed += 1
                logger.error(f"重启离线任务失败 {task_id}：{error}")
        result = {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        if keys:
            if not self._sync_handler:
                return {"success": False, "message": "同步处理器未初始化"}
            result = self._run_offline_monitor(
                force=True,
                pending_keys=keys,
            )
        return {
            "success": restart_failed == 0,
            "message": (
                    f"已重启 {restarted} 个离线任务，复查 {result['checked']} 个后处理任务，"
                    f"完成 {result['completed']} 个，仍待处理 {result['pending']} 个"
                    + (f"，重启失败 {restart_failed} 个" if restart_failed else "")
            ),
            "data": {
                **result,
                "restarted": restarted,
                "restart_failed": restart_failed,
            },
        }

    def api_delete_offline_task(
            self, task_id: str, pending_key: str = "", delete_source_file: bool = False
    ) -> dict:
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务"}
        try:
            normalized_id = str(task_id or "").strip().upper()
            normalized_pending_key = str(pending_key or "").strip()
            monitor_result = None
            removed_pending = 0
            if self._sync_handler and (normalized_pending_key or normalized_id):
                removed_pending = self._sync_handler.delete_pending_finalize_tasks(
                    {normalized_pending_key} if normalized_pending_key else set(),
                    {normalized_id} if normalized_id else set(),
                )
            if normalized_id:
                try:
                    offline_tasks.delete_offline_task(
                        normalized_id, delete_source_file=delete_source_file
                    )
                except Exception:
                    if not removed_pending:
                        raise
                    logger.debug(
                        f"{self._cloud_drive.name}离线任务已不存在，继续清理待后处理记录：{normalized_id}"
                    )
            if self._sync_handler and normalized_id and not removed_pending:
                snapshot = offline_tasks.get_offline_tasks_snapshot(force=False)
                monitor_result = self._run_offline_monitor(
                    force=True,
                    pending_keys={normalized_id},
                    offline_tasks=snapshot.get("tasks") or [],
                    # 删除接口成功已经确认该任务不存在，无需再请求一次任务列表。
                    offline_tasks_valid=True,
                )
            file_msg = "已删除网盘原文件" if delete_source_file else "已下载文件保留"
            if normalized_id and removed_pending:
                message = f"离线任务和后处理任务已删除，{file_msg}"
            elif normalized_id:
                message = f"离线任务已删除，{file_msg}"
            else:
                message = f"后处理任务已删除，{file_msg}"
            if monitor_result and monitor_result.get("failed"):
                message += "；目标文件不存在，下载历史已结束"
            elif monitor_result and monitor_result.get("completed"):
                message += "；目标文件已完成后处理"
            return {
                "success": True,
                "message": message,
                "data": {
                    "monitor": monitor_result,
                    "pending_deleted": removed_pending,
                },
            }
        except Exception as error:
            logger.error(f"删除离线任务失败：{error}")
            return {"success": False, "message": str(error)}

    def api_delete_offline_tasks(
            self,
            task_ids: List[str],
            pending_keys: Optional[List[str]] = None,
            delete_source_file: bool = False,
    ) -> dict:
        offline_tasks = self._offline_task_service()
        if not offline_tasks:
            return {"success": False, "message": "当前网盘不支持离线任务"}
        try:
            normalized_ids = {
                str(value or "").strip().upper() for value in task_ids
                if str(value or "").strip()
            }
            normalized_pending_keys = {
                str(value or "").strip() for value in (pending_keys or [])
                if str(value or "").strip()
            }
            removed_pending = 0
            if self._sync_handler and (normalized_pending_keys or normalized_ids):
                removed_pending = self._sync_handler.delete_pending_finalize_tasks(
                    normalized_pending_keys,
                    normalized_ids,
                )
            try:
                count = offline_tasks.delete_offline_tasks(
                    task_ids, delete_source_file=delete_source_file
                ) if task_ids else 0
            except Exception:
                if not removed_pending:
                    raise
                count = 0
                logger.debug(
                    f"部分{self._cloud_drive.name}离线任务已不存在，继续批量清理待后处理记录"
                )
            monitor_result = None
            if self._sync_handler and normalized_ids and not removed_pending:
                snapshot = offline_tasks.get_offline_tasks_snapshot(force=False)
                monitor_result = self._run_offline_monitor(
                    force=True,
                    pending_keys=normalized_ids,
                    offline_tasks=snapshot.get("tasks") or [],
                    offline_tasks_valid=True,
                )
            return {
                "success": True,
                "message": (
                        f"已删除 {count} 个离线任务"
                        + (f"、{removed_pending} 个后处理任务" if removed_pending else "")
                        + ("，已删除网盘原文件" if delete_source_file else "，已下载文件保留")
                ),
                "data": {
                    "deleted": count,
                    "pending_deleted": removed_pending,
                    "monitor": monitor_result,
                },
            }
        except Exception as error:
            logger.error(f"批量删除离线任务失败：{error}")
            return {"success": False, "message": str(error)}

    def _update_offline_monitor(self, pending_count: int) -> None:
        """仅在存在待后处理文件时运行独立监控。"""
        self._refresh_postprocessing_sync_tasks()
        with self._offline_scheduler_lock:
            registry = self._scheduler_registry()
            if pending_count > 0:
                if registry.get_job("CloudSubscribe_OfflineMonitor") is not None:
                    registry.modify_job_next_run(
                        "CloudSubscribe_OfflineMonitor",
                        datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        ) + datetime.timedelta(seconds=1),
                    )
                    return
                registry.ensure_job(
                    "CloudSubscribe_OfflineMonitor",
                    func=self.monitor_offline_tasks,
                    trigger=IntervalTrigger(seconds=20),
                    next_run_time=datetime.datetime.now(
                        tz=pytz.timezone(settings.TZ)
                    ) + datetime.timedelta(seconds=3),
                    max_instances=2,
                    coalesce=True,
                )
                # v1.26.0：监控启动/停止提为 INFO——此前是 DEBUG，任务停跑
                # 在插件日志里不可见（见根因 (a)/(b)）。
                logger.info(f"网盘文件终态后处理监控已启动（待处理 {pending_count} 个）")
                return

            if registry.remove_job("CloudSubscribe_OfflineMonitor"):
                logger.info("网盘文件终态后处理队列已清空，监控已停止")

    def monitor_offline_tasks(self):
        """轻量检查到期节点，实际网盘请求间隔由待处理任务控制。"""
        return self._run_offline_monitor()

    def _offline_task_service(self):
        if not self._cloud_drive or not self._cloud_drive.supports(
                CloudDriveCapability.OFFLINE_TASKS
        ):
            return None
        return self._cloud_drive.require(CloudDriveCapability.OFFLINE_TASKS)
