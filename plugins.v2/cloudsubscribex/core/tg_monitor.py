"""TG 频道监控：用用户自己的 TG 会话读取频道消息，抽取网盘分享链接。

设计要点：
- 只用 Telethon 的**用户会话**（api_id + api_hash + StringSession），
  因此私有频道（账号已加入）和公开频道都能读，Bot API 做不到这一点。
- 扫描是增量的：每个频道记住最后处理的消息 id，只读新消息；首次按
  ``scan_limit`` 回溯。
- 抓到资源只入库，不自动转存：页面点「转存」时复用插件既有的手动转存流程，
  避免监控线程直接触发大批量的网盘操作。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.log import logger

from ..search.types import TYPE_HOSTS

try:  # Telethon 是可选的：未安装时插件其余功能照常工作
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    TELETHON_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺依赖
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]
    SessionPasswordNeededError = Exception  # type: ignore[assignment]
    TELETHON_AVAILABLE = False


ITEMS_KEY = "items"
STATE_KEY = "state"
STATUS_KEY = "status"

_URL_RE = re.compile(r"https?://[^\s<>\"'）)】，。；、！？】]+")
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+")
_ED2K_RE = re.compile(r"ed2k://\|file\|[^\s]+")

#: 反查域名 → 资源类型（复用搜索渠道的域名表，避免两套口径）
_HOST_TO_TYPE: Dict[str, str] = {}
for _type, _hosts in TYPE_HOSTS.items():
    for _host in _hosts:
        _HOST_TO_TYPE[_host.lower()] = _type


def resource_type_of_url(url: str) -> str:
    """按域名判断网盘类型；认不出返回空串。"""
    text = str(url or "").strip()
    if text.startswith("magnet:"):
        return "magnet"
    if text.startswith("ed2k://"):
        return "ed2k"
    match = re.match(r"https?://([^/:?#]+)", text, re.IGNORECASE)
    if not match:
        return ""
    host = match.group(1).lower()
    for domain, rtype in _HOST_TO_TYPE.items():
        if host == domain or host.endswith("." + domain):
            return rtype
    return ""


def extract_share_links(text: str) -> List[Tuple[str, str]]:
    """从消息文本里抽取网盘分享链接，返回 [(url, type)]（按出现顺序去重）。"""
    body = str(text or "")
    if not body:
        return []
    seen: Dict[str, str] = {}
    for url in _URL_RE.findall(body):
        cleaned = url.rstrip(".,;，。；、!！?？]>")
        rtype = resource_type_of_url(cleaned)
        if rtype:
            seen.setdefault(cleaned, rtype)
    for url in _MAGNET_RE.findall(body):
        seen.setdefault(url, "magnet")
    for url in _ED2K_RE.findall(body):
        seen.setdefault(url, "ed2k")
    return list(seen.items())


def normalize_channel(value: str) -> str:
    """把设置页里填的频道写法归一成 Telethon 能识别的形式。

    支持 ``@name`` / ``name`` / ``https://t.me/name`` /
    ``https://t.me/c/123`` / ``-100123456`` / ``https://t.me/+invite``。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        return text
    if text.startswith("-") or text.isdigit():
        return text
    match = re.match(r"https?://(?:t|telegram)\.me/(.+)$", text, re.IGNORECASE)
    if match:
        path = match.group(1).strip("/")
        if path.startswith("+") or path.startswith("joinchat"):
            return f"https://t.me/{path}"
        if path.startswith("c/"):
            return path
        return "@" + path.split("/")[0]
    if "/" in text:
        return text
    return "@" + text


def _item_id(channel: str, message_id: int) -> str:
    return hashlib.sha1(f"{channel}:{message_id}".encode("utf-8")).hexdigest()[:16]


