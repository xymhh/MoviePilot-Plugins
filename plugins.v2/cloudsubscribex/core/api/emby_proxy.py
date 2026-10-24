"""CloudSubscribeX 内置的 Emby 302 反向代理。

独立端口起一个 uvicorn 线程：播放请求命中 STRM 时 302 到 115 CDN，其余请求
透传给 Emby。换链复用 :mod:`core.api.redirect` 的 6 小时缓存与限流，不另写一套
115 直链逻辑。代理失败一律回落透传，最坏等于没装。
"""

import asyncio
import json
import os
import threading
import time
from typing import Any, Dict, Mapping, Optional

from app.log import logger

from .emby_proxy_rules import (
    INDEX_REDIRECT,
    PLAYBACK_INFO,
    STREAM_CHUNK_SIZE,
    STREAM_REDIRECT,
    StrmFileCache,
    allowed_client,
    build_origin,
    classify_request,
    encrypt_playback_payload,
    extract_media_path,
    filter_request_headers,
    filter_response_headers,
    is_fake_direct_stream,
    is_rewritable_playback_response,
    is_transcode_request,
    map_path,
    request_media_ids,
    rewrite_playback_info,
)

#: 代理默认监听端口，可在插件设置里改。
DEFAULT_PORT = 3010
#: Emby Items 路径缓存：LRU 2048、TTL 1 小时。
_PATH_CACHE_MAX = 2048
_PATH_CACHE_TTL = 3600
#: uvicorn 优雅退出等待上限（秒）。
STOP_TIMEOUT = 3.0


def _as_bool(value: Any, default: bool = False) -> bool:
    """兼容设置页可能传来的字符串布尔值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _TTLCache:
    """带 LRU 上限与 TTL 的极简缓存（单事件循环内使用，无需加锁）。"""

    def __init__(self, max_entries: int, ttl: float):
        self.max_entries = int(max_entries)
        self.ttl = float(ttl)
        self._entries: Dict[str, Any] = {}
        self._expires: Dict[str, float] = {}

    def get(self, key: str) -> Any:
        if not key:
            return None
        expires_at = self._expires.get(key)
        if expires_at is None:
            return None
        if expires_at <= time.time():
            self.pop(key)
            return None
        # 移到末尾表示最近使用。
        value = self._entries.pop(key)
        self._entries[key] = value
        return value

    def put(self, key: str, value: Any) -> None:
        if not key:
            return
        self.pop(key)
        while len(self._entries) >= self.max_entries:
            oldest = next(iter(self._entries), None)
            if oldest is None:
                break
            self.pop(oldest)
        self._entries[key] = value
        self._expires[key] = time.time() + self.ttl

    def pop(self, key: str) -> None:
        self._entries.pop(key, None)
        self._expires.pop(key, None)


class EmbyProxyStats:
    """代理统计；写操作可能来自 uvicorn 线程与测试线程。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = {"hit": 0, "miss": 0, "fallback": 0, "error": 0}

    def increment(self, key: str, amount: int = 1) -> None:
        if key not in self._values:
            return
        with self._lock:
            self._values[key] += int(amount)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._values)


