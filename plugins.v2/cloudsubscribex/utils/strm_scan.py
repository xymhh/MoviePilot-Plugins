"""STRM 全量校准：按目录 CID 遍历媒体库并补齐/校准本地 STRM。"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .strm import StrmGenerator, StrmTemplateError
from .strm_export import _directory_ops

logger = logging.getLogger(__name__)


def _load_scan_state(
        state_path: Optional[Path], cloud_root: str,
) -> Optional[Dict[str, Any]]:
    """读取上次未扫完的续扫状态；根目录不一致则丢弃。"""
    if state_path is None:
        return None
    try:
        if not state_path.is_file():
            return None
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("cloud_root") != cloud_root:
        return None
    pending = payload.get("pending")
    if not isinstance(pending, list) or not pending:
        return None
    visited = payload.get("visited")
    return {
        "pending": [
            (str(item[0]), str(item[1]))
            for item in pending
            if isinstance(item, (list, tuple)) and len(item) == 2
        ],
        "visited": [str(item) for item in visited] if isinstance(visited, list) else [],
    }


def _save_scan_state(
        state_path: Optional[Path],
        cloud_root: str,
        stack: List[tuple],
        visited: set,
) -> bool:
    """把未扫目录写盘，供下次分批续扫。"""
    if state_path is None:
        return False
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cloud_root": cloud_root,
            "pending": [[cid, path] for cid, path in stack],
            "visited": sorted(visited),
            "saved_at": int(time.time()),
        }
        state_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except OSError as error:  # noqa: BLE001
        logger.warning(f"STRM 全量校准：续扫状态写入失败：{error}")
        return False


def _clear_scan_state(state_path: Optional[Path]) -> None:
    if state_path is None:
        return
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("_clear_scan_state 分支忽略异常", exc_info=True)


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
        time_budget_seconds: float = 0.0,
        state_path: Optional[Path] = None,
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
    directory_ops = _directory_ops(provider)
    lookup = directory_ops.resolve_directory(cloud_root)
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
    resumed = _load_scan_state(state_path, root_path)
    if resumed:
        stack: List[tuple] = list(resumed["pending"])
        visited = set(resumed["visited"])
        logger.info(
            f"STRM 全量校准：从上次断点续扫，剩余 {len(stack)} 个目录待处理"
        )
    else:
        stack = [(str(lookup.directory_id), root_path)]
        visited = set()
    deadline = (
        time.monotonic() + float(time_budget_seconds)
        if time_budget_seconds and time_budget_seconds > 0
        else None
    )

    while stack:
        if deadline is not None and time.monotonic() > deadline:
            _save_scan_state(state_path, root_path, stack, visited)
            stats["interrupted"] = 1
            stats["pending_directories"] = len(stack)
            logger.info(
                f"STRM 全量校准：本次已达时间上限，剩余 {len(stack)} 个目录"
                "留待下次续扫"
            )
            return stats
        cid, dir_path = stack.pop()
        if cid in visited:
            continue
        visited.add(cid)
        listing = directory_ops.list_directory(cid)
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
    _clear_scan_state(state_path)
    return stats
