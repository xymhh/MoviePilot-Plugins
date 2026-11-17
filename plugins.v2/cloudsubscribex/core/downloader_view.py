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
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import quote

from .episode_parse import EPISODE_TAG_PATTERN, parse_episodes_from_names

# 下载管理页的标签名与下载器配置里的 type（插件类名）。
DOWNLOADER_NAME = "网盘订阅助手"
DOWNLOADER_TYPE = "CloudSubscribeX"

# 运行时任务里视为“进行中”的状态；其余一律按已暂停展示。
# stopping 是正在取消的瞬时态（仍在 ACTIVE 里），不是用户暂停，
# 按“已暂停”画会给一张点“继续”也没用的卡，归进行中。
_DOWNLOADING_STATUSES = {
    "queued", "running", "downloading", "transferring",
    "postprocessing", "pending", "stopping",
}
_COMPLETED_STATUSES = {"completed", "success"}
# 终态失败不属于下载页：它们归历史记录。下载页的任务模型没有失败态，
# 一旦投过去就会被 map_task_state 按“已暂停”展示，进度停在失败那一刻，
# 与历史记录对不上；卡片上的“继续”又是死的（start_torrents 恒 False），
# 所以 project_tasks 直接把这类任务拦掉。
_TERMINAL_FAILED_STATUSES = {"failed", "canceled", "aborted", "error"}


# 搜索阶段不上下载页：同步一轮开始时每个订阅都会先建一张父任务卡
# （queued 0%“等待调度”→ running 10%“检查缺失内容”→ 搜索 45~65%
# “搜索缺失剧集”），而下载页的任务模型只有下载中/已暂停/已完成，
# 这些卡片全被画成“下载中”，实际上一个字节都还没动；搜完没找到
# 资源的还会留一张 100%“已完成”卡 10 分钟。父任务只有真正开始
# 传了才属于下载页：有活跃子任务（transfer_active，合并时打上）
# 或已传文件数 transferred > 0；其余的排队/搜索/搜完无结果态拦掉。
# 独立跨盘任务（id 为 cross:…）不受此限；downloading /
# transferring / postprocessing / stopped 的父任务也不受此限
# （它们到这一步时已有真实转存或后处理活动）。1.10.12。
_SEARCH_PHASE_STATUSES = {"queued", "running", "completed"}
_PARENT_TASK_PREFIXES = ("subscribe:", "media:")


def _is_search_phase_parent(task: Dict[str, Any]) -> bool:
    """订阅父任务是否还在搜索阶段（没开始传任何文件）。"""
    task_id = str(task.get("id") or "")
    if not task_id.startswith(_PARENT_TASK_PREFIXES):
        return False
    status = str(task.get("status") or "").strip().lower()
    if status not in _SEARCH_PHASE_STATUSES:
        return False
    if task.get("transfer_active"):
        return False
    try:
        transferred = int(task.get("transferred") or 0)
    except (TypeError, ValueError):
        transferred = 0
    return transferred <= 0


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
    # stopped（用户点的暂停）/ 未知：一律按已暂停展示。终态失败
    # （failed / canceled / aborted / error）不由这里处理——
    # project_tasks 会先把它们拦掉，不让它们上下载页。
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


# 下载页按阶段展示：下载阶段进度=已下载字节占比，上传阶段进度=已上传占比。
# 任务字典自带的 progress 是两段总进度（下载 0-50、上传 50-100），直接投
# 到下载页会让“下载完了”还显示 50%；而卡片本身就有 ↓/↑ 两路速度位
# （旧模板按 parseFloat(dlspeed/upspeed) > 0 分别点亮），按阶段分开投，
# 下载时 ↓ 亮、上传时 ↑ 亮，阶段一目了然。1.10.11。
_DOWNLOAD_PHASES = {"downloading", "streaming"}
_UPLOAD_PHASES = {"uploading"}
# 下载已完成、上传尚未开始的阶段：校验/秒传尝试/缓存命中，展示 100%。
_DOWNLOAD_DONE_PHASES = {"hashing", "rapid_upload", "cache_ready", "done"}