class EmbyProxyService:
    """代理的启停、配置读取与 ASGI 请求处理。"""

    def __init__(self, owner: Any):
        self._owner = owner
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._start_error = ""
        self.stats = EmbyProxyStats()
        self._path_cache = _TTLCache(_PATH_CACHE_MAX, _PATH_CACHE_TTL)
        self._strm_cache = StrmFileCache()

    # ------------------------------------------------------------------ 配置
    def _get(self, key: str, default: Any) -> Any:
        return getattr(self._owner, f"_{key}", default)

    def _enabled(self) -> bool:
        return _as_bool(self._get("emby_proxy_enabled", False))

    def _port(self) -> int:
        return _as_int(self._get("emby_proxy_port", DEFAULT_PORT), DEFAULT_PORT)

    def _upstream_base(self) -> str:
        value = str(self._get("emby_proxy_upstream", "") or "").strip()
        if not value:
            value = str(self._get("media_server_host", "") or "").strip()
        if not value:
            # 1.7.44：插件没填时回落 MP 里已配置的 Emby/Jellyfin 地址，
            # 避免只开开关就被判定「未配置上游」而全部 502。
            value = self._media_server_fallback("host")
        if not value:
            return ""
        if not value.startswith(("http://", "https://")):
            value = f"http://{value}"
        return value.rstrip("/")

    @staticmethod
    def _media_server_fallback(field: str) -> str:
        """从 MP 系统设置里已配置的 Emby/Jellyfin 取 host / apikey。

        媒体服务器在 MP 里存成 ``{"name","type","enabled","config":{...}}``，
        ``MediaServerHelper.get_configs()`` 并不保证暴露扁平 host 字段，因此优先
        直接读系统设置，helper 只作二级兜底。
        """
        config_key = {"host": "host", "api_key": "apikey"}.get(field, field)
        try:
            from app.db.systemconfig_oper import SystemConfigOper
            from app.schemas.types import SystemConfigKey

            servers = SystemConfigOper().get(SystemConfigKey.MediaServers) or []
            for item in servers:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").lower() not in ("emby", "jellyfin"):
                    continue
                value = str((item.get("config") or {}).get(config_key) or "").strip()
                if value:
                    return value
        except Exception as error:  # noqa: BLE001
            logger.debug(f"读取 MP 媒体服务器设置失败：{error}")
        try:
            try:
                from app.helper.mediaserver import MediaServerHelper
            except ImportError:  # MP v3 新路径
                from app.application.mediaserver import MediaServerHelper
            for config in (MediaServerHelper().get_configs() or {}).values():
                if str(getattr(config, "type", "")).lower() not in ("emby", "jellyfin"):
                    continue
                value = str(getattr(config, field, "") or "").strip()
                if value:
                    return value
        except Exception as error:  # noqa: BLE001
            logger.debug(f"读取 MP 媒体服务器配置失败：{error}")
        return ""

    def _api_key(self) -> str:
        value = str(self._get("media_server_api_key", "") or "").strip()
        if value:
            return value
        return self._media_server_fallback("api_key")

    def _path_mappings(self) -> str:
        return str(self._get("media_server_path_mappings", "") or "")

    def _allowed_clients(self) -> str:
        return str(self._get("emby_proxy_allowed_clients", "") or "")

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """启动 uvicorn 子线程；端口占用等异常只记录，不向插件主流程抛。"""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._start_error = ""
            self._thread = threading.Thread(
                target=self._run,
                name="cloudsubscribe-emby-proxy",
                daemon=True,
            )
            self._thread.start()
        self._wait_started()
        return True

    def _wait_started(self, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            server = self._server
            if server is not None and getattr(server, "started", False):
                return
            thread = self._thread
            if thread is None or not thread.is_alive():
                return
            time.sleep(0.05)

    def _run(self) -> None:
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            import uvicorn

            config = uvicorn.Config(
                app=EmbyProxyApp(self),
                host="0.0.0.0",
                port=self._port(),
                log_level="warning",
                access_log=False,
                lifespan="off",
                timeout_graceful_shutdown=2,
            )
            server = uvicorn.Server(config)
            # 线程里不接管进程信号，避免和 MoviePilot 主事件循环抢。
            server.install_signal_handlers = lambda: None
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._server = server
            self._loop = loop
            logger.info(
                f"Emby 302 反代启动：0.0.0.0:{self._port()} -> "
                f"{self._upstream_base() or '未配置上游'}"
            )
            loop.run_until_complete(server.serve())
        except Exception as error:  # noqa: BLE001 - 代理失败不能影响插件主流程
            self._start_error = str(error)
            logger.error(f"Emby 302 反代启动失败：{error}")
        finally:
            self._server = None
            self._loop = None
            if loop is not None:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """通知 uvicorn 退出并等待端口释放。"""
        with self._lock:
            server = self._server
            thread = self._thread
            if server is not None:
                server.should_exit = True
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
            if thread is not None and not thread.is_alive():
                self._thread = None
            if self._thread is None:
                self._server = None
                self._loop = None

    def running(self) -> bool:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        server = self._server
        return bool(server is not None and getattr(server, "started", False))

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled(),
            "running": self.running(),
            "port": self._port(),
            "upstream": self._upstream_base(),
            "stats": self.stats.snapshot(),
        }

    def restart(self) -> Dict[str, Any]:
        self.stop()
        if self._enabled():
            self.start()
        return self.status()

    # ------------------------------------------------------------------ 换链
    def _link_available(self) -> bool:
        """网盘客户端可用才改写 PlaybackInfo / 302，否则整条链路降级透传。"""
        provider = getattr(self._owner, "_cloud_drive", None)
        if provider is None:
            return False
        try:
            from .. import CloudDriveCapability

            provider.require(CloudDriveCapability.FILE_DOWNLOAD)
            return True
        except Exception:  # noqa: BLE001 - 能力缺失按不可用处理
            return False

    async def _resolve_media_path(
            self, item_id: str, media_source_id: str
    ) -> str:
        """MediaSourceId/ItemId → Emby 记录的 Path；结果按 id 缓存 1 小时。"""
        cache_key = media_source_id or item_id
        cached = self._path_cache.get(cache_key)
        if cached:
            return str(cached)
        if not cache_key:
            return ""

        base = self._upstream_base()
        if not base:
            return ""
        from urllib.parse import quote

        url = f"{base}/emby/Items?Ids={quote(cache_key)}&Fields=Path"
        api_key = self._api_key()
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-Emby-Token"] = api_key
            url = f"{url}&api_key={quote(api_key)}"
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as error:  # noqa: BLE001 - 反查失败按未命中处理
            logger.debug(f"Emby 反代：反查媒体路径失败 id={cache_key}（{error}）")
            return ""
        path = extract_media_path(payload, media_source_id, item_id)
        if path:
            self._path_cache.put(cache_key, path)
        return path

    async def _resolve_strm_url(
            self, emby_path: str, fallback_name: str = ""
    ) -> str:
        """Path → 本地 STRM → 直链；每一步失败都返回空串触发透传。"""
        local_path = map_path(emby_path, self._path_mappings())
        if not local_path.lower().endswith(".strm"):
            return ""
        link = self._strm_cache.get(local_path)
        if link is None:
            return ""
        if link.kind == "direct":
            return link.value
        if link.kind != "pickcode":
            return ""
        file_name = link.file_name or fallback_name or os.path.basename(local_path)
        try:
            from .redirect import RedirectApi

            api = RedirectApi(self._owner)
            url, error_code = await asyncio.to_thread(
                api._resolve_link, link.value, file_name
            )
        except Exception as error:  # noqa: BLE001 - 换链失败必须回落
            logger.debug(f"Emby 反代：换链异常 pickcode={link.value}（{error}）")
            return ""
        if error_code or not url:
            logger.debug(
                f"Emby 反代：换链失败 pickcode={link.value}，错误码={error_code}"
            )
            return ""
        return str(url)

    async def _try_redirect(self, path: str, query: str) -> str:
        item_id, media_source_id = request_media_ids(path, query)
        emby_path = await self._resolve_media_path(item_id, media_source_id)
        if not emby_path:
            self.stats.increment("miss")
            return ""
        url = await self._resolve_strm_url(
            emby_path, fallback_name=f"{media_source_id or item_id}.mkv"
        )
        if not url:
            self.stats.increment("fallback")
            return ""
        self.stats.increment("hit")
        return url

    # ------------------------------------------------------------------ ASGI
    async def handle_http(self, scope: Mapping[str, Any], receive, send) -> None:
        path = str(scope.get("path") or "/")
        query = bytes(scope.get("query_string") or b"").decode("utf-8", "replace")
        headers = {
            bytes(key).decode("latin-1").lower(): bytes(value).decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        action = classify_request(path, query, headers)

        if action == INDEX_REDIRECT:
            await _send_redirect(send, "/web/index.html")
            return

        if action == STREAM_REDIRECT:
            if not is_fake_direct_stream(query) and is_transcode_request(query):
                # 带转码参数的请求必须交给 Emby，302 会把转码流打断。
                await self._proxy(scope, receive, send)
                return
            if not allowed_client(
                    headers.get("user-agent", ""), self._allowed_clients()
            ):
                await self._proxy(scope, receive, send)
                return
            url = await self._try_redirect(path, query)
            if url:
                logger.debug(f"Emby 反代：302 {path} -> 115 CDN")
                await _send_redirect(send, url)
                return
            await self._proxy(scope, receive, send)
            return

        if action == PLAYBACK_INFO:
            await self._proxy_playback_info(scope, receive, send, headers)
            return

        # PASSTHROUGH 以及 WebSocket（第一版不代理，交给 _proxy 返回 400）。
        if action == "websocket_reject":
            # TODO(ws): 后续用 websockets 或 aiohttp 代理 /embywebsocket，
            # 客户端会自动回落 HTTP 轮询，不影响播放。
            logger.debug(f"Emby 反代：拒绝 WebSocket {path}")
            await _send_status(send, 400, b"websocket not supported")
            return
        await self._proxy(scope, receive, send)

    # ------------------------------------------------------------- WebSocket
    async def handle_websocket(self, scope, receive, send) -> None:
        """把 Emby 的 WebSocket 双向转发到上游。

        Emby Web 与客户端登录后会连 ``/embywebsocket`` 收会话通知，不代理会
        表现为「登录后连接断开」或界面停转。上游连不上就直接关闭，REST 不受影响。
        """
        base = self._upstream_base()
        if not base:
            await send({"type": "websocket.close", "code": 1011})
            return
        path = str(scope.get("path") or "/")
        query = bytes(scope.get("query_string") or b"").decode("latin-1")
        url = f"{base.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        skip = {
            "host", "connection", "upgrade", "sec-websocket-key",
            "sec-websocket-version", "sec-websocket-extensions",
            "sec-websocket-accept", "origin",
        }
        headers = {
            bytes(key).decode("latin-1"): bytes(value).decode("latin-1")
            for key, value in scope.get("headers") or []
            if bytes(key).decode("latin-1").lower() not in skip
        }
        try:
            import websockets  # type: ignore
        except Exception:
            logger.warning("Emby 反代：缺少 websockets 库，无法代理 WebSocket")
            await send({"type": "websocket.close", "code": 1011})
            return
        try:
            connection = await self._ws_connect(websockets, url, headers)
        except Exception as exc:
            self.stats.increment("error")
            logger.warning(f"Emby 反代：WebSocket 连接上游失败 {exc}")
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept"})

        async def pump_client_to_upstream() -> None:
            while True:
                message = await receive()
                if message.get("type") == "websocket.disconnect":
                    return
                text = message.get("text")
                data = message.get("bytes")
                if text is not None:
                    await connection.send(text)
                elif data is not None:
                    await connection.send(data)
                else:
                    return

        async def pump_upstream_to_client() -> None:
            async for payload in connection:
                if isinstance(payload, str):
                    await send({"type": "websocket.send", "text": payload})
                else:
                    await send({"type": "websocket.send", "bytes": payload})

        try:
            await asyncio.gather(
                pump_client_to_upstream(), pump_upstream_to_client()
            )
        except Exception:
            pass
        finally:
            try:
                await connection.close()
            except Exception:
                pass
            try:
                await send({"type": "websocket.close", "code": 1000})
            except Exception:
                pass

    @staticmethod
    async def _ws_connect(websockets_module: Any, url: str, headers: Dict[str, str]):
        """兼容 websockets 新旧版本的头参数名（additional_headers / extra_headers）。"""
        common = {
            "open_timeout": 10,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": None,
        }
        try:
            return await websockets_module.connect(
                url, additional_headers=headers, **common
            )
        except TypeError:
            return await websockets_module.connect(
                url, extra_headers=headers, **common
            )

    # ------------------------------------------------------------------ 转发
    async def _proxy(self, scope: Mapping[str, Any], receive, send) -> None:
        """原样流式透传，支持 Range 与 HEAD。"""
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "/")
        query = bytes(scope.get("query_string") or b"").decode("latin-1")
        headers = {
            bytes(key).decode("latin-1"): bytes(value).decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        base = self._upstream_base()
        if not base:
            self.stats.increment("error")
            await _send_status(send, 502, b"emby upstream not configured")
            return
        url = f"{base}{path}"
        if query:
            url = f"{url}?{query}"
        body = await _read_body(receive)
        request_headers = filter_request_headers(headers, _netloc(base))
        started = False
        try:
            import httpx

            async with httpx.AsyncClient(
                    timeout=60.0, follow_redirects=False
            ) as client:
                request = client.build_request(
                    method, url, headers=request_headers, content=body or None
                )
                response = await client.send(request, stream=True)
                encoding = str(
                    response.headers.get("content-encoding", "")
                ).strip().lower()
                # 上游返回压缩体时必须用 aiter_bytes 交给 httpx 解压（我们把
                # Content-Encoding 剔除了）。206 分段内容不能解压，只能原样透传并
                # 保留原始编码头。
                keep_encoding = bool(encoding) and encoding != "identity" and (
                    response.status_code == 206
                )
                response_headers = filter_response_headers(response.headers)
                if keep_encoding:
                    response_headers.append(("content-encoding", encoding))
                await _send_response_start(
                    send, response.status_code, response_headers
                )
                started = True
                if encoding and encoding != "identity" and not keep_encoding:
                    stream_iter = response.aiter_bytes(
                        chunk_size=STREAM_CHUNK_SIZE
                    )
                else:
                    stream_iter = response.aiter_raw(
                        chunk_size=STREAM_CHUNK_SIZE
                    )
                async for chunk in stream_iter:
                    if chunk:
                        await _send_body(send, chunk, more_body=True)
                await _send_body(send, b"", more_body=False)
                await response.aclose()
        except Exception as error:  # noqa: BLE001 - 上游异常统一回落 502
            self.stats.increment("error")
            logger.warning(f"Emby 反代：透传失败 {method} {path}（{error}）")
            if not started:
                await _send_status(send, 502, b"emby upstream error")
            else:
                await _send_body(send, b"", more_body=False)

    async def _proxy_playback_info(
            self,
            scope: Mapping[str, Any],
            receive,
            send,
            headers: Mapping[str, str],
    ) -> None:
        """转发 PlaybackInfo；仅在能换链时改写 STRM 的 DirectStreamUrl。"""
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "/")
        query = bytes(scope.get("query_string") or b"").decode("latin-1")
        base = self._upstream_base()
        if not base:
            self.stats.increment("error")
            await _send_status(send, 502, b"emby upstream not configured")
            return
        url = f"{base}{path}"
        if query:
            url = f"{url}?{query}"
        body = await _read_body(receive)
        request_headers = filter_request_headers(headers, _netloc(base))
        try:
            import httpx

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.request(
                    method, url, headers=request_headers, content=body or None
                )
        except Exception as error:  # noqa: BLE001
            self.stats.increment("error")
            logger.warning(f"Emby 反代：PlaybackInfo 转发失败 {path}（{error}）")
            await _send_status(send, 502, b"emby upstream error")
            return

        content = response.content
        response_headers = filter_response_headers(response.headers)
        if (
                self._link_available()
                and is_rewritable_playback_response(
                    response.status_code, content
                )
        ):
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                payload = None
            sources = (
                payload.get("MediaSources") if isinstance(payload, dict) else None
            )
            has_strm = isinstance(sources, list) and any(
                isinstance(source, dict)
                and str(source.get("Path") or "").lower().endswith(".strm")
                for source in sources
            )
            if has_strm:
                origin = build_origin(scope, headers)
                rewritten = rewrite_playback_info(payload, origin)
                content = encrypt_playback_payload(rewritten)
                response_headers["Content-Type"] = "application/json; charset=utf-8"
        await _send_response_start(
            send, response.status_code, response_headers
        )
        await _send_body(send, content, more_body=False)


