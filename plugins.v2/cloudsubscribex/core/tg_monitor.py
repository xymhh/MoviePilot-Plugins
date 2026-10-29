"""TG 频道监控：用用户自己的 TG 会话读取频道消息，抽取网盘分享链接。

设计要点：
- 只用 Telethon 的**用户会话**（api_id + api_hash + StringSession），
  因此私有频道（账号已加入）和公开频道都能读，Bot API 做不到这一点。
- 扫描是增量的：每个频道记住最后处理的消息 id，只读新消息；首次按
  ``scan_limit`` 回溯。
- 抓到资源先入库，按标题/季号与**全部订阅**匹配：唯一高置信命中才转存
  （缺集优先、其次洗版，由既有同步链路保证），未命中直接 ignored。
- 转存不占用扫描线程：命中条目入队，由单线程 worker 按订阅合并成批、串行
  调用手动转存链路（1.7.89 起，根治「一次扫描几十条把扫描线程卡几小时」）。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import queue
import re
import sys
import threading
import time
import weakref
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.log import logger

from ..search.types import TYPE_HOSTS
from .tg_match import match_item
from .tg_quality import normalize_quality_preferences, parse_quality

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
# 增量扫描单轮最多追 5 倍「回溯条数」，并封顶 500 条；避免频道发帖速度
# 长期高于每轮 scan_limit 时游标一直落后。
SCAN_CATCHUP_FACTOR = 5
SCAN_CATCHUP_MAX = 500
# 未命中订阅的消息直接忽略；只留最近 N 条用于排障，不占待处理名额。
UNMATCHED_KEEP = 20
# 转存队列上限：超过就忽略（reason=队列积压），避免一次性把 115 打满。
QUEUE_MAX = 200
# 队列优先级：订阅仍缺集 → 先补集；订阅已齐 → 只可能洗版，排在后面。
QUEUE_PRIORITY_MISSING = 0
QUEUE_PRIORITY_UPGRADE = 1
# 同一订阅一次合并提交的最大链接数（手动转存接口上限是 50）。
BATCH_LINK_LIMIT = 20
# skipped/transferred 只是历史记录，最多占池子的 12%（500 条时约 60 条）；
# 其余名额留给待处理条目。线上实测 104 条 skipped 会把列表淹掉。
HISTORY_QUOTA_RATIO = 0.12

# 邀请链接解析失败后的冷却时间：CheckChatInvite 每轮都调会被 Telegram
# 按 420 FLOOD_WAIT 重罚，失败后先退避，别连续试探。
RESOLVE_RETRY_SECONDS = 6 * 3600

# 同一进程里只允许一个监控线程：插件热重载会创建新实例，旧实例如果
# 没被 stop，多个线程会同时扫描并触发 Telegram 限流。
#
# 注意：登记表不能放在模块级变量里 —— MoviePilot 热重载会重新执行本模块，
# 模块变量随之重置，新实例就看不见旧实例的线程，旧线程（跑的还是旧代码）
# 会一直扫下去。所以把它挂在 sys 上，重载后仍然共享同一份。
_REGISTRY_ATTR = "_cloudsubscribe_tg_monitor_registry"


def _shared_monitor_registry():
    """返回 (活跃实例弱集合, 锁)，跨热重载共享。"""
    state = getattr(sys, _REGISTRY_ATTR, None)
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("lock"), type(threading.Lock()))
        or not hasattr(state.get("monitors"), "add")
    ):
        state = {"monitors": weakref.WeakSet(), "lock": threading.Lock()}
        setattr(sys, _REGISTRY_ATTR, state)
    return state["monitors"], state["lock"]


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


def migrate_legacy_channel_rules(
        raw_rules: Any, *, channels: Iterable[str] = (),
        keywords: Iterable[str] = (), exclude_keywords: Iterable[str] = (),
        scan_limit: int = 20,
) -> List[Dict[str, Any]]:
    """把 1.6.64 前的「全局频道 + 全局关键词」一次性展开成逐频道规则。

    返回待写入 ``tg_channel_rules`` 的规则列表；已有规则时返回空列表
    （不覆盖用户当前配置）。迁移后旧键由调用方清空，只保留一个配置来源。
    """
    if normalize_channel_rules(raw_rules):
        return []
    default_keywords = _split_terms(keywords)
    default_exclude = _split_terms(exclude_keywords)
    rows = [
        {
            "channel": str(channel).strip(),
            "enabled": True,
            "keywords": list(default_keywords),
            "exclude_keywords": list(default_exclude),
            "scan_limit": scan_limit,
        }
        for channel in channels
        if str(channel).strip()
    ]
    return normalize_channel_rules(rows)


def normalize_channel_rules(
        raw_rules: Any, *, channels: Iterable[str] = (),
        keywords: Iterable[str] = (), exclude_keywords: Iterable[str] = (),
        scan_limit: int = 20,
) -> List[Dict[str, Any]]:
    """归一化每频道规则：``tg_channel_rules`` 是唯一来源，不再回退旧全局配置。"""
    payload = _parse_rules_payload(raw_rules)
    rows: List[Any] = []
    if isinstance(payload, dict):
        rows = [payload]
    elif isinstance(payload, (list, tuple)):
        rows = list(payload)
    elif isinstance(payload, str) and payload.strip():
        rows = [payload]

    rules: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows:
        if isinstance(row, str):
            channel = row
            rule_enabled = True
            rule_keywords: Any = []
            rule_exclude: Any = []
            rule_limit = scan_limit
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
        else:
            continue
        normalized = normalize_channel(str(channel or ""))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        # 1.7.89 起判定链只有一条：命中全部订阅才转存。
        # 旧配置里的 monitor_mode / auto_subscribe_ids 保留解析但不再生效。
        rules.append({
            "channel": normalized,
            "enabled": rule_enabled,
            "keywords": [word.lower() for word in _split_terms(rule_keywords)],
            "exclude_keywords": [
                word.lower() for word in _split_terms(rule_exclude)
            ],
            "scan_limit": max(1, min(rule_limit, 200)),
            "auto_transfer": True,
            "monitor_mode": "subscriptions",
            "auto_subscribe_id": 0,
            "auto_subscribe_ids": [],
        })
    return rules


class _JsonFallbackStore:
    """状态库不可用时的本地 JSON 兜底（单测/极端环境）。

    生产走 ``runtime_records``；只有 ``initialize()`` 抛错（没有 sqlalchemy、
    数据库被锁死）时才退到这里，保证扫描游标与 access_hash 不会每轮丢失，
    否则会反复调用 CheckChatInvite 触发 Telegram 限流。
    """

    FILE_NAME = "tg_monitor_fallback.json"

    def __init__(self, owner):
        self._lock = threading.RLock()
        try:
            self._path = str(owner.get_data_path() / self.FILE_NAME)
        except Exception:  # noqa: BLE001 - 连路径都拿不到就纯内存
            self._path = ""
        self._rows: Dict[str, Any] = {}
        self._loaded = False

    def _load_all(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._rows = data
        except FileNotFoundError:
            return
        except Exception as error:  # noqa: BLE001 - 坏文件不阻塞扫描
            logger.warning(f"TG 监控兜底状态文件读取失败：{error}")

    def _flush(self) -> None:
        if not self._path:
            return
        try:
            import os

            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._rows, handle, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"TG 监控兜底状态文件写入失败：{error}")

    def load(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._load_all()
            return self._rows.get(key, default)

    def save(self, key: str, value: Any) -> None:
        with self._lock:
            self._load_all()
            self._rows[key] = value
            self._flush()

    def delete(self, key: str) -> None:
        with self._lock:
            self._load_all()
            self._rows.pop(key, None)
            self._flush()


class _MonitorStore:
    """频道监控自己的小型状态存储（统一落插件私有库 ``runtime_records``）。

    条目是「消息级」数据、更新频繁且不需要关系查询，放进插件私有库的好处是：
    一个库、一份事务边界、一次 WAL 提交，不再有散落的 ``tg_monitor/*.json``。
    """

    #: 运行期键值表的命名空间
    NAMESPACE = "tg_monitor"

    def __init__(self, owner):
        self._owner = owner
        self._lock = threading.RLock()
        self._store = None

    def _target(self):
        if self._store is None:
            store = None
            try:
                store = self._owner._get_data_store()
            except Exception:  # noqa: BLE001 - 单测替身只有 get_data_path
                try:
                    from .storage import CloudSubscribeDataStore

                    store = CloudSubscribeDataStore(self._owner)
                except Exception:  # noqa: BLE001
                    store = None
            if store is not None:
                try:
                    self._store = store.runtime_store(self.NAMESPACE)
                    return self._store
                except Exception as error:  # noqa: BLE001 - 库不可用就兜底
                    logger.warning(
                        f"TG 监控状态库不可用，改用本地 JSON 兜底：{error}"
                    )
            self._store = _JsonFallbackStore(self._owner)
        return self._store

    def load(self, key: str, default: Any = None) -> Any:
        with self._lock:
            try:
                value = self._target().load(key, default)
            except Exception as error:  # noqa: BLE001 - 读失败不应拖垮插件
                logger.warning(f"TG 监控数据读取失败（{key}）：{error}")
                return default
            return default if value is None else value

    def save(self, key: str, value: Any) -> None:
        with self._lock:
            try:
                self._target().save(key, value)
            except Exception as error:  # noqa: BLE001 - 写失败只记日志
                logger.warning(f"TG 监控数据写入失败（{key}）：{error}")


class TgChannelMonitor:
    """单例式监控服务：扫描线程 + 资源入库 + 登录辅助。"""

    def __init__(self, owner):
        self._owner = owner
        self._lock = threading.RLock()
        self._store = _MonitorStore(owner)
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
        self._auto_transfer_batch_handler = None
        self._subscribe_provider = None
        # 扫描与转存 worker 都会改池子：读写一律走这把锁，避免互相覆盖。
        self._items_lock = threading.RLock()
        # 优先级队列：(priority, seq, item_id)；同优先级按入队顺序先进先出。
        self._transfer_queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._queue_seq = 0
        self._transfer_thread: Optional[threading.Thread] = None
        self._transfer_stop = threading.Event()
        self._queued_ids: set = set()
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
            quality_preferences: Any = None,
            auto_transfer_handler: Any = None,
            auto_transfer_batch_handler: Any = None,
            subscribe_provider: Any = None,
            keep_unmatched: bool = False,
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
            self._quality_preferences = normalize_quality_preferences(
                quality_preferences
            )
            self._auto_transfer_handler = auto_transfer_handler
            self._auto_transfer_batch_handler = auto_transfer_batch_handler
            self._subscribe_provider = subscribe_provider
            # 旧开关保留但不再生效：未命中订阅一律忽略（1.7.89）。
            self._keep_unmatched = False
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
        monitors, monitors_lock = _shared_monitor_registry()
        with monitors_lock:
            # 只在这里做集合读写。禁止持锁调用其它实例的方法：
            # 对端的 stop() 会再次获取同一把不可重入锁，同线程二次获取即死锁
            # （2026-10-24 插件更新后长期停在“等待加载”的根因）。
            others = [other for other in monitors if other is not self]
            monitors.add(self)
        for other in others:
            try:
                other.stop()
            except Exception:  # noqa: BLE001 - 旧实例清理失败不阻塞
                logger.debug("start 分支忽略异常", exc_info=True)
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
        self._start_transfer_worker()

    def stop(self) -> None:
        monitors, monitors_lock = _shared_monitor_registry()
        with monitors_lock:
            monitors.discard(self)
        with self._lock:
            stop_event = self._stop
            thread = self._thread
            self._thread = None
            self._running = False
        stop_event.set()
        self._stop_transfer_worker()
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

    # --------------------------------------------------------------- 转存 worker
    def _start_transfer_worker(self) -> None:
        with self._lock:
            if self._transfer_thread and self._transfer_thread.is_alive():
                return
            stop_event = threading.Event()
            self._transfer_stop = stop_event
            self._transfer_thread = threading.Thread(
                target=self._transfer_loop, args=(stop_event,),
                name="cloudsubscribe-tg-transfer", daemon=True,
            )
            self._transfer_thread.start()
        self._recover_queued_items()

    def _stop_transfer_worker(self) -> None:
        with self._lock:
            stop_event = self._transfer_stop
            thread = self._transfer_thread
            self._transfer_thread = None
        stop_event.set()
        if (
                thread and thread.is_alive()
                and thread is not threading.current_thread()
        ):
            thread.join(timeout=2)

    def _recover_queued_items(self) -> None:
        """插件重启后把残留 queued 条目重新入队。"""
        try:
            items = self._load_items()
        except Exception as error:  # noqa: BLE001
            logger.debug(f"TG 转存队列恢复失败：{error}")
            return
        for item in items:
            if str(item.get("status") or "") == "queued":
                self._enqueue_item(item)

    def _load_items(self) -> List[dict]:
        with self._items_lock:
            return list(self._store.load(ITEMS_KEY, []) or [])

    def _save_items(self, items: List[dict]) -> None:
        with self._items_lock:
            self._store.save(ITEMS_KEY, items)

    @staticmethod
    def _queue_priority(item: Dict[str, Any]) -> int:
        """缺集补全优先于洗版：订阅还缺集时排前面。"""
        raw = (item or {}).get("queue_priority")
        if raw is None:
            # 老条目（升级前入队）没有优先级字段：按「只会洗版」排后面更安全，
            # 缺集条目在下一轮扫描会重新写入正确优先级。
            return QUEUE_PRIORITY_UPGRADE
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return QUEUE_PRIORITY_UPGRADE
        return value if value in (QUEUE_PRIORITY_MISSING, QUEUE_PRIORITY_UPGRADE) \
            else QUEUE_PRIORITY_UPGRADE

    def _enqueue_item(self, item: Dict[str, Any]) -> bool:
        """把已判定为 queued 的条目放进转存队列（同一 id 只入一次）。"""
        item_id = str((item or {}).get("id") or "")
        if not item_id:
            return False
        priority = self._queue_priority(item)
        with self._items_lock:
            if item_id in self._queued_ids:
                return False
            if self._transfer_queue.qsize() >= QUEUE_MAX:
                return False
            self._queued_ids.add(item_id)
            self._queue_seq += 1
            seq = self._queue_seq
        self._transfer_queue.put((priority, seq, item_id))
        return True

    @staticmethod
    def _queue_entry_id(entry: Any) -> str:
        """兼容 (priority, seq, id) 元组与旧的纯 id 队列条目。"""
        if isinstance(entry, tuple) and len(entry) == 3:
            return str(entry[2] or "")
        return str(entry or "")

    def _mark_items(
            self, item_ids: Iterable[str], status: str, **changes: Any,
    ) -> int:
        """按 id 更新池子条目并落盘；调用方不必自己处理加锁。"""
        wanted = {str(item) for item in item_ids if str(item)}
        if not wanted:
            return 0
        with self._items_lock:
            items = list(self._store.load(ITEMS_KEY, []) or [])
            touched = 0
            for item in items:
                if str(item.get("id")) in wanted:
                    item["status"] = status
                    item.update(changes)
                    touched += 1
            if touched:
                self._store.save(ITEMS_KEY, items)
        return touched

    def _transfer_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                first = self._queue_entry_id(self._transfer_queue.get(timeout=1.0))
            except queue.Empty:
                continue
            batch_ids = [first]
            # 一次最多捞 100 个 id，再按订阅分组，避免批次过大。
            while len(batch_ids) < 100:
                try:
                    batch_ids.append(
                        self._queue_entry_id(self._transfer_queue.get_nowait())
                    )
                except queue.Empty:
                    break
            try:
                self._process_transfer_items(batch_ids, stop_event)
            except Exception as error:  # noqa: BLE001 - worker 不能死
                for item_id in batch_ids:
                    self._queued_ids.discard(item_id)
                self._mark_items(
                    batch_ids, "failed",
                    auto_transfer_error=f"转存异常：{error}"[:300],
                )
                logger.warning(f"TG 频道转存批次异常：{error}")

    def _process_transfer_items(
            self, item_ids: List[str], stop_event: threading.Event,
    ) -> None:
        items = self._load_items()
        by_id = {str(item.get("id")): item for item in items}
        groups: Dict[int, List[dict]] = {}
        for item_id in item_ids:
            self._queued_ids.discard(item_id)
            item = by_id.get(item_id)
            if not item or str(item.get("status") or "") != "queued":
                continue
            try:
                subscribe_id = int(
                    item.get("matched_subscribe_id")
                    or item.get("subscribe_id") or 0
                )
            except (TypeError, ValueError):
                subscribe_id = 0
            if subscribe_id <= 0:
                self._mark_items(
                    [item_id], "failed",
                    auto_transfer_error="缺少目标订阅",
                )
                continue
            groups.setdefault(subscribe_id, []).append(item)
        for subscribe_id, rows in groups.items():
            if stop_event.is_set():
                return
            self._transfer_group(subscribe_id, rows)

    def _transfer_group(
            self, subscribe_id: int, rows: List[dict],
    ) -> None:
        """同一订阅的多条资源合并成一次手动转存，减少重复建立基线。"""
        batch_handler = getattr(self, "_auto_transfer_batch_handler", None)
        single_handler = getattr(self, "_auto_transfer_handler", None)
        item_ids = [str(row.get("id") or "") for row in rows]
        if len(rows) > 1 and callable(batch_handler):
            # 手动转存接口单次上限 50 个链接，这里按 20 个链接切块，留足余量。
            chunk: List[dict] = []
            links_in_chunk = 0
            for row, item_id in zip(rows, item_ids):
                link_count = max(1, len(row.get("links") or []))
                if chunk and links_in_chunk + link_count > BATCH_LINK_LIMIT:
                    self._run_batch_chunk(batch_handler, subscribe_id, chunk, links_in_chunk)
                    chunk, links_in_chunk = [], 0
                chunk.append({"row": row, "id": item_id})
                links_in_chunk += link_count
            if chunk:
                self._run_batch_chunk(batch_handler, subscribe_id, chunk, links_in_chunk)
            return
        if not callable(single_handler):
            self._mark_items(
                item_ids, "failed",
                auto_transfer_error="自动转存未启用：插件未提供转存处理器",
            )
            return
        for row, item_id in zip(rows, item_ids):
            result = self._call_transfer(single_handler, row, subscribe_id)
            outcome, message = _auto_transfer_outcome(result)
            self._apply_group_outcome(
                [item_id], subscribe_id, outcome, message, [row],
            )

    def _run_batch_chunk(
            self, batch_handler: Any, subscribe_id: int,
            chunk: List[Dict[str, Any]], link_count: int,
    ) -> None:
        rows = [entry["row"] for entry in chunk]
        item_ids = [str(entry["id"]) for entry in chunk]
        result = self._call_transfer(batch_handler, rows, subscribe_id)
        outcome, message = _auto_transfer_outcome(result)
        if outcome == "skipped" and link_count:
            message = message or "库中已有，未转存新文件"
        self._apply_group_outcome(item_ids, subscribe_id, outcome, message, rows)

    @staticmethod
    def _call_transfer(handler: Any, payload: Any, subscribe_id: int) -> Any:
        try:
            return handler(payload, subscribe_id)
        except Exception as error:  # noqa: BLE001 - 单批失败不影响后续
            return {"success": False, "message": str(error)}

    def _apply_group_outcome(
            self, item_ids: List[str], subscribe_id: int,
            outcome: str, message: str, rows: List[dict],
    ) -> None:
        now = int(time.time())
        changes: Dict[str, Any] = {"subscribe_id": subscribe_id}
        if outcome == "transferred":
            changes.update({
                "match_status": "transferred",
                "auto_transferred_at": now,
                "auto_transfer_error": "",
            })
            self._mark_items(item_ids, "transferred", **changes)
            logger.info(
                f"TG 频道自动转存完成：订阅 {subscribe_id}，"
                f"{len(item_ids)} 条消息（{str(rows[0].get('title') or '')[:40]} 等）"
            )
            return
        if outcome == "skipped":
            changes.update({
                "match_status": "skipped",
                "auto_skipped_at": now,
                "auto_transfer_error": (message or "库中已有，未转存新文件")[:300],
            })
            self._mark_items(item_ids, "skipped", **changes)
            return
        changes.update({
            "match_status": "failed",
            "auto_transfer_error": (message or "转存失败")[:300],
        })
        self._mark_items(item_ids, "failed", **changes)
        logger.warning(
            f"TG 频道自动转存失败（订阅 {subscribe_id}）：{message}"
        )

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
                logger.debug("_scan_async 分支忽略异常", exc_info=True)

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
            # 上一轮打满上限说明频道在积压：本轮临时把回溯条数翻倍（最多 4 倍），
            # 追平后自动回落，不需要用户手调 scan_limit。
            backlog = int(state.get(f"backlog:{raw_channel}") or 0)
            if backlog:
                rule_limit = min(
                    rule_limit * (2 ** min(backlog, 2)), SCAN_CATCHUP_MAX,
                )
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
                logger.debug(
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
            fetch_cap = min(
                max(rule_limit, rule_limit * SCAN_CATCHUP_FACTOR),
                SCAN_CATCHUP_MAX,
            )
            page_limit = min(rule_limit, fetch_cap)
            try:
                if last_id:
                    # 增量：先取一页，再按 max_id 继续取，直到追平或到单轮上限。
                    # 频道发帖速度高于 scan_limit 时，旧逻辑每轮只前进一页，
                    # 游标会长期落后；这里一次多追几页，但仍保留上限。
                    messages = list(await client.get_messages(
                        entity, limit=page_limit, min_id=last_id, reverse=True,
                    ) or [])
                    while len(messages) < fetch_cap and len(messages) >= page_limit:
                        current_max = max(
                            int(getattr(message, "id", 0) or 0)
                            for message in messages
                        )
                        batch = list(await client.get_messages(
                            entity,
                            limit=min(page_limit, fetch_cap - len(messages)),
                            min_id=current_max,
                            reverse=True,
                        ) or [])
                        if not batch:
                            break
                        batch_max = max(
                            int(getattr(message, "id", 0) or 0)
                            for message in batch
                        )
                        if batch_max <= current_max:
                            # 防御：接口忽略 min_id 时避免原地打转。
                            break
                        messages.extend(batch)
                    messages = messages[:fetch_cap]
                else:
                    # 首次：取最新 N 条（Telethon 默认倒序），再翻转成升序
                    messages = list(reversed(await client.get_messages(
                        entity, limit=rule_limit,
                    )))
            except Exception as error:  # noqa: BLE001
                errors.append(f"{channel_title}: 读取消息失败（{error}）")
                continue
            messages = list(messages or [])
            if len(messages) >= fetch_cap:
                state[f"backlog:{raw_channel}"] = min(backlog + 1, 2)
            else:
                state.pop(f"backlog:{raw_channel}", None)
            scanned += len(messages)
            rule_debug.append({
                "channel": raw_channel,
                "channel_key": channel_key,
                "start_id": last_id,
                "messages": len(messages),
                "fetch_cap": fetch_cap,
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
                auto_candidates.append((item, dict(rule)))
            state[channel_key] = max_id
            state[f"{channel_key}:logic_version"] = SCAN_LOGIC_VERSION
        items = self._trim_items(items)
        # 旧条目补判定：旧版本留下的 new/pending 条目重新走一次匹配，
        # 已转存/已忽略/在队列里的条目不碰。
        candidate_ids = {str(item.get("id")) for item, _rule in auto_candidates}
        rules_by_channel = {
            str(rule.get("channel") or ""): rule
            for rule in self._channel_rules
            if rule.get("enabled", True)
        }
        for item in items:
            item_id = str(item.get("id"))
            if item_id in candidate_ids:
                continue
            if str(item.get("status") or "") not in ("new", "pending"):
                continue
            rule = rules_by_channel.get(str(item.get("channel") or ""))
            if not rule:
                continue
            auto_candidates.append((item, dict(rule)))
        # 画质标签是 1.7.14 才写入的新字段；旧版扫描留下的
        # skipped/transferred 条目不会进入重新匹配，因此永远缺字段。
        # 在保存前统一补齐，前端无需重扫即可显示历史记录画质。
        self._fill_missing_quality(items)
        auto_stats, to_queue = self._match_and_enqueue(auto_candidates)
        # 判定会打上 ignored/queued 标记，先落盘再入队：
        # 否则 worker 可能先拿到 id、读到的还是 new，条目就永远停住了。
        items = self._persist_scan_items(items, state)
        for item in to_queue:
            self._enqueue_item(item)
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
            "auto_queued": auto_stats["queued"],
            "auto_ignored": auto_stats["ignored"],
            "auto_failed": auto_stats["failed"],
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

    def _quality_candidate_key(
            self, item: Dict[str, Any], matched: Dict[str, Any],
    ) -> Optional[Tuple[int, str, int, int]]:
        """同一订阅、同季同集的候选才互相比较画质。"""
        parsed = matched.get("parsed") or {}
        sub = matched.get("matched") or {}
        try:
            subscribe_id = int(sub.get("id") or 0)
        except (TypeError, ValueError):
            return None
        if subscribe_id <= 0:
            return None
        episode = parsed.get("episode")
        if episode is not None:
            try:
                season = int(
                    parsed.get("season")
                    or sub.get("season")
                    or 1
                )
                return (subscribe_id, "episode", season, int(episode))
            except (TypeError, ValueError):
                return None
        if str(parsed.get("media_type") or "") == "movie":
            return (subscribe_id, "movie", 0, 0)
        # 剧集整包没有集号时不参与同集比较，避免把整季包和单集混在一起。
        return None

    def _select_quality_candidates(
            self, candidates: List[Tuple[dict, dict]],
            subscriptions: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Dict[str, Any]]:
        """按全局画质偏好挑出同集最优候选，其余标记丢弃。

        Returns:
            ``{item_id: {"matched": ..., "held_reason"/"discard_reason": ...}}``。
            没有画质标签或不参与同集比较的条目不会出现在这里。
        """
        selections: Dict[str, Dict[str, Any]] = {}
        prefs = getattr(self, "_quality_preferences", None) or {}
        if not prefs.get("enabled", True):
            return selections
        groups: Dict[Tuple[int, str, int, int], List[Dict[str, Any]]] = {}
        for item, _rule in candidates:
            matched = match_item(item, subscriptions or [], allowed_ids=None)
            if not matched.get("matched"):
                continue
            quality = parse_quality(item.get("title"), prefs)
            item["quality"] = quality
            if not quality.get("rankable"):
                # 默认放行：识别不到分辨率就不参与排序，也不阻塞。
                continue
            key = self._quality_candidate_key(item, matched)
            if not key:
                continue
            groups.setdefault(key, []).append({
                "item": item,
                "matched": matched,
                "quality": quality,
            })
        for rows in groups.values():
            if len(rows) < 2:
                continue
            rows.sort(key=lambda row: (
                row["quality"].get("rank_key") or [],
                -int(row["item"].get("published_at")
                     or row["item"].get("created_at") or 0),
                str(row["item"].get("id") or ""),
            ))
            best = rows[0]
            best_label = str(best["quality"].get("label") or "未知")
            best_title = str(best["item"].get("title") or "")[:48]
            for row in rows:
                item_id = str(row["item"].get("id") or "")
                if not item_id:
                    continue
                if row is best:
                    selections[item_id] = {
                        "matched": row["matched"],
                        "held_reason": "",
                    }
                else:
                    selections[item_id] = {
                        "matched": row["matched"],
                        "discard_reason": (
                            f"同集已有更优画质：{best_label}（{best_title}）"
                        ),
                    }
        return selections

    def _trim_items(self, items: List[dict]) -> List[dict]:
        """按状态分层裁剪池子，避免历史记录把待处理条目挤出去。

        - ignored（白名单外噪音）只留最近 ``UNMATCHED_KEEP`` 条，供补匹配；
        - skipped/transferred 最多占 ``max_items * HISTORY_QUOTA_RATIO``；
        - 其余名额全部留给 new/pending。
        """
        if not items:
            return items
        cap = max(50, int(self._max_items or 500))
        history_cap = max(20, int(cap * HISTORY_QUOTA_RATIO))

        def _ts(row):
            return int(row.get("created_at") or 0)

        ignored = [row for row in items if row.get("status") == "ignored"]
        history = [
            row for row in items
            if row.get("status") in ("skipped", "transferred")
        ]
        active = [
            row for row in items
            if row.get("status") not in ("ignored", "skipped", "transferred")
        ]
        if len(ignored) > UNMATCHED_KEEP:
            ignored = sorted(ignored, key=_ts, reverse=True)[:UNMATCHED_KEEP]
        if len(history) > history_cap:
            history = sorted(history, key=_ts, reverse=True)[:history_cap]
        merged = active + history + ignored
        merged.sort(key=_ts, reverse=True)
        return merged[:cap]

    # 扫描期间 worker 可能已把条目写成这几态，扫描落盘时不能覆盖回去。
    _TERMINAL_STATUSES = frozenset({"transferred", "skipped", "failed"})

    def _persist_scan_items(
            self, items: List[dict], state: Dict[str, Any],
    ) -> List[dict]:
        """把扫描结果并入池子：只更新非终态条目，保留 worker 的转存结果。

        扫描全程可能几分钟（抓消息 + 匹配），期间转存 worker 会写
        transferred/skipped/failed。若这里直接用扫描开始时的快照覆盖，
        刚转存成功/失败的条目会被打回 queued，再也不会被处理（1.7.89 线上
        29 条卡在 queued 就是这个原因）。
        """
        with self._items_lock:
            stored = list(self._store.load(ITEMS_KEY, []) or [])
            by_id = {str(row.get("id")): row for row in stored}
            merged: List[dict] = []
            seen: set = set()
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                current = by_id.get(item_id)
                if current is None:
                    merged.append(item)
                    continue
                if str(current.get("status") or "") in self._TERMINAL_STATUSES:
                    # worker 已经给出结论：保留它的状态与转存字段。
                    merged.append(current)
                    continue
                current.update(item)
                merged.append(current)
            # 扫描期间别处新写入的条目（极少见）也保留，避免被这一次保存吞掉。
            for item_id, current in by_id.items():
                if item_id not in seen:
                    merged.append(current)
            merged = self._trim_items(merged)
            self._store.save(ITEMS_KEY, merged)
            self._store.save(STATE_KEY, state)
            return merged

    def _match_and_enqueue(
            self, candidates: List[Tuple[dict, dict]],
    ) -> Tuple[Dict[str, int], List[dict]]:
        """按「命中订阅」判定每条候选：命中入队，其余直接忽略。

        返回 ``(统计, 待入队条目)``。真正的入队由调用方在落盘之后执行，
        保证 worker 读到的状态已经是 queued。
        """
        stats = {"queued": 0, "ignored": 0, "failed": 0}
        if not candidates:
            return stats, []
        provider = getattr(self, "_subscribe_provider", None)
        if not callable(provider):
            for item, _rule in candidates:
                item["match_status"] = "pending"
                item["match_reason"] = "未配置订阅列表，无法匹配"[:300]
            stats["failed"] = len(candidates)
            return stats, []
        try:
            subscriptions = list(provider() or [])
        except Exception as error:  # noqa: BLE001 - 读订阅失败不应误转
            logger.warning(f"TG 频道匹配读取订阅失败：{error}")
            for item, _rule in candidates:
                item["match_status"] = "pending"
                item["match_reason"] = f"读取订阅列表失败：{error}"[:300]
            stats["failed"] = len(candidates)
            return stats, []

        selections = self._select_quality_candidates(candidates, subscriptions)
        lack_by_subscribe: Dict[int, int] = {}
        for sub in subscriptions or []:
            try:
                sub_id = int(sub.get("id") or sub.get("subscribe_id") or 0)
                lack = int(sub.get("lack") or 0)
            except (TypeError, ValueError):
                continue
            if sub_id > 0:
                lack_by_subscribe[sub_id] = max(0, lack)
        queue_full = self._transfer_queue.qsize() >= QUEUE_MAX
        # 同订阅同季只留一条：队列里已有的、本批更高分的都算占位，
        # 避免 19 条排队里 4 条都是同一个订阅同一季。
        occupied = self._queued_dedupe_keys()
        best_in_batch: Dict[Tuple, Tuple[float, dict, str]] = {}
        to_queue: List[dict] = []
        for item, _rule in candidates:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            selection = selections.get(item_id) or {}
            discard_reason = str(selection.get("discard_reason") or "")
            if discard_reason:
                item["status"] = "ignored"
                item["match_status"] = "ignored"
                item["match_reason"] = discard_reason[:300]
                stats["ignored"] += 1
                continue
            matched = selection.get("matched") or match_item(
                item, subscriptions, allowed_ids=None
            )
            item["match_candidates"] = [
                {
                    "id": row["id"], "name": row["name"],
                    "season": row["season"], "type": row["type"],
                    "score": row["score"], "reason": row["reason"],
                }
                for row in (matched.get("candidates") or [])
            ]
            item["match_reason"] = str(matched.get("reason") or "")[:300]
            if not matched.get("matched"):
                # 不合适的直接忽略：只留少量样本用于排障，不进待处理列表。
                item["status"] = "ignored"
                item["match_status"] = "unmatched"
                stats["ignored"] += 1
                continue
            subscribe_id = int(matched["matched"]["id"])
            item["match_status"] = "matched"
            item["matched_subscribe_id"] = subscribe_id
            dedupe_key = self._quality_candidate_key(item, matched)
            score = self._candidate_quality_score(item)
            if dedupe_key:
                item["matched_dedupe_key"] = list(dedupe_key)
                if dedupe_key in occupied:
                    item["status"] = "ignored"
                    item["match_reason"] = (
                        "同订阅同集已有条目在队列中，本条不再重复转存"
                    )[:300]
                    stats["ignored"] += 1
                    continue
                previous = best_in_batch.get(dedupe_key)
                if previous is not None and previous[0] >= score:
                    item["status"] = "ignored"
                    item["match_reason"] = (
                        f"同集已有更优候选（{previous[2]}），本条不再转存"
                    )[:300]
                    stats["ignored"] += 1
                    continue
                if previous is not None:
                    # 本批出现更优候选：把先前那条退回忽略，队列里只留更好的
                    previous_item = previous[1]
                    previous_item["status"] = "ignored"
                    previous_item["match_reason"] = (
                        f"同集出现更优候选（{item.get('title') or ''}）"
                    )[:300]
                    if previous_item in to_queue:
                        to_queue.remove(previous_item)
                    stats["queued"] = max(0, stats["queued"] - 1)
                    stats["ignored"] += 1
                best_in_batch[dedupe_key] = (
                    score, item, str(item.get("title") or "")[:48]
                )
            lack = lack_by_subscribe.get(subscribe_id, 0)
            item["queue_priority"] = (
                QUEUE_PRIORITY_MISSING if lack > 0 else QUEUE_PRIORITY_UPGRADE
            )
            item["queue_priority_reason"] = (
                f"订阅还缺 {lack} 集，优先补集" if lack > 0
                else "订阅已齐，仅洗版"
            )
            if not queue_full:
                item["status"] = "queued"
                item["queued_at"] = int(time.time())
                to_queue.append(item)
                stats["queued"] += 1
                if len(to_queue) >= QUEUE_MAX:
                    queue_full = True
                continue
            item["status"] = "ignored"
            item["match_status"] = "matched"
            item["match_reason"] = "转存队列积压（超过上限），本条忽略"[:300]
            stats["ignored"] += 1
        return stats, to_queue

    def _queued_dedupe_keys(self) -> set:
        """已在队列/待转存条目的「同订阅同季同集」键，用于跨批次去重。"""
        try:
            items = self._load_items()
        except Exception as error:  # noqa: BLE001 - 读台账失败只失去去重，不影响入队
            logger.debug(f"读取待转存条目失败，跳过去重：{error}", exc_info=True)
            return set()
        keys = set()
        for item in items or []:
            if item.get("status") not in ("queued", "transferring", "processing"):
                continue
            raw = item.get("matched_dedupe_key")
            if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                continue
            try:
                keys.add((
                    int(raw[0]), str(raw[1]), int(raw[2]), int(raw[3]),
                ))
            except (TypeError, ValueError):
                continue
        return keys

    @staticmethod
    def _candidate_quality_score(item: Dict[str, Any]) -> float:
        """同集内比较用的画质分：识别不到分辨率时按 0 分，先到先得。"""
        quality = item.get("quality") or {}
        rank_key = quality.get("rank_key") or []
        score = 0.0
        for value in rank_key:
            try:
                score = score * 10 - float(value)
            except (TypeError, ValueError):
                continue
        return score

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
            "new_count": sum(
                1 for item in items
                if item.get("status") in ("queued", "new", "pending")
            ),
            "queued_count": sum(
                1 for item in items if item.get("status") == "queued"
            ),
            "queue_pending": int(self._transfer_queue.qsize()),
            "last_scan_at": self._last_scan_at or int(stored.get("last_scan_at") or 0),
            "last_error": self._last_error,
            "scan_limit": self._scan_limit,
            "interval_minutes": max(1, self._interval // 60),
        }

    def _fill_missing_quality(self, items: Iterable[dict]) -> None:
        """为旧条目补画质字段；已有有效标签的不覆盖。"""
        prefs = getattr(self, "_quality_preferences", None)
        for item in items:
            if not isinstance(item, dict):
                continue
            quality = item.get("quality")
            fresh = parse_quality(item.get("title"), prefs)
            if (
                    isinstance(quality, dict)
                    and quality.get("label") not in (None, "", "未知")
            ):
                # 1.7.14 的旧对象只有 label/rank，没有 tags；补展示字段，
                # 但不改已有 label，避免覆盖人工或历史排序结果。
                for key in ("tags", "subtitle", "source", "free"):
                    if key not in quality:
                        quality[key] = fresh.get(key)
                if not quality.get("audio"):
                    quality["audio"] = fresh.get("audio")
                continue
            item["quality"] = fresh

    def list_items(self, status: str = "") -> List[dict]:
        items = self._load_items()
        # 读接口兜底：升级后即使还没触发扫描，历史卡片也能显示画质。
        self._fill_missing_quality(items)
        if status:
            items = [item for item in items if item.get("status") == status]
        return items

    def update_items(self, item_ids: Iterable[str], **changes: Any) -> int:
        wanted = {str(item) for item in item_ids if str(item)}
        if not wanted:
            return 0
        with self._items_lock:
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
        with self._items_lock:
            items = list(self._store.load(ITEMS_KEY, []) or [])
            kept = [item for item in items if str(item.get("id")) not in wanted]
            if len(kept) != len(items):
                self._store.save(ITEMS_KEY, kept)
            removed = len(items) - len(kept)
        for item_id in wanted:
            self._queued_ids.discard(item_id)
        return removed

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
                logger.debug("_login_send_code_async 分支忽略异常", exc_info=True)
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
                logger.debug("_login_submit_code_async 分支忽略异常", exc_info=True)
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
                logger.debug("_login_submit_password_async 分支忽略异常", exc_info=True)
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
            monitor._store = _MonitorStore(owner)
        return monitor