def display_phase(task: Dict[str, Any]) -> str:
    """下载页展示阶段：downloading / uploading / done / organizing / ""（无字节口径时）。

    organizing 是独立的一档（MP 整理中）：不塞进下载/上传/已完成三档，
    否则按字节算出的进度与速度会把它画成还在传。
    """
    phase = str(
        task.get("transfer_phase") or task.get("phase") or ""
    ).strip().lower()
    if phase == "organizing":
        return "organizing"
    if phase in _DOWNLOAD_PHASES:
        return "downloading"
    if phase in _UPLOAD_PHASES:
        return "uploading"
    if phase in _DOWNLOAD_DONE_PHASES:
        return "done"
    # 阶段码不可用时（父任务 phase 是文案）按字节推断。
    try:
        total = float(task.get("total") or 0)
        downloaded = float(task.get("downloaded_bytes") or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    if downloaded < total:
        return "downloading"
    return "uploading"


def display_progress(task: Dict[str, Any]) -> Optional[float]:
    """下载页展示进度：下载阶段看已下载占比，上传阶段看已上传占比。

    没有字节口径的任务（普通订阅父任务）返回 None，调用方走原有总进度。
    organizing 档没有有意义的百分比，返回 None，不伪造进度。
    """
    if display_phase(task) == "organizing":
        return None
    try:
        total = float(task.get("total") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    try:
        downloaded = float(task.get("downloaded_bytes") or 0)
        uploaded = float(task.get("uploaded_bytes") or 0)
    except (TypeError, ValueError):
        return None
    phase = display_phase(task)
    if phase == "downloading":
        # 没有字节字段的老格式字典（只有总进度）不按字节算，走总进度。
        if "downloaded_bytes" not in task:
            return None
        return max(0.0, min(100.0, downloaded * 100.0 / total))
    if phase == "uploading":
        if "uploaded_bytes" not in task:
            return None
        return max(0.0, min(100.0, uploaded * 100.0 / total))
    if phase == "done":
        return 100.0
    return None


def _phase_speed(task: Dict[str, Any], phase: str) -> Optional[str]:
    """按阶段取速度文案：下载阶段只给 ↓，上传阶段只给 ↑，其余不给。

    分路速度优先，没有分路字段时退回总速度（老任务字典只有 speed 字段）；
    另一路的速度即使有残值也不投——旧模板按有无点亮 ↑/↓，残值会亮错路。
    organizing 档不给任何一路的速度，除非登记表里有正在走的上传：
    有实时条目且 speed > 0 时给 ↑ 实时速度，否则仍返回 None。
    """
    if display_phase(task) == "organizing":
        if phase != "uploading":
            return None
        live = _live_upload_for_task(task)
        if not live:
            return None
        try:
            speed = float(live.get("speed") or 0)
        except (TypeError, ValueError):
            return None
        return format_speed(speed) if speed > 0 else None
    if display_phase(task) != phase:
        return None
    key = (
        "download_speed_bytes_per_second"
        if phase == "downloading"
        else "upload_speed_bytes_per_second"
    )
    value = task.get(key)
    if value is None:
        value = task.get("speed_bytes_per_second")
    return format_speed(value)


def _task_progress(task: Dict[str, Any]) -> float:
    """任务完成百分比（0-100）：有字节口径时按阶段展示，余下走总进度。

    后处理阶段仍走原有总进度/后处理进度（95% 待收尾），不按阶段算——
    子任务都传完了不等于收尾做完了，显示 100% 会误导。
    organizing 档同样只回落到任务自带进度，且上限 95%，绝不显示 100%；
    但登记表里有对应上传在走时，优先返回实时字节百分比（上限 99%），
    让整理中的大文件在下载页能看到真进度，而不是 95% 的假进度。
    """
    if display_phase(task) == "organizing":
        live = _live_upload_for_task(task)
        if live:
            try:
                live_total = float(live.get("total") or 0)
                live_uploaded = float(live.get("uploaded") or 0)
            except (TypeError, ValueError):
                live_total = 0.0
                live_uploaded = 0.0
            if live_total > 0:
                pct = live_uploaded * 100.0 / live_total
                if 0 < pct < 100:
                    return min(99.0, pct)
        try:
            progress = float(task.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        if not progress and str(task.get("status") or "") == "postprocessing":
            try:
                progress = float(task.get("postprocess_progress") or 0)
            except (TypeError, ValueError):
                progress = 0.0
        return max(0.0, min(95.0, progress))
    if str(task.get("status") or "") != "postprocessing":
        shown = display_progress(task)
        if shown is not None:
            return shown
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

    海报取不到时 media 其它字段照常填（标题/季集不能跟着丢）；电影不带季集。
    季和集都留空：MP 卡片模板先拼 media 里的季、集两个数字（有数字就
    显示裸数字，如 "1 9"），只有两者都空才回退到 season_episode 的标准
    写法（S01E09）；多集/仅季/电影同理，季集一律走 season_episode。
    """
    media_type = str(task.get("media_type") or "").strip()
    if media_type not in {"电视剧", "电影"}:
        return None
    tmdb_id = _task_tmdb_id(task)
    poster = (
        resolve_poster_url(tmdb_id, media_type, resolver=poster_resolver)
        if tmdb_id
        else ""
    )
    if not poster:
        # 实时查 TMDB 取不到时，用任务创建时带上的存量海报地址兜底
        # （历史记录的 image / 同步时 mediainfo 的海报）；没有也不
        # 影响标题与季集，只是不显示海报图。
        poster = str(task.get("poster") or "").strip()
    media: Dict[str, Any] = {
        "type": media_type,
        # 独立跨盘任务的 title 是文件名，媒体标题用 media_title（有的话），
        # 否则海报旁显示的就是文件名。
        "title": str(task.get("media_title") or task.get("title") or "未命名订阅"),
        # 季/集留空（见 docstring）：卡片模板据此回退到 season_episode。
        "season": None,
        "episode": None,
        "poster": poster or None,
    }
    if tmdb_id:
        media["media_source"] = "themoviedb"
        media["media_id"] = str(tmdb_id)
    return media


def _display_name(task: Dict[str, Any]) -> str:
    """下载页的显示名称：标题 + 季集（有的话）。

    独立跨盘任务的 title 是文件名，卡片标题用媒体标题（有的话），
    否则下载页上只剩一个文件名、认不出是哪部剧。
    """
    title = str(task.get("media_title") or task.get("title") or "未命名订阅")
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


_TMDB_TAG_PATTERN = re.compile(r"\{tmdb[=\-](\d+)\}")
_HASH_SUFFIX_PATTERN = re.compile(r"\s*\([0-9a-fA-F]{6,}\)\s*$")


def _parse_upload_media_title(file_name: str) -> str:
    """从上传文件名解析媒体标题。

    去扩展名、{tmdb-N} 标记、“ - S01E01 - …”后缀和“(hash)”后缀后
    的第一段；解析不到就用去扩展名的 stem。
    """
    raw = str(file_name or "").strip()
    stem = Path(raw).stem if raw else ""
    cleaned = _TMDB_TAG_PATTERN.sub("", stem)
    first = cleaned.split(" - ")[0].strip()
    first = _HASH_SUFFIX_PATTERN.sub("", first).strip()
    return first or stem


def _parse_upload_tmdb_id(file_name: str) -> Optional[int]:
    """从文件名 {tmdb-N} / {tmdb=N} 标记解析 tmdb_id；没有返回 None。"""
    match = _TMDB_TAG_PATTERN.search(str(file_name or ""))
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _format_left_time(total: float, uploaded: float, speed: float) -> Optional[str]:
    """剩余时间文案：>=60 秒按“剩余约X分Y秒”，否则“剩余约X秒”。

    没速度或已传完时返回 None（不瞎猜）。
    """
    try:
        remaining = float(total or 0) - float(uploaded or 0)
        rate = float(speed or 0)
    except (TypeError, ValueError):
        return None
    if rate <= 0 or remaining <= 0:
        return None
    seconds = int(remaining / rate)
    if seconds >= 60:
        return f"剩余约{seconds // 60}分{seconds % 60}秒"
    return f"剩余约{seconds}秒"


def synthetic_upload_tasks() -> List[Dict[str, Any]]:
    """把进行中的 115 上传投影成运行时任务字典（下载页任务卡）。

    MP 整理队列里的整理没有对应的插件任务卡，运行时同步任务又早已
    过期，所以上传进度只能从这里来：上传回调把字节进度记进进程内
    登记表，这里每条在传记录造一张卡（真实百分比 + ↑ 速度 +
    MB/MB + 剩余时间）。登记表模块每次调用都重新取——模块可能被
    测试重载，顶层 import 会绑到旧表。
    """
    try:
        from .upload_progress import snapshot_upload_progress
    except Exception:
        return []
    try:
        entries = snapshot_upload_progress()
    except Exception:
        return []
    tasks: List[Dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            key = str(entry.get("key") or "").strip()
            file_name = str(entry.get("file_name") or "").strip()
            total = float(entry.get("total") or 0)
            uploaded = float(entry.get("uploaded") or 0)
            speed = float(entry.get("speed") or 0)
        except (TypeError, ValueError):
            continue
        if not key or not file_name or total <= 0:
            continue
        media_title = _parse_upload_media_title(file_name)
        episode_match = EPISODE_TAG_PATTERN.search(file_name)
        task: Dict[str, Any] = {
            "id": (
                "upload:"
                + hashlib.sha1(f"{key}|{total}".encode("utf-8")).hexdigest()[:16]
            ),
            "title": (
                f"{file_name} · {uploaded / 1048576:.0f}/{total / 1048576:.0f}MB"
            ),
            "media_title": media_title,
            # 电视剧才进 media 投影（海报+季集）；电影/未知只显示名字，
            # media_type 非“电视剧/电影”时 _task_media 返回 None。
            "media_type": "电视剧" if episode_match else "",
            "status": "transferring",
            "transfer_phase": "uploading",
            "total": total,
            "uploaded_bytes": uploaded,
            "upload_speed_bytes_per_second": speed,
            "current_file": file_name,
            "task_kind": "upload",
        }
        if episode_match:
            # EPISODE_TAG_PATTERN 的季号组恒为数字，int 不会失败。
            task["season"] = int(episode_match.group(1))
        tmdb_id = _parse_upload_tmdb_id(file_name)
        if tmdb_id:
            task["tmdb_id"] = tmdb_id
        left_time = _format_left_time(total, uploaded, speed)
        if left_time:
            task["left_time"] = left_time
        tasks.append(task)
    return tasks


def _live_upload_for_task(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在上传登记表里找与任务对应的实时条目；找不到返回 None。

    匹配顺序：key 精确匹配 → basename 匹配 → file_name 匹配。
    key 是本地文件名，任务里的 current_file / transfer_file_names
    可能是完整路径，先按原值比，再按 basename 比。
    """
    try:
        from .upload_progress import snapshot_upload_progress
    except Exception:
        return None
    try:
        entries = snapshot_upload_progress()
    except Exception:
        return None
    if not entries:
        return None
    names = _task_file_names(task)
    if not names:
        return None
    basenames = [Path(name).name for name in names]
    for entry in entries:
        if entry.get("key") in names:
            return entry
    for entry in entries:
        if entry.get("key") in basenames:
            return entry
    for entry in entries:
        if entry.get("file_name") in names or entry.get("file_name") in basenames:
            return entry
    return None


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
        dlspeed=_phase_speed(task, "downloading"),
        upspeed=_phase_speed(task, "uploading"),
        left_time=task.get("left_time"),
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
        if (
            str(task.get("status") or "").strip().lower()
            in _TERMINAL_FAILED_STATUSES
        ):
            # 终态失败归历史记录：不进下载页，避免被画成一张
            # 进度停在失败那一刻的“已暂停”卡片。
            continue
        if _is_search_phase_parent(task):
            # 还在排队/搜索、或搜完无结果的订阅父任务：没传任何文件，
            # 不上下载页（搜索进度归插件主页的同步状态行）。
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
