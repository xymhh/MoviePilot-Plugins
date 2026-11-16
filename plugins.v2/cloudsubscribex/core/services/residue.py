"""跨盘缓存残留清理服务。

原先这组方法挂在插件入口（__init__.py）上；1.9.98 起收进职责组件，
API 层（RuntimeApi.api_vue_cleanup_residue）与启动清理都调同一个入口。

1.10.0 起任务结束无论成败都删临时文件，残留清理只负责无主文件
（崩溃、异常退出、旧版遗留）：不看保留期，不做磁盘压力清理；
在途文件靠跨盘任务管理器的引用精确保护。
"""

from __future__ import annotations

from pathlib import Path

from app.log import logger

from ..aria2 import resolve_data_dir
from ..delegation import OwnerDelegator
from ..residue import resolve_roots, scan_residue

#: 孤儿文件最小保留期（秒）：防未知写入竞态的宽限；在途文件靠引用保护，
#: 与文件新旧无关。
ORPHAN_MIN_AGE_SECONDS = 60


class ResidueService(OwnerDelegator):
    """删除跨盘缓存残留（cloud-transfer-*），返回统计。"""

    def cleanup_transfer_residue(self, reason: str = "手动") -> dict:
        """删除无主的跨盘中继临时文件（cloud-transfer-*），返回统计。

        只删不属于进行中任务的文件：运行中的任务靠
        ``CrossTransferTaskManager.active_cache_keys()`` 精确保护，
        不再依赖修改时间；60 秒宽限只防未知写入竞态。
        """
        active_keys: set = set()
        active_names: set = set()
        manager = getattr(self, "_cross_transfer_manager", None)
        getter = getattr(manager, "active_cache_keys", None)
        if callable(getter):
            try:
                active_keys = {
                    str(key or "").strip() for key in getter() or ()
                }
            except Exception:  # noqa: BLE001 - 取不到在途列表也不影响清理
                active_keys = set()
            active_keys.discard("")
        # 目标名落盘的断点续传文件没有 meta，组键就是文件名本身，靠名字保护。
        name_getter = getattr(manager, "active_cache_names", None)
        if callable(name_getter):
            try:
                active_names = {
                    str(name or "").strip() for name in name_getter() or ()
                }
            except Exception:  # noqa: BLE001 - 取不到在途列表也不影响清理
                active_names = set()
            active_names.discard("")
        result = self._empty_residue_result()
        roots = self._residue_roots()
        result["roots"] = roots
        for root in roots:
            scanned = scan_residue(
                root,
                active_names=active_names,
                active_keys=active_keys,
                min_age_seconds=ORPHAN_MIN_AGE_SECONDS,
                disk_free_ratio=0,
            )
            self._merge_residue_result(result, scanned)
            if not result.get("root"):
                result["root"] = scanned.get("root")
        freed_mb = int(result.get("freed_bytes") or 0) / (1024 * 1024)
        logger.info(
            f"跨盘缓存残留清理（{reason}）：扫描 {len(roots)} 个目录，"
            f"删除 {result.get('deleted_files')} 个文件、"
            f"释放 {freed_mb:.1f} MB；跳过任务中 {result.get('skipped_active')} 组、"
            f"未满宽限 {result.get('skipped_recent')} 组"
        )
        for error in result.get("errors") or []:
            logger.warning(f"跨盘缓存残留清理：{error}")
        return result

    @staticmethod
    def _empty_residue_result() -> dict:
        return {
            "root": "",
            "roots": [],
            "deleted_files": 0,
            "freed_bytes": 0,
            "skipped_recent": 0,
            "skipped_active": 0,
            "skipped_protected": 0,
            "pressure_deleted_files": 0,
            "pressure_freed_bytes": 0,
            "disk_free_ratio": None,
            "disk_free_target": None,
            "errors": [],
        }

    @staticmethod
    def _merge_residue_result(total: dict, part: dict) -> None:
        for key in (
            "deleted_files", "freed_bytes", "skipped_recent", "skipped_active",
            "skipped_protected", "pressure_deleted_files", "pressure_freed_bytes",
        ):
            total[key] = int(total.get(key) or 0) + int(part.get(key) or 0)
        if part.get("disk_free_ratio") is not None:
            total["disk_free_ratio"] = part.get("disk_free_ratio")
        total["disk_free_target"] = part.get("disk_free_target")
        total["errors"].extend(part.get("errors") or [])

    def _residue_roots(self) -> list[str]:
        """缓存清理要扫的目录：配置的缓存目录、aria2 工作目录、历史插件数据目录。

        历史版本把 aria2 数据目录（含日志、会话与早期下载残留）放在插件数据
        目录下，升级后这些残留没人清，所以保留这一条只读扫描路径。
        """
        data_path = ""
        getter = getattr(self, "get_data_path", None)
        if callable(getter):
            try:
                data_path = getter()
            except Exception as error:  # noqa: BLE001 - 取不到数据目录不影响清理
                logger.debug(f"读取插件数据目录失败：{error}", exc_info=True)
        directories = [
            self._cross_transfer_download_path or "/tmp",
            resolve_data_dir(self._cross_transfer_download_path, data_path),
            data_path,
        ]
        roots = resolve_roots(*directories)
        # 只扫真实存在的目录：aria2 工作目录可能还没建，报「目录不存在」是噪音。
        return [path for path in roots if Path(path).is_dir()] or roots
