"""媒体文件命名、目标分类路径计算与资源扫描服务。"""

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.types import MediaType

from .utils import normalize_season

try:
    from app.helper.directory import DirectoryHelper
except Exception:
    try:
        from app.application.directory import DirectoryHelper
    except Exception:
        DirectoryHelper = None

try:
    from app.sdk.classification import classify_media
except Exception:
    classify_media = None

try:
    from app.modules.themoviedb.category import CategoryHelper
except Exception:
    CategoryHelper = None

from ...core import CloudFile, OwnerDelegator
from ...core.media import media_identity
from ...utils import FileMatcher, MediaFileParser
from .baseline import normalize_platform_cache_key
from .utils import format_episode_ranges


class SyncNamingService(OwnerDelegator):
    """负责媒体分类根目录、重命名路径生成以及本地和网盘资源目录扫描。"""

    def _platform_classified_root(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
    ) -> Optional[Path]:
        """缓存分类根目录，避免逐集重复执行相同目录规则。"""
        media_source, media_id = media_identity(mediainfo)
        key = (
            str(root_path),
            media_source,
            media_id,
            getattr(mediainfo, "tmdb_id", None),
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "year", None),
            getattr(mediainfo, "type", None),
            getattr(mediainfo, "category", None),
            getattr(subscribe, "id", None),
            getattr(subscribe, "media_category", None),
        )
        cache_key = normalize_platform_cache_key(key)
        platform_root_lock = getattr(self, "_platform_root_lock", None)
        platform_root_cache = getattr(self, "_platform_root_cache", None)

        if platform_root_lock and platform_root_cache and cache_key in platform_root_cache:
            return platform_root_cache.get(cache_key)

        category_name = self._resolve_classification_category(subscribe, mediainfo)
        if category_name:
            if not getattr(mediainfo, "category", None):
                mediainfo.category = category_name
            if not getattr(mediainfo, "library_category", None):
                mediainfo.library_category = category_name

        directory = DirectoryHelper().get_dir(media=mediainfo, include_unsorted=False) if DirectoryHelper else None
        resolved = None
        if directory:
            updates = {"library_path": root_path}
            if hasattr(directory, "model_copy"):
                target_directory = directory.model_copy(deep=True, update=updates)
            else:
                target_directory = directory.copy(deep=True, update=updates)
            classified_root = TransHandler().get_dest_dir(
                mediainfo=mediainfo,
                target_dir=target_directory,
            )
            if classified_root:
                resolved = Path(classified_root)
                # 关键守护：当目录启用了按类别建目录（library_category_folder），且媒体识别出二级分类时，
                # 若 TransHandler 返回的路径末级未包含该二级分类，自动追加该分类，防止直接堆积在一级目录
                if getattr(target_directory, "library_category_folder", True) and category_name:
                    resolved_posix = resolved.as_posix().rstrip("/")
                    cat_posix = category_name.strip("/")
                    if not resolved_posix.endswith(cat_posix):
                        resolved = resolved / cat_posix
        if resolved is None:
            media_type_value = getattr(
                getattr(mediainfo, "type", None), "value", None
            )
            if media_type_value in {
                MediaType.MOVIE.value,
                MediaType.TV.value,
            }:
                resolved = Path(str(root_path or "/")) / media_type_value
                if category_name:
                    resolved = resolved / category_name.strip("/")

        if platform_root_lock and platform_root_cache:
            with platform_root_lock:
                platform_root_cache.set(cache_key, resolved)
        return resolved

    def _platform_rename_path(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Optional[Path]:
        """使用当前分类目录和重命名模板生成完整目标路径。"""
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        classified_root = self._platform_classified_root(
            root_path, subscribe, effective_media
        )
        if not classified_root:
            return None
        custom_words_list = None
        if subscribe and getattr(subscribe, "custom_words", None):
            cw = subscribe.custom_words
            custom_words_list = [w for w in (cw.split("\n") if isinstance(cw, str) else list(cw)) if w.strip()]
        meta = MetaInfo(source_name, custom_words=custom_words_list)
        meta.type = effective_media.type
        meta.year = getattr(subscribe, "year", None) or effective_media.year
        if season is not None:
            meta.begin_season = season
        if episode is not None:
            if hasattr(meta, "set_episode"):
                meta.set_episode(episode)
            else:
                meta.begin_episode = episode
        relative_name = FileManagerModule.recommend_name(meta, effective_media)
        if not relative_name:
            clean_title = effective_media.title or getattr(subscribe, "name", "") or "Unknown"
            clean_year = getattr(subscribe, "year", None) or effective_media.year or ""
            year_part = f" ({clean_year})" if clean_year else ""
            source_suffix = Path(source_name).suffix or ".mp4"
            if effective_media.type == MediaType.TV:
                s_num = normalize_season(season)
                e_num = max(1, int(episode or 1))
                relative_name = f"{clean_title}{year_part}/Season {s_num}/{clean_title} - S{s_num:02d}E{e_num:02d}{source_suffix}"
            else:
                relative_name = f"{clean_title}{year_part}/{clean_title}{year_part}{source_suffix}"
        return classified_root / Path(relative_name)

    def _platform_target_file(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Optional[Path]:
        """生成平台目标文件路径。"""
        return self._platform_rename_path(
            root_path, subscribe, mediainfo, source_name, season, episode
        )

    def _platform_target(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Tuple[str, str]:
        """生成平台分类后的目标目录和规范文件名。"""
        full_path = self._platform_target_file(
            root_path, subscribe, mediainfo, source_name, season, episode
        )
        if not full_path:
            return "", ""
        parent_posix = full_path.parent.as_posix()
        if not parent_posix.startswith("/"):
            parent_posix = "/" + parent_posix.lstrip("/")
        return parent_posix, full_path.name

    @classmethod
    def _match_category_from_yaml(cls, mediainfo: MediaInfo) -> str:
        """从 /config/category.yaml 读取并匹配分类规则兜底。"""
        yaml_path = Path("/config/category.yaml")
        if not yaml_path.exists():
            return ""
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""

        media_type = getattr(mediainfo, "type", None)
        type_key = "tv" if media_type == MediaType.TV else ("movie" if media_type == MediaType.MOVIE else None)
        if not type_key or type_key not in data:
            return ""

        type_rules = data.get(type_key) or {}
        if not isinstance(type_rules, dict):
            return ""

        origin_countries = [str(c).strip().upper() for c in (getattr(mediainfo, "origin_country", None) or [])]
        prod_countries = [str(c).strip().upper() for c in (getattr(mediainfo, "production_countries", None) or [])]
        all_countries = set(origin_countries + prod_countries)

        original_language = str(getattr(mediainfo, "original_language", "") or "").strip().lower()

        genre_ids = {str(gid) for gid in (getattr(mediainfo, "genre_ids", None) or [])}
        for g in getattr(mediainfo, "genres", None) or []:
            if isinstance(g, dict) and g.get("id"):
                genre_ids.add(str(g["id"]))

        for cat_name, conditions in type_rules.items():
            if not conditions:
                return str(cat_name).strip()
            if not isinstance(conditions, dict):
                continue
            matched = True
            if "genre_ids" in conditions:
                expected_genres = {str(item).strip() for item in str(conditions["genre_ids"]).split(",") if
                                   item.strip()}
                if not (genre_ids & expected_genres):
                    matched = False
            country_cond = conditions.get("origin_country") or conditions.get("production_countries")
            if country_cond and matched:
                expected_countries = {str(item).strip().upper() for item in str(country_cond).split(",") if
                                      item.strip()}
                if not (all_countries & expected_countries):
                    matched = False
            if "original_language" in conditions and matched:
                expected_langs = {str(item).strip().lower() for item in str(conditions["original_language"]).split(",")
                                  if item.strip()}
                if original_language not in expected_langs:
                    matched = False
            if matched:
                return str(cat_name).strip()
        return ""

    @classmethod
    def _resolve_classification_category(
            cls,
            subscribe,
            mediainfo: MediaInfo,
    ) -> str:
        """多级解析当前媒体的二级分类：
        1. 订阅显式指定的分类；
        2. 媒体已有有效分类；
        3. 宿主 v3 统一分类服务 (classify_media)；
        4. 宿主门面 (CategoryHelper)；
        5. 本地 /config/category.yaml 规则兜底。
        """
        sub_cat = getattr(subscribe, "media_category", None) or getattr(subscribe, "media_category_id", None)
        if sub_cat and str(sub_cat).strip():
            return str(sub_cat).strip()

        invalid_categories = {"电影", "电视剧", "tv", "movie", "未知", "default", "none"}
        for attr in ("library_category", "category"):
            val = getattr(mediainfo, attr, None)
            if val and str(val).strip() and str(val).strip().lower() not in invalid_categories:
                return str(val).strip()

        if classify_media:
            try:
                classified = classify_media(mediainfo)
                for attr in ("library_category", "category"):
                    val = getattr(classified, attr, None)
                    if val and str(val).strip() and str(val).strip().lower() not in invalid_categories:
                        return str(val).strip()
            except Exception:
                pass

        if CategoryHelper:
            try:
                cat_helper = CategoryHelper()
                tmdb_info = getattr(mediainfo, "tmdb_info", None)
                if not tmdb_info and hasattr(mediainfo, "to_dict"):
                    try:
                        tmdb_info = mediainfo.to_dict()
                    except Exception:
                        tmdb_info = None
                if tmdb_info and isinstance(tmdb_info, dict):
                    media_type = getattr(mediainfo, "type", None)
                    cat = ""
                    if media_type == MediaType.TV:
                        cat = cat_helper.get_tv_category(tmdb_info)
                    elif media_type == MediaType.MOVIE:
                        cat = cat_helper.get_movie_category(tmdb_info)
                    if cat and str(cat).strip() and str(cat).strip().lower() not in invalid_categories:
                        return str(cat).strip()
            except Exception:
                pass

        cat_from_yaml = cls._match_category_from_yaml(mediainfo)
        if cat_from_yaml and cat_from_yaml.lower() not in invalid_categories:
            return cat_from_yaml

        return ""

    @classmethod
    def _effective_mediainfo(cls, subscribe, mediainfo: MediaInfo) -> MediaInfo:
        """使用订阅卡片的展示信息与平台分类生成整理专用媒体副本。"""
        effective_media = copy.deepcopy(mediainfo)
        subscribe_title = str(getattr(subscribe, "name", "") or "").strip()
        if subscribe_title:
            effective_media.title = subscribe_title
        subscribe_year = getattr(subscribe, "year", None)
        if subscribe_year:
            effective_media.year = subscribe_year

        cat = cls._resolve_classification_category(subscribe, effective_media)
        if cat:
            effective_media.category = cat
            effective_media.library_category = cat
            if classify_media and getattr(effective_media, "classification", None) is None:
                try:
                    c = classify_media(effective_media)
                    if getattr(c, "classification", None) is not None:
                        effective_media.classification = c.classification
                except Exception:
                    pass
        return effective_media

    def _resolve_resource_season_dir(
            self,
            resource_root: str,
            subscribe,
            mediainfo: MediaInfo,
            season: int
    ) -> Optional[Path]:
        """使用平台的目录分类和命名规则生成媒体季目录。"""
        if not resource_root or not mediainfo:
            return None

        media_type = getattr(getattr(mediainfo, "type", None), "value", None)
        media_source, media_id = media_identity(mediainfo)
        cache_key = (
            str(resource_root),
            media_type or str(getattr(mediainfo, "type", "") or ""),
            media_source,
            media_id,
            getattr(mediainfo, "tmdb_id", None),
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "year", None),
            getattr(mediainfo, "category", None),
            getattr(subscribe, "id", None),
            getattr(subscribe, "name", None),
            getattr(subscribe, "year", None),
            getattr(subscribe, "media_category", None),
            int(season or 0),
        )
        platform_cache_key = normalize_platform_cache_key(cache_key)
        season_lock = getattr(self, "_resource_season_dir_lock", None)
        season_cache = getattr(self, "_resource_season_dir_cache", None)

        if season_lock and season_cache:
            with season_lock:
                if platform_cache_key in season_cache:
                    return season_cache.get(platform_cache_key)

        resolved = None
        try:
            rename_path = self._platform_rename_path(
                root_path=resource_root,
                subscribe=subscribe,
                mediainfo=mediainfo,
                source_name=getattr(subscribe, "name", None) or mediainfo.title,
                season=season,
                episode=1,
            )
            if rename_path:
                resolved = rename_path.parent
        except Exception as error:
            logger.debug(f"资源路径解析失败：{mediainfo.title_year}，{error}")

        if season_lock and season_cache:
            with season_lock:
                season_cache.set(platform_cache_key, resolved)
        return resolved

    def _get_local_resource_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int
    ) -> List[Path]:
        """获取平台规则生成的季目录中的本地或挂载媒体文件。"""
        season_dir = self._resolve_resource_season_dir(
            getattr(self, "_local_resource_path", ""), subscribe, mediainfo, season
        )
        if not season_dir or not season_dir.is_dir():
            return []
        try:
            allowed_extensions = set(MediaFileParser.VIDEO_EXTENSIONS) | {".strm"}
            return [
                item for item in season_dir.iterdir()
                if item.is_file() and item.suffix.lower() in allowed_extensions
            ]
        except OSError as error:
            logger.debug(f"资源季目录读取失败 {season_dir}: {error}")
            return []

    def _scan_local_resource_episodes(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: Optional[int] = None,
            total_episode: Optional[int] = None
    ) -> Set[int]:
        """按元数据解析器识别已落盘或已挂载的剧集。"""
        resource_files = self._get_local_resource_files(subscribe, mediainfo, season)
        found_episodes = self._parse_resource_episode_names(
            (resource_file.name for resource_file in resource_files),
            season=season,
            start_episode=start_episode,
            total_episode=total_episode,
            subscribe=subscribe,
        )
        if found_episodes:
            logger.info(
                f"媒体路径检查：{getattr(subscribe, 'name', '?')} S{season:02d} "
                f"识别到 {len(found_episodes)} 集"
            )
        return found_episodes

    @staticmethod
    def _parse_resource_episode_names(
            file_names,
            season: int,
            start_episode: Optional[int] = None,
            total_episode: Optional[int] = None,
            subscribe: Any = None,
    ) -> Set[int]:
        """使用元数据解析器从文件名提取目标季集数，支持订阅卡片的自定义词与偏移规则。"""
        found_episodes = set()
        custom_words_list = None
        if subscribe and getattr(subscribe, "custom_words", None):
            cw = subscribe.custom_words
            custom_words_list = [w for w in (cw.split("\n") if isinstance(cw, str) else list(cw)) if w.strip()]

        for file_name in file_names:
            file_meta = MetaInfo(Path(str(file_name)).stem, custom_words=custom_words_list)
            file_season = file_meta.begin_season or season
            if file_season != season:
                continue
            episodes = list(getattr(file_meta, "episode_list", None) or [])
            if not episodes and file_meta.begin_episode:
                episodes = [file_meta.begin_episode]
            for episode in episodes:
                if start_episode is not None and episode < start_episode:
                    continue
                if total_episode and episode > total_episode:
                    continue
                found_episodes.add(int(episode))
        return found_episodes

    def _scan_cloud_resource_episode_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: int,
            total_episode: int,
    ) -> Tuple[bool, Dict[int, CloudFile], str]:
        """一次读取目标季目录，返回真实存在的逐集网盘文件。"""
        cloud_media_root = getattr(self, "_CLOUD_MEDIA_ROOT", "/")
        cloud_dir = self._resolve_resource_season_dir(
            cloud_media_root, subscribe, mediainfo, season
        )
        if not cloud_dir:
            return False, {}, ""
        cloud_path = cloud_dir.as_posix()
        cloud_directories = getattr(self, "_cloud_directories", None)
        if not cloud_directories:
            return False, {}, cloud_path

        lookup = cloud_directories.resolve_directory(cloud_path)
        if not lookup.checked:
            return False, {}, cloud_path
        if lookup.directory_id is None:
            return True, {}, cloud_path

        listing = cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return False, {}, cloud_path
        episode_files: Dict[int, CloudFile] = {}
        upgrade_mode = getattr(self, "_upgrade_mode", "largest")
        for item in listing.files:
            if item.is_directory:
                continue
            name = item.name
            if not MediaFileParser.is_video(name):
                continue
            episodes = self._parse_resource_episode_names(
                [name], season, start_episode, total_episode, subscribe=subscribe
            )
            for episode in episodes:
                current = episode_files.get(episode)
                if not current:
                    episode_files[episode] = item
                    continue
                current_size = int(getattr(current, "size", 0) or 0)
                candidate_size = int(getattr(item, "size", 0) or 0)
                prefer_candidate = (
                    candidate_size < current_size
                    if upgrade_mode == "smallest"
                    else candidate_size > current_size
                )
                if prefer_candidate:
                    episode_files[episode] = item
        return True, episode_files, cloud_path

    def _cloud_drive_name(self) -> str:
        """当前目标网盘名称，未获取到时回退为通用称呼。"""
        return str(getattr(getattr(self, "_cloud_drive", None), "name", "") or "网盘")

    def _scan_cloud_resource_episodes(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: int,
            total_episode: int,
    ) -> Tuple[bool, Set[int], str]:
        """扫描平台规则生成的网盘季目录；目录不存在时不创建。"""
        valid, episode_files, cloud_path = self._scan_cloud_resource_episode_files(
            subscribe=subscribe,
            mediainfo=mediainfo,
            season=season,
            start_episode=start_episode,
            total_episode=total_episode,
        )
        drive_name = self._cloud_drive_name()
        label = f"{drive_name}媒体路径 {cloud_path}" if cloud_path else f"{drive_name}媒体路径"
        return valid, set(episode_files), label

    def _find_cloud_movie_file(
            self,
            subscribe,
            mediainfo: MediaInfo,
    ) -> Optional[Tuple[str, str, CloudFile]]:
        """检查平台规则生成的网盘电影目录，不递归扫描其他路径。"""
        cloud_media_root = getattr(self, "_CLOUD_MEDIA_ROOT", "/")
        try:
            cloud_dir, expected_name = self._platform_target(
                cloud_media_root,
                subscribe,
                mediainfo,
                f"{getattr(subscribe, 'name', None) or mediainfo.title}.mkv",
            )
        except Exception as error:
            logger.debug(
                f"{self._cloud_drive_name()}电影目标路径计算失败：{mediainfo.title_year}，{error}"
            )
            return None
        cloud_directories = getattr(self, "_cloud_directories", None)
        if not cloud_directories:
            return None
        lookup = cloud_directories.resolve_directory(cloud_dir)
        if not lookup.checked or lookup.directory_id is None:
            return None
        expected_stem = Path(expected_name).stem
        listing = cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return None
        for item in listing.files:
            if item.is_directory:
                continue
            name = item.name
            path = Path(name)
            if not MediaFileParser.is_video(name):
                continue
            if path.stem == expected_stem:
                return cloud_dir, name, item
        return None

    @staticmethod
    def _summarize_share_episodes(
            files: List[dict], season: int, mediainfo: Optional[MediaInfo] = None
    ) -> Tuple[int, Set[int]]:
        """递归统计分享中的实际视频数量和目标季集数。"""
        video_count = 0
        episodes = set()

        def walk(items: List[dict]):
            nonlocal video_count
            for item in items or []:
                if item.get("is_dir"):
                    walk(item.get("children") or [])
                    continue
                name = str(item.get("name") or "")
                if not MediaFileParser.is_video(name):
                    continue
                video_count += 1
                episode = FileMatcher.episode_from_file(item, season, mediainfo)
                if episode is not None:
                    episodes.add(episode)

        walk(files)
        return video_count, episodes

    @staticmethod
    def _format_episode_ranges(episodes: Set[int]) -> str:
        """把集数集合压缩为 E01-E03、E05 形式，优先使用平台工具。"""
        return format_episode_ranges(episodes)
