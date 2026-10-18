"""STRM 全量校准：按目录 CID 遍历媒体库并补齐/校准本地 STRM。"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .strm import StrmGenerator, StrmTemplateError

logger = logging.getLogger(__name__)


def rebuild_strm_tree(
        provider: Any,
        generator: StrmGenerator,
        local_root: str,
        cloud_root: str,
        video_extensions: Iterable[str],
        reference_values: Callable[[Any], Dict[str, str]],
        overwrite: bool = False,
        progress: Optional[Callable[[Dict[str, int]], None]] = None,
        interval_seconds: float = 0.0,
) -> Dict[str, int]:
    """从 ``cloud_root`` 起按 cid 递归，为每个视频补齐 STRM。

    ``provider`` 需提供 ``resolve_directory(path)`` 与 ``list_directory(cid)``；
    目录项使用统一的 ``CloudFile``（``id``/``name``/``is_directory``）。
    不删除任何网盘文件；``overwrite=False`` 时只补缺失。
    """
    stats: Dict[str, int] = {
        "directories": 0,
        "files": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
    }
    lookup = provider.resolve_directory(cloud_root)
    if not lookup.checked or lookup.directory_id is None:
        stats["errors"] += 1
        logger.warning(f"STRM 全量校准：无法解析媒体库根目录 {cloud_root}")
        return stats

    extensions = {
        str(ext or "").strip().lower()
        for ext in video_extensions
        if str(ext or "").strip()
    }
    root_path = "/" + str(cloud_root or "/").replace("\\", "/").strip("/")
    stack: List[tuple] = [(str(lookup.directory_id), root_path)]
    visited = set()

    while stack:
        cid, dir_path = stack.pop()
        if cid in visited:
            continue
        visited.add(cid)
        listing = provider.list_directory(cid)
        if not listing.checked:
            stats["errors"] += 1
            continue
        stats["directories"] += 1
        if progress and stats["directories"] % 200 == 0:
            progress(stats)
        if interval_seconds > 0:
            # 目录间主动节流：iterdir 的 cooldown 只在「翻页」之间生效，
            # 小目录单页返回时不会经过账号级限流器，全量扫描必须自己减速。
            time.sleep(interval_seconds)

        for item in listing.files:
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            child_path = (
                f"{dir_path.rstrip('/')}/{name}"
                if dir_path != "/" else f"/{name}"
            )
            if getattr(item, "is_directory", False):
                stack.append((str(item.id), child_path))
                continue
            if Path(name).suffix.lower() not in extensions:
                continue

            stats["files"] += 1
            try:
                values = reference_values(item) or {}
                if not values:
                    stats["skipped"] += 1
                    continue
                content = generator.render(
                    file_name=name,
                    file_path=child_path,
                    template_values=values,
                )
                strm_path = generator.local_path(
                    local_root=local_root,
                    cloud_root=cloud_root,
                    cloud_dir=dir_path,
                    file_name=name,
                )
                if strm_path.is_file():
                    existing = strm_path.read_text(encoding="utf-8", errors="ignore")
                    if not overwrite or existing == content:
                        stats["skipped"] += 1
                        continue
                    stats["updated"] += 1
                else:
                    stats["created"] += 1
                generator.write(
                    local_root=local_root,
                    cloud_root=cloud_root,
                    cloud_dir=dir_path,
                    file_name=name,
                    template_values=values,
                )
            except StrmTemplateError as error:
                stats["errors"] += 1
                logger.debug(f"STRM 全量校准模板错误：{child_path}：{error}")
            except OSError as error:
                stats["errors"] += 1
                logger.debug(f"STRM 全量校准写盘失败：{child_path}：{error}")
    return stats