class EmbyProxyApp:
    """最小 ASGI 入口；把请求交给 :class:`EmbyProxyService`。"""

    def __init__(self, service: EmbyProxyService):
        self._service = service

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            await self._service.handle_http(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await self._service.handle_websocket(scope, receive, send)
            return
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message.get("type") == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return


async def _read_body(receive) -> bytes:
    """读完 ASGI 请求体；HEAD/GET 通常立即得到空 body。"""
    chunks = []
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            break
        chunks.append(message.get("body", b"") or b"")
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _netloc(url: str) -> str:
    """取 URL 的 host:port，用作转发请求的 Host 头。"""
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).netloc
    except ValueError:
        return ""


async def _send_response_start(
        send, status: int, headers: Any
) -> None:
    """发送响应头；headers 既支持 (k, v) 列表也支持字典。"""
    items = (
        headers.items() if hasattr(headers, "items") else headers
    )
    encoded = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in items
    ]
    await send({
        "type": "http.response.start",
        "status": int(status),
        "headers": encoded,
    })


async def _send_body(send, body: bytes, more_body: bool) -> None:
    await send({
        "type": "http.response.body",
        "body": body or b"",
        "more_body": bool(more_body),
    })


async def _send_status(send, status: int, body: bytes = b"") -> None:
    await _send_response_start(
        send, status, {"Content-Type": "text/plain; charset=utf-8"}
    )
    await _send_body(send, body, more_body=False)


