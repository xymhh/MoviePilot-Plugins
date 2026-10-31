"""可选的内置 aria2 下载引擎配置与单例入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from app.log import logger


@dataclass(frozen=True)
class Aria2Settings:
    """aria2 引擎运行参数；默认关闭，关闭时不触碰进程与数据目录。"""

    enabled: bool = False
    connections: int = 16
    max_concurrent: int = 2
    min_split_size: str = "1M"
    disk_cache: str = "32M"
    data_dir: Path = Path("/tmp/cloudsubscribex-aria2")


_SETTINGS = Aria2Settings()
_ENGINE: Optional[Any] = None
_LOCK = Lock()


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def _as_text(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def configure_aria2(**kwargs) -> None:
    """覆盖模块级配置；关闭时顺带停掉已启动的 daemon。"""
    global _SETTINGS, _ENGINE

    settings = Aria2Settings(
        enabled=bool(kwargs.get("enabled", _SETTINGS.enabled)),
        connections=_as_int(
            kwargs.get("connections", _SETTINGS.connections), 16, 1, 16
        ),
        max_concurrent=_as_int(
            kwargs.get("max_concurrent", _SETTINGS.max_concurrent), 2, 1, 4
        ),
        min_split_size=_as_text(
            kwargs.get("min_split_size", _SETTINGS.min_split_size), "1M"
        ),
        disk_cache=_as_text(
            kwargs.get("disk_cache", _SETTINGS.disk_cache), "32M"
        ),
        data_dir=Path(
            kwargs.get("data_dir", _SETTINGS.data_dir)
            or "/tmp/cloudsubscribex-aria2"
        ),
    )
    stale = None
    with _LOCK:
        _SETTINGS = settings
        if not settings.enabled and _ENGINE is not None:
            stale = _ENGINE
            _ENGINE = None
    if stale is not None:
        try:
            stale.stop()
        except Exception as error:  # noqa: BLE001 - 关停失败不影响配置生效
            logger.warning(f"关闭 aria2 下载引擎失败：{error}")


def aria2_settings() -> Aria2Settings:
    return _SETTINGS


def get_engine():
    """返回模块级 daemon 单例（惰性创建，不在此启动进程）。"""
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            from .engine import Aria2Engine

            _ENGINE = Aria2Engine(_SETTINGS)
        return _ENGINE


def shutdown_aria2() -> None:
    """停掉 aria2 daemon（幂等）。"""
    global _ENGINE
    with _LOCK:
        engine = _ENGINE
        _ENGINE = None
    if engine is not None:
        try:
            engine.stop()
        except Exception as error:  # noqa: BLE001
            logger.warning(f"关闭 aria2 下载引擎失败：{error}")


from .service import Aria2FileDownloadService

__all__ = [
    "Aria2FileDownloadService",
    "Aria2Settings",
    "aria2_settings",
    "configure_aria2",
    "shutdown_aria2",
]
