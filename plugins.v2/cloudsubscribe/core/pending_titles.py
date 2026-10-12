"""片名待选记录：登记渠道里发来的纯片名，等平台候选被用户选定后的搜索再来消费。

1.6.26 及更早：收到纯片名消息立刻识别并创建订阅，同名片名会误建。
1.6.27 起：片名消息只登记候选，消息交回平台（由平台给出候选列表），
只有在用户选定后平台发起的真实搜索里命中该片名，才创建订阅并入队网盘搜索。
"""

import difflib
import re
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

# 记录有效期：从用户发片名到点选通常只隔数秒到数分钟
DEFAULT_TTL_SECONDS = 900.0
# 同时挂起的片名上限，避免长期无人点选时内存无界增长
DEFAULT_MAX_RECORDS = 64
# 模糊同名阈值：覆盖「机智医生生活」vs「机智的医生生活」这类差一字的输入
DEFAULT_FUZZY_MIN_RATIO = 0.85
# 模糊同名只对足够长的片名生效，避免两字名之间误撞
FUZZY_MIN_TITLE_LENGTH = 3

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
            fuzzy_min_ratio: float = DEFAULT_FUZZY_MIN_RATIO,
    ) -> None:
        self._lock = threading.Lock()
        self._records: List[Dict[str, Any]] = []
        self._ttl = float(ttl)
        self._max_records = int(max_records)
        self._fuzzy_min_ratio = float(fuzzy_min_ratio)

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
        """匹配并消费一条记录，三级顺序：TMDB 身份 > 片名同名 > 片名模糊同名。

        用户点选的那一项自带 TMDB 身份与正式片名，所以身份优先；但登记时 TMDB 可能
        识别失败（例如输入比正式片名少一个字），因此补一级「模糊同名」兜底 —— 它只在
        候选类型相容、且两侧片名都够长时生效。宁可漏也不错建：片名对不上就不消费，
        记录留到过期。
        返回的记录带 ``match`` 字段标明命中级别，便于日志核对。
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
            fuzzy_hit: Optional[int] = None
            best_ratio = 0.0
            for index, record in enumerate(self._records):
                if not self._compatible(record, wanted_type):
                    continue
                if wanted_id and record.get("tmdbid") == wanted_id:
                    identity_hit = index
                    break
                record_keys = record.get("keys") or set()
                if title_hit is None and wanted_keys & record_keys:
                    title_hit = index
                    continue
                ratio = self._best_ratio(wanted_keys, record_keys)
                if ratio > best_ratio:
                    best_ratio = ratio
                    fuzzy_hit = index
            index: Optional[int] = None
            reason: Optional[str] = None
            if identity_hit is not None:
                index, reason = identity_hit, "tmdbid"
            elif title_hit is not None:
                index, reason = title_hit, "title"
            elif fuzzy_hit is not None and best_ratio >= self._fuzzy_min_ratio:
                index, reason = fuzzy_hit, "fuzzy"
            if index is None:
                return None
            record = self._records.pop(index)
            record["match"] = reason
            if reason == "fuzzy":
                record["match_ratio"] = round(best_ratio, 3)
            return record

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
    def _best_ratio(wanted_keys: Iterable[Any], record_keys: Iterable[Any]) -> float:
        """两侧片名集合里最相似的一对有多像（0~1），供模糊同名兜底判定。"""
        best = 0.0
        for wanted in wanted_keys:
            wanted_text = str(wanted or "")
            if len(wanted_text) < FUZZY_MIN_TITLE_LENGTH:
                continue
            for candidate in record_keys:
                candidate_text = str(candidate or "")
                if len(candidate_text) < FUZZY_MIN_TITLE_LENGTH:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, wanted_text, candidate_text
                ).ratio()
                if ratio > best:
                    best = ratio
        return best

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
