"""深度删除事件去重。

神医/Emby 一次整剧删除会连发 4 条 webhook（library.deleted ×2 + deep.delete ×2），
每条都带同一个 Item Path。不做去重就会把同一个网盘目录删 3 次、发 3 条通知
（2026-10-27 16:40 线上实测：16:40:51 / :53 / :54 三条一模一样的「删除完成」）。

窗口内对同一目标只认第一条事件，其余直接忽略（既不重复删，也不重复通知）。
"""

import threading
import time
from typing import Callable, Dict, Iterable, List, Tuple


class DeepDeleteDeduplicator:
    """按目标路径做时间窗占位，窗口内同一目标只放行一次。"""

    def __init__(
        self,
        window_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._window = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._claimed: Dict[str, float] = {}

    @staticmethod
    def _key(path: object) -> str:
        return str(path or "").strip().rstrip("/")

    def claim(self, paths: Iterable[str]) -> Tuple[List[str], List[str]]:
        """占位并返回 ``(本次放行的路径, 窗口内已处理过的路径)``。"""
        now = self._clock()
        fresh: List[str] = []
        duplicate: List[str] = []
        with self._lock:
            self._prune(now)
            for path in paths or []:
                key = self._key(path)
                if not key:
                    continue
                if key in self._claimed:
                    duplicate.append(key)
                    continue
                self._claimed[key] = now
                fresh.append(key)
        return fresh, duplicate

    def release(self, paths: Iterable[str]) -> None:
        """撤销占位：处理彻底失败时让后续事件还能重试。"""
        with self._lock:
            for path in paths or []:
                self._claimed.pop(self._key(path), None)

    def pending(self) -> int:
        """当前仍在窗口内的目标数量（测试/诊断用）。"""
        with self._lock:
            self._prune(self._clock())
            return len(self._claimed)

    def _prune(self, now: float) -> None:
        expired = [key for key, ts in self._claimed.items() if now - ts >= self._window]
        for key in expired:
            self._claimed.pop(key, None)
