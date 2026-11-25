"""缓存目录与磁盘水位巡检（1.32.0）。

背景：插件此前对磁盘完全无感知——2026-11-24 德国 VPS 上 38GB 失败残留
堆在缓存目录里，直到磁盘用到 91% 才被人发现，而盘满会连带 MP 的数据库、
Emby 扫描、STRM 生成全线故障（只能靠日志里的 "No space left on device"
事后翻译）。这里每 6 小时量一次缓存目录体积与所在分区剩余，
越过阈值发一条通知（带处理建议），回落到阈值内再发一条恢复通知。

阈值刻意做成模块常量而不是配置项：新配置键在配置页没有入口，
用户找不到反而更糟；真有需求再连同 UI 一起加。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.log import logger
from app.schemas.types import NotificationType

from ..delegation import OwnerDelegator

#: 缓存目录体积告警阈值：20GB。
CACHE_DIR_ALERT_BYTES = 20 * 1024 ** 3
#: 分区剩余比例告警阈值：15%。
DISK_FREE_ALERT_RATIO = 0.15
#: 同一状态多少秒内不重复通知（跨状态切换立即通知）。
ALERT_COOLDOWN_SECONDS = 12 * 3600
#: 巡检状态落盘文件名（放插件数据目录，不是缓存目录）。
STATE_FILENAME = "cache_watch_state.json"

LEVEL_OK = "ok"
LEVEL_WARN = "warn"


def _human_bytes(value: Any) -> str:
    """把字节数转成人类可读；非法值按 0 处理。"""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        size = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


class CacheWatchService(OwnerDelegator):
    """量一次缓存目录与磁盘水位，越阈值发通知（带 12 小时去重）。"""

    def check(self, reason: str = "定时") -> Dict[str, Any]:
        """执行一次巡检并返回明细（不抛异常，量不到按跳过处理）。"""
        cache_dir = self._cache_dir()
        cache_bytes = self._cache_usage(cache_dir)
        free_bytes, total_bytes = self._disk_usage(cache_dir)
        ratio: Optional[float] = None
        if free_bytes is not None and total_bytes:
            ratio = max(0.0, min(1.0, float(free_bytes) / float(total_bytes)))
        level = self._level(cache_bytes, ratio)
        result: Dict[str, Any] = {
            "reason": reason,
            "cache_dir": cache_dir,
            "cache_bytes": cache_bytes,
            "disk_free_bytes": free_bytes,
            "disk_total_bytes": total_bytes,
            "disk_free_ratio": ratio,
            "level": level,
            "notified": False,
        }
        previous = self._load_state()
        if self._should_notify(previous, level):
            result["notified"] = self._notify(level, result)
            self._save_state(level, cache_bytes, ratio)
        logger.info(
            f"缓存巡检（{reason}）：目录 {_human_bytes(cache_bytes)}"
            f"，分区剩余 {_human_bytes(free_bytes)}"
            + (f"（{ratio * 100:.0f}%）" if ratio is not None else "")
            + f"，状态 {level}"
            + ("，已通知" if result["notified"] else "")
        )
        return result

    # -- 采样 --------------------------------------------------------

    def _cache_dir(self) -> str:
        """缓存目录：优先跨盘下载目录，回退插件数据目录。"""
        text = str(getattr(self, "_cross_transfer_download_path", "") or "").strip()
        if text:
            return text
        getter = getattr(self, "get_data_path", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception as error:  # noqa: BLE001 - 取不到按空目录
                logger.debug(f"读取插件数据目录失败：{error}", exc_info=True)
        return ""

    def _cache_usage(self, cache_dir: str) -> int:
        """缓存目录顶层文件总量（不递归：跨盘残留都落在顶层）。"""
        if not cache_dir:
            return 0
        base = Path(cache_dir)
        if not base.is_dir():
            return 0
        total = 0
        try:
            entries = list(base.iterdir())
        except OSError as error:
            logger.debug(f"读取缓存目录失败：{error}", exc_info=True)
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                total += int(entry.stat().st_size or 0)
            except OSError:
                continue
        return total

    def _disk_usage(self, cache_dir: str) -> tuple:
        """返回 (剩余字节, 总字节)；量不到返回 (None, 0)。"""
        if not cache_dir:
            return None, 0
        try:
            usage = shutil.disk_usage(cache_dir)
        except OSError as error:
            logger.debug(f"读取磁盘水位失败（{cache_dir}）：{error}", exc_info=True)
            return None, 0
        return int(getattr(usage, "free", 0) or 0), int(getattr(usage, "total", 0) or 0)

    @staticmethod
    def _level(cache_bytes: int, ratio: Optional[float]) -> str:
        """判级：缓存目录超阈值，或分区剩余低于阈值 → warn。"""
        if int(cache_bytes or 0) >= CACHE_DIR_ALERT_BYTES:
            return LEVEL_WARN
        if ratio is not None and ratio < DISK_FREE_ALERT_RATIO:
            return LEVEL_WARN
        return LEVEL_OK

    # -- 去重与通知 --------------------------------------------------

    def _state_path(self) -> Optional[Path]:
        getter = getattr(self, "get_data_path", None)
        if not callable(getter):
            return None
        try:
            data_path = str(getter() or "")
        except Exception as error:  # noqa: BLE001 - 取不到数据目录不写状态
            logger.debug(f"读取插件数据目录失败：{error}", exc_info=True)
            return None
        if not data_path:
            return None
        return Path(data_path) / STATE_FILENAME

    def _load_state(self) -> Dict[str, Any]:
        path = self._state_path()
        if path is None:
            return {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception as error:  # noqa: BLE001 - 状态坏了按首次巡检
            logger.debug(f"读取缓存巡检状态失败：{error}", exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(
            self, level: str, cache_bytes: int, ratio: Optional[float]
    ) -> None:
        path = self._state_path()
        if path is None:
            return
        payload = {
            "level": level,
            "notified_at": int(time.time()),
            "cache_bytes": int(cache_bytes or 0),
            "disk_free_ratio": ratio,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except Exception as error:  # noqa: BLE001 - 状态写不进去只影响去重
            logger.debug(f"写入缓存巡检状态失败：{error}", exc_info=True)

    def _should_notify(self, previous: Dict[str, Any], level: str) -> bool:
        """首次只有 warn 才通知；同状态按冷却窗口去重；状态切换立即通知。"""
        previous_level = str(previous.get("level") or "").strip()
        if not previous_level:
            return level == LEVEL_WARN
        if previous_level != level:
            return True
        if level != LEVEL_WARN:
            return False
        try:
            last_at = int(previous.get("notified_at") or 0)
        except (TypeError, ValueError):
            last_at = 0
        return (time.time() - last_at) >= ALERT_COOLDOWN_SECONDS

    def _notify(self, level: str, result: Dict[str, Any]) -> bool:
        """发一条通知；失败只记日志，不影响巡检结论。"""
        cache_bytes = int(result.get("cache_bytes") or 0)
        free_bytes = result.get("disk_free_bytes")
        ratio = result.get("disk_free_ratio")
        if level == LEVEL_WARN:
            title = "缓存目录占用过高"
            lines = [
                f"缓存目录：{result.get('cache_dir') or '-'}",
                f"占用 {_human_bytes(cache_bytes)}（阈值 {_human_bytes(CACHE_DIR_ALERT_BYTES)}）",
                "分区剩余 "
                + _human_bytes(free_bytes)
                + (f"（{ratio * 100:.0f}%，阈值低于 {DISK_FREE_ALERT_RATIO * 100:.0f}%）" if ratio is not None else ""),
                "",
                "常见原因：115 上传连续失败触发熔断 / MP 整理失败，",
                "下载好的文件会一直留在缓存目录里。",
                "处理：插件设置 → 跨盘转存 → 「立即清理残留」；",
                "若清理不掉，先看「失败记录」里 115 上传的错误。",
            ]
        else:
            title = "缓存目录已恢复正常"
            lines = [
                f"缓存目录：{result.get('cache_dir') or '-'}",
                f"当前占用 {_human_bytes(cache_bytes)}",
                "分区剩余 "
                + _human_bytes(free_bytes)
                + (f"（{ratio * 100:.0f}%）" if ratio is not None else ""),
            ]
        notify_type = getattr(self, "_notification_type", NotificationType.Plugin)
        try:
            self.post_message(
                mtype=notify_type,
                title=title,
                text="\n".join(lines),
            )
            return True
        except Exception as error:  # noqa: BLE001 - 通知失败不影响巡检
            logger.warning(f"缓存巡检通知发送失败：{error}")
            return False
