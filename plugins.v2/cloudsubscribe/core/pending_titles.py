"""片名待选记录：登记渠道里发来的纯片名，等平台候选被用户选定后的搜索再来消费。

1.6.26 及更早：收到纯片名消息立刻识别并创建订阅，同名片名会误建。
1.6.27 起：片名消息只登记候选，消息交回平台（由平台给出候选列表），
只有在用户选定后平台发起的真实搜索里命中该片名，才创建订阅并入队网盘搜索。
"""

import re
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

# 记录有效期：从用户发片名到点选通常只隔数秒到数分钟
DEFAULT_TTL_SECONDS = 900.0
# 同时挂起的片名上限，避免长期无人点选时内存无界增长
DEFAULT_MAX_RECORDS = 64

# 归一化：去掉空白、标点与下划线，只比较「名字本身」
_NORMALIZE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_title(value: Any) -> str:
    """把片名归一化成可比对的形式（去空白/标点、统一小写）。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return _NORMALIZE.sub("", text)


def media_type_value(value: Any) -> Optional[str]:
    """把 MediaType 枚举或字符串统一成 ``movie`` / ``tv`` 这样的取值。"""
    text = str(getattr(value, "value", value) or "").strip().lower()
    return text or None


class PendingTitleStore:
    """线程安全的片名登记表；命中即消费，一条记录只会被用一次。"""

    def __init__(
            self,
            ttl: float = DEFAULT_TTL_SECONDS,
            max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        self._lock = threading.Lock()
        self._records: List[Dict[str, Any]] = []
        self._ttl = float(ttl)
        self._max_records = int(max_records)

    # ------------------------------------------------------------ 写入
    def remember(
            self,
            *,
            text: Any,
            title: Any = None,
            year: Any = None,
            mtype: Any = None,
            season: Any = None,
            tmdbid: Any = None,
            userid: Any = None,
            username: Any = None,
            channel: Any = None,
            source: Any = None,
            chat_id: Any = None,
            message_id: Any = None,
    ) -> Dict[str, Any]:
        """登记一条片名候选，返回记录（含 ``id`` 供后续补齐身份）。"""
        normalized_text = str(text or "").strip()
        normalized_title = str(title or normalized_text).strip() or normalized_text
        record: Dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "text": normalized_text,
            "title": normalized_title,
            "year": str(year or "") or None,
            "mtype": media_type_value(mtype),
            "season": self._as_int(season),
            "tmdbid": self._as_int(tmdbid),
            "userid": userid,
            "username": username,
            "channel": channel,
            "source": source,
            "chat_id": chat_id,
            "message_id": message_id,
            "created_at": time.monotonic(),
            "keys": self._keys(normalized_text, normalized_title),
        }
        with self._lock:
            self._records.append(record)
            self._purge_locked(time.monotonic())
            overflow = len(self._records) - self._max_records
            if overflow > 0:
                del self._records[:overflow]
        return record

    def enrich(self, record_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """为已登记记录补齐识别结果（标题/年份/类型/季/身份），不覆盖已有渠道字段。"""
        with self._lock:
            for record in self._records:
                if record.get("id") != record_id:
                    continue
                if "title" in fields and fields["title"]:
                    record["title"] = str(fields["title"]).strip()
                if fields.get("year"):
                    record["year"] = str(fields["year"]).strip()
                if fields.get("mtype"):
                    record["mtype"] = media_type_value(fields["mtype"])
                if fields.get("season") is not None:
                    record["season"] = self._as_int(fields.get("season"))
                if fields.get("tmdbid"):
                    record["tmdbid"] = self._as_int(fields.get("tmdbid"))
                record["keys"] = self._keys(record.get("text"), record.get("title"))
                return record
        return None

    # ------------------------------------------------------------ 读取
    def consume(
            self,
            *,
            titles: Iterable[Any] = (),
            tmdbid: Any = None,
            mtype: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """匹配并消费一条记录：优先 TMDB 身份完全一致，其次片名完全同名。

        同名片名不误建的前提是这里只认「归一化后完全相等」，不做包含匹配。
        """
        wanted_keys = {key for key in (normalize_title(item) for item in titles) if key}
        wanted_id = self._as_int(tmdbid)
        wanted_type = media_type_value(mtype)
        if not wanted_keys and not wanted_id:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            identity_hit: Optional[int] = None
            title_hit: Optional[int] = None
            for index, record in enumerate(self._records):
                if not self._compatible(record, wanted_type):
                    continue
                if wanted_id and record.get("tmdbid") == wanted_id:
                    identity_hit = index
                    break
                if title_hit is None and wanted_keys & (record.get("keys") or set()):
                    title_hit = index
            index = identity_hit if identity_hit is not None else title_hit
            if index is None:
                return None
            return self._records.pop(index)

    def peek(self) -> List[Dict[str, Any]]:
        """返回未过期记录的副本（测试与日志用，不消费）。"""
        with self._lock:
            self._purge_locked(time.monotonic())
            return [dict(record) for record in self._records]

    def drop(self, record_id: str) -> None:
        """主动丢弃一条记录。"""
        with self._lock:
            self._records = [
                record for record in self._records if record.get("id") != record_id
            ]

    def clear(self) -> None:
        """清空全部记录（插件卸载时调用）。"""
        with self._lock:
            self._records = []

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked(time.monotonic())
            return len(self._records)

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _keys(*values: Any) -> set:
        return {key for key in (normalize_title(item) for item in values) if key}

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _compatible(record: Dict[str, Any], wanted_type: Optional[str]) -> bool:
        """电影与剧集同名时不能串台；任一侧未知则放行。"""
        record_type = record.get("mtype")
        if not wanted_type or not record_type:
            return True
        return wanted_type == record_type

    def _purge_locked(self, now: float) -> None:
        if not self._records:
            return
        deadline = now - self._ttl
        self._records = [
            record for record in self._records
            if float(record.get("created_at") or 0) >= deadline
        ]


_STORE = PendingTitleStore()


def get_pending_title_store() -> PendingTitleStore:
    """返回进程内共享的片名登记表。"""
    return _STORE


def reset_pending_title_store() -> PendingTitleStore:
    """重建登记表（测试用）。"""
    global _STORE
    _STORE = PendingTitleStore()
    return _STORE
