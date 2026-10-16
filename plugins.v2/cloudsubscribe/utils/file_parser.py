"""媒体文件树展开与季集标记解析。"""

import re
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple


class MediaFileParser:
    """文件结构和命名解析"""

    DEFAULT_VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".iso"})
    DEFAULT_SUBTITLE_EXTENSIONS = frozenset({
        ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks",
    })
    VIDEO_EXTENSIONS = set(DEFAULT_VIDEO_EXTENSIONS)
    SUBTITLE_EXTENSIONS = set(DEFAULT_SUBTITLE_EXTENSIONS)

    _SXEX_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
    _SEASON_EPISODE_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee]")
    _CN_SEASON_PATTERN = re.compile(r"第\s*(\d{1,2})\s*季")
    _EN_SEASON_PATTERN = re.compile(r"[Ss]eason\s*(\d{1,2})", re.IGNORECASE)
    # 裸季号标记（如目录名「21806-极恶老大 S2」/「Show S02」）：无 E/季/season 伴随，
    # 上面三个既有模式都匹配不到，路径季号守卫需单独补这一类。
    _BARE_SEASON_PATTERN = re.compile(
        r"(?:^|[^0-9A-Za-z])[Ss]0?(\d{1,2})(?![0-9A-Za-z])"
    )
    ANY_SEASON_PATTERN = re.compile(
        r"[Ss]\d+[Ee]|第\s*\d+\s*季|[Ss]eason\s*\d+", re.IGNORECASE
    )

    @classmethod
    def configure_extensions(
            cls,
            video_extensions: Optional[Any] = None,
            subtitle_extensions: Optional[Any] = None,
    ) -> Tuple[set, set]:
        """动态配置视频与字幕扩展名，自动标准化带点小写格式。"""
        if video_extensions is not None:
            cls.VIDEO_EXTENSIONS = cls._normalize_extensions(
                video_extensions, cls.DEFAULT_VIDEO_EXTENSIONS
            )
        if subtitle_extensions is not None:
            cls.SUBTITLE_EXTENSIONS = cls._normalize_extensions(
                subtitle_extensions, cls.DEFAULT_SUBTITLE_EXTENSIONS
            )
        return set(cls.VIDEO_EXTENSIONS), set(cls.SUBTITLE_EXTENSIONS)

    @staticmethod
    def _normalize_extensions(raw: Any, default_set: Any) -> set:
        if isinstance(raw, str):
            parts = re.split(r"[,，\n\s]+", raw)
        elif isinstance(raw, (list, set, tuple)):
            parts = list(raw)
        else:
            return set(default_set)
        result = set()
        for item in parts:
            text = str(item or "").strip().lower()
            if not text:
                continue
            if not text.startswith("."):
                text = f".{text}"
            result.add(text)
        return result or set(default_set)

    @classmethod
    def is_video(cls, file_name: str) -> bool:
        ext = Path(str(file_name or "")).suffix.lower()
        if not ext:
            return False
        return ext in cls.VIDEO_EXTENSIONS or ext.lstrip(".") in cls.VIDEO_EXTENSIONS

    @classmethod
    def is_subtitle(cls, file_name: str) -> bool:
        ext = Path(str(file_name or "")).suffix.lower()
        if not ext:
            return False
        return ext in cls.SUBTITLE_EXTENSIONS or ext.lstrip(".") in cls.SUBTITLE_EXTENSIONS

    @classmethod
    def contains_other_season(cls, file_name: str, target_season: int) -> bool:
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
        ):
            match = pattern.search(file_name)
            if match:
                return int(match.group(1)) != target_season
        return False

    @classmethod
    def matches_target_season(cls, file_name: str, target_season: int) -> bool:
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
        ):
            match = pattern.search(file_name)
            if match:
                return int(match.group(1)) == target_season
        return False

    @classmethod
    def path_season(cls, text: str) -> Optional[int]:
        """识别目录/路径文本中的显式季号，识别不到返回 None。

        依次匹配 ``SxxExx``、``第N季``、``Season N``，最后补上裸 ``S2``/``S02``
        这类目录名季号（既有 :meth:`contains_other_season` 的三个模式匹配不到）。
        """
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
                cls._BARE_SEASON_PATTERN,
        ):
            match = pattern.search(text or "")
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def path_conflicts_season(cls, text: str, target_season: int) -> bool:
        """路径显式季号存在且与目标季不一致时为 True。"""
        season = cls.path_season(text)
        if season is None:
            return False
        return season != int(target_season)

    @classmethod
    def extract_season_episode(
            cls, file_name: str
    ) -> Optional[Tuple[int, int]]:
        match = cls._SXEX_PATTERN.search(file_name)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @classmethod
    def iter_files(cls, files: list, parent_path: str = "") -> Iterator[Any]:
        for item in files or []:
            name = str(item.get("name") or item.get("file_name") or "").strip()
            relative_path = "/".join(
                value for value in (parent_path.strip("/"), name) if value
            )
            if item.get("is_dir"):
                yield from cls.iter_files(
                    item.get("children") or [], relative_path
                )
            else:
                if relative_path and isinstance(item, dict):
                    item.setdefault("_relative_path", relative_path)
                    if parent_path:
                        item.setdefault("parent_path", parent_path)
                    yield item
                elif relative_path:
                    enriched = dict(item)
                    enriched["_relative_path"] = relative_path
                    if parent_path:
                        enriched["parent_path"] = parent_path
                    yield enriched
                else:
                    if isinstance(item, dict) and item.get("relative_path"):
                        item.setdefault("_relative_path", item["relative_path"])
                    yield item
