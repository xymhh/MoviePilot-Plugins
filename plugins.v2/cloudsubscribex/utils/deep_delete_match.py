"""深度删除（Emby 侧删除 → 联动删网盘）的记录匹配规则。

抽成独立模块便于单测，不依赖 MoviePilot 运行时。
"""
from pathlib import Path
from typing import Any, List, Set


def match_history_record_to_targets(
        file_name: str,
        media_server_path: str,
        target_filenames: Set[str],
        target_stems: Set[str],
        normalized_targets: List[str],
) -> bool:
    """判断一条历史记录是否落在本次深度删除的目标范围内。

    - 文件名/词干相同 → 命中（消除 .strm 与源文件、挂载前缀差异）
    - 媒体服务器路径相同（或去扩展名后相同）→ 命中
    - 目标是目录（整剧/整季）且记录路径在其下 → 命中
    """
    record_file_cf = str(file_name or "").casefold()
    if not record_file_cf:
        return False
    if record_file_cf in target_filenames:
        return True
    if Path(record_file_cf).stem in target_stems:
        return True

    ms_cf = str(media_server_path or "").casefold().rstrip("/")
    if not ms_cf:
        return False
    ms_stem = str(Path(ms_cf).with_suffix(""))
    for target in normalized_targets:
        target_cf = str(target or "").casefold().rstrip("/")
        if not target_cf:
            continue
        if ms_cf == target_cf or ms_stem == str(Path(target_cf).with_suffix("")):
            return True
        if ms_cf.startswith(target_cf + "/"):
            return True
    return False
