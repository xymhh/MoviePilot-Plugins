"""115 网盘整理台的纯函数：目录遍历、季集解析、分组与字幕关联。

这里刻意不导入 MoviePilot 运行时，便于单测直接覆盖扫描/分组/去重逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".iso", ".ts", ".m2ts", ".wmv", ".mov", ".flv", ".rmvb"})
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks"})

_SEASON_EPISODE_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,4})"),
    re.compile(r"(\d{1,3})[xX](\d{1,4})"),
    re.compile(r"第\s*(\d{1,2})\s*季.*?第\s*(\d{1,4})\s*[集话]"),
)
_EPISODE_ONLY_PATTERNS = (
    re.compile(r"第\s*(\d{1,4})\s*[集话]"),
    re.compile(r"(?<![A-Za-z0-9])EP?\s*(\d{1,4})(?![A-Za-z0-9])", re.IGNORECASE),
)
_SEASON_PATTERNS = (
    re.compile(r"[Ss](\d{1,2})[Ee]"),
    re.compile(r"第\s*(\d{1,2})\s*季"),
    re.compile(r"[Ss]eason\s*(\d{1,2})", re.IGNORECASE),
    re.compile(r"(?:^|[^0-9A-Za-z])[Ss]0?(\d{1,2})(?![0-9A-Za-z])"),
)
_GENERIC_DIRS = {
    "电影", "电影库", "剧集", "剧集库", "电视剧", "电视", "动画", "动漫",
    "纪录片", "综艺", "短剧", "微短剧", "番剧",
    "国产剧", "国产电视剧", "国剧", "大陆剧", "华语剧",
    "欧美剧", "美剧", "英剧", "日韩剧", "日剧", "韩剧",
    "港剧", "台剧", "港台剧", "泰剧", "海外剧", "其他剧",
    "国漫", "日漫",
    "movies", "tv", "shows", "anime", "documentary", "variety",
    "downloads", "download",
    "待整理", "整理", "暂存", "临时", "未分类", "未整理", "其他", "其它",
    "temp", "incoming", "uploads", "upload",
}
_TECH_TOKEN = re.compile(
    r"(?i)(?:\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|hdr10|dv|dovi|"
    r"web[- .]?dl|webrip|bluray|blu[- .]?ray|bdrip|remux|hdrip|hdtv|"
    r"x26[45]|h[ .]?26[45]|hevc|avc|av1|ddp?[ .]?\d(?:[ .]\d)?|eac3|"
    r"aac|flac|truehd|atmos|dts(?:[- .]?hd)?|ma|"
    r"chs|cht|gb|big5|zh|eng|sub|subs|subbed|multi|proper|repack)\b)"
)
_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_DATE_CONTAINER = re.compile(
    r"(?:(?:19|20)\d{2}[-._年]?\d{1,2}[-._月]?\d{1,2}日?|"
    r"\d{1,2}[-._月]\d{1,2}日?)"
)


@dataclass(frozen=True)
class OrganizeEntry:
    """扫描到的网盘文件。"""

    file_id: str
    name: str
    size: int
    parent_path: str
    is_video: bool = False
    is_subtitle: bool = False
    cloud_file: Any = field(default=None, repr=False, compare=False)

    @property
    def path(self) -> str:
        base = str(self.parent_path or "/").rstrip("/")
        return f"{base}/{self.name}" if base else f"/{self.name}"

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.name).suffix.lower()

    @property
    def stem(self) -> str:
        return PurePosixPath(self.name).stem


@dataclass
class ScanStats:
    directories: int = 0
    files: int = 0
    videos: int = 0
    subtitles: int = 0
    errors: int = 0
    truncated: bool = False


@dataclass
class ScanResult:
    entries: List[OrganizeEntry] = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)
    errors: List[str] = field(default_factory=list)


@dataclass
class VideoCandidate:
    entry: OrganizeEntry
    season: int
    episode: int
    subtitles: List[OrganizeEntry] = field(default_factory=list)


@dataclass
class PackagePlan:
    key: str
    pack_name: str
    source_dirs: List[str]
    videos: List[VideoCandidate]
    duplicates: List[OrganizeEntry]
    ignored: List[OrganizeEntry]
    is_tv: bool


def normalize_extensions(values: Optional[Iterable[str]], default: Iterable[str]) -> Set[str]:
    result = set()
    for raw in values or ():
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if not text.startswith("."):
            text = f".{text}"
        result.add(text)
    return result or set(default)


def suffix_of(name: str) -> str:
    return PurePosixPath(str(name or "")).suffix.lower()


def is_video(name: str, video_extensions: Optional[Iterable[str]] = None) -> bool:
    return suffix_of(name) in normalize_extensions(video_extensions, VIDEO_EXTENSIONS)


def is_subtitle(name: str, subtitle_extensions: Optional[Iterable[str]] = None) -> bool:
    return suffix_of(name) in normalize_extensions(subtitle_extensions, SUBTITLE_EXTENSIONS)


def parse_episode(name: str) -> Optional[Tuple[int, int]]:
    """从文件名解析 ``(season, episode)``；季号未知时返回 0。"""
    text = str(name or "")
    for pattern in _SEASON_EPISODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))
    for pattern in _EPISODE_ONLY_PATTERNS:
        match = pattern.search(text)
        if match:
            return 0, int(match.group(1))
    return None


def parse_season(text: str) -> Optional[int]:
    value = str(text or "")
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(value)
        if match:
            return int(match.group(1))
    return None


def clean_title(text: str) -> str:
    """把目录/文件名粗洗成可识别的标题，仅用于分组与兜底识别。"""
    value = str(text or "")
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"【[^】]*】", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"（[^）]*）", " ", value)
    parsed = parse_episode(value)
    if parsed:
        value = re.split(r"[Ss]\d{1,2}[ ._-]*[Ee]\d{1,4}|第\s*\d{1,4}\s*[集话]|EP?\s*\d{1,4}", value, maxsplit=1,
                         flags=re.IGNORECASE)[0]
    value = _TECH_TOKEN.sub(" ", value)
    value = re.sub(r"[._]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    return value


def relative_parts(path: str, source_root: str) -> List[str]:
    root = "/" + str(source_root or "/").strip("/")
    target = "/" + str(path or "/").strip("/")
    if target == root:
        return []
    if target.startswith(root.rstrip("/") + "/"):
        target = target[len(root.rstrip("/")) + 1:]
    return [part for part in target.strip("/").split("/") if part]


def package_key(entry: OrganizeEntry, source_root: str) -> str:
    """返回作品包键：优先取源根下第一个非季号/非通用容器的目录。"""
    parts = relative_parts(entry.parent_path, source_root)
    # 取离文件最近的、不是容器（季/日期/媒体库分类）的目录作为作品名。
    # 这样 /剧集/国产剧/某剧/Season 1 会得到「某剧」，而不是「国产剧」。
    while parts and is_container_dir(parts[-1]):
        parts.pop()
    if parts:
        return str(parts[-1]).strip()
    # 文件直接散落在源根/日期目录/季目录里时，用文件名识别作品，
    # 不能把所有散文件都归到「待整理」这个容器名下。
    return clean_title(entry.stem) or entry.stem


def is_container_dir(text: str) -> bool:
    """判断目录名是否只是容器（库、季、日期、集），不是作品名。"""
    value = str(text or "").strip()
    if not value:
        return True
    if value.lower() in _GENERIC_DIRS:
        return True
    if parse_season(value) is not None and len(value) <= 20:
        return True
    if _DATE_CONTAINER.fullmatch(value):
        return True
    if re.fullmatch(r"(?:第\s*)?\d{1,4}\s*[天日集期]", value):
        return True
    return False


def package_name(entry: OrganizeEntry, source_root: str) -> str:
    key = package_key(entry, source_root)
    if key and key != entry.stem:
        return key
    return clean_title(entry.stem) or entry.stem


def build_packages(
        entries: Sequence[OrganizeEntry],
        source_root: str,
        video_extensions: Optional[Iterable[str]] = None,
        subtitle_extensions: Optional[Iterable[str]] = None,
) -> List[PackagePlan]:
    """按作品包分组，解析季集，关联字幕并剔除重复集。"""
    grouped: Dict[str, List[OrganizeEntry]] = {}
    for entry in entries:
        if entry.is_video or is_video(entry.name, video_extensions):
            grouped.setdefault(package_key(entry, source_root), []).append(entry)

    plans: List[PackagePlan] = []
    for key, videos in grouped.items():
        videos = sorted(videos, key=lambda item: (-int(item.size or 0), item.name))
        subtitles = [
            entry for entry in entries
            if (entry.is_subtitle or is_subtitle(entry.name, subtitle_extensions))
            and package_key(entry, source_root) == key
        ]
        parsed: List[VideoCandidate] = []
        unparsed: List[OrganizeEntry] = []
        for video in videos:
            season, episode = _resolve_episode(video)
            if episode <= 0:
                unparsed.append(video)
                continue
            parsed.append(VideoCandidate(
                entry=video,
                season=season,
                episode=episode,
                subtitles=associate_subtitles(video, subtitles),
            ))
        is_tv = bool(parsed)
        duplicates: List[OrganizeEntry] = []
        if not is_tv:
            # 电影：包内最大视频为主视频，其余视为花絮/样片，不自动搬。
            main = videos[0]
            parsed = [VideoCandidate(entry=main, season=0, episode=0,
                                     subtitles=associate_subtitles(main, subtitles))]
            ignored = [video for video in videos if video is not main]
        else:
            parsed, duplicates = dedupe_episodes(parsed)
            ignored = duplicates + unparsed
        source_dirs = sorted({item.entry.parent_path for item in parsed})
        plans.append(PackagePlan(
            key=key,
            pack_name=package_name(videos[0], source_root),
            source_dirs=source_dirs,
            videos=parsed,
            duplicates=duplicates,
            ignored=ignored,
            is_tv=is_tv,
        ))
    plans.sort(key=lambda plan: plan.pack_name.lower())
    return plans


def _resolve_episode(video: OrganizeEntry) -> Tuple[int, int]:
    parsed = parse_episode(video.name)
    path_season = parse_season(video.parent_path)
    if parsed:
        season, episode = parsed
        if season <= 0 and path_season:
            season = path_season
        return max(0, season), max(0, episode)
    if path_season:
        # 目录写明了季，但文件名没有集号：不能猜，交给调用方标记待确认。
        return path_season, 0
    return 0, 0


def associate_subtitles(video: OrganizeEntry, subtitles: Sequence[OrganizeEntry]) -> List[OrganizeEntry]:
    video_episode = parse_episode(video.name)
    candidates: List[OrganizeEntry] = []
    for subtitle in subtitles:
        if subtitle.parent_path != video.parent_path:
            continue
        sub_episode = parse_episode(subtitle.name)
        if sub_episode and video_episode:
            if sub_episode != video_episode:
                continue
        else:
            prefix_match = subtitle.stem.lower().startswith(video.stem.lower())
            video_title = clean_title(video.stem).lower()
            subtitle_title = clean_title(subtitle.stem).lower()
            title_match = bool(video_title) and (
                subtitle_title == video_title or subtitle_title.startswith(f"{video_title} ")
            )
            if not prefix_match and not title_match:
                continue
        candidates.append(subtitle)
    return sorted(candidates, key=lambda item: item.name)


def dedupe_episodes(videos: Sequence[VideoCandidate]) -> Tuple[List[VideoCandidate], List[OrganizeEntry]]:
    """同一季集只保留体积最大的版本；其余作为重复项返回。"""
    best: Dict[Tuple[int, int], VideoCandidate] = {}
    duplicates: List[OrganizeEntry] = []
    for candidate in videos:
        key = (candidate.season, candidate.episode)
        current = best.get(key)
        if current is None or int(candidate.entry.size or 0) > int(current.entry.size or 0):
            if current is not None:
                duplicates.append(current.entry)
            best[key] = candidate
        else:
            duplicates.append(candidate.entry)
    ordered = sorted(best.values(), key=lambda item: (item.season, item.episode, item.entry.name))
    return ordered, duplicates


def scan_entries(
        provider: Any,
        source_root: str,
        *,
        video_extensions: Optional[Iterable[str]] = None,
        subtitle_extensions: Optional[Iterable[str]] = None,
        max_files: int = 500,
        max_depth: int = 8,
        max_entries: int = 5000,
        stop_requested: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[ScanStats], None]] = None,
) -> ScanResult:
    """按目录 CID BFS 遍历源目录，只读不写。"""
    result = ScanResult()
    root = "/" + str(source_root or "/").strip("/")
    lookup = provider.resolve_directory(root)
    if not getattr(lookup, "checked", False) or getattr(lookup, "directory_id", None) is None:
        result.errors.append(f"无法解析源目录：{root}")
        return result

    stack: List[Tuple[str, str, int]] = [(str(lookup.directory_id), root, 0)]
    visited: Set[str] = set()
    max_files = max(1, int(max_files or 1))
    max_depth = max(0, int(max_depth or 0))
    max_entries = max(max_files, int(max_entries or max_files))

    while stack:
        if stop_requested and stop_requested():
            result.stats.truncated = True
            break
        cid, dir_path, depth = stack.pop()
        if cid in visited:
            continue
        visited.add(cid)
        listing = provider.list_directory(cid)
        if not getattr(listing, "checked", False):
            result.stats.errors += 1
            if len(result.errors) < 20:
                result.errors.append(f"读取目录失败：{dir_path}")
            continue
        result.stats.directories += 1
        if progress and result.stats.directories % 10 == 0:
            progress(result.stats)
        for item in getattr(listing, "files", ()) or ():
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            child_path = f"{dir_path.rstrip('/')}/{name}" if dir_path != "/" else f"/{name}"
            if getattr(item, "is_directory", False):
                if depth < max_depth:
                    stack.append((str(getattr(item, "id", "") or ""), child_path, depth + 1))
                continue
            result.stats.files += 1
            if result.stats.files >= max_entries:
                result.stats.truncated = True
                break
            video = is_video(name, video_extensions)
            subtitle = is_subtitle(name, subtitle_extensions)
            if not video and not subtitle:
                continue
            if video and result.stats.videos >= max_files:
                result.stats.truncated = True
                continue
            entry = OrganizeEntry(
                file_id=str(getattr(item, "id", "") or ""),
                name=name,
                size=int(getattr(item, "size", 0) or 0),
                parent_path=dir_path,
                is_video=video,
                is_subtitle=subtitle,
                cloud_file=item,
            )
            result.entries.append(entry)
            if video:
                result.stats.videos += 1
            elif subtitle:
                result.stats.subtitles += 1
        if result.stats.truncated:
            break
        if result.stats.videos >= max_files:
            # 达到视频上限后不再继续扩大目录范围。
            break
    if progress:
        progress(result.stats)
    return result


def safe_join(base: str, *parts: str) -> str:
    path = PurePosixPath(str(base or "/"))
    for part in parts:
        text = str(part or "").strip().strip("/")
        if text:
            path = path / text
    return "/" + path.as_posix().lstrip("/")


def suffix_name(name: str, index: int) -> str:
    """在扩展名前加 `` (n)``，用于同名冲突策略。"""
    path = PurePosixPath(str(name or ""))
    return f"{path.stem} ({index}){path.suffix}"
