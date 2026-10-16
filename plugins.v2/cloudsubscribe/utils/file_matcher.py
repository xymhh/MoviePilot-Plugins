"""按媒体结构匹配实际文件候选。"""

import re
from typing import Any, Dict, List

from app.core.metainfo import MetaInfo
from app.log import logger

from .file_parser import MediaFileParser
from ..search.matching import normalize_title, title_matches


class FileMatcher:
    """电影或剧集候选"""

    _TITLE_NOISE_RE = re.compile(r"[丨｜|¦]+")
    _COMPACT_YEAR_RE = re.compile(r"(?<!\s)(\((?:19|20)\d{2}\))")
    _TITLELESS_SEASON_EPISODE = re.compile(
        r"^[Ss]0*(\d{1,3})[ ._~-]*[Ee](?:[Pp])?0*(\d{1,4})(?=[ ._~-]|$)",
        re.IGNORECASE,
    )
    _TITLELESS_EPISODE = re.compile(
        r"^(?:第\s*)?(?:[Ee](?:[Pp])?)?0*(\d{1,4})(?:\s*集)?(?=[ ._~-]|$)",
        re.IGNORECASE,
    )
    # 跨季路径守卫：只看文件所在的分享目录/相对路径（文件名自身的季号仍走
    # contains_other_season 原逻辑）。开关由插件配置 cross_season_path_guard 写入。
    path_season_guard_enabled: bool = True
    _PATH_SEASON_DIR_FIELDS = ("_relative_path", "relative_path")
    _PATH_SEASON_DIR_AS_IS_FIELDS = ("parent_path", "cloud_path")

    @staticmethod
    def _matches_target_media(meta: Any, mediainfo: Any, season: int | None = None) -> bool:
        if mediainfo is None:
            return True
        expected_titles = [
            getattr(mediainfo, key, None)
            for key in ("title", "en_title", "original_title", "original_name")
        ]
        names = getattr(mediainfo, "names", None)
        if names is None and isinstance(mediainfo, dict):
            names = mediainfo.get("names")
        if isinstance(names, (list, tuple, set)):
            expected_titles.extend(names)
        expected_titles = [t for t in expected_titles if t]

        candidate_titles = [
            getattr(meta, key, None)
            for key in ("cn_name", "en_name", "original_name")
        ]
        if not any(
                title_matches(candidate, expected_titles)
                for candidate in candidate_titles
                if candidate
        ):
            return False

        expected_years = {
            str(value).strip()
            for value in (
                getattr(mediainfo, "year", None),
                (
                    (getattr(mediainfo, "season_years", {}) or {}).get(season)
                    if season is not None else None
                ),
            )
            if str(value or "").strip()
        }
        candidate_year = str(getattr(meta, "year", None) or "").strip()
        if candidate_year and expected_years and candidate_year not in expected_years:
            return False
        candidate_type = getattr(meta, "type", None)
        expected_type = getattr(mediainfo, "type", None)
        if candidate_type and expected_type:
            c_val = str(getattr(candidate_type, "value", candidate_type) or "").strip().lower()
            e_val = str(getattr(expected_type, "value", expected_type) or "").strip().lower()
            type_map = {
                "tv": "tv", "电视剧": "tv", "series": "tv", "teleplay": "tv",
                "movie": "movie", "电影": "movie",
            }
            norm_c = type_map.get(c_val, c_val)
            norm_e = type_map.get(e_val, e_val)
            if (
                    norm_c and norm_e
                    and norm_c not in {"未知", "unknown"}
                    and norm_e not in {"未知", "unknown"}
                    and norm_c != norm_e
            ):
                return False
        return True

    @staticmethod
    def _platform_meta(name: str) -> Any:
        try:
            # 部分分享用竖线类字符拆散中文片名，先去除这类装饰噪声再交给平台识别。
            normalized = FileMatcher._TITLE_NOISE_RE.sub("", name)
            normalized = FileMatcher._COMPACT_YEAR_RE.sub(r" \1", normalized)
            return MetaInfo(normalized)
        except Exception as error:
            logger.debug(f"平台媒体解析失败，跳过文件：{name} - {error}")
            return None

    @staticmethod
    def _has_explicit_media_title(meta: Any) -> bool:
        """数字、E01、S01E01 等只有季集标记的名称不算作品标题。"""
        for key in ("cn_name", "en_name", "original_name"):
            normalized = normalize_title(getattr(meta, key, None))
            if not normalized:
                continue
            residue = re.sub(r"(?i)(?:s\d{1,3})?(?:e|ep)?\d{1,4}", "", normalized)
            if residue:
                return True
        return False

    @classmethod
    def _titleless_episode_number(
            cls, name: str, meta: Any, season: int
    ) -> int | None:
        """仅为无作品名前缀的文件补全当前媒体和季号。"""
        if cls._has_explicit_media_title(meta):
            return None
        parsed_season = getattr(meta, "begin_season", None)
        parsed_episode = getattr(meta, "begin_episode", None)
        if parsed_episode is not None:
            try:
                if parsed_season is None or int(parsed_season) == int(season):
                    return int(parsed_episode)
            except (TypeError, ValueError):
                return None
        season_episode = cls._TITLELESS_SEASON_EPISODE.match(name)
        if season_episode:
            if int(season_episode.group(1)) == int(season):
                return int(season_episode.group(2))
            return None
        episode = cls._TITLELESS_EPISODE.match(name)
        return int(episode.group(1)) if episode else None

    @classmethod
    def _meta_with_target_defaults(
            cls, name: str, meta: Any, mediainfo: Any, season: int
    ) -> tuple[Any, int | None]:
        episode = cls._titleless_episode_number(name, meta, season)
        if episode is None or episode <= 0:
            return meta, episode
        if mediainfo is None:
            return cls._platform_meta(f"S{int(season):02d}E{episode:02d}"), episode
        title = str(
            getattr(mediainfo, "title", None)
            or getattr(mediainfo, "en_title", None)
            or ""
        ).strip()
        if not title:
            return meta, None
        season_years = getattr(mediainfo, "season_years", {}) or {}
        year = str(
            season_years.get(season) or getattr(mediainfo, "year", None) or ""
        ).strip()
        synthetic = f"{title} {year} S{int(season):02d}E{episode:02d}".strip()
        return cls._platform_meta(synthetic), episode

    @classmethod
    def media_name_matches(cls, name: str, mediainfo: Any) -> bool:
        """确认文本经识别后属于目标媒体。"""
        normalized = str(name or "").strip()
        if not normalized:
            return False
        meta = cls._platform_meta(normalized)
        return bool(meta and cls._matches_target_media(meta, mediainfo))

    @classmethod
    def media_matches(cls, item: Any, mediainfo: Any) -> bool:
        """确认分享文件经识别后属于目标媒体。"""
        name = str((item or {}).get("name") or "").strip()
        return bool(
            name
            and MediaFileParser.is_video(name)
            and cls.media_name_matches(name, mediainfo)
        )

    @classmethod
    def _path_season_conflict(cls, item: Any, season: Any) -> tuple[bool, str, int]:
        """文件所在分享目录含与目标季冲突的显式季号时返回 (True, 命中目录, 季号)。

        ``_relative_path``/``relative_path`` 去掉文件名后按目录处理；
        ``parent_path``/``cloud_path`` 直接当目录串。无季号信息或开关关闭时不变。
        """
        if not cls.path_season_guard_enabled or season is None:
            return False, "", 0
        try:
            target = int(season)
        except (TypeError, ValueError):
            return False, "", 0
        data = item if isinstance(item, dict) else {}
        dir_texts: List[str] = []
        for field in cls._PATH_SEASON_DIR_FIELDS:
            text = str(data.get(field) or "").strip()
            if not text:
                continue
            parts = [part for part in text.strip("/").split("/") if part]
            if len(parts) < 2:
                continue
            dir_text = "/".join(parts[:-1])
            if dir_text and dir_text not in dir_texts:
                dir_texts.append(dir_text)
        for field in cls._PATH_SEASON_DIR_AS_IS_FIELDS:
            text = str(data.get(field) or "").strip().strip("/")
            if text and text not in dir_texts:
                dir_texts.append(text)
        for dir_text in dir_texts:
            if MediaFileParser.path_conflicts_season(dir_text, target):
                found = MediaFileParser.path_season(dir_text)
                return True, dir_text, int(found) if found is not None else 0
        return False, "", 0

    @classmethod
    def _log_path_season_rejection(
            cls, conflict_path: str, season: Any, found_season: int,
    ) -> None:
        logger.debug(
            f"分享目录季号与目标季冲突，拒绝候选：{conflict_path}；"
            f"目标=S{int(season):02d}，识别到=S{found_season:02d}"
        )

    @classmethod
    def episode_from_file(
            cls, item: Any, season: int, mediainfo: Any = None
    ) -> int | None:
        """使用元数据解析文件，并确认它属于目标媒体。"""
        name = str((item or {}).get("name") or "").strip()
        if not name or not MediaFileParser.is_video(name):
            return None
        if MediaFileParser.contains_other_season(name, season):
            return None
        conflict, conflict_path, found_season = cls._path_season_conflict(item, season)
        if conflict:
            cls._log_path_season_rejection(conflict_path, season, found_season)
            return None
        meta = cls._platform_meta(name)
        if not meta:
            return None

        if not cls._has_explicit_media_title(meta):
            meta, default_episode = cls._meta_with_target_defaults(
                name, meta, mediainfo, season
            )
            if not meta:
                return None
        else:
            default_episode = None

        parsed_season = getattr(meta, "begin_season", None)
        episode = getattr(meta, "begin_episode", None) or default_episode
        if parsed_season is None or episode is None:
            return None
        try:
            parsed_season, episode = int(parsed_season), int(episode)
        except (TypeError, ValueError):
            return None
        if parsed_season != int(season) or episode <= 0:
            return None
        if not cls._matches_target_media(meta, mediainfo, season):
            return None
        return episode

    @staticmethod
    def episode_candidates(
            files: list,
            season: int,
            episodes: List[int],
            mediainfo: Any = None,
    ) -> Dict[int, List[Any]]:
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        if not episode_list:
            return {}

        target_episodes = set(episode_list)
        strict_matches = {episode: [] for episode in episode_list}
        loose_matches = {episode: [] for episode in episode_list}
        loosest_matches = {episode: [] for episode in episode_list}
        loose_patterns = {
            episode: (
                re.compile(rf"第\s*{episode}\s*集", re.IGNORECASE),
                re.compile(rf"[Ee][Pp]{episode}(?!\d)", re.IGNORECASE),
                re.compile(
                    rf"[\[\(\s\.\-_][Ee]0?{episode}[\]\)\s\.\-_]",
                    re.IGNORECASE,
                ),
            )
            for episode in episode_list
        }
        loosest_patterns = {
            episode: re.compile(
                rf"[\.\s\-_]0?{episode}[\.\s\-_]", re.IGNORECASE
            )
            for episode in episode_list
        }
        total_files = 0

        for item in MediaFileParser.iter_files(files):
            file_name = str(item.get("name") or "")
            total_files += 1
            if not MediaFileParser.is_video(file_name):
                continue
            conflict, conflict_path, found_season = FileMatcher._path_season_conflict(
                item, season
            )
            if conflict:
                # 目录季号冲突的文件不得进入 strict/loose/loosest 任何候选
                FileMatcher._log_path_season_rejection(
                    conflict_path, season, found_season
                )
                continue
            found_episode = FileMatcher.episode_from_file(item, season, mediainfo)
            if found_episode is not None:
                if found_episode in target_episodes:
                    strict_matches[found_episode].append(item)
                continue
            # 有目标媒体时，平台无法识别标题或季集的文件不能进入转存。
            if mediainfo is not None:
                continue
            matches_season = MediaFileParser.matches_target_season(file_name, season)
            has_season_marker = bool(MediaFileParser.ANY_SEASON_PATTERN.search(file_name))
            for episode in episode_list:
                if any(pattern.search(file_name) for pattern in loose_patterns[episode]):
                    if season == 1 or matches_season or not has_season_marker:
                        loose_matches[episode].append(item)
                    continue
                if matches_season and loosest_patterns[episode].search(file_name):
                    loosest_matches[episode].append(item)

        results = {}
        for episode in episode_list:
            results[episode] = (
                    strict_matches[episode]
                    or loose_matches[episode]
                    or loosest_matches[episode]
            )
            if not results[episode] and total_files:
                logger.debug(f"S{season:02d}E{episode:02d} 未匹配到实际媒体文件")
        return results

    @staticmethod
    def movie_candidates(
            files: list, min_size_mb: int = 500, mediainfo: Any = None
    ) -> List[Any]:
        min_size = max(0, int(min_size_mb or 0)) * 1024 * 1024
        candidates = [
            item
            for item in MediaFileParser.iter_files(files)
            if MediaFileParser.is_video(str(item.get("name") or ""))
               and int(item.get("size") or 0) >= min_size
               and (mediainfo is None or FileMatcher.media_matches(item, mediainfo))
        ]
        candidates.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        return candidates
