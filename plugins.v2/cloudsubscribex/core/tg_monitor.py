"""TG 频道监控：用用户自己的 TG 会话读取频道消息，抽取网盘分享链接。

设计要点：
- 只用 Telethon 的**用户会话**（api_id + api_hash + StringSession），
  因此私有频道（账号已加入）和公开频道都能读，Bot API 做不到这一点。
- 扫描是增量的：每个频道记住最后处理的消息 id，只读新消息；首次按
  ``scan_limit`` 回溯。
- 抓到资源先入库；频道规则开启自动转存且配置了白名单时，按标题/季号匹配
  唯一订阅后复用插件既有的手动转存流程，其余条目保留为 pending 供人工选择。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import threading
import time
import weakref
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.log import logger

from ..search.types import TYPE_HOSTS
from .tg_match import match_item

try:  # Telethon 是可选的：未安装时插件其余功能照常工作
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession
    from telethon.tl.types import InputPeerChannel

    TELETHON_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺依赖
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]
    SessionPasswordNeededError = Exception  # type: ignore[assignment]
    InputPeerChannel = None  # type: ignore[assignment]
    TELETHON_AVAILABLE = False


ITEMS_KEY = "items"
STATE_KEY = "state"
# 解析逻辑版本：升级后首次扫描忽略旧游标，回填最近 N 条
# 1 -> 纯正文正则；2 -> 正文 + entities/webpage/按钮 URL
SCAN_LOGIC_VERSION = 2

# 邀请链接解析失败后的冷却时间：CheckChatInvite 每轮都调会被 Telegram
# 按 420 FLOOD_WAIT 重罚，失败后先退避，别连续试探。
RESOLVE_RETRY_SECONDS = 6 * 3600

# 同一进程里只允许一个监控线程：插件热重载会创建新实例，旧实例如果
# 没被 stop，多个线程会同时扫描并触发 Telegram 限流。
_ACTIVE_MONITORS: "weakref.WeakSet[TgChannelMonitor]" = weakref.WeakSet()
_ACTIVE_MONITORS_LOCK = threading.Lock()
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


def _message_entity_urls(message: Any) -> List[str]:
    """抽取 Telegram 消息里不显示在正文中的 URL。

    频道常把分享链接做成「点击跳转」的文字链接（``MessageEntityTextUrl``）、
    网页预览（``web_page.url``）或底部按钮；这些 URL 不在 ``message.message``
    里，只靠正则扫正文会漏掉整条资源。
    """
    text = str(getattr(message, "message", "") or "")
    urls: List[str] = []
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            urls.append(str(url))
            continue
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if isinstance(offset, int) and isinstance(length, int) and length > 0:
            fragment = text[offset:offset + length].strip()
            if "://" in fragment:
                urls.append(fragment)
    media = getattr(message, "media", None)
    webpage = getattr(media, "webpage", None)
    webpage_url = getattr(webpage, "url", None)
    if webpage_url:
        urls.append(str(webpage_url))
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            button_url = getattr(button, "url", None)
            if button_url:
                urls.append(str(button_url))
    return urls


def extract_share_links(
        text: str, extra_urls: Iterable[str] = (),
) -> List[Tuple[str, str]]:
    """从消息文本及实体/按钮 URL 中抽取分享链接，按出现顺序去重。"""
    body = str(text or "")
    candidates = list(_URL_RE.findall(body)) + [
        str(url or "").strip() for url in (extra_urls or [])
    ]
    seen: Dict[str, str] = {}
    for url in candidates:
        cleaned = url.rstrip(".,;，。；、!！?？]>")
        rtype = resource_type_of_url(cleaned)
        if rtype:
            seen.setdefault(cleaned, rtype)
    for url in _MAGNET_RE.findall(body):
        seen.setdefault(url, "magnet")
    for url in _ED2K_RE.findall(body):
        seen.setdefault(url, "ed2k")
    return list(seen.items())


def extract_message_links(message: Any) -> List[Tuple[str, str]]:
    """解析一条 Telegram 消息的全部分享链接（正文 + 实体 + 按钮）。"""
    return extract_share_links(
        str(getattr(message, "message", "") or ""),
        extra_urls=_message_entity_urls(message),
    )


def _scan_start_id(state: Dict[str, Any], channel_key: str) -> int:
    """返回本轮扫描起点；解析逻辑升级后首次强制回填。"""
    version = int(state.get(f"{channel_key}:logic_version") or 0)
    if version < SCAN_LOGIC_VERSION:
        return 0
    return int(state.get(channel_key) or 0)


def _flood_wait_seconds(error: Any) -> int:
    """从 Telethon FloodWaitError 或错误文本里取出等待秒数。"""
    seconds = int(getattr(error, "seconds", 0) or 0)
    if seconds > 0:
        return seconds
    match = re.search(r"(\d+)\s+seconds", str(error or ""), re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _auto_transfer_outcome(result: Any) -> Tuple[str, str]:
    """把同步结果归一为 transferred / skipped / failed。"""
    if not isinstance(result, dict) or not result.get("success"):
        message = (
            str(result.get("message") or "转存失败")
            if isinstance(result, dict) else "转存失败"
        )
        return "failed", message
    data = result.get("data") or {}
    try:
        transferred = int(data.get("transferred") or 0)
    except (TypeError, ValueError):
        transferred = 0
    if transferred > 0:
        return "transferred", ""
    message = str(result.get("message") or "").strip()
    return "skipped", message or "同步完成，未转存新文件（库里已有或未匹配到可下载资源）"


def _unwrap_channel_text(value: Any) -> str:
    """拆掉频道字段外面多包的 JSON/Python 列表和引号。

    设置页或旧配置可能把 ``["https://t.me/+xxx"]`` 存成字符串，甚至再
    ``str()`` 一次变成 ``"['https://t.me/+xxx']"``；直接拿去 get_entity 会报
    ``Cannot find any entity corresponding to "['https://...']"``。
    """
    text = str(value or "").strip()
    for _ in range(4):
        before = text
        text = text.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple)) and parsed:
                text = str(parsed[0])
                continue
            text = text[1:-1].strip()
        text = text.strip("'\"").strip()
        if text == before:
            break
    return text


def normalize_channel(value: str) -> str:
    """把设置页里填的频道写法归一成 Telethon 能识别的形式。

    支持 ``@name`` / ``name`` / ``https://t.me/name`` /
    ``https://t.me/c/123`` / ``-100123456`` / ``https://t.me/+invite``。
    也兼容被列表/多层引号包住的旧配置。
    """
    text = _unwrap_channel_text(value)
    if not text:
        return ""
    if text.startswith("@"):
        return text
    if text.startswith("-") or text.isdigit():
        return text
    match = re.match(
        r"(?:https?://)?(?:t|telegram)\.me/(.+)$", text, re.IGNORECASE,
    )
    if match:
        path = match.group(1).strip("/")
        if path.startswith("+") or path.startswith("joinchat"):
            return f"https://t.me/{path}"
        if path.startswith("c/"):
            parts = path.split("/")
            if len(parts) > 1 and parts[1].isdigit():
                return f"-100{parts[1]}"
            return path
        return "@" + path.split("/")[0]
    if "/" in text:
        return text
    return "@" + text


def _mask_phone(phone: str) -> str:
    digits = str(phone or "").strip()
    if len(digits) <= 5:
        return digits
    return f"{digits[:4]}****{digits[-3:]}"


def _code_delivery(sent) -> str:
    """把 Telethon 的 SentCodeType 映射成 app/sms/call/flashcall。"""
    name = type(getattr(sent, "type", None)).__name__.lower()
    if "flashcall" in name:
        return "flashcall"
    if "call" in name:
        return "call"
    if "sms" in name:
        return "sms"
    if "app" in name:
        return "app"
    return "unknown"


def _item_id(channel: str, message_id: int) -> str:
    return hashlib.sha1(f"{channel}:{message_id}".encode("utf-8")).hexdigest()[:16]


def _message_title(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = _URL_RE.sub("", line).strip(" -|·:")
        if cleaned:
            return cleaned[:120]
    return ""


def _split_terms(value: Any) -> List[str]:
    """把关键词字段归一成列表，兼容列表、逗号、顿号与换行分隔。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        text = str(value or "")
        for separator in ("，", "、", ";", "；", ","):
            text = text.replace(separator, "\n")
        items = text.splitlines()
    return [item.strip() for item in items if item.strip()]


