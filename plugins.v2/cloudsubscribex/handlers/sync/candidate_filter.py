"""候选资源预筛：把明显不值得调用网盘 API 的候选挡在列目录之前。

线上背景（2026-10-24 日志实测）：10 小时里 118 条「分享根目录与订阅标题不符」，
每条都发生在 ``list_share_files`` 之后——标题守卫只能拦下转存，拦不住那次分享
目录读取；其中还有「…主题曲 (FLAC 24bit 48khz)」这类音乐分享被当成影视资源校验。

本模块做两件事：
1. ``non_video_reason``：标题里出现音频/电子书强特征时直接判定为非影视资源；
2. ``mismatch_seen/remember_mismatch``：同一份 (分享链接, 标题) 被判定「标题不符」
   后 24h 内不再重复列目录，避免每轮扫描都白跑一次。

负缓存刻意用进程内字典实现，不依赖 MoviePilot 缓存后端：这里存的是短命布尔标记，
热重载丢一次即可，没必要为它引入序列化与 Redis 往返。
"""

import re
import time
from threading import Lock
from typing import Dict, Optional, Tuple

# 音频 / 电子书强特征。CJK 词直接子串匹配，ASCII 标记要求独立词边界，
# 避免「ESCAPE」被 APE 命中这类误伤。
_CJK_MARKERS = (
    "无损", "母带", "原声带", "原声大碟", "主题曲", "插曲", "单曲", "专辑",
    "有声书", "有声小说", "有声剧", "广播剧", "听书", "电子书",
)
_ASCII_MARKERS = (
    "FLAC", "APE", "WAV", "MP3", "AAC", "DSD", "OST", "EPUB", "MOBI", "AZW3",
)

_MISMATCH_TTL = 24 * 60 * 60
_MISMATCH_MAXSIZE = 4000

_mismatch_lock = Lock()
_mismatch_records: Dict[Tuple[str, str], float] = {}


def non_video_reason(resource_title: str) -> Optional[str]:
    """标题命中音频/电子书特征时返回原因，否则返回 None。"""
    text = str(resource_title or "").strip()
    if not text:
        return None
    upper = text.upper()
    for marker in _ASCII_MARKERS:
        if re.search(rf"(?<![A-Z0-9]){marker}(?![A-Z0-9])", upper):
            return f"疑似非影视资源（{marker}）"
    for marker in _CJK_MARKERS:
        if marker in text:
            return f"疑似非影视资源（{marker}）"
    return None


def _memo_key(share_url: str, resource_title: str) -> Tuple[str, str]:
    return (str(share_url or "").strip(), str(resource_title or "").strip())


def mismatch_seen(share_url: str, resource_title: str) -> bool:
    """该 (链接, 标题) 是否已在 24h 内被判为标题不符。"""
    key = _memo_key(share_url, resource_title)
    now = time.time()
    with _mismatch_lock:
        expires_at = _mismatch_records.get(key)
        if expires_at is None:
            return False
        if expires_at <= now:
            _mismatch_records.pop(key, None)
            return False
        return True


def remember_mismatch(share_url: str, resource_title: str) -> None:
    """记录一次标题不符，供后续扫描跳过。"""
    key = _memo_key(share_url, resource_title)
    if not key[0]:
        return
    now = time.time()
    with _mismatch_lock:
        if len(_mismatch_records) >= _MISMATCH_MAXSIZE:
            # 先清过期项，仍超额则整体清空，避免无界增长。
            expired = [k for k, exp in _mismatch_records.items() if exp <= now]
            for item in expired:
                _mismatch_records.pop(item, None)
            if len(_mismatch_records) >= _MISMATCH_MAXSIZE:
                _mismatch_records.clear()
        _mismatch_records[key] = now + _MISMATCH_TTL
