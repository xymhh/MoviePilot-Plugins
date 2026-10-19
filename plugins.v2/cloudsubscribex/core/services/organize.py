"""115 网盘整理台：扫描预览、批量重命名/移动、STRM 与定时执行。"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas.types import MediaType

from ...core import CloudDriveCapability, OwnerDelegator
from ...utils.organize_plan import (
    OrganizeEntry,
    PackagePlan,
    build_packages,
    safe_join,
    scan_entries,
    suffix_name,
)


@dataclass
class TargetFile:
    """一个待整理文件的目标。"""

    file_id: str
    source_name: str
    source_dir: str
    target_dir: str
    target_name: str
    episode: int = 0
    is_subtitle: bool = False


@dataclass
class GroupPlan:
    """一个作品包的完整计划。"""

    key: str
    group: Dict[str, Any]
    package: PackagePlan
    media: Any
    targets: List[TargetFile] = field(default_factory=list)
    subtitles: List[TargetFile] = field(default_factory=list)


class OrganizeService(OwnerDelegator):
    """整理台服务；状态只保存在当前插件进程内，重启后从历史摘要开始。"""

    _MAX_HISTORY = 20
    _MAX_GROUPS_PREVIEW = 500

    def __init__(self, owner):
        super().__init__(owner)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {}
        self._plans: Dict[str, GroupPlan] = {}
        self._history: List[Dict[str, Any]] = []
        self._target_name_cache: Dict[str, Optional[set]] = {}
        self._scan_id = ""

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def start_scan(
            self,
            source_path: str = "",
            max_files: int = 0,
            include_subtitles: Optional[bool] = None,
            auto_execute: bool = False,
            trigger: str = "手动",
    ) -> Dict[str, Any]:
        source = str(source_path or self._organize_source_path or "").strip()
        if not source:
            return {"success": False, "message": "请先选择 115 待整理源目录"}
        if not source.startswith("/"):
            source = f"/{source}"
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"success": False, "message": "已有整理任务在运行，请先等待完成或停止"}
            scan_id = uuid.uuid4().hex[:12]
            self._scan_id = scan_id
            self._stop_event.clear()
            self._plans = {}
            self._target_name_cache = {}
            settings = self._settings_snapshot()
            settings["source_path"] = source
            settings["max_files"] = self._normalize_max_files(max_files or settings["max_files"])
            if include_subtitles is not None:
                settings["include_subtitles"] = bool(include_subtitles)
            self._state = {
                "scan_id": scan_id,
                "phase": "scanning",
                "message": "正在扫描目录",
                "source_path": source,
                "trigger": trigger,
                "settings": settings,
                "created_at": time.time(),
                "updated_at": time.time(),
                "stats": {
                    "directories": 0, "files": 0, "videos": 0, "subtitles": 0,
                    "groups": 0, "ready": 0, "unmatched": 0, "conflict": 0,
                    "items": 0, "done": 0, "failed": 0, "skipped": 0,
                },
                "groups": [],
                "errors": [],
                "execution": {},
                "auto_execute": bool(auto_execute),
            }
            self._thread = threading.Thread(
                target=self._scan_worker,
                args=(source, settings, auto_execute, trigger),
                name="cloudsubscribe-organize-scan",
                daemon=True,
            )
            self._thread.start()
        return {"success": True, "scan_id": scan_id, "message": "已开始扫描"}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state) if self._state else {
                "phase": "idle",
                "message": "尚未扫描",
                "settings": self._settings_snapshot(),
                "groups": [],
                "stats": {},
            }
        state["settings"] = self._settings_snapshot()
        state["history"] = copy.deepcopy(self._history[-self._MAX_HISTORY:])
        return {"success": True, "data": state}

    def execute(self, keys: Optional[Sequence[str]] = None, trigger: str = "手动") -> Dict[str, Any]:
        with self._lock:
            if not self._state or self._state.get("phase") not in {"ready", "done", "cancelled"}:
                return {"success": False, "message": "请先完成扫描预览"}
            if self._thread and self._thread.is_alive():
                return {"success": False, "message": "已有任务在运行"}
            selected = [str(key) for key in (keys or []) if str(key).strip()]
            plans = [
                plan for key, plan in self._plans.items()
                if plan.group.get("status") == "ready" and (not selected or key in selected)
            ]
            if not plans:
                return {"success": False, "message": "没有可执行的 ready 分组"}
            self._start_execution(plans, trigger)
        return {"success": True, "message": f"已开始整理 {len(plans)} 个分组"}

    def _start_execution(self, plans: Sequence[GroupPlan], trigger: str) -> None:
        """调用方需持有锁；从扫描线程自动执行时复用，不再做线程互斥检查。"""
        self._stop_event.clear()
        self._state["phase"] = "executing"
        self._state["message"] = f"正在整理 {len(plans)} 个分组"
        self._state["execution"] = {
            "trigger": trigger,
            "selected": len(plans),
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
            "started_at": time.time(),
        }
        self._state["stats"]["done"] = 0
        self._state["stats"]["failed"] = 0
        self._state["stats"]["skipped"] = 0
        self._thread = threading.Thread(
            target=self._execute_worker,
            args=(plans, trigger),
            name="cloudsubscribe-organize-execute",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> Dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            if self._state:
                self._state["message"] = "正在停止…"
        return {"success": True, "message": "已请求停止"}

    def history(self) -> Dict[str, Any]:
        return {"success": True, "data": copy.deepcopy(self._history[-self._MAX_HISTORY:])}

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _scan_worker(self, source: str, settings: Dict[str, Any], auto_execute: bool, trigger: str) -> None:
        started = time.time()
        try:
            drive = getattr(self, "_cloud_drive", None)
            if not drive:
                raise RuntimeError("网盘未登录或未就绪")
            directories = drive.require(CloudDriveCapability.DIRECTORY_READ)
            result = scan_entries(
                directories,
                source,
                max_files=settings["max_files"],
                max_depth=settings["max_depth"],
                max_entries=max(settings["max_files"] * 5, 2000),
                stop_requested=self._stop_event.is_set,
                progress=self._update_scan_stats,
            )
            with self._lock:
                if self._state.get("scan_id") != self._scan_id:
                    return
                self._state["stats"].update({
                    "directories": result.stats.directories,
                    "files": result.stats.files,
                    "videos": result.stats.videos,
                    "subtitles": result.stats.subtitles,
                    "errors": result.stats.errors,
                    "truncated": result.stats.truncated,
                })
                self._state["errors"] = list(result.errors[:20])
                if result.stats.truncated:
                    self._state["message"] = "扫描达到上限，结果仅包含已扫描部分"
                if self._stop_event.is_set():
                    self._state["phase"] = "cancelled"
                    self._state["message"] = "扫描已停止"
                    return
            if not result.entries:
                with self._lock:
                    self._state["phase"] = "failed"
                    self._state["message"] = "源目录没有可整理的视频文件"
                return
            packages = build_packages(
                result.entries,
                source,
                video_extensions=getattr(self, "_video_extensions", None),
                subtitle_extensions=getattr(self, "_subtitle_extensions", None),
            )
            with self._lock:
                self._state["message"] = f"正在识别 {len(packages)} 个作品包"
                self._state["phase"] = "recognizing"
            plans: List[GroupPlan] = []
            for index, package in enumerate(packages[:self._MAX_GROUPS_PREVIEW], start=1):
                if self._stop_event.is_set():
                    break
                plan = self._build_group_plan(package, index)
                plans.append(plan)
                with self._lock:
                    self._state["groups"].append(plan.group)
                    self._state["stats"]["groups"] = len(plans)
                    self._state["message"] = f"正在识别 {index}/{min(len(packages), self._MAX_GROUPS_PREVIEW)}"
            with self._lock:
                self._plans = {plan.key: plan for plan in plans}
                stats = self._state["stats"]
                stats["groups"] = len(plans)
                stats["ready"] = sum(1 for plan in plans if plan.group["status"] == "ready")
                stats["unmatched"] = sum(1 for plan in plans if plan.group["status"] == "unmatched")
                stats["conflict"] = sum(1 for plan in plans if plan.group["status"] == "conflict")
                stats["items"] = sum(len(plan.targets) + len(plan.subtitles) for plan in plans)
                if self._stop_event.is_set():
                    self._state["phase"] = "cancelled"
                    self._state["message"] = "扫描已停止"
                else:
                    self._state["phase"] = "ready"
                    self._state["message"] = (
                        f"扫描完成：{len(plans)} 个分组，"
                        f"{stats['ready']} 个可执行，{stats['unmatched']} 个待确认"
                    )
            logger.info(
                f"整理台扫描完成（{trigger}）：源={source}，目录={result.stats.directories}，"
                f"视频={result.stats.videos}，分组={len(plans)}，可执行={stats['ready']}"
            )
            if auto_execute and not self._stop_event.is_set():
                ready_plans = [
                    plan for plan in plans if plan.group.get("status") == "ready"
                ]
                if ready_plans:
                    with self._lock:
                        self._start_execution(ready_plans, f"{trigger}（自动）")
                else:
                    self._finish_history(trigger, started, note="没有可自动执行的分组")
        except Exception as error:  # noqa: BLE001
            logger.error(f"整理台扫描失败：{error}", exc_info=True)
            with self._lock:
                self._state["phase"] = "failed"
                self._state["message"] = f"扫描失败：{error}"
                self._state["errors"] = [str(error)]

    def _update_scan_stats(self, stats) -> None:
        with self._lock:
            if not self._state:
                return
            self._state["stats"].update({
                "directories": stats.directories,
                "files": stats.files,
                "videos": stats.videos,
                "subtitles": stats.subtitles,
            })
            self._state["updated_at"] = time.time()

    def _build_group_plan(self, package: PackagePlan, index: int) -> GroupPlan:
        group: Dict[str, Any] = {
            "key": package.key,
            "title": package.pack_name,
            "media_type": "tv" if package.is_tv else "movie",
            "season": package.videos[0].season if package.videos else 0,
            "year": None,
            "tmdb_id": None,
            "status": "unmatched",
            "reason": "",
            "target_dir": "",
            "episode_count": len(package.videos),
            "subtitle_count": sum(len(video.subtitles) for video in package.videos),
            "ignored_count": len(package.ignored),
            "duplicate_count": len(package.duplicates),
            "items": [],
        }
        if not package.videos:
            group["reason"] = "没有解析到视频"
            return GroupPlan(key=package.key, group=group, package=package, media=None)
        try:
            media = self._recognize_package(package)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"整理台识别失败：{package.pack_name}：{error}")
            media = None
        if not media:
            group["reason"] = "媒体识别失败，请确认目录名或手动改名后重扫"
            return GroupPlan(key=package.key, group=group, package=package, media=None)
        group["title"] = getattr(media, "title", None) or package.pack_name
        group["year"] = getattr(media, "year", None)
        group["tmdb_id"] = getattr(media, "tmdb_id", None)
        group["season"] = int(package.videos[0].season or 0)

        targets: List[TargetFile] = []
        subtitles: List[TargetFile] = []
        for candidate in package.videos:
            episode = int(candidate.episode or 0) if package.is_tv else 0
            try:
                naming = getattr(self, "_sync_handler", None)
                if not naming:
                    raise RuntimeError("同步服务尚未初始化")
                target_dir, target_name = naming._platform_target(  # noqa: SLF001
                    "/",
                    None,
                    media,
                    candidate.entry.name,
                    season=int(candidate.season or 1) if package.is_tv else None,
                    episode=episode or None,
                )
            except Exception as error:  # noqa: BLE001
                logger.debug(f"整理台目标路径失败：{candidate.entry.path}：{error}")
                group["reason"] = f"目标路径生成失败：{error}"
                break
            if not target_dir or not target_name:
                group["reason"] = "目标路径为空，请检查 MoviePilot 媒体库目录配置"
                break
            target_dir = "/" + str(target_dir).strip("/")
            targets.append(TargetFile(
                file_id=candidate.entry.file_id,
                source_name=candidate.entry.name,
                source_dir=candidate.entry.parent_path,
                target_dir=target_dir,
                target_name=target_name,
                episode=episode,
            ))
            for subtitle in candidate.subtitles:
                label = subtitle.stem
                if label.lower().startswith(candidate.entry.stem.lower()):
                    label = label[len(candidate.entry.stem):]
                label = label.strip(" ._-")
                subtitle_name = (
                    f"{PurePosixPath(target_name).stem}"
                    f"{'.' + label if label else ''}{subtitle.suffix}"
                )
                subtitles.append(TargetFile(
                    file_id=subtitle.file_id,
                    source_name=subtitle.name,
                    source_dir=subtitle.parent_path,
                    target_dir=target_dir,
                    target_name=subtitle_name,
                    episode=episode,
                    is_subtitle=True,
                ))
        if targets:
            group["status"] = "ready"
            group["target_dir"] = targets[0].target_dir
            if group["duplicate_count"]:
                group["reason"] = f"已自动保留 {len(package.duplicates)} 个重复集的较大版本"
            group["items"] = [
                {
                    "name": item.source_name,
                    "target_name": item.target_name,
                    "episode": item.episode,
                    "kind": "subtitle" if item.is_subtitle else "video",
                }
                for item in targets + subtitles
            ]
        else:
            group["status"] = "unmatched"
            group["reason"] = group["reason"] or "没有可执行的目标文件"
        return GroupPlan(
            key=package.key,
            group=group,
            package=package,
            media=media,
            targets=targets,
            subtitles=subtitles,
        )

    def _recognize_package(self, package: PackagePlan):
        media_type = MediaType.TV if package.is_tv else MediaType.MOVIE
        meta = MetaInfo(package.pack_name)
        meta.type = media_type
        if package.is_tv:
            season = max(1, int(package.videos[0].season or 1))
            meta.begin_season = season
            meta.begin_episode = max(1, int(package.videos[0].episode or 1))
        kwargs = {"meta": meta, "mtype": media_type, "cache": True}
        recognizer = getattr(self, "_recognize_media_once", None)
        if recognizer:
            key = (
                "organize", str(getattr(media_type, "value", media_type)),
                package.pack_name, getattr(meta, "year", None),
                package.videos[0].season if package.is_tv else None,
            )
            return recognizer(key, **kwargs)
        from ...core.media import recognize_media
        return recognize_media(getattr(self, "chain", None), **kwargs)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def _execute_worker(self, plans: Sequence[GroupPlan], trigger: str) -> None:
        started = time.time()
        total_done = total_failed = total_skipped = 0
        for plan in plans:
            if self._stop_event.is_set():
                break
            with self._lock:
                self._state["message"] = f"正在整理：{plan.group.get('title')}"
            try:
                result = self._execute_plan(plan)
            except Exception as error:  # noqa: BLE001
                logger.error(f"整理台执行分组失败：{plan.key}：{error}", exc_info=True)
                result = {"done": 0, "failed": 1, "skipped": 0, "items": [{
                    "name": plan.group.get("title"), "success": False, "reason": str(error),
                }]}
            total_done += result["done"]
            total_failed += result["failed"]
            total_skipped += result["skipped"]
            with self._lock:
                plan.group["status"] = "done" if not result["failed"] else "partial"
                plan.group["result"] = {
                    "done": result["done"],
                    "failed": result["failed"],
                    "skipped": result["skipped"],
                }
                stats = self._state["stats"]
                stats["done"] = total_done
                stats["failed"] = total_failed
                stats["skipped"] = total_skipped
                execution = self._state.setdefault("execution", {})
                execution["done"] = total_done
                execution["failed"] = total_failed
                execution["skipped"] = total_skipped
                execution.setdefault("items", []).extend(result["items"][:50])
                execution["items"] = execution["items"][:300]
        cancelled = self._stop_event.is_set()
        with self._lock:
            self._state["phase"] = "cancelled" if cancelled else "done"
            self._state["message"] = (
                "整理已停止" if cancelled else
                f"整理完成：成功 {total_done}，跳过 {total_skipped}，失败 {total_failed}"
            )
            self._state["finished_at"] = time.time()
        self._finish_history(trigger, started, done=total_done, failed=total_failed, skipped=total_skipped,
                             cancelled=cancelled)

    def _execute_plan(self, plan: GroupPlan) -> Dict[str, Any]:
        done = failed = skipped = 0
        items: List[Dict[str, Any]] = []
        policy = str(getattr(self, "_organize_conflict_policy", "skip") or "skip")
        # 视频先处理；视频被跳过后其字幕也不再搬。
        moved_episodes = set()
        moved_movie_dirs = set()
        for target in plan.targets:
            if self._stop_event.is_set():
                break
            try:
                success, actual_name, reason = self._move_target(target, policy)
            except Exception as error:  # noqa: BLE001
                success, actual_name, reason = False, target.target_name, str(error)
            moved = bool(success)
            if success:
                done += 1
                self._generate_strm_for_target(target, actual_name)
                if target.episode:
                    moved_episodes.add(target.episode)
                else:
                    moved_movie_dirs.add(target.target_dir)
            elif reason.startswith("skip:"):
                skipped += 1
                success = True
            else:
                failed += 1
            items.append({
                "name": target.source_name,
                "episode": target.episode,
                "target": safe_join(target.target_dir, actual_name or target.target_name),
                "success": bool(success),
                "reason": reason.removeprefix("skip:") if reason else "",
            })
            if not moved:
                continue

        video_success_episodes = moved_episodes
        for target in plan.subtitles:
            if self._stop_event.is_set():
                break
            video_missing = (
                target.episode not in video_success_episodes
                if target.episode else target.target_dir not in moved_movie_dirs
            )
            if video_missing:
                skipped += 1
                items.append({
                    "name": target.source_name,
                    "target": safe_join(target.target_dir, target.target_name),
                    "success": True,
                    "reason": "对应视频未搬，字幕跳过",
                })
                continue
            try:
                success, actual_name, reason = self._move_target(target, policy)
            except Exception as error:  # noqa: BLE001
                success, actual_name, reason = False, target.target_name, str(error)
            if success:
                done += 1
            elif reason.startswith("skip:"):
                skipped += 1
                success = True
            else:
                failed += 1
            items.append({
                "name": target.source_name,
                "target": safe_join(target.target_dir, actual_name or target.target_name),
                "success": bool(success),
                "reason": reason.removeprefix("skip:") if reason else "",
            })
        return {"done": done, "failed": failed, "skipped": skipped, "items": items}

    def _move_target(self, target: TargetFile, policy: str) -> Tuple[bool, str, str]:
        drive = getattr(self, "_cloud_drive", None)
        if not drive:
            return False, target.target_name, "网盘未就绪"
        batch = drive.require(CloudDriveCapability.BATCH_FILE_MUTATION) \
            if drive.supports(CloudDriveCapability.BATCH_FILE_MUTATION) else None
        mutations = drive.require(CloudDriveCapability.FILE_MUTATION)
        entry = self._find_entry(target)
        if not entry:
            return False, target.target_name, "源文件已不存在"

        final_name = target.target_name
        existing = self._directory_names(target.target_dir)
        if existing is None:
            return False, final_name, "无法读取目标目录，已跳过（避免覆盖）"
        if target.source_dir == target.target_dir and target.source_name == final_name:
            return True, final_name, ""
        if existing is not None and final_name in existing:
            if policy != "suffix":
                return False, final_name, "skip:目标同名文件已存在"
            for index in range(2, 100):
                candidate = suffix_name(final_name, index)
                if candidate not in existing:
                    final_name = candidate
                    break
            else:
                return False, final_name, "目标同名文件过多，已跳过"
            existing.add(final_name)

        renamed: Optional[Any] = entry
        if target.source_name != final_name:
            try:
                if batch:
                    result = batch.rename_files(target.source_dir, {
                        target.file_id: {"item": entry, "target_name": final_name},
                    }) or {}
                    renamed = result.get(target.file_id)
                else:
                    ok = mutations.rename_file(target.source_dir, entry, final_name)
                    renamed = entry if ok else None
            except Exception as error:  # noqa: BLE001
                return False, final_name, f"重命名失败：{error}"
            if not renamed:
                return False, final_name, "重命名失败"

        if target.source_dir == target.target_dir:
            # 同目录只改名，避免 115 对「移动到原目录」的异常处理。
            return True, str(getattr(renamed, "name", "") or final_name), ""

        try:
            if batch:
                moved = batch.move_files({target.file_id: renamed}, target.target_dir) or {}
                actual = moved.get(target.file_id)
            else:
                actual = mutations.move_file(renamed, target.target_dir, final_name)
        except Exception as error:  # noqa: BLE001
            return False, final_name, f"移动失败：{error}"
        if not actual:
            return False, final_name, "移动失败"
        actual_name = str(getattr(actual, "name", "") or final_name)
        if existing is not None:
            existing.add(actual_name)
        return True, actual_name, ""

    def _find_entry(self, target: TargetFile) -> Optional[Any]:
        """从扫描缓存取 CloudFile；扫描对象不可用时重新精确查一次。"""
        for plan in self._plans.values():
            for candidate in plan.package.videos:
                if candidate.entry.file_id == target.file_id:
                    return candidate.entry.cloud_file
                for subtitle in candidate.subtitles:
                    if subtitle.file_id == target.file_id:
                        return subtitle.cloud_file
        drive = getattr(self, "_cloud_drive", None)
        if not drive:
            return None
        try:
            query = drive.require(CloudDriveCapability.FILE_QUERY)
            return query.find_file(target.source_dir, target.source_name)
        except Exception:  # noqa: BLE001
            return None

    def _directory_names(self, path: str) -> Optional[set]:
        cached = self._target_name_cache.get(path, "__missing__")
        if cached != "__missing__":
            return cached
        drive = getattr(self, "_cloud_drive", None)
        if not drive:
            return None
        try:
            directories = drive.require(CloudDriveCapability.DIRECTORY_READ)
            lookup = directories.resolve_directory(path)
            if not getattr(lookup, "checked", False):
                self._target_name_cache[path] = None
                return None
            if getattr(lookup, "directory_id", None) is None:
                names: set = set()
            else:
                listing = directories.list_directory(str(lookup.directory_id))
                if not getattr(listing, "checked", False):
                    self._target_name_cache[path] = None
                    return None
                names = {
                    str(getattr(item, "name", "") or "")
                    for item in getattr(listing, "files", ()) or ()
                }
            self._target_name_cache[path] = names
            return names
        except Exception as error:  # noqa: BLE001
            logger.debug(f"整理台读取目标目录失败：{path}：{error}")
            self._target_name_cache[path] = None
            return None

    def _generate_strm_for_target(self, target: TargetFile, actual_name: str) -> None:
        if target.is_subtitle:
            return
        handler = getattr(self, "_sync_handler", None)
        if not handler:
            return
        try:
            handler._generate_strm(  # noqa: SLF001 - 复用同步服务的 STRM 生成逻辑
                target.target_dir,
                actual_name,
                target_file=None,
                lookup_target=True,
                log_success=False,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(f"整理台生成 STRM 失败：{target.target_dir}/{actual_name}：{error}")

    # ------------------------------------------------------------------
    # 配置、历史与通知
    # ------------------------------------------------------------------
    def _settings_snapshot(self) -> Dict[str, Any]:
        return {
            "source_path": str(getattr(self, "_organize_source_path", "") or ""),
            "max_files": self._normalize_max_files(getattr(self, "_organize_max_files", 500)),
            "max_depth": max(1, min(int(getattr(self, "_organize_max_depth", 8) or 8), 20)),
            "include_subtitles": bool(getattr(self, "_organize_include_subtitles", True)),
            "conflict_policy": str(getattr(self, "_organize_conflict_policy", "skip") or "skip"),
            "schedule_enabled": bool(getattr(self, "_organize_schedule_enabled", False)),
            "schedule_cron": str(getattr(self, "_organize_schedule_cron", "0 4 * * *") or "0 4 * * *"),
            "schedule_auto_execute": bool(getattr(self, "_organize_schedule_auto_execute", True)),
        }

    @staticmethod
    def _normalize_max_files(value: Any) -> int:
        try:
            number = int(value or 500)
        except (TypeError, ValueError):
            number = 500
        return max(1, min(number, 5000))

    def _finish_history(
            self, trigger: str, started: float, done: int = 0, failed: int = 0,
            skipped: int = 0, cancelled: bool = False, note: str = "",
    ) -> None:
        with self._lock:
            if not self._state:
                return
            record = {
                "scan_id": self._state.get("scan_id"),
                "trigger": trigger,
                "source_path": self._state.get("source_path"),
                "started_at": started,
                "finished_at": time.time(),
                "groups": self._state.get("stats", {}).get("groups", 0),
                "ready": self._state.get("stats", {}).get("ready", 0),
                "done": done,
                "failed": failed,
                "skipped": skipped,
                "cancelled": cancelled,
                "note": note,
            }
            self._history.append(record)
            self._history = self._history[-self._MAX_HISTORY:]
            notify = bool(getattr(self, "_organize_schedule_notify", True))
            trigger_is_schedule = "定时" in str(trigger)
        if trigger_is_schedule and notify and not cancelled:
            try:
                self.post_message(
                    title="115 网盘整理完成",
                    text=(
                        f"源目录：{record['source_path']}\n"
                        f"分组：{record['groups']}，可执行：{record['ready']}\n"
                        f"成功 {done}，跳过 {skipped}，失败 {failed}"
                    ),
                )
            except Exception as error:  # noqa: BLE001
                logger.debug(f"整理台通知发送失败：{error}")
