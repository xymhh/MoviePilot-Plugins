"""CloudSubscribeX 统一任务执行上下文（TaskContext）。

借鉴 RQ 与 Celery 的 Job Context 架构，为每轮调度注入唯一追踪标识（run_id）、
取消令牌（Cancellation Token）、时间预算控制与结构化跳过采集器，
彻底终结散落的裸线程与无法优雅停止的问题。
"""

import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .exceptions import TaskAbortedError, TaskTimeoutError


class TaskContext:
    """单次同步/调度任务的生命周期上下文。"""

    def __init__(
        self,
        scope: str = "all",
        timeout_budget: float = 3600.0,
        run_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.scope = str(scope or "all")
        self.started_at = time.time()
        self.timeout_budget = float(timeout_budget or 3600.0)
        # 默认生成毫秒级时间戳批次号，如 "20261102153022"
        self.run_id = str(run_id or datetime.now().strftime("%Y%m%d%H%M%S"))
        self._cancel_event = cancel_event or threading.Event()
        self._progress_callback = progress_callback

        # 进度指标
        self.current_index = 0
        self.total_count = 0
        self.transferred_count = 0
        self.phase = "准备中"
        self._metadata: Dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        """检查任务是否已被请求停止。"""
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        """触发即时取消信号。"""
        self._cancel_event.set()

    def check_alive(self) -> None:
        """周期性健康探针：若被取消或超出时间预算，立即抛出受控异常快速退出。"""
        if self.is_cancelled:
            raise TaskAbortedError("任务已收到停止信号，快速退出")
        elapsed = time.time() - self.started_at
        if self.timeout_budget > 0 and elapsed > self.timeout_budget:
            raise TaskTimeoutError(elapsed=elapsed, budget=self.timeout_budget)

    def set_progress(
        self,
        current: Optional[int] = None,
        total: Optional[int] = None,
        transferred: Optional[int] = None,
        phase: Optional[str] = None,
    ) -> None:
        """原子更新当前执行进度并触发回调。"""
        with self._lock:
            if current is not None:
                self.current_index = int(current)
            if total is not None:
                self.total_count = int(total)
            if transferred is not None:
                self.transferred_count = int(transferred)
            if phase is not None:
                self.phase = str(phase)

        if self._progress_callback:
            try:
                self._progress_callback(self.snapshot())
            except Exception as err:
                # 进度回调为非关键旁路，记录调试日志并不阻断核心流程
                try:
                    from app.log import logger
                    logger.debug(f"任务上下文进度回调异常: {err}", exc_info=True)
                except Exception:
                    # 极端兜底（若 app.log 不可用）
                    return

    def snapshot(self) -> Dict[str, Any]:
        """导出当前任务执行快照。"""
        with self._lock:
            return {
                "run_id": self.run_id,
                "scope": self.scope,
                "started_at": self.started_at,
                "elapsed_seconds": round(time.time() - self.started_at, 2),
                "is_cancelled": self.is_cancelled,
                "current": self.current_index,
                "total": self.total_count,
                "transferred": self.transferred_count,
                "phase": self.phase,
            }

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._metadata[key] = value

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._metadata.get(key, default)
