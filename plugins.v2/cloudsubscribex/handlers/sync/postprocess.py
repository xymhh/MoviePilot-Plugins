"""离线任务完成检测与文件后处理。"""

import copy
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import SessionFactory
from app.db.models.subscribe import Subscribe
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from .utils import extract_ed2k_filename, normalize_season
from ...core import CloudDriveCapability, OwnerDelegator
from ...core.failure import apply_failure, failure_retryable_of
from ...search.subs_filter import anime_file_candidates
from ...utils import MediaFileParser


class DirectoryFileIndex(dict):
    """支持文件名直接访问与动态网盘哈希（如 sha1, md5, sha256）及剧集季集 O(1) 快速反查的目录文件索引对象。"""

    def __init__(
            self,
            files: Optional[List[Any]] = None,
            algorithms: Optional[Set[str]] = None,
    ):
        super().__init__()
        self.by_hash: Dict[str, Any] = {}
        self.by_season_episode: Dict[Tuple[int, int], Any] = {}
        self.algorithms = frozenset(str(a).lower() for a in (algorithms or {"sha1", "md5"}))
        if files:
            for file_obj in files:
                self.add_file(file_obj)

    @property
    def by_sha1(self) -> Dict[str, Any]:
        return self.by_hash

    def add_file(self, file_obj: Any) -> None:
        name = getattr(file_obj, "name", None) or (file_obj.get("name") if isinstance(file_obj, dict) else None)
        if name:
            self[name] = file_obj
            se = MediaFileParser.extract_season_episode(name)
            if se and se not in self.by_season_episode:
                self.by_season_episode[se] = file_obj
        for algo in self.algorithms:
            val = getattr(file_obj, algo, None) or (file_obj.get(algo) if isinstance(file_obj, dict) else None)
            if val:
                self.by_hash[str(val).strip().upper()] = file_obj

    def remove_file(self, file_obj: Any) -> None:
        name = getattr(file_obj, "name", None) or (file_obj.get("name") if isinstance(file_obj, dict) else None)
        if name and name in self:
            del self[name]
            se = MediaFileParser.extract_season_episode(name)
            if se and self.by_season_episode.get(se) == file_obj:
                self.by_season_episode.pop(se, None)
        for algo in self.algorithms:
            val = getattr(file_obj, algo, None) or (file_obj.get(algo) if isinstance(file_obj, dict) else None)
            if val:
                self.by_hash.pop(str(val).strip().upper(), None)

    def get_by_hash(self, file_hash: str) -> Optional[Any]:
        if not file_hash:
            return None
        return self.by_hash.get(str(file_hash).strip().upper())

    def get_by_sha1(self, sha1: str) -> Optional[Any]:
        return self.get_by_hash(sha1)

    def get_by_season_episode(self, season: int, episode: int) -> Optional[Any]:
        return self.by_season_episode.get((season, episode))


class PostprocessBatchContext:
    """后处理单次批量执行会话上下文。"""

    def __init__(
            self,
            pending: Dict[str, Dict[str, Any]],
            pending_snapshot: Dict[str, Dict[str, Any]],
            due_keys: List[str],
            now: float,
            monitor_token: str,
            task_map: Dict[str, Any],
            tasks_valid: bool,
            subscribe_cache: Dict[int, Any],
    ):
        self.pending = pending
        self.pending_snapshot = pending_snapshot
        self.due_keys = due_keys
        self.now = now
        self.monitor_token = monitor_token
        self.task_map = task_map
        self.tasks_valid = tasks_valid
        self.subscribe_cache = subscribe_cache

        self.completed: int = 0
        self.failed: int = 0
        self.committed_keys: Set[str] = set()

        self.directory_snapshots: Dict[str, Tuple[bool, DirectoryFileIndex]] = {}
        self.media_context_cache: Dict[Tuple[Any, ...], Tuple[Any, Dict[str, Any]]] = {}
        self.prepared_files: Dict[str, Any] = {}
        self.moved_files: Dict[str, Any] = {}
        self.upgrade_delete_batch: Dict[str, Dict[str, Any]] = {}
        self.subscription_batches: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.finalized_details: List[Dict[str, Any]] = []
        self.notification_contexts: List[Tuple[Dict[str, Any], str]] = []

    def sync_rename(self, dir_path: str, old_file: Any, new_file: Any) -> None:
        """重命名成功后直接在内存索引中同步更新，避免向网盘重新发请求。"""
        normalized_dir = str(dir_path or "").rstrip("/")
        if normalized_dir in self.directory_snapshots:
            valid, index = self.directory_snapshots[normalized_dir]
            if valid and isinstance(index, DirectoryFileIndex):
                index.remove_file(old_file)
                index.add_file(new_file)

    def sync_move(self, src_dir: str, dst_dir: str, file_obj: Any) -> None:
        """移动成功后跨目录同步内存索引，免除双方目录的网络二次扫描。"""
        src_norm = str(src_dir or "").rstrip("/")
        dst_norm = str(dst_dir or "").rstrip("/")
        if src_norm in self.directory_snapshots:
            valid, index = self.directory_snapshots[src_norm]
            if valid and isinstance(index, DirectoryFileIndex):
                index.remove_file(file_obj)
        if dst_norm in self.directory_snapshots:
            valid, index = self.directory_snapshots[dst_norm]
            if valid and isinstance(index, DirectoryFileIndex):
                index.add_file(file_obj)


