"""基于 115「导出目录树」的 STRM 差异补齐。

思路（与 P115StrmHelper 一致）：

1. 一次 ``export_dir`` 拿到云端整棵目录树的全部路径（服务端任务，请求数恒定）；
2. 用这些路径和本地 ``.strm`` 做差集，得出「云端有、本地没有」的文件；
3. 只对差异文件**所在目录**各取一次文件列表拿 pickcode，落盘 STRM。

稳定期（无新增）不产生任何 ``list_directory`` 调用，请求数从「目录数」降到常数级。
"""

import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from app.log import logger

from ..core.cloud import CloudDriveCapability


def rebuild_strm_by_export_tree(
        provider,
        generator,
        local_root: str,
        cloud_root: str,
        video_extensions: Iterable[str],
        reference_values: Callable,
        *,
        overwrite: bool = False,
        timeout_seconds: float = 600.0,
        progress: Optional[Callable[[Dict[str, int]], None]] = None,
) -> Dict[str, int]:
    """用导出目录树比对本地 STRM，只补缺失（或按需覆盖）。"""
    stats: Dict[str, int] = {
        "directories": 0,
        "files": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "exported": 0,
    }
    if not provider.supports(CloudDriveCapability.STRM_EXPORT):
        stats["errors"] += 1
        logger.warning("STRM 增量：当前网盘不支持导出目录树")
        return stats

    lookup = provider.resolve_directory(cloud_root)
    if not lookup.checked or lookup.directory_id is None:
        stats["errors"] += 1
        logger.warning(f"STRM 增量：无法解析媒体库根目录 {cloud_root}")
        return stats

    extensions = {
        str(ext or "").strip().lower()
        for ext in video_extensions
        if str(ext or "").strip()
    }
    root_path = "/" + str(cloud_root or "/").replace("\\", "/").strip("/")

    started = time.monotonic()
    exporter = provider.require(CloudDriveCapability.STRM_EXPORT)
    try:
        tree_paths = list(
            exporter.export_directory_tree(
                str(lookup.directory_id), timeout_seconds=timeout_seconds,
            )
        )
    except Exception as error:  # noqa: BLE001 - 导出失败交由调用方降级
        stats["errors"] += 1
        logger.warning(f"STRM 增量：导出目录树失败（{error}）")
        return stats

    stats["exported"] = len(tree_paths)
    logger.info(
        f"STRM 增量：目录树 {len(tree_paths)} 条路径，"
        f"耗时 {time.monotonic() - started:.1f} 秒"
    )

    # 第一遍：只算差异，不产生网盘请求
    pending: Dict[str, List[str]] = {}
    for raw_path in tree_paths:
        relative = str(raw_path or "").replace("\\", "/").strip()
        if not relative:
            continue
        if not relative.startswith("/"):
            relative = "/" + relative
        name = Path(relative).name
        if not name:
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in extensions:
            continue  # 目录行与非视频文件
        parent_relative = relative.rsplit("/", 1)[0]
        cloud_dir = (
            f"{root_path.rstrip('/')}{parent_relative}"
            if parent_relative not in ("", "/")
            else root_path
        )
        stats["files"] += 1
        try:
            target = generator.local_path(
                local_root, cloud_root, cloud_dir, name,
            )
        except Exception as error:  # noqa: BLE001
            logger.debug(f"STRM 增量：路径计算失败 {cloud_dir}/{name}（{error}）")
            stats["errors"] += 1
            continue
        if target.exists() and not overwrite:
            stats["skipped"] += 1
            continue
        pending.setdefault(cloud_dir, []).append(name)

    if not pending:
        logger.info(
            f"STRM 增量：无缺失，检查 {stats['files']} 个视频，"
            f"耗时 {time.monotonic() - started:.1f} 秒"
        )
        return stats

    logger.info(
        f"STRM 增量：待补 {sum(len(v) for v in pending.values())} 个文件，"
        f"涉及 {len(pending)} 个目录"
    )

    # 第二遍：只对差异目录各取一次列表，拿 pickcode 后落盘
    for cloud_dir, names in pending.items():
        if progress:
            progress(stats)
        lookup = provider.resolve_directory(cloud_dir)
        if not lookup.checked or lookup.directory_id is None:
            stats["errors"] += len(names)
            logger.warning(f"STRM 增量：无法解析目录 {cloud_dir}")
            continue
        listing = provider.list_directory(str(lookup.directory_id))
        if not listing.checked:
            stats["errors"] += len(names)
            continue
        stats["directories"] += 1
        index = {
            str(getattr(item, "name", "") or "").strip(): item
            for item in listing.files
            if not getattr(item, "is_directory", False)
        }
        for name in names:
            item = index.get(name)
            if item is None:
                stats["errors"] += 1
                logger.debug(f"STRM 增量：目录列表中未找到 {cloud_dir}/{name}")
                continue
            values = reference_values(item) or {}
            if not values:
                stats["skipped"] += 1
                continue
            try:
                target = generator.local_path(
                    local_root, cloud_root, cloud_dir, name,
                )
                existed = target.exists()
                generator.write(
                    local_root=local_root,
                    cloud_root=cloud_root,
                    cloud_dir=cloud_dir,
                    file_name=name,
                    template_values=values,
                )
                if existed:
                    stats["updated"] += 1
                else:
                    stats["created"] += 1
            except Exception as error:  # noqa: BLE001
                stats["errors"] += 1
                logger.warning(f"STRM 增量：写入失败 {cloud_dir}/{name}（{error}）")

    logger.info(
        f"STRM 增量完成：新增 {stats['created']}，更新 {stats['updated']}，"
        f"跳过 {stats['skipped']}，失败 {stats['errors']}，"
        f"耗时 {time.monotonic() - started:.1f} 秒"
    )
    return stats