def _message_title(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = _URL_RE.sub("", line).strip(" -|·:")
        if cleaned:
            return cleaned[:120]
    return ""


class _JsonStore:
    """频道监控自己的小型 JSON 存储。

    条目是「消息级」数据、更新频繁且不需要关系查询，放进插件私有库要给
    ``CloudSubscribeDataStore`` 加表迁移；独立 JSON 文件更轻，删掉也不会影响
    历史记录等核心数据。
    """

    def __init__(self, owner):
        self._owner = owner
        self._lock = threading.RLock()

    def _path(self, key: str):
        directory = self._owner.get_data_path() / "tg_monitor"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}.json"

    def load(self, key: str, default: Any = None) -> Any:
        path = self._path(key)
        with self._lock:
            try:
                if not path.exists():
                    return default
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as error:  # noqa: BLE001 - 坏文件不应拖垮插件
                logger.warning(f"TG 监控数据读取失败（{key}）：{error}")
                return default

    def save(self, key: str, value: Any) -> None:
        path = self._path(key)
        with self._lock:
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8",
            )
            temp.replace(path)


class TgChannelMonitor:
    """单例式监控服务：扫描线程 + 资源入库 + 登录辅助。"""

    def __init__(self, owner):
        self._owner = owner
        self._lock = threading.RLock()
        self._store = _JsonStore(owner)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._login_client = None
        self._login_phone = ""
        self._login_hash = ""
        self._last_scan_at = 0.0
        self._last_error = ""
        self._running = False
        self.configure(
            enabled=False, api_id=0, api_hash="", session="", channels=[],
            keywords=[], exclude_keywords=[], scan_limit=20,
            interval_minutes=10, max_items=500,
        )

    # ------------------------------------------------------------------ 配置
    def configure(
            self, *, enabled: bool, api_id: int, api_hash: str, session: str,
            channels: Iterable[str], keywords: Iterable[str],
            exclude_keywords: Iterable[str], scan_limit: int,
            interval_minutes: int, max_items: int,
    ) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._api_id = int(api_id or 0)
            self._api_hash = str(api_hash or "").strip()
            self._session = str(session or "").strip()
            self._channels = [normalize_channel(item) for item in channels if str(item).strip()]
            self._keywords = [str(item).lower() for item in keywords if str(item).strip()]
            self._exclude_keywords = [
                str(item).lower() for item in exclude_keywords if str(item).strip()
            ]
            self._scan_limit = max(1, int(scan_limit or 20))
            self._interval = max(1, int(interval_minutes or 10)) * 60
            self._max_items = max(50, int(max_items or 500))
        self.stop()
        if self._enabled:
            self.start()

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="cloudsubscribe-tg-monitor", daemon=True,
            )
            self._running = True
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._thread = None
            self._running = False

    def _loop(self) -> None:
        # 启动后先等 20 秒，避免和插件初始化抢资源
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            try:
                self.scan(trigger="定时")
            except Exception as error:  # noqa: BLE001 - 后台线程不能死
                self._last_error = str(error)
                logger.warning(f"TG 频道监控扫描失败：{error}")
            if self._stop.wait(self._interval):
                return

    # ------------------------------------------------------------------ 客户端
    def _client_or_none(self):
        if self._client is not None:
            return self._client
        if not TELETHON_AVAILABLE:
            raise RuntimeError("未安装 telethon，请重装插件依赖")
        if not (self._api_id and self._api_hash and self._session):
            raise RuntimeError("TG 凭据不完整（api_id / api_hash / 会话）")
        if self._client is None:
            client = TelegramClient(StringSession(self._session), self._api_id, self._api_hash)
            client.connect()
            if not client.is_user_authorized():
                raise RuntimeError("TG 会话已失效，请在设置页重新登录")
            self._client = client
        return self._client

    def reset_client(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None

    # ------------------------------------------------------------------ 扫描
    def scan(self, trigger: str = "手动") -> Dict[str, Any]:
        if not self._enabled:
            return {"ok": False, "message": "频道监控未启用"}
        if not self._channels:
            return {"ok": False, "message": "还没有配置要监控的频道"}
        client = self._client_or_none()
        state = dict(self._store.load(STATE_KEY, {}) or {})
        items = list(self._store.load(ITEMS_KEY, []) or [])
        existing = {str(item.get("id")) for item in items}
        existing_links = {
            link for item in items for link in (item.get("links") or [])
        }
        added: List[dict] = []
        errors: List[str] = []
        scanned = 0
        for raw_channel in self._channels:
            try:
                entity = client.get_entity(raw_channel)
            except Exception as error:  # noqa: BLE001
                errors.append(f"{raw_channel}: 无法解析频道（{error}）")
                continue
            channel_key = str(getattr(entity, "id", raw_channel))
            channel_title = (
                getattr(entity, "title", "")
                or getattr(entity, "username", "")
                or raw_channel
            )
            last_id = int(state.get(channel_key) or 0)
            try:
                if last_id:
                    # 增量：只取比 last_id 新的消息，按时间升序处理
                    messages = list(client.iter_messages(
                        entity, limit=self._scan_limit, min_id=last_id, reverse=True,
                    ))
                else:
                    # 首次：取最新 N 条（Telethon 默认倒序），再翻转成升序
                    messages = list(reversed(list(client.iter_messages(
                        entity, limit=self._scan_limit,
                    ))))
            except Exception as error:  # noqa: BLE001
                errors.append(f"{channel_title}: 读取消息失败（{error}）")
                continue
            scanned += len(messages)
            max_id = last_id
            for message in messages:
                message_id = int(getattr(message, "id", 0) or 0)
                max_id = max(max_id, message_id)
                text = str(getattr(message, "message", "") or "")
                if not text:
                    continue
                lowered = text.lower()
                if self._keywords and not any(word in lowered for word in self._keywords):
                    continue
                if self._exclude_keywords and any(
                        word in lowered for word in self._exclude_keywords):
                    continue
                links = extract_share_links(text)
                if not links:
                    continue
                item_id = _item_id(channel_key, message_id)
                if item_id in existing:
                    continue
                fresh = [
                    (url, rtype) for url, rtype in links if url not in existing_links
                ]
                if not fresh:
                    continue
                username = getattr(entity, "username", "") or ""
                message_url = (
                    f"https://t.me/{username}/{message_id}" if username else ""
                )
                published = getattr(message, "date", None)
                item = {
                    "id": item_id,
                    "channel": raw_channel,
                    "channel_title": channel_title,
                    "channel_id": channel_key,
                    "message_id": message_id,
                    "message_url": message_url,
                    "published_at": int(published.timestamp()) if published else 0,
                    "title": _message_title(text),
                    "text": text[:400],
                    "links": [url for url, _ in fresh],
                    "types": sorted({rtype for _, rtype in fresh}),
                    "status": "new",
                    "created_at": int(time.time()),
                }
                items.insert(0, item)
                added.append(item)
                existing.add(item_id)
                existing_links.update(item["links"])
            state[channel_key] = max_id
        if len(items) > self._max_items:
            items = items[: self._max_items]
        self._store.save(ITEMS_KEY, items)
        self._store.save(STATE_KEY, state)
        self._last_scan_at = time.time()
        self._last_error = "; ".join(errors[-3:])
        result = {
            "ok": True,
            "trigger": trigger,
            "scanned": scanned,
            "added": len(added),
            "channels": len(self._channels),
            "errors": errors,
        }
        self._store.save(STATUS_KEY, {
            "last_scan_at": self._last_scan_at,
            "last_result": result,
        })
        if added:
            logger.info(
                f"TG 频道监控（{trigger}）新增 {len(added)} 条资源，"
                f"扫描消息 {scanned} 条"
            )
        elif errors:
            logger.info(f"TG 频道监控（{trigger}）无新增；{self._last_error}")
        return result

    # ------------------------------------------------------------------ 查询
    def status(self) -> Dict[str, Any]:
        stored = self._store.load(STATUS_KEY, {}) or {}
        items = self._store.load(ITEMS_KEY, []) or []
        return {
            "available": TELETHON_AVAILABLE,
            "enabled": self._enabled,
            "configured": bool(self._api_id and self._api_hash and self._session),
            "running": bool(self._thread and self._thread.is_alive()),
            "channels": list(self._channels),
            "channel_count": len(self._channels),
            "item_count": len(items),
            "new_count": sum(1 for item in items if item.get("status") == "new"),
            "last_scan_at": self._last_scan_at or int(stored.get("last_scan_at") or 0),
            "last_error": self._last_error,
            "scan_limit": self._scan_limit,
            "interval_minutes": max(1, self._interval // 60),
        }

    def list_items(self, status: str = "") -> List[dict]:
        items = list(self._store.load(ITEMS_KEY, []) or [])
        if status:
            items = [item for item in items if item.get("status") == status]
        return items

    def update_items(self, item_ids: Iterable[str], **changes: Any) -> int:
        wanted = {str(item) for item in item_ids if str(item)}
        if not wanted:
            return 0
        items = list(self._store.load(ITEMS_KEY, []) or [])
        touched = 0
        for item in items:
            if str(item.get("id")) in wanted:
                item.update(changes)
                touched += 1
        if touched:
            self._store.save(ITEMS_KEY, items)
        return touched

    def delete_items(self, item_ids: Iterable[str]) -> int:
        wanted = {str(item) for item in item_ids if str(item)}
        if not wanted:
            return 0
        items = list(self._store.load(ITEMS_KEY, []) or [])
        kept = [item for item in items if str(item.get("id")) not in wanted]
        if len(kept) != len(items):
            self._store.save(ITEMS_KEY, kept)
        return len(items) - len(kept)

    # ------------------------------------------------------------------ 登录
    def login_send_code(self, phone: str) -> Dict[str, Any]:
        if not TELETHON_AVAILABLE:
            raise RuntimeError("未安装 telethon，请重装插件依赖")
        if not (self._api_id and self._api_hash):
            raise RuntimeError("请先填写 api_id 和 api_hash 并保存")
        phone = str(phone or "").strip()
        if not phone:
            raise RuntimeError("请填写手机号（含国家码，如 +8613800000000）")
        client = TelegramClient(StringSession(), self._api_id, self._api_hash)
        client.connect()
        sent = client.send_code_request(phone)
        self._login_client = client
        self._login_phone = phone
        self._login_hash = str(getattr(sent, "phone_code_hash", "") or "")
        return {"ok": True, "message": "验证码已发送，请填入收到的登录码"}

    def login_submit_code(self, code: str) -> Dict[str, Any]:
        client = self._login_client
        if client is None:
            raise RuntimeError("请先发送验证码")
        try:
            client.sign_in(self._login_phone, str(code or "").strip(),
                           phone_code_hash=self._login_hash)
        except SessionPasswordNeededError:
            return {"ok": True, "need_password": True, "message": "该账号启用了两步验证，请填写密码"}
        return self._finish_login(client)

    def login_submit_password(self, password: str) -> Dict[str, Any]:
        client = self._login_client
        if client is None:
            raise RuntimeError("请先发送验证码")
        client.sign_in(password=str(password or ""))
        return self._finish_login(client)

    def _finish_login(self, client) -> Dict[str, Any]:
        session = StringSession.save(client.session)
        self._session = session
        self._login_client = None
        self._login_phone = ""
        self._login_hash = ""
        try:
            self._owner._persist_config_values(tg_session=session)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"TG 会话持久化失败：{error}")
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "message": "登录成功，会话已保存", "session_saved": True}

    def logout(self) -> Dict[str, Any]:
        self.reset_client()
        self._session = ""
        self._owner._persist_config_values(tg_session="")
        return {"ok": True, "message": "已清除 TG 会话"}


_MONITORS: Dict[str, TgChannelMonitor] = {}
_MONITOR_LOCK = threading.Lock()


def get_channel_monitor(owner) -> TgChannelMonitor:
    """取全局唯一的监控实例，避免插件重载后旧线程继续跑。"""
    key = "default"
    with _MONITOR_LOCK:
        monitor = _MONITORS.get(key)
        if monitor is None:
            monitor = TgChannelMonitor(owner)
            _MONITORS[key] = monitor
        elif monitor._owner is not owner:
            # 插件实例被重建：把监控挂到新实例上，旧客户端断掉重连
            monitor.stop()
            monitor.reset_client()
            monitor._owner = owner
            monitor._store = _JsonStore(owner)
        return monitor
