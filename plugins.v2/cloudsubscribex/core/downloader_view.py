"""把网盘订阅助手的运行时任务投影成 MoviePilot 下载器任务。

下载管理页按系统配置里的下载器名称分标签；本插件登记一个
「网盘订阅助手」下载器条目（见 :meth:`CloudSubscribeX._ensure_downloader_entry`），
实际任务由插件的 ``list_torrents`` 返回，本模块只做纯投影——
不碰任务执行、不碰历史表、不删文件。

任务来源是 :meth:`SyncRuntimeService._serialize_runtime_tasks`
返回的字典：订阅同步任务（id 如 ``subscribe:123`` / ``media:...``）
与无归属的跨盘转存子任务（id 如 ``cross:<hex>``）。

投影同时给出 ``media``（DownloadTaskMedia 口径：类型/标题/季/集/海报）：
前端海报只读 ``info.media.poster``，MP 自身的历史富化按任务 hash 查不到
记录，所以海报必须由本投影直接给。海报按任务字典里的 ``tmdb_id`` 经
MP 进程内 MediaChain 解析，进程内按 tmdb_id 缓存（命中永久有效，
未命中 10 分钟后重试），下载页每 3 秒轮询也不会重复调 TMDB。
"""

import hashlib
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import quote

from .episode_parse import parse_episodes_from_names

# 下载管理页的标签名与下载器配置里的 type（插件类名）。
DOWNLOADER_NAME = "网盘订阅助手"
DOWNLOADER_TYPE = "CloudSubscribeX"

# 运行时任务里视为“进行中”的状态；其余一律按已暂停展示。
_DOWNLOADING_STATUSES = {
    "queued", "running", "downloading", "transferring",
    "postprocessing", "pending",
}
_COMPLETED_STATUSES = {"completed", "success"}


def task_hash(task_id: str) -> str:
    """给任务 id 生成稳定的 40 位十六进制 hash。

    下载页的启停/删除接口按 hash 定位任务；MoviePilot 的任务修改接口
    还会校验 hash 必须是 40 位十六进制，所以不能直接用 ``subscribe:123``
    这类原始 id。
    """
    return hashlib.sha1(f"cloudsubscribex:{task_id}".encode("utf-8")).hexdigest()


def map_task_state(status: Any) -> str:
    """把运行时任务状态映射成下载页的统一状态。"""
    text = str(status or "").strip().lower()
    if text in _COMPLETED_STATUSES:
        return "completed"
    if text in _DOWNLOADING_STATUSES:
        return "downloading"
    # stopping / failed / stopped / 未知：一律按已暂停展示。
    return "paused"


def format_speed(bytes_per_second: Any) -> Optional[str]:
    """把字节/秒格式化成下载页的速度文案，无速度时返回 None。"""
    try:
        speed = float(bytes_per_second or 0)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    return f"{speed / 1024 / 1024:.1f} MB/s"