class PostprocessService(OwnerDelegator):
    """监控待处理文件并完成重命名、STRM和历史状态更新。"""

    _POSTPROCESS_STEPS = (
        ("locate", "检查并定位文件"),
        ("organize", "重命名与移动"),
        ("strm", "生成 STRM"),
        ("subtitle", "处理字幕"),
        ("metadata", "刮削元数据"),
        ("commit", "登记完成状态"),
        ("notify", "消息通知"),
    )

    _FINALIZE_DEAD_LOG = "文件后处理连续失败 {} 次，已终止重试：{}"
    _FINALIZE_DEAD_REASON = "文件后处理连续失败 {} 次，已停止自动重试"
    _FINALIZE_DEAD_TITLE = "网盘文件后处理失败"
    _FINALIZE_DEAD_TEXT = (
        "{}\n\n连续 {} 次后处理失败，已停止自动重试，"
        "请检查网盘文件与媒体目录状态"
    )

    def _supported_hash_algorithms(self) -> frozenset[str]:
        """动态获取当前目标网盘支持的文件哈希校验算法集合。"""
        cloud_drive = getattr(self, "_cloud_drive", None)
        if cloud_drive and cloud_drive.supports(CloudDriveCapability.RAPID_UPLOAD):
            try:
                rapid = cloud_drive.require(CloudDriveCapability.RAPID_UPLOAD)
                algorithms = getattr(rapid, "algorithms", None)
                if algorithms:
                    return frozenset(str(a).lower() for a in algorithms)
            except Exception:
                logger.debug("_supported_hash_algorithms 分支忽略异常", exc_info=True)
        return frozenset({"sha1", "md5"})

    def _cloud_directory_snapshot(
            self,
            cloud_dir: str,
            cache: Optional[Dict[str, Tuple[bool, DirectoryFileIndex]]] = None,
    ) -> Tuple[bool, DirectoryFileIndex]:
        """获取并缓存网盘目录下的文件双索引。"""
        normalized_dir = str(cloud_dir or "").rstrip("/")
        if cache is not None and normalized_dir in cache:
            return cache[normalized_dir]
        lookup = self._cloud_directories.resolve_directory(normalized_dir)
        algorithms = self._supported_hash_algorithms()
        if not lookup.checked:
            result = (False, DirectoryFileIndex(algorithms=algorithms))
        elif lookup.directory_id is None:
            result = (True, DirectoryFileIndex(algorithms=algorithms))
        else:
            listing = self._cloud_directories.list_directory(lookup.directory_id)
            if not listing.checked:
                result = (False, DirectoryFileIndex(algorithms=algorithms))
            else:
                result = (True, DirectoryFileIndex(listing.files, algorithms=algorithms))
        if cache is not None:
            cache[normalized_dir] = result
        return result

    @staticmethod
    def _postprocess_task_id(item: Dict[str, Any]) -> str:
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return f"subscribe:{subscribe_id}"
        sub_key = str(item.get("sub_key") or "").strip()
        return f"media:{sub_key}" if sub_key else ""

    def _postprocess_steps(
            self, item: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, str]]:
        item = item or {}
        task_type = str(item.get("task_type") or "share").strip().lower()
        waiting_offline = task_type in {"magnet", "ed2k", "offline"} and not bool(
            item.get("offline_completed") or item.get("moved_at")
        )
        has_subtitles = bool((item or {}).get("subtitles"))
        strm_enabled = bool(
            self._strm_generate_enabled
            and self._strm_generator
            and self._local_resource_path
        )
        metadata_enabled = bool(
            self._metadata_scraper
            and self._local_resource_path
            and (self._nfo_scrape_enabled or self._image_scrape_enabled)
        )
        return [
            {
                "key": key,
                "label": "等待离线下载" if key == "locate" and waiting_offline else label,
            }
            for key, label in self._POSTPROCESS_STEPS
            if not (key == "strm" and not strm_enabled)
               and not (key == "subtitle" and not has_subtitles)
               and not (key == "metadata" and not metadata_enabled)
               and not (key == "notify" and not self._notify)
        ]

    def _update_postprocess_progress(
            self,
            item: Dict[str, Any],
            pending_key: str,
            step: str,
            detail: str = "",
    ) -> None:
        """通过现有任务运行态推送当前文件和处理步骤。"""
        if not self._postprocess_task_update:
            return
        task_id = self._postprocess_task_id(item)
        if not task_id:
            return
        steps = self._postprocess_steps(item)
        step_index = next(
            (
                index for index, value in enumerate(steps)
                if value["key"] == step
            ),
            None,
        )
        if step_index is None:
            return
        self._postprocess_task_update(
            task_id,
            _pending_key=pending_key,
            current_file=str(item.get("file_name") or "").strip(),
            postprocess_active=True,
            postprocess_detail=str(detail or "").strip(),
            postprocess_step=step,
            postprocess_step_index=step_index,
            postprocess_step_total=len(steps),
            postprocess_steps=steps,
        )

    def _cleanup_failed_offline_task(
            self, item: Dict[str, Any], reason: str
    ) -> None:
        """失败后仅记录日志，保留离线任务及已下载文件，避免误删用户云端资产。"""
        task_id = str(item.get("task_id") or "").strip().upper()
        if not task_id:
            return
        logger.warning(
            f"Magnet 后处理未完成，保留离线任务及已下载文件：{task_id}，原因：{reason}"
        )

    @staticmethod
    def _upgrade_backup_name(file_name: str, task_id: str) -> str:
        """仅在原文件名后追加短任务 ID，避免隐藏文件和冗长标记。"""
        source = Path(str(file_name or ""))
        short_id = "".join(
            value for value in str(task_id or "") if value.isalnum()
        )[:10]
        if not short_id:
            short_id = uuid.uuid4().hex[:10]
        return f"{source.stem}-{short_id}{source.suffix}"

    def _due_pending_keys(
            self,
            pending: Dict[str, Dict[str, Any]],
            now: float,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[str]:
        selected = set(pending_keys or [])
        return [
            key
            for key, item in pending.items()
            if (not selected or key in selected)
               and (bool(selected and key in selected) or now >= float(item.get("_monitor_until") or 0))
               and (force or now >= float(item.get("next_check_at") or 0))
        ]

    @staticmethod
    def _media_context_key(item: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return "subscribe", subscribe_id
        sub_key = str(item.get("sub_key") or "").strip()
        if sub_key:
            return "sub_key", sub_key
        media_data = item.get("mediainfo") or {}
        media_id = (
                media_data.get("tmdb_id")
                or media_data.get("douban_id")
                or media_data.get("media_id")
        )
        if not media_id:
            return None
        return (
            "media",
            str(media_data.get("type") or ""),
            str(media_id),
            int(item.get("season") or 0),
        )

    @staticmethod
    def _offline_media_group_key(
            item: Dict[str, Any], pending_key: str
    ) -> Tuple[Any, ...]:
        media_data = item.get("mediainfo") or {}
        media_type = str(media_data.get("type") or item.get("type") or "")
        media_id = (
                media_data.get("tmdb_id")
                or media_data.get("douban_id")
                or media_data.get("media_id")
        )
        if media_id:
            return "media", media_type, str(media_id)
        title = str(media_data.get("title") or item.get("title") or "").strip()
        if title:
            return (
                "title",
                media_type,
                title.casefold(),
                str(media_data.get("year") or item.get("year") or ""),
            )
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return "subscribe", subscribe_id
        sub_key = str(item.get("sub_key") or "").strip()
        if sub_key:
            return "sub_key", sub_key
        return "pending", pending_key

    def get_due_offline_task_groups(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按媒体聚合当前到期任务；同一媒体由单个工作线程顺序处理。"""
        if not self._get_data:
            return []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
        now = time.time()
        due_keys = self._due_pending_keys(
            pending, now, force=force, pending_keys=pending_keys
        )
        groups: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for pending_key in due_keys:
            item = pending[pending_key]
            group_key = self._offline_media_group_key(item, pending_key)
            group = groups.setdefault(group_key, {
                "pending_keys": set(),
                "needs_offline": False,
                "task_ids": set(),
            })
            group["pending_keys"].add(pending_key)
            group["needs_offline"] = group["needs_offline"] or str(
                item.get("task_type") or "share"
            ) in {"ed2k", "magnet"}
            task_id = str(item.get("task_id") or "").strip().upper()
            if task_id:
                group["task_ids"].add(task_id)
        return list(groups.values())

    @staticmethod
    def _strm_file_ready(strm_path: Optional[Path]) -> bool:
        try:
            return bool(
                strm_path
                and strm_path.is_file()
                and strm_path.stat().st_size > 0
            )
        except OSError:
            return False

    def _delete_upgrade_old_strm(
            self,
            item: Dict[str, Any],
            replacement_path: Optional[Path] = None,
    ) -> None:
        """新 STRM 就绪后清理路径不同的旧版本 STRM。"""
        if not self._strm_generator or not self._local_resource_path:
            return
        old_dir = str(item.get("upgrade_old_cloud_dir") or "").strip()
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        if not old_dir or not old_name:
            return
        try:
            strm_path = self._strm_generator.local_path(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=old_dir,
                file_name=old_name,
            )
            if replacement_path and (
                    strm_path.resolve(strict=False)
                    == replacement_path.resolve(strict=False)
            ):
                return
            if strm_path.is_file():
                strm_path.unlink()
                logger.debug(f"洗版清理旧 STRM：{strm_path}")
        except (OSError, ValueError) as error:
            logger.debug(f"洗版清理旧 STRM 失败：{old_dir}/{old_name}，{error}", exc_info=True)

    def _dedupe_moved_duplicate(
            self,
            final_dir: str,
            file_name: str,
            moved_file: Any,
    ) -> Any:
        """移动后兜底：115 同名自动改名为「xxx (1).ext」时，删掉同内容副本。

        并发中继/两个整理任务同时移动同一集时，115 不会覆盖而是生成 (1)。
        只要基础名文件仍在、大小与哈希一致，就保留基础名文件，删除刚生成的副本。
        """
        moved_name = str(getattr(moved_file, "name", "") or "")
        if not moved_file or moved_name == file_name:
            return moved_file
        base_stem = Path(file_name).stem
        suffix = Path(file_name).suffix
        if (
                not moved_name.startswith(f"{base_stem} (")
                or Path(moved_name).suffix.lower() != suffix.lower()
        ):
            return moved_file
        valid, index = self._cloud_directory_snapshot(final_dir)
        base_file = index.get(file_name) if valid else None
        if not base_file or str(getattr(base_file, "id", "")) == str(getattr(moved_file, "id", "")):
            return moved_file
        moved_size = int(getattr(moved_file, "size", 0) or 0)
        base_size = int(getattr(base_file, "size", 0) or 0)
        same_size = bool(moved_size and base_size and abs(moved_size - base_size) <= 1024)
        same_hash = False
        hash_compared = False
        for algo in self._supported_hash_algorithms():
            left = str(getattr(moved_file, algo, "") or "").strip().upper()
            right = str(getattr(base_file, algo, "") or "").strip().upper()
            if left and right:
                hash_compared = True
                if left == right:
                    same_hash = True
                    break
        # 有哈希时以哈希为准；只有两边都拿不到哈希才退回大小比较。
        same_content = same_hash if hash_compared else same_size
        if not same_content:
            logger.warning(
                f"目标目录已有同名文件但内容不同，保留两份待人工确认："
                f"{final_dir}/{file_name} 与 {moved_name}"
            )
            return moved_file
        if self._cloud_mutations.delete_file(getattr(moved_file, "id", "")):
            logger.info(
                f"已清理并发移动产生的同内容副本：{final_dir}/{moved_name}"
            )
            return base_file
        logger.warning(f"同内容副本清理失败，保留待重试：{final_dir}/{moved_name}")
        return moved_file

    def _replace_upgrade_file(
            self,
            item: Dict[str, Any],
            pending_key: str,
            target_file: Any,
            staging_dir: str,
            file_name: str,
            now: float,
            directory_snapshot,
    ) -> Optional[Any]:
        """将新文件移入目标位置；旧文件仅临时避让，等待最终提交删除。"""
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        old_dir = str(item.get("upgrade_old_cloud_dir") or final_dir).rstrip("/") or "/"
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        old_id = str(item.get("upgrade_old_file_id") or "").strip()
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        old_file = None
        if old_id or old_name:
            old_valid, old_index = directory_snapshot(old_dir)
            if old_valid:
                old_file = next(
                    (value for value in old_index.values()
                     if old_id and str(getattr(value, "id", "")) == old_id),
                    None,
                )
                old_file = old_file or old_index.get(old_name)

        if not item.get("upgrade_old_backed_up") and old_file and (
                str(getattr(old_file, "id", "")) != str(getattr(target_file, "id", ""))
        ):
            backup_name = backup_name or (
                self._upgrade_backup_name(
                    old_name, item.get("task_id") or pending_key
                )
            )
            logger.debug(
                f"洗版临时备份旧文件：{old_dir}/{old_name} -> {backup_name}"
            )
            if not self._cloud_mutations.rename_file(old_dir, old_file, backup_name):
                logger.debug(f"洗版替换无法备份旧文件：{old_dir}/{old_name}")
                return None
            item["upgrade_old_backed_up"] = True
            item["upgrade_backup_name"] = backup_name
            item["upgrade_old_file_id"] = str(getattr(old_file, "id", "") or old_id)

        if not item.get("moved_at"):
            if target_file.name != file_name:
                if not self._cloud_mutations.rename_file(staging_dir, target_file, file_name):
                    if item.get("upgrade_old_backed_up") and old_file:
                        if self._cloud_mutations.rename_file(old_dir, old_file, old_name):
                            item.pop("upgrade_old_backed_up", None)
                        else:
                            logger.error(
                                f"洗版新文件重命名失败且旧文件恢复失败：{old_dir}/{backup_name}"
                            )
                    self._schedule_finalize_retry(item, now)
                    return None
                item["staging_name"] = file_name
                target_file = self._cloud_query.get_cached_file(staging_dir, file_name)
                if not target_file:
                    self._schedule_finalize_retry(item, now)
                    return None
            moved_file = (
                target_file if staging_dir == final_dir
                else self._cloud_mutations.move_file(target_file, final_dir, file_name)
            )
            if not moved_file:
                if item.get("upgrade_old_backed_up") and old_file:
                    if self._cloud_mutations.rename_file(old_dir, old_file, old_name):
                        item.pop("upgrade_old_backed_up", None)
                    else:
                        logger.error(
                            f"洗版新文件移动失败且旧文件恢复失败：{old_dir}/{backup_name}"
                        )
                self._schedule_finalize_retry(item, now)
                return None
            item["moved_at"] = now
            target_file = self._dedupe_moved_duplicate(
                final_dir, file_name, moved_file
            )

        return target_file

    def _upgrade_old_file_id(
            self,
            item: Dict[str, Any],
            directory_snapshot,
    ) -> str:
        """解析已备份旧文件的真实 ID，供单个或批量回收复用。"""
        if item.get("upgrade_old_deleted") or not item.get("upgrade_old_backed_up"):
            return ""
        old_dir = str(
            item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        backup_id = str(item.get("upgrade_old_file_id") or "").strip()
        if backup_id:
            return backup_id
        if not backup_name:
            return ""
        _, backup_index = directory_snapshot(old_dir)
        backup_file = backup_index.get(backup_name)
        return str(getattr(backup_file, "id", "") or "") if backup_file else ""

    def _delete_upgrade_old_file(
            self,
            item: Dict[str, Any],
            directory_snapshot,
    ) -> bool:
        """新文件及 STRM 就绪后，提交洗版并删除临时避让的旧文件。"""
        if item.get("upgrade_old_deleted") or not item.get("upgrade_old_backed_up"):
            return True
        old_dir = str(
            item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        backup_id = self._upgrade_old_file_id(item, directory_snapshot)
        if backup_id and self._cloud_mutations.delete_file(backup_id):
            item["upgrade_old_deleted"] = True
        if item.get("upgrade_old_deleted"):
            logger.debug(f"洗版完成，已删除旧文件：{old_dir}/{backup_name}")
            return True
        logger.debug(f"洗版新文件已就绪，旧文件删除待重试：{old_dir}/{backup_name}")
        return False

    def _prepare_postprocess_context(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
            offline_tasks: Optional[List[Dict[str, Any]]] = None,
            offline_tasks_valid: Optional[bool] = None,
    ) -> Optional[PostprocessBatchContext]:
        """初始化后处理批次会话上下文，完成到期任务筛选与租约锁定。"""
        if (
                not self._get_data
                or not self._cloud_directories
                or not self._cloud_query
                or not self._cloud_mutations
        ):
            return None
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if not pending:
                return None

            now = time.time()
            due_keys = self._due_pending_keys(
                pending, now, force=force, pending_keys=pending_keys
            )
            if not due_keys:
                return PostprocessBatchContext(
                    pending=pending,
                    pending_snapshot=copy.deepcopy(pending),
                    due_keys=[],
                    now=now,
                    monitor_token="",
                    task_map={},
                    tasks_valid=False,
                    subscribe_cache={},
                )

            monitor_token = uuid.uuid4().hex
            for key in due_keys:
                item = pending[key]
                item["_monitor_token"] = monitor_token
                item["_monitor_until"] = now + self._OFFLINE_MONITOR_LEASE_SECONDS
            self._save_offline_pending(pending)
            pending_snapshot = copy.deepcopy(pending)

        subscribe_ids = {
            int((pending.get(key) or {}).get("subscribe_id") or 0)
            for key in due_keys
            if int((pending.get(key) or {}).get("subscribe_id") or 0) > 0
        }
        subscribe_cache: Dict[int, Any] = {}
        if subscribe_ids:
            try:
                with SessionFactory() as db:
                    subscribes = db.query(Subscribe).filter(
                        Subscribe.id.in_(sorted(subscribe_ids))
                    ).all()
                    subscribe_cache = {
                        int(subscribe.id): subscribe for subscribe in subscribes
                    }
                for subscribe_id in subscribe_ids - set(subscribe_cache):
                    subscribe_cache[subscribe_id] = None
            except Exception as error:
                logger.debug(f"批量读取后处理订阅失败，将按需查询：{error}", exc_info=True)

        needs_offline = any(
            str((pending.get(key) or {}).get("task_type") or "share")
            in {"ed2k", "magnet", "offline"}
            for key in due_keys
        )
        tasks = offline_tasks
        if needs_offline and tasks is None and self._offline_tasks:
            snapshot = self._offline_tasks.get_offline_task_list_snapshot(
                force=True,
            )
            tasks = snapshot.get("tasks") or []
            offline_tasks_valid = bool(snapshot.get("refresh_ok"))
        tasks_valid = bool(offline_tasks_valid)
        task_map: Dict[str, Any] = {}
        for task in (tasks or []):
            task_id = str(task.get("id") or "").upper()
            if task_id:
                task_map[task_id] = task
            native_id = str(task.get("native_id") or "").upper()
            if native_id and native_id not in task_map:
                task_map[native_id] = task

        return PostprocessBatchContext(
            pending=pending,
            pending_snapshot=pending_snapshot,
            due_keys=due_keys,
            now=now,
            monitor_token=monitor_token,
            task_map=task_map,
            tasks_valid=tasks_valid,
            subscribe_cache=subscribe_cache,
        )

    @staticmethod
    def _queue_subscription_completion(
            item: Dict[str, Any],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """记录已完成的订阅集数，供批次结束时聚合更新订阅进度。"""
        if item.get("transient_target"):
            return
        episode_values = (
                item.get("success_episodes")
                or item.get("notification_episodes")
                or item.get("target_episodes")
                or ([item.get("episode")] if item.get("episode") else [])
        )
        episodes = set()
        for value in episode_values:
            try:
                episode = int(value or 0)
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.add(episode)
        if not media or not episodes:
            return
        key = (
            int(item.get("subscribe_id") or 0),
            int(getattr(media, "tmdb_id", 0) or 0),
            int(item.get("season") or 0),
            str(item.get("task_type") or "share").strip().lower(),
        )
        batch = ctx.subscription_batches.setdefault(key, {
            "item": copy.deepcopy(item),
            "mediainfo": media,
            "media_data": dict(media_data or {}),
            "episodes": set(),
        })
        batch["episodes"].update(episodes)

    def _extract_item_hash(self, item: Dict[str, Any]) -> str:
        """根据当前目标网盘动态支持的文件算法，从待办项中提取匹配的文件哈希。"""
        for algo in self._supported_hash_algorithms():
            val = item.get(f"source_{algo}") or item.get(algo)
            if val:
                return str(val).strip().upper()
        for fallback in ("source_sha1", "sha1", "source_md5", "md5"):
            val = item.get(fallback)
            if val:
                return str(val).strip().upper()
        return ""

    def _match_cloud_file_in_index(
            self,
            file_index: Any,
            candidate_names: List[str],
            target_hash: str,
            item: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """在已取到的文件索引里按 1–4 策略匹配（Phase 3a 第 5 项：
        单层快照与递归树共用的同一套匹配逻辑）。

        1. 精确名称 O(1)；2. 哈希 O(1)；3. 边界前后缀与网盘重名副本
        容错（如 URL 截断名、特殊编码名或 115 自动生成的 "(1)"、
        "_1" 副本）；4. 剧集季集 O(1) 索引智能定位。
        """
        # 1. 精确名称查找 (O(1))
        for name in candidate_names:
            if name and name in file_index:
                return file_index[name]

        # 2. 哈希查找 (O(1))
        clean_hash = str(target_hash or "").strip().upper()
        if clean_hash and hasattr(file_index, "get_by_hash"):
            found = file_index.get_by_hash(clean_hash)
            if found:
                return found

        # 3. 边界前后缀与网盘重名副本容错匹配 (如 URL 截断名、特殊编码名或 115 自动生成的 "(1)"、"_1" 副本)
        for candidate_name in candidate_names:
            if not candidate_name:
                continue
            cand_stem = Path(candidate_name).stem.lower()
            cand_ext = Path(candidate_name).suffix.lower()
            for fname, fobj in file_index.items():
                if candidate_name.endswith(fname) or fname.endswith(candidate_name):
                    return fobj
                fname_stem = Path(fname).stem.lower()
                fname_ext = Path(fname).suffix.lower()
                if cand_stem and fname_stem and (fname_stem.startswith(cand_stem) or cand_stem.startswith(fname_stem)):
                    if not cand_ext or not fname_ext or cand_ext == fname_ext:
                        return fobj

        # 4. 剧集季集 O(1) 索引智能定位 (避免重复对所有文件循环正则解析)
        if item:
            target_season = item.get("season")
            target_eps = item.get("success_episodes") or item.get("notification_episodes")
            target_ep = target_eps[0] if isinstance(target_eps, list) and target_eps else item.get("episode")
            if target_season is None or target_ep is None:
                for cand in candidate_names:
                    if cand:
                        se = MediaFileParser.extract_season_episode(cand)
                        if se:
                            if target_season is None:
                                target_season = se[0]
                            if target_ep is None:
                                target_ep = se[1]
                            break
            if target_season is not None and target_ep is not None:
                try:
                    t_season_int = int(target_season)
                    t_ep_int = int(target_ep)
                    if hasattr(file_index, "get_by_season_episode"):
                        found = file_index.get_by_season_episode(t_season_int, t_ep_int)
                        if found:
                            return found
                except (ValueError, TypeError):
                    logger.debug("_locate_cloud_file 分支忽略异常", exc_info=True)

        return None

    def _locate_cloud_files_in_tree(
            self, root_path: str, max_depth: int = 6
    ) -> List[Any]:
        """递归列出某目录树下的全部文件（Phase 3a 第 5 项：递归树定位
        的唯一原语；_locate_cloud_file 的递归策略与 magnet finalize
        共用）。列目录失败时返回 []（不抛），调用方按“没找到”处理。
        """
        root = str(root_path or "").strip()
        if not root:
            return []
        try:
            files = self._cloud_query.list_files_recursive(
                root, max_depth=max_depth
            )
        except Exception as error:
            logger.debug(f"递归列目录失败：{root}，{error}", exc_info=True)
            return []
        return list(files or [])

    def _locate_cloud_file(
            self,
            dir_path: str,
            candidate_names: List[str],
            target_hash: str,
            ctx: PostprocessBatchContext,
            item: Optional[Dict[str, Any]] = None,
            dest_path: Optional[str] = None,
            recursive: bool = False,
    ) -> Optional[Any]:
        """在指定网盘目录下定位目标物理文件（Phase 3a 第 5 项：唯一实现）。

        策略按顺序、命中即返：
        0. dest 策略（dest_path 非空时）：只对 dest 父目录取快照、按
           dest 文件名精确查；查不到直接返回 None，不进后续策略
           （dest 是 MP 历史的精确路径，模糊匹配会抓错文件）。
        1–4. 单层快照匹配（见 _match_cloud_file_in_index：精确名 /
           哈希 / 前后缀容错 / 季集索引）。
        5. 递归树策略（recursive=True 时）：递归列出 dir_path 下的
           文件树，对树里的文件复用同一套 1–4 策略。
        """
        if dest_path:
            dest = str(dest_path or "").strip()
            if not dest:
                return None
            dest_parent = str(Path(dest).parent)
            dest_name = Path(dest).name
            if not dest_name:
                return None
            valid, file_index = self._cloud_directory_snapshot(
                dest_parent, ctx.directory_snapshots
            )
            if not valid or not file_index or dest_name not in file_index:
                return None
            return file_index[dest_name]
        if not dir_path:
            return None
        valid, file_index = self._cloud_directory_snapshot(dir_path, ctx.directory_snapshots)
        # 只看 valid：空目录的快照是空 dict（falsy），但它是有效实测数据，
        # 仍要进递归树策略（文件可能在子目录里）；无效快照才直接返回。
        if not valid or file_index is None:
            return None
        found = self._match_cloud_file_in_index(
            file_index, candidate_names, target_hash, item
        )
        if found is not None:
            return found
        if not recursive:
            return None
        tree_files = self._locate_cloud_files_in_tree(dir_path)
        if not tree_files:
            return None
        return self._match_cloud_file_in_index(
            DirectoryFileIndex(tree_files), candidate_names, target_hash, item
        )

    def _mp_organize_tracker(self) -> Any:
        """返回 MP 整理轮询器（每轮 finalize 新建）。

        MP 侧调用很便宜：历史是单条精确查询，队列是内存列表。
        """
        from ...core.services.mp_organize_tracker import MpOrganizeTracker

        return MpOrganizeTracker()

    def _locate_cloud_file_by_dest(
            self, dest_path: str, ctx: PostprocessBatchContext
    ) -> Optional[Any]:
        """按 MP 历史 dest 的完整嵌套路径直查文件。

        薄包装：唯一实现见 _locate_cloud_file 的 dest 策略（精确路径，
        不进模糊/季集兜底）。MP 整理出的文件在“剧名/Season 1/”两层
        子目录下，平铺扫描看不见；这里只对 dest 的父目录取一次单层
        快照、按精确文件名查。
        """
        return self._locate_cloud_file(
            "", [], "", ctx, dest_path=dest_path
        )

    def _has_existing_cloud_file(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> bool:
        """检查任务对应的物理文件是否已在暂存目录或最终媒体目录就位，命中时复用对象避免二次定位。"""
        task = ctx.task_map.get(str(item.get("task_id") or pending_key).upper())
        if bool(item.get("moved_at") or (task and task.get("completed"))):
            return True
        cached_file = ctx.moved_files.get(pending_key) or ctx.prepared_files.get(pending_key)
        if cached_file:
            return True
        staging_dir = str(item.get("staging_dir") or item.get("cloud_dir") or "/").rstrip("/") or "/"
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        task_name = str((task or {}).get("name") or "").strip()
        candidate_names = [
            str(item.get("file_name") or ""),
            str(item.get("staging_name") or ""),
            task_name,
            extract_ed2k_filename(str(item.get("share_url") or "")),
        ]
        target_hash = self._extract_item_hash(item)
        found = self._locate_cloud_file(staging_dir, candidate_names, target_hash, ctx, item=item)
        if found:
            ctx.prepared_files[pending_key] = found
            return True
        if final_dir != staging_dir:
            found = self._locate_cloud_file(final_dir, candidate_names, target_hash, ctx, item=item)
            if found:
                ctx.moved_files[pending_key] = found
                item["moved_at"] = ctx.now
                item["staging_dir"] = final_dir
                return True
        return False

    def _pending_item_to_terminal_record(
            self, item: Dict[str, Any], pending_key: str, status: str
    ) -> Optional[Dict[str, Any]]:
        """把后处理项拼成 history 形状的记录 dict，供终态写入重试索引。

        字段只取后处理项自身 + 序列化 mediainfo + 分享平台推导的跨盘信息
        （与 history 建记录时同一口径，history.py:122-141）；share_url 或
        file_name 缺失时算不出 record_id，返回 None。
        """
        share_url = str(item.get("share_url") or "").strip()
        file_name = str(item.get("file_name") or "").strip()
        if not share_url or not file_name:
            return None
        media = item.get("mediainfo") or {}
        if not isinstance(media, dict):
            media = {}
        media_type = media.get("type")
        if hasattr(media_type, "value"):
            media_type = media_type.value
        created_at = float(item.get("created_at") or 0)
        record_time = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
            if created_at > 0 else ""
        )
        record: Dict[str, Any] = {
            "time": record_time,
            "share_url": share_url,
            "file_name": file_name,
            "source_file_name": str(item.get("staging_name") or file_name),
            "file_size": int(item.get("file_size") or 0),
            "source_sha1": str(item.get("source_sha1") or ""),
            "source_md5": "",
            "tmdb_id": media.get("tmdb_id"),
            "title": str(media.get("title") or item.get("title") or file_name),
            "type": str(media_type or ""),
            "season": item.get("season"),
            "episode": item.get("episode"),
            "year": str(media.get("year") or "") or None,
            "cloud_dir": str(item.get("cloud_dir") or ""),
            "finalize_key": pending_key,
            "status": status,
            "stage": "",
            "transfer_mode": "",
            "source_drive_key": "",
            "target_drive_key": "",
        }
        try:
            source_provider = self._owner.resource_transfer._resource_provider_for_url(share_url)
            target_drive = getattr(self, "_cloud_drive", None)
            if (
                    source_provider is not None
                    and target_drive is not None
                    and getattr(source_provider, "key", None)
                    and getattr(target_drive, "key", None)
                    and source_provider.key != target_drive.key
            ):
                record.update({
                    "transfer_mode": "cross",
                    "source_drive_key": str(source_provider.key),
                    "target_drive_key": str(target_drive.key),
                })
        except Exception as error:  # noqa: BLE001 - 跨盘信息拿不到时按同盘处理
            logger.debug(f"推导跨盘信息失败，按同盘处理：{error}", exc_info=True)
        return record

    def _write_terminal_state(
            self, item: Dict[str, Any], pending_key: str, status: str,
            reason: str = "", sync_monitor: bool = True,
    ) -> bool:
        """写后处理终态（§4.4）：成功不再写插件 history——MP 整理历史是
        唯一载体；失败写进 retry_index（只写可重试的）；跨盘成功但落盘
        自检未通过写一条修复行。始终返回 True——结算不再因找不到历史
        记录而重排队，旧 _resolve_history_record 的 10 次重排队与补写
        逻辑随 history 一起删除。

        Phase 4 第 7 项：终态同时回写频道监控（_sync_finalize_outcome_to_
        monitor），成功/失败都写；sync_monitor=False 只用于用户主动删
        除/停止的任务——那不是 finalize 的终态，不应改监控条目。

        Phase 4 第 1 项：重试索引的终态写入只允许三处（本方法是其一）——
        ① finalize 终态（本方法）、② 同步结果落盘 persist_sync_results、
        ③ 重试执行（retry.py 的 _upsert_retry_index_row /
        _delete_retry_index_row）。_reconcile_pending_state 只同步
        pending 存储的字段差分，绝不碰重试索引；不要在这里之外再加
        第四处写入，否则“失败”会在展示面与索引里反复横跳。
        """
        if sync_monitor:
            self._sync_finalize_outcome_to_monitor(item, status, reason)
        if status == "成功":
            self._delete_terminal_index_row(item, pending_key)
            self._write_repair_row_if_unlanded(item, pending_key)
            return True
        record = self._pending_item_to_terminal_record(item, pending_key, "失败")
        if record is None:
            logger.warning(
                f"终态失败无法写入重试索引（缺 share_url/file_name）："
                f"{item.get('file_name') or pending_key}"
            )
            return True
        apply_failure(record, reason or "转存失败")
        if not failure_retryable_of(record):
            return True
        try:
            from ...core.retry_index import record_to_index_row
        except Exception as error:  # noqa: BLE001 - 索引模块不可用时只记日志
            logger.debug(f"重试索引模块不可用，失败终态未写入：{error}", exc_info=True)
            return True
        row = record_to_index_row(record, "失败")
        if row is None:
            return True
        try:
            get_store = getattr(self, "_get_data_store", None)
            if callable(get_store):
                get_store().repositories.retry_index.upsert_row(row)
        except Exception as error:  # noqa: BLE001 - 索引写失败不阻塞终态结算
            logger.debug(f"失败终态写重试索引失败：{error}", exc_info=True)
        return True

    def _sync_finalize_outcome_to_monitor(
            self, item: Dict[str, Any], status: str, reason: str = "",
    ) -> None:
        """finalize 终态回写频道监控（Phase 4 第 7 项）。

        按 pending 项的 share_url 找监控条目；监控不可用（无插件宿主、
        监控未初始化、回写抛异常）时静默跳过，绝不阻塞终态结算。
        share_url 为空的项（本地文件、手动提交无链接等）没有监控条目
        可写，直接返回。
        """
        share_url = str(item.get("share_url") or "").strip()
        if not share_url:
            return
        plugin = getattr(self, "_plugin", None)
        if plugin is None:
            return
        try:
            from ...core.tg_monitor import get_channel_monitor

            monitor = get_channel_monitor(plugin)
            monitor.apply_finalize_outcome(
                share_url,
                int(item.get("subscribe_id") or 0),
                success=(status == "成功"),
                reason=reason,
                file_name=str(item.get("file_name") or ""),
            )
        except Exception as error:  # noqa: BLE001 - 监控回写失败不阻塞终态
            logger.debug(f"finalize 终态回写频道监控失败：{error}", exc_info=True)

    def _delete_terminal_index_row(
            self, item: Dict[str, Any], pending_key: str
    ) -> None:
        """成功终态：删掉该记录在重试索引里的旧行（失败/处理中行已无
        重试价值）。删行失败不阻塞结算；跨盘待修复行由
        _write_repair_row_if_unlanded 随后重新 upsert。"""
        record = self._pending_item_to_terminal_record(item, pending_key, "成功")
        if record is None:
            return
        try:
            from ...core.retry_index import retry_record_id
            record_id = retry_record_id(record)
            if not record_id:
                return
            get_store = getattr(self, "_get_data_store", None)
            if callable(get_store):
                get_store().repositories.retry_index.delete_by_record_ids(
                    [record_id]
                )
        except Exception as error:  # noqa: BLE001 - 删行失败不阻塞成功结算
            logger.debug(f"成功终态删除重试索引旧行失败：{error}", exc_info=True)

    def _write_repair_row_if_unlanded(
            self, item: Dict[str, Any], pending_key: str
    ) -> None:
        """跨盘成功终态：落盘自检未通过时写一条修复行进重试索引
        （status 成功、landed False，供瘦页重试）。同盘成功不写——
        MP 整理历史是唯一载体；自检拿不到实测数据（landed None）也不写。"""
        record = self._pending_item_to_terminal_record(item, pending_key, "成功")
        if record is None or str(record.get("transfer_mode") or "") != "cross":
            return
        verify = getattr(self._owner.landed_verify, "verify_landed_records", None)
        if not callable(verify):
            return
        try:
            infos = verify([record]) or {}
        except Exception as error:  # noqa: BLE001 - 自检失败不阻塞成功结算
            logger.debug(f"落盘自检失败，跳过修复行写入：{error}", exc_info=True)
            return
        info = infos.get(id(record)) or {}
        if info.get("landed") is not False:
            return
        record["landed"] = False
        try:
            from ...core.retry_index import record_to_index_row
        except Exception as error:  # noqa: BLE001 - 索引模块不可用时只记日志
            logger.debug(f"重试索引模块不可用，修复行未写入：{error}", exc_info=True)
            return
        row = record_to_index_row(record, "成功")
        if row is None:
            return
        try:
            get_store = getattr(self, "_get_data_store", None)
            if callable(get_store):
                get_store().repositories.retry_index.upsert_row(row)
                logger.warning(
                    f"跨盘转存成功但落盘自检未通过，已记修复行："
                    f"{record.get('file_name') or pending_key}"
                )
        except Exception as error:  # noqa: BLE001 - 索引写失败不阻塞成功结算
            logger.debug(f"修复行写重试索引失败：{error}", exc_info=True)

    def get_pending_finalize_tasks(self) -> List[Dict[str, Any]]:
        """返回等待115文件就绪、重命名或生成STRM的持久任务。"""
        if not self._get_data:
            return []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            return [copy.deepcopy({**item, "pending_key": key}) for key, item in pending.items()]

    def delete_pending_finalize_tasks(
            self,
            pending_keys: Set[str],
            task_ids: Optional[Set[str]] = None,
    ) -> int:
        """删除用户明确取消的待后处理任务，并把失败终态记进重试索引。"""
        removed_keys, pending_count, removed_items = self._remove_pending_finalize_tasks(
            pending_keys,
            task_ids=task_ids,
        )
        if not removed_keys:
            return 0
        for pending_key, item in removed_items.items():
            self._write_terminal_state(
                item, pending_key, "失败", "后处理任务已由用户删除",
                sync_monitor=False,
            )
        self._notify_offline_pending_changed(pending_count)
        return len(removed_keys)

    def stop_pending_finalize_tasks(self, pending_keys: Set[str]) -> int:
        """安全停止后移除尚未提交的后处理任务，并把失败终态记进重试索引。"""
        removed_keys, pending_count, removed_items = self._remove_pending_finalize_tasks(
            pending_keys
        )
        if not removed_keys:
            return 0
        for pending_key, item in removed_items.items():
            self._write_terminal_state(
                item, pending_key, "失败", "后处理任务已由用户停止",
                sync_monitor=False,
            )
        self._notify_offline_pending_changed(pending_count)
        return len(removed_keys)

    def _remove_pending_finalize_tasks(
            self,
            pending_keys: Set[str],
            task_ids: Optional[Set[str]] = None,
    ) -> Tuple[Set[str], int, Dict[str, Dict[str, Any]]]:
        """仅移除待后处理状态；调用方负责写终态和状态通知。"""
        keys = {str(value or "").strip() for value in pending_keys if str(value or "").strip()}
        normalized_task_ids = {
            str(value or "").strip().upper()
            for value in (task_ids or set())
            if str(value or "").strip()
        }
        if (not keys and not normalized_task_ids) or not self._get_data:
            return set(), 0, {}
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            removed_keys = {
                key for key, item in pending.items()
                if key in keys
                   or str((item or {}).get("task_id") or "").strip().upper()
                   in normalized_task_ids
            }
            if not removed_keys:
                return set(), len(pending), {}
            removed_items = {
                key: copy.deepcopy(pending[key]) for key in removed_keys
            }
            for key in removed_keys:
                pending.pop(key, None)
            self._save_offline_pending(pending)
            return removed_keys, len(pending), removed_items


    def _commit_single_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            strm_path: Optional[Path],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """单项后处理完成后立即结算终态并将通知入队，避免中途停止丢失已完成项。"""
        self._update_postprocess_progress(
            item, pending_key, "commit", "更新终态和订阅进度"
        )
        if item.get("upgrade") and str(
                item.get("upgrade_mode") or self._upgrade_mode
        ) != "coexist":
            self._delete_upgrade_old_strm(
                item, replacement_path=strm_path
            )
        self._write_terminal_state(item, pending_key, "成功")
        self._queue_subscription_completion(item, media, media_data, ctx)
        detail = self._owner.transfer_details._notify_pending_file_finalized(
            item,
            pending_key,
            strm_path,
            mediainfo=media,
            media_data=media_data,
            finish_subscription=media is None,
            subscribe_cache=ctx.subscribe_cache,
        )
        if detail:
            ctx.finalized_details.append(detail)
            ctx.notification_contexts.append((item, pending_key))
        if not strm_path:
            logger.debug(f"文件后处理完成：{item.get('file_name') or pending_key}")
        ctx.pending.pop(pending_key, None)
        ctx.committed_keys.add(pending_key)
        task_id = self._postprocess_task_id(item)
        if task_id and self._postprocess_task_update:
            self._postprocess_task_update(
                task_id,
                _pending_key=pending_key,
                file_completed=True,
                postprocess_active=False,
                postprocess_detail="文件处理完成",
            )
        ctx.completed += 1
        self._log_episode_telemetry(item, pending_key, "成功", getattr(ctx, "now", None))

    def _abort_single_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            reason: str,
            ctx: PostprocessBatchContext,
            notify: bool = True,
    ) -> None:
        """单项后处理定位或执行失败后，彻底结算该项，失败记入重试索引，
        并通知前端停止该项。"""
        logger.warning(
            f"文件后处理失败并停止：{item.get('file_name') or pending_key}，原因：{reason}"
        )
        self._write_terminal_state(item, pending_key, "失败", reason)
        # 终态失败统一走已有的死任务失败通知口径（同一标题/文案、同一 _notify 开关）：
        # 此前只有 finalize_dead 路径会通知，直接 abort（定位失败、离线超时等）
        # 静默结算，用户收不到任何完成/失败通知——手动转存的跨盘文件一旦走到
        # 这里就表现为「下载完了但没有通知」。死任务路径的通知调用已并入此处，
        # 同一项只会通知一次；用户主动停止（notify=False）不发失败通知，
        # 停止流程本身已有「任务已停止」通知。
        if notify:
            self._notify_finalize_dead(item, pending_key)
        ctx.pending.pop(pending_key, None)
        ctx.committed_keys.add(pending_key)
        ctx.failed += 1
        self._log_episode_telemetry(item, pending_key, "失败", getattr(ctx, "now", None))
        task_id = self._postprocess_task_id(item)
        if task_id and self._postprocess_task_update:
            self._postprocess_task_update(
                task_id,
                _pending_key=pending_key,
                file_completed=True,
                item_failed=True,
                postprocess_active=False,
                postprocess_detail=f"定位失败：{reason}",
            )

    def _log_episode_telemetry(
            self,
            item: Dict[str, Any],
            pending_key: str,
            outcome: str,
            now: float,
    ) -> None:
        """终态耗时行（1.12.17 Phase 0-10，速度面验收的观测手段）。

        一集只在终态记一行：总耗时、下载段、MP 整理等待段（有 MP 整
        理身份时）+ 进程累计 115 API 快照。MP 整理内的上传发生在 MP
        链里，不在插件计时内——那段就是“整理等待”。只在终态记
        （提交成功 / abort / 死任务），重试中途不记，避免刷屏。
        计量是观测手段：telemetry 解析失败时静默跳过，绝不能影响结算。
        """
        try:
            from ...core.telemetry import api_meter, format_snapshot
        except Exception:
            return
        try:
            now = float(now) if now else time.time()
            file_name = str(item.get("file_name") or pending_key)
            created_at = float(item.get("created_at") or now)
            total = max(0.0, now - created_at)
            segments = [f"总={total:.1f}s"]
            downloaded_at = item.get("download_completed_at")
            if downloaded_at:
                segments.append(
                    f"下载={max(0.0, float(downloaded_at) - created_at):.1f}s"
                )
            mp_organize = item.get("mp_organize")
            if isinstance(mp_organize, dict) and mp_organize.get("triggered_at"):
                segments.append(
                    "整理等待="
                    f"{max(0.0, now - float(mp_organize['triggered_at'])):.1f}s"
                )
            size = item.get("size") or item.get("file_size")
            size_text = ""
            if size:
                size_text = f" {float(size) / 1048576:.2f}MB"
            ep_text = ""
            try:
                season = int(item.get("season") or 0)
                episode = int(item.get("episode") or 0)
                if season and episode:
                    ep_text = f" S{season:02d}E{episode:02d}"
            except (TypeError, ValueError):
                ep_text = ""
            logger.info(
                f"集耗时：{file_name}{ep_text}{size_text} 结果={outcome} "
                f"{' '.join(segments)} "
                f"累计={format_snapshot(api_meter.snapshot())}"
            )
        except Exception as error:
            logger.debug(f"集耗时记录失败：{error}", exc_info=True)

    def _finalize_ready_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            strm_path: Optional[Path],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """处理就绪项的洗版旧文件删除并最终提交。"""
        is_replacement = item.get("upgrade") and str(
            item.get("upgrade_mode") or self._upgrade_mode
        ) != "coexist"
        if is_replacement and self._cloud_batch_mutations:
            backup_id = self._upgrade_old_file_id(
                item, lambda d: self._cloud_directory_snapshot(d, ctx.directory_snapshots)
            )
            if backup_id:
                ctx.upgrade_delete_batch[pending_key] = {
                    "item": item,
                    "file_id": backup_id,
                    "strm_path": strm_path,
                    "media": media,
                    "media_data": media_data,
                }
                return
        if is_replacement and not self._delete_upgrade_old_file(
                item, lambda d: self._cloud_directory_snapshot(d, ctx.directory_snapshots)
        ):
            if self._finalize_failure(item, pending_key):
                return
            self._schedule_finalize_retry(item, ctx.now)
            return
        self._commit_single_item(
            item, pending_key, strm_path, media, media_data, ctx
        )

    def _finalize_after_metadata(
            self,
            item: Dict[str, Any],
            pending_key: str,
            file_name: str,
            strm_path: Optional[Path],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """元数据刮削后推进最终确认。"""
        if (
                getattr(self, "_organize_after_transfer", True)
                and media
                and self._metadata_scraper
                and self._local_resource_path
                and (self._nfo_scrape_enabled or self._image_scrape_enabled)
        ):
            self._update_postprocess_progress(
                item, pending_key, "metadata", "刮削当前文件元数据"
            )
            self._scrape_metadata_batch([{
                "cloud_dir": item["cloud_dir"],
                "file_name": file_name,
                "notification_episodes": (
                    [item.get("episode")] if item.get("episode") else []
                ),
            }], media, season=item.get("season"))
        self._finalize_ready_item(
            item, pending_key, strm_path, media, media_data, ctx
        )

    def _execute_batch_cloud_mutations(self, ctx: PostprocessBatchContext) -> None:
        """统一执行洗版旧文件批量避让、新文件批量重命名与批量移动。"""
        if not self._cloud_batch_mutations:
            return

        # 1. 洗版旧文件批量备份避让
        upgrade_backup_groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for pending_key in ctx.due_keys:
            item = ctx.pending.get(pending_key) or {}
            task_type = str(item.get("task_type") or "share")
            task = ctx.task_map.get(
                str(item.get("task_id") or pending_key).upper()
            )
            if (
                    task_type == "magnet"
                    or (task_type in {"ed2k", "offline"} and not bool(
                task and task.get("completed")
            ))
                    or not item.get("upgrade")
                    or str(item.get("upgrade_mode") or self._upgrade_mode) == "coexist"
                    or item.get("upgrade_old_backed_up")
            ):
                continue
            old_dir = str(
                item.get("upgrade_old_cloud_dir")
                or item.get("cloud_dir") or "/"
            ).rstrip("/") or "/"
            old_name = str(item.get("upgrade_old_file_name") or "").strip()
            old_id = str(item.get("upgrade_old_file_id") or "").strip()
            directory_valid, old_index = self._cloud_directory_snapshot(
                old_dir, ctx.directory_snapshots
            )
            if not directory_valid:
                continue
            old_file = next(
                (
                    value for value in old_index.values()
                    if old_id and str(getattr(value, "id", "")) == old_id
                ),
                None,
            ) or old_index.get(old_name)
            if not old_file:
                continue
            backup_name = self._upgrade_backup_name(
                old_name, item.get("task_id") or pending_key
            )
            upgrade_backup_groups.setdefault(old_dir, {})[pending_key] = {
                "item": old_file,
                "target_name": backup_name,
            }
            item["upgrade_backup_name"] = backup_name

        for old_dir, rename_items in upgrade_backup_groups.items():
            renamed = self._cloud_batch_mutations.rename_files(
                old_dir, rename_items
            )
            for pending_key, backup_file in renamed.items():
                item = ctx.pending.get(pending_key)
                if not item:
                    continue
                item["upgrade_old_backed_up"] = True
                item["upgrade_old_file_id"] = str(
                    getattr(backup_file, "id", "")
                    or item.get("upgrade_old_file_id") or ""
                )
                old_item_info = rename_items.get(pending_key)
                if old_item_info:
                    ctx.sync_rename(old_dir, old_item_info["item"], backup_file)

        organize_enabled = getattr(self, "_organize_after_transfer", True)

        # 2. 批量重命名
        rename_groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for pending_key in ctx.due_keys:
            item = ctx.pending.get(pending_key) or {}
            task_type = str(item.get("task_type") or "share").strip().lower()
            is_replacement = item.get("upgrade") and str(
                item.get("upgrade_mode") or self._upgrade_mode
            ) != "coexist"
            if (
                    task_type in {"magnet", "ed2k", "offline"}
                    or item.get("moved_at")
                    or (is_replacement and not item.get("upgrade_old_backed_up"))
            ):
                continue
            task = ctx.task_map.get(
                str(item.get("task_id") or pending_key).upper()
            )
            staging_dir = str(
                item.get("staging_dir") or item.get("cloud_dir") or "/"
            ).rstrip("/") or "/"
            directory_valid, file_index = self._cloud_directory_snapshot(
                staging_dir, ctx.directory_snapshots
            )
            if not directory_valid:
                continue
            file_name = str(item.get("file_name") or pending_key)
            staging_name = str(
                item.get("staging_name") or item.get("file_name") or ""
            )
            task_name = str((task or {}).get("name") or "").strip()

            # 1. 暂存目录已存在目标文件名的，说明已重命名就绪，直接进入待移动队列
            if file_name and file_name in file_index:
                ctx.prepared_files[pending_key] = file_index[file_name]
                continue

            # 2. 定位待重命名源文件（支持哈希、暂存名、任务名、容错与季集定位）
            target_hash = self._extract_item_hash(item)
            source_file = self._locate_cloud_file(
                staging_dir,
                [staging_name, task_name],
                target_hash,
                ctx,
                item=item,
            )

            if source_file:
                if not organize_enabled or getattr(source_file, "name", "") == file_name:
                    ctx.prepared_files[pending_key] = source_file
                    continue
                rename_groups.setdefault(staging_dir, {})[pending_key] = {
                    "item": source_file,
                    "target_name": file_name,
                }

        for staging_dir, rename_items in rename_groups.items():
            first_key = next(iter(rename_items), "")
            first_item = ctx.pending.get(first_key) or {}
            if first_item:
                self._update_postprocess_progress(
                    first_item,
                    first_key,
                    "organize",
                    f"批量重命名 {len(rename_items)} 个文件",
                )
            renamed = self._cloud_batch_mutations.rename_files(
                staging_dir, rename_items
            )
            for pending_key, target_file in renamed.items():
                item = ctx.pending.get(pending_key)
                if not item:
                    continue
                item["staging_name"] = item["file_name"]
                ctx.prepared_files[pending_key] = target_file
                orig_info = rename_items.get(pending_key)
                if orig_info:
                    ctx.sync_rename(staging_dir, orig_info["item"], target_file)

        # 3. 批量移动
        move_groups: Dict[str, Dict[str, Any]] = {}
        organize_enabled = getattr(self, "_organize_after_transfer", True)
        for pending_key, target_file in ctx.prepared_files.items():
            item = ctx.pending.get(pending_key) or {}
            staging_dir = str(item.get("staging_dir") or "/").rstrip("/") or "/"
            final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
            if staging_dir == final_dir or not organize_enabled:
                # 整理关闭时：将 cloud_dir 修正为转存路径，文件保留在原位
                if not organize_enabled and staging_dir != final_dir:
                    item["cloud_dir"] = staging_dir
                    item["file_name"] = getattr(target_file, "name", item.get("file_name") or "")
                    logger.debug(
                        f"转存后整理已关闭，保留在转存目录（批量阶段）：{staging_dir}/{getattr(target_file, 'name', '')}"
                    )
                item["moved_at"] = ctx.now
                ctx.moved_files[pending_key] = target_file
                continue
            move_groups.setdefault(final_dir, {})[pending_key] = target_file

        for final_dir, move_items in move_groups.items():
            first_key = next(iter(move_items), "")
            first_item = ctx.pending.get(first_key) or {}
            if first_item:
                self._update_postprocess_progress(
                    first_item,
                    first_key,
                    "organize",
                    f"批量移动 {len(move_items)} 个文件",
                )
            moved = self._cloud_batch_mutations.move_files(
                move_items, final_dir
            )
            for pending_key, target_file in moved.items():
                item = ctx.pending.get(pending_key)
                if not item:
                    continue
                item["moved_at"] = ctx.now
                ctx.moved_files[pending_key] = target_file
                ctx.sync_move(item.get("staging_dir") or "/", final_dir, target_file)

    def _should_skip_for_stop(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> bool:
        """任务收到停止请求时，核验网盘中是否已有现成文件，保护已有物理文件不被遗弃。"""
        if not self._stop_requested():
            return False
        if not self._has_existing_cloud_file(item, pending_key, ctx):
            logger.info(f"任务停止：目标文件尚未就绪，安全跳过并终止：{item.get('file_name') or pending_key}")
            self._abort_single_item(
                item, pending_key, "任务已被用户主动停止，终止后处理", ctx,
                notify=False,
            )
            return True
        return False

    def _handle_finalize_dead_item(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> None:
        """处理达到连续失败上限的死任务。"""
        fail_count = int(item.get("fail_count") or 0)
        reason = self._FINALIZE_DEAD_REASON.format(fail_count)
        self._abort_single_item(item, pending_key, reason, ctx)

    def _poll_offline_ready(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """统一检查网盘离线任务就绪状态。"""
        task_id = str(item.get("task_id") or "").strip().upper()
        task = ctx.task_map.get(task_id) if task_id else None

        # 1. 任务明确失败
        if task and bool(task.get("failed")):
            reason = f"离线下载失败：{task.get('status_text') or '任务错误'}"
            self._add_offline_blacklist(item.get("share_url") or pending_key, reason)
            self._cleanup_failed_offline_task(item, reason)
            self._abort_single_item(item, pending_key, reason, ctx)
            return task, False

        # 2. 检查完成状态（网盘暂存目录已有文件 或 任务标记完成）
        staging_dir = str(item.get("staging_dir") or item.get("cloud_dir") or "/")
        directory_valid, file_index = self._cloud_directory_snapshot(
            staging_dir, ctx.directory_snapshots
        )
        task_done = bool(
            (directory_valid and file_index)
            or item.get("moved_at")
            or (task and task.get("completed"))
        )

        if task_done:
            item.setdefault("download_completed_at", ctx.now)
            return task, True

        # 3. 未完成：判断超时或调度重试
        created_at = float(item.get("created_at") or ctx.now)
        timeout_mins = max(1, int(getattr(self, "_OFFLINE_TIMEOUT", 1800) // 60))
        if ctx.now - created_at >= self._OFFLINE_TIMEOUT:
            reason = f"离线下载超过 {timeout_mins} 分钟未完成，已退出"
            self._add_offline_blacklist(item.get("share_url") or pending_key, reason)
            self._cleanup_failed_offline_task(item, reason)
            self._abort_single_item(item, pending_key, reason, ctx)
            return task, False

        self._schedule_finalize_retry(item, ctx.now)
        retry_at = float(item.get("next_check_at") or ctx.now)
        retry_minutes = max(1, int(max(0, retry_at - ctx.now) + 59) // 60)
        self._update_postprocess_progress(
            item, pending_key, "locate", f"等待离线下载完成（将在 {retry_minutes} 分钟后复查）"
        )
        return task, False

    def _finalize_magnet_due_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            task: Optional[Dict[str, Any]],
    ) -> None:
        """整理 Magnet 下载文件包。"""
        self._update_postprocess_progress(
            item, pending_key, "organize", "整理 Magnet 下载文件"
        )
        finalized = self._finalize_magnet_package(
            item, pending_key, subscribe_cache=ctx.subscribe_cache, offline_task=task
        )
        if finalized is None:
            if not self._finalize_failure(item, pending_key):
                self._schedule_finalize_retry(item, ctx.now)
            return
        ctx.pending.pop(pending_key, None)
        ctx.committed_keys.add(pending_key)
        task_id = self._postprocess_task_id(item)
        if task_id and self._postprocess_task_update:
            self._postprocess_task_update(
                task_id,
                _pending_key=pending_key,
                file_completed=True,
                postprocess_active=False,
                postprocess_detail="Magnet 任务完成",
            )
        if finalized:
            self._update_postprocess_progress(
                item, pending_key, "commit", "登记文件和通知结果"
            )
            ctx.finalized_details.extend(finalized)
            ctx.notification_contexts.append((item, pending_key))
            ctx.completed += len(finalized)
            self._log_episode_telemetry(item, pending_key, "成功", getattr(ctx, "now", None))
        else:
            self._add_offline_blacklist(
                item.get("share_url") or pending_key,
                "Magnet 下载完成但未匹配到目标媒体文件",
            )
            ctx.failed += 1
            self._log_episode_telemetry(item, pending_key, "失败", getattr(ctx, "now", None))

    def _track_mp_organize_stall(
            self,
            item: Dict[str, Any],
            mp_organize: Dict[str, Any],
            state: str,
            now: float,
    ) -> None:
        """MP 队列 state 不变满 15 分钟时把停滞文案写进 pending 项，但不 abort。

        MP 是单线程串行队列，队头卡住时后续任务只能排队；这里只提示，
        不自动取消/重试 MP 队列任务。文案存在 pending 项的
        mp_organize_status_text 字段上，随后处理批次一起落盘（Phase 4：
        _MP_ORGANIZING 内存登记表已删，下载页改从 pending 记录推导
        “整理中”态）；state 一变就清掉，避免旧文案残留。
        """
        from ...core.services.mp_organize_tracker import (
            MP_ORGANIZE_QUEUE_STALL_SECONDS,
        )

        wait = item.get("mp_organize_wait")
        if not isinstance(wait, dict):
            wait = {}
        last_state = str(wait.get("last_state") or "")
        last_change_at = float(wait.get("last_change_at") or 0)
        if last_state != state or not last_change_at:
            item["mp_organize_wait"] = {
                "last_state": state,
                "last_change_at": now,
            }
            item.pop("mp_organize_status_text", None)
            return
        if now - last_change_at < MP_ORGANIZE_QUEUE_STALL_SECONDS:
            return
        item["mp_organize_status_text"] = "整理停滞（队头阻塞，后续任务排队中）"

    def _poll_mp_organize_state(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mp_organize: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> Tuple[str, Optional[Any], str]:
        """轮询 MP 整理并处理等待态。

        :return: (action, found_file, dest_parent)；action 为 retry（已调度
            复查，调用方直接返回）、abort（已 abort，调用方直接返回）、
            located（dest 文件已找到，调用方继续成功分支）、dest_missed
            （历史成功但 dest 查无文件，调用方回落平铺定位，仍找不到时
            只复查、不计 fail_count、不 abort）。
        """
        from ...core.services.mp_organize_tracker import (
            MP_ORGANIZE_GRACE_SECONDS,
            MP_ORGANIZE_WAIT_TIMEOUT_SECONDS,
        )

        tracker = self._mp_organize_tracker()
        state, detail, dest_path = tracker.poll(mp_organize)
        triggered_at = float(
            mp_organize.get("triggered_at")
            or item.get("download_completed_at")
            or item.get("created_at")
            or ctx.now
        )
        if (
                state in {"queued", "running", "missing", "unavailable"}
                and ctx.now - triggered_at > MP_ORGANIZE_WAIT_TIMEOUT_SECONDS
        ):
            self._abort_single_item(
                item, pending_key, "MP 整理超时（90 分钟内未完成）", ctx
            )
            return ("abort", None, "")
        if state == "failed":
            # detail 已是 MP 原因原文（截断到约 200 字），原样透传。
            # 但 manual_review 的 detail 本身就是完整表述（"MP 整理待人工
            # 复核"），再加“MP 整理失败：”前缀会读成双重否定——直接用原文。
            # 归属：manual_review 只由 MP 侧翻转，插件这里只是把它当作
            # 自己追踪的终态失败（见 mp_organize_tracker 模块 docstring）。
            if str(detail or "").startswith("MP 整理待人工复核"):
                reason = str(detail)
            else:
                reason = f"MP 整理失败：{detail}"
            self._abort_single_item(item, pending_key, reason, ctx)
            return ("abort", None, "")
        if state in {"queued", "running", "unavailable"}:
            # unavailable 是 MP 侧不可用（不是“未受理”）：继续等，不走
            # missing 的宽限期 abort，只受 90 分钟总超时约束。
            if state == "unavailable":
                text = "MP 整理链不可用，等待恢复"
            else:
                text = "MP 整理排队中" if state == "queued" else "MP 整理进行中"
            self._update_postprocess_progress(
                item, pending_key, "organize", text
            )
            self._track_mp_organize_stall(item, mp_organize, state, ctx.now)
            self._schedule_finalize_retry(item, ctx.now)
            return ("retry", None, "")
        if state == "missing":
            if ctx.now - triggered_at < MP_ORGANIZE_GRACE_SECONDS:
                self._schedule_finalize_retry(item, ctx.now)
                return ("retry", None, "")
            self._abort_single_item(item, pending_key, "MP 整理未受理", ctx)
            return ("abort", None, "")
        if state == "succeeded":
            found = None
            if dest_path:
                found = self._locate_cloud_file_by_dest(dest_path, ctx)
            if found is not None:
                item["moved_at"] = ctx.now
                ctx.moved_files[pending_key] = found
                # Phase 3a 第 11 项：MP 整理已按 MP 自己的格式改名，
                # 实际落盘名以 MP 历史的 dest 文件名为准——把 pending
                # 项的文件名反向同步成 dest 的真实值，后续 STRM/字幕/
                # 终态都用它，不再用插件自己算的目标名去定位。
                # 注意只同步文件名，不动 cloud_dir：cloud_dir 是 STRM
                # 落点与终态记录的媒体库目录，dest 父目录可能是 /转存/
                # 下的嵌套路径（S01E234 回归用例），同步过去会把 STRM
                # 写错地方。
                actual_name = str(getattr(found, "name", "") or "").strip()
                if actual_name:
                    item["file_name"] = actual_name
                return ("located", found, str(Path(str(dest_path)).parent))
            return ("dest_missed", None, "")
        # 未知态：当作等待处理，只复查、不计失败。
        self._schedule_finalize_retry(item, ctx.now)
        return ("retry", None, "")

    def _organize_and_finalize_single_item(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> None:
        """执行单文件的物理定位、自愈匹配、重命名/移动、洗版替换、字幕与 STRM 生成。"""
        task_type = str(item.get("task_type") or "share").strip().lower()
        file_name = str(item.get("file_name") or pending_key)
        created_at = float(item.get("created_at") or ctx.now)

        already_moved = bool(item.get("moved_at"))
        staging_dir = str(
            item.get("cloud_dir") if already_moved
            else item.get("staging_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        staging_name = (
            file_name if already_moved
            else str(item.get("staging_name") or file_name)
        )

        if not already_moved and task_type in {"ed2k", "offline"} and getattr(self, "_organize_after_transfer", True):
            current_final = str(item.get("cloud_dir") or "").rstrip("/") or "/"
            if current_final == staging_dir:
                try:
                    calc_media, _ = self._owner.transfer_details._restore_pending_media_context(item, pending_key)
                    sub_id = int(item.get("subscribe_id") or 0)
                    calc_sub = ctx.subscribe_cache.get(sub_id)
                    if calc_media:
                        season_val = max(1, int(item.get("season") or 1)) if calc_media.type == MediaType.TV else None
                        ep_list = [int(v) for v in (item.get("target_episodes") or []) if int(v) > 0]
                        ep_val = ep_list[0] if ep_list else (
                            int(item.get("episode")) if item.get("episode") else None
                        )
                        dyn_dir, dyn_name = self._owner.naming._platform_target(
                            self._CLOUD_MEDIA_ROOT, calc_sub, calc_media,
                            staging_name, season=season_val, episode=ep_val
                        )
                        if dyn_dir:
                            item["cloud_dir"] = dyn_dir
                            source_suffix = Path(staging_name).suffix
                            if source_suffix and not dyn_name.endswith(source_suffix):
                                dyn_name = f"{Path(dyn_name).stem}{source_suffix}"
                            item["file_name"] = dyn_name
                            file_name = dyn_name
                except Exception as dyn_err:
                    logger.debug(f"动态计算离线文件目标媒体路径异常：{dyn_err}", exc_info=True)

        target_file = ctx.moved_files.get(pending_key) or ctx.prepared_files.get(pending_key)
        target_hash = self._extract_item_hash(item)

        # v1.12.6 C：带 MP 整理身份的项先轮询 MP 链：MP 还在整理/排队时只
        # 复查、不计 fail_count；MP 已失败时以 MP 原因 abort；MP 已成功时
        # 按历史 dest 精确路径定位，跳过平铺目录扫描。
        mp_organize = item.get("mp_organize")
        mp_dest_missed = False
        if (
                isinstance(mp_organize, dict) and mp_organize
                and not target_file and not already_moved
        ):
            mp_action, mp_found, mp_dest_dir = self._poll_mp_organize_state(
                item, pending_key, mp_organize, ctx
            )
            if mp_action in {"retry", "abort"}:
                return
            if mp_action == "located":
                target_file = mp_found
                already_moved = True
                staging_dir = mp_dest_dir or staging_dir
                # Phase 3a 第 11 项：本地 file_name 跟着 MP 的实际落盘名
                # 走（_poll_mp_organize_state 里已把 item["file_name"]
                # 同步成 dest 文件名），后续 STRM/终态都用真实名。
                file_name = str(item.get("file_name") or file_name)
            elif mp_action == "dest_missed":
                mp_dest_missed = True

        # 仅在尚未就绪物理文件时才执行网盘检索与状态广播，避免重复检查与多余推送
        if not target_file:
            self._update_postprocess_progress(
                item, pending_key, "locate", f"在 {staging_dir} 定位 {staging_name}"
            )
            task = ctx.task_map.get(str(item.get("task_id") or pending_key).upper())
            task_name = str((task or {}).get("name") or "").strip() if task_type in {"ed2k", "offline"} else ""
            ed2k_url_name = extract_ed2k_filename(str(item.get("share_url") or ""))
            res_file_list = (item.get("resource") or {}).get("file_list")
            res_file_name = str(res_file_list[0]).strip() if isinstance(res_file_list, list) and res_file_list else ""
            candidate_names = [file_name, staging_name, task_name, ed2k_url_name, res_file_name]
            target_file = self._locate_cloud_file(staging_dir, candidate_names, target_hash, ctx, item=item)
            if target_file:
                ctx.prepared_files[pending_key] = target_file

        if not target_file and not already_moved:
            final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
            if final_dir != staging_dir:
                final_candidates = [file_name, staging_name, task_name, ed2k_url_name, res_file_name]
                final_target = self._locate_cloud_file(final_dir, final_candidates, target_hash, ctx, item=item)
                if final_target:
                    target_file = final_target
                    item["moved_at"] = ctx.now
                    already_moved = True
                    staging_dir = final_dir
                    ctx.moved_files[pending_key] = final_target
                    if getattr(final_target, "name", "") != file_name:
                        source_suffix = Path(final_target.name).suffix
                        if source_suffix and not file_name.endswith(source_suffix):
                            file_name = f"{Path(file_name).stem}{source_suffix}"
                            item["file_name"] = file_name
                        if self._cloud_mutations.rename_file(final_dir, final_target, file_name):
                            renamed_target = self._cloud_query.get_cached_file(final_dir, file_name)
                            if renamed_target:
                                ctx.sync_rename(final_dir, target_file, renamed_target)
                                target_file = renamed_target
                                ctx.moved_files[pending_key] = renamed_target
                    logger.info(
                        f"后处理在最终目录找到文件并就绪，继续生成STRM："
                        f"{final_dir}/{getattr(target_file, 'name', file_name)}"
                    )

        if not target_file:
            if mp_dest_missed:
                # MP 历史已成功但 dest 查无文件（S01E06 库内查找不一致就是
                # 前车之鉴）：不计失败、不 abort，只复查，等 MP 侧落定。
                self._schedule_finalize_retry(item, ctx.now)
                return
            # Phase 4：定位失败的计数与超时只调 _finalize_failure（阈值与
            # 超时在 service.py 一处定义）。能走到这里的项没有 mp_organize
            # 身份——带身份的项已在前面走 _poll_mp_organize_state，MP 整理
            # 等待不受 2 次/120 秒约束，只受 MP 90 分钟总超时约束；这里的
            # 2 次/120 秒只约束插件自己的定位步骤。
            # 若是普通转存/整理或已完成下载的离线任务，最多允许重试 1 次（给网盘极短暂的元数据刷新时间）；
            # 只有正在下载中的离线任务才允许重试更多次
            is_offline_downloading = (
                    task_type in {"ed2k", "magnet", "offline"}
                    and not bool(item.get("download_completed_at"))
            )
            ready_at = float(item.get("download_completed_at") or created_at)
            timeout_seconds = (
                self._OFFLINE_TIMEOUT if is_offline_downloading
                else self._FINALIZE_LOCATE_TIMEOUT_SECONDS
            )
            if self._finalize_failure(
                    item, pending_key, now=ctx.now, ready_at=ready_at,
                    timeout_seconds=timeout_seconds,
            ):
                reason = (
                    f"网盘文件定位失败（在转存路径 {staging_dir} 及目标路径均未找到匹配文件，已尝试 {item.get('fail_count', 0)} 次）"
                )
                self._abort_single_item(item, pending_key, reason, ctx)
            else:
                self._schedule_finalize_retry(item, ctx.now)
            return

        if item.get("upgrade") and str(
                item.get("upgrade_mode") or self._upgrade_mode
        ) != "coexist":
            self._update_postprocess_progress(
                item, pending_key, "organize", "替换旧版本文件"
            )
            replaced_file = self._replace_upgrade_file(
                item=item,
                pending_key=pending_key,
                target_file=target_file,
                staging_dir=staging_dir,
                file_name=file_name,
                now=ctx.now,
                directory_snapshot=lambda d: self._cloud_directory_snapshot(d, ctx.directory_snapshots),
            )
            if not replaced_file:
                return
            target_file = replaced_file
            already_moved = True

        if not already_moved and not getattr(self, "_organize_after_transfer", True):
            item["cloud_dir"] = staging_dir
            item["file_name"] = getattr(target_file, "name", staging_name)
            item["moved_at"] = ctx.now
            already_moved = True
            logger.debug(
                f"转存后整理已关闭，保留在转存目录：{staging_dir}/{item['file_name']}"
            )

        if not already_moved:
            self._update_postprocess_progress(
                item, pending_key, "organize", "重命名并移动到媒体目录"
            )
            if target_file.name != file_name:
                source_suffix = Path(target_file.name).suffix
                if source_suffix and not file_name.endswith(source_suffix):
                    file_name = f"{Path(file_name).stem}{source_suffix}"
                    item["file_name"] = file_name
                staging_valid, staging_index = self._cloud_directory_snapshot(
                    staging_dir, ctx.directory_snapshots
                )
                if staging_valid and file_name in staging_index and str(staging_index[file_name].id) == str(
                        target_file.id):
                    target_file = staging_index[file_name]
                else:
                    orig_target = target_file
                    if not self._cloud_mutations.rename_file(
                            staging_dir, target_file, file_name
                    ):
                        if self._finalize_failure(item, pending_key):
                            self._abort_single_item(
                                item, pending_key, f"网盘文件重命名失败，已重试 {item.get('fail_count', 0)} 次", ctx
                            )
                        else:
                            self._schedule_finalize_retry(item, ctx.now)
                        return
                    item["staging_name"] = file_name
                    target_file = self._cloud_query.get_cached_file(
                        staging_dir, file_name
                    )
                    if not target_file:
                        if self._finalize_failure(item, pending_key):
                            self._abort_single_item(
                                item, pending_key, f"重命名后定位失败，已重试 {item.get('fail_count', 0)} 次", ctx
                            )
                        else:
                            self._schedule_finalize_retry(item, ctx.now)
                        return
                    ctx.sync_rename(staging_dir, orig_target, target_file)

            final_dir = str(item["cloud_dir"]).rstrip("/") or "/"
            if staging_dir != final_dir:
                final_valid, final_index = self._cloud_directory_snapshot(
                    final_dir, ctx.directory_snapshots
                )
                existing_in_final = final_index.get(file_name) if final_valid else None
                source_size = int(item.get("file_size") or getattr(target_file, "size", 0) or 0)
                existing_size = int(getattr(existing_in_final, "size", 0) or 0)
                matched_hash = False
                for algo in self._supported_hash_algorithms():
                    source_val = str(getattr(target_file, algo, "") or item.get(f"source_{algo}") or item.get(
                        algo) or "").strip().upper()
                    existing_val = str(getattr(existing_in_final, algo, "") or "").strip().upper()
                    if source_val and existing_val and source_val == existing_val:
                        matched_hash = True
                        break
                matched_existing = bool(
                    existing_in_final and (
                            matched_hash
                            or (source_size > 0 and existing_size > 0 and abs(source_size - existing_size) <= 1024)
                            or (existing_size > 0 and not source_size)
                            or str(existing_in_final.id) == str(target_file.id)
                    )
                )
                if matched_existing:
                    logger.debug(
                        f"媒体目录已存在目标文件，自愈复用并跳过移动：{final_dir}/{file_name}"
                    )
                    if str(existing_in_final.id) != str(target_file.id):
                        try:
                            self._cloud_mutations.delete_file(target_file.id)
                        except Exception as clean_err:
                            logger.debug(f"清理暂存区多余副本失败：{clean_err}", exc_info=True)
                    moved_file = existing_in_final
                else:
                    moved_file = self._cloud_mutations.move_file(
                        target_file, item["cloud_dir"], file_name
                    )
            else:
                moved_file = target_file
            if not moved_file:
                final_valid, final_index = self._cloud_directory_snapshot(
                    final_dir, ctx.directory_snapshots
                )
                if final_valid and file_name in final_index:
                    moved_file = final_index[file_name]
                    logger.debug(
                        f"移动操作返回失败但在目标目录找到文件，自愈恢复：{final_dir}/{file_name}"
                    )
                else:
                    if self._finalize_failure(item, pending_key):
                        self._abort_single_item(
                            item, pending_key, f"移动到媒体目录失败，已重试 {item.get('fail_count', 0)} 次", ctx
                        )
                    else:
                        self._schedule_finalize_retry(item, ctx.now)
                    return
            ctx.sync_move(staging_dir, final_dir, moved_file)
            target_file = self._dedupe_moved_duplicate(
                final_dir, file_name, moved_file
            )
            item["moved_at"] = ctx.now

        context_key = self._media_context_key(item)
        cached_context = (
            ctx.media_context_cache.get(context_key) if context_key else None
        )
        if cached_context:
            media, media_data = cached_context
            item["mediainfo"] = media_data
        else:
            media, media_data = self._owner.transfer_details._restore_pending_media_context(
                item, pending_key
            )
            resolved_key = context_key or self._media_context_key(item)
            if resolved_key and media:
                ctx.media_context_cache[resolved_key] = (media, media_data)

        if not getattr(self, "_organize_after_transfer", True):
            # 整理已关闭：跳过元数据刮削、跳过 STRM 生成与字幕整理，直接就绪终态
            self._finalize_ready_item(
                item, pending_key, None, media, media_data, ctx
            )
            return

        if (
                not self._strm_generate_enabled
                or not self._strm_generator
                or not self._local_resource_path
        ):
            if item.get("subtitles") and getattr(self, "_organize_subtitles", True):
                self._update_postprocess_progress(
                    item, pending_key, "subtitle", "检查并整理伴随字幕"
                )
                if not self._owner.subtitles._finalize_subtitle_files(
                        item, lambda d: self._cloud_directory_snapshot(d, ctx.directory_snapshots)
                ):
                    logger.debug(f"伴随字幕整理未完成，跳过字幕继续处理视频主文件：{file_name}")
            self._finalize_after_metadata(
                item, pending_key, file_name, None, media, media_data, ctx
            )
            return

        self._update_postprocess_progress(
            item, pending_key, "strm", "生成并校验 STRM 文件"
        )
        strm_path = self._owner.strm_ops._generate_strm(
            item["cloud_dir"], file_name, target_file=target_file
        )
        if strm_path and not self._strm_file_ready(strm_path):
            logger.error(f"STRM 生成后文件不存在或为空：{strm_path}")
            strm_path = None
        if strm_path:
            if item.get("subtitles") and getattr(self, "_organize_subtitles", True):
                self._update_postprocess_progress(
                    item, pending_key, "subtitle", "检查并整理伴随字幕"
                )
                if not self._owner.subtitles._finalize_subtitle_files(
                        item, lambda d: self._cloud_directory_snapshot(d, ctx.directory_snapshots), strm_path=strm_path
                ):
                    logger.debug(f"伴随字幕整理未完成，跳过字幕继续处理视频主文件：{file_name}")
            if not self._strm_file_ready(strm_path):
                logger.error(f"洗版后 STRM 文件不存在或为空：{strm_path}")
                self._schedule_finalize_retry(item, ctx.now)
                return
            self._finalize_after_metadata(
                item, pending_key, file_name, strm_path, media, media_data, ctx
            )
            return

        ready_at = float(item.get("download_completed_at") or created_at)
        if ctx.now - ready_at >= self._FILE_FINALIZE_TIMEOUT:
            # 与 _abort_single_item 同口径结算：写失败历史、清队列、推任务终态、
            # 发失败通知；此前这里内联结算，不推任务终态也不通知，文件静默失败。
            self._abort_single_item(
                item, pending_key, "文件已下载但30分钟内仍无法生成 STRM", ctx
            )
        else:
            self._schedule_finalize_retry(item, ctx.now)

    def _process_due_items(self, ctx: PostprocessBatchContext) -> None:
        """按序调度处理所有到期的后处理任务。"""
        for pending_key in ctx.due_keys:
            item = ctx.pending.get(pending_key)
            if not item:
                continue
            if self._should_skip_for_stop(item, pending_key, ctx):
                continue
            if item.get("finalize_dead"):
                self._handle_finalize_dead_item(item, pending_key, ctx)
                continue

            task_type = str(item.get("task_type") or "share").strip().lower()
            if task_type in {"magnet", "ed2k", "offline"}:
                task, ready = self._poll_offline_ready(item, pending_key, ctx)
                if not ready:
                    continue
                if task_type == "magnet":
                    self._finalize_magnet_due_item(item, pending_key, ctx, task)
                    continue
            self._organize_and_finalize_single_item(item, pending_key, ctx)

    def _flush_batch_postprocess(self, ctx: PostprocessBatchContext) -> None:
        """批次后处理收尾：批量回收洗版旧文件、批量提交订阅集数、发送批次聚合通知。"""
        if ctx.upgrade_delete_batch:
            delete_ids = list(dict.fromkeys(
                value["file_id"] for value in ctx.upgrade_delete_batch.values()
            ))
            deleted_ids = {
                str(file_id) for file_id in
                self._cloud_batch_mutations.delete_files(delete_ids)
            }
            for pending_key, value in ctx.upgrade_delete_batch.items():
                item = value["item"]
                if str(value["file_id"]) not in deleted_ids:
                    self._schedule_finalize_retry(item, ctx.now)
                    continue
                item["upgrade_old_deleted"] = True
                self._commit_single_item(
                    item,
                    pending_key,
                    value["strm_path"],
                    value["media"],
                    value["media_data"],
                    ctx,
                )
            success_count = len(deleted_ids & set(map(str, delete_ids)))
            total_count = len(delete_ids)
            logger.info(
                f"洗版旧文件批量回收完成：成功 {success_count}/{total_count} 个"
            )
            if success_count < total_count:
                logger.warning(
                    f"洗版旧文件有 {total_count - success_count} 个回收失败，"
                    "已保留后处理任务等待重试"
                )

        for batch in ctx.subscription_batches.values():
            completion_item = batch["item"]
            completion_item["success_episodes"] = sorted(batch["episodes"])
            completion_item["notification_episodes"] = sorted(
                batch["episodes"]
            )
            self._finish_pending_subscription(
                completion_item,
                batch["media_data"],
                mediainfo=batch["mediainfo"],
            )

        if ctx.finalized_details:
            if self._notify and ctx.notification_contexts:
                progress_item, progress_key = ctx.notification_contexts[-1]
                self._update_postprocess_progress(
                    progress_item,
                    progress_key,
                    "notify",
                    f"汇总发送 {len(ctx.finalized_details)} 个文件的完成通知",
                )
            self._owner.transfer_details._send_finalized_batch(ctx.finalized_details)

    def _reconcile_pending_state(self, ctx: PostprocessBatchContext) -> Dict[str, int]:
        """将批次处理结果协调写回持久化存储，并更新前端任务进度状态。

        只做一件事：把 ctx.pending 里本批处理过的项与持久化存储里的同
        代项做字段差分同步（改了的字段写回、删了的字段清掉、已终态的项
        出队）。Phase 4 第 1 项：本方法绝不碰重试索引——终态写索引只
        允许 finalize（_write_terminal_state）、persist_sync_results
        与重试执行三处；这里即使看到失败项也不写行、不删行。
        """
        with self._offline_pending_lock:
            current_pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            for pending_key in ctx.due_keys:
                original_item = ctx.pending_snapshot.get(pending_key)
                processed_item = ctx.pending.get(pending_key)
                current_item = current_pending.get(pending_key)
                if current_item is None or original_item is None:
                    continue
                if current_item.get("_monitor_token") != ctx.monitor_token:
                    continue
                generation = (
                    original_item.get("created_at"),
                    original_item.get("share_url"),
                    original_item.get("file_name"),
                    original_item.get("task_type"),
                )
                current_generation = (
                    current_item.get("created_at"),
                    current_item.get("share_url"),
                    current_item.get("file_name"),
                    current_item.get("task_type"),
                )
                if generation != current_generation:
                    continue
                if processed_item is None:
                    current_pending.pop(pending_key, None)
                    continue
                processed_item.pop("_monitor_token", None)
                processed_item.pop("_monitor_until", None)
                for field in set(original_item) | set(processed_item):
                    if original_item.get(field) == processed_item.get(field):
                        continue
                    if field in processed_item:
                        current_item[field] = copy.deepcopy(processed_item[field])
                    else:
                        current_item.pop(field, None)
            self._save_offline_pending(current_pending)
            pending_count = len(current_pending)

        result = {
            "checked": len(ctx.due_keys),
            "completed": ctx.completed,
            "failed": ctx.failed,
            "pending": pending_count,
        }
        task_items = {
            pending_key: item
            for pending_key in ctx.due_keys
            if (item := ctx.pending_snapshot.get(pending_key))
               and self._postprocess_task_id(item)
        }
        if self._postprocess_task_update:
            for pending_key, item in task_items.items():
                if pending_key in current_pending:
                    waiting_item = current_pending.get(pending_key) or item
                    is_offline = str(waiting_item.get("task_type") or "share") in {"magnet", "ed2k", "offline"}
                    retry_at = float(waiting_item.get("next_check_at") or ctx.now)
                    mins = max(1, int(max(0, retry_at - ctx.now) + 59) // 60)
                    wait_detail = f"等待离线下载完成（将在 {mins} 分钟后复查）" if is_offline else f"等待文件就绪（将在 {mins} 分钟后复查）"
                    self._postprocess_task_update(
                        self._postprocess_task_id(waiting_item),
                        _pending_key=pending_key,
                        postprocess_active=True,
                        postprocess_step="locate",
                        postprocess_detail=wait_detail,
                    )
                else:
                    self._postprocess_task_update(
                        self._postprocess_task_id(item),
                        _pending_key=pending_key,
                        postprocess_active=False,
                        postprocess_detail="",
                    )
        self._notify_offline_pending_changed(result["pending"])
        return result

    def monitor_offline_strm_tasks(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
            offline_tasks: Optional[List[Dict[str, Any]]] = None,
            offline_tasks_valid: Optional[bool] = None,
    ) -> Dict[str, int]:
        """检查离线下载和网盘文件后处理；手动刷新可立即重试指定任务。"""
        ctx = self._prepare_postprocess_context(
            force=force,
            pending_keys=pending_keys,
            offline_tasks=offline_tasks,
            offline_tasks_valid=offline_tasks_valid,
        )
        if not ctx:
            return {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        if not ctx.due_keys:
            return {"checked": 0, "completed": 0, "failed": 0, "pending": len(ctx.pending)}

        try:
            self._execute_batch_cloud_mutations(ctx)
            self._process_due_items(ctx)
            self._flush_batch_postprocess(ctx)
        finally:
            result = self._reconcile_pending_state(ctx)
        return result

    def _finalize_magnet_package(
            self,
            item: Dict[str, Any],
            pending_key: str,
            subscribe_cache: Optional[Dict[int, Any]] = None,
            offline_task: Optional[Dict[str, Any]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """读取完成后的真实文件树，只移动实际匹配的媒体文件。"""
        mediainfo, media_data = self._owner.transfer_details._restore_pending_media_context(item, pending_key)
        if not mediainfo:
            self._cleanup_failed_offline_task(item, "媒体元数据不存在")
            return []
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_cache is not None:
            subscribe = subscribe_cache.get(subscribe_id)
        elif subscribe_id:
            with SessionFactory() as db:
                subscribe = SubscribeOper(db=db).get(subscribe_id)
        else:
            subscribe = None
        if not subscribe and (item.get("transient_target") or subscribe_id <= 0):
            target_data = dict(item.get("target_subscribe") or {})
            for attr in (
                    "quality", "resolution", "effect", "include", "exclude",
                    "audio_quality", "audio_format", "min_bitrate", "min_bit_depth",
                    "min_sample_rate", "filter", "filter_groups", "sites", "custom_words",
            ):
                target_data.setdefault(attr, None)
            target_data.setdefault("id", subscribe_id or -1)
            target_data.setdefault("name", getattr(mediainfo, "title", "未命名媒体"))
            target_data.setdefault("year", getattr(mediainfo, "year", None))
            target_data.setdefault("season", item.get("season"))
            subscribe = SimpleNamespace(**target_data)
            subscribe.to_dict = lambda: dict(subscribe.__dict__)
        if not subscribe:
            self._cleanup_failed_offline_task(item, "订阅已不存在")
            self._write_terminal_state(
                item, pending_key, "失败", "Magnet 下载完成时订阅已不存在"
            )
            return []

        task_files_func = getattr(self._cloud_query, "list_offline_task_files", None)
        files = []
        if callable(task_files_func) and offline_task:
            try:
                files = task_files_func(offline_task, item.get("staging_dir") or item.get("cloud_dir") or "/")
            except Exception as e:
                logger.debug(f"精准定位离线任务文件异常，回退扫描：{e}", exc_info=True)
        if not files:
            # Phase 3a 第 5 项：递归树列目录走唯一原语
            # _locate_cloud_files_in_tree（与 _locate_cloud_file 的
            # 递归策略同一实现；失败返回 []，不抛）。
            files = self._locate_cloud_files_in_tree(
                item.get("staging_dir") or "/"
            )
        if not files and item.get("cloud_dir") and item.get("cloud_dir") != item.get("staging_dir"):
            files = self._locate_cloud_files_in_tree(item.get("cloud_dir"))
        video_files = [
            file_item for file_item in files
            if MediaFileParser.is_video(str(file_item.get("name") or ""))
        ]
        if not video_files:
            recovered = self._try_recover_magnet_from_destination(
                item, pending_key, mediainfo, subscribe
            )
            if recovered is not None:
                return recovered
            logger.debug(f"Magnet 已完成但真实文件树尚未就绪：{item.get('file_name')}")
            return None

        def directory_snapshot(cloud_dir: str) -> Tuple[bool, Dict[str, Any]]:
            return self._cloud_directory_snapshot(cloud_dir)

        matched: List[Tuple[Optional[int], Any, int]] = []
        season = item.get("season")
        if mediainfo.type == MediaType.TV:
            target_episodes = []
            for value in item.get("target_episodes") or []:
                try:
                    episode = int(str(value or "0"))
                except ValueError:
                    continue
                if episode > 0:
                    target_episodes.append(episode)
            resource = item.get("resource") or {}
            if resource.get("source") in {"mikan", "animegarden"}:
                candidates = anime_file_candidates(
                    video_files,
                    resource.get("title") or "",
                    normalize_season(season),
                    target_episodes,
                )
                episode_files = {
                    episode: self._search_handler.select_file_candidate(files, mediainfo, subscribe)
                    for episode, files in candidates.items()
                }
            else:
                episode_files = self._owner.file_matching._match_episode_files(
                    video_files,
                    mediainfo,
                    subscribe,
                    normalize_season(season),
                    target_episodes,
                )
            matched = [
                (episode, episode_files[episode][0], episode_files[episode][1])
                for episode in target_episodes
                if episode_files.get(episode, (None, 0))[0]
            ]
        else:
            movie_file, movie_score = self._owner.file_matching._match_movie_file(
                video_files, mediainfo, subscribe
            )
            if movie_file:
                matched = [(None, movie_file, movie_score)]

        if not matched:
            reason = "Magnet 下载完成，但真实文件名未匹配当前订阅"
            logger.debug(f"{reason}：{item.get('file_name')}")
            self._cleanup_failed_offline_task(item, reason)
            self._write_terminal_state(item, pending_key, "失败", reason)
            return []

        history_records = []
        details = []
        success_episodes = []
        resource = item.get("resource") or {}
        share_url = str(item.get("share_url") or "")
        upgrade_baseline = item.get("upgrade_baseline") or {}
        for episode, source_file, current_score in matched:
            source_name = str(source_file.get("name") or "")
            baseline_key = str(episode) if episode else "movie"
            old_baseline = upgrade_baseline.get(baseline_key) or {}
            is_upgrade = bool(old_baseline)
            source_size = self._owner.upgrade_rule_scoring._resource_size_bytes(source_file.get("size"))
            if is_upgrade:
                should_upgrade, reason = self._owner.upgrade_rule_scoring._should_upgrade_candidate(
                    int(old_baseline.get("score") or 0),
                    current_score,
                    int(old_baseline.get("size") or 0),
                    source_size,
                    old_file_name=str(old_baseline.get("file_name") or ""),
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    episode=episode,
                )
                if not should_upgrade:
                    label = f"E{int(episode):02d}" if episode else mediainfo.title
                    logger.debug(f"Magnet 下载后洗版候选跳过 {label}：{reason}")
                    continue
            cloud_dir, target_name = self._owner.naming._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                source_name,
                season=max(1, int(season or 1)) if episode else None,
                episode=episode,
            )
            source_suffix = Path(source_name).suffix
            if source_suffix and not target_name.endswith(source_suffix):
                target_name = f"{Path(target_name).stem}{source_suffix}"
            mode = str(item.get("upgrade_mode") or self._upgrade_mode)
            if is_upgrade and mode == "coexist":
                target_name = self._owner.upgrade_rule_scoring._coexist_target_name(
                    target_name, source_name, source_size, source_file.get("sha1") or ""
                )
            if is_upgrade and mode != "coexist":
                old_dir = str(old_baseline.get("cloud_dir") or "").strip()
                old_name = str(old_baseline.get("file_name") or "").strip()
                old_file_id = str(old_baseline.get("file_id") or "").strip()
                if not old_dir or not old_name:
                    old_dir, old_name = self._owner.naming._platform_target(
                        self._CLOUD_MEDIA_ROOT,
                        subscribe,
                        mediainfo,
                        old_name or source_name,
                        season=max(1, int(season or 1)) if episode else None,
                        episode=episode,
                    )
                if not old_file_id:
                    old_file = self._cloud_query.get_cached_file(old_dir, old_name)
                    old_file_id = str(getattr(old_file, "id", "") or "")
                replace_item = {
                    **item,
                    "cloud_dir": cloud_dir,
                    "file_name": target_name,
                    "upgrade_old_cloud_dir": old_dir,
                    "upgrade_old_file_name": old_name,
                    "upgrade_old_file_id": old_file_id,
                }
                source_dir = str(
                    (getattr(source_file, "native", None) or {}).get("_cloud_dir")
                    or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                moved = self._replace_upgrade_file(
                    replace_item,
                    f"{pending_key}:{baseline_key}",
                    source_file,
                    source_dir,
                    target_name,
                    time.time(),
                    directory_snapshot,
                )
            else:
                moved = self._cloud_mutations.move_file(
                    source_file, cloud_dir, target_name
                )
            if not moved:
                continue
            if not self._finalize_magnet_single_output(
                    cloud_dir=cloud_dir,
                    target_name=target_name,
                    target_file=moved,
                    mediainfo=mediainfo,
                    subscribe=subscribe,
                    season=season,
                    episode=episode,
                    is_upgrade=is_upgrade,
                    mode=mode,
                    replace_item=replace_item if (is_upgrade and mode != "coexist") else None,
                    directory_snapshot=directory_snapshot,
            ):
                continue

            if episode:
                success_episodes.append(int(episode))
            else:
                success_episodes.append(1)
            episode_fields = (
                {"season": int(season or 1), "episode": int(episode)}
                if episode else {}
            )
            record = self._owner.transfer_details._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="成功",
                share_url=share_url,
                file_name=target_name,
                source_file_name=source_name,
                cloud_dir=cloud_dir,
                resource=resource,
                file_size=source_size,
                source_sha1=str(source_file.get("sha1") or ""),
                rule_score=current_score,
                upgrade=is_upgrade,
                **episode_fields,
            )
            history_records.append(record)
            detail = {
                "type": record["type"],
                "title": mediainfo.title,
                "year": mediainfo.year,
                "image": mediainfo.get_poster_image(),
                "file_name": target_name,
            }
            if episode:
                detail.update({"season": int(season or 1), "episodes": [int(episode)]})
            details.append(detail)

        if history_records:
            apply_landed = getattr(
                self._owner.landed_verify, "apply_landed_verification_to_records", None
            )
            if callable(apply_landed):
                apply_landed(subscribe, mediainfo, season, history_records)
        details = self._persist_magnet_finalized_outputs(
            item=item,
            pending_key=pending_key,
            mediainfo=mediainfo,
            media_data=media_data,
            history_records=history_records,
            details=details,
            success_episodes=success_episodes,
        )
        if details:
            logger.info(
                f"Magnet 下载后文件匹配完成：移动 {len(history_records)} 个文件，"
                f"未匹配内容保留在隔离目录"
            )
        return details

    def _finalize_magnet_single_output(
            self,
            cloud_dir: str,
            target_name: str,
            target_file: Any,
            mediainfo: Any,
            subscribe: Any,
            season: Optional[int],
            episode: Optional[int],
            is_upgrade: bool = False,
            mode: str = "",
            replace_item: Optional[Dict[str, Any]] = None,
            directory_snapshot: Optional[Any] = None,
    ) -> bool:
        """完成 Magnet 单个匹配文件的元数据刮削、STRM 生成与媒体服务器通知。"""
        self._scrape_metadata(
            cloud_dir,
            target_name,
            mediainfo,
            season=season,
            episode=episode,
        )
        strm_path = None
        if self._strm_generate_enabled and self._strm_generator and self._local_resource_path:
            strm_path = self._owner.strm_ops._generate_strm(
                cloud_dir, target_name, target_file=target_file, lookup_target=False
            )
            if strm_path:
                if is_upgrade and mode != "coexist" and replace_item and directory_snapshot:
                    if not self._delete_upgrade_old_file(replace_item, directory_snapshot):
                        logger.debug(f"Magnet 洗版旧文件删除失败：{target_name}")
                        return False
                    self._delete_upgrade_old_strm(replace_item, replacement_path=strm_path)
                self._media_server_notifier.notify(
                    path=strm_path, mediainfo=mediainfo, file_name=target_name
                )
        elif self._local_resource_path:
            if is_upgrade and mode != "coexist" and replace_item and directory_snapshot:
                if not self._delete_upgrade_old_file(replace_item, directory_snapshot):
                    logger.debug(f"Magnet 洗版旧文件删除失败：{target_name}")
                    return False
                self._delete_upgrade_old_strm(replace_item)
            notify_path = self._owner.naming._resolve_resource_season_dir(
                self._local_resource_path,
                subscribe,
                mediainfo,
                max(1, int(season or 1)),
            )
            if notify_path:
                self._media_server_notifier.notify(
                    path=notify_path, mediainfo=mediainfo, file_name=target_name
                )
        return True

    def _persist_magnet_finalized_outputs(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            media_data: Optional[Dict[str, Any]],
            history_records: List[Dict[str, Any]],
            details: List[Dict[str, Any]],
            success_episodes: List[int],
    ) -> Optional[List[Dict[str, Any]]]:
        """统一结算 Magnet 匹配或自愈产生的订阅状态并返回详情（v1.13.3：
        成功不再写插件 history——终态只走 _write_terminal_state，失败才进
        重试索引；跨盘成功但落盘自检未通过写一条修复行）。"""
        if not history_records:
            return None
        if any(not record.get("skip_history") for record in history_records):
            self._write_terminal_state(item, pending_key, "成功")
        item["success_episodes"] = (
            [] if item.get("transient_target") else success_episodes
        )
        item["notification_episodes"] = (
            success_episodes if mediainfo.type == MediaType.TV else []
        )
        if media_data is not None and not item.get("transient_target"):
            self._finish_pending_subscription(
                item, media_data, mediainfo=mediainfo
            )
        return details

    def _try_recover_magnet_from_destination(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            subscribe: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        """当离线暂存目录文件为空时，检查目标媒体库是否已存在匹配文件（自愈机制）。"""
        season = item.get("season")
        episodes = []
        if mediainfo.type == MediaType.TV:
            for value in item.get("target_episodes") or []:
                try:
                    ep = int(str(value or "0"))
                    if ep > 0:
                        episodes.append(ep)
                except ValueError:
                    continue
            if not episodes:
                return None
        else:
            episodes = [None]

        recovered_files = []
        for ep in episodes:
            cloud_dir, target_name = self._owner.naming._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                item.get("file_name") or mediainfo.title,
                season=max(1, int(season or 1)) if ep else None,
                episode=ep,
            )
            if not cloud_dir or not target_name:
                return None
            dest_file = self._cloud_query.get_cached_file(cloud_dir, target_name)
            if not dest_file:
                lookup = self._cloud_directories.resolve_directory(cloud_dir)
                if lookup.checked and lookup.directory_id is not None:
                    listing = self._cloud_directories.list_directory(lookup.directory_id)
                    if listing.checked:
                        dest_file = next(
                            (f for f in listing.files if f.name == target_name or (
                                    MediaFileParser.is_video(f.name) and (
                                f"E{ep:02d}" in f.name.upper() if ep else True
                            )
                            )),
                            None
                        )
            if not dest_file:
                return None
            recovered_files.append((ep, dest_file, cloud_dir, dest_file.name or target_name))

        if not recovered_files:
            return None

        logger.debug(f"离线任务自愈成功：{item.get('file_name')}")
        history_records = []
        details = []
        success_episodes = []
        share_url = str(item.get("share_url") or "")
        resource = item.get("resource") or {}

        for ep, target_file, cloud_dir, final_name in recovered_files:
            self._finalize_magnet_single_output(
                cloud_dir=cloud_dir,
                target_name=final_name,
                target_file=target_file,
                mediainfo=mediainfo,
                subscribe=subscribe,
                season=season,
                episode=ep,
            )
            if ep:
                success_episodes.append(int(ep))
            else:
                success_episodes.append(1)

            episode_fields = (
                {"season": int(season or 1), "episode": int(ep)}
                if ep else {}
            )
            record = self._owner.transfer_details._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="成功",
                share_url=share_url,
                file_name=final_name,
                source_file_name=str(
                    item.get("staging_name") or item.get("source_name") or item.get("source_file_name") or final_name),
                cloud_dir=cloud_dir,
                resource=resource,
                file_size=getattr(target_file, "size", 0) or 0,
                source_sha1=str(getattr(target_file, "sha1", "") or ""),
                rule_score=0,
                upgrade=False,
                **episode_fields,
            )
            history_records.append(record)
            detail = {
                "type": record["type"],
                "title": mediainfo.title,
                "year": mediainfo.year,
                "image": mediainfo.get_poster_image(),
                "file_name": final_name,
            }
            if ep:
                detail.update({"season": int(season or 1), "episodes": [int(ep)]})
            details.append(detail)

        if history_records:
            apply_landed = getattr(
                self._owner.landed_verify, "apply_landed_verification_to_records", None
            )
            if callable(apply_landed):
                apply_landed(subscribe, mediainfo, season, history_records)
        return self._persist_magnet_finalized_outputs(
            item=item,
            pending_key=pending_key,
            mediainfo=mediainfo,
            media_data=None,
            history_records=history_records,
            details=details,
            success_episodes=success_episodes,
        )

    def _schedule_finalize_retry(self, item: Dict[str, Any], now: float) -> None:
        check_index = min(
            int(item.get("check_index") or 0) + 1,
            len(self._OFFLINE_CHECK_DELAYS) - 1,
        )
        item["check_index"] = check_index
        retry_at = now + self._OFFLINE_CHECK_DELAYS[check_index]
        if str(item.get("task_type") or "share") in {"ed2k", "magnet", "offline"}:
            created_at = float(item.get("created_at") or now)
            retry_at = min(retry_at, created_at + self._OFFLINE_TIMEOUT)
        item["next_check_at"] = retry_at
        retry_minutes = max(1, int(max(0, retry_at - now) + 59) // 60)
        logger.debug(
            f"文件后处理尚未完成：{item.get('file_name')}，"
            f"{retry_minutes} 分钟后复查"
        )

    def _finalize_failure(
            self, item: Dict[str, Any], pending_key: str, max_failures: Optional[int] = None,
            now: Optional[float] = None, ready_at: Optional[float] = None,
            timeout_seconds: Optional[float] = None,
    ) -> bool:
        """记录一次后处理实际失败（如重命名/移动/定位缺失）。

        达到连续失败上限后返回 True，并将任务标记为死任务（finalize_dead），
        由后续监控扫描彻底移出队列并记录失败历史与通知，终止无限重试。

        阈值与超时只在 service.py（SyncHandler）定义一处：离线任务用
        _FINALIZE_MAX_FAILURES，其余用 _FINALIZE_MAX_FAILURES_LOCAL。
        now/ready_at/timeout_seconds 同时给出时，定位等待超时也判死——
        但超时只约束插件自己的定位/移动步骤；MP 整理等待走
        _poll_mp_organize_state（90 分钟总超时），不走这里。
        """
        task_type = str(item.get("task_type") or "share").strip().lower()
        is_offline = task_type in {"ed2k", "magnet", "offline"}
        threshold = max_failures if max_failures is not None else (
            self._FINALIZE_MAX_FAILURES if is_offline else self._FINALIZE_MAX_FAILURES_LOCAL
        )
        fail_count = int(item.get("fail_count") or 0) + 1
        item["fail_count"] = fail_count
        timed_out = (
            now is not None and ready_at is not None and timeout_seconds is not None
            and float(now) - float(ready_at) >= float(timeout_seconds)
        )
        if fail_count < threshold and not timed_out:
            return False
        if fail_count >= threshold:
            item["finalize_dead"] = True
            item["next_check_at"] = 0.0
            logger.error(
                self._FINALIZE_DEAD_LOG.format(
                    fail_count, str(item.get("file_name") or pending_key)
                )
            )
        return True

    def _notify_finalize_dead(
            self, item: Dict[str, Any], pending_key: str
    ) -> None:
        post_msg = getattr(self, "_post_message", None) or getattr(self, "post_message", None)
        if not post_msg or not getattr(self, "_notify", False):
            return
        try:
            post_msg(
                mtype=self._notification_type,
                title=self._FINALIZE_DEAD_TITLE,
                text=self._FINALIZE_DEAD_TEXT.format(
                    str(item.get("file_name") or pending_key),
                    max(1, int(item.get("fail_count") or 0)),
                ),
            )
        except Exception as error:
            logger.debug(f"后处理失败通知发送异常：{error}", exc_info=True)