def _rule_whitelist(rule: Dict[str, Any]) -> List[int]:
    """频道规则的订阅白名单；兼容 1.6.68 之前的单值 auto_subscribe_id。"""
    raw_ids = rule.get("auto_subscribe_ids") or []
    if isinstance(raw_ids, str):
        text = raw_ids.strip()
        if text.startswith("["):
            try:
                raw_ids = json.loads(text)
            except (TypeError, ValueError):
                raw_ids = _split_terms(text)
        else:
            raw_ids = _split_terms(text)
    if not isinstance(raw_ids, (list, tuple, set)):
        raw_ids = [raw_ids]
    values: List[Any] = list(raw_ids)
    if rule.get("auto_subscribe_id"):
        values.append(rule.get("auto_subscribe_id"))
    result: List[int] = []
    for value in values:
        try:
            subscribe_id = int(value or 0)
        except (TypeError, ValueError):
            continue
        if subscribe_id > 0 and subscribe_id not in result:
            result.append(subscribe_id)
    return result


def _rule_mode(rule: Dict[str, Any]) -> str:
    """频道规则的监控模式。

    ``messages``：收录全部消息（关键词仍生效），不自动转存；
    ``subscriptions``：收录后按标题在全部订阅内匹配，唯一高置信才自动转存。
    旧配置没有该字段时按 ``auto_transfer`` 推断，保证升级不改语义。
    """
    mode = str(rule.get("monitor_mode") or "").strip().lower()
    if mode in ("subscriptions", "all_subscriptions", "sub"):
        return "subscriptions"
    if mode in ("messages", "all", "all_messages"):
        return "messages"
    return "subscriptions" if rule.get("auto_transfer") else "messages"


