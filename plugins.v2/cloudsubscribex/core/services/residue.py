"""跨盘缓存残留清理服务。

原先这组方法挂在插件入口（__init__.py）上；1.9.98 起收进职责组件，
API 层（RuntimeApi.api_vue_cleanup_residue）与定时任务都调同一个入口。
"""

from __future__ import annotations

from pathlib import Path

from app.log import logger

from ..aria2 import resolve_data_dir
from ..delegation import OwnerDelegator
from ..residue import resolve_roots, scan_residue


class ResidueService(OwnerDelegator):
    """删除跨盘缓存残留（cloud-transfer-*），返回统计。"""

    def cleanup_transfer_residue(self, reason: str = "手动") -> dict:
        """删除跨盘缓存残留（cloud-transfer-*），返回统计。

        判定见 :mod:`core.residue`：先删超过保留期（默认 6 小时）的缓存 ——
        运行中的下载会持续刷新 mtime，所以不需要额外的任务列表兜底
        （1.7.35 移除 Gopeed 后已无外部任务列表可查）；磁盘余量低于阈值时
        再按最老优先清理未到期缓存，10 分钟内有写入的组一律保护。
        """
        retention_hours = max(1, int(getattr(self, "_residue_retention_hours", 6) or 6))
        result = self._empty_residue_result()
        roots = self._residue_roots()
        result["roots"] = roots
        for root in roots:
            scanned = scan_residue(
                root,
                min_age_seconds=retention_hours * 3600,
                disk_free_ratio=getattr(self, "_residue_disk_free_ratio", 0.0) or 0.0,
            )
            self._merge_residue_result(result, scanned)
            if not result.get("root"):
                result["root"] = scanned.get("root")
        freed_mb = int(result.get("freed_bytes") or 0) / (1024 * 1024)
        pressure_mb = int(result.get("pressure_freed_bytes") or 0) / (1024 * 1024)
        logger.info(
            f"跨盘缓存残留清理（{reason}）：扫描 {len(roots)} 个目录，"
            f"删除 {result.get('deleted_files')} 个文件、"
            f"释放 {freed_mb:.1f} MB（其中磁盘压力清理 {result.get('pressure_deleted_files')} 个 / "
            f"{pressure_mb:.1f} MB）；跳过未过期 {result.get('skipped_recent')} 组、"
            f"任务中 {result.get('skipped_active')} 组、活跃保护 {result.get('skipped_protected')} 组；"
            f"磁盘剩余 {result.get('disk_free_ratio')}"
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
