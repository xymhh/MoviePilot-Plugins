"""分享识别（M1：纯逻辑，只读）。

按 `docs/plan/2026-11-07-分享识别重构-方案.md` 的 v2 方案实现识别核心：

1. 枚举结果（完整文件路径 + 体积）进来；
2. 一级目录 = 版本组，根目录散文件 = 散文件组；
3. 每组算集号并集 → 选主版本（体积中位数最大）→ 缺集从更小的组补齐。

本模块**不联网、不转存、不看消息文本**：只有文件事实参与决策，方便单测和回归。
"""

from __future__ import annotations

import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .file_parser import MediaFileParser

# R2：只有这些后缀算集。iso 由开关控制（默认关）。
MEDIA_EXTS = frozenset({
    ".mkv", ".mp4", ".ts", ".m2ts", ".mts", ".avi", ".mov", ".wmv",
    ".flv", ".rmvb", ".rm", ".m4v", ".mpg", ".mpeg", ".webm",
})
ISO_EXT = ".iso"
# R3：字幕/外挂音轨不参与识别，但选中媒体同目录的一起转。
SIDECAR_EXTS = frozenset({
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".mka",
    ".ac3", ".dts", ".flac", ".m4a", ".aac", ".eac3", ".truehd",
})

#: 状态：只有三种（R6）
READY, REVIEW, SKIP = "ready", "review", "skip"

#: 异常码（方案 §5）
NO_MEDIA = "NO_MEDIA"
NO_EPISODES = "NO_EPISODES"
NO_IDENTITY = "NO_IDENTITY"
SCAN_FAILED = "SCAN_FAILED"

#: 明显不是集号的数字（分辨率/年份），避免「1080p.mkv」被当成第 1080 集。
_NOT_EPISODE = {480, 576, 720, 1080, 2160, 4320}

def _ext_of(name: str) -> str:
    name = str(name or "")
    if "." not in name:
        return ""
    return f".{name.rsplit('.', 1)[-1].lower()}"


def classify_path(path: str, size: int = 0, treat_iso: bool = False) -> Dict[str, Any]:
    """把一个文件路径变成 File 记录：kind / group / season / ep。

    ``group`` 先按原始路径的一级目录填，随后由 :func:`build_plan` 统一裁剪公共前缀。
    """
    path = str(path or "").strip("/")
    name = path.rsplit("/", 1)[-1]
    ext = _ext_of(name)
    media = ext in MEDIA_EXTS or (treat_iso and ext == ISO_EXT)
    if media:
        kind = "media"
    elif ext in SIDECAR_EXTS:
        kind = "sidecar"
    else:
        kind = "other"
    dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
    season = MediaFileParser.path_season(name)
    if season is None and dir_path:
        season = MediaFileParser.path_season(dir_path)
    ep = _episode_from_name(name, season) if media else None
    return {
        "path": path,
        "name": name,
        "ext": ext,
        "size": int(size or 0),
        "kind": kind,
        "group": path.split("/", 1)[0] if "/" in path else "",
        "season": season,
        "ep": ep,
    }


def _episode_from_name(name: str, season: Optional[int]) -> Optional[int]:
    """只按文件名取集号；取不到返回 None（不猜）。"""
    stem = str(name or "")
    pair = MediaFileParser.extract_season_episode(stem)
    if pair:
        ep_season, ep = int(pair[0]), int(pair[1])
        if season is not None and ep_season != int(season):
            return None
        return ep if ep > 0 else None
    for pattern, group in _EPISODE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        value = int(match.group(group))
        if 0 < value <= 999 and value not in _NOT_EPISODE:
            return value
    return None


_EPISODE_PATTERNS: Sequence[Tuple[Any, int]] = (
    (re.compile(r"[Ee][Pp]?0*(\d{1,4})(?![0-9])"), 1),
    (re.compile(r"第\s*0*(\d{1,4})\s*[集话話]"), 1),
    (re.compile(r"^0*(\d{1,4})(?=[ ._\-]|$)"), 1),
    (re.compile(r"(?:[-–—]\s*|\[)(\d{1,4})(?=[ ._\]-]|$)"), 1),
)


def _common_prefix_depth(files: List[Dict[str, Any]]) -> int:
    """所有文件共有的外层目录层数（分享常被套一层中文剧名目录）。

    只用于划版本组，**不改写 path**：转存要的是原始路径。
    """
    if not files:
        return 0
    segments = [f["path"].split("/") for f in files]
    depth = 0
    while (all(len(s) > depth + 1 for s in segments)
           and len({s[depth] for s in segments}) == 1):
        depth += 1
    return depth


def _label_rank(group: str) -> int:
    """标签优先级：DV > HDR > SDR > 无标签（只在体积中位数并列时用）。"""
    text = str(group or "").upper()
    if "DV" in text or "杜比" in text:
        return 3
    if "HDR" in text:
        return 2
    if "SDR" in text:
        return 1
    return 0


