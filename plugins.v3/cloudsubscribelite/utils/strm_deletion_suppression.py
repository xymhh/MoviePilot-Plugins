"""插件主动删除本地 .strm 的短期屏蔽表（v1.31.6，防 Emby 深度删除连锁）。

事故（2026-11-23，见模块注释与恢复清单）：批量删除 45 组同视频重复后，
Emby 对消失的 .strm 发 library.deleted / item.deleted 事件，深度删除
处理器（core/api/media_library.py）按词干/季集匹配把 115 里的视频也删了
（47 次，48 个文件、32 个目录）。插件自己删的 .strm 必须在事件到达时
被认出来并跳过——只屏蔽路径，不改用户在 Emby 里主动删片的行为。

设计：
* 表放 SyncHandler 实例上（``_strm_deletion_suppression``），删除动作
  都在 SyncHandler 侧组件里，webhook 处理器经 ``self._sync_handler``
  可取到；本模块只放表本身与防御式调用 helper，不认识任何组件。
* 键是媒体服务器口径的路径（``/media/strm/115网盘/...``，与 webhook
  传来的 ``item_path`` 同口径）。登记时本地路径先按
  ``media_server_path_mappings``（如 ``/media/strm=/strm``）翻译，
  同时登记翻译后与原始两条——映射未配时 webhook 也可能直接带本地口径。
* TTL 30 分钟：本次事故 Emby 事件最长延迟约 11 分钟（16:27 删文件 →
  16:38 才来 webhook），30 分钟留足余量；过期即恢复正常删除。
* 过期项在每次读写时惰性清理；表设 1000 条上限，超了先清过期、再丢
  最旧的记录，永不无限增长。
* 残留风险（写明，不藏）：Emby 事件延迟超过 TTL 会恢复删除；用户在
  TTL 内手动在 Emby 里删同一部片会被误拦一次（路径相同无法区分）；
  Emby 若发来文件夹路径而非 .strm 文件路径，精确路径匹配拦不住
  （本次事故的 47 次全部走文件档、路径就是 .strm 本身，见
  VIDEO_SUFFIXES 含 .strm 的核对；文件夹路径事件无实测证据）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable

#: 默认 TTL（秒）：覆盖实测最长约 11 分钟的 Emby 事件延迟，留足余量。
DEFAULT_TTL_SECONDS = 30 * 60
#: 表规模上限：一次批量删除最多几十条；超了丢最旧的，永不无限增长。
DEFAULT_MAX_ENTRIES = 1000


def normalize_media_server_path(path: Any) -> str:
    """归一化媒体服务器路径：与 ``MediaLibraryApi._normalize_media_path``
    同口径（统一分隔符、压掉重复斜杠、去尾部斜杠），两侧键才能对上。
    ``handlers/sync/cleanup.py`` 的同名实现已合并到这里（测试
    ``test_normalize_matches_deep_delete_paths`` 校验样例）。
    """
    value = str(path or "").strip().replace("\\", "/")
    while "//" in value:
        value = value.replace("//", "/")
    return value.rstrip("/")


class StrmDeletionSuppression:
    """带 TTL 的 .strm 删除屏蔽表：路径（媒体服务器口径）→ 过期时刻。

    线程安全（webhook 线程读、同步线程写）；所有方法永不抛错到调用方
    ——表本身只是优化，坏了按“未屏蔽”走，不让修复成为故障点。
    """

    def __init__(
            self,
            ttl_seconds: float = DEFAULT_TTL_SECONDS,
            max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = max(0.0, float(ttl_seconds or 0))
        self._max_entries = max(1, int(max_entries or 1))
        self._lock = threading.RLock()
        self._entries: dict[str, float] = {}

    def suppress(self, server_path: Any) -> bool:
        """登记一条屏蔽路径；空路径返回 False。"""
        key = normalize_media_server_path(server_path)
        if not key:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            if len(self._entries) >= self._max_entries:
                oldest = next(iter(self._entries), None)
                if oldest is not None:
                    self._entries.pop(oldest, None)
            self._entries[key] = now + self._ttl_seconds
        return True

    def suppress_many(self, server_paths: Iterable[Any]) -> int:
        """批量登记，返回实际登记条数。"""
        count = 0
        for path in server_paths or []:
            if self.suppress(path):
                count += 1
        return count

    def is_suppressed(self, server_path: Any) -> bool:
        """该路径是否在屏蔽期内；过期项惰性清理。"""
        key = normalize_media_server_path(server_path)
        if not key:
            return False
        now = time.monotonic()
        with self._lock:
            deadline = self._entries.get(key)
            if deadline is None:
                return False
            if deadline <= now:
                self._entries.pop(key, None)
                return False
            return True

    def _prune_locked(self, now: float) -> int:
        expired = [key for key, deadline in self._entries.items() if deadline <= now]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)

    def __len__(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._entries)


def suppress_deleted_strm_path(owner: Any, local_path: Any) -> bool:
    """防御式登记一条插件主动删除的本地 .strm。

    ``owner`` 是组件或 SyncHandler 本身；经 ``suppress_strm_deletion``
    方法登记（本地 → 媒体服务器路径翻译在 SyncHandler 里做）。
    owner 上没有该方法、方法不可调用、或登记抛错时返回 False，不抛错
    ——调用方（删除主流程）不因此失败。
    """
    if owner is None:
        return False
    try:
        method = getattr(owner, "suppress_strm_deletion", None)
    except Exception:
        return False
    if not callable(method):
        return False
    try:
        return bool(method(local_path))
    except Exception:
        return False
