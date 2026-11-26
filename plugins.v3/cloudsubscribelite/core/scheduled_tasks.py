"""ScheduledTasksMixin：CloudSubscribeLite 入口拆出的混入类。

方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
"""

from __future__ import annotations

from app.core.config import settings
from app.log import logger
from typing import Any, Callable, Dict
from .services import CacheWatchService
import datetime
import pytz
import traceback


class ScheduledTasksMixin:
    """ScheduledTasksMixin：从 CloudSubscribeLite 入口拆出的逻辑。

    方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
    """

    def _run_scheduled_task(self, task: str) -> Any:
        """MP 调度的插件维护任务唯一入口（get_service 声明的 func）。

        v1.27.0：这几项原来挂在插件自建调度器上，异常只进 APScheduler 自己的
        日志，95% 卡那次监控崩了一整晚没人看见。改由 MP 调度后，任务抛出的
        异常由 MP 记 ERROR、发系统消息与 SystemError 事件（MP v3.2.6
        app/scheduler/execution.py ``_handle_job_error``），「定时服务」页也能看到；
        这里另在插件日志记一条带堆栈的 ERROR。同步服务缺方法这类插件内部错误
        （``internal_error``）也转成异常抛给 MP，不再静默返回。
        """
        runners: Dict[str, Callable[[], Any]] = {
            "retry_index_maintenance": self._run_retry_index_maintenance,
            # 1.32.0：缓存目录体积 + 磁盘水位巡检（6 小时一轮，越阈值发通知）。
            "cache_watch": self._run_cache_watch,
        }
        runner = runners.get(task)
        if runner is None:
            raise ValueError(f"未知的插件定时任务：{task}")
        try:
            result = runner()
        except Exception as error:
            logger.error(
                f"插件定时任务执行异常（{task}）：{error}\n{traceback.format_exc()}"
            )
            raise
        if isinstance(result, dict) and result.get("internal_error"):
            raise RuntimeError(str(result.get("message") or f"插件定时任务内部错误：{task}"))
        return result


    def _run_retry_index_maintenance(self) -> None:
        try:
            purged = self._get_data_store().repositories.retry_index.purge_old()
        except Exception as error:  # noqa: BLE001
            logger.warning(f"重试索引过期清理失败：{error}")
            return
        if purged:
            logger.info(f"重试索引过期清理完成：删除 {purged} 条")


    def _start_residue_cleanup(self) -> None:
        """启动时跑一次孤儿清理（崩溃 / 异常退出留下的临时文件）。

        1.10.0 起任务结束即删临时文件，不再需要定时清理：定时任务
        已移除，只保留启动时的一次性清理；手动清理走「立即清理残留」
        按钮。配置改动后再次调用本方法即可：首次挂任务时才跑一次，
        配置重载不重复跑。
        """
        registry = self._scheduler_registry()
        registry.remove_job("cloudsubscribe_residue_cleanup")
        if not self._residue_cleanup:
            return
        if registry.get_job("cloudsubscribe_residue_cleanup_startup") is None:
            # 首次挂任务时才跑一次启动清理；配置重载不重复跑。
            registry.ensure_job(
                "cloudsubscribe_residue_cleanup_startup",
                func=lambda: self.cleanup_transfer_residue("启动"),
                trigger="date",
                run_date=(
                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=1)
                ),
            )


    def _run_cache_watch(self) -> Any:
        """缓存目录与磁盘水位巡检（1.32.0，MP 每 6 小时调一次）。"""
        return self._get_component(CacheWatchService).check("定时")