def _median(sizes: Iterable[int]) -> float:
    values = [int(s) for s in sizes if s]
    return float(statistics.median(values)) if values else 0.0


def _group_views(files: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按组汇总：媒体文件、集号→文件、体积中位数。"""
    groups: Dict[str, Dict[str, Any]] = {}
    for item in files:
        if item["kind"] != "media":
            continue
        group = item["group"] or ""
        view = groups.setdefault(group, {"group": group, "files": [], "by_ep": {}})
        view["files"].append(item)
        if item["ep"]:
            # 同组同集多文件时留体积最大的那个，其余记为重复。
            current = view["by_ep"].get(item["ep"])
            if current is None or item["size"] > current["size"]:
                if current is not None:
                    view["files"].remove(current)
                view["by_ep"][item["ep"]] = item
            else:
                view["files"].remove(item)
    for view in groups.values():
        view["median"] = _median(f["size"] for f in view["files"])
        view["rank"] = _label_rank(view["group"])
    return groups


#: 兜底：中文名与集号紧贴、无任何分隔符（黑色五叶草078.mp4）。
#: MP 宿主的 MetaVideo 也要求数字前有空格/短横/下划线才认，这类分享会整批判无集号。
#: 末尾数字前不能是 ASCII 字母（排除 078v2 / Part2 这种）。
_TAIL_EPISODE_RE = re.compile(r"(?<![0-9A-Za-z])(\d{1,3})$")


def _tail_episode_from_name(name: str) -> Optional[int]:
    """只认「末尾紧贴的数字」：黑色五叶草078.mp4 → 78；认不出返回 None。"""
    stem = str(name or "")
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    match = _TAIL_EPISODE_RE.search(stem)
    if not match:
        return None
    value = int(match.group(1))
    if value <= 0 or value in _NOT_EPISODE:
        return None
    return value


def episode_from_name(name: str, season: Optional[int] = None) -> Optional[int]:
    """对外暴露与 :func:`classify_path` 同源的集号解析，供转存阶段复用。

    与 ``rescue_tail_episodes_from_names`` 不同：本函数单文件即可判定，不设「命中 >= 3」
    门槛。仍复用 ``_NOT_EPISODE`` 排除年份/分辨率，避免 ``2024.mkv`` / ``2160p.mkv``
    被当集号；TMDB 整季无分集数据的旧口径由调用方自行保留。

    背景（2026-11-13 玫瑰战争事故）：识别计划用本函数口径认出 ``14 DV.mkv → E14``，
    而转存阶段 ``FileMatcher.episode_from_file`` 认不出该文件、``rescue_tail_episodes_from_names``
    只认「末尾紧贴数字」，导致 ``share_episodes={1..13}``，``missing={14}`` 交集为空 → 转存 0 个文件。
    """
    return _episode_from_name(str(name or ""), season)


def rescue_tail_episodes_from_names(names: Iterable[str]) -> Set[int]:
    """整批文件名都解析不出集号时的兜底（识别阶段与转存阶段共用）。

    门槛与 `_rescue_tail_episodes` 一致：命中 >= 3、集号去重 >= 3、命中率 >= 80%。
    任一不满足就返回空集合，宁可留 review 也不乱转。
    """
    name_list = [str(name or "") for name in names]
    values: List[int] = []
    for name in name_list:
        episode = _tail_episode_from_name(name)
        if episode is not None:
            values.append(episode)
    unique = set(values)
    if len(values) < 3 or len(unique) < 3:
        return set()
    if len(values) / max(len(name_list), 1) < 0.8:
        return set()
    return unique


def _outline_hits(groups: Dict[str, Dict[str, Any]]) -> List[Tuple[Dict[str, Any], int]]:
    """在每组内找「末尾紧贴数字」的文件，命中率不足 80% 的组整组放弃。"""
    hits: List[Tuple[Dict[str, Any], int]] = []
    for view in groups.values():
        candidates: List[Tuple[Dict[str, Any], int]] = []
        for item in view["files"]:
            if item["ep"]:
                continue
            value = _tail_episode_from_name(str(item["name"]))
            if value is None:
                continue
            candidates.append((item, value))
        if not candidates:
            continue
        # 命中率太低说明这一组文件名里混着别的数字（版本、年份），宁可不算。
        if len(candidates) / max(len(view["files"]), 1) < 0.8:
            continue
        hits.extend(candidates)
    return hits


def _rescue_tail_episodes(groups: Dict[str, Dict[str, Any]]) -> bool:
    """整批一个集号都没解析出来时的兜底：只在证据充分时采信「末尾数字=集号」。

    门槛（任一不满足就放弃，宁可留 review 也不乱转）：
    - 命中文件 >= 3；
    - 集号去重 >= 3；
    - 集号不能是年份（4 位由正则长度天然排除）或分辨率。
    """
    hits = _outline_hits(groups)
    episodes = {value for _, value in hits}
    if len(hits) < 3 or len(episodes) < 3:
        return False
    for item, value in hits:
        item["ep"] = value
    return True


def episode_universe(
        entries: Iterable[Dict[str, Any]],
        treat_iso: bool = False,
) -> Set[int]:
    """整棵分享里出现过的集号集合（不看季、不看组），供身份层反推季。"""
    raw = [e for e in (entries or []) if not e.get("is_dir")]
    files = [classify_path(e.get("path", ""), e.get("size", 0), treat_iso) for e in raw]
    return {int(f["ep"]) for f in files if f["kind"] == "media" and f["ep"]}


def build_plan(
        entries: Iterable[Dict[str, Any]],
        work_id: Optional[int] = None,
        season: Optional[int] = None,
        season_total: Optional[int] = None,
        treat_iso: bool = False,
) -> Dict[str, Any]:
    """按 v2 方案算出转存计划。

    :param entries: 枚举结果，每项含 ``path`` / ``size``，可选 ``is_dir``。
    :param work_id: TMDB 作品 id；None 时最高只能到 ``review``。
    :param season_total: TMDB 该季集数，只用于算「缺几集」，不参与选版本。
    """
    raw = [e for e in (entries or []) if not e.get("is_dir")]
    files = [classify_path(e.get("path", ""), e.get("size", 0), treat_iso) for e in raw]
    depth = _common_prefix_depth(files)
    for item in files:
        parts = item["path"].split("/")
        # 组 = 公共前缀之下的一级目录；直接躺在根上的散文件归入 "" 组。
        item["group"] = parts[depth] if len(parts) > depth + 1 else ""
    media = [f for f in files if f["kind"] == "media"]
    plan: Dict[str, Any] = {
        "work_id": work_id,
        "season": season,
        "group": None,
        "selected": [],
        "skipped": [],
        "missing": [],
        "mixed_quality": False,
        "status": READY,
        "reason": "",
        "filled_from": {},
    }
    if not media:
        plan.update(status=SKIP, reason=NO_MEDIA)
        plan["skipped"] = [(f["path"], "not_media") for f in files]
        return plan
    groups = _group_views(files)
    if not groups:
        plan.update(status=SKIP, reason=NO_MEDIA)
        return plan
    episodes = {ep for view in groups.values() for ep in view["by_ep"]}
    if not episodes and _rescue_tail_episodes(groups):
        # 兜底命中：重建分组，让去重与中位数走正常路径。
        groups = _group_views(files)
        episodes = {ep for view in groups.values() for ep in view["by_ep"]}
    if not episodes:
        plan.update(status=REVIEW, reason=NO_EPISODES)
        plan["skipped"] = [(f["path"], "no_episode") for f in media]
        return plan

    # 主版本：体积中位数最大；并列按标签优先级。
    primary = max(groups.values(),
                  key=lambda v: (v["median"], v["rank"], len(v["by_ep"])))
    chosen: Dict[int, Dict[str, Any]] = dict(primary["by_ep"])
    filled_from: Dict[int, str] = {}

    # 补集：缺的集按组体积中位数从大到小从其它组补，补满即停。
    if season_total:
        missing = [ep for ep in range(1, int(season_total) + 1) if ep not in chosen]
        donors = sorted((v for v in groups.values() if v is not primary),
                        key=lambda v: (v["median"], v["rank"]), reverse=True)
        for view in donors:
            if not missing:
                break
            for ep in list(missing):
                donor = view["by_ep"].get(ep)
                if donor is None:
                    continue
                chosen[ep] = donor
                filled_from[ep] = view["group"]
                missing.remove(ep)

    selected: List[str] = [chosen[ep]["path"] for ep in sorted(chosen)]
    primary_paths = {f["path"] for f in primary["files"]}
    # R3：选中媒体同目录的字幕/外挂音轨一起带上。
    selected_dirs = {p.rsplit("/", 1)[0] if "/" in p else "" for p in selected}
    for item in files:
        if item["kind"] != "sidecar":
            continue
        directory = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else ""
        if directory in selected_dirs:
            selected.append(item["path"])

    taken = set(selected)
    for item in files:
        if item["path"] in taken:
            continue
        if item["kind"] != "media":
            plan["skipped"].append((item["path"], f"not_media:{item['kind']}"))
        elif item["path"] not in primary_paths:
            plan["skipped"].append((item["path"], "other_version"))

    plan["group"] = primary["group"]
    plan["selected"] = selected
    plan["mixed_quality"] = bool(filled_from)
    plan["filled_from"] = filled_from
    if season_total:
        plan["missing"] = [ep for ep in range(1, int(season_total) + 1)
                           if ep not in chosen]

    if work_id is None:
        plan.update(status=REVIEW, reason=NO_IDENTITY)
    return plan
