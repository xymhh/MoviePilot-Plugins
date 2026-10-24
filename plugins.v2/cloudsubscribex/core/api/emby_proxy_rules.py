"""Emby 302 反代的纯逻辑：URL 分类、PlaybackInfo 改写、STRM 解析与头处理。

这里刻意不碰网络、不碰插件状态，所有函数都可以离线单测。代理主流程只负责
把这里的判定结果落到 HTTP 请求/响应上，避免「规则」和「IO」混在一起后无法回归。
"""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

#: 请求分类结果。命中顺序见 :func:`classify_request`，与任务书 §3 完全一致。
PASSTHROUGH = "passthrough"
INDEX_REDIRECT = "index_redirect"
PLAYBACK_INFO = "playback_info"
STREAM_REDIRECT = "stream_redirect"
WEBSOCKET_REJECT = "websocket_reject"

#: 单次流式转发的块大小（任务书要求 64KB）。
STREAM_CHUNK_SIZE = 64 * 1024
#: STRM 文件内容缓存 TTL（mtime+size 变化会立即失效）。
STRM_CACHE_TTL = 30 * 60
#: Emby 返回的 Items 列表里，MediaSources 的 Path 才是我们要的本地 STRM 路径。
_PATH_FIELD = "Path"

#: PlaybackInfo 路径；允许客户端带 ``/emby`` 前缀（Emby 的 API 根随部署而变）。
_PLAYBACK_INFO_PATTERN = re.compile(
    r"(?:^|/)(?:emby/)?Items/[^/]+/PlaybackInfo/?$", re.IGNORECASE
)
#: 视频流路径。``stream.mkv`` 这种带扩展名的写法也要认。
_VIDEO_STREAM_PATTERN = re.compile(
    r"(?:^|/)(?:emby/)?Videos/[^/]+/(?:stream(?:\.[^/]*)?|original)/?$",
    re.IGNORECASE,
)
#: 音频流路径。``universal`` 是 Emby 客户端的音频通用入口。
_AUDIO_STREAM_PATTERN = re.compile(
    r"(?:^|/)(?:emby/)?Audio/[^/]+/(?:stream(?:\.[^/]*)?|universal)/?$",
    re.IGNORECASE,
)
#: 115 的 pickcode 固定 17 位字母数字。
_PICKCODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{17})(?![A-Za-z0-9])")
#: 直接播放请求里的视频/音频 ID。
_ITEM_ID_PATTERN = re.compile(
    r"/(?:Videos|Audio|Items)/([^/?#]+)", re.IGNORECASE
)
#: 会被视为「请求转码」的参数。命中时一律透传，不能 302 打断转码流。
_TRANSCODE_PARAMS = frozenset({
    "videocodec",
    "audiocodec",
    "maxstreamingbitrate",
    "transcodingmaxaudiosample",
    "transcodereasons",
    "transcode",
})
#: 逐跳头：不能原样转给上游或客户端。
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
})
#: Content-Encoding 被剔除后响应体可能是已解压内容，长度不能再沿用原值。
_RESPONSE_STRIP_HEADERS = _HOP_BY_HOP_HEADERS | {
    "content-encoding",
    "content-length",
}


def _query_values(query: str) -> Dict[str, List[str]]:
    """把 query 解析成小写键，便于大小写不敏感地查参数。"""
    try:
        parsed = parse_qs(str(query or ""), keep_blank_values=True)
    except ValueError:
        return {}
    return {str(key).lower(): values for key, values in parsed.items()}


def is_websocket(path: str, headers: Optional[Mapping[str, str]] = None) -> bool:
    """判断是否为 WebSocket 升级请求；第一版不代理 WS，直接拒绝。"""
    lowered_path = str(path or "").lower()
    if "embywebsocket" in lowered_path or lowered_path.endswith("/socket"):
        return True
    normalized = {
        str(key).lower(): str(value).lower()
        for key, value in (headers or {}).items()
    }
    return "websocket" in normalized.get("upgrade", "")


