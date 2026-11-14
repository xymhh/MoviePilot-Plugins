"""MoviePilot 原生搜索接入：把订阅助手的搜索结果交给 MP 搜索页。

链路（与网盘直链 QuarkChannel 同款，只换转存后端）：

1. 搜索：MP 的 ``SearchChain`` 调模块 ``search_torrents``，本模块用插件
   已有的 ``SearchHandler`` 按关键词查各搜索渠道，把候选登记成
   内存快照后映射成 ``TorrentInfo`` 返回；``enclosure`` 是本插件
   的 ``/torrent`` 合成种子地址，MP 点下载时会先拉这个种子。
2. 取种子：MP 请求 ``/torrent?sid=...&sig=...``，验签通过后按快照里的
   分享链接现场取文件清单，编码成合法种子返回。
3. 下载：MP 把种子字节交给模块 ``download``，本模块从种子 comment 里
   回读 sid，找到快照后复用 ``submit_platform_links`` 走插件自己
   的转存流程（不另写转存），返回的 task_hash 与下载页
   ``list_torrents`` 的投影对上号，任务就显示在下载管理
   「网盘订阅助手」标签里。

本模块是纯逻辑层：只依赖标准库 + 宿主的 TorrentInfo / MediaType /
MediaInfo / Response，以及插件已有的 ``downloader_view`` 与
``search.types`` 的类型名表；插件本体（``__init__.py``）只做薄委托。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    from app.domain.context import TorrentInfo
except ImportError:  # pragma: no cover - 宿主旧版放在 app.schemas
    from app.schemas import TorrentInfo

try:
    from app.schemas import MediaInfo
except ImportError:  # pragma: no cover - 测试桩环境
    MediaInfo = None  # type: ignore[assignment]

try:
    from app.schemas.types import MediaType
except ImportError:  # pragma: no cover - 测试桩环境
    MediaType = None  # type: ignore[assignment]

try:
    from app.log import logger
except ImportError:  # pragma: no cover - 测试桩环境
    logger = logging.getLogger(__name__)

try:
    from fastapi.responses import Response
except ImportError:  # pragma: no cover - 单测环境没有 fastapi
    class Response:  # type: ignore[no-redef]
        """fastapi Response 的最小替身：只保留测试与 MP 侧用到的字段."""

        def __init__(self, content: bytes = b"", status_code: int = 200,
                     media_type: Optional[str] = None,
                     headers: Optional[Dict[str, str]] = None):
            self.body = bytes(content or b"")
            self.content = self.body
            self.status_code = int(status_code)
            self.media_type = media_type
            self.headers = dict(headers or {})

from .bencode import build_torrent
from .downloader_view import DOWNLOADER_NAME, task_hash


def _make_response(content: bytes, status_code: int = 200,
                   media_type: Optional[str] = None,
                   headers: Optional[Dict[str, str]] = None) -> Any:
    """构造种子接口的 Response。

    线上用 fastapi 的 Response（支持 media_type 关键字）；单测环境有
    的测试文件会装只有 (status_code, headers, content) 的最小 Response
    桩，关键字不兼容时退回该签名，并把用例要读的 body / media_type
    字段补上。
    """
    try:
        return Response(
            content=content, status_code=status_code,
            media_type=media_type, headers=headers,
        )
    except TypeError:
        resp = Response(
            content=content, status_code=status_code, headers=headers,
        )
        if not hasattr(resp, "body"):
            try:
                resp.body = bytes(content or b"")
            except Exception as error:  # noqa: BLE001 - 桩 Response 补字段失败不影响返回
                logger.debug(f"补 Response.body 失败：{error}", exc_info=True)
        if not hasattr(resp, "media_type"):
            try:
                resp.media_type = media_type
            except Exception as error:  # noqa: BLE001 - 桩 Response 补字段失败不影响返回
                logger.debug(f"补 Response.media_type 失败：{error}", exc_info=True)
        return resp

try:
    from ..search.types import TYPE_NAMES as _TYPE_NAMES
    from ..search.types import normalize_resource_type as _normalize_resource_type

    if not isinstance(_TYPE_NAMES, dict):  # 测试桩可能把模块替成 MagicMock
        raise ImportError("TYPE_NAMES 不是 dict")
    TYPE_NAMES = _TYPE_NAMES
    normalize_resource_type = _normalize_resource_type
except Exception:  # pragma: no cover - search.types 不可用时的兜底
    TYPE_NAMES = {
        "115": "115网盘", "123": "123网盘", "quark": "夸克网盘",
        "alipan": "阿里云盘", "baidu": "百度网盘", "uc": "UC网盘",
        "tianyi": "天翼云盘", "guangya": "光鸭网盘", "yun139": "移动云盘",
        "xunlei": "迅雷云盘", "pikpak": "PikPak",
        "magnet": "磁力链接", "ed2k": "电驴链接", "torrent": "BT种子",
        "cloud": "网盘路径", "share": "网盘分享", "other": "其他来源",
        "unknown": "未知",
    }

    def normalize_resource_type(value: Any) -> str:  # type: ignore[misc]
        return str(value or "").strip().lower()


# 合成种子 comment 里的标记：CLOUDSUBSCRIBEX|<sid>|<资源类型>
MARKER = "CLOUDSUBSCRIBEX"
# 快照 TTL：与网盘直链的缓存同量级；过期后 /torrent 与 download 都按
# 「请重新搜索」处理，不再静默取旧分享。
SNAPSHOT_TTL_SECONDS = 3600
# 每个搜索渠道最多取的候选数（search_sources 的 result_limit 是单源口径）；
# 跨渠道合并后总上限另见 MAX_RESULTS。
RESULT_LIMIT_PER_SOURCE = 5
MAX_RESULTS = 8
# /torrent 现场取文件清单的最长等待（网盘直链 _lazy_snapshot 同款 8 秒）。
SNAPSHOT_TIMEOUT_SECONDS = 8
# download 提交后、推不出任务 id 时轮询运行时任务的上限与间隔。
TASK_POLL_TIMEOUT_SECONDS = 5.0
TASK_POLL_INTERVAL_SECONDS = 0.3
# MP 内部地址默认值：与 MP 同进程时 localhost 即可。
DEFAULT_MP_BASE_URL = "http://127.0.0.1:3000"
# 签名密钥的配置键：首次生成后写回配置，不硬编码。
SIGN_SECRET_CONFIG_KEY = "mp_search_sign_secret"

# 注册表创建锁：插件实例上的登记表是惰性创建的，多线程下只建一份。
_REGISTRY_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 快照登记
# ---------------------------------------------------------------------------

@dataclass
class ShareSnapshot:
    """一次搜索候选的登记信息：download 与 /torrent 都按 sid 回读它。"""

    sid: str
    share_url: str
    passcode: str = ""
    resource_type: str = ""
    title: str = ""
    # submit_platform_links 用的 "movie" / "tv"（不是 MediaType 的中文值）。
    media_type: str = "tv"
    season: Optional[int] = None
    tmdb_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)

    @property
    def folder_name(self) -> str:
        """种子名：去掉路径非法字符，供 MoviePilot 当下载目录名。"""
        name = (self.title or self.sid).strip() or self.sid
        for char in '\\/:*?"<>|':
            name = name.replace(char, " ")
        return " ".join(name.split())[:120] or self.sid


class SnapshotRegistry:
    """分享快照的内存登记表：线程安全，过期条目读取时即丢弃。"""

    def __init__(self, ttl_seconds: float = SNAPSHOT_TTL_SECONDS):
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        self._items: Dict[str, ShareSnapshot] = {}

    def put(self, snapshot: ShareSnapshot) -> None:
        with self._lock:
            snapshot.created_at = time.time()
            self._items[snapshot.sid] = snapshot

    def get(self, sid: str) -> Optional[ShareSnapshot]:
        with self._lock:
            snapshot = self._items.get(str(sid or ""))
            if snapshot is None:
                return None
            if time.time() - (snapshot.created_at or 0) >= self._ttl:
                self._items.pop(str(sid or ""), None)
                return None
            return snapshot

    def drop(self, sid: str) -> None:
        with self._lock:
            self._items.pop(str(sid or ""), None)


def _snapshot_registry(plugin: Any) -> SnapshotRegistry:
    """取插件实例上的登记表（没有就建一个，挂在实例上不污染模块全局）。"""
    obj_dict = getattr(plugin, "__dict__", None)
    registry = (
        obj_dict.get("_mp_search_registry")
        if isinstance(obj_dict, dict) else None
    )
    if isinstance(registry, SnapshotRegistry):
        return registry
    with _REGISTRY_LOCK:
        obj_dict = getattr(plugin, "__dict__", None)
        registry = (
            obj_dict.get("_mp_search_registry")
            if isinstance(obj_dict, dict) else None
        )
        if isinstance(registry, SnapshotRegistry):
            return registry
        registry = SnapshotRegistry()
        if isinstance(obj_dict, dict):
            obj_dict["_mp_search_registry"] = registry
        return registry


def normalize_share_url(url: Any) -> str:
    """去重与生成 sid 用的归一化分享链接：只去空白与末尾斜杠。"""
    return str(url or "").strip().rstrip("/")


def build_sid(share_url: str) -> str:
    """用分享地址生成稳定 id（网盘直链同款：md5 前 16 位）。"""
    return hashlib.md5(share_url.strip().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 签名（网盘直链 torrents.py 同款：HMAC-SHA256 截 32 位 hex）
# ---------------------------------------------------------------------------

def sign(sid: str, secret: str) -> str:
    """对分享 id 生成 HMAC 签名。"""
    return hmac.new(
        str(secret or "").encode("utf-8"),
        str(sid or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def verify(sid: str, signature: str, secret: str) -> bool:
    """校验签名，避免匿名端点被外部枚举。"""
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign(sid, secret), str(signature))


# ---------------------------------------------------------------------------
# 合成种子组装（网盘直链 torrents.py 的思路，marker 换成本插件）
# ---------------------------------------------------------------------------

def render_torrent(snapshot: ShareSnapshot, files: List[Dict[str, Any]]) -> bytes:
    """把分享快照 + 文件清单渲染为种子字节串。

    :param snapshot: 搜索阶段登记的快照
    :param files: ``preview_resource_files`` 的条目（含 name / size /
        ``_relative_path``）
    :return: 种子字节串
    :raises ValueError: 没有可下载的文件
    """
    entries: List[Dict[str, Any]] = []
    for item in files or []:
        if not isinstance(item, dict):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        raw_path = str(
            item.get("_relative_path")
            or item.get("relative_path")
            or item.get("name")
            or item.get("file_name")
            or "file"
        )
        parts = [
            piece for piece in raw_path.replace("\\", "/").split("/")
            if piece not in ("", ".")
        ]
        if not parts:
            parts = ["file"]
        entries.append({"length": size, "path": parts})
    if not entries:
        raise ValueError("分享内没有可下载的文件")
    return build_torrent(
        name=snapshot.folder_name,
        files=entries,
        comment=f"{MARKER}|{snapshot.sid}|{snapshot.resource_type or 'share'}",
    )


def parse_marker(content: bytes) -> Optional[Tuple[str, str]]:
    """从种子内容中取回 (sid, resource_type)。

    comment 是 ``<长度>:CLOUDSUBSCRIBEX|<sid>|<资源类型>``，因此先定位标记、
    再回读长度前缀，避免把后续 bencode 内容当成资源类型
    （网盘直链 parse_marker 的写法，直接照抄思路）。

    :param content: 种子字节串
    :return: 命中本插件标记时返回 sid 与资源类型，否则 None
    """
    if not content:
        return None
    head = content[:8192]
    marker = f"{MARKER}|".encode("utf-8")
    index = head.find(marker)
    if index <= 0:
        return None
    cursor = index - 1
    if cursor < 0 or head[cursor:cursor + 1] != b":":
        return None
    digits_end = cursor
    cursor -= 1
    while cursor >= 0 and head[cursor:cursor + 1].isdigit():
        cursor -= 1
    try:
        length = int(head[cursor + 1:digits_end])
    except ValueError:
        return None
    comment = head[index:index + length]
    parts = comment.split(b"|")
    if len(parts) < 3:
        return None
    sid = parts[1].decode("utf-8", "ignore")
    resource_type = parts[2].decode("utf-8", "ignore")
    return (sid, resource_type or "share")


# ---------------------------------------------------------------------------
# 标题处理（_clean_title 从网盘直链完整复制；整包标题只做轻量规范化）
# ---------------------------------------------------------------------------

def clean_title(title: str) -> str:
    """去掉分享标题里的水印、广告词与会误导识别的片段。

    去掉：方头括号/方括号里的广告段（公众号、微信、资源、更新、高清等）、
    「更新至第 N 集」「第 N 集」「EP12」、SxxExx 及其区间写法、
    「全集/完结」等状态词。保留剧名、年份、分辨率等识别信息。
    """
    text = str(title or "")
    # 括号里的广告段：方头括号内含公众号、[更新至12集]、(夸克分享)
    text = re.sub(
        r"[\u3010\[][^\u3011\]]*(?:公众号|微信|资源|分享|夸克|更新|高清|中字|独家|首发|搬运|转载|关注|群|频道|网盘)[^\u3011\]]*[\u3011\]]",
        " ", text,
    )
    text = re.sub(
        r"\(\s*(?:公众号|微信|资源|分享|夸克|更新|高清|中字|独家|首发)[^)]*\)",
        " ", text,
    )
    # 更新至第 N 集 / 更新 N 集
    text = re.sub(r"更新至?\s*(?:第\s*)?\d{1,4}\s*集", " ", text)
    text = re.sub(r"更新\s*\d{1,4}", " ", text)
    # 第 X-Y 集区间与单集：第12集、第1-2集
    text = re.sub(r"第\s*\d{1,4}\s*[-~]\s*\d{1,4}\s*集", " ", text)
    text = re.sub(r"第\s*\d{1,4}\s*集", " ", text)
    # EP12 这类单集标记
    text = re.sub(r"(?:EP|Ep|ep)\s*\d{1,4}(?!\d)", " ", text)
    # SxxExx 及区间：S01E01、S01E01-E12、S01E01-12
    text = re.sub(
        r"[Ss]\d{1,2}[._ ]?[Ee]\d{1,4}(?:\s*[-~]\s*(?:[Ss]\d{1,2}[._ ]?)?[Ee]?\d{1,4})?",
        " ", text,
    )
    # 公众号/微信号后跟的账号名
    text = re.sub(r"(?:公众号|微信号|VX|vx)\s*[:：]?\s*\S+", " ", text)
    # 裸露的画质/字幕广告词：高清中字、高清、中字、蓝光、超清
    text = re.sub(r"(?:高清中字|高清|中字|蓝光|超清)", " ", text)
    # 全集/完结/连载中等状态词（集数范围已表达该信息）
    text = re.sub(r"(?:全集|完结|连载中|更新中|已完结)", " ", text)
    # 残留的成对空括号（内容保留，如方头括号里 2024 的年份）
    text = re.sub(r"[\u3010\u3011\[\]()\uff08\uff09]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_\u00b7\u2022| ")


def pack_title(raw_title: str) -> str:
    """搜索阶段的整包标题：只给干净剧名，不拼 SxxEyy。

    搜索阶段没有快照、不知道集数范围，拼上 SxxEyy 反而会误导 MP 把整包
    识别成单集；季/集展开是后续单集展开再做的事。
    """
    name = clean_title(raw_title)
    if not name or name.startswith(("http://", "https://")):
        return "分享"
    return name


_SEASON_RE = re.compile(
    r"(?:[Ss](?:eason)?\s*0*(\d{1,3})|第\s*0*(\d{1,3})\s*季)"
)


def season_of_title(title: str) -> Optional[int]:
    """从标题里解析季号（Sxx / 第 N 季 / Season N），解析不出返回 None。"""
    match = _SEASON_RE.search(str(title or ""))
    if not match:
        return None
    try:
        return int(match.group(1) or match.group(2))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 插件配置读写（mp_base_url 与签名密钥）
# ---------------------------------------------------------------------------

def _plugin_config(plugin: Any) -> Dict[str, Any]:
    """读插件当前配置：优先内存里的已应用配置，其次走 get_config()。"""
    config = getattr(plugin, "_applied_config", None)
    if isinstance(config, dict) and config:
        return config
    getter = getattr(plugin, "get_config", None)
    if callable(getter):
        try:
            config = getter()
        except Exception:
            config = None
        if isinstance(config, dict):
            return config
    return {}


def get_mp_base_url(plugin: Any) -> str:
    """MP 自己拉合成种子用的内部地址；未配置时默认 localhost。"""
    config = _plugin_config(plugin)
    base = str((config or {}).get("mp_base_url") or "").strip().rstrip("/")
    return base or DEFAULT_MP_BASE_URL


def get_sign_secret(plugin: Any) -> str:
    """取签名密钥；不存在时生成一个并写回配置（不硬编码密钥）。

    生成的密钥同时缓存在插件实例上，配置写回失败也不影响本次进程内
    的签名/验签（快照只有 1 小时 TTL，下次启动会重新生成）。
    """
    try:
        cached = getattr(plugin, "__dict__", {}).get("_mp_search_sign_secret")
    except Exception as error:  # noqa: BLE001 - 缓存读不到就按缺失处理
        logger.debug(f"读取搜索签名密钥缓存失败：{error}", exc_info=True)
        cached = None
    if cached:
        return str(cached)
    config = _plugin_config(plugin)
    secret = str((config or {}).get(SIGN_SECRET_CONFIG_KEY) or "").strip()
    if not secret:
        secret = secrets.token_hex(32)
        merged = dict(config or {})
        merged[SIGN_SECRET_CONFIG_KEY] = secret
        updater = getattr(plugin, "update_config", None)
        result = None
        if callable(updater):
            try:
                result = updater(merged)
            except Exception as error:  # noqa: BLE001 - 写回失败不阻断搜索
                logger.warning(f"搜索签名密钥写回配置失败：{error}")
        if result is not False:
            try:
                plugin._applied_config = merged
            except Exception as error:  # noqa: BLE001 - 内存同步失败不影响本次进程
                logger.debug(f"搜索签名密钥内存同步失败：{error}", exc_info=True)
    obj_dict = getattr(plugin, "__dict__", None)
    if isinstance(obj_dict, dict):
        obj_dict["_mp_search_sign_secret"] = secret
    return secret


def _torrent_url(plugin: Any, sid: str) -> str:
    """合成种子的下载地址（带签名，/torrent 是匿名端点）。"""
    base = get_mp_base_url(plugin)
    return (
        f"{base}/api/v1/plugin/CloudSubscribeX/torrent"
        f"?sid={quote(str(sid))}&sig={sign(str(sid), get_sign_secret(plugin))}"
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _plugin_enabled(plugin: Any) -> bool:
    """插件是否启用：优先实例上的 _enabled，拿不到时走 get_state()。"""
    enabled = getattr(plugin, "_enabled", None)
    if isinstance(enabled, bool):
        return enabled
    getter = getattr(plugin, "get_state", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:
            return True
    return True


def _tv_member() -> Any:
    """MediaType.TV；宿主枚举不可用时返回 None（调用方按 None 处理）。"""
    if MediaType is None:
        return None
    return getattr(MediaType, "TV", None)


def _media_type_str(mtype: Any) -> str:
    """mtype → submit_platform_links 用的 "movie" / "tv"。

    mtype 为 None 时按 TV 走：标题-only 的关键词搜索没有类型信息，
    TV 是源集合最广的一侧——get_enabled_sources 只在 MOVIE 时剔除
    mikan/animegarden 这类动漫源，TV 则全留；盘搜的
    ``_media_type_matches`` 在 TV 下只过滤掉明确标电影的候选，无标签
    的候选照常通过；快照的 media_type 只是转存时的默认值，真正建任务
    前 submit_platform_links 还会按分享内容重新识别一次。
    """
    if mtype is None:
        return "tv"
    name = str(getattr(mtype, "name", "") or "").upper()
    if name == "MOVIE":
        return "movie"
    if name == "TV":
        return "tv"
    value = str(getattr(mtype, "value", "") or "").strip().lower()
    if value in ("movie", "电影"):
        return "movie"
    if value in ("tv", "电视剧"):
        return "tv"
    text = str(mtype).strip().lower()
    if text in ("movie", "电影"):
        return "movie"
    return "tv"


def _category_of(mtype: Any) -> Optional[str]:
    """TorrentInfo.category：mtype 的值；mtype 为 None 时置 None。

    None 表示「类型未知」，让 MP 自己按标题识别，不要在搜索阶段
    把没有类型信息的关键词搜索硬标成电视剧。
    """
    if mtype is None:
        return None
    value = getattr(mtype, "value", None)
    if value:
        return str(value)
    if isinstance(mtype, str):
        return mtype
    return None


def _make_mediainfo(keyword: str, mtype: Any) -> Any:
    """构造搜索用的 MediaInfo（标题-only 关键词，无 tmdb/年份）。"""
    if MediaInfo is None:
        return None
    try:
        return MediaInfo(title=keyword, type=mtype)
    except Exception:
        try:
            info = MediaInfo()
            info.title = keyword
            info.type = mtype
            return info
        except Exception:
            return None


def _channel_display_names(plugin: Any, handler: Any,
                           sources: List[str]) -> Dict[str, str]:
    """各搜索源的展示名：优先 handler 的 get_source_display_name。"""
    names: Dict[str, str] = {}
    getter = getattr(handler, "get_source_display_name", None)
    for source in sources:
        if callable(getter):
            try:
                names[source] = str(getter(source) or source)
                continue
            except Exception as error:  # noqa: BLE001 - 单源展示名失败即用 key
                logger.debug(f"读取搜索源展示名失败：{error}", exc_info=True)
        names[source] = source
    return names


def _channel_priorities(plugin: Any, sources: List[str]) -> Dict[str, int]:
    """各搜索源的渠道 priority（渠道库 priority 倒序，第一条优先）。

    渠道库读不到时退回搜索顺序本身：排在前面的源 priority 更高，
    保证结果排序与 get_enabled_sources 的顺序一致。
    """
    priorities: Dict[str, int] = {}
    try:
        store_getter = getattr(plugin, "_channel_store", None)
        store = store_getter() if callable(store_getter) else None
        channels = store.list() if store is not None else []
        for channel in channels or []:
            if not isinstance(channel, dict):
                continue
            if channel.get("type") == "tg_channel":
                continue
            key = str(channel.get("type") or "").strip().lower()
            if not key or key in priorities:
                continue
            try:
                priorities[key] = int(channel.get("priority") or 0)
            except (TypeError, ValueError):
                priorities[key] = 0
    except Exception as error:  # noqa: BLE001 - 渠道库异常不阻断搜索
        logger.debug(f"读取渠道 priority 失败：{error}", exc_info=True)
    for index, source in enumerate(sources):
        priorities.setdefault(source, len(sources) - index)
    return priorities


# ---------------------------------------------------------------------------
# 搜索入口
# ---------------------------------------------------------------------------

def search_torrents(plugin: Any, site: Any = None, keyword: Optional[str] = None,
                    mtype: Any = None, cat: Any = None,
                    page: int = 0) -> List[Any]:
    """MP 搜索页入口：把订阅助手的搜索候选映射成 TorrentInfo。

    只用 :meth:`get_enabled_sources` 返回的搜索类渠道（已按 priority
    排好，tg_channel 不在其中）；单渠道失败不影响其他渠道；结果按渠道
    优先级合并、按规范化分享 URL 去重，总数上限 8 条。
    """
    if not _plugin_enabled(plugin):
        return []
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    try:
        page = int(page or 0)
    except (TypeError, ValueError):
        page = 0
    if page > 0:
        # 第一版不做翻页：搜索结果只有一页 8 条，MP 翻页时直接返回空。
        return []
    handler = getattr(plugin, "_search_handler", None)
    if handler is None:
        return []
    # mtype 为 None 时按 TV 走（理由见 _media_type_str），category 置 None。
    resolved_mtype = mtype if mtype is not None else _tv_member()
    try:
        sources = handler.get_enabled_sources(resolved_mtype)
    except Exception as error:  # noqa: BLE001 - 渠道读取失败即无结果
        logger.debug(f"读取已启用搜索渠道失败：{error}", exc_info=True)
        return []
    sources = [str(item).strip().lower() for item in (sources or []) if str(item).strip()]
    if not sources:
        return []
    mediainfo = _make_mediainfo(keyword, resolved_mtype)
    if mediainfo is None:
        return []
    try:
        grouped = handler.search_sources(
            sources=sources,
            mediainfo=mediainfo,
            media_type=resolved_mtype,
            season=None,
            apply_platform_rules=False,
            force_refresh=False,
            result_limit=RESULT_LIMIT_PER_SOURCE,
        )
    except Exception as error:  # noqa: BLE001 - 搜索整体失败即无结果
        logger.debug(f"网盘订阅助手搜索失败：{error}", exc_info=True)
        return []
    media_type_str = _media_type_str(mtype)
    display_names = _channel_display_names(plugin, handler, sources)
    priorities = _channel_priorities(plugin, sources)
    registry = _snapshot_registry(plugin)
    pubdate = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    category = _category_of(mtype)
    results: List[Any] = []
    seen_urls: set = set()
    for source in sources:
        items = (grouped or {}).get(source) or []
        for item in items:
            if len(results) >= MAX_RESULTS:
                break
            if not isinstance(item, dict):
                continue
            share_url = normalize_share_url(
                item.get("url") or item.get("share_url") or ""
            )
            if not share_url or share_url in seen_urls:
                continue
            seen_urls.add(share_url)
            raw_title = str(item.get("title") or "")
            title = pack_title(raw_title)
            resource_type = normalize_resource_type(
                item.get("resource_type") or item.get("pan_type") or ""
            )
            snapshot = ShareSnapshot(
                sid=build_sid(share_url),
                share_url=share_url,
                passcode=str(item.get("password") or ""),
                resource_type=resource_type,
                title=title,
                media_type=media_type_str,
                season=season_of_title(raw_title),
            )
            registry.put(snapshot)
            try:
                size = float(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            channel_name = display_names.get(source, source)
            type_name = TYPE_NAMES.get(resource_type) or resource_type or "网盘分享"
            results.append(TorrentInfo(
                site=None,
                site_name=channel_name,
                title=title,
                description=f"网盘订阅助手 · {type_name} · {channel_name}",
                enclosure=_torrent_url(plugin, snapshot.sid),
                page_url=share_url,
                size=max(0.0, size),
                seeders=0,
                pubdate=pubdate,
                pri_order=int(priorities.get(source, 0)),
                category=category,
            ))
        if len(results) >= MAX_RESULTS:
            break
    return results


# ---------------------------------------------------------------------------
# /torrent 接口
# ---------------------------------------------------------------------------

def _preview_files(plugin: Any, share_url: str) -> List[Dict[str, Any]]:
    """调 preview_resource_files 取文件清单，8 秒超时，失败/空清单返回 []。"""
    handler = getattr(plugin, "_sync_handler", None)
    preview = getattr(handler, "preview_resource_files", None)
    if not callable(preview):
        preview = getattr(plugin, "preview_resource_files", None)
    if not callable(preview):
        return []
    executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="cloudsubscribex-mp-search",
    )
    try:
        future = executor.submit(preview, share_url)
        files = future.result(timeout=SNAPSHOT_TIMEOUT_SECONDS)
        return list(files or [])
    except FuturesTimeoutError:
        logger.warning(f"现场读取分享超时：{share_url}")
        return []
    except Exception as error:  # noqa: BLE001 - 取清单失败按 404 处理
        logger.warning(f"现场读取分享失败：{share_url}，{type(error).__name__}")
        return []
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def api_torrent(plugin: Any, sid: str = "", sig: str = "") -> Any:
    """按分享 id 输出合成种子：验签 403、快照缺失 404。

    MP 拉种子时不带登录态，本接口注册时需 ``allow_anonymous=True``，
    安全靠 sid + HMAC 签名（与网盘直链 /torrent 同款）。
    """
    sid = str(sid or "").strip()
    sig = str(sig or "").strip()
    try:
        secret = get_sign_secret(plugin)
    except Exception:
        return _make_response(b"forbidden", status_code=403)
    if not verify(sid, sig, secret):
        return _make_response(b"forbidden", status_code=403)
    snapshot = _snapshot_registry(plugin).get(sid)
    if snapshot is None:
        return _make_response(b"not found", status_code=404)
    files = _preview_files(plugin, snapshot.share_url)
    if not files:
        return _make_response(b"not found", status_code=404)
    try:
        payload = render_torrent(snapshot, files)
    except ValueError as error:
        logger.warning(f"生成合成种子失败：{error}")
        return _make_response(b"invalid share", status_code=404)
    return _make_response(
        payload,
        status_code=200,
        media_type="application/x-bittorrent",
        headers={"Content-Disposition": f'attachment; filename="{sid}.torrent"'},
    )


# ---------------------------------------------------------------------------
# 下载入口
# ---------------------------------------------------------------------------

def _normalize_match_text(value: Any) -> str:
    """标题匹配用的归一化：去空白与分隔符、转小写。"""
    return re.sub(r"[\s\-_\u00b7\u2022|/]+", "", str(value or "")).lower()


def _task_matches_snapshot(task: Dict[str, Any], snapshot: ShareSnapshot,
                           snapshot_title: str) -> bool:
    """运行时任务是否与本次提交的快照匹配：标题相含 + 季号一致（有季号时）。"""
    task_title = _normalize_match_text(task.get("title") or "")
    if not task_title or not snapshot_title:
        return False
    if snapshot_title not in task_title and task_title not in snapshot_title:
        return False
    if snapshot.season is not None:
        try:
            task_season = int(task.get("season") or 1)
        except (TypeError, ValueError):
            task_season = 1
        if task_season != int(snapshot.season):
            return False
    return True


def _task_id_from_media(media: Dict[str, Any],
                        snapshot: ShareSnapshot) -> str:
    """按 submit 返回的 media 推导瞬态任务 id。

    口径与 ``subscription_budget_key`` 一致：tmdb 优先、无 tmdb 时用
    标题；电视剧 ``media:tmdb_<id>_S<季>``，电影 ``media:tmdb_<id>_movie``。
    """
    try:
        tmdb_id = int(media.get("tmdb_id") or 0)
    except (TypeError, ValueError):
        tmdb_id = 0
    media_type = str(
        media.get("media_type") or snapshot.media_type or "tv"
    ).strip().lower()
    seasons = media.get("seasons") or []
    season = None
    try:
        if seasons:
            season = int(list(seasons)[0])
    except (TypeError, ValueError):
        season = None
    if season is None:
        season = snapshot.season or 1
    title = str(media.get("title") or snapshot.title or "").strip()
    identity = f"tmdb_{tmdb_id}" if tmdb_id else title
    if not identity:
        return ""
    if media_type == "movie":
        return f"media:{identity}_movie"
    return f"media:{identity}_S{season}"


def _poll_new_task(plugin: Any, snapshot: ShareSnapshot,
                   before_ids: set) -> str:
    """兜底：短暂轮询运行时任务，找标题/季匹配的新任务 id。"""
    serializer = getattr(plugin, "_serialize_runtime_tasks", None)
    if not callable(serializer):
        return ""
    snapshot_title = _normalize_match_text(snapshot.title)
    deadline = time.monotonic() + TASK_POLL_TIMEOUT_SECONDS
    while True:
        try:
            tasks = serializer() or []
        except Exception:
            tasks = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or "")
            if not task_id or task_id in before_ids:
                continue
            if _task_matches_snapshot(task, snapshot, snapshot_title):
                return task_id
        if time.monotonic() >= deadline:
            return ""
        time.sleep(TASK_POLL_INTERVAL_SECONDS)


def _resolve_task_id(plugin: Any, result: Dict[str, Any],
                     snapshot: ShareSnapshot, before_ids: set) -> str:
    """从 submit 结果推导 download 要返回的任务 id。

    ① 匹配到已有订阅：``data.matched_subscribe_id`` → ``subscribe:<id>``；
    ② 无订阅：``data.media``（即 media_target，tmdb_id/media_type/
    title/seasons）→ ``media:tmdb_<id>_S<季>`` 或 ``media:..._movie``；
    ③ 都推不出：轮询运行时任务找标题/季匹配的新任务。
    """
    data = result.get("data") or {}
    try:
        matched_id = int(data.get("matched_subscribe_id") or 0)
    except (TypeError, ValueError):
        matched_id = 0
    if matched_id > 0:
        return f"subscribe:{matched_id}"
    media = data.get("media") or {}
    if isinstance(media, dict) and isinstance(media.get("media_target"), dict):
        media = media["media_target"]
    if isinstance(media, dict) and media:
        task_id = _task_id_from_media(media, snapshot)
        if task_id:
            return task_id
    return _poll_new_task(plugin, snapshot, before_ids)


def download(plugin: Any, content: Any, download_dir: Any,
             cookie: Any = None, episodes: Any = None, category: Any = None,
             label: Any = None, downloader: Any = None,
             custom_words: Any = None) -> Any:
    """MP 下载入口：只接管本插件合成种子的任务，其余返回 None。

    返回 ``(下载器名, task_hash, 布局, 错误原因)``：第四元组非空表示失败；
    返回 None 表示「不是我的任务」，交给下一个 provider。
    episodes 第一版忽略：整包转存，后续单集展开再用。
    """
    if not _plugin_enabled(plugin):
        return None
    marker = parse_marker(content if isinstance(content, bytes) else b"")
    if not marker:
        return None
    sid, _resource_type = marker
    snapshot = _snapshot_registry(plugin).get(sid)
    if snapshot is None:
        return None, None, None, "分享快照已过期，请重新搜索"
    submit = getattr(plugin, "submit_platform_links", None)
    if not callable(submit):
        return None, None, None, "转存入口未就绪，请稍后重试"
    # 兜底轮询要排除提交前就已存在的任务，先记一份快照。
    before_ids: set = set()
    serializer = getattr(plugin, "_serialize_runtime_tasks", None)
    if callable(serializer):
        try:
            before_ids = {
                str(task.get("id") or "")
                for task in (serializer() or [])
                if isinstance(task, dict)
            }
        except Exception:
            before_ids = set()
    try:
        result = submit(
            subscribe_id=0,
            resource_links=[snapshot.share_url],
            wait=False,
            title=snapshot.title,
            media_type=snapshot.media_type,
            season=snapshot.season,
            tmdb_id=snapshot.tmdb_id or 0,
        )
    except Exception as error:  # noqa: BLE001 - 提交异常按失败返回
        logger.warning(f"提交转存失败：{error}", exc_info=True)
        return None, None, None, f"提交转存失败：{error}"
    if not isinstance(result, dict) or not result.get("success"):
        message = ""
        if isinstance(result, dict):
            message = str(result.get("message") or "")
        return None, None, None, message or "提交转存失败"
    task_id = _resolve_task_id(plugin, result, snapshot, before_ids)
    if not task_id:
        return (
            None, None, None,
            "转存任务已提交，但未能定位运行时任务，请在下载管理页查看",
        )
    return DOWNLOADER_NAME, task_hash(task_id), "", ""
