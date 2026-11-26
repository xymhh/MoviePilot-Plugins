"""平台总览与运行性能（v1.24.0 从 platform.py 拆出）。

PlatformIntegrationService 的总览侧 mixin：总览聚合、缓存清理、
运行性能汇总。方法体逐行原样搬移，行为不变。"""

import copy
import datetime
import time
from typing import Any, Dict
import pytz
from app.core.config import settings
from ...core import CloudDriveCapability


class PlatformOverviewMixin:
    def _cache_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        if self._search_handler:
            stats["search"] = self._search_handler.get_cache_stats()
        if self._cloud_drive and self._cloud_drive.supports(
                CloudDriveCapability.CACHE_MAINTENANCE
        ):
            cache_service = self._cloud_drive.require(
                CloudDriveCapability.CACHE_MAINTENANCE
            )
            stats[self._cloud_drive.key] = cache_service.get_cache_stats()
        return stats

    def get_platform_overview(
            self, recent_limit: int = 5, include_runtime: bool = True
    ) -> Dict[str, Any]:
        limit = max(0, min(int(recent_limit or 0), 20))
        cache_key = f"overview:{int(bool(include_runtime))}"
        cached = self._overview_cache.get(cache_key)
        if isinstance(cached, dict):
            result = copy.deepcopy(cached)
            result["recent_history"] = list(cached.get("recent_history") or [])[:limit]
            return result

        today = datetime.datetime.now(pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
        # Lite：本分支没有 history_overview 聚合（历史走 MP 自己的整理历史），
        # 缺这个方法会让 /overview 直接 500、上游 UI 打开即报错。
        # 这里按能力探测降级为空统计，界面显示 0 而不是崩。
        try:
            database_overview = self._get_data_store().history_overview(
                today, recent_limit=20
            )
        except AttributeError:
            database_overview = {}
        history_count = int(database_overview.get("total") or 0)
        transferred_today = int(database_overview.get("today") or 0)
        success = int(database_overview.get("success") or 0)
        failed = int(database_overview.get("failed") or 0)
        recent_history = list(database_overview.get("recent") or [])
        provider = self._cloud_drive
        overview = {
            "stats": [
                {"title": "总转存", "value": history_count, "color": "primary", "icon": "mdi-cloud-upload-outline"},
                {"title": "今日转存", "value": transferred_today, "color": "info", "icon": "mdi-calendar-today"},
                {"title": "成功", "value": success, "color": "success", "icon": "mdi-check-circle-outline"},
                {"title": "失败", "value": failed, "color": "error", "icon": "mdi-alert-circle-outline"},
            ],
            "history_count": history_count,
            "recent_history": recent_history,
            "cache": self._cache_stats(),
            "provider": {
                "key": provider.key if provider else "",
                "name": provider.name if provider else "未配置",
                "capabilities": sorted(
                    capability.value for capability in provider.capabilities
                ) if provider else [],
            },
        }
        if include_runtime:
            overview["runtime"] = self._runtime_snapshot()
        self._overview_cache[cache_key] = copy.deepcopy(overview)
        result = copy.deepcopy(overview)
        result["recent_history"] = recent_history[:limit]
        return result

    def clear_platform_cache(self) -> Dict[str, int]:
        """清理平台聚合与智能体候选缓存。"""
        counts = {
            "platform_overview": len(list(self._overview_cache.items())),
            "agent_resources": len(list(self._agent_resource_cache.items())),
            "link_selections": len(list(self._link_selection_cache.items())),
        }
        self._overview_cache.clear()
        self._agent_resource_cache.clear()
        self._link_selection_cache.clear()
        return counts

    def get_runtime_performance(self, include_tasks: bool = True) -> Dict[str, Any]:
        """汇总当前任务、搜索缓存和同步阶段性能指标。"""
        now = time.time()
        tasks = self._serialize_runtime_tasks() if include_tasks else []
        search_metrics = (
            self._search_handler.get_search_metrics()
            if self._search_handler else {}
        )
        sync_metrics = (
            self._sync_handler.get_sync_metrics()
            if self._sync_handler else {}
        )
        queue = {"pending": 0, "active": 0}
        if self._subscribe_search_queue_lock is not None:
            with self._subscribe_search_queue_lock:
                queue = {
                    "pending": len(self._subscribe_search_pending),
                    "active": len(self._subscribe_search_active),
                }
        external_calls = 0
        external_elapsed_ms = 0
        positive_hits = 0
        negative_hits = 0
        for metric in search_metrics.values():
            external_calls += int(metric.get("external_calls") or 0)
            external_elapsed_ms += int(
                metric.get("external_elapsed_ms") or 0
            )
            positive_hits += int(metric.get("positive_cache_hits") or 0)
            negative_hits += int(metric.get("negative_cache_hits") or 0)
        run_elapsed_ms = (
            int(max(0.0, now - self._sync_run_started_at) * 1000)
            if self._sync_running and self._sync_run_started_at else 0
        )
        return {
            "success": True,
            "message": "运行性能数据已汇总",
            "runtime": {
                "status": self._sync_status,
                "task": self._sync_task_text,
                "progress": self._sync_progress,
                "running": self._sync_running,
                "elapsed_ms": run_elapsed_ms,
                "last_elapsed_ms": int(self._sync_last_elapsed_ms or 0),
                "last_finished_at": float(self._sync_last_finished_at or 0),
                "transferred": int(self._sync_context.get("transferred") or 0),
                "configured_concurrency": int(self._subscription_concurrency or 1),
            },
            "queue": queue,
            "tasks": tasks,
            "search": {
                "summary": {
                    "external_calls": external_calls,
                    "external_elapsed_ms": external_elapsed_ms,
                    "positive_cache_hits": positive_hits,
                    "negative_cache_hits": negative_hits,
                },
                "sources": search_metrics,
                "cache": self._search_handler.get_cache_stats()
                if self._search_handler else {},
            },
            "sync_stages": sync_metrics,
            "next_step": (
                "请用中文说明当前任务是否正常推进、最耗时的搜索源或同步阶段、"
                "缓存命中效果，以及是否需要调整并发或缓存配置。"
            ),
        }

    def api_platform_overview(self, include_runtime: bool = True) -> Dict[str, Any]:
        return {
            "success": True,
            "data": self.get_platform_overview(
                6, include_runtime=bool(include_runtime)
            ),
        }

    def start_platform_sync(self) -> Dict[str, Any]:
        return self.api_vue_start_sync()