def classify_request(
        path: str,
        query: str = "",
        headers: Optional[Mapping[str, str]] = None,
) -> str:
    """按任务书 §3 的顺序给请求分流，命中即停。

    顺序不能换：``/`` 必须先于其它规则（否则会落到透传，首页打不开）；
    PlaybackInfo 必须先于流请求（它需要改写响应体，而不是 302）；
    ``/emby/http`` 是 MediaRelay 的假直链入口，必须单独认。
    """
    raw_path = str(path or "/")
    if is_websocket(raw_path, headers):
        return WEBSOCKET_REJECT
    if raw_path == "/":
        return INDEX_REDIRECT
    if _PLAYBACK_INFO_PATTERN.search(raw_path):
        return PLAYBACK_INFO
    if "/emby/http" in raw_path.lower() and is_fake_direct_stream(query):
        return STREAM_REDIRECT
    if _VIDEO_STREAM_PATTERN.search(raw_path):
        return STREAM_REDIRECT
    if _AUDIO_STREAM_PATTERN.search(raw_path):
        return STREAM_REDIRECT
    return PASSTHROUGH


def is_fake_direct_stream(query: str) -> bool:
    """``FakeDirectStream=true`` 是 PlaybackInfo 改写后客户端回访的标志。"""
    values = _query_values(query)
    return any(
        str(value).strip().lower() in {"true", "1"}
        for value in values.get("fakedirectstream", [])
    )


def is_transcode_request(query: str) -> bool:
    """请求里带转码参数时返回 True；这类请求必须原样交给 Emby。"""
    values = _query_values(query)
    for key, items in values.items():
        if key not in _TRANSCODE_PARAMS:
            continue
        if any(str(item).strip() for item in items):
            return True
    return False


def request_media_ids(path: str, query: str) -> Tuple[str, str]:
    """从播放请求里取 (ItemId, MediaSourceId)，没有的返回空串。"""
    values = _query_values(query)
    item_id = str((values.get("itemid") or [""])[0]).strip()
    media_source_id = str((values.get("mediasourceid") or [""])[0]).strip()
    if not item_id:
        match = _ITEM_ID_PATTERN.search(str(path or ""))
        if match:
            item_id = match.group(1)
    if not media_source_id and item_id:
        # 有的客户端只带 ItemId；Items?Ids= 也能反查到 MediaSources。
        media_source_id = item_id
    return item_id, media_source_id


def build_origin(scope: Mapping[str, Any], headers: Mapping[str, str]) -> str:
    """从请求推断客户端访问代理用的 scheme+host。"""
    normalized = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
    }
    scheme = (
        normalized.get("x-forwarded-proto", "").split(",")[0].strip()
        or str(scope.get("scheme") or "http")
    )
    host = (
        normalized.get("x-forwarded-host", "").split(",")[0].strip()
        or normalized.get("host", "")
    )
    if not host:
        server = scope.get("server") or ("", 0)
        host = str(server[0] or "")
        port = server[1] if len(server) > 1 else None
        if port and port not in (80, 443):
            host = f"{host}:{port}"
    return f"{scheme}://{host}".rstrip("/")


