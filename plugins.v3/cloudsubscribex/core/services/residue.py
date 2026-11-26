"""跨盘缓存残留清理服务。

原先这组方法挂在插件入口（__init__.py）上；1.9.98 起收进职责组件，
API 层（RuntimeApi.api_vue_cleanup_residue）与启动清理都调同一个入口。

1.10.0 起任务结束无论成败都删临时文件，残留清理只负责无主文件
（崩溃、异常退出、旧版遗留）：不看保留期，不做磁盘压力清理；
在途文件靠跨盘任务管理器的引用精确保护。

1.32.0 起补上真名落盘的媒体残留：1.12.1 之后下载即用目标名落盘
（文件名没有 hash），旧规则只认 ``cloud-transfer-*``，这些文件一旦
MP 整理失败就永远留在缓存目录里（2026-11-24 堆到 38GB）。现在按
``cross_transfer_real_name_retention_hours``（默认 24 小时）清理，
并剔除漏斗里仍在自动重试的行所指向的文件。
"""

from __future__ import annotations

from pathlib import Path

from app.log import logger

from ..aria2 import resolve_data_dir
from ..delegation import OwnerDelegator
from ..residue import (
    DEFAULT_REAL_NAME_MIN_AGE_SECONDS,
    resolve_roots,
    scan_residue,
)

#: 孤儿文件最小保留期（秒）：防未知写入竞态的宽限；在途文件靠引用保护，
#: 与文件新旧无关。
ORPHAN_MIN_AGE_SECONDS = 60

#: 漏斗里仍需自动重试的状态。held 不在此列：held 表示不再自动重试，
#: 本地副本再留一个保留期就是纯占盘（删的时候逐条记名，便于追溯）。
FUNNEL_PROTECTED_STATUSES = ("queued", "processing", "failed")

#: 保护名单一次最多取多少行，避免大表拖慢清理。
FUNNEL_PROTECTED_LIMIT = 500


class ResidueService(OwnerDelegator):
    """删除跨盘缓存残留（cloud-transfer-* 与超期真名媒体），返回统计。"""

    def cleanup_transfer_residue(self, reason: str = "手动") -> dict:
        """删除无主的跨盘缓存残留，返回统计。

        只删不属于进行中任务的文件：运行中的任务靠
        ``CrossTransferTaskManager.active_cache_keys()`` 精确保护，
        不再依赖修改时间；60 秒宽限只防未知写入竞态。
        真名媒体文件另有独立保留期（默认 24 小时），并叠加漏斗在途行保护。
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
        # 1.32.0：漏斗里还在自动重试的行指向的本地文件，一律不删。
        protected_names = self._funnel_protected_names()
        if protected_names:
            active_names |= protected_names
        real_name_age = self._real_name_min_age_seconds()
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
                real_name_min_age_seconds=real_name_age,
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
        real_deleted = int(result.get("real_name_deleted_files") or 0)
        if real_deleted:
            real_freed_mb = int(result.get("real_name_freed_bytes") or 0) / (1024 * 1024)
            names = result.get("deleted_real_names") or []
            preview = "、".join(names[:5])
            suffix = f" 等 {len(names)} 个" if len(names) > 5 else ""
            logger.info(
                f"跨盘缓存残留清理（{reason}）：清理超期真名媒体 {real_deleted} 个、"
                f"释放 {real_freed_mb:.1f} MB（{preview}{suffix}）"
            )
        for error in result.get("errors") or []:
            logger.warning(f"跨盘缓存残留清理：{error}")
        return result

    def _real_name_min_age_seconds(self) -> float:
        """真名媒体残留的保留期（秒）；0 表示关闭这条清理路径。"""
        raw = getattr(self, "_cross_transfer_real_name_retention_hours", None)
        if raw is None:
            return float(DEFAULT_REAL_NAME_MIN_AGE_SECONDS)
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            return float(DEFAULT_REAL_NAME_MIN_AGE_SECONDS)
        if hours <= 0:
            return 0.0
        return min(hours, 168.0) * 3600.0

    def _funnel_protected_names(self) -> set:
        """漏斗里仍在自动重试的行的本地文件名（staging_name / file_name）。"""
        names: set = set()
        store_getter = getattr(self, "_get_data_store", None)
        if not callable(store_getter):
            return names
        try:
            store = store_getter()
            repository = getattr(
                getattr(store, "repositories", None), "funnel_queue", None
            )
            if repository is None:
                return names
            rows = repository.list_by_status(
                FUNNEL_PROTECTED_STATUSES, limit=FUNNEL_PROTECTED_LIMIT
            )
        except Exception as error:  # noqa: BLE001 - 取不到保护名单时按无保护处理
            logger.debug(f"读取漏斗在途行失败（跳过保护名单）：{error}", exc_info=True)
            return names
        for row in rows or ():
            for key in ("staging_name", "file_name"):
                text = str((row or {}).get(key) or "").strip()
                if not text:
                    continue
                names.add(text)
                names.add(Path(text).name)
        return names

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
            "real_name_deleted_files": 0,
            "real_name_freed_bytes": 0,
            "real_name_skipped_recent": 0,
            "deleted_real_names": [],
            "disk_free_ratio": None,
            "disk_free_target": None,
            "errors": [],
        }

    @staticmethod
    def _merge_residue_result(total: dict, part: dict) -> None:
        for key in (
            "deleted_files", "freed_bytes", "skipped_recent", "skipped_active",
            "skipped_protected", "pressure_deleted_files", "pressure_freed_bytes",
            "real_name_deleted_files", "real_name_freed_bytes",
            "real_name_skipped_recent",
        ):
            total[key] = int(total.get(key) or 0) + int(part.get(key) or 0)
        total["deleted_real_names"].extend(part.get("deleted_real_names") or [])
        if part.get("disk_free_ratio") is not None:
            total["disk_free_ratio"] = part["disk_free_ratio"]
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
