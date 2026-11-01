"""跨盘缓存残留清理。

只处理 ``cloud-transfer-<32位hash>.bin`` 及其自动改名副本
``… (1).bin``、伴随文件 ``.meta.json`` / ``.parts.json``（含 ``.tmp``），
目录里其他文件一律不碰。

两级判定：

1. **到期清理**：修改时间早于保留期（默认 6 小时，运行中的下载会持续刷新
   mtime，因此不需要额外的任务列表兜底）；
2. **磁盘压力清理**：磁盘剩余比例低于阈值（默认 10%）时，在到期清理之后，
   再按「最老优先」删除尚未到期的缓存，直到余量回到阈值以上。压力清理
   永不触碰 ``protect_recent_seconds``（默认 10 分钟）内有写入的组，避免
   删掉正在下载的文件。
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

RESIDUE_PATTERN = re.compile(
    r"^(?P<key>cloud-transfer-[0-9a-f]{32})(?: \(\d+\))?"
    r"\.bin(?:\.meta\.json(?:\.tmp)?|\.parts\.json(?:\.tmp)?)?$"
)

_VARIANT_PATTERN = re.compile(r" \(\d+\)(?=\.bin(?:\.|$))")

#: 到期保留期：6 小时内写过的缓存不动（下载中会持续刷新 mtime）。
DEFAULT_MIN_AGE_SECONDS = 6 * 3600
#: 磁盘压力阈值（剩余比例），低于该值触发最老优先清理；0 表示关闭。
DEFAULT_DISK_FREE_RATIO = 0.10
#: 压力清理的保护窗口：这段时间内有写入的组视为活跃，永不清理。
DEFAULT_PROTECT_RECENT_SECONDS = 10 * 60


def _canonical(name: str) -> str:
    """去掉下载器 AutoRename 的 `` (n)`` 变体，便于与任务名比对。"""
    return _VARIANT_PATTERN.sub("", str(name or ""))


def _disk_free_ratio(base: Path) -> Optional[float]:
    """返回根目录所在文件系统的剩余比例；取不到返回 None。"""
    try:
        usage = shutil.disk_usage(str(base))
    except OSError:
        return None
    total = int(getattr(usage, "total", 0) or 0)
    free = int(getattr(usage, "free", 0) or 0)
    if total <= 0:
        return None
    return max(0.0, min(1.0, free / total))


def _newest_mtime(files: Iterable[Path], fallback: float) -> float:
    newest = 0.0
    for item in files:
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            newest = max(newest, fallback)
    return newest


def _delete_group(files: Iterable[Path], result: Dict[str, object], *, pressure: bool) -> int:
    deleted = 0
    for item in files:
        try:
            size = item.stat().st_size
            item.unlink()
        except OSError as error:
            result["errors"].append(f"{item.name}: {error}")
            continue
        deleted += 1
        result["deleted_files"] = int(result["deleted_files"]) + 1
        result["freed_bytes"] = int(result["freed_bytes"]) + int(size or 0)
        if pressure:
            result["pressure_deleted_files"] = int(result["pressure_deleted_files"]) + 1
            result["pressure_freed_bytes"] = int(result["pressure_freed_bytes"]) + int(size or 0)
    return deleted


def scan_residue(
        root: str | Path,
        active_names: Optional[Iterable[str]] = None,
        min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
        now: Optional[float] = None,
        disk_free_ratio: Optional[float] = None,
        protect_recent_seconds: float = DEFAULT_PROTECT_RECENT_SECONDS,
) -> Dict[str, object]:
    """扫描并删除残留，返回统计（不抛异常，单个文件失败记入 errors）。"""
    base = Path(root)
    result: Dict[str, object] = {
        "root": str(base),
        "deleted_files": 0,
        "freed_bytes": 0,
        "skipped_recent": 0,
        "skipped_active": 0,
        "skipped_protected": 0,
        "pressure_deleted_files": 0,
        "pressure_freed_bytes": 0,
        "disk_free_ratio": None,
        "disk_free_target": disk_free_ratio,
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

    stamp = time.time() if now is None else float(now)
    cutoff = stamp - max(0.0, float(min_age_seconds))
    remaining: Dict[str, tuple] = {}
    for key, files in groups.items():
        if any(item.name in active or _canonical(item.name) in active for item in files):
            result["skipped_active"] += 1
            continue
        newest = _newest_mtime(files, cutoff + 1)
        if newest > cutoff:
            result["skipped_recent"] += 1
            remaining[key] = (files, newest)
            continue
        _delete_group(files, result, pressure=False)

    # 阶段二：磁盘压力清理（最老优先，保护活跃组）
    target = float(disk_free_ratio or 0)
    if target > 0:
        ratio = _disk_free_ratio(base)
        result["disk_free_ratio"] = ratio
        if ratio is not None and ratio < target:
            protect_cutoff = stamp - max(0.0, float(protect_recent_seconds))
            ordered = sorted(remaining.values(), key=lambda item: item[1])
            candidates = []
            for files, newest in ordered:
                if newest > protect_cutoff:
                    result["skipped_protected"] += 1
                    continue
                candidates.append(files)
            for files in candidates:
                _delete_group(files, result, pressure=True)
                ratio = _disk_free_ratio(base)
                result["disk_free_ratio"] = ratio
                if ratio is None or ratio >= target:
                    break
    return result