def _task_progress(task: Dict[str, Any]) -> float:
    """任务完成百分比（0-100），后处理阶段优先用后处理进度。"""
    try:
        progress = float(task.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0.0
    if not progress and str(task.get("status") or "") == "postprocessing":
        try:
            progress = float(task.get("postprocess_progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
    return max(0.0, min(100.0, progress))


def _task_file_names(task: Dict[str, Any]) -> List[str]:
    """任务关联的文件名：合并进来的跨盘子任务文件名、当前处理文件、跨盘子任务自身标题。"""
    names: List[str] = []
    for name in task.get("transfer_file_names") or []:
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
    current_file = str(task.get("current_file") or "").strip()
    if current_file and current_file not in names:
        names.append(current_file)
    if str(task.get("task_kind") or "") == "cross_transfer" or str(
        task.get("id") or ""
    ).startswith("cross:"):
        title = str(task.get("title") or "").strip()
        if title and title not in names:
            names.append(title)
    return names


def _task_episodes(task: Dict[str, Any]) -> List[int]:
    """任务集号：文件名解析 > target_episodes；电影返回 []。

    手动转存的任务 target_episodes 常为空（例如只传了一条夸克分享链接），
    真正的集号在正在传的文件名里；文件名里没有集号时才退回 target_episodes。
    """
    if str(task.get("media_type") or "") != "电视剧":
        return []
    try:
        season = int(task.get("season") or 1)
    except (TypeError, ValueError):
        season = 1
    from_names = parse_episodes_from_names(_task_file_names(task), season)
    if from_names:
        return sorted(from_names)
    return sorted({
        int(episode)
        for episode in (task.get("target_episodes") or [])
        if str(episode).isdigit()
    })


def _season_episode_text(task: Dict[str, Any]) -> Optional[str]:
    """季集展示文本，如 S01、S01E21-E22；电影返回 None。"""
    if str(task.get("media_type") or "") != "电视剧":
        return None
    try:
        season = int(task.get("season") or 1)
    except (TypeError, ValueError):
        season = 1
    episodes = _task_episodes(task)
    if not episodes:
        return f"S{season:02d}"
    if len(episodes) == 1:
        return f"S{season:02d}E{episodes[0]:02d}"
    return f"S{season:02d}E{min(episodes):02d}-E{max(episodes):02d}"


# 海报缓存：tmdb_id -> 海报 URL（命中永久有效）；未命中记时间，10 分钟后重试，
# 避免 TMDB 一次抖动就让该媒体在进程生命周期内永远没海报。
_POSTER_CACHE_MAX = 512
_POSTER_MISS_TTL_SECONDS = 600.0
_POSTER_CACHE: Dict[int, str] = {}
_POSTER_MISS_AT: Dict[int, float] = {}
_POSTER_CACHE_LOCK = threading.Lock()


def _tmdb_image_url(poster_path: Any, size: str = "w500") -> str:
    """海报路径转前端可直接加载的地址（与 core/api/page.py 同口径）。"""
    raw = str(poster_path or "").strip()
    if not raw or raw.startswith(("data:", "blob:")):
        return raw
    if not raw.lower().startswith(("http://", "https://")):
        if raw.startswith("/"):
            raw = f"https://image.tmdb.org/t/p/{size}{raw}"
        else:
            return raw
    return f"/api/v1/system/cache/image?url={quote(raw, safe='')}" if raw else ""


def _media_value(media: Any, *names: str) -> Any:
    """兼容平台媒体对象与原始字典，返回首个有效字段。"""
    for name in names:
        value = (
            media.get(name)
            if isinstance(media, dict)
            else getattr(media, name, None)
        )
        if value not in (None, "", 0, "0", [], {}):
            return value
    return None


def _default_poster_resolver(tmdb_id: int, media_type: str) -> str:
    """MP 进程内取海报：走 MediaChain 的 TMDB 详情，不直接调 TMDB 公网接口。

    任何异常（链路缺失、TMDB 不可用、无海报）都按缺失处理，返回空串，
    绝不让海报解析拖垮下载页投影。
    """
    try:
        from app.chain.media import MediaChain

        chain = MediaChain()
    except Exception:
        return ""
    mtype = (
        "tv"
        if str(media_type or "") == "电视剧"
        else "movie"
        if str(media_type or "") == "电影"
        else None
    )
    info = None
    for fetcher_name in ("tmdb_info", "get_tmdbinfo"):
        fetcher = getattr(chain, fetcher_name, None)
        if not callable(fetcher):
            continue
        for kwargs in (
            {"tmdbid": tmdb_id, "mtype": mtype},
            {"tmdbid": tmdb_id},
            {"mediaid": str(tmdb_id), "mtype": mtype},
        ):
            try:
                candidate = fetcher(**kwargs)
            except TypeError:
                continue
            except Exception:
                candidate = None
            if candidate:
                info = candidate
                break
        if info:
            break
    if info is None:
        return ""
    poster = None
    getter = getattr(info, "get_poster_image", None)
    if callable(getter):
        try:
            poster = getter()
        except Exception:
            poster = None
    if not poster:
        poster = _media_value(info, "poster_path", "poster", "poster_url")
    if not poster:
        return ""
    return _tmdb_image_url(poster, "w500")


def resolve_poster_url(
    tmdb_id: Any,
    media_type: str,
    *,
    resolver: Optional[Callable[[int, str], str]] = None,
) -> str:
    """按 tmdb_id 取海报 URL（进程内缓存）；取不到返回空串。"""
    try:
        key = int(tmdb_id or 0)
    except (TypeError, ValueError):
        return ""
    if key <= 0:
        return ""
    now = time.time()
    with _POSTER_CACHE_LOCK:
        if key in _POSTER_CACHE:
            return _POSTER_CACHE[key]
        miss_at = _POSTER_MISS_AT.get(key, 0.0)
        if miss_at and now - miss_at < _POSTER_MISS_TTL_SECONDS:
            return ""
    try:
        url = str((resolver or _default_poster_resolver)(key, media_type) or "").strip()
    except Exception:
        url = ""
    with _POSTER_CACHE_LOCK:
        if url:
            _POSTER_MISS_AT.pop(key, None)
            if len(_POSTER_CACHE) >= _POSTER_CACHE_MAX:
                _POSTER_CACHE.pop(next(iter(_POSTER_CACHE)))
            _POSTER_CACHE[key] = url
        else:
            _POSTER_MISS_AT[key] = now
    return url


def clear_poster_cache() -> None:
    """清空海报缓存（测试与插件重载用）。"""
    with _POSTER_CACHE_LOCK:
        _POSTER_CACHE.clear()
        _POSTER_MISS_AT.clear()


def _task_tmdb_id(task: Dict[str, Any]) -> Optional[int]:
    try:
        raw = task.get("tmdb_id")
    except AttributeError:
        return None
    if raw in (None, "", 0, "0"):
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _task_media(
    task: Dict[str, Any],
    *,
    poster_resolver: Optional[Callable[[int, str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """下载页卡片的 media（DownloadTaskMedia 口径）。

    海报取不到时 media 其它字段照常填（标题/季集不能跟着丢）；电影不带季集；
    多集时 episode 留空，让前端回退到 season_episode 的完整区间文案。
    """
    media_type = str(task.get("media_type") or "").strip()
    if media_type not in {"电视剧", "电影"}:
        return None
    episodes = _task_episodes(task)
    season: Optional[int] = None
    episode: Optional[int] = None
    if media_type == "电视剧":
        try:
            season = int(task.get("season") or 1)
        except (TypeError, ValueError):
            season = 1
        episode = episodes[0] if len(episodes) == 1 else None
    tmdb_id = _task_tmdb_id(task)
    poster = (
        resolve_poster_url(tmdb_id, media_type, resolver=poster_resolver)
        if tmdb_id
        else ""
    )
    media: Dict[str, Any] = {
        "type": media_type,
        "title": str(task.get("title") or "未命名订阅"),
        "season": season,
        "episode": episode,
        "poster": poster or None,
    }
    if tmdb_id:
        media["media_source"] = "themoviedb"
        media["media_id"] = str(tmdb_id)
    return media


def _display_name(task: Dict[str, Any]) -> str:
    """下载页的显示名称：标题 + 季集（有的话）。"""
    title = str(task.get("title") or "未命名订阅")
    season_episode = _season_episode_text(task)
    return f"{title} {season_episode}" if season_episode else title


def _task_path(task: Dict[str, Any]) -> Optional[Path]:
    """任务关联的保存路径；订阅同步任务没有单文件路径，返回 None。"""
    target_path = str(task.get("target_path") or "").strip()
    if not target_path:
        return None
    return Path(target_path)


def _default_torrent_cls() -> Any:
    """运行期从宿主导入 DownloaderTorrent（测试可显式传入桩类）。"""
    from app.schemas import DownloaderTorrent

    return DownloaderTorrent


def task_to_torrent(
    task: Dict[str, Any],
    *,
    downloader_name: str = DOWNLOADER_NAME,
    torrent_cls: Any = None,
    poster_resolver: Optional[Callable[[int, str], str]] = None,
) -> Any:
    """把一个运行时任务字典投影成 DownloaderTorrent。"""
    cls = torrent_cls or _default_torrent_cls()
    try:
        total = float(task.get("total") or 0)
    except (TypeError, ValueError):
        total = 0.0
    year = task.get("year")
    path = _task_path(task)
    return cls(
        downloader=downloader_name,
        hash=task_hash(str(task.get("id") or "")),
        title=str(task.get("title") or "未命名订阅"),
        name=_display_name(task),
        year=str(year) if year else None,
        season_episode=_season_episode_text(task),
        media=_task_media(task, poster_resolver=poster_resolver),
        path=path,
        size=total,
        progress=_task_progress(task),
        state=map_task_state(task.get("status")),
        dlspeed=format_speed(task.get("speed_bytes_per_second")),
        tags=None,
        save_path=str(path) if path else None,
        content_path=str(path) if path else None,
        category=None,
    )


def _default_file_cls() -> Any:
    """运行期从宿主导入 DownloaderFile（测试可显式传入桩类）。"""
    from app.schemas import DownloaderFile

    return DownloaderFile


def task_to_files(
    task: Dict[str, Any],
    *,
    file_cls: Any = None,
) -> List[Any]:
    """返回任务的文件清单与单文件进度；没有文件明细时返回 []。"""
    cls = file_cls or _default_file_cls()
    progress = _task_progress(task)
    if str(task.get("task_kind") or "") == "cross_transfer" or str(
        task.get("id") or ""
    ).startswith("cross:"):
        # 跨盘转存子任务本身就是一个文件。
        name = str(task.get("title") or "未知文件")
        try:
            size = int(task.get("total") or 0)
        except (TypeError, ValueError):
            size = 0
        return [
            cls(
                id=name,
                name=name,
                size=size or None,
                priority=0,
                progress=progress,
            )
        ]
    current_file = str(task.get("current_file") or "").strip()
    if not current_file:
        return []
    return [
        cls(
            id=current_file,
            name=current_file,
            size=None,
            priority=0,
            progress=progress,
        )
    ]


def ensure_downloader_entry(
    items: List[Dict[str, Any]],
    *,
    name: str = DOWNLOADER_NAME,
    type_name: str = DOWNLOADER_TYPE,
) -> List[Dict[str, Any]]:
    """往下载器配置列表里登记（或启用）本插件条目，返回新列表。"""
    entries = [dict(item) for item in items if isinstance(item, dict)]
    existing = next((item for item in entries if item.get("name") == name), None)
    if existing is not None:
        existing["enabled"] = True
        return entries
    entries.append({
        "name": name,
        "type": type_name,
        "default": False,
        "config": {},
        "enabled": True,
        "path_mapping": [],
    })
    return entries


def remove_downloader_entry(
    items: List[Dict[str, Any]],
    *,
    name: str = DOWNLOADER_NAME,
    type_name: str = DOWNLOADER_TYPE,
) -> List[Dict[str, Any]]:
    """从下载器配置列表里移除本插件条目（按名称 + type 双重匹配），返回新列表。"""
    return [
        dict(item)
        for item in items
        if isinstance(item, dict)
        and not (item.get("name") == name and item.get("type") == type_name)
    ]


def resolve_task_id(tasks: List[Dict[str, Any]], hash_value: str) -> str:
    """按下载页传回的 hash 反查运行时任务 id；找不到返回空串。"""
    wanted = str(hash_value or "").strip().lower()
    if not wanted:
        return ""
    for task in tasks:
        task_id = str(task.get("id") or "")
        if task_id and task_hash(task_id) == wanted:
            return task_id
    return ""


def _as_hash_set(hashs: Optional[Union[list[str], str]]) -> Optional[set]:
    if hashs is None:
        return None
    if isinstance(hashs, str):
        values = [hashs]
    else:
        values = list(hashs)
    return {str(item).strip().lower() for item in values if str(item).strip()}


def project_tasks(
    tasks: List[Dict[str, Any]],
    *,
    status: Any = None,
    hashs: Optional[Union[list[str], str]] = None,
    downloader: Optional[str] = None,
    own_name: str = DOWNLOADER_NAME,
    torrent_cls: Any = None,
    poster_resolver: Optional[Callable[[int, str], str]] = None,
) -> List[Any]:
    """按下载页的查询语义过滤并投影运行时任务。

    ``status`` 与网盘直链的 ``list_torrents`` 同口径：``all``（或空）返回全部，
    ``downloading`` 只返回进行中，``completed``/``transfer`` 只返回已完成
    （且进度 100%），``paused``/``stopped`` 只返回已暂停；``downloader``
    不是本插件时返回 []，避免在别的下载器标签下重复显示。
    """
    if downloader and downloader != own_name:
        return []
    query = str(getattr(status, "value", status) or "all").lower()
    wanted = _as_hash_set(hashs)
    results = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        if wanted is not None and task_hash(task_id) not in wanted:
            continue
        torrent = task_to_torrent(
            task,
            downloader_name=own_name,
            torrent_cls=torrent_cls,
            poster_resolver=poster_resolver,
        )
        state = str(getattr(torrent, "state", "") or "")
        if query in {"transfer", "completed"} and state != "completed":
            continue
        if query == "downloading" and state != "downloading":
            continue
        if query in {"paused", "stopped"} and state != "paused":
            continue
        results.append(torrent)
    if query in {"transfer", "completed"}:
        results = [
            item for item in results if float(getattr(item, "progress", 0) or 0) >= 100
        ]
    return results
