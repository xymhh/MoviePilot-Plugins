"""TG 频道资源画质解析与排序。

只做「软偏好」：能从标题识别画质时，同集多条候选先转最优，其余保留
pending 作为兜底；识别不到画质默认放行，绝不因为画质未知而拦截。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_RESOLUTION_ORDER = ("4K", "1080P", "720P")
DEFAULT_HDR_ORDER = ("DOLBY VISION", "HDR10+", "HDR10", "HDR", "SDR")
DEFAULT_CODEC_ORDER = ("HEVC", "AVC", "AV1")

_RESOLUTION_RE = re.compile(
    r"(?<![A-Z0-9])(8K|2160P|4K|UHD|1080P|FHD|720P|480P)(?![A-Z0-9])",
    re.IGNORECASE,
)
_HDR_RE = re.compile(
    r"(杜比\s*视界|DOLBY\s*VISION|DOVI|HDR10\+|HDR10|"
    r"HDR(?![A-Z0-9])|SDR(?![A-Z0-9])|(?<![A-Z])DV(?![A-Z0-9]))",
    re.IGNORECASE,
)
_CODEC_RE = re.compile(
    r"(?<![A-Z0-9])(HEVC|H\.?265|X265|AVC|H\.?264|X264|AV1)(?![A-Z0-9])",
    re.IGNORECASE,
)
_AUDIO_RE = re.compile(
    r"(杜比\s*全景声|ATMOS|TRUEHD|DTS[\s-]*HD|DTS[\s-]*X|DTS|"
    r"DDP|EAC3|AC3|AAC|FLAC|LPCM)",
    re.IGNORECASE,
)

_RESOLUTION_ALIASES = {
    "8K": "8K",
    "2160P": "4K",
    "4K": "4K",
    "UHD": "4K",
    "1080P": "1080P",
    "FHD": "1080P",
    "720P": "720P",
    "480P": "480P",
}
_HDR_ALIASES = {
    "杜比视界": "DOLBY VISION",
    "DOLBYVISION": "DOLBY VISION",
    "DOVI": "DOLBY VISION",
    "DV": "DOLBY VISION",
    "HDR10+": "HDR10+",
    "HDR10": "HDR10",
    "HDR": "HDR",
    "SDR": "SDR",
}
_CODEC_ALIASES = {
    "HEVC": "HEVC",
    "H265": "HEVC",
    "H.265": "HEVC",
    "X265": "HEVC",
    "AVC": "AVC",
    "H264": "AVC",
    "H.264": "AVC",
    "X264": "AVC",
    "AV1": "AV1",
}
_AUDIO_ALIASES = {
    "杜比全景声": "ATMOS",
    "ATMOS": "ATMOS",
    "TRUEHD": "TRUEHD",
    "DTSHD": "DTS-HD",
    "DTS-X": "DTS-X",
    "DTSX": "DTS-X",
    "DTS": "DTS",
    "DDP": "DDP",
    "EAC3": "EAC3",
    "AC3": "AC3",
    "AAC": "AAC",
    "FLAC": "FLAC",
    "LPCM": "LPCM",
}
_PREF_SPLIT_RE = re.compile(r"[\n,，、;；]+")


def _split_preferences(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = [str(item or "") for item in value]
    else:
        values = _PREF_SPLIT_RE.split(str(value or ""))
    return [item.strip() for item in values if item.strip()]


def _normalize_choice(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _normalized_aliases(
        values: Iterable[str], aliases: Dict[str, str],
) -> List[str]:
    result: List[str] = []
    for value in values:
        key = re.sub(r"[\s._-]+", "", _normalize_choice(value))
        normalized = aliases.get(key)
        if not normalized:
            # 允许用户写自定义值；统一大写、压缩空格
            normalized = re.sub(r"\s+", " ", _normalize_choice(value))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def normalize_quality_preferences(raw: Any = None) -> Dict[str, Any]:
    """把配置中的字符串/字典规范成一套全局画质偏好。"""
    raw = raw if isinstance(raw, dict) else {}
    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"0", "false", "off", "no", "否"}

    def _order(key: str, default: Iterable[str], aliases: Dict[str, str]) -> List[str]:
        values = _split_preferences(raw.get(key))
        normalized = _normalized_aliases(values or list(default), aliases)
        return normalized or list(default)

    preferred_groups = _split_preferences(raw.get("preferred_groups"))
    return {
        "enabled": bool(enabled),
        "resolution_order": _order(
            "resolution_order", DEFAULT_RESOLUTION_ORDER, _RESOLUTION_ALIASES
        ),
        "hdr_order": _order("hdr_order", DEFAULT_HDR_ORDER, _HDR_ALIASES),
        "codec_order": _order(
            "codec_order", DEFAULT_CODEC_ORDER, _CODEC_ALIASES
        ),
        "preferred_groups": list(dict.fromkeys(preferred_groups)),
    }


def _find_alias(pattern: re.Pattern, text: str, aliases: Dict[str, str]) -> Optional[str]:
    found: List[tuple[int, str]] = []
    for match in pattern.finditer(text):
        raw = match.group(1)
        key = re.sub(r"[\s._-]+", "", _normalize_choice(raw))
        normalized = aliases.get(key)
        if normalized:
            found.append((match.start(), normalized))
    if not found:
        return None
    # 标题里可能同时出现 HDR10 和 HDR；取最靠前的明确标签
    found.sort(key=lambda item: item[0])
    return found[0][1]


_SUBTITLE_PATTERNS = (
    ("内封简繁英", re.compile(r"内封简繁英|内封简繁英字", re.IGNORECASE)),
    ("内封简繁", re.compile(r"内封简繁", re.IGNORECASE)),
    ("中字", re.compile(
        r"中字|中文字幕|中英字幕|中英双字|简中|繁中|中英|双语字幕",
        re.IGNORECASE,
    )),
)
_CHANNEL_RE = re.compile(r"(?<!\d)([1-7]\.[01])(?!\d)")
_SOURCE_RE = re.compile(
    r"(?<![A-Z0-9])(WEB[- ]?DL|WEBRip|BluRay|REMUX|HDTV)(?![A-Z0-9])",
    re.IGNORECASE,
)
_FREE_RE = re.compile(r"免费")


def _subtitle_tag(text: str) -> Optional[str]:
    for tag, pattern in _SUBTITLE_PATTERNS:
        if pattern.search(text):
            return tag
    return None


def _audio_tag(text: str, audio: Optional[str]) -> Optional[str]:
    if audio == "ATMOS":
        return "杜比全景声"
    if not audio:
        return None
    channel = _CHANNEL_RE.search(text)
    return f"{audio}{channel.group(1)}" if channel else audio


def _preferred_group(text: str, groups: Iterable[str]) -> Optional[str]:
    compressed = re.sub(r"[\s._-]+", "", text).casefold()
    for group in groups:
        needle = re.sub(r"[\s._-]+", "", str(group or "")).casefold()
        if needle and needle in compressed:
            return str(group)
    return None


def _index_or_last(value: Optional[str], order: List[str]) -> int:
    if value and value in order:
        return order.index(value)
    return len(order)


def parse_quality(
        title: Any, preferences: Any = None,
) -> Dict[str, Any]:
    """从标题解析画质并计算同一套偏好下的排序键。

    ``rankable`` 只在识别到分辨率时为 True。没有分辨率的标题（包括只有
    HDR/编码、或完全没标签）一律按「未知画质」处理，由调用方默认放行。
    """
    text = str(title or "")
    prefs = normalize_quality_preferences(preferences)
    resolution = _find_alias(_RESOLUTION_RE, text, _RESOLUTION_ALIASES)
    hdr = _find_alias(_HDR_RE, text, _HDR_ALIASES)
    codec = _find_alias(_CODEC_RE, text, _CODEC_ALIASES)
    audio = _find_alias(_AUDIO_RE, text, _AUDIO_ALIASES)
    subtitle = _subtitle_tag(text)
    source_match = _SOURCE_RE.search(text)
    source = source_match.group(1).upper().replace(" ", "-") if source_match else None
    free = bool(_FREE_RE.search(text))
    audio_tag = _audio_tag(text, audio)
    group = _preferred_group(text, prefs["preferred_groups"])

    resolution_order = prefs["resolution_order"]
    hdr_order = prefs["hdr_order"]
    codec_order = prefs["codec_order"]
    resolution_index = _index_or_last(resolution, resolution_order)
    hdr_index = _index_or_last(hdr, hdr_order)
    codec_index = _index_or_last(codec, codec_order)
    group_index = 0 if group else (1 if prefs["preferred_groups"] else 0)

    parts = [item for item in (resolution, hdr, codec, group) if item]
    label = ".".join(parts) if parts else "未知"
    rankable = bool(resolution)
    hdr_tag = "杜比视界" if hdr == "DOLBY VISION" else hdr
    tags = [
        tag for tag in (
            "免费" if free else None,
            resolution,
            hdr_tag,
            subtitle,
            audio_tag,
        ) if tag
    ]
    return {
        "enabled": prefs["enabled"],
        "label": label,
        "resolution": resolution,
        "hdr": hdr,
        "codec": codec,
        "audio": audio,
        "subtitle": subtitle,
        "source": source,
        "free": free,
        "tags": tags,
        "group": group,
        "rankable": rankable,
        "preferred_resolution": bool(
            resolution and resolution_index == 0
        ),
        "rank_key": [
            resolution_index, hdr_index, codec_index, group_index,
        ],
    }
