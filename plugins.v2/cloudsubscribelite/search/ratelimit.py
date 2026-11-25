"""解锁节流：HDHaven 与 HDHive Web 共用的窗口限流器。

两家原来各写一份 ``_UnlockLimiter``，骨架相同（60 秒窗口、按 key 的历史
与锁、等待循环），但策略不同；直接合并不了——必须把策略差异参数化，
否则一方的行为会被另一方改掉。差异点：

* ``_next_wait_seconds``：hdhaven 是纯窗口计数（窗口内未满立即放行，满了
  等最早一条滑出窗口）；hdhive 是相邻两次强制最小间隔（60/per_window +
  1–4 秒随机抖动），且每个 key 首次调用另有 3–8 秒首解锁延迟。
* ``wait_for_slot``：hdhaven 只查风控冷却；hdhive 先查停止标志再查冷却。
* 钳制上限：hdhaven 5，hdhive 3；空 key 兜底：hdhaven ``"default"``，
  hdhive ``""``；错误类型与文案各是各的。

本类把这些差异收成构造参数，两处调用点显式传自己的策略，行为与合并前
逐字相同。状态字典按 ``namespace`` 分区（hdhaven / hdhive），两家即使
``session_key`` 撞名也不会互相限流——与合并前“各是各的类属性”隔离性相同。
"""

import random
import threading
import time
from collections import deque
from typing import Callable, Dict, Optional, Tuple

from app.log import logger


class UnlockLimiter:
    """资源解锁节流控制（HDHaven 与 HDHive Web 共用，策略由构造参数定）。"""

    WINDOW_SECONDS = 60.0
    _STATE_LOCK = threading.RLock()
    _HISTORIES: Dict[str, deque] = {}
    _LOCKS: Dict[str, threading.RLock] = {}
    _READY_ATS: Dict[str, float] = {}

    def __init__(
            self,
            session_key: str,
            per_window: int = 2,
            *,
            namespace: str = "",
            max_per_window: int = 5,
            key_default: str = "default",
            min_interval: bool = False,
            first_delay: Tuple[float, float] = (0.0, 0.0),
            error_factory: Callable[..., Exception],
            cooldown_message: str,
            stopped_message: Optional[str] = None,
            log_prefix: str = "",
    ):
        key = str(session_key or key_default)
        self._key = f"{namespace}:{key}" if namespace else key
        self._per_window = max(1, min(int(per_window or 2), max_per_window))
        self._min_interval = bool(min_interval)
        self._first_delay = (float(first_delay[0]), float(first_delay[1]))
        self._error_factory = error_factory
        self._cooldown_message = cooldown_message
        self._stopped_message = stopped_message
        self._log_prefix = log_prefix

    def lock(self) -> threading.RLock:
        with self._STATE_LOCK:
            return self._LOCKS.setdefault(self._key, threading.RLock())

    @property
    def per_window(self) -> int:
        return self._per_window

    def _evict(self, history: deque, now: float) -> None:
        while history and (now - history[0]) >= self.WINDOW_SECONDS:
            history.popleft()

    def _next_wait_seconds(self) -> float:
        interval = 0.0
        if self._min_interval:
            interval = self.WINDOW_SECONDS / self._per_window + random.uniform(1.0, 4.0)
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._key, deque())
            now = time.monotonic()
            self._evict(history, now)
            wait_seconds = 0.0
            if self._first_delay != (0.0, 0.0):
                ready_at = self._READY_ATS.setdefault(
                    self._key, now + random.uniform(*self._first_delay)
                )
                wait_seconds = max(ready_at - now, 0.0)
            if history and self._min_interval:
                wait_seconds = max(wait_seconds, interval - (now - history[-1]))
            if len(history) >= self._per_window:
                wait_seconds = max(
                    wait_seconds, self.WINDOW_SECONDS - (now - history[0])
                )
            return wait_seconds

    def record(self) -> None:
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._key, deque())
            now = time.monotonic()
            self._evict(history, now)
            history.append(now)

    def wait_for_slot(
            self,
            stop_requested: Optional[Callable[[], bool]] = None,
            cooldown_remaining: Optional[Callable[[], float]] = None,
    ) -> None:
        def ensure_runnable():
            if stop_requested is not None and stop_requested():
                raise self._error_factory(
                    self._stopped_message or "", code="stopped"
                )
            if cooldown_remaining:
                remaining = cooldown_remaining()
                if remaining > 0:
                    raise self._error_factory(
                        f"{self._cooldown_message}（剩余 {int(remaining + 0.999)} 秒）",
                        code="rate_limited",
                    )

        while True:
            ensure_runnable()
            wait_seconds = self._next_wait_seconds()
            if wait_seconds <= 0:
                return
            logger.debug(f"{self._log_prefix} {wait_seconds:.1f} 秒")
            deadline = time.monotonic() + wait_seconds
            while deadline > time.monotonic():
                ensure_runnable()
                time.sleep(min(deadline - time.monotonic(), 0.25))
