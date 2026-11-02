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

# 电子书 / 有声书强特征：只可能出现在非影视资源里，命中即判。
_CJK_STRONG = (
    "有声书", "有声小说", "有声剧", "广播剧", "听书", "电子书", "专辑", "单曲",
)
# 音乐强特征：只可能出现在音乐分享里，命中即判。
_ASCII_STRONG = ("EPUB", "MOBI", "AZW3")
# 弱特征：也常作为**影视资源的音轨规格**出现在标题里
# （`4K WEB-DL AAC`、`4K 10bit 60FPS FLAC无损HiFi声`、`1080p HDR AAC5.1`），
# 因此只有标题里完全没有视频信号时才判非影视。
_ASCII_WEAK = ("FLAC", "APE", "WAV", "MP3", "AAC", "DSD", "OST")
_CJK_WEAK = ("无损", "母带", "原声带", "原声大碟", "主题曲", "插曲")

# 视频信号：分辨率 / 片源 / 编码 / 集号，任一命中就认为这是影视资源。
_VIDEO_SIGNAL = re.compile(
    r"(?<![A-Z0-9])(?:4K|8K|2160P?|1080P?|720P?|480P?|WEB-?DL|WEBRIP|BLURAY|BDRIP|"
    r"HDTV|HDTVRIP|REMUX|HEVC|AVC|H\.?26[45]|X26[45]|HDR|SDR|DOLBY|DV|"
    r"S\d{1,2}\s*E\d{1,4}|E\d{1,4})(?![A-Z0-9])",
    re.IGNORECASE,
)

_MISMATCH_TTL = 24 * 60 * 60
_MISMATCH_MAXSIZE = 4000

_mismatch_lock = Lock()
_mismatch_records: Dict[Tuple[str, str], float] = {}


def _has_video_signal(text: str) -> bool:
    """标题里是否有影视信号（分辨率/片源/编码/集号）。"""
    return bool(_VIDEO_SIGNAL.search(text))


def non_video_reason(resource_title: str) -> Optional[str]:
    """标题命中音频/电子书特征时返回原因，否则返回 None。

    强特征（FLAC/APE/DSD/电子书格式）命中即判；
    弱特征（AAC/WAV/MP3/OST）只有在标题不含任何视频信号时才判——
    `嫁金钗 (2026) S01E26 ✨4K WEB-DL AAC` 里的 AAC 是音轨编码，不是音频资源。
    """
    text = str(resource_title or "").strip()
    if not text:
        return None
    upper = text.upper()
    for marker in _ASCII_STRONG:
        if re.search(rf"(?<![A-Z0-9]){marker}(?![A-Z0-9])", upper):
            return f"疑似非影视资源（{marker}）"
    for marker in _CJK_STRONG:
        if marker in text:
            return f"疑似非影视资源（{marker}）"
    if not _has_video_signal(text):
        for marker in _ASCII_WEAK:
            if re.search(rf"(?<![A-Z0-9]){marker}(?![A-Z0-9])", upper):
                return f"疑似非影视资源（{marker}）"
        for marker in _CJK_WEAK:
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
