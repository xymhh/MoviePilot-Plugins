"""
历史记录关联物理文件与网盘目录的级联清理服务。
"""
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import list_subscribes_by_tmdb_id
from ...utils.deep_delete_match import match_history_record_to_targets


class HistoryCleanupService(OwnerDelegator):
    """负责删除历史记录时级联清理本地 STRM 文件、元数据文件与网盘文件目录。"""

    _METADATA_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
    _SEASON_DIRECTORY_PATTERN = re.compile(r"^season[ ._-]*\d+$", re.IGNORECASE)

    def _refresh_deleted_media(self, records: List[Dict[str, Any]]) -> None:
        """删除关联文件后，按受影响 STRM 路径与媒体目录刷新媒体库。"""
        if not self._media_server_notifier or not records:
            return
        notified_dirs = set()
        for record in records:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            if not cloud_dir or not file_name or not self._local_resource_path:
                continue
            try:
                local_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
                if local_path.exists():
                    logger.warning(
                        f"关联 STRM 仍然存在，跳过媒体库删除通知：{local_path}"
                    )
                    continue
                self._media_server_notifier.notify_deleted_path(local_path, record)

                # 当季目录或整剧本地目录已不存在（如整体删除）时，将目录本身也纳入删除通知
                season_dir = local_path.parent
                if season_dir not in notified_dirs:
                    notified_dirs.add(season_dir)
                    if not season_dir.exists():
                        self._media_server_notifier.notify_deleted_path(season_dir, record)

                if self._is_season_directory(cloud_dir, record):
                    series_dir = season_dir.parent
                    if series_dir not in notified_dirs:
                        notified_dirs.add(series_dir)
                        if not series_dir.exists():
                            self._media_server_notifier.notify_deleted_path(series_dir, record)
            except Exception as error:
                logger.warning(
                    f"删除历史后刷新媒体库失败：{file_name} - {error}"
                )

    @staticmethod
    def _history_episodes(record: Dict[str, Any]) -> Set[int]:
        values = record.get("episodes") or record.get("notification_episodes")
        if values is None:
            values = [record.get("episode")]
        return {
            int(value) for value in values
            if str(value or "").isdigit() and int(value) > 0
        }

    def _refresh_deleted_subscribe_notes(
            self,
            deleted_records: List[Dict[str, Any]],
            remaining_history: List[Dict[str, Any]],
    ) -> None:
        """按删除后的剩余历史修正电视剧订阅 note 和缺集数。"""
        targets: Dict[Tuple[str, int], Set[int]] = {}
        for record in deleted_records:
            tmdb_id = str(record.get("tmdb_id") or "").strip()
            season = int(record.get("season") or 0)
            episodes = self._history_episodes(record)
            if tmdb_id and season > 0 and episodes:
                targets.setdefault((tmdb_id, season), set()).update(episodes)
        if not targets:
            return
        for (tmdb_id, season), deleted_episodes in targets.items():
            remaining_episodes = {
                episode
                for record in remaining_history
                if str(record.get("tmdb_id") or "").strip() == tmdb_id
                   and int(record.get("season") or 0) == season
                   and str(record.get("status") or "") == "成功"
                for episode in self._history_episodes(record)
            }
            for subscribe in list_subscribes_by_tmdb_id(
                    SubscribeOper(), int(tmdb_id), season):
                if str(getattr(subscribe, "type", "")) != MediaType.TV.value:
                    continue
                current_note = {
                    int(value) for value in (getattr(subscribe, "note", None) or [])
                    if str(value).isdigit()
                }
                new_note = sorted(current_note - deleted_episodes)
                if new_note == sorted(current_note):
                    continue
                start = int(getattr(subscribe, "start_episode", 1) or 1)
                total = int(getattr(subscribe, "total_episode", 0) or 0)
                expected = max(0, total - start + 1)
                lack = len(set(range(start, total + 1)) - set(new_note)) if expected else 0
                SubscribeOper().update(
                    subscribe.id,
                    {"note": new_note, "lack_episode": lack},
                )
                logger.info(
                    f"历史删除后更新订阅 note：{subscribe.name}，"
                    f"{sorted(current_note)} -> {new_note}"
                )

    def delete_by_media_server_paths(self, paths: List[str]) -> Dict[str, int]:
        """按媒体服务器路径精确/词干匹配并联动删除终态历史与源文件。"""
        normalized_targets = []
        target_stems = set()
        target_filenames = set()

        for path in paths:
            clean = self._normalize_media_server_path(path)
            if not clean:
                continue
            normalized_targets.append(clean)
            p = Path(clean)
            target_filenames.add(p.name.casefold())
            stem = p.stem.casefold()
            if stem:
                target_stems.add(stem)

        if not normalized_targets or not self._get_data:
            return {"matched": 0, "deleted": 0, "linked_deleted": 0,
                    "cache_deleted": 0, "skipped": 0}

        matched = []
        for record in self._get_data("history") or []:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            if not file_name:
                continue

            record_ms_path = ""
            if cloud_dir and self._local_resource_path:
                try:
                    local_path = self._path_mapper.local_path(
                        local_root=self._local_resource_path,
                        cloud_root=self._CLOUD_MEDIA_ROOT,
                        cloud_dir=cloud_dir,
                        file_name=file_name,
                    )
                    record_ms_path = self._normalize_media_server_path(
                        self._media_server_notifier.media_server_path(local_path)
                    )
                except Exception as error:
                    logger.debug(f"计算深度删除匹配路径失败：{file_name} - {error}")

            if match_history_record_to_targets(
                    file_name, record_ms_path, target_filenames,
                    target_stems, normalized_targets,
            ):
                matched.append(record)

        if not matched:
            # 兜底：该条目不是本插件转存的（无转存历史），但用户已在媒体服务器整剧删除，
            # 按媒体库路径反推网盘目录直接删除，避免网盘残留。
            fallback = self._delete_cloud_media_by_server_paths(normalized_targets)
            if fallback.get("matched"):
                return fallback
            samples = []
            for record in (self._get_data("history") or [])[:2000]:
                cloud_dir = str(record.get("cloud_dir") or "").strip()
                file_name = str(record.get("file_name") or "").strip()
                if cloud_dir or file_name:
                    samples.append(f"{cloud_dir}/{file_name}".strip("/"))
                if len(samples) >= 3:
                    break
            logger.debug(
                f"深度删除未命中诊断：目标={normalized_targets}，"
                f"历史样本={samples}"
            )
            return {"matched": 0, "deleted": 0, "linked_deleted": 0,
                    "cache_deleted": 0, "skipped": 0}
        result = self.delete_history_records(matched, delete_linked_files=True)
        return {"matched": len(matched), **result}

    def _virtual_media_path(self, value: Any) -> Optional[Path]:
        """媒体库条目已被删除时，按「媒体服务器路径=本地路径」映射推断本地路径。

        神医深度删除会先删掉本地 STRM 文件，webhook 到达时目标路径通常已不存在，
        此时无法靠 exists() 校验，只能按显式映射做纯前缀翻译；解出的相对路径仍要
        在媒体根之下、且层级由调用方继续校验。没有映射就返回 None（不猜）。
        """
        if not self._local_resource_path:
            return None
        raw_value = str(value or "").strip()
        if not raw_value:
            return None
        try:
            mappings = list(self._parsed_media_server_path_mappings())
        except Exception:
            mappings = []
        if not mappings:
            return None
        root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        for server_root, local_root in mappings:
            if raw_value != server_root and not raw_value.startswith(f"{server_root}/"):
                continue
            relative = raw_value[len(server_root):].lstrip("/")
            if not relative:
                continue
            translated = f"{local_root}/{relative}"
            translated_path = Path(translated).expanduser().resolve(strict=False)
            if translated_path != root and root not in translated_path.parents:
                continue
            return translated_path
        return None

    def _delete_cloud_media_by_server_paths(self, targets: List[str]) -> Dict[str, int]:
        """兜底删除：无转存历史时，按媒体服务器路径反推网盘目录并删除。

        只处理「整剧/整部电影目录」：反推出的网盘路径必须落在媒体库根之下且至少两级，
        目录需在网盘真实存在，删除走网盘回收站（可恢复），并同步清理本地 STRM 目录。
        """
        empty = {"matched": 0, "deleted": 0, "linked_deleted": 0,
                 "cache_deleted": 0, "skipped": 0,
                 "cloud_files_deleted": 0, "cloud_directories_deleted": 0,
                 "strm_deleted": 0}
        if not targets:
            return dict(empty)
        if not getattr(self, "_local_resource_path", ""):
            return dict(empty)
        if not getattr(self, "_cloud_directories", None) or not getattr(
                self, "_cloud_mutations", None
        ):
            logger.warning("深度删除兜底：网盘目录服务未初始化，已跳过")
            return dict(empty)

        local_root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        media_root_text = str(getattr(self, "_CLOUD_MEDIA_ROOT", "/") or "/")
        media_root = PurePosixPath(media_root_text)
        result = dict(empty)
        deleted_dirs: List[str] = []

        for target in targets:
            try:
                local_path, _reason = self._configured_media_path(target)
            except Exception as error:
                local_path = self._virtual_media_path(target)
                if local_path is None:
                    logger.info(f"深度删除兜底：无法定位媒体路径，跳过 {target} - {error}")
                    result["skipped"] += 1
                    continue
                logger.info(
                    f"深度删除兜底：本地媒体路径已不存在，按路径映射推断 {local_path}"
                    f"（原路径 {target}；{error}）"
                )
            resolved = Path(local_path).expanduser().resolve(strict=False)
            try:
                relative = resolved.relative_to(local_root)
            except ValueError:
                logger.warning(f"深度删除兜底：路径越出媒体根，跳过 {resolved}")
                result["skipped"] += 1
                continue
            # 至少保留「分类/媒体」两级边界，绝不删除媒体根或分类目录本身。
            if len(relative.parts) < 2:
                logger.warning(f"深度删除兜底：路径层级过浅，拒绝删除 {resolved}")
                result["skipped"] += 1
                continue
            if resolved.is_file():
                logger.info(f"深度删除兜底：目标是单文件而非媒体目录，跳过 {resolved}")
                result["skipped"] += 1
                continue
            cloud_dir = str(media_root.joinpath(*relative.parts))
            if self._is_protected_cloud_directory(cloud_dir):
                logger.warning(f"深度删除兜底：命中共保护目录，拒绝删除 {cloud_dir}")
                result["skipped"] += 1
                continue
            try:
                lookup = self._cloud_directories.resolve_directory(cloud_dir)
                if not lookup.checked or not lookup.directory_id:
                    logger.info(f"深度删除兜底：网盘目录不存在，跳过 {cloud_dir}")
                    result["skipped"] += 1
                    continue
                if not self._cloud_mutations.delete_file(lookup.directory_id):
                    logger.warning(f"深度删除兜底：网盘目录删除失败 {cloud_dir}")
                    result["skipped"] += 1
                    continue
            except Exception as error:
                logger.warning(f"深度删除兜底：删除网盘目录失败 {cloud_dir} - {error}")
                result["skipped"] += 1
                continue
            deleted_dirs.append(cloud_dir)
            result["cloud_directories_deleted"] += 1
            result["strm_deleted"] += self._purge_local_media_directory(resolved)

        if deleted_dirs:
            result["matched"] = len(deleted_dirs)
            result["deleted"] = len(deleted_dirs)
            logger.info(
                f"深度删除兜底完成：条目无转存历史，按媒体库路径反查并删除网盘目录 "
                f"{len(deleted_dirs)} 个：{deleted_dirs}"
            )
        return result

    def _purge_local_media_directory(self, directory: Path) -> int:
        """删除媒体服务器已移除的本地 STRM 目录，返回删除的文件数。"""
        if not getattr(self, "_local_resource_path", ""):
            return 0
        local_root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        resolved = directory.expanduser().resolve(strict=False)
        if local_root not in resolved.parents:
            return 0
        try:
            relative = resolved.relative_to(local_root)
        except ValueError:
            return 0
        if len(relative.parts) < 2:
            return 0
        if not resolved.is_dir():
            return 0
        try:
            removed = sum(1 for item in resolved.rglob("*") if item.is_file())
            shutil.rmtree(resolved)
        except Exception as error:
            logger.warning(f"深度删除兜底：清理本地媒体目录失败 {resolved} - {error}")
            return 0
        return removed

    @staticmethod
    def _normalize_media_server_path(path: Any) -> str:
        """标准化用于精确比较的媒体服务器路径。"""
        value = str(path or "").strip().replace("\\", "/")
        while "//" in value:
            value = value.replace("//", "/")
        return value.rstrip("/")

    def _delete_history_cache(self, record: Dict[str, Any]) -> int:
        if not self._cross_transfer_manager:
            return 0
        cache_key = str(record.get("cache_key") or "").strip()
        if not cache_key:
            return 0
        return self._cross_transfer_manager.delete_cache(cache_key)

    def _delete_history_linked_files_batch(
            self, records: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """按网盘目录聚合删除；目录内容全部命中时直接删除目录。"""
        preflight = self._preflight_linked_media_directories(records)
        local_handled = preflight["local_handled"]
        cloud_handled = preflight["cloud_handled"]
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        strm_deleted = preflight["strm_deleted"]
        local_directories_deleted = preflight["local_directories_deleted"]
        local_cleanup_targets: Dict[Path, Path] = {}
        for record in records:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            record_key = id(record)
            if cloud_dir and file_name and record_key not in cloud_handled:
                grouped.setdefault(cloud_dir, []).append(record)
            if (
                    record_key in local_handled
                    or not self._local_resource_path
                    or not cloud_dir
                    or not file_name
            ):
                continue
            try:
                strm_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
                if strm_path.is_file():
                    strm_path.unlink()
                    strm_deleted += 1
                self._delete_local_metadata_for_stem(strm_path)
                local_directory = strm_path.parent
                media_directory = (
                    local_directory.parent
                    if self._is_season_directory(cloud_dir, record)
                    else local_directory
                )
                local_cleanup_targets[local_directory] = media_directory
            except Exception as error:
                logger.warning(f"批量删除关联 STRM 失败：{file_name} - {error}")

        for local_directory, media_directory in sorted(
                local_cleanup_targets.items(),
                key=lambda item: len(item[0].parts),
                reverse=True,
        ):
            local_directories_deleted += self._cleanup_local_metadata_directory(
                local_directory
            )
            if media_directory != local_directory:
                local_directories_deleted += self._cleanup_local_metadata_directory(
                    media_directory
                )

        cloud_deleted = preflight["cloud_files_deleted"]
        directories_deleted = preflight["cloud_directories_deleted"]
        for cloud_dir, directory_records in grouped.items():
            try:
                lookup = self._cloud_directories.resolve_directory(cloud_dir)
                if not lookup.checked:
                    raise RuntimeError("无法确认目录状态")
                if not lookup.directory_id:
                    continue
                listing = self._cloud_directories.list_directory(lookup.directory_id)
                if not listing.checked:
                    raise RuntimeError("无法读取目录内容")
                target_names = {
                    str(record.get("file_name") or "").strip()
                    for record in directory_records
                }
                target_stems = {Path(name).stem.lower() for name in target_names}
                target_files = [
                    item for item in listing.files if item.name in target_names
                ]
                remaining_files = [
                    item for item in listing.files if item.name not in target_names
                ]
                if self._cloud_items_are_generated_metadata(
                        remaining_files, target_stems
                ):
                    if not self._is_protected_cloud_directory(cloud_dir):
                        if self._cloud_mutations.delete_file(lookup.directory_id):
                            cloud_deleted += len(target_files)
                            directories_deleted += 1
                            if self._is_season_directory(
                                    cloud_dir, directory_records[0]
                            ):
                                directories_deleted += self._cleanup_cloud_media_parent(
                                    cloud_dir
                                )
                    continue
                file_ids = [item.id for item in target_files]
                if not file_ids:
                    continue
                deleted_ids = self._cloud_batch_mutations.delete_files(file_ids)
                cloud_deleted += len(deleted_ids)
            except Exception as error:
                logger.warning(f"批量删除关联网盘内容失败：{cloud_dir} - {error}")

        logger.info(
            f"历史联动批量删除完成：历史记录={len(records)} 条，"
            f"网盘文件={cloud_deleted} 个，网盘目录={directories_deleted} 个，"
            f"STRM={strm_deleted} 个，本地目录={local_directories_deleted} 个"
        )
        return {
            "cloud_files_deleted": cloud_deleted,
            "cloud_directories_deleted": directories_deleted,
            "strm_deleted": strm_deleted,
            "local_directories_deleted": local_directories_deleted,
        }

    def _preflight_linked_media_directories(
            self, records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """删除前扫描一次STRM；整季或整部命中时直接回收目录。"""
        result = {
            "local_handled": set(),
            "cloud_handled": set(),
            "cloud_files_deleted": 0,
            "cloud_directories_deleted": 0,
            "strm_deleted": 0,
            "local_directories_deleted": 0,
        }
        if not self._local_resource_path:
            return result
        media_groups: Dict[Path, List[Dict[str, Any]]] = {}
        for record in records:
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            file_name = str(record.get("file_name") or "").strip()
            if not cloud_dir or not file_name:
                continue
            try:
                strm_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                )
            except Exception:
                continue
            is_season = self._is_season_directory(cloud_dir, record)
            media_directory = strm_path.parent.parent if is_season else strm_path.parent
            cloud_media_dir = str(PurePosixPath(cloud_dir).parent) if is_season else cloud_dir
            media_groups.setdefault(media_directory, []).append({
                "record": record,
                "strm_path": strm_path,
                "season_directory": strm_path.parent if is_season else None,
                "cloud_directory": cloud_dir,
                "cloud_media_directory": cloud_media_dir or "/",
            })

        for media_directory, entries in media_groups.items():
            if not self._is_safe_local_media_directory(media_directory):
                continue
            target_paths = {
                entry["strm_path"].resolve(strict=False) for entry in entries
            }
            current_strms = {
                path.resolve(strict=False) for path in media_directory.rglob("*.strm")
            } if media_directory.is_dir() else set()
            if media_directory.is_dir() and current_strms.issubset(target_paths):
                result["strm_deleted"] += len(current_strms & target_paths)
                cloud_path = entries[0]["cloud_media_directory"]
                if self._delete_cloud_directory_direct(cloud_path):
                    result["cloud_handled"].update(id(entry["record"]) for entry in entries)
                    result["cloud_files_deleted"] += len(entries)
                    result["cloud_directories_deleted"] += 1
                shutil.rmtree(media_directory)
                result["local_handled"].update(id(entry["record"]) for entry in entries)
                result["local_directories_deleted"] += 1
                continue

            season_groups: Dict[Path, List[Dict[str, Any]]] = {}
            for entry in entries:
                season_directory = entry["season_directory"]
                if season_directory:
                    season_groups.setdefault(season_directory, []).append(entry)
            for season_directory, season_entries in season_groups.items():
                if not self._is_safe_local_media_directory(season_directory):
                    continue
                season_targets = {
                    entry["strm_path"].resolve(strict=False) for entry in season_entries
                }
                season_strms = {
                    path.resolve(strict=False) for path in season_directory.glob("*.strm")
                } if season_directory.is_dir() else set()
                if not season_directory.is_dir() or not season_strms.issubset(season_targets):
                    continue
                result["strm_deleted"] += len(season_strms & season_targets)
                cloud_path = season_entries[0]["cloud_directory"]
                if self._delete_cloud_directory_direct(cloud_path):
                    result["cloud_handled"].update(
                        id(entry["record"]) for entry in season_entries
                    )
                    result["cloud_files_deleted"] += len(season_entries)
                    result["cloud_directories_deleted"] += 1
                shutil.rmtree(season_directory)
                result["local_handled"].update(
                    id(entry["record"]) for entry in season_entries
                )
                result["local_directories_deleted"] += 1
        return result

    def _is_safe_local_media_directory(self, directory: Path) -> bool:
        local_root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        resolved = directory.resolve(strict=False)
        if local_root not in resolved.parents:
            return False
        try:
            relative = resolved.relative_to(local_root)
        except ValueError:
            return False
        return len(relative.parts) >= 2

    def _is_protected_cloud_directory(self, cloud_dir: str) -> bool:
        """检查网盘路径是否为受保护的系统路径（如根目录、转存路径、媒体库根目录及其父级），严禁级联删除。"""
        candidate = PurePosixPath(str(cloud_dir or "/").strip())
        candidate_str = str(candidate).rstrip("/") or "/"
        if candidate_str == "/":
            return True
        protected_paths = set()
        media_root = getattr(self, "_CLOUD_MEDIA_ROOT", "/") or "/"
        protected_paths.add(PurePosixPath(media_root))
        transfer_path = getattr(self, "_cloud_transfer_path", None)
        if transfer_path:
            protected_paths.add(PurePosixPath(transfer_path))
        all_transfer_paths = getattr(self, "_cloud_transfer_paths", {}) or {}
        if isinstance(all_transfer_paths, dict):
            for p in all_transfer_paths.values():
                if p:
                    protected_paths.add(PurePosixPath(p))
        for prot in protected_paths:
            prot_str = str(prot).rstrip("/") or "/"
            if not prot_str:
                continue
            if candidate_str == prot_str:
                return True
            try:
                prot.relative_to(candidate)
                return True
            except ValueError:
                pass
        return False

    def _delete_cloud_directory_direct(self, cloud_dir: str) -> bool:
        if self._is_protected_cloud_directory(cloud_dir):
            logger.warning(f"拒绝删除受保护的网盘目录：{cloud_dir}")
            return False
        if len(self._cloud_media_relative_parts(cloud_dir)) < 2:
            return False
        try:
            lookup = self._cloud_directories.resolve_directory(cloud_dir)
            return bool(
                lookup.checked
                and lookup.directory_id
                and self._cloud_mutations.delete_file(lookup.directory_id)
            )
        except Exception as error:
            logger.warning(f"直接删除关联网盘目录失败，将回退逐文件删除：{cloud_dir} - {error}")
            return False

    @classmethod
    def _is_generated_metadata_name(
            cls, name: str, target_stems: Optional[Set[str]] = None
    ) -> bool:
        path = Path(str(name or ""))
        stem = path.stem.lower()
        suffix = path.suffix.lower()
        target_stems = target_stems or set()
        if suffix == ".nfo":
            return stem in {"tvshow", "season"} or stem in target_stems
        if suffix not in cls._METADATA_IMAGE_SUFFIXES:
            return False
        return bool(
            stem in {"poster", "fanart"}
            or re.fullmatch(r"season\d{2}-poster", stem)
            or stem == "season-specials-poster"
            or any(stem == f"{target_stem}-thumb" for target_stem in target_stems)
        )

    @classmethod
    def _cloud_items_are_generated_metadata(
            cls, items: List[Any], target_stems: Optional[Set[str]] = None
    ) -> bool:
        return all(
            not item.is_directory
            and cls._is_generated_metadata_name(item.name, target_stems)
            for item in items
        )

    @classmethod
    def _is_season_directory(
            cls, cloud_dir: str, record: Dict[str, Any]
    ) -> bool:
        directory_name = PurePosixPath(str(cloud_dir or "/")).name
        return bool(
            cls._SEASON_DIRECTORY_PATTERN.fullmatch(directory_name)
            or record.get("season") not in (None, "")
        )

    @classmethod
    def _delete_local_metadata_for_stem(cls, strm_path: Path) -> int:
        deleted = 0
        candidates = [strm_path.with_suffix(".nfo")]
        candidates.extend(
            item
            for item in strm_path.parent.glob(f"{strm_path.stem}-thumb.*")
            if item.suffix.lower() in cls._METADATA_IMAGE_SUFFIXES
        )
        for candidate in candidates:
            if candidate.is_file():
                candidate.unlink()
                deleted += 1
        return deleted

    def _cleanup_local_metadata_directory(self, directory: Path) -> int:
        if not directory.is_dir():
            return 0
        local_root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        resolved = directory.resolve(strict=False)
        if local_root not in resolved.parents:
            return 0
        try:
            relative = resolved.relative_to(local_root)
        except ValueError:
            return 0
        # 至少保留“分类/媒体”两级边界，绝不清理本地资源根或分类目录。
        if len(relative.parts) < 2:
            return 0
        entries = list(directory.iterdir())
        if any(
                item.is_dir() or not self._is_generated_metadata_name(item.name)
                for item in entries
        ):
            return 0
        for item in entries:
            item.unlink()
        directory.rmdir()
        return 1

    def _cleanup_cloud_media_parent(self, cloud_dir: str) -> int:
        child_path = PurePosixPath(cloud_dir)
        parent_path = str(child_path.parent) or "/"
        if self._is_protected_cloud_directory(parent_path):
            logger.debug(f"父级目录属于受保护路径，跳过清理：{parent_path}")
            return 0
        if len(self._cloud_media_relative_parts(parent_path)) < 2:
            return 0
        lookup = self._cloud_directories.resolve_directory(parent_path)
        if not lookup.checked or not lookup.directory_id:
            return 0
        listing = self._cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return 0
        remaining = [
            item for item in listing.files
            if not (item.is_directory and item.name == child_path.name)
        ]
        if not self._cloud_items_are_generated_metadata(remaining):
            return 0
        return int(bool(self._cloud_mutations.delete_file(lookup.directory_id)))

    def _cloud_media_relative_parts(self, path: str) -> Tuple[str, ...]:
        root = PurePosixPath(self._CLOUD_MEDIA_ROOT)
        candidate = PurePosixPath(str(path or "/"))
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return ()
        return tuple(part for part in relative.parts if part not in {"", ".", "/"})

    def _delete_history_linked_files(
            self, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        """尽力删除115目标文件及本地STRM，任何失败均不阻止历史删除。"""
        file_name = str(record.get("file_name") or "").strip()
        result = self._delete_history_linked_files_batch([record])
        cloud_deleted = (
                result["cloud_files_deleted"] > 0
                or result["cloud_directories_deleted"] > 0
        )
        strm_deleted = result["strm_deleted"] > 0
        return {
            "cloud_file_deleted": cloud_deleted,
            "cloud_file_error": "",
            "strm_deleted": strm_deleted,
            "strm_error": "",
            "strm_path": "",
            "cloud_directories_deleted": result["cloud_directories_deleted"],
            "local_directories_deleted": result["local_directories_deleted"],
        }
