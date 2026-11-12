"""跨盘真实上传 115 的每日字节额度（按 settings.TZ 自然日）。

只统计**真实上传**字节：秒传命中 / 复用 / abort / 失败一律 0，不占额度。
持久化走插件私有 runtime 表，跨插件重载、容器重启保留；读写异常 fail-open
（不阻断上传），只记 WARN。
"""
import logging
import threading
from datetime import datetime
from typing import Any, Optional, Tuple

import pytz

NAMESPACE = "cross_upload_quota"
GB = 1024 * 1024 * 1024
MAX_LIMIT_GB = 900


def _tz_name() -> str:
    """取宿主时区；拿不到或不是合法时区名时回落 Asia/Shanghai。"""
    try:
        from app.core.config import settings
        value = getattr(settings, "TZ", None)
        if isinstance(value, str) and value:
            try:
                pytz.timezone(value)
                return value
            except Exception:
                logging.getLogger(__name__).debug(
                    "宿主 TZ 不是合法时区，回落 Asia/Shanghai", exc_info=True
                )
    except Exception:
        logging.getLogger(__name__).debug(
            "读取宿主 TZ 失败，回落 Asia/Shanghai", exc_info=True
        )
    return "Asia/Shanghai"


def _warn(message: str) -> None:
    try:
        from app.log import logger
        logger.warning(message)
    except Exception:
        logging.getLogger(__name__).debug(
            "宿主 logger 不可用，额度告警已降级", exc_info=True
        )


def clamp_limit_gb(value: Any) -> int:
    """把配置值收敛到 1..900；空 / 非数字 / 负数 / 超界一律回合法值。"""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return MAX_LIMIT_GB
    if limit < 1:
        return 1
    return min(MAX_LIMIT_GB, limit)


class UploadQuota:
    """按天累计真实上传字节；任务开始前预检，成功后原子累加。"""

    def __init__(self, owner: Any = None, limit_gb: Any = MAX_LIMIT_GB,
                 runtime: Any = None):
        self._owner = owner
        self.limit_gb = clamp_limit_gb(limit_gb)
        self._runtime_override = runtime
        self._lock = threading.Lock()

    @property
    def limit_bytes(self) -> int:
        return self.limit_gb * GB

    @staticmethod
    def date_key(now: Optional[datetime] = None) -> str:
        tz = pytz.timezone(_tz_name())
        current = now or datetime.now(tz)
        if current.tzinfo is None:
            current = tz.localize(current)
        return current.astimezone(tz).strftime("%Y-%m-%d")

    def _runtime(self):
        if self._runtime_override is not None:
            return self._runtime_override
        return self._owner._get_data_store().repositories.runtime

    def _read_used(self, date_key: str = "") -> Optional[int]:
        """返回 None 表示读取失败，调用方应 fail-open 而不是当成 0 去判超限。"""
        key = date_key or self.date_key()
        try:
            payload = self._runtime().load(NAMESPACE, key, None) or {}
            return max(0, int(payload.get("bytes") or 0))
        except Exception as error:
            _warn(f"每日上传额度读取失败：{error}")
            return None

    def used_bytes(self, date_key: str = "") -> int:
        value = self._read_used(date_key)
        return 0 if value is None else value

    def remaining_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes())

    def can_start(self, size_bytes: int) -> Tuple[bool, str]:
        """任务开始前按文件大小预检；额度读写异常一律放行。"""
        size = max(0, int(size_bytes or 0))
        try:
            used = self._read_used()
            if used is None:
                _warn("每日上传额度读取失败，本次预检放行")
                return True, ""
            if used + size > self.limit_bytes:
                return False, (
                    f"每日上传额度已满（{used / GB:.1f}/{self.limit_gb} GB），"
                    f"本次需 {size / GB:.1f} GB，等次日自然日恢复"
                )
            return True, ""
        except Exception as error:
            _warn(f"每日上传额度预检失败，本次放行：{error}")
            return True, ""

    def add(self, size_bytes: int) -> None:
        """上传成功后原子累加（读改写加锁）；失败 fail-open。"""
        size = max(0, int(size_bytes or 0))
        if size <= 0:
            return
        key = self.date_key()
        with self._lock:
            try:
                runtime = self._runtime()
                payload = runtime.load(NAMESPACE, key, None) or {}
                used = max(0, int(payload.get("bytes") or 0)) + size
                runtime.save(NAMESPACE, key, {
                    "bytes": used,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
            except Exception as error:
                _warn(f"每日上传额度累加失败，不影响本次上传：{error}")
