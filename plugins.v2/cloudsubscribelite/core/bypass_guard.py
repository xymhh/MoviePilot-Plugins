"""旁路能力熔断守护。

主链路只有三段：搜索 → 转存 → 落库。其余都是旁路：签到、STRM 生成、
媒体库刷新、消息通知、aria2 加速。旁路允许失败，但必须满足三条约束：

1. 失败只降级该能力，不影响主链路的任何返回值；
2. 连续失败到阈值后进入冷却，冷却期内直接跳过，不再每轮重复踩坑；
3. 状态可被 Doctor 读到（谁在冷却、上次什么错），不用翻容器日志。

设计见 docs/plan/2026-11-02-旁路解耦-调研与架构.md。
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from app.log import logger

#: 旁路能力的中文名（Doctor 与日志共用）
BYPASS_LABELS: Dict[str, str] = {
    "strm": "STRM 生成",
    "media_server": "媒体库刷新",
    "notify": "消息通知",
    "checkin": "签到",
    "aria2": "aria2 加速",
    "organize": "网盘整理",
}

#: 不吞的异常：用户主动停止 / 进程退出必须立刻生效
_PASSTHROUGH = (InterruptedError, KeyboardInterrupt, SystemExit)


class _BypassState:
    """单个旁路能力的运行状态。"""

    __slots__ = (
        "failures", "trips", "cooldown_until",
        "last_error", "last_error_at", "last_ok_at", "skipped",
    )

    def __init__(self) -> None:
        self.failures = 0
        self.trips = 0
        self.cooldown_until = 0.0
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_ok_at = 0.0
        self.skipped = 0


class BypassGuard:
    """能力级熔断表：冷却期内跳过，失败累计到阈值才熔断。"""

    #: 连续失败多少次后熔断
    THRESHOLD = 3
    #: 首次熔断冷却秒数，之后每次重新熔断翻倍
    BASE_COOLDOWN = 600.0
    #: 冷却上限
    MAX_COOLDOWN = 3600.0

    def __init__(
            self,
            threshold: Optional[int] = None,
            base_cooldown: Optional[float] = None,
            max_cooldown: Optional[float] = None,
            clock: Callable[[], float] = time.time,
    ) -> None:
        self._threshold = max(1, int(threshold or self.THRESHOLD))
        self._base_cooldown = float(base_cooldown or self.BASE_COOLDOWN)
        self._max_cooldown = float(max_cooldown or self.MAX_COOLDOWN)
        self._clock = clock
        self._states: Dict[str, _BypassState] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 状态

    def _state(self, key: str) -> _BypassState:
        state = self._states.get(key)
        if state is None:
            state = _BypassState()
            self._states[key] = state
        return state

    @staticmethod
    def _label(key: str) -> str:
        return BYPASS_LABELS.get(key, key)

    def allow(self, key: str) -> bool:
        """当前是否放行该能力。冷却中返回 False（只在首次跳过时记一条日志）。"""
        with self._lock:
            state = self._state(key)
            now = self._clock()
            if state.cooldown_until <= now:
                return True
            state.skipped += 1
            if state.skipped == 1:
                remain = int(state.cooldown_until - now)
                logger.warning(
                    f"旁路「{self._label(key)}」冷却中，剩余 {remain} 秒，"
                    f"本轮跳过；上次失败：{state.last_error or '未知'}"
                )
            return False

    def call(
            self,
            key: str,
            func: Callable[..., Any],
            *args: Any,
            fallback: Any = None,
            **kwargs: Any,
    ) -> Any:
        """统一包裹旁路调用：异常与冷却都退化为 fallback，绝不外抛。"""
        if not self.allow(key):
            return fallback
        try:
            result = func(*args, **kwargs)
        except _PASSTHROUGH:
            raise
        except Exception as error:  # noqa: BLE001 - 旁路失败必须被吃掉
            self.record_failure(key, error)
            return fallback
        self.record_success(key)
        return result

    def record_success(self, key: str) -> None:
        with self._lock:
            state = self._state(key)
            state.failures = 0
            state.trips = 0
            state.cooldown_until = 0.0
            state.last_ok_at = self._clock()
            state.skipped = 0

    def record_failure(self, key: str, error: Any) -> None:
        with self._lock:
            state = self._state(key)
            now = self._clock()
            state.failures += 1
            state.last_error = f"{type(error).__name__}: {error}"
            state.last_error_at = now
            if state.failures < self._threshold:
                logger.warning(
                    f"旁路「{self._label(key)}」失败 "
                    f"{state.failures}/{self._threshold}：{state.last_error}"
                )
                return
            state.trips += 1
            cooldown = min(
                self._base_cooldown * (2 ** (state.trips - 1)),
                self._max_cooldown,
            )
            state.cooldown_until = now + cooldown
            state.failures = 0
            state.skipped = 0
            logger.warning(
                f"旁路「{self._label(key)}」连续失败达上限，熔断 {int(cooldown)} 秒："
                f"{state.last_error}"
            )

    def note(self, key: str, error: Any) -> None:
        """调用方已自行降级时只记账（如 aria2 回落内置下载器）。"""
        self.record_failure(key, error)

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._states.clear()
            else:
                self._states.pop(key, None)

    def snapshot(self) -> List[Dict[str, Any]]:
        """供 Doctor 与排障使用的能力状态快照。"""
        now = self._clock()
        rows: List[Dict[str, Any]] = []
        with self._lock:
            for key in sorted(self._states):
                state = self._states[key]
                cooling = state.cooldown_until > now
                rows.append({
                    "key": key,
                    "name": self._label(key),
                    "state": "cooling" if cooling else "ok",
                    "failures": state.failures,
                    "trips": state.trips,
                    "remaining_seconds": (
                        int(state.cooldown_until - now) if cooling else 0
                    ),
                    "skipped": state.skipped,
                    "last_error": state.last_error,
                    "last_error_at": state.last_error_at,
                    "last_ok_at": state.last_ok_at,
                })
        return rows


#: 进程级单例：插件所有组件共用一张熔断表
bypass_guard = BypassGuard()
