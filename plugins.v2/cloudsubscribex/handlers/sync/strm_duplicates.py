"""STRM 重复扫描：同一集多份 .strm 的发现、核对与删除（v1.31.0）。

根因（2026-11-22 两京十五日 S01E05/E06 实测，见提交说明）：
``StrmGenerator.write`` / ``local_path`` 只按视频文件名派生 .strm 名，
从不按“集”去重——同一集两次入库、两次文件名不同（一次带 ``{tmdb-}``
标记、一次不带），就会在同一目录下写出两份 .strm。两份 .strm 各指一份
不同的 115 视频（pickcode 不同，实测 ay9zcfn2… vs ay9zequ54…），
不是旧文件残留。时间戳也佐证这一点：带标签份先写（E05 20:01、E06
20:13），没标签份后写（E05 20:09、E06 20:27）——是两次独立入库各写
一份，不是改名留旧文件。

逐条写点的结论（以代码为准）：
* ``handlers/sync/strm_ops.py::_generate_strm``（:310）是全部实时入库
  路径的唯一 choke 点（direct-strm、postprocess finalize、movie.py、
  retry.py、pt_upgrade.py、transfer_ops 批量都走它）：写成功即返回，
  不检查同目录下是否已有同集 .strm——**这就是根因**。修法：写成功后
  清理同集兄弟 .strm（见 ``cleanup_sibling_strms``），新生成优先。
  漏斗的落盘前实查（v1.29.10 B1 修复）是第一道防线（第二份视频根本
  不应被整理）；这里是最后一道防线，保证入库后只剩一份 .strm。
  洗版场景下清理会比 ``_delete_upgrade_old_strm`` 早一步删掉旧版
  .strm：新版 .strm 已有效、旧视频仍在网盘（等 ``_apply_replacement_deletion``
  删），终态与洗版成功时一致；若提交失败走重试，集仍以新版展示，
  旧视频成孤儿（正常走洗版提交时 ``_apply_replacement_deletion`` 删；
  提交失败则由挂起行“删旧版”清理——扫描页看不到已无 .strm 的旧视频），
  可以接受。
* ``utils/strm_scan.py::rebuild_strm_tree``（:198）与
  ``utils/strm_export.py::rebuild_strm_by_export_tree``（:218）直接调
  ``generator.write``，绕过 ``_generate_strm``，只按精确路径判存在——
  视频改名后补齐会写出第二份 .strm。但 11-21 的事故不是它造成的
  （两份 .strm 都是实时入库时写的），按“不是根因不顺手修”，这里不动；
  改名触发的重复由本页扫描发现、人工删除。
* 漏斗 worker 改名步（``funnel.py:858-880``）在整理前把 staging 名
  改成规范名，.strm 在改名后才生成，改名前 staging 名从未有过 .strm——
  不是根因，不动。
* ``.nfo`` 重复是视频重复的后果，不是独立问题：``_scrape_metadata``
  按（cloud_dir, file_name）给每份视频各刮一份，.nfo 名取视频主干
  （``Path(file_name).stem + ".nfo"``），两份视频主干不同就有两份
  .nfo。所以修 .strm 去重就够了；删除时连带删同主干 .nfo 即可，
  不另修刮削。

扫描只读本地 .strm 文件，零 115 API 调用（删除那一步除外）：
* 扫描：os.walk 递归 .strm 根目录（插件已有 ``_local_resource_path``
  配置，``StrmGenerator.local_path`` 口径，不硬编码 /strm），按
  （父目录、去标签剧名、季、集）分组；三种文件名算同一集。只走
  这一根（线上 /strm/115网盘）；另写的分享树（线上
  /strm/115网盘分享）是按调用方 save_path 另写的、无稳定插件侧根，
  本版不扫。
* 大小核对（enrich）：每组逐项 ``_cloud_query.find_file`` 取 115 视频
  实际大小与 file_id——MP 历史 dest 不可用作统一口径（回填行的
  ``dest_fileitem.size`` 是本地 .strm 大小，见 core 注释），115 列表
  是大小与 file_id 的唯一权威来源；删除本来就要查它。
* 删除：删除前重读磁盘 .strm 核对 pickcode、``find_file_strict``
  （fail-closed：目录读不到抛错，不当作“文件不存在”）定位视频、
  ``_playback_reference`` 核对 pickcode 一致后才删；解析不到就拒绝，
  报明确错误。删除不可逆，不做恢复。

删除规则（用户已定“留大”）：
* 同 pickcode（或同分享 id）= 同一视频：只删多余 .strm（+ 同名 .nfo），
  不动视频。
* 不同 pickcode = 不同视频：删视频 + .strm + 同名 .nfo；分享子树
  的视频不在自己网盘，删不了视频，只删本地 .strm + .nfo 并明说。
* pickcode 与分享 id 都读不到：无法确认视频身份，拒绝删除。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from app.log import logger

from ...core import OwnerDelegator
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


def group_strm_files(
        entries: List[Dict[str, Any]],
        meta_factory=None,
        episode_fallback=None,
) -> List[Dict[str, Any]]:
    """把扫描到的 .strm 条目按集分组，只返回 ≥2 份的重复组。

    条目：``{"path": Path, "content": str, "size": int, "relative": str}``，
    ``relative`` 是相对 .strm 根的路径（展示与删除定位用）。
    每组按“留大”以外的确定性规则先排好序：带 ``{tmdb-}`` 标记的在前
    （现行规范名），再按文件名——删除建议（留大）由 enrich 阶段按
    视频实际大小定，这里只给初始 ``suggested_keep``（同视频组）或 None。
    """
    buckets: Dict[Tuple[str, str, Optional[int], Optional[int]], List[Dict[str, Any]]] = {}
    for entry in entries or []:
        path = entry.get("path")
        key = strm_group_key(
            path,
            meta_factory=meta_factory,
            episode_fallback=episode_fallback,
        )
        if key is None:
            continue
        buckets.setdefault(key, []).append(entry)
    groups: List[Dict[str, Any]] = []
    for (parent, title, season, episode), members in buckets.items():
        if len(members) < 2:
            continue
        items: List[Dict[str, Any]] = []
        for member in members:
            identity = parse_strm_identity(str(member.get("content") or ""))
            items.append({
                "file_name": Path(str(member.get("path") or "")).name,
                "local_path": str(member.get("relative") or ""),
                "size": int(member.get("size") or 0),
                "pickcode": identity["pickcode"],
                "pickcode_prefix": (identity["pickcode"] or "")[:8],
                "share_id": identity["share_id"],
                "video_name": identity["file_name"],
                "kind": identity["kind"],
                "video_size": None,
                "video_id": None,
            })
        # 带标记的规范名在前（同视频组默认保留它）；再按文件名排，稳定。
        items.sort(key=lambda item: (
            0 if _TMDB_TAG_RE.search(item["file_name"]) else 1,
            item["file_name"],
        ))
        pickcodes = {item["pickcode"] for item in items if item["pickcode"]}
        share_ids = {item["share_id"] for item in items if item["share_id"]}
        if len(pickcodes) == 1:
            kind = "same-video"
        elif len(pickcodes) > 1:
            kind = "different-video"
        elif len(share_ids) == 1:
            kind = "same-video"
        elif len(share_ids) > 1:
            kind = "different-video"
        else:
            kind = "unknown"
        if episode is not None:
            media_type = "tv"
            season_episode = f"S{int(season or 0):02d}E{int(episode):02d}"
            group_key = f"{parent}|{title}|{season_episode}"
        else:
            media_type = "movie"
            season_episode = ""
            group_key = f"{parent}|{title}|movie"
        groups.append({
            "key": group_key,
            "title": title,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "season_episode": season_episode,
            "dir": parent,
            "kind": kind,
            "items": items,
            "suggested_keep": 0 if kind == "same-video" else None,
            "sizes_pending": True,
        })
    groups.sort(key=lambda group: (
        group["title"], group["season"] or 0, group["episode"] or 0,
    ))
    return groups


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


class StrmDuplicatesService(OwnerDelegator):
    """重复扫描服务：扫描（纯本地）→ 核对大小 → 删除（人工确认后）。"""

    def duplicates_scan(self) -> Dict[str, Any]:
        """递归走 .strm 根目录，只读本地 .strm，零 115 调用。

        返回重复组（≥2 份同集）+ 进度（已扫目录数 / .strm 数）；
        视频大小此时未知（``sizes_pending``），由 enrich 阶段核对。
        """
        local_root = str(getattr(self, "_local_resource_path", "") or "").strip()
        if not local_root:
            return {"success": False, "message": "未配置本地/挂载媒体根路径"}
        root = Path(local_root)
        if not root.is_dir():
            return {
                "success": False,
                "message": f"本地媒体根目录不存在：{local_root}",
            }
        entries: List[Dict[str, Any]] = []
        dirs_scanned = 0
        unreadable = 0
        for current, _dirs, files in os.walk(root):
            dirs_scanned += 1
            for name in files:
                if not str(name).lower().endswith(".strm"):
                    continue
                path = Path(current) / name
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    unreadable += 1
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    relative = path.name
                entries.append({
                    "path": path, "content": content, "size": size,
                    "relative": relative,
                })
        groups = group_strm_files(entries)
        logger.info(
            "重复扫描完成：已扫 %s 个目录、%s 个 STRM，"
            "发现 %s 组重复（%s 个文件读失败）",
            dirs_scanned, len(entries), len(groups), unreadable,
        )
        return {
            "success": True,
            "data": {
                "strm_root": local_root,
                "dirs_scanned": dirs_scanned,
                "strm_count": len(entries),
                "unreadable": unreadable,
                "groups": groups,
            },
        }

    def _cloud_dir_for_local(self, local_path: str) -> str:
        """本地 .strm 相对路径 → 网盘目录。

        本地路径是网盘目录相对 ``_CLOUD_MEDIA_ROOT`` 的部分（
        ``StrmGenerator.local_path`` 的口径：本地 = 根 + 相对目录），
        反推网盘目录必须补回媒体库根——线上
        ``/strm/115网盘/剧集/...`` 对应
        ``/Alist/影视/moviepilot/媒体库/剧集/...``，不是 ``/剧集/...``。
        直接拿相对目录当网盘目录，线上 enrich 查不到大小、delete
        永远拒绝删除（v1.31.0 复核发现，初版即按此修）。未配
        ``_CLOUD_MEDIA_ROOT`` 时退回 ``/``（单测假服务口径）。
        """
        media_root = str(getattr(self, "_CLOUD_MEDIA_ROOT", "") or "/").strip() or "/"
        parent = Path(str(local_path or "")).parent.as_posix().strip("/")
        if parent in ("", "."):
            return media_root.rstrip("/") or "/"
        return (media_root.rstrip("/") + "/" + parent) or "/"

    def duplicates_enrich(self, groups: Any) -> Dict[str, Any]:
        """按 115 列表核对每组各份的视频实际大小与 file_id，给出留大建议。

        大小取 115 视频的实际大小（不是 .strm 文件大小）：MP 历史
        dest 不可用作统一口径（回填行的 dest_fileitem.size 是本地
        .strm 大小），115 列表是大小与 file_id 的唯一权威来源；删除
        本来就要查它。分享形 .strm 的视频不在自己网盘，不查、不给大小。
        每组一个 find_file 调用（按视频名精确查）；查不到的项大小留空，
        建议随之降级（大小全未知时不给建议，请人工选）。
        """
        local_root = str(getattr(self, "_local_resource_path", "") or "").strip()
        cloud_query = getattr(self, "_cloud_query", None)
        if not local_root:
            return {"success": False, "message": "未配置本地/挂载媒体根路径"}
        if cloud_query is None:
            return {"success": False, "message": "网盘查询服务不可用"}
        if not isinstance(groups, list):
            return {"success": False, "message": "扫描结果格式错误"}
        enriched: List[Dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group = dict(group)
            items = [dict(item) for item in (group.get("items") or [])]
            group["items"] = items
            for item in items:
                if item.get("kind") != "redirect":
                    continue
                video_name = str(item.get("video_name") or "").strip()
                local_path = str(item.get("local_path") or "").strip()
                if not video_name or not local_path:
                    continue
                cloud_dir = self._cloud_dir_for_local(local_path)
                try:
                    found = cloud_query.find_file(cloud_dir, video_name)
                except Exception as error:  # noqa: BLE001 - 单项查不到不阻塞整组
                    logger.debug(
                        f"重复扫描核对失败：{cloud_dir}/{video_name}，{error}",
                        exc_info=True,
                    )
                    continue
                if found is None:
                    continue
                try:
                    item["video_size"] = int(getattr(found, "size", 0) or 0)
                except (TypeError, ValueError):
                    item["video_size"] = None
                item["video_id"] = str(getattr(found, "id", "") or "")
            group["suggested_keep"] = self._suggest_keep(group)
            group["sizes_pending"] = False
            enriched.append(group)
        return {"success": True, "data": {"groups": enriched}}

    @staticmethod
    def _suggest_keep(group: Dict[str, Any]) -> Optional[int]:
        """留大：不同视频组保留视频最大的那份；同视频组保留规范名那份。

        大小全未知时返回 None（请人工选）；大小并列时优先带标记名。
        """
        items = group.get("items") or []
        if not items:
            return None
        if group.get("kind") == "same-video":
            return 0
        if group.get("kind") != "different-video":
            return None
        best_index: Optional[int] = None
        best_key: Optional[Tuple[int, int]] = None
        for index, item in enumerate(items):
            size = item.get("video_size")
            if not isinstance(size, int):
                continue
            tagged = 1 if _TMDB_TAG_RE.search(str(item.get("file_name") or "")) else 0
            key = (size, tagged)
            if best_key is None or key > best_key:
                best_key = key
                best_index = index
        return best_index

    def duplicates_delete(self, group: Any, keep_index: Any) -> Dict[str, Any]:
        """按用户确认删除一组重复（每组一键删）。

        逐项核对、逐项删：重读磁盘 .strm 核对 pickcode/分享 id、
        find_file_strict 定位视频（fail-closed）、playback 引用核对
        pickcode 一致后才删视频；视频删成功（或同视频组本就不用删视频）
        才删本地 .strm + 同主干 .nfo。解析不到 / 核对不上 / 网盘删失败
        的项拒绝删除并报明确原因，不动其他项。
        """
        local_root = str(getattr(self, "_local_resource_path", "") or "").strip()
        if not local_root:
            return {"success": False, "message": "未配置本地/挂载媒体根路径"}
        if not isinstance(group, dict):
            return {"success": False, "message": "重复组格式错误"}
        items = group.get("items") or []
        try:
            keep = int(keep_index)
        except (TypeError, ValueError):
            return {"success": False, "message": "未指定保留哪一份"}
        if not 0 <= keep < len(items):
            return {"success": False, "message": "保留序号超出范围"}
        if group.get("kind") == "unknown":
            return {
                "success": False,
                "message": "该组无法确认视频身份（读不到 pickcode），请人工核对后手动删除",
            }
        results: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            if index == keep:
                results.append({
                    "file_name": str(item.get("file_name") or ""),
                    "kept": True, "success": True, "message": "已保留",
                })
                continue
            results.append(self._delete_duplicate_item(
                group, item, Path(local_root),
            ))
        deleted = sum(1 for result in results if result.get("success") and not result.get("kept"))
        failed = [result for result in results if not result.get("success")]
        return {
            "success": not failed,
            "message": (
                f"删除完成：删 {deleted} 份"
                + (f"，{len(failed)} 份失败" if failed else "")
            ),
            "data": {"results": results},
        }

    def _delete_duplicate_item(
            self,
            group: Dict[str, Any],
            item: Dict[str, Any],
            local_root: Path,
    ) -> Dict[str, Any]:
        """删一组里的一份（非保留项）：核对 → 删视频（若需）→ 删本地文件。"""
        file_name = str(item.get("file_name") or "")
        base = {"file_name": file_name, "kept": False}
        local_path = str(item.get("local_path") or "").strip()
        if not local_path:
            return {**base, "success": False, "message": "缺少本地路径，拒绝删除"}
        strm_path = local_root / local_path
        try:
            content = strm_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as error:
            return {**base, "success": False, "message": f"本地 STRM 读不到：{error}"}
        identity = parse_strm_identity(content)
        if identity["kind"] == "unknown":
            return {
                **base, "success": False,
                "message": "STRM 内容读不到 pickcode，无法确认视频身份，拒绝删除",
            }
        if identity["kind"] == "redirect":
            if identity["pickcode"] != str(item.get("pickcode") or ""):
                return {
                    **base, "success": False,
                    "message": "STRM 内容已变化（pickcode 与扫描时不一致），请重新扫描",
                }
        elif identity["share_id"] != str(item.get("share_id") or ""):
            return {
                **base, "success": False,
                "message": "STRM 内容已变化（分享 id 与扫描时不一致），请重新扫描",
            }
        video_deleted = False
        video_note = ""
        if group.get("kind") == "different-video" and identity["kind"] == "redirect":
            check = self._delete_cloud_video(strm_path, identity)
            if not check["success"]:
                return {**base, "success": False, "message": check["message"]}
            video_deleted = True
        elif group.get("kind") == "different-video" and identity["kind"] == "share":
            video_note = "视频在分享方网盘，未删视频，仅删本地 STRM"
        nfo_deleted = False
        try:
            strm_path.unlink()
        except OSError as error:
            return {
                **base, "success": False,
                "message": f"本地 STRM 删失败：{error}",
                "video_deleted": video_deleted,
            }
        nfo = nfo_path_for_strm(strm_path)
        try:
            if nfo.is_file():
                nfo.unlink()
                nfo_deleted = True
        except OSError as error:
            logger.warning(f"删 NFO 失败：{nfo}，{error}")
        return {
            **base, "success": True,
            "message": (
                "已删视频 + STRM" if video_deleted
                else video_note or "已删 STRM（同视频，只删多余 STRM）"
            ),
            "video_deleted": video_deleted,
            "nfo_deleted": nfo_deleted,
        }

    def _delete_cloud_video(
            self, strm_path: Path, identity: Dict[str, str],
    ) -> Dict[str, Any]:
        """定位并删除一份 115 视频：三重核对，任一不符即拒绝。

        1. 视频名取自 .strm 内容的 file_name 参数（不是猜后缀）；
        2. 云目录由本地路径按 local_path 口径反推（相对根的父目录）；
        3. find_file_strict 严格定位（目录读不到抛错，不当作不存在）；
        4. playback 引用核对 pickcode 与 .strm 一致。
        """
        cloud_query = getattr(self, "_cloud_query", None)
        cloud_mutations = getattr(self, "_cloud_mutations", None)
        if cloud_query is None or cloud_mutations is None:
            return {"success": False, "message": "网盘查询/删除服务不可用"}
        video_name = str(identity.get("file_name") or "").strip()
        if not video_name:
            return {
                "success": False,
                "message": "STRM 里没有视频文件名，无法定位视频，拒绝删除",
            }
        local_root = str(getattr(self, "_local_resource_path", "") or "").strip()
        try:
            relative = strm_path.relative_to(Path(local_root)).as_posix()
        except ValueError:
            return {
                "success": False,
                "message": "STRM 不在媒体根目录下，无法定位视频，拒绝删除",
            }
        cloud_dir = self._cloud_dir_for_local(relative)
        try:
            found = cloud_query.find_file_strict(cloud_dir, video_name)
        except Exception as error:  # noqa: BLE001 - 定位异常 = 核对失败，拒绝删
            return {
                "success": False,
                "message": f"网盘目录读不到，无法确认视频，拒绝删除：{error}",
            }
        if found is None:
            return {
                "success": False,
                "message": f"网盘里找不到该视频（{cloud_dir}/{video_name}），拒绝删除",
            }
        file_id = str(getattr(found, "id", "") or "").strip()
        if not file_id:
            return {"success": False, "message": "网盘视频没有 file_id，拒绝删除"}
        pickcode = self._cloud_pickcode(found)
        if pickcode is None:
            return {
                "success": False,
                "message": "无法核对视频 pickcode，拒绝删除",
            }
        if pickcode != identity["pickcode"]:
            return {
                "success": False,
                "message": "网盘视频 pickcode 与 STRM 不一致，拒绝删除（可能已换文件）",
            }
        try:
            deleted = bool(cloud_mutations.delete_file(file_id))
        except Exception as error:  # noqa: BLE001 - 删除异常按失败计
            return {"success": False, "message": f"网盘删除异常：{error}"}
        if not deleted:
            return {"success": False, "message": "网盘删除失败（接口返回失败）"}
        logger.warning(
            "重复扫描删视频：%s/%s（file_id %s，pickcode %s）",
            cloud_dir, video_name, file_id, pickcode[:8],
        )
        return {"success": True, "file_id": file_id}

    def _cloud_pickcode(self, cloud_file: Any) -> Optional[str]:
        """取网盘文件的 playback pickcode；取不到返回 None（核对失败）。"""
        reference = getattr(self, "_playback_reference", None)
        if reference is None:
            return None
        try:
            values = reference.reference_values(cloud_file) or {}
        except Exception as error:  # noqa: BLE001 - 取不到按核对失败
            logger.debug(f"取视频 playback 引用失败：{error}", exc_info=True)
            return None
        for key in ("pickcode", "pick_code", "pc"):
            value = str(values.get(key) or "").strip()
            if value:
                return value
        return None
