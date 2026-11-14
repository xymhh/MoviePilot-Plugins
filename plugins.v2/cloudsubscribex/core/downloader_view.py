"""把网盘订阅助手的运行时任务投影成 MoviePilot 下载器任务。

下载管理页按系统配置里的下载器名称分标签；本插件登记一个
「网盘订阅助手」下载器条目（见 :meth:`CloudSubscribeX._ensure_downloader_entry`），
实际任务由插件的 ``list_torrents`` 返回，本模块只做纯投影——
不碰任务执行、不碰历史表、不删文件。

任务来源是 :meth:`SyncRuntimeService._serialize_runtime_tasks`
返回的字典：订阅同步任务（id 如 ``subscribe:123`` / ``media:...``）
与无归属的跨盘转存子任务（id 如 ``cross:<hex>``）。
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

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


def _season_episode_text(task: Dict[str, Any]) -> Optional[str]:
    """季集展示文本，如 S01、S01E21-E22；电影返回 None。"""
    if str(task.get("media_type") or "") != "电视剧":
        return None
    try:
        season = int(task.get("season") or 1)
    except (TypeError, ValueError):
        season = 1
    episodes = [
        int(episode) for episode in (task.get("target_episodes") or [])
        if str(episode).isdigit()
    ]
    if not episodes:
        return f"S{season:02d}"
    if len(episodes) == 1:
        return f"S{season:02d}E{episodes[0]:02d}"
    return f"S{season:02d}E{min(episodes):02d}-E{max(episodes):02d}"


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
            task, downloader_name=own_name, torrent_cls=torrent_cls
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
