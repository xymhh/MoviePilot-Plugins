"""按体积/分辨率择优入库的比较器（纯函数，可单测）。

判据顺序：分辨率 > 体积，体积需要达到最小增益阈值才认为「更好」。
设计见 docs/plan/2026-10-27-按体积洗版-架构.md
"""

import re
from typing import Any, Iterable, Optional, Sequence, Tuple

# 分辨率提升时允许新文件比旧文件小的比例（防止「4K 片段」把正片换掉）
RES_UPGRADE_MIN_SIZE_RATIO = 0.6

_RESOLUTION_PATTERNS = (
    (4320, re.compile(r"(?<!\d)(?:4320[pi]|8k)(?!\w)", re.IGNORECASE)),
    (2160, re.compile(r"(?<!\d)(?:2160[pi]|4k|uhd)(?!\w)", re.IGNORECASE)),
    (1440, re.compile(r"(?<!\d)1440[pi](?!\w)", re.IGNORECASE)),
    (1080, re.compile(r"(?<!\d)(?:1080[pi]|fhd)(?!\w)", re.IGNORECASE)),
    (720, re.compile(r"(?<!\d)720[pi](?!\w)", re.IGNORECASE)),
    (576, re.compile(r"(?<!\d)576[pi](?!\w)", re.IGNORECASE)),
    (480, re.compile(r"(?<!\d)480[pi](?!\w)", re.IGNORECASE)),
    (360, re.compile(r"(?<!\d)360[pi](?!\w)", re.IGNORECASE)),
)


def parse_resolution(name: Any) -> int:
    """从文件名解析分辨率（如 2160/1080/720），解析不出返回 0。"""
    text = str(name or "")
    if not text:
        return 0
    for value, pattern in _RESOLUTION_PATTERNS:
        if pattern.search(text):
            return value
    return 0


def human_size(size: Any) -> str:
    """人类可读体积，保持与本插件通知一致的单位风格。"""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    if value <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}".replace(".00", "")
        value /= 1024
    return f"{value:.2f} TB"


def _best_old(versions: Sequence[Tuple[Any, Any]]) -> Tuple[str, int, int]:
    """旧版本里取最好的一条：先比分辨率，再比体积。"""
    best_name = ""
    best_size = 0
    best_res = -1
    for item in versions or []:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            name, size = item[0], item[1]
        elif isinstance(item, dict):
            name = item.get("file_name") or item.get("name")
            size = item.get("file_size") or item.get("size")
        else:
            continue
        try:
            size_value = max(0, int(size or 0))
        except (TypeError, ValueError):
            size_value = 0
        res_value = parse_resolution(name)
        if (res_value, size_value) > (best_res, best_size):
            best_name, best_res, best_size = str(name or ""), res_value, size_value
    return best_name, max(best_res, 0), best_size


def compare_quality(
        new_name: Any,
        new_size: Any,
        old_versions: Iterable[Any],
        min_gain_percent: int = 10,
        min_gain_mb: int = 200,
) -> Tuple[str, str]:
    """比较新候选与库里已有版本，返回 ``(action, reason)``。

    action: ``upgrade`` 需要替换；``keep`` 保留现有版本。
    """
    try:
        new_size_value = max(0, int(new_size or 0))
    except (TypeError, ValueError):
        new_size_value = 0

    old_name, old_res, old_size = _best_old(list(old_versions or []))
    new_res = parse_resolution(new_name)

    if old_size <= 0:
        if new_size_value > 0:
            return "upgrade", f"旧文件体积未知，新文件 {human_size(new_size_value)}"
        return "keep", "新旧文件体积均未知，保持现状"

    if new_res and old_res:
        if new_res > old_res:
            ratio = new_size_value / old_size if old_size else 0
            if ratio >= RES_UPGRADE_MIN_SIZE_RATIO:
                return (
                    "upgrade",
                    f"分辨率 {old_res}p → {new_res}p（{human_size(old_size)} → "
                    f"{human_size(new_size_value)}）",
                )
            return (
                "keep",
                f"分辨率虽为 {new_res}p，但体积只有旧版 {ratio:.0%}"
                f"（{human_size(old_size)} → {human_size(new_size_value)}），疑似伪 4K/片段",
            )
        if new_res < old_res:
            return (
                "keep",
                f"分辨率 {new_res}p 低于现有 {old_res}p（{human_size(old_size)}）",
            )

    try:
        percent = max(0, int(min_gain_percent or 0))
    except (TypeError, ValueError):
        percent = 0
    try:
        min_mb = max(0, int(min_gain_mb or 0))
    except (TypeError, ValueError):
        min_mb = 0

    need_by_percent = int(old_size * (1 + percent / 100.0))
    need_by_mb = old_size + min_mb * 1024 * 1024
    need = max(need_by_percent, need_by_mb)
    if new_size_value >= need and new_size_value > old_size:
        gain = (new_size_value - old_size) / old_size if old_size else 0
        res_note = f"分辨率同为 {new_res}p，" if new_res and new_res == old_res else ""
        return (
            "upgrade",
            f"{res_note}体积 {human_size(old_size)} → {human_size(new_size_value)}"
            f"（+{gain:.0%}）",
        )

    res_note = f"分辨率同为 {new_res}p，" if new_res and new_res == old_res else ""
    return (
        "keep",
        f"{res_note}体积增益不足（{human_size(old_size)} → {human_size(new_size_value)}，"
        f"需 ≥ {human_size(need)}）",
    )


def compare_movie_quality(
        new_name: Any,
        new_size: Any,
        old_versions: Iterable[Any],
        min_gain_percent: int = 10,
        min_gain_mb: int = 200,
) -> Tuple[str, str]:
    """电影侧入口，规则与剧集一致（留作语义区分）。"""
    return compare_quality(new_name, new_size, old_versions, min_gain_percent, min_gain_mb)
