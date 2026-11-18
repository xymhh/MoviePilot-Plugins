"""115 上传重试策略表 + 429 退避 + 上传熔断器 + 数据面 pacing。

Phase 3b（v1.18.0）上传扁平化：三套分片实现（oss2 自管分片、p115oss
回落、远程 Range 直传）原来各写各的重试——init 3 次固定 2/4 秒、每片
2 次不睡觉、整文件 3 次固定 2/4 秒——这里收成一张表，三个阶段共用
同一套「次数 + 指数退避 + 抖动」语义。

纯标准库，不依赖任何插件模块（drive 层与 core 层共用，避免循环引用）。

退避约定（attempt 从 1 计，即第 1 次失败后等待的时长）：
``delay = min(base * factor ** (attempt - 1), 60)``，再叠加 ±jitter
的均匀抖动。429/限流时不用阶段的 base，改用 ``UPLOAD_429_BACKOFF``
的 5 秒起步——被限流时多等一会儿，比原地硬顶更划算。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Dict

#: 阶段 -> {attempts: 最多尝试次数, base_delay: 首次退避秒数,
#: factor: 指数底数, jitter: 抖动比例（±）}。
#: 次数与基数沿用 v1.17.0 各实现的既有值，只把散落的常量收拢到一张表：
#: init 3 次 / 2 秒起、每片 2 次 / 1 秒起、整文件 3 次 / 2 秒起、
#: 源盘 Range 读 3 次 / 0.5 秒起。
UPLOAD_RETRY_POLICY: Dict[str, Dict[str, float]] = {
    "init": {"attempts": 3, "base_delay": 2.0, "factor": 2.0, "jitter": 0.2},
    "part": {"attempts": 2, "base_delay": 1.0, "factor": 2.0, "jitter": 0.2},
    "file": {"attempts": 3, "base_delay": 2.0, "factor": 2.0, "jitter": 0.2},
    "range": {"attempts": 3, "base_delay": 0.5, "factor": 2.0, "jitter": 0.2},
}

#: 429/限流退避：5 秒起步、指数翻倍、±20% 抖动、上限 60 秒。
UPLOAD_429_BACKOFF: Dict[str, float] = {
    "base_delay": 5.0,
    "factor": 2.0,
    "jitter": 0.2,
    "max_delay": 60.0,
}

#: 单次退避上限（秒），所有阶段共用。
UPLOAD_RETRY_MAX_DELAY_SECONDS = 60.0

#: 数据面 PUT 起步 pacing 间隔（秒）。注意：这不是控制面 1.5 秒的
#: 翻版——控制面每次调用都是轻量 API，1.5 秒能防刷；数据面每个 PUT
#: 是 10–16MB 的分片，1.5GB 文件上百个分片若每片等 1.5 秒会被拖垮。
#: 0.1 秒只做突发平滑：起步速率上限 10 次/秒 ≈ 100MB/s（10MB/片），
#: 远高于真实吞吐，分片本身耗时数秒时这 0.1 秒基本不产生等待；
#: 真正的刹车是分片并发度上限 + 下面的上传熔断器。
UPLOAD_DATA_PUT_MIN_INTERVAL = 0.1

#: 上传熔断器默认：连续失败 5 次后打开。
UPLOAD_BREAKER_DEFAULT_THRESHOLD = 5
#: 上传熔断器默认冷却（秒）：打开后 5 分钟内新上传直接跳过。
UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS = 300.0


def retry_delay(
        stage: str,
        attempt: int,
        *,
        rate_limited: bool = False,
        rand: Callable[[float, float], float] = random.uniform,
) -> float:
    """算一次重试前要等的秒数。

    ``stage`` 是 ``UPLOAD_RETRY_POLICY`` 的键（未知键按 part 计）；
    ``attempt`` 从 1 计（第 1 次失败后等待 ``base`` 秒）；
    ``rate_limited`` 为真时改用 429 退避（5 秒起步）。
    ``rand`` 供测试注入确定性随机，生产默认 ``random.uniform``。
    """
    entry = UPLOAD_RETRY_POLICY.get(stage) or UPLOAD_RETRY_POLICY["part"]
    if rate_limited:
        base = UPLOAD_429_BACKOFF["base_delay"]
        factor = UPLOAD_429_BACKOFF["factor"]
        jitter = UPLOAD_429_BACKOFF["jitter"]
    else:
        base = entry["base_delay"]
        factor = entry["factor"]
        jitter = entry["jitter"]
    delay = base * (factor ** max(0, int(attempt) - 1))
    delay = min(delay, UPLOAD_RETRY_MAX_DELAY_SECONDS)
    if jitter > 0:
        delay = delay + rand(-jitter * delay, jitter * delay)
    return max(0.0, delay)


class UploadCircuitBreaker:
    """115 上传熔断器：连续失败 N 次后打开，冷却期内新上传直接跳过，
    只放行秒传探针。

    与搜索侧 ``SearchCircuitBreaker`` 同一状态机
    （closed → open → half_open → closed），但只管一条上传通道
    （不分 key）：连续失败计数只增不减，成功一次即清零。
    半开时放行下一次真实上传当探针：成功则关闭，失败则重新打开。

    线程安全；配置（阈值/冷却）每次使用时由调用方从插件配置重读，
    改配置不用重启。
    """

    def __init__(
            self,
            failure_threshold: int = UPLOAD_BREAKER_DEFAULT_THRESHOLD,
            cooldown_seconds: float = UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown_seconds = max(5.0, float(cooldown_seconds))
        self._failures = 0
        self._open_until = 0.0
        self._half_open = False

    def configure(
            self,
            failure_threshold: int | None = None,
            cooldown_seconds: float | None = None,
    ) -> None:
        """热更新阈值/冷却（改配置后立即生效，不重置已有计数）。"""
        with self._lock:
            if failure_threshold is not None:
                self._failure_threshold = max(1, int(failure_threshold))
            if cooldown_seconds is not None:
                self._cooldown_seconds = max(5.0, float(cooldown_seconds))

    def state(self) -> str:
        """当前状态：closed / open / half_open。"""
        with self._lock:
            if self._open_until <= 0:
                return "closed"
            if time.monotonic() >= self._open_until:
                return "half_open"
            return "open"

    def can_upload(self) -> bool:
        """是否允许开始一次真实上传。冷却到期时自动转 half_open。"""
        with self._lock:
            if self._open_until <= 0:
                return True
            if time.monotonic() >= self._open_until:
                self._half_open = True
                return True
            return False

    def record_success(self) -> None:
        """一次上传成功：清零失败计数、关闭熔断器。"""
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._half_open = False

    def record_failure(self) -> None:
        """一次上传终态失败：计数 +1，达到阈值则打开冷却。"""
        with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._open_until = time.monotonic() + self._cooldown_seconds
                self._half_open = False

    def cooldown_remaining(self) -> float:
        """熔断剩余冷却秒数（未打开时为 0）。"""
        with self._lock:
            if self._open_until <= 0:
                return 0.0
            return max(0.0, self._open_until - time.monotonic())
