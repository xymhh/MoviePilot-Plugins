"""同步作业重试退避策略（纯函数，便于单测）。"""

from __future__ import annotations

BASE_RETRY_DELAY_SECONDS = 60
MAX_RETRY_DELAY_SECONDS = 1800
DEAD_LETTER_PREFIX = "dead_letter: "


def backoff_delay(attempt: int) -> int:
    """第 ``attempt`` 次失败后的等待秒数：60 / 120 / 240 …，上限 1800。"""
    try:
        value = int(attempt or 0)
    except (TypeError, ValueError):
        value = 0
    value = max(1, value)
    return min(
        BASE_RETRY_DELAY_SECONDS * (2 ** (value - 1)),
        MAX_RETRY_DELAY_SECONDS,
    )


def is_dead_letter(
        state: object,
        last_error: object,
        attempt: object,
        max_attempts: object,
) -> bool:
    """死信口径：失败且带前缀，或旧数据里重试次数已耗尽的 retry_wait。"""
    job_state = str(state or "")
    text = str(last_error or "")
    if job_state == "failed" and text.startswith(DEAD_LETTER_PREFIX):
        return True
    try:
        used = int(attempt or 0)
        limit = int(max_attempts or 0)
    except (TypeError, ValueError):
        return False
    return job_state == "retry_wait" and limit > 0 and used >= limit


__all__ = [
    "BASE_RETRY_DELAY_SECONDS",
    "DEAD_LETTER_PREFIX",
    "MAX_RETRY_DELAY_SECONDS",
    "backoff_delay",
    "is_dead_letter",
]
