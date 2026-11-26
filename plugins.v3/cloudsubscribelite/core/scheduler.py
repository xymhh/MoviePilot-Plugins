"""插件内统一调度器：一个 BackgroundScheduler + 按 id 注册的任务表。

历史遗留：插件里曾有 7 个 BackgroundScheduler 各自启停（主任务、离线监控、
残留清理、STRM 全量、STRM 增量、历史状态维护、115 整理），stop_service 只停
了其中 5 个，历史状态维护与 115 整理两个调度器在停用/卸载后继续跑
（1.9.98 之前）。这里只保留一个调度器，任务按 id 注册，启停一次管全部。

MP 自身的定时服务（get_service 声明的榜单自动订阅、签到等）仍由 MP 的
调度器管理，不在这里登记。v1.27.0 起 STRM 全量/增量、115 整理定时、重试
索引维护这几个固定周期任务也交给了 MP 调度；这里只剩按需出现的动态任务
（离线监控：有待处理文件才存在、20 秒一轮）和启动时的一次性任务。

v1.27.0：``ensure_job`` 是本注册表唯一的登记处，登记时统一给任务函数套一层
观测：任务抛异常时先在插件日志记 ERROR（带任务 id 与堆栈），再原样抛回
APScheduler。此前异常只进 APScheduler 自己的日志通道——离线监控自 v1.19.0
起每轮崩、插件日志里一行都没有，就是这么漏掉的。
"""

from __future__ import annotations

import functools
import threading
import traceback
from typing import TYPE_CHECKING, Any, Callable, List, Optional

from app.core.config import settings
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler

if TYPE_CHECKING:
    from apscheduler.job import Job


def observed_job(job_id: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """包装任务函数：异常写进插件日志（ERROR + 堆栈）后原样抛出。

    只加日志、不改语义：返回值、异常类型与 APScheduler 的错误事件都不变。
    """

    @functools.wraps(func)
    def _run(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as error:
            logger.error(
                f"插件定时任务执行异常（{job_id}）：{error}\n{traceback.format_exc()}"
            )
            raise

    _run.__observed_job__ = job_id  # type: ignore[attr-defined]
    return _run


class SchedulerRegistry:
    """持有插件唯一的 BackgroundScheduler，任务按 id 增删改查。

    调度器按需创建、首次登记任务时启动；shutdown 后下一次登记会重建，
    因此插件重载（stop_service -> init_plugin）不需要额外处理。
    """

    def __init__(self, timezone: Optional[str] = None):
        self._timezone = timezone or settings.TZ
        self._lock = threading.Lock()
        self._scheduler: Optional[BackgroundScheduler] = None

    def _ensure_scheduler(self) -> BackgroundScheduler:
        """取到运行中的调度器，没有就新建并启动（调用方需持锁）。"""
        if self._scheduler is None:
            scheduler = BackgroundScheduler(timezone=self._timezone)
            scheduler.start()
            self._scheduler = scheduler
        return self._scheduler

    def ensure_job(
        self, job_id: str, func: Callable[..., Any], trigger: Any, **kwargs: Any
    ) -> bool:
        """登记（或替换）一个任务；同 id 已存在时原地替换触发器与参数。"""
        with self._lock:
            try:
                scheduler = self._ensure_scheduler()
                scheduler.add_job(
                    func=observed_job(job_id, func),
                    trigger=trigger,
                    id=job_id,
                    replace_existing=True,
                    **kwargs,
                )
                return True
            except Exception as error:  # noqa: BLE001 - 定时任务挂不上不影响主流程
                logger.warning(f"登记定时任务失败（{job_id}）：{error}")
                return False

    def remove_job(self, job_id: str) -> bool:
        """移除一个任务；不存在返回 False。"""
        with self._lock:
            scheduler = self._scheduler
            if scheduler is None:
                return False
            try:
                scheduler.remove_job(job_id)
                return True
            except Exception as error:  # noqa: BLE001 - 任务不存在也算移除成功
                logger.debug(f"移除定时任务失败（{job_id}）：{error}", exc_info=True)
                return False

    def reschedule_job(self, job_id: str, trigger: Any) -> bool:
        """只换触发器，任务本身与参数不动；不存在返回 False。"""
        with self._lock:
            scheduler = self._scheduler
            if scheduler is None:
                return False
            try:
                scheduler.reschedule_job(job_id, trigger=trigger)
                return True
            except Exception as error:  # noqa: BLE001 - 不存在也算失败但不抛
                logger.debug(f"重排定时任务失败（{job_id}）：{error}", exc_info=True)
                return False

    def get_job(self, job_id: str) -> Optional[Job]:
        """取一个已登记的任务；不存在返回 None。"""
        with self._lock:
            scheduler = self._scheduler
            if scheduler is None:
                return None
            try:
                return scheduler.get_job(job_id)
            except Exception as error:  # noqa: BLE001 - 查不到按不存在处理
                logger.debug(f"查询定时任务失败（{job_id}）：{error}", exc_info=True)
                return None

    def modify_job_next_run(self, job_id: str, run_time: Any) -> bool:
        """把任务的下次执行时间改到 run_time；不存在返回 False。"""
        with self._lock:
            scheduler = self._scheduler
            if scheduler is None:
                return False
            try:
                scheduler.modify_job(job_id, next_run_time=run_time)
                return True
            except Exception as error:  # noqa: BLE001 - 不存在也算失败但不抛
                logger.debug(
                    f"调整定时任务下次执行时间失败（{job_id}）：{error}",
                    exc_info=True,
                )
                return False

    @property
    def running(self) -> bool:
        """调度器是否在运行。"""
        with self._lock:
            scheduler = self._scheduler
            return bool(scheduler is not None and scheduler.running)

    def shutdown(self, wait: bool = False) -> None:
        """清掉全部任务并停掉调度器；可反复调用。"""
        with self._lock:
            scheduler = self._scheduler
            self._scheduler = None
            if scheduler is None:
                return
            try:
                scheduler.remove_all_jobs()
            except Exception as error:  # noqa: BLE001 - 清理失败仍要停调度器
                logger.debug(f"清空定时任务失败：{error}", exc_info=True)
            try:
                if scheduler.running:
                    scheduler.shutdown(wait=wait)
            except Exception as error:  # noqa: BLE001 - 停机失败不得阻塞卸载
                logger.debug(f"停止调度器失败：{error}", exc_info=True)
