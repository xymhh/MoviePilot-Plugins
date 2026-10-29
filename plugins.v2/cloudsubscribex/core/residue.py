"""跨盘缓存残留清理。

只处理 ``cloud-transfer-<32位hash>.bin`` 及其自动改名副本
``… (1).bin``、伴随文件 ``.meta.json`` / ``.parts.json``（含 ``.tmp``），
目录里其他文件一律不碰。

判定为残留需同时满足：
1. 修改时间早于保留期（默认 24 小时，运行中的下载会持续刷新 mtime）；
2. （可选）文件名不出现在调用方给出的活跃任务名列表里 —— 现在没有外部
   任务列表可查，运行中的下载靠 mtime 持续刷新即可判定，因此默认只看时间。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

RESIDUE_PATTERN = re.compile(
    r"^(?P<key>cloud-transfer-[0-9a-f]{32})(?: \(\d+\))?"
    r"\.bin(?:\.meta\.json(?:\.tmp)?|\.parts\.json(?:\.tmp)?)?$"
)

_VARIANT_PATTERN = re.compile(r" \(\d+\)(?=\.bin(?:\.|$))")


def _canonical(name: str) -> str:
    """去掉下载器 AutoRename 的 `` (n)`` 变体，便于与任务名比对。"""
    return _VARIANT_PATTERN.sub("", str(name or ""))


def scan_residue(
        root: str | Path,
        active_names: Optional[Iterable[str]] = None,
        min_age_seconds: float = 24 * 3600,
        now: Optional[float] = None,
) -> Dict[str, object]:
    """扫描并删除残留，返回统计（不抛异常，单个文件失败记入 errors）。"""
    base = Path(root)
    result: Dict[str, object] = {
        "root": str(base),
        "deleted_files": 0,
        "freed_bytes": 0,
        "skipped_recent": 0,
        "skipped_active": 0,
        "errors": [],
    }
    if not base.is_dir():
        result["errors"].append(f"下载目录不存在：{base}")
        return result

    active: Set[str] = set()
    for name in active_names or ():
        text = str(name or "").strip()
        if text:
            active.add(text)
            active.add(_canonical(text))

    groups: Dict[str, list] = {}
    try:
        entries = list(base.iterdir())
    except OSError as error:
        result["errors"].append(f"读取下载目录失败：{error}")
        return result
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        match = RESIDUE_PATTERN.match(entry.name)
        if not match:
            continue
        groups.setdefault(match.group("key"), []).append(entry)

    cutoff = (time.time() if now is None else now) - max(0.0, float(min_age_seconds))
    for files in groups.values():
        if any(item.name in active or _canonical(item.name) in active for item in files):
            result["skipped_active"] += 1
            continue
        newest = 0.0
        for item in files:
            try:
                newest = max(newest, item.stat().st_mtime)
            except OSError:
                newest = max(newest, cutoff + 1)
        if newest > cutoff:
            result["skipped_recent"] += 1
            continue
        for item in files:
            try:
                size = item.stat().st_size
                item.unlink()
                result["deleted_files"] += 1
                result["freed_bytes"] += int(size or 0)
            except OSError as error:
                result["errors"].append(f"{item.name}: {error}")
    return result