def _parse_rules_payload(value: Any) -> Any:
    """``tg_channel_rules`` 可能是列表、JSON 字符串或多行文本。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return text
        return text
    return value


def normalize_channel_rules(
        raw_rules: Any, *, channels: Iterable[str] = (),
        keywords: Iterable[str] = (), exclude_keywords: Iterable[str] = (),
        scan_limit: int = 20,
) -> List[Dict[str, Any]]:
    """归一化每频道规则；旧版的全局频道/关键词会自动展开成每频道一份。"""
    payload = _parse_rules_payload(raw_rules)
    rows: List[Any] = []
    if isinstance(payload, dict):
        rows = [payload]
    elif isinstance(payload, (list, tuple)):
        rows = list(payload)
    elif isinstance(payload, str) and payload.strip():
        rows = [payload]

    if not rows:
        # 兼容 1.6.64 及更早的配置：频道列表 + 全局关键词/回溯条数
        default_keywords = list(keywords)
        default_exclude = list(exclude_keywords)
        rows = [
            {
                "channel": channel,
                "keywords": default_keywords,
                "exclude_keywords": default_exclude,
                "scan_limit": scan_limit,
            }
            for channel in channels
            if str(channel).strip()
        ]

    rules: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows:
        if isinstance(row, str):
            channel = row
            rule_enabled = True
            rule_keywords: Any = []
            rule_exclude: Any = []
            rule_limit = scan_limit
            rule_auto_transfer = False
            rule_auto_subscribe_id = 0
            rule_auto_subscribe_ids: Any = []
            rule_monitor_mode = ""
        elif isinstance(row, dict):
            channel = (
                row.get("channel") or row.get("url")
                or row.get("name") or row.get("id") or ""
            )
            rule_enabled = bool(row.get("enabled", True))
            rule_keywords = row.get("keywords", [])
            rule_exclude = row.get("exclude_keywords", [])
            try:
                rule_limit = int(row.get("scan_limit", scan_limit) or scan_limit)
            except (TypeError, ValueError):
                rule_limit = scan_limit
            rule_auto_transfer = bool(row.get("auto_transfer", False))
            try:
                rule_auto_subscribe_id = int(row.get("auto_subscribe_id", 0) or 0)
            except (TypeError, ValueError):
                rule_auto_subscribe_id = 0
            rule_auto_subscribe_ids = row.get("auto_subscribe_ids", [])
            rule_monitor_mode = str(row.get("monitor_mode") or "").strip().lower()
        else:
            continue
        normalized = normalize_channel(str(channel or ""))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rule_mode = _rule_mode({
            "monitor_mode": rule_monitor_mode,
            "auto_transfer": rule_auto_transfer,
        })
        rules.append({
            "channel": normalized,
            "enabled": rule_enabled,
            "keywords": [word.lower() for word in _split_terms(rule_keywords)],
            "exclude_keywords": [
                word.lower() for word in _split_terms(rule_exclude)
            ],
            "scan_limit": max(1, min(rule_limit, 200)),
            "auto_transfer": rule_mode == "subscriptions",
            "monitor_mode": rule_mode,
            "auto_subscribe_id": max(0, rule_auto_subscribe_id),
            "auto_subscribe_ids": _rule_whitelist({
                "auto_subscribe_ids": rule_auto_subscribe_ids,
                "auto_subscribe_id": rule_auto_subscribe_id,
            }),
        })
    return rules


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
        self._login_session = ""
        self._login_delivery = ""
        self._login_phone = ""
        self._login_hash = ""
        self._last_scan_at = 0.0
        self._last_error = ""
        self._running = False
        self._auto_transfer_handler = None
        self._subscribe_provider = None
        self.configure(
            enabled=False, api_id=0, api_hash="", session="", channels=[],
            keywords=[], exclude_keywords=[], scan_limit=20,
            interval_minutes=10, max_items=500,
        )

    # ------------------------------------------------------------------ 配置
    def configure(
            self, *, enabled: bool, api_id: int, api_hash: str, session: str,
            channels: Iterable[str] = (), keywords: Iterable[str] = (),
            exclude_keywords: Iterable[str] = (), scan_limit: int = 20,
            interval_minutes: int = 10, max_items: int = 500,
            channel_rules: Any = None,
            auto_transfer_handler: Any = None,
            subscribe_provider: Any = None,
    ) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._api_id = int(api_id or 0)
            self._api_hash = str(api_hash or "").strip()
            self._session = str(session or "").strip()
            self._channel_rules = normalize_channel_rules(
                channel_rules,
                channels=channels,
                keywords=keywords,
                exclude_keywords=exclude_keywords,
                scan_limit=scan_limit,
            )
            self._auto_transfer_handler = auto_transfer_handler
            self._subscribe_provider = subscribe_provider
            self._channels = [rule["channel"] for rule in self._channel_rules]
            # 旧字段保留，供 status/兼容读取；扫描只认每频道规则
            self._keywords = [str(item).lower() for item in keywords if str(item).strip()]
            self._exclude_keywords = [
                str(item).lower() for item in exclude_keywords if str(item).strip()
            ]
            self._scan_limit = max(1, int(scan_limit or 20))
            self._interval = max(1, int(interval_minutes or 10)) * 60
            self._max_items = max(50, int(max_items or 500))
            signature = (
                self._enabled, self._api_id, self._api_hash, self._session,
                json.dumps(self._channel_rules, ensure_ascii=False, sort_keys=True),
                self._interval, self._max_items,
            )
            unchanged = (
                signature == getattr(self, "_config_signature", None)
                and self._thread is not None and self._thread.is_alive()
            )
            self._config_signature = signature
        if unchanged:
            return
        self.stop()
        if self._enabled:
            self.start()

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        with _ACTIVE_MONITORS_LOCK:
            for other in list(_ACTIVE_MONITORS):
                if other is not self:
                    try:
                        other.stop()
                    except Exception:  # noqa: BLE001 - 旧实例清理失败不阻塞
                        pass
            _ACTIVE_MONITORS.add(self)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            # 每次启动用独立的停止事件；旧线程即使还没退出，也只会看到
            # 自己的已 set 事件，不会因为新线程 clear 共享事件而“复活”。
            stop_event = threading.Event()
            self._stop = stop_event
            self._thread = threading.Thread(
                target=self._loop, args=(stop_event,),
                name="cloudsubscribe-tg-monitor", daemon=True,
            )
            self._running = True
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            stop_event = self._stop
            thread = self._thread
            self._thread = None
            self._running = False
        stop_event.set()
        if (
            thread and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            # 不无限等待：当前扫描可能卡在 Telegram 请求里。事件已 set，
            # 它结束当前一轮后会自行退出。
            thread.join(timeout=2)

    def _loop(self, stop_event: threading.Event) -> None:
        # 启动后先等 20 秒，避免和插件初始化抢资源
        if stop_event.wait(20):
            return
        while not stop_event.is_set():
            try:
                self.scan(trigger="定时")
            except Exception as error:  # noqa: BLE001 - 后台线程不能死
                self._last_error = str(error)
                logger.warning(f"TG 频道监控扫描失败：{error}")
            if stop_event.wait(self._interval):
                return

    # ------------------------------------------------------------------ 客户端
    def _new_client(self, session: str = ""):
        """每次调用新建客户端：Telethon 是纯异步的，跨线程复用连接不安全。"""
        if not TELETHON_AVAILABLE:
            raise RuntimeError("未安装 telethon，请重装插件依赖")
        return TelegramClient(
            StringSession(str(session or "")), self._api_id, self._api_hash,
            timeout=20, connection_retries=3,
        )

    def _require_credentials(self) -> None:
        if not (self._api_id and self._api_hash):
            raise RuntimeError("请先填写 api_id 和 api_hash 并保存")

    def reset_client(self) -> None:
        with self._lock:
            self._client = None

    # ------------------------------------------------------------------ 扫描
    def scan(self, trigger: str = "手动", full: bool = False) -> Dict[str, Any]:
        if not self._enabled:
            return {"ok": False, "message": "频道监控未启用"}
        if not self._channel_rules:
            return {"ok": False, "message": "还没有配置要监控的频道"}
        if not any(rule.get("enabled", True) for rule in self._channel_rules):
            return {"ok": False, "message": "所有频道规则都已停用"}
        return asyncio.run(self._scan_async(trigger, full=full))

    async def _scan_async(
            self, trigger: str, full: bool = False,
    ) -> Dict[str, Any]:
        client = self._new_client(self._session)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("TG 会话已失效，请在设置页重新登录")
            # StringSession 不保存实体缓存。已有 access_hash 的频道不需要
            # 对话列表；只有首次解析或缓存失效时才全量拉一遍（全量拉取本身
            # 也会占用 Telegram 的请求配额）
            state = self._store.load(STATE_KEY, {}) or {}
            need_warm = any(
                rule.get("enabled", True)
                and not str(
                    state.get(f"resolved_hash:{rule['channel']}") or ""
                ).isdigit()
                for rule in self._channel_rules
            )
            if need_warm:
                try:
                    await client.get_dialogs(limit=None)
                except Exception as error:  # noqa: BLE001
                    logger.warning(
                        f"TG 拉取对话列表失败（不影响邀请链接扫描）：{error}"
                    )
            return await self._scan_with_client(client, trigger, full=full)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _scan_with_client(
            self, client, trigger: str, full: bool = False,
    ) -> Dict[str, Any]:
        state = dict(self._store.load(STATE_KEY, {}) or {})
        items = list(self._store.load(ITEMS_KEY, []) or [])
        existing = {str(item.get("id")) for item in items}
        existing_links = {
            link for item in items for link in (item.get("links") or [])
        }
        added: List[dict] = []
        auto_candidates: List[Tuple[dict, dict]] = []
        errors: List[str] = []
        scanned = 0
        rule_debug: List[dict] = []
        for rule in self._channel_rules:
            raw_channel = rule["channel"]
            if not rule.get("enabled", True):
                rule_debug.append({"channel": raw_channel, "enabled": False})
                continue
            rule_keywords = list(rule.get("keywords") or [])
            rule_exclude = list(rule.get("exclude_keywords") or [])
            rule_limit = int(rule.get("scan_limit") or self._scan_limit)
            cached_id = str(
                state.get(f"resolved:{raw_channel}") or ""
            ).strip()
            cached_hash = str(
                state.get(f"resolved_hash:{raw_channel}") or ""
            ).strip()
            now = int(time.time())
            cooldown_until = int(
                state.get(f"flood_until:{raw_channel}") or 0
            )
            if cooldown_until > now:
                remaining = cooldown_until - now
                rule_debug.append({
                    "channel": raw_channel,
                    "cooldown_remaining": remaining,
                })
                logger.info(
                    f"TG 频道 {raw_channel} 限流冷却中，剩余 {remaining}s"
                )
                continue
            retry_until = int(
                state.get(f"resolve_retry:{raw_channel}") or 0
            )
            if retry_until > now:
                rule_debug.append({
                    "channel": raw_channel,
                    "resolve_retry_remaining": retry_until - now,
                })
                continue
            entity = None
            resolve_error: Optional[Exception] = None
            candidates: List[Any] = []
            if cached_id.isdigit():
                # 有 access_hash 就直接构造 peer，完全绕开 CheckChatInvite
                if cached_hash.isdigit() and InputPeerChannel is not None:
                    candidates.append(
                        InputPeerChannel(int(cached_id), int(cached_hash))
                    )
                candidates.append(int(cached_id))
                # 缓存失效（换会话/被踢）时才回退一次邀请链接
                candidates.append(raw_channel)
            elif re.fullmatch(r"-?\d+", str(raw_channel)):
                candidates.append(int(raw_channel))
            else:
                candidates.append(raw_channel)
            for candidate in candidates:
                try:
                    entity = await client.get_entity(candidate)
                    break
                except Exception as error:  # noqa: BLE001
                    resolve_error = error
            if entity is None:
                seconds = _flood_wait_seconds(resolve_error)
                if seconds > 0:
                    state[f"flood_until:{raw_channel}"] = now + seconds
                    logger.warning(
                        f"TG 频道 {raw_channel} 触发限流，"
                        f"{seconds}s 后自动重试"
                    )
                    rule_debug.append({
                        "channel": raw_channel,
                        "cooldown_remaining": seconds,
                    })
                    continue
                detail = str(resolve_error)
                if "not part of" in detail:
                    detail = f"{detail}（请先用该账号加入此频道）"
                state[f"resolve_retry:{raw_channel}"] = (
                    now + RESOLVE_RETRY_SECONDS
                )
                errors.append(
                    f"{raw_channel}: 无法解析频道（{detail}），"
                    f"{RESOLVE_RETRY_SECONDS // 3600}h 内不再重试"
                )
                continue
            channel_key = str(getattr(entity, "id", raw_channel))
            access_hash = getattr(entity, "access_hash", None)
            state[f"resolved:{raw_channel}"] = channel_key
            if access_hash:
                state[f"resolved_hash:{raw_channel}"] = str(access_hash)
            state.pop(f"flood_until:{raw_channel}", None)
            state.pop(f"resolve_retry:{raw_channel}", None)
            channel_title = (
                getattr(entity, "title", "")
                or getattr(entity, "username", "")
                or raw_channel
            )
            last_id = 0 if full else _scan_start_id(state, channel_key)
            try:
                if last_id:
                    # 增量：只取比 last_id 新的消息，按时间升序处理
                    messages = await client.get_messages(
                        entity, limit=rule_limit, min_id=last_id, reverse=True,
                    )
                else:
                    # 首次：取最新 N 条（Telethon 默认倒序），再翻转成升序
                    messages = list(reversed(await client.get_messages(
                        entity, limit=rule_limit,
                    )))
            except Exception as error:  # noqa: BLE001
                errors.append(f"{channel_title}: 读取消息失败（{error}）")
                continue
            messages = list(messages or [])
            scanned += len(messages)
            rule_debug.append({
                "channel": raw_channel,
                "channel_key": channel_key,
                "start_id": last_id,
                "messages": len(messages),
                "logic_version": int(
                    state.get(f"{channel_key}:logic_version") or 0
                ),
            })
            max_id = last_id
            for message in messages:
                message_id = int(getattr(message, "id", 0) or 0)
                max_id = max(max_id, message_id)
                text = str(getattr(message, "message", "") or "")
                if not text:
                    continue
                lowered = text.lower()
                if rule_keywords and not any(word in lowered for word in rule_keywords):
                    continue
                if rule_exclude and any(word in lowered for word in rule_exclude):
                    continue
                links = extract_message_links(message)
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
                if _rule_mode(rule) == "subscriptions":
                    auto_candidates.append((item, dict(rule)))
            state[channel_key] = max_id
            state[f"{channel_key}:logic_version"] = SCAN_LOGIC_VERSION
        if len(items) > self._max_items:
            items = items[: self._max_items]
        self._store.save(ITEMS_KEY, items)
        self._store.save(STATE_KEY, state)
        auto_stats = self._run_auto_transfers(auto_candidates)
        self._store.save(ITEMS_KEY, items)
        self._last_scan_at = time.time()
        self._last_error = "; ".join(errors[-3:])
        result = {
            "ok": True,
            "trigger": trigger,
            "scanned": scanned,
            "added": len(added),
            "channels": sum(
                1 for rule in self._channel_rules if rule.get("enabled", True)
            ),
            "errors": errors,
            "debug": rule_debug,
            "auto_transferred": auto_stats["auto_transferred"],
            "auto_skipped": auto_stats["auto_skipped"],
            "auto_failed": auto_stats["auto_failed"],
            "auto_pending": auto_stats["auto_pending"],
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

    def _run_auto_transfers(
            self, candidates: List[Tuple[dict, dict]],
    ) -> Dict[str, int]:
        """扫描后按频道规则自动转存；失败只记在条目上，不重试、不阻塞下次扫描。"""
        stats = {
            "auto_transferred": 0, "auto_skipped": 0,
            "auto_failed": 0, "auto_pending": 0,
        }
        if not candidates:
            return stats
        handler = getattr(self, "_auto_transfer_handler", None)
        if not callable(handler):
            for item, _rule in candidates:
                item["auto_transfer_error"] = "自动转存未启用：插件未提供转存处理器"
            stats["auto_failed"] = len(candidates)
            return stats

        provider = getattr(self, "_subscribe_provider", None)
        subscriptions = None
        provider_error = ""
        if callable(provider):
            try:
                subscriptions = list(provider() or [])
            except Exception as error:  # noqa: BLE001 - 读订阅失败不应误转
                provider_error = str(error)
                logger.warning(f"TG 频道自动匹配读取订阅失败：{error}")

        for item, rule in candidates:
            subscribe_id = 0
            if callable(provider):
                if provider_error:
                    item["match_status"] = "pending"
                    item["match_reason"] = f"读取订阅列表失败：{provider_error}"[:300]
                    stats["auto_pending"] += 1
                    continue
                matched = match_item(item, subscriptions or [], allowed_ids=None)
                item["match_candidates"] = [
                    {
                        "id": row["id"], "name": row["name"],
                        "season": row["season"], "type": row["type"],
                        "score": row["score"], "reason": row["reason"],
                    }
                    for row in (matched.get("candidates") or [])
                ]
                item["match_reason"] = matched.get("reason") or ""
                item["match_status"] = (
                    "matched" if matched.get("matched") else "pending"
                )
                if not matched.get("matched"):
                    stats["auto_pending"] += 1
                    logger.info(
                        f"TG 频道自动匹配留待手动（{rule.get('channel')}）："
                        f"{item.get('title')} / {item['match_reason']}"
                    )
                    continue
                subscribe_id = int(matched["matched"]["id"])
                item["matched_subscribe_id"] = subscribe_id
            else:
                subscribe_id = 0
            if subscribe_id <= 0:
                item["match_status"] = "pending"
                item["match_reason"] = "需要订阅列表才能匹配目标订阅"
                stats["auto_pending"] += 1
                continue

            try:
                result = handler(item, subscribe_id)
            except Exception as error:  # noqa: BLE001 - 单条失败不影响扫描
                result = {"success": False, "message": str(error)}
            outcome, message = _auto_transfer_outcome(result)
            item["subscribe_id"] = subscribe_id
            if outcome == "transferred":
                item["status"] = "transferred"
                item["match_status"] = "transferred"
                item["auto_transferred_at"] = int(time.time())
                item.pop("auto_transfer_error", None)
                stats["auto_transferred"] += 1
            elif outcome == "skipped":
                # 同步成功但没转出文件：通常库里已有该集，或资源没有可下载文件。
                # 不能标成“已转存”，否则用户会以为历史记录漏了。
                item["status"] = "skipped"
                item["match_status"] = "skipped"
                item["auto_skipped_at"] = int(time.time())
                item["auto_transfer_error"] = message[:300]
                stats["auto_skipped"] += 1
                logger.info(
                    f"TG 频道自动转存无新文件（{rule.get('channel')} / "
                    f"订阅 {subscribe_id}）：{message}"
                )
            else:
                item["auto_transfer_error"] = message[:300]
                item["auto_subscribe_id"] = subscribe_id
                item["match_status"] = "failed"
                stats["auto_failed"] += 1
                logger.warning(
                    f"TG 频道自动转存失败（{rule.get('channel')} / "
                    f"订阅 {subscribe_id}）：{message}"
                )
        return stats

    # ------------------------------------------------------------------ 查询
    def status(self) -> Dict[str, Any]:
        stored = self._store.load(STATUS_KEY, {}) or {}
        items = self._store.load(ITEMS_KEY, []) or []
        return {
            "available": TELETHON_AVAILABLE,
            "enabled": self._enabled,
            "has_api": bool(self._api_id and self._api_hash),
            "configured": bool(self._api_id and self._api_hash and self._session),
            "running": bool(self._thread and self._thread.is_alive()),
            "channels": list(self._channels),
            "channel_count": len(self._channels),
            "enabled_channel_count": sum(
                1 for rule in self._channel_rules if rule.get("enabled", True)
            ),
            "channel_rules": [
                {
                    "channel": rule["channel"],
                    "enabled": bool(rule.get("enabled", True)),
                    "keywords": list(rule.get("keywords") or []),
                    "exclude_keywords": list(rule.get("exclude_keywords") or []),
                    "scan_limit": int(rule.get("scan_limit") or self._scan_limit),
                    "auto_transfer": _rule_mode(rule) == "subscriptions",
                    "monitor_mode": _rule_mode(rule),
                    "auto_subscribe_id": int(rule.get("auto_subscribe_id") or 0),
                    "auto_subscribe_ids": list(rule.get("auto_subscribe_ids") or []),
                }
                for rule in self._channel_rules
            ],
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
        self._require_credentials()
        phone = str(phone or "").strip()
        if not phone:
            raise RuntimeError("请填写手机号（含国家码，如 +8613800000000）")
        return asyncio.run(self._login_send_code_async(phone))

    async def _login_send_code_async(self, phone: str) -> Dict[str, Any]:
        client = self._new_client()
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            self._login_session = StringSession.save(client.session)
            self._login_phone = phone
            self._login_hash = str(getattr(sent, "phone_code_hash", "") or "")
            delivery = _code_delivery(sent)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._login_delivery = delivery
        logger.info(f"TG 登录验证码请求已提交：号码={_mask_phone(phone)}，发送方式={delivery}")
        hint = {
            "app": "验证码已发到你的 Telegram 客户端（官方「Telegram」服务对话），不是短信",
            "sms": "验证码已通过短信发送",
            "call": "Telegram 会拨打你的手机，语音播报验证码",
            "flashcall": "Telegram 会来电，来电号码即验证码",
        }.get(delivery, "验证码已发送")
        return {"ok": True, "message": hint, "delivery": delivery}

    def login_submit_code(self, code: str) -> Dict[str, Any]:
        if not self._login_session or not self._login_hash:
            raise RuntimeError("请先发送验证码")
        return asyncio.run(self._login_submit_code_async(str(code or "").strip()))

    async def _login_submit_code_async(self, code: str) -> Dict[str, Any]:
        client = self._new_client(self._login_session)
        await client.connect()
        try:
            await client.sign_in(
                self._login_phone, code, phone_code_hash=self._login_hash,
            )
        except SessionPasswordNeededError:
            # 两步验证：保留这一步的会话，下一步直接输密码
            self._login_session = StringSession.save(client.session)
            return {"ok": True, "need_password": True, "message": "该账号启用了两步验证，请填写密码"}
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return self._finish_login(client)

    def login_submit_password(self, password: str) -> Dict[str, Any]:
        if not self._login_session:
            raise RuntimeError("请先发送验证码")
        return asyncio.run(self._login_submit_password_async(str(password or "")))

    async def _login_submit_password_async(self, password: str) -> Dict[str, Any]:
        client = self._new_client(self._login_session)
        await client.connect()
        try:
            await client.sign_in(password=password)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return self._finish_login(client)

    def _finish_login(self, client) -> Dict[str, Any]:
        session = StringSession.save(client.session)
        self._session = session
        self._login_session = ""
        self._login_phone = ""
        self._login_hash = ""
        try:
            self._owner._persist_config_values(tg_session=session)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"TG 会话持久化失败：{error}")
        return {"ok": True, "message": "登录成功，会话已保存", "session_saved": True}

    def logout(self) -> Dict[str, Any]:
        self.reset_client()
        self._session = ""
        self._login_session = ""
        self._login_phone = ""
        self._login_hash = ""
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
