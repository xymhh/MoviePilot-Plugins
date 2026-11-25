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

# 不能在模块级 import core.*：core/__init__.py 会 import utils/__init__，
# 而 utils/__init__ 又 import 本模块——若本模块反向 import core.cloud，
# core 尚未初始化完就递归拿同一把模块锁，MP 热重载报
# `deadlock detected by _ModuleLock('app.plugins.cloudsubscribex.core.api.sync')`
# （2026-11-12 真机 13:51 / 16:38 两次复现，导致插件更新失败）。
# 改为函数内延迟 import，此时 core 包已完全初始化。


def _strip_duplicated_prefix(root_path: str, relative: str) -> str:
    """去掉导出树路径里与媒体库根重复的那一段。

    115 的 ``export_dir`` 是以「导出目录自身」为根给出相对路径的，但我们拿到的
    ``directory_id`` 可能指向媒体库的某级父目录，于是 ``relative`` 开头会带上
    ``root_path`` 的尾部（线上实测：root=``/Alist/影视/moviepilot/媒体库``，
    relative=``/moviepilot/媒体库/电影/...``）。直接拼接会得到
    ``.../媒体库/moviepilot/媒体库/...`` 这种不存在的路径，每个目录都解析失败。
    这里找 ``root_path`` 的最长后缀并去掉。
    """
    root_parts = [part for part in str(root_path or "").strip("/").split("/") if part]
    rel_parts = [part for part in str(relative or "").strip("/").split("/") if part]
    if not root_parts or not rel_parts:
        return relative

    # 情况一：导出路径本身已经是从根开始的完整路径（线上实测还会出现这种）
    if rel_parts[:len(root_parts)] == root_parts:
        rest = rel_parts[len(root_parts):]
        return "/" + "/".join(rest) if rest else ""

    # 情况二：导出路径以媒体库根的一段后缀开头，反复剥离直到不再重复
    while True:
        for size in range(min(len(root_parts), len(rel_parts)), 0, -1):
            if rel_parts[:size] == root_parts[-size:]:
                rel_parts = rel_parts[size:]
                break
        else:
            break
    return "/" + "/".join(rel_parts) if rel_parts else ""


def _directory_ops(provider):
    """取目录读取服务。

    ``CloudDriveProvider`` 只是能力容器，自身不暴露 ``resolve_directory`` /
    ``list_directory``，必须用 ``require(DIRECTORY_READ)`` 拿到带 IO 限流的服务对象；
    直接传入服务对象（测试与内部直调）时原样返回。
    """
    from ..core.cloud import CloudDriveCapability

    require = getattr(provider, "require", None)
    if callable(require) and hasattr(provider, "supports"):
        try:
            return require(CloudDriveCapability.DIRECTORY_READ)
        except Exception as error:  # noqa: BLE001 - 能力缺失时退回原对象
            logger.debug(f"取目录读取服务失败，按原对象调用：{error}", exc_info=True)
    return provider