async def _send_redirect(send, url: str) -> None:
    await _send_response_start(send, 302, {
        "Location": url,
        "Cache-Control": "no-store",
        "Content-Length": "0",
    })
    await _send_body(send, b"", more_body=False)


# ---------------------------------------------------------------------- 模块入口
def get_emby_proxy(owner: Any) -> EmbyProxyService:
    """取得（或创建）该插件实例对应的代理服务。"""
    service = owner.__dict__.get("_emby_proxy_service")
    if service is None:
        service = EmbyProxyService(owner)
        owner.__dict__["_emby_proxy_service"] = service
    return service


def start_emby_proxy(owner: Any) -> Dict[str, Any]:
    """按配置启动；关闭时顺手停掉已有线程，保证开关立即生效。"""
    service = get_emby_proxy(owner)
    try:
        if service._enabled():
            service.start()
        else:
            service.stop()
    except Exception as error:  # noqa: BLE001 - 不能影响插件主流程
        logger.error(f"Emby 302 反代启停异常：{error}")
    return service.status()


def stop_emby_proxy(owner: Any) -> None:
    """停止代理；插件卸载/重载时调用。"""
    service = owner.__dict__.get("_emby_proxy_service")
    if service is None:
        return
    try:
        service.stop()
    except Exception as error:  # noqa: BLE001
        logger.warning(f"Emby 302 反代停止异常：{error}")


def emby_proxy_status(owner: Any) -> Dict[str, Any]:
    """管理端点：返回开关、运行态、端口、上游与统计。"""
    return get_emby_proxy(owner).status()


def restart_emby_proxy(owner: Any) -> Dict[str, Any]:
    """管理端点：停再起；关闭状态下只停不起。"""
    service = get_emby_proxy(owner)
    try:
        return service.restart()
    except Exception as error:  # noqa: BLE001
        logger.error(f"Emby 302 反代重启失败：{error}")
        return service.status()
