from typing import Any, Iterable
from urllib.parse import unquote

try:
    from app.domain.episode import format_ranges as platform_format_ranges
except Exception:
    platform_format_ranges = None


def extract_ed2k_filename(url: str) -> str:
    """从 ED2K 链接中解析提取原始文件名。"""
    if not url or not str(url).strip().lower().startswith("ed2k://|file|"):
        return ""
    parts = str(url).strip().split("|")
    if len(parts) > 2 and parts[2]:
        return unquote(parts[2]).strip()
    return ""


def format_episode_ranges(episodes: Iterable[Any]) -> str:
    """把集数集合压缩为 E01-E03、E05 形式，优先使用平台原生 format_ranges。"""
    if not episodes:
        return "无"
    numbers = sorted({int(ep) for ep in episodes if str(ep).isdigit() or isinstance(ep, int)})
    if not numbers:
        return "无"
    if platform_format_ranges:
        try:
            result = platform_format_ranges(numbers)
            if result:
                return result
        except Exception:
            pass
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        start = previous = number
    ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
    return "、".join(ranges)


def normalize_season(value: Any, default: int = 1) -> int:
    """规范化季号：None 或空字符串回退默认季号；0 表示特别篇 S00。"""
    try:
        return max(0, int(default if value is None or value == "" else value))
    except (TypeError, ValueError):
        return default