def sweep_orphan_strms(
        generator,
        local_root: str,
        cloud_root: str,
        expected_local: Iterable,
        *,
        max_ratio: float = 0.3,
        min_sample: int = 20,
) -> List[Path]:
    """删掉「本地有 .strm、但 115 目录树里已经没有对应视频」的陈旧指针。

    1.33.0：增量扫描对已存在的 .strm 直接跳过，兄弟清理只在写新文件时触发，
    所以历史上因改名/换代（带不带 ``{tmdb-id}``、原始发布名 vs 模板名）
    留下的重复 .strm 永远不会自愈——现网 85 组重复里 46 组至少一份已对不上
    任何 115 视频（Emby 里就是同一集多出几个条目）。

    只删本地指针文件，不动网盘视频；一次要删的比例超过 ``max_ratio`` 时判定
    目录树导出不完整，整轮放弃（宁可不删）。返回被删的路径列表。
    """
    deleted: List[Path] = []
    try:
        base = Path(
            generator.local_path(local_root, cloud_root, cloud_root, "probe.mp4")
        ).parent
    except Exception as error:  # noqa: BLE001 - 算不出镜像根就放弃
        logger.debug(f"陈旧 STRM 清扫：算镜像根失败（{error}）", exc_info=True)
        return deleted
    if not base.is_dir():
        return deleted
    expected = {str(item) for item in expected_local or () if str(item or "")}
    if not expected:
        return deleted
    try:
        local_strms = [
            path for path in base.rglob("*.strm") if path.is_file()
        ]
    except OSError as error:  # noqa: BLE001 - 列不出目录不删
        logger.debug(f"陈旧 STRM 清扫：遍历失败（{error}）", exc_info=True)
        return deleted
    if not local_strms:
        return deleted
    orphans = [path for path in local_strms if str(path.resolve()) not in expected]
    if not orphans:
        return deleted
    ratio = len(orphans) / len(local_strms)
    if len(local_strms) >= int(min_sample or 0) and ratio > max_ratio:
        logger.warning(
            f"陈旧 STRM 清扫已放弃：待删 {len(orphans)}/{len(local_strms)} "
            f"（{ratio * 100:.0f}% 超过 {max_ratio * 100:.0f}% 安全阀，"
            "疑似目录树导出不完整）"
        )
        return deleted
    for path in orphans:
        try:
            path.unlink()
            deleted.append(path)
        except OSError as error:  # noqa: BLE001 - 单个失败不影响其余
            logger.debug(f"陈旧 STRM 删除失败：{path}（{error}）", exc_info=True)
    if deleted:
        logger.info(
            f"陈旧 STRM 已清理：{len(deleted)} 份（115 里已无对应视频，"
            f"检查本地 {len(local_strms)} 份）"
        )
    return deleted


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
        on_strm_deleted: Optional[Callable[[Path], None]] = None,
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
        "orphans": 0,
    }
    from ..core.cloud import CloudDriveCapability

    if not provider.supports(CloudDriveCapability.STRM_EXPORT):
        stats["errors"] += 1
        logger.warning("STRM 增量：当前网盘不支持导出目录树")
        return stats

    directory_ops = _directory_ops(provider)
    lookup = directory_ops.resolve_directory(cloud_root)
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
    expected_local: List[str] = []
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
        parent_relative = _strip_duplicated_prefix(root_path, parent_relative)
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
        try:
            expected_local.append(str(target.resolve()))
        except OSError:
            expected_local.append(str(target))
        if target.exists() and not overwrite:
            stats["skipped"] += 1
            continue
        pending.setdefault(cloud_dir, []).append(name)

    # 1.33.0：顺手清掉「本地有、115 已无」的陈旧指针（存量重复自愈）。
    # 放在补齐之前：本轮的期望集合已经算全，新写的文件天然在集合里。
    try:
        swept = sweep_orphan_strms(
            generator, local_root, cloud_root, expected_local,
        )
    except Exception as error:  # noqa: BLE001 - 清扫失败不影响补齐
        swept = []
        logger.debug(f"陈旧 STRM 清扫异常（忽略）：{error}", exc_info=True)
    if swept:
        stats["orphans"] = len(swept)
        if on_strm_deleted is not None:
            for path in swept:
                try:
                    on_strm_deleted(path)
                except Exception as error:  # noqa: BLE001 - 登记失败不影响主流程
                    logger.debug(
                        f"陈旧 STRM 删除登记失败：{path}（{error}）",
                        exc_info=True,
                    )

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
        lookup = directory_ops.resolve_directory(cloud_dir)
        if not lookup.checked or lookup.directory_id is None:
            stats["errors"] += len(names)
            logger.warning(f"STRM 增量：无法解析目录 {cloud_dir}")
            continue
        listing = directory_ops.list_directory(str(lookup.directory_id))
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
        f"清陈旧指针 {stats.get('orphans', 0)}，"
        f"耗时 {time.monotonic() - started:.1f} 秒"
    )
    return stats
