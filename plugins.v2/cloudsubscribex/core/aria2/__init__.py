"""可选的内置 aria2 下载引擎配置与单例入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from app.log import logger

from .rpc import Aria2Error


@dataclass(frozen=True)
class Aria2Settings:
    """aria2 引擎运行参数；默认关闭，关闭时不触碰进程与数据目录。"""

    enabled: bool = False
    connections: int = 64
    max_concurrent: int = 2
    min_split_size: str = "1M"
    disk_cache: str = "32M"
    data_dir: Path = Path("/tmp/cloudsubscribex-aria2")


_SETTINGS = Aria2Settings()

#: 跟随跨盘缓存目录时的工作子目录名（放二进制、会话与日志）。
DATA_DIR_NAME = ".aria2"


def resolve_data_dir(download_path: Any, plugin_data_path: Any) -> Path:
    """aria2 工作目录：配置了跨盘缓存目录就跟随它，否则保持插件的原位置。

    下载本体一直由 RPC 的 ``dir`` / ``out`` 指向跨盘缓存目录，这里只决定
    aria2c 二进制、会话文件与日志落在哪个盘上。未配置缓存目录时返回插件
    数据目录本身（与历史行为一致，不迁移已有 bin/log）。
    """
    root = str(download_path or "").strip()
    if root:
        return Path(root) / DATA_DIR_NAME
    text = str(plugin_data_path or "").strip()
    return Path(text) if text else Path("/tmp/cloudsubscribex-aria2")


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
            kwargs.get("connections", _SETTINGS.connections), 64, 1, 96
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
        # 数据目录变化必须重启 daemon：--log/--dir/二进制位置都写在启动参数里，
        # 只换 _SETTINGS 会让旧进程继续往旧目录写。
        moved = Path(settings.data_dir) != Path(_SETTINGS.data_dir)
        _SETTINGS = settings
        if _ENGINE is not None and (not settings.enabled or moved):
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
    "Aria2Error",
    "Aria2FileDownloadService",
    "Aria2Settings",
    "aria2_settings",
    "configure_aria2",
    "shutdown_aria2",
]
