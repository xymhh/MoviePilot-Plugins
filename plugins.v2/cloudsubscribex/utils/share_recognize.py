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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
