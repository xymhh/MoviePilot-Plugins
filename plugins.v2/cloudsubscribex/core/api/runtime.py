"""运行状态、离线任务与历史操作 API。"""

import asyncio
import json
import time
from threading import Thread
from typing import Any, Dict, List, Optional

from app.core.config import global_vars
from app.log import logger
from fastapi import Request
from fastapi.responses import StreamingResponse

from .page import clear_ui_options_cache
from ..cloud import CloudDriveCapability
from ..delegation import OwnerDelegator
from ..sync_pause import set_paused, snapshot
from ..services.runtime import sync_lock
from ..task_center import collect_tasks
from ...utils import clear_magnet_metadata_cache


class RuntimeApi(OwnerDelegator):
    def api_vue_stop_sync(self) -> dict:
        return self.api_stop_sync()

    def api_vue_stop_sync_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_stop_sync_task(
            str((payload or {}).get("task_id") or ""),
        )

    def api_vue_runtime_status(self) -> dict:
        return self.api_runtime_status()

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
        """统一任务中心：只读聚合非终态历史。"""
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
        """切换全局暂停：暂停后不再启动新转存，正在写的文件不打断。"""
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
        message = "已暂停转存，不再启动新转存" if state["paused"] else "已恢复转存"
        return {"success": True, "message": message, "data": state}

    def _sync_pause_repository(self) -> Any:
        """暂停开关用的仓储；库没就绪时返回 None（读按未暂停、写明确报错）。"""
        try:
            return self._get_data_store().repositories.runtime
        except Exception as error:  # noqa: BLE001 - 暂停开关故障不能拖垮调用方
            logger.warning(f"获取转存暂停开关仓储失败：{error}")
            return None

    def api_vue_offline_tasks(self, refresh: bool = False) -> dict:
        return self.api_offline_tasks(refresh)

    def api_vue_refresh_offline_tasks(self) -> dict:
        """显式刷新离线任务；周期读取保持只读缓存语义。"""
        return self.api_offline_tasks(refresh=True)

    def api_vue_delete_offline_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_offline_task(
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
            pending_keys,
            task_ids,
        )

    def _retry_index_record_ids(
            self, identities: List[Dict[str, Any]]
    ) -> List[str]:
        """把失败页传来的记录身份（time/share_url/file_name/tmdb/season/
        episode）换算成重试索引的 record_id；算不出的直接跳过。"""
        from ...core.retry_index import retry_record_id
        record_ids = []
        for item in identities or []:
            if not isinstance(item, dict):
                continue
            record_id = retry_record_id(item)
            if record_id:
                record_ids.append(record_id)
        return record_ids

    def api_vue_clear_history(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        """清空失败记录页：删掉重试索引里全部失败与待修复行（处理中行
        正在重试，不删）；可选同时清空积分消费记录。"""
        data = payload or {}
        try:
            store = self._get_data_store()
            store.initialize()
            rows = store.repositories.retry_index.list_retryable() or []
            record_ids = [
                str(row.get("record_id") or "")
                for row in rows if str(row.get("record_id") or "")
            ]
            deleted = 0
            if record_ids:
                deleted = int(
                    store.repositories.retry_index.delete_by_record_ids(record_ids)
                    or 0
                )
        except Exception as error:
            logger.error(f"清空失败记录异常：{error}")
            return {"success": False, "message": str(error)}
        message = f"已清理 {deleted} 条失败记录"
        points = {}
        if data.get("clear_points_history") is True and self._search_handler:
            try:
                points = self._search_handler.clear_point_history() or {}
            except Exception as error:
                logger.warning(f"清空积分消费记录失败：{error}")
            if points:
                message += (
                    f"；已清理积分消费记录 HDHive={int(points.get('hdhive') or 0)}、"
                    f"Dian115={int(points.get('dian115') or 0)}"
                )
        if self._history_changed:
            self._history_changed()
        return {"success": True, "message": message, "data": {"deleted": deleted, "points_history": points}}

    def api_vue_delete_history(self, payload: Dict[str, Any]) -> dict:
        """删除单条失败记录（只删索引行，不删网盘文件与 STRM）。"""
        data = payload or {}
        record_ids = self._retry_index_record_ids([data])
        if not record_ids:
            return {"success": False, "message": "未找到对应的失败记录"}
        try:
            store = self._get_data_store()
            store.initialize()
            deleted = int(
                store.repositories.retry_index.delete_by_record_ids(record_ids)
                or 0
            )
        except Exception as error:
            logger.warning(f"删除失败记录失败：{error}")
            return {"success": False, "message": str(error)}
        if self._history_changed:
            self._history_changed()
        return {
            "success": True,
            "message": "失败记录已删除，网盘文件和 STRM 均已保留",
            "data": {"deleted": deleted},
        }

    def api_vue_delete_history_batch(self, payload: Dict[str, Any]) -> dict:
        """批量删除失败记录（只删索引行，不删网盘文件与 STRM）。"""
        data = payload or {}
        records = [item for item in (data.get("records") or []) if isinstance(item, dict)]
        if not records:
            return {"success": False, "message": "未选择可删除的失败记录"}
        if len(records) > 2000:
            return {"success": False, "message": "单次最多删除 2000 条失败记录"}
        record_ids = self._retry_index_record_ids(records)
        if not record_ids:
            return {"success": False, "message": "未找到对应的失败记录"}
        try:
            store = self._get_data_store()
            store.initialize()
            deleted = int(
                store.repositories.retry_index.delete_by_record_ids(record_ids)
                or 0
            )
        except Exception as error:
            logger.warning(f"批量删除失败记录失败：{error}")
            return {"success": False, "message": str(error)}
        if self._history_changed:
            self._history_changed()
        return {
            "success": True,
            "message": f"已删除 {deleted} 条失败记录，网盘文件和 STRM 均已保留",
            "data": {"deleted": deleted},
        }

    def api_vue_upgrade_history(self, payload: Dict[str, Any]) -> dict:
        """从媒体服务器内容发起洗版（v1.13.3：history 来源随转存记录页
        一起删除，只剩 media_server 入口）。"""
        data = payload or {}
        if not bool(getattr(self, "_enable_cloud_upgrade", False)):
            return {"success": False, "message": "请先在洗版设置中启用网盘洗版"}
        if getattr(self, "_sync_running", False) or sync_lock.locked():
            return {"success": False, "message": "已有订阅或洗版任务正在运行"}
        items = [
            value for value in ((data or {}).get("items") or [])
            if isinstance(value, dict)
        ]
        if not items:
            return {"success": False, "message": "请选择至少一个媒体库内容"}
        if len(items) > 200:
            return {"success": False, "message": "单次最多选择 200 个媒体内容"}
        try:
            targets = self._sync_handler.resolve_media_server_upgrade_targets(items)
        except Exception as error:
            logger.warning(f"媒体库洗版目标校验失败：{error}")
            return {"success": False, "message": str(error)}
        upgrade_request = {"source": "resolved", "targets": targets}
        Thread(
            target=self.sync_subscribes,
            kwargs={"upgrade_request": upgrade_request},
            daemon=True,
            name="cloudsubscribe-history-upgrade",
        ).start()
        return {"success": True, "message": f"已提交 {len(items)} 个媒体库内容的洗版任务"}

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
        """把选中的待修复行标记为已解决（删掉索引行，文件本身不动）；
        处理中行正在重试，跳过不标记。"""
        records = [item for item in ((payload or {}).get("records") or []) if isinstance(item, dict)]
        if not records:
            return {"success": False, "message": "未选择可标记的失败记录"}
        if len(records) > 2000:
            return {"success": False, "message": "单次最多标记 2000 条失败记录"}
        record_ids = self._retry_index_record_ids(records)
        if not record_ids:
            return {"success": False, "message": "未找到对应的失败记录"}
        try:
            store = self._get_data_store()
            store.initialize()
            rows = store.repositories.retry_index.list_all() or []
            processing_ids = {
                str(row.get("record_id") or "")
                for row in rows
                if str(row.get("status") or "") == "处理中"
            }
            deletable = [rid for rid in record_ids if rid not in processing_ids]
            deleted = 0
            if deletable:
                deleted = int(
                    store.repositories.retry_index.delete_by_record_ids(deletable)
                    or 0
                )
        except Exception as error:
            logger.warning(f"标记失败记录为成功失败：{error}")
            return {"success": False, "message": str(error)}
        skipped = len(record_ids) - deleted
        message = f"已把 {deleted} 条记录标记为成功"
        if skipped:
            message += f"，跳过 {skipped} 条（处理中或已变化）"
        if self._history_changed:
            self._history_changed()
        return {
            "success": True,
            "message": message,
            "data": {"updated": deleted, "skipped": skipped},
        }

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
