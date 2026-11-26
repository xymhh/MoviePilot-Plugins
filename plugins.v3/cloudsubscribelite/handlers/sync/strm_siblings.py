"""STRM 同集兄弟清理：新 .strm 写成功后删同目录同集旧 .strm（+ 同主干 .nfo）。

从已删除的 ``handlers/sync/strm_duplicates.py``（重复扫描页）里抽出的最小
子集——只有 ``handlers/sync/strm_ops.py::_generate_strm`` 还在用它（写成功
后的最后一道去重防线；扫描页本身已随精简删除）。其余扫描/核对/删除逻辑
没有恢复，不要往这里加。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from app.log import logger

from ...core.episode_parse import (
    CN_EPISODE_PATTERN,
    EPISODE_TAG_PATTERN,
    parse_season_episode,
)
from ...utils.strm import DEFAULT_VIDEO_EXTENSIONS
from .naming import _TMDB_TAG_RE

#: .strm 内容是单行 URL；redirect 形带 pickcode，分享形带 id。
#: file_name 参数是视频在网盘里的准确文件名（URL 编码后）。
_STRM_SEASON_DIR_RE = re.compile(r"season\s*(\d+)", re.IGNORECASE)


def parse_strm_identity(content: str) -> Dict[str, str]:
    """解析 .strm 内容，返回 pickcode / 分享 id / 视频文件名。

    redirect 形：``.../redirect?pickcode=...&file_name=...``；
    分享形：``.../redirect?share_code=...&receive_code=...&id=...&file_name=...``；
    都读不到时 kind 为 ``"unknown"``，调用方按“无法确认身份”处理。
    """
    text = str(content or "").strip()
    result = {"pickcode": "", "share_id": "", "file_name": "", "kind": "unknown"}
    if not text:
        return result
    try:
        query = parse_qs(urlparse(text).query)
    except Exception:
        return result

    def _first(key: str) -> str:
        values = query.get(key) or []
        return str(values[0] or "").strip() if values else ""

    result["pickcode"] = _first("pickcode")
    result["share_id"] = _first("id")
    raw_name = _first("file_name")
    if raw_name:
        try:
            result["file_name"] = unquote(raw_name)
        except Exception:
            result["file_name"] = raw_name
    if result["pickcode"]:
        result["kind"] = "redirect"
    elif result["share_id"]:
        result["kind"] = "share"
    return result


def strip_video_suffix(stem: str) -> str:
    """去掉主干末尾一层视频后缀（如 “.mp4.strm” 的主干 “.mp4”）。

    “.strm” 本身调用方已用 ``Path(name).stem`` 去掉；这里只处理视频那层。
    """
    text = str(stem or "")
    suffix = Path(text).suffix.lower()
    if suffix in DEFAULT_VIDEO_EXTENSIONS:
        return Path(text).stem
    return text


def normalize_strm_stem(stem: str) -> str:
    """去标签 + 去视频后缀 + 空白归一，供分组与标题提取用。

    与 ``handlers/sync/naming.py:_TMDB_TAG_RE`` 同口径（``{tmdb-123}`` /
    ``[tmdbid-123]``）；任一处改动须同步（测试校验两者在样例上一致）。
    """
    text = strip_video_suffix(str(stem or ""))
    text = _TMDB_TAG_RE.sub("", text)
    return " ".join(text.split()).strip()


def season_from_dirname(dirname: str) -> Optional[int]:
    """从父目录名取季号（MP 分类目录形如 “Season 1”）；取不到返回 None。"""
    match = _STRM_SEASON_DIR_RE.search(str(dirname or ""))
    if not match:
        return None
    try:
        season = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return season if season > 0 else None


def strm_group_key(
        strm_path: Any,
        meta_factory=None,
        episode_fallback=None,
) -> Optional[Tuple[str, str, Optional[int], Optional[int]]]:
    """算一份 .strm 的分组键（父目录、去标签剧名、季、集）。

    集号解析复用 ``core/episode_parse.parse_season_episode``（唯一入口）；
    父目录季号只作缺省（文件名无季号时兜底）。电影无集号，键的季、集
    均为 None，按整主干标题分组。解析不出剧名或集号不定（多集）时
    返回 None，调用方跳过该文件（宁可漏报，不可错并）。
    ``meta_factory`` / ``episode_fallback`` 只为单测打桩，生产不传。
    """
    path = Path(str(strm_path or ""))
    if not path.name:
        return None
    stem = normalize_strm_stem(path.stem)
    if not stem:
        return None
    dir_season = season_from_dirname(path.parent.name)
    season, episodes = parse_season_episode(
        stem,
        season=dir_season,
        meta_factory=meta_factory,
        episode_fallback=episode_fallback,
    )
    title = ""
    tag_match = EPISODE_TAG_PATTERN.search(stem)
    if tag_match:
        title = stem[:tag_match.start()].strip(" -_").strip()
    else:
        cn_match = CN_EPISODE_PATTERN.search(stem)
        if cn_match:
            title = stem[:cn_match.start()].strip(" -_").strip()
        else:
            title = stem
    if not title:
        return None
    if episodes:
        if len(episodes) != 1:
            return None
        return (path.parent.as_posix(), title, season, int(episodes[0]))
    return (path.parent.as_posix(), title, None, None)


def nfo_path_for_strm(strm_path: Any) -> Path:
    """同主干 .nfo 的路径（刮削规则：视频主干 + “.nfo”）。

    “.mp4.strm” 的 .nfo 是 “主干.nfo”（不是“主干.mp4.nfo”）：
    ``_scrape_metadata`` 用 ``mapped_path.with_suffix(视频后缀)`` 得
    media_path，再以它取 .nfo 名。
    """
    path = Path(str(strm_path or ""))
    base = strip_video_suffix(path.stem)
    return path.parent / f"{base}.nfo"


def cleanup_sibling_strms(
        local_dir: Any,
        written_path: Any,
        meta_factory=None,
        episode_fallback=None,
) -> List[Path]:
    """新 .strm 写成功后，删掉同目录下同集的旧 .strm（+ 同主干 .nfo）。

    根因修复（v1.31.0）：``_generate_strm`` 的唯一补位——写点只按视频
    文件名派生 .strm 名，两次入库文件名不同就各写一份。这里按“集”
    去重：新生成优先，旧兄弟只删文件，不动网盘视频（删视频是扫描页
    人工确认的动作，这里不做）。单文件失败不影响其他兄弟；调用方
    再包一层 try/except，本函数永不抛错到生成主流程。
    """
    deleted: List[Path] = []
    try:
        directory = Path(str(local_dir or ""))
        written = Path(str(written_path or ""))
    except Exception:
        return deleted
    if not directory.is_dir() or not written.name:
        return deleted
    try:
        key = strm_group_key(
            written, meta_factory=meta_factory, episode_fallback=episode_fallback,
        )
    except Exception:
        return deleted
    if key is None:
        return deleted
    try:
        siblings = [
            item for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() == ".strm"
            and item.name != written.name
        ]
    except OSError:
        return deleted
    for sibling in siblings:
        try:
            sibling_key = strm_group_key(
                sibling,
                meta_factory=meta_factory,
                episode_fallback=episode_fallback,
            )
        except Exception:
            continue
        if sibling_key != key:
            continue
        try:
            identity = parse_strm_identity(
                sibling.read_text(encoding="utf-8", errors="ignore")
            )
        except OSError:
            identity = {"pickcode": ""}
        try:
            sibling.unlink()
            deleted.append(sibling)
            logger.warning(
                "同集旧 STRM 已清理：%s（新生成 %s 接管，旧 pickcode %s）",
                sibling, written.name, (identity.get("pickcode") or "")[:8],
            )
        except OSError as error:
            logger.warning(f"清理同集旧 STRM 失败：{sibling}，{error}")
            continue
        nfo = nfo_path_for_strm(sibling)
        try:
            if nfo.is_file():
                nfo.unlink()
                logger.warning(f"同集旧 NFO 已清理：{nfo}")
        except OSError as error:
            logger.warning(f"清理同集旧 NFO 失败：{nfo}，{error}")
    return deleted
