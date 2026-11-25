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
        replaced_from = row.get("replaced_from") or []
        # v1.30.0 阶段 4：孤儿文件位置（held 行原因里的 orphan_location=
        # 标记），队列页“删除孤儿文件”按钮据此显示。
        orphan_location = ""
        marker = "orphan_location="
        marker_index = reason.find(marker)
        display_reason = reason
        if marker_index >= 0:
            rest = reason[marker_index + len(marker):].strip()
            # 原因串中英混排，分隔符可能是 ASCII 或中文分号。
            ends = [
                end for end in (rest.find(";"), rest.find("；")) if end >= 0
            ]
            orphan_location = (
                rest[:min(ends)] if ends else rest
            ).strip()
            # 展示原因里去掉机器标记（位置另有 orphan_location 字段，
            # 队列页“删孤儿”按钮据此显示；DB 里的原因不动，解析仍走它）。
            tail = rest[min(ends) + 1:] if ends else ""
            display_reason = (
                reason[:marker_index] + tail
            ).strip().strip(";；").strip()
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
            "reason": display_reason,
            "is_upgrade": bool(row.get("is_upgrade")),
            # v1.29.10 热修复：挂起/失败行的人工删旧版按钮据此显示。
            "has_old_version": bool(replaced_from),
            "old_version_count": len(replaced_from),
            "has_orphan": bool(orphan_location),
            "orphan_location": orphan_location,
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
                    # v1.30.0 阶段 4：总闸（kill-switch）与 dry-run 开关。
                    "funnel_enabled": bool(
                        getattr(self, "_funnel_enabled", True)
                    ),
                    "funnel_scan_dry_run": bool(
                        getattr(self, "_funnel_scan_dry_run", True)
                    ),
                    "phase1_enabled": bool(
                        getattr(self, "_funnel_phase1_enabled", False)
                    ),
                    "phase2_enabled": bool(
                        getattr(self, "_funnel_phase2_enabled", False)
                    ),
                    "phase3_enabled": bool(
                        getattr(self, "_funnel_phase3_enabled", False)
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
                # v1.30.0 阶段 4：总闸（kill-switch）与 dry-run 开关。
                "funnel_enabled": bool(
                    getattr(self, "_funnel_enabled", True)
                ),
                "funnel_scan_dry_run": bool(
                    getattr(self, "_funnel_scan_dry_run", True)
                ),
                "phase1_enabled": bool(
                    getattr(self, "_funnel_phase1_enabled", False)
                ),
                "phase2_enabled": bool(
                    getattr(self, "_funnel_phase2_enabled", False)
                ),
                "phase3_enabled": bool(
                    getattr(self, "_funnel_phase3_enabled", False)
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

    def api_vue_funnel_delete_old_version(self, payload: Dict[str, Any] = None) -> dict:
        """v1.29.10 热修复：人工删旧版（held/failed 行的 replaced_from）。"""
        handler, failure = self._sync_handler_or_error("funnel_delete_old_version")
        if handler is None:
            return failure
        ids = self._row_ids(payload or {})
        if not ids:
            return {"success": False, "message": "未选择可删旧版的行"}
        try:
            result = handler.funnel_delete_old_version(ids)
        except Exception as error:  # noqa: BLE001 - 删旧版异常返回失败
            logger.warning(f"人工删旧版失败：{error}")
            return {"success": False, "message": f"删旧版失败：{error}"}
        results = (result or {}).get("results") or []
        ok_count = sum(1 for item in results if item.get("success"))
        fail_count = len(results) - ok_count
        if fail_count and not ok_count:
            return {
                "success": False,
                "message": "; ".join(
                    str(item.get("message") or "删旧版失败")
                    for item in results
                ),
                "data": result,
            }
        return {
            "success": True,
            "message": (
                f"已删旧版 {ok_count} 行"
                + (f"，失败 {fail_count} 行" if fail_count else "")
            ),
            "data": result,
        }

    def api_vue_funnel_rounds(self) -> dict:
        """v1.30.0 阶段 4：最近 20 轮漏斗轮次摘要（worker + 扫描轮次）。"""
        handler, failure = self._sync_handler_or_error("funnel_round_history")
        if handler is None:
            return failure
        try:
            rounds = handler.funnel_round_history()
        except Exception as error:  # noqa: BLE001 - 读历史异常返回失败
            logger.warning(f"读取漏斗轮次历史失败：{error}")
            return {"success": False, "message": f"读取轮次历史失败：{error}"}
        return {"success": True, "data": {"rounds": rounds or []}}

    def api_vue_funnel_scan(self, payload: Dict[str, Any] = None) -> dict:
        """v1.30.0 阶段 4：手动触发一次扫描对账（默认 dry-run，只分类记日志）。"""
        handler, failure = self._sync_handler_or_error("funnel_run_scan_dry_run")
        if handler is None:
            return failure
        try:
            result = handler.funnel_run_scan_dry_run(trigger="手动")
        except Exception as error:  # noqa: BLE001 - 扫描异常返回失败
            logger.warning(f"手动扫描对账失败：{error}")
            return {"success": False, "message": f"扫描失败：{error}"}
        if not (result or {}).get("success"):
            return {
                "success": False,
                "message": str((result or {}).get("message") or "扫描失败"),
                "data": result,
            }
        return {
            "success": True,
            "message": (
                "扫描对账 dry-run 完成：扫描 "
                f"{(result or {}).get('scanned', 0)} 包，可认领 "
                f"{(result or {}).get('recognizable', 0)}，待人工认领 "
                f"{(result or {}).get('unrecognizable', 0)}，已入库跳过 "
                f"{(result or {}).get('already_in_library', 0)}"
                "（未入队，详见插件日志）"
            ),
            "data": result,
        }

    def api_vue_funnel_claim(self, payload: Dict[str, Any] = None) -> dict:
        """v1.30.0 阶段 4：held 行人工认领（指定剧集/TMDB 后重新入队）。"""
        handler, failure = self._sync_handler_or_error("funnel_claim_row")
        if handler is None:
            return failure
        payload = payload or {}
        try:
            row_id = int(payload.get("id") or 0)
            tmdb_id = int(payload.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "行 ID 或 TMDB ID 非法"}
        media_type = str(payload.get("media_type") or "").strip().lower()
        season = payload.get("season")
        episode = payload.get("episode")
        try:
            result = handler.funnel_claim_row(
                row_id, tmdb_id, media_type, season=season, episode=episode,
            )
        except Exception as error:  # noqa: BLE001 - 认领异常返回失败
            logger.warning(f"人工认领漏斗行失败：{error}")
            return {"success": False, "message": f"认领失败：{error}"}
        if not (result or {}).get("success"):
            return {
                "success": False,
                "message": str((result or {}).get("message") or "认领失败"),
                "data": result,
            }
        return {
            "success": True,
            "message": str((result or {}).get("message") or "已认领并重新入队"),
            "data": result,
        }

    def api_vue_funnel_delete_orphan(self, payload: Dict[str, Any] = None) -> dict:
        """v1.30.0 阶段 4：held 行人工删孤儿文件（.mkv 连带同名 .strm）。"""
        handler, failure = self._sync_handler_or_error("funnel_delete_orphan_files")
        if handler is None:
            return failure
        ids = self._row_ids(payload or {})
        if not ids:
            return {"success": False, "message": "未选择可删孤儿文件的行"}
        try:
            result = handler.funnel_delete_orphan_files(ids)
        except Exception as error:  # noqa: BLE001 - 删孤儿异常返回失败
            logger.warning(f"人工删孤儿文件失败：{error}")
            return {"success": False, "message": f"删孤儿文件失败：{error}"}
        results = (result or {}).get("results") or []
        ok_count = sum(1 for item in results if item.get("success"))
        fail_count = len(results) - ok_count
        if fail_count and not ok_count:
            return {
                "success": False,
                "message": "; ".join(
                    str(item.get("message") or "删孤儿文件失败")
                    for item in results
                ),
                "data": result,
            }
        return {
            "success": True,
            "message": (
                f"已删孤儿文件 {ok_count} 行"
                + (f"，失败 {fail_count} 行" if fail_count else "")
            ),
            "data": result,
        }