def rewrite_playback_info(
        payload: Any,
        origin: str,
        can_resolve: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Any:
    """把 STRM MediaSource 改写成经代理的 DirectStreamUrl。

    只改 ``Path`` 以 ``.strm`` 结尾的项：真实文件由 Emby 直出，改了反而可能
    把可直连的客户端逼到转码。``can_resolve`` 用来表达「网盘客户端是否可用」，
    不可用时整份 PlaybackInfo 保持原样（任务书 §4 的降级要求）。
    """
    if not isinstance(payload, dict):
        return payload
    sources = payload.get("MediaSources")
    if not isinstance(sources, list):
        return payload

    base = str(origin or "").rstrip("/")
    parent_item_id = str(payload.get("ItemId") or payload.get("Id") or "")
    for source in sources:
        if not isinstance(source, dict):
            continue
        path = str(source.get(_PATH_FIELD) or "")
        if not path.lower().endswith(".strm"):
            continue
        if can_resolve is not None and not can_resolve(source):
            continue
        item_id = str(source.get("ItemId") or parent_item_id or "")
        source_id = str(source.get("Id") or "")
        if not item_id or not source_id:
            continue
        source["DirectStreamUrl"] = (
            f"{base}/Videos/{item_id}/stream"
            f"?MediaSourceId={source_id}&Static=true&FakeDirectStream=true"
        )
        source["TranscodeReasons"] = []
        source["SupportsTranscoding"] = False
        source["SupportsDirectPlay"] = True
        source["SupportsDirectStream"] = True
        source["Protocol"] = "Http"
    return payload


def is_rewritable_playback_response(status_code: int, content: bytes) -> bool:
    """只有 200 + 合法 JSON 才改写；其余状态与非 JSON 一律原样透传。"""
    if int(status_code or 0) != 200 or not content:
        return False
    try:
        json.loads(content)
    except (ValueError, TypeError):
        return False
    return True


def extract_media_path(
        payload: Any,
        media_source_id: str = "",
        item_id: str = "",
) -> str:
    """从 Emby ``Items?Ids=`` / ``Items/{id}`` 响应里取出 MediaSource 的 Path。"""
    items: Iterable[Any]
    if isinstance(payload, dict):
        raw_items = payload.get("Items")
        items = raw_items if isinstance(raw_items, list) else [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        return ""

    fallback = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        sources = item.get("MediaSources")
        if not isinstance(sources, list):
            sources = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            path = str(source.get(_PATH_FIELD) or "")
            if not path:
                continue
            if media_source_id and str(source.get("Id") or "") == media_source_id:
                return path
            if not fallback:
                fallback = path
        direct_path = str(item.get(_PATH_FIELD) or "")
        if direct_path and not fallback:
            fallback = direct_path
        if item_id and str(item.get("Id") or "") == item_id and direct_path:
            return direct_path
    return fallback


@dataclass(frozen=True)
class StrmLink:
    """STRM 里解析出的播放目标：直链或 pickcode。"""

    kind: str
    value: str
    file_name: str = ""


def _file_name_from_url(url: str) -> str:
    """从 115 直链的 ``?/文件名`` 或路径末段里取文件名（取不到返回空）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    # 115 直链常见写法：https://cdn.../xxx?/电影.mkv
    query = parts.query or ""
    if query.startswith("/"):
        candidate = query[1:]
    else:
        candidate = parts.path.rsplit("/", 1)[-1]
    if "." not in candidate:
        return ""
    return candidate.rsplit("/", 1)[-1]


def _direct_link(url: str) -> bool:
    """是否是 115 CDN 直链；分享页/网页链接不能直接 302。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return "115cdn" in host or host == "115.com" or host.endswith(".115.com")


def parse_strm_content(text: str) -> Optional[StrmLink]:
    """解析 STRM 文本，返回直链或 pickcode；认不出返回 None。"""
    content = str(text or "").strip().lstrip("\ufeff").strip()
    if not content:
        return None
    first_line = content.splitlines()[0].strip()
    if first_line.startswith(("http://", "https://")):
        if _direct_link(first_line):
            return StrmLink("direct", first_line, _file_name_from_url(first_line))
        values = _query_values(urlsplit(first_line).query)
        for key in ("pickcode", "pick_code"):
            for value in values.get(key, []):
                if _PICKCODE_PATTERN.fullmatch(str(value).strip()):
                    return StrmLink(
                        "pickcode",
                        str(value).strip(),
                        _file_name_from_url(first_line),
                    )
        # 路径里的 /d/{pickcode} 或 /file/{pickcode} 同款写法。
        path = urlsplit(first_line).path
        match = _PICKCODE_PATTERN.search(path)
        if match:
            return StrmLink(
                "pickcode", match.group(1), _file_name_from_url(first_line)
            )
        return None
    if _PICKCODE_PATTERN.fullmatch(content):
        return StrmLink("pickcode", content)
    match = re.search(r"(?:pickcode|pick_code)=([A-Za-z0-9]{17})", content)
    if match:
        return StrmLink("pickcode", match.group(1))
    return None


class StrmFileCache:
    """按路径缓存 STRM 解析结果；mtime+size 变化或超过 TTL 才重读。"""

    def __init__(
            self,
            ttl: float = STRM_CACHE_TTL,
            reader: Optional[Callable[[str], str]] = None,
    ):
        self.ttl = float(ttl)
        self._reader = reader
        self._entries: Dict[str, Tuple[float, int, float, Optional[StrmLink]]] = {}

    @staticmethod
    def _default_reader(path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def get(self, path: str) -> Optional[StrmLink]:
        """读取并解析；文件不存在、为空或无法读取都返回 None。"""
        key = os.path.abspath(str(path or ""))
        if not key:
            return None
        try:
            stat = os.stat(key)
        except OSError:
            self._entries.pop(key, None)
            return None
        now = time.time()
        cached = self._entries.get(key)
        if cached:
            mtime, size, expires_at, link = cached
            if (
                    mtime == stat.st_mtime
                    and size == stat.st_size
                    and expires_at > now
            ):
                return link
        try:
            reader = self._reader or self._default_reader
            text = reader(key)
        except OSError:
            self._entries.pop(key, None)
            return None
        link = parse_strm_content(text)
        self._entries[key] = (
            stat.st_mtime,
            stat.st_size,
            now + self.ttl,
            link,
        )
        return link

    def clear(self) -> None:
        """清空缓存（测试与手动重启用）。"""
        self._entries.clear()


@dataclass(frozen=True)
class PathMapping:
    """Emby 侧路径前缀 → 本机挂载路径。"""

    source: str
    target: str


def parse_path_mappings(raw: Any) -> List[PathMapping]:
    """解析 ``Emby侧=本机侧`` 多行配置，长前缀优先。

    兼容仓库既有的 ``=`` 与 ``#`` 两种分隔符；``#`` 开头的整行仍是注释。
    """
    mappings: List[PathMapping] = []
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        separator = "=" if "=" in text else ("#" if "#" in text else "")
        if not separator:
            continue
        left, _, right = text.partition(separator)
        source = left.strip().replace("\\", "/").rstrip("/")
        target = right.strip().replace("\\", "/").rstrip("/")
        if not source or not target:
            continue
        mappings.append(PathMapping(source, target))
    mappings.sort(key=lambda item: len(item.source), reverse=True)
    return mappings


def map_path(emby_path: str, mappings: Any) -> str:
    """把 Emby 返回的 Path 翻译成本机路径；没有命中映射时原样返回。"""
    raw = str(emby_path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    for mapping in parse_path_mappings(mappings):
        if raw != mapping.source and not raw.startswith(f"{mapping.source}/"):
            continue
        relative = raw[len(mapping.source):].lstrip("/")
        return f"{mapping.target}/{relative}" if relative else mapping.target
    return raw


def filter_request_headers(
        headers: Mapping[str, str],
        upstream_host: str = "",
) -> Dict[str, str]:
    """构造转发给上游的请求头：去掉逐跳头、替换 Host、保留 Range。"""
    result: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        lowered = str(key).lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered in {"host", "content-length"}:
            continue
        result[str(key)] = str(value)
    # 只声明 httpx 能自动解压的编码：Emby 会无视 identity 强制压缩，
    # 若上游返回压缩体而我们剔除了 Content-Encoding，客户端将无法解析
    # （登录后 /Users、/Items 全部 JSON 乱码 → 表现为“登录失败”）。
    result["Accept-Encoding"] = "gzip, deflate"
    if upstream_host:
        result["Host"] = str(upstream_host)
    return result


def filter_response_headers(headers: Any) -> List[Tuple[str, str]]:
    """构造回传给客户端的响应头：剔除逐跳头与 Content-Encoding。

    返回 (key, value) 列表而不是字典：``Set-Cookie`` 这类头允许重复，
    走字典会被合并成逗号串，浏览器无法解析。
    """
    items = (
        headers.multi_items()
        if hasattr(headers, "multi_items")
        else list((headers or {}).items())
    )
    return [
        (str(key), str(value))
        for key, value in items
        if str(key).lower() not in _RESPONSE_STRIP_HEADERS
    ]


def allowed_client(user_agent: str, allowed_clients: str) -> bool:
    """客户端 UA 过滤：配置为空时全放行，否则只匹配任一子串。"""
    raw = str(allowed_clients or "").strip()
    if not raw:
        return True
    agent = str(user_agent or "").lower()
    return any(
        token.strip().lower() in agent
        for token in re.split(r"[,，]", raw)
        if token.strip()
    )


def encrypt_playback_payload(payload: Any) -> bytes:
    """序列化回写给客户端的 PlaybackInfo（紧凑 JSON，UTF-8）。"""
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
