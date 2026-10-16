"""按季集判重替换（1.6.48）。

判重键 =（剧集身份 tmdbid/订阅名/年份）+ 季 + 集，不再以目标文件名为键。
数据源两路取并集：目标 Season 目录扫描（MetaInfo 口径）+ 插件既有基线。
新 ≥ 旧 → 删旧写新；新 < 旧 → 跳过并记 skipped；同分沿用 upgrade_mode。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.schemas import MediaInfo

from ...core import OwnerDelegator
from ...utils.cache import normalize_platform_cache_key
from ...utils.file_parser import MediaFileParser


class EpisodeDedupService(OwnerDelegator):
    """转存前按「季集身份」判重，支持删旧写新或跳过。"""

    def _episode_dedup_active(self) -> bool:
        """开关关闭或共存模式时判重门整体放行。"""
        if not bool(getattr(self, "_episode_dedup_enabled", True)):
            return False
        return str(getattr(self, "_upgrade_mode", "largest") or "largest") != "coexist"

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    def _season_dir_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
    ) -> Tuple[bool, str, List[Dict[str, Any]]]:
        """列出目标季目录里的正片文件，返回 ``(是否拿到实测数据, 目录路径, 文件条目)``。

        判重与「历史记录存在性校验」共用这一份列目录结果，避免同一季重复请求网盘。
        ``verified=False`` 表示目录解析/列目录失败（网络或鉴权问题），此时调用方
        必须按「没有实测数据」处理，不能据此判定某集不存在。
        """
        key = None
        cache = getattr(self, "_season_listing_cache", None)
        cache_key_builder = getattr(self, "_baseline_key", None)
        if cache is not None and cache_key_builder is not None:
            try:
                key = normalize_platform_cache_key(
                    (cache_key_builder(subscribe, season), int(season))
                )
            except Exception:
                key = None
            if key is not None:
                cached = cache.get(key)
                if cached is not None:
                    return bool(cached[0]), str(cached[1]), [
                        dict(item) for item in (cached[2] or [])
                    ]

        cloud_media_root = getattr(self, "_CLOUD_MEDIA_ROOT", "/")
        try:
            cloud_dir = self._resolve_resource_season_dir(
                cloud_media_root, subscribe, mediainfo, season
            )
        except Exception as error:
            logger.debug(f"判重季目录解析失败：{mediainfo.title_year}，{error}")
            return False, "", []
        if not cloud_dir:
            return False, "", []
        cloud_directories = getattr(self, "_cloud_directories", None)
        if not cloud_directories:
            return False, "", []
        lookup = cloud_directories.resolve_directory(cloud_dir.as_posix())
        if not lookup.checked:
            return False, "", []
        if lookup.directory_id is None:
            # 目录确实不存在（而不是请求失败）：该季一盘皆无，按实测空目录处理
            return True, cloud_dir.as_posix(), []
        listing = cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return False, "", []

        cloud_path = cloud_dir.as_posix()
        files: List[Dict[str, Any]] = []
        for item in listing.files:
            if getattr(item, "is_directory", False):
                continue
            name = str(getattr(item, "name", "") or "")
            if not MediaFileParser.is_video(name) or self._is_extra_clip(name):
                continue
            files.append({
                "file_id": str(getattr(item, "id", "") or ""),
                "file_name": name,
                "cloud_dir": cloud_path,
                "file_size": int(getattr(item, "size", 0) or 0),
            })
        if cache is not None and key is not None:
            cache.set(key, (True, cloud_path, [dict(item) for item in files]))
        return True, cloud_path, files

    def _scan_episode_versions_in_dir(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """扫描目标季目录，用 MetaInfo 解析每条视频的季集，返回全部版本。"""
        verified, cloud_path, files = self._season_dir_files(
            subscribe, mediainfo, season
        )
        if not verified:
            return {}

        result: Dict[int, List[Dict[str, Any]]] = {}
        custom_words = None
        raw_words = getattr(subscribe, "custom_words", None)
        if raw_words:
            custom_words = [
                w for w in (
                    raw_words.split("\n") if isinstance(raw_words, str) else list(raw_words)
                ) if w.strip()
            ]

        for item in files:
            name = str(item.get("file_name") or "")
            file_meta = self._dedup_meta_info(Path(name).stem, custom_words)
            file_season = getattr(file_meta, "begin_season", None) or season
            if file_season != season:
                continue
            episodes = list(getattr(file_meta, "episode_list", None) or [])
            if not episodes and getattr(file_meta, "begin_episode", None):
                episodes = [file_meta.begin_episode]
            size = int(item.get("file_size") or 0)
            file_id = str(item.get("file_id") or "")
            # 目录扫描出的旧版本同样要按规则组评分，否则会被当成 0 分而被无条件替换
            score = self._get_mp_rule_score(name, size, subscribe, season, mediainfo)
            for episode in episodes:
                result.setdefault(int(episode), []).append({
                    "file_id": file_id,
                    "file_name": name,
                    "cloud_dir": cloud_path,
                    "file_size": size,
                    "score": score,
                    "source": "目标目录扫描",
                })
        return result

    @staticmethod
    def _is_extra_clip(name: str) -> bool:
        """排除预告/花絮/样片这类同目录附属视频，避免误判为已有版本。"""
        stem = Path(str(name or "")).stem.lower()
        return any(
            word in stem
            for word in ("sample", "trailer", "preview", "花絮", "预告", "样片")
        )

    @staticmethod
    def _dedup_meta_info(stem: str, custom_words: Optional[List[str]]):
        """延迟导入 MetaInfo，便于单测打桩。"""
        from app.core.metainfo import MetaInfo
        return MetaInfo(stem, custom_words=custom_words)

    def _baseline_episode_versions(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """从插件基线取逐集版本（含 file_id / cloud_dir，覆盖文件名无季集的情况）。"""
        try:
            baseline = self._build_episode_baseline(
                subscribe, mediainfo, season, include_saved=False
            )
        except Exception as error:
            logger.debug(f"判重基线构建失败：{mediainfo.title_year}，{error}")
            return {}
        result: Dict[int, List[Dict[str, Any]]] = {}
        for episode, item in (baseline or {}).items():
            try:
                episode_number = int(episode)
            except (TypeError, ValueError):
                continue
            if episode_number <= 0:
                continue
            file_name = str(
                item.get("target_file_name") or item.get("file_name") or ""
            ).strip()
            cloud_dir = str(item.get("cloud_dir") or "").strip()
            file_id = str(item.get("cloud_file_id") or "").strip()
            if not file_name and not file_id:
                continue
            result.setdefault(episode_number, []).append({
                "file_id": file_id,
                "file_name": file_name,
                "target_file_name": str(item.get("target_file_name") or "").strip(),
                "cloud_dir": cloud_dir,
                "file_size": int(item.get("file_size") or 0),
                "score": int(item.get("score") or item.get("rule_score") or 0),
                "source": "插件基线",
                # 保留上游来源（MoviePilot整理记录 / 插件转存记录 / 媒体服务器 …），
                # 判重时据此决定要不要做「目标目录有实物」校验
                "origin": str(item.get("source") or "").strip(),
            })
        return result

    # 判重只信「有实物」的来源：整理记录与转存记录都可能是假成功
    _HISTORY_BASELINE_SOURCES = frozenset({"MoviePilot整理记录", "插件转存记录"})

    def _baseline_entry_backed_by_storage(
            self,
            episode: int,
            entry: Dict[str, Any],
            scanned: Dict[int, List[Dict[str, Any]]],
            verified: bool,
            season_dir: str,
            present_names: set,
            present_ids: set,
    ) -> bool:
        """历史来源的旧版本必须在云盘上找得到实物，否则按缺失处理。

        1.6.50 之前的口径：只要历史里有该集的「成功」记录就当作已有版本。跨盘转存
        偶发「记录成功但文件没落进季目录」（cloud_dir 为空、MP 整理历史里只有源文件名），
        这种幻影版本会让该集被永久跳过，缺失集再也补不上。
        """
        if not verified:
            # 拿不到实测数据（目录解析失败/列目录失败）→ 保持旧行为，避免误判成缺失
            return True
        if str(entry.get("origin") or "") not in self._HISTORY_BASELINE_SOURCES:
            # 媒体服务器、手动基线这类来源不在本轮实测范围内，维持原判重口径
            return True
        if int(episode) in scanned:
            return True
        entry_dir = str(entry.get("cloud_dir") or "").strip().rstrip("/")
        season_dir = str(season_dir or "").strip().rstrip("/")
        if entry_dir and season_dir and entry_dir != season_dir:
            # 记录落在别的目录（媒体库/其它路径），本轮没扫，不越权判定
            return True
        for name_field in ("target_file_name", "file_name"):
            name = str(entry.get(name_field) or "").strip()
            if name and name in present_names:
                return True
        file_id = str(entry.get("file_id") or "").strip()
        if file_id and file_id in present_ids:
            return True
        return False

    def _collect_episode_versions(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """两路数据源取并集，按 file_id（无则 目录+文件名）去重。

        基线里的历史条目先过一遍「目标目录有没有实物」的校验：查无实据的按缺失处理
        （自动修复缺失集），只留日志说明，绝不静默丢弃。
        """
        merged: Dict[int, List[Dict[str, Any]]] = {}
        seen: Dict[int, set] = {}

        def _add(episode: int, entry: Dict[str, Any]) -> None:
            key = entry.get("file_id") or (
                f"{entry.get('cloud_dir', '')}\0{entry.get('file_name', '')}"
            )
            if not key:
                return
            bucket = seen.setdefault(episode, set())
            if key in bucket:
                return
            bucket.add(key)
            merged.setdefault(episode, []).append(entry)

        scanned = self._scan_episode_versions_in_dir(subscribe, mediainfo, season)
        for episode, entries in scanned.items():
            for entry in entries:
                _add(int(episode), entry)

        verified, season_dir, season_files = self._season_dir_files(
            subscribe, mediainfo, season
        )
        present_names = {
            str(item.get("file_name") or "") for item in season_files
        }
        present_ids = {
            str(item.get("file_id") or "") for item in season_files if item.get("file_id")
        }

        for episode, entries in self._baseline_episode_versions(
                subscribe, mediainfo, season
        ).items():
            for entry in entries:
                if not self._baseline_entry_backed_by_storage(
                        int(episode), entry, scanned, verified, season_dir,
                        present_names, present_ids,
                ):
                    logger.info(
                        f"判重忽略历史记录：{mediainfo.title_year} "
                        f"S{int(season):02d}E{int(episode):02d} 历史显示已存在，"
                        f"但目标目录里找不到该文件，按缺失处理"
                        f"（来源={entry.get('origin') or '未知'}，"
                        f"文件={entry.get('target_file_name') or entry.get('file_name') or '未记录'}）"
                    )
                    continue
                _add(int(episode), entry)
        return merged

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------
    def _episode_dedup_decision(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            episode: int,
            new_score: int,
            new_size: int,
            existing_versions: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        """返回 (action, old_versions, reason)。

        action: proceed=无现有版本可写；replace=删旧写新；skip=已有更优版本。
        """
        if not self._episode_dedup_active():
            return "proceed", [], "判重开关关闭或共存模式"
        versions = (existing_versions or {}).get(int(episode)) or []
        if not versions:
            return "proceed", [], "该集无现有版本"
        new_score = int(new_score or 0)
        new_size = max(0, int(new_size or 0))

        template_version = self._template_conformant_version(
            versions, subscribe, mediainfo, season, episode
        )
        if template_version:
            template_name = str(template_version.get("file_name") or "").strip()
            return (
                "skip",
                list(versions),
                f"现有版本已符合当前重命名模板，保留 {template_name}",
            )

        best_old_score = max(int(v.get("score") or 0) for v in versions)
        best_old_size = max(int(v.get("file_size") or 0) for v in versions)
        should_upgrade, reason = self._should_upgrade_candidate(
            best_old_score,
            new_score,
            best_old_size,
            new_size,
            has_existing=True,
        )
        if should_upgrade:
            return "replace", list(versions), reason
        return "skip", list(versions), reason

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def _apply_episode_dedup(
            self,
            old_versions: List[Dict[str, Any]],
            new_score: int = 0,
            new_size: int = 0,
            subscribe=None,
            mediainfo: Optional[MediaInfo] = None,
            season: int = 0,
            episode: int = 0,
    ) -> int:
        """删除旧版本并写日志；定位不到 file_id 时记 warning 不阻塞。返回删除数。"""
        deleted = 0
        cloud_mutations = getattr(self, "_cloud_mutations", None)
        title = str(
            getattr(mediainfo, "title_year", "") or getattr(subscribe, "name", "") or ""
        ).strip()
        if int(season or 0) > 0 and int(episode or 0) > 0:
            media_label = f"{title} S{int(season):02d}E{int(episode):02d}".strip()
        else:
            media_label = title or "电影"
        new_score_text = int(new_score or 0)
        new_size_text = max(0, int(new_size or 0))
        for version in old_versions or []:
            file_id = str(version.get("file_id") or "").strip()
            file_name = str(version.get("file_name") or "").strip()
            cloud_dir = str(version.get("cloud_dir") or "").strip()
            old_score = int(version.get("score") or 0)
            old_size = int(version.get("file_size") or 0)
            display = f"{cloud_dir.rstrip('/')}/{file_name}" if cloud_dir else file_name
            if not file_id:
                logger.warning(
                    f"episode_dedup_miss：{media_label} 定位不到旧文件（无 file_id），"
                    f"跳过删除继续写入：{display or '(未知路径)'}"
                    f"，旧评分 {old_score}，新评分 {new_score_text}"
                )
                continue
            if not cloud_mutations:
                logger.warning(
                    f"episode_dedup_miss：{media_label} 无删除能力，跳过删除：{display}"
                )
                continue
            try:
                ok = cloud_mutations.delete_file(file_id)
            except Exception as error:
                logger.warning(
                    f"episode_dedup_miss：{media_label} 删除旧文件异常：{display}，{error}"
                )
                continue
            if ok:
                deleted += 1
                logger.info(
                    f"按季集判重替换：{media_label} 已删除旧文件 {display}"
                    f"（旧评分 {old_score}/{old_size}B → 新评分 {new_score_text}/{new_size_text}B）"
                )
            else:
                logger.warning(
                    f"episode_dedup_miss：{media_label} 删除旧文件失败：{display}，file_id={file_id}"
                )
        return deleted

    # ------------------------------------------------------------------
    # 电影侧
    # ------------------------------------------------------------------
    def _find_cloud_movie_versions(
            self,
            subscribe,
            mediainfo: MediaInfo,
    ) -> List[Dict[str, Any]]:
        """列出 tmdbid 电影目录内全部视频文件（不再要求文件名与模板一致）。"""
        cloud_media_root = getattr(self, "_CLOUD_MEDIA_ROOT", "/")
        try:
            cloud_dir, _expected_name = self._platform_target(
                cloud_media_root,
                subscribe,
                mediainfo,
                f"{getattr(subscribe, 'name', None) or mediainfo.title}.mkv",
            )
        except Exception as error:
            logger.debug(
                f"{self._cloud_drive_name()}电影目标路径计算失败：{mediainfo.title_year}，{error}"
            )
            return []
        cloud_directories = getattr(self, "_cloud_directories", None)
        if not cloud_directories or not cloud_dir:
            return []
        lookup = cloud_directories.resolve_directory(cloud_dir)
        if not lookup.checked or lookup.directory_id is None:
            return []
        listing = cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return []
        versions: List[Dict[str, Any]] = []
        for item in listing.files:
            if item.is_directory:
                continue
            if not MediaFileParser.is_video(item.name) or self._is_extra_clip(item.name):
                continue
            versions.append({
                "file_id": str(getattr(item, "id", "") or ""),
                "file_name": item.name,
                "cloud_dir": cloud_dir,
                "file_size": int(getattr(item, "size", 0) or 0),
            })
        return versions

    def _movie_dedup_decision(
            self,
            subscribe,
            mediainfo: MediaInfo,
            new_score: int,
            new_size: int,
            existing_versions: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        """电影判重：目录内任一视频即视为已有版本，再走同一套决策。"""
        if not self._episode_dedup_active():
            return "proceed", [], "判重开关关闭或共存模式"
        versions = list(existing_versions or [])
        if not versions:
            return "proceed", [], "电影目录无现有版本"
        new_score = int(new_score or 0)
        new_size = max(0, int(new_size or 0))
        for version in versions:
            if "score" not in version:
                version["score"] = self._get_mp_rule_score(
                    str(version.get("file_name") or ""),
                    int(version.get("file_size") or 0),
                    subscribe,
                    0,
                    mediainfo,
                )
        template_version = self._template_conformant_version(
            versions, subscribe, mediainfo
        )
        if template_version:
            template_name = str(template_version.get("file_name") or "").strip()
            return (
                "skip",
                list(versions),
                f"现有版本已符合当前重命名模板，保留 {template_name}",
            )
        best_old_score = max(int(v.get("score") or 0) for v in versions)
        best_old_size = max(int(v.get("file_size") or 0) for v in versions)
        should_upgrade, reason = self._should_upgrade_candidate(
            best_old_score,
            new_score,
            best_old_size,
            new_size,
            has_existing=True,
        )
        if should_upgrade:
            return "replace", versions, reason
        return "skip", versions, reason
