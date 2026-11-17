"""最小重试索引（Phase 2 v1.13.1）。

重试入口从插件 history 表改读 retry_index 表：失败且可重试的行与跨盘
成功但 landed=False 的行进索引，其余行不进。本模块只做三件事——
按 history 记录算出索引行（纯函数）、history 落库后同步索引、启动时
一次性导入。分类逻辑不动，仍用 core/failure.py 的纯函数。

history 表本身在 v1.13.2 才删，本版两边并行跑：重试入口先查索引，
查不到或索引不可用时回落 history 扫描。
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from .failure import failure_kind_of, failure_retryable_of

#: 索引行状态沿用 history 的中文状态：失败 / 成功（只表示跨盘成功待修复）
#: / 处理中（重试进行中）。
INDEX_STATUS_FAILED = "失败"
INDEX_STATUS_REPAIR = "成功"
INDEX_STATUS_PROCESSING = "处理中"

#: record_id 的身份三元组（与 HistoryService._ensure_history_record_id
#: 同一规则：share_url + file_name 缺一不可，其余三项参与哈希）。
_IDENTITY_KEYS = ("share_url", "file_name", "tmdb_id", "season", "episode")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_int_or_none(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_time_to_epoch(value: Any, fallback: int) -> int:
    """history 记录的 time（本地时间字符串）转 epoch，供 created_at 用。"""
    text = str(value or "").strip()
    if text:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(time.mktime(datetime.strptime(text, pattern).timetuple()))
            except (TypeError, ValueError):
                continue
    return fallback


def retry_record_id(record: Mapping[str, Any]) -> str:
    """算一条 history 记录的重试索引主键。

    与 HistoryService._ensure_history_record_id 同一规则，但不改动原记录。
    """
    record_id = str(record.get("record_id") or "").strip()
    if record_id:
        return record_id
    identity = tuple(str(record.get(key) or "") for key in _IDENTITY_KEYS)
    if not identity[0] or not identity[1]:
        return ""
    return hashlib.sha1("\0".join(identity).encode("utf-8")).hexdigest()


def cross_success_needs_repair(record: Mapping[str, Any]) -> bool:
    """跨盘成功行是否待修复（与 HistoryService._cross_success_needs_repair
    同一规则：landed 明确 False 即待修复；landed 明确 True 即完成；
    landed 未知时按云盘目录/源文件名启发式判断）。"""
    landed = record.get("landed")
    if landed is False:
        return True
    if landed is True:
        return False
    if not str(record.get("cloud_dir") or "").strip():
        return True
    file_name = str(record.get("file_name") or "").strip()
    source_name = str(record.get("source_file_name") or "").strip()
    return bool(source_name) and source_name == file_name


def retry_index_status(
        record: Mapping[str, Any], include_processing: bool = False
) -> Optional[str]:
    """一条 history 记录在重试索引里应有的状态；不该进索引时返回 None。

    只收两类：失败且可重试；跨盘成功但待修复。处理中行只在同步时
    （include_processing=True）返回，用于把已有的索引行翻成处理中，
    启动导入时不收处理中行。
    """
    status = str(record.get("status") or "")
    if status == INDEX_STATUS_FAILED:
        return INDEX_STATUS_FAILED if failure_retryable_of(record) else None
    if (
            status == INDEX_STATUS_REPAIR
            and str(record.get("transfer_mode") or "") == "cross"
            and cross_success_needs_repair(record)
    ):
        return INDEX_STATUS_REPAIR
    if include_processing and status == INDEX_STATUS_PROCESSING:
        return INDEX_STATUS_PROCESSING
    return None


def record_to_index_row(
        record: Mapping[str, Any], status: str, now: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """把一条 history 记录转成重试索引行；算不出 record_id 时返回 None。"""
    record_id = retry_record_id(record)
    if not record_id:
        return None
    current = int(now or time.time())
    landed = record.get("landed")
    return {
        "record_id": record_id,
        "record_time": str(record.get("time") or ""),
        "share_url": str(record.get("share_url") or ""),
        "file_name": str(record.get("file_name") or ""),
        "tmdb_id": _to_int_or_none(record.get("tmdb_id")),
        "title": str(record.get("title") or record.get("file_name") or ""),
        "type": str(record.get("type") or ""),
        "season": _to_int(record.get("season"), 1),
        "episode": _to_int_or_none(record.get("episode")),
        "year": str(record.get("year") or "") or None,
        "source_file_name": str(record.get("source_file_name") or ""),
        "file_size": _to_int(record.get("file_size"), 0),
        "source_sha1": str(record.get("source_sha1") or ""),
        "source_md5": str(record.get("source_md5") or ""),
        "transfer_mode": str(record.get("transfer_mode") or ""),
        "source_drive_key": str(record.get("source_drive_key") or ""),
        "target_drive_key": str(record.get("target_drive_key") or ""),
        "cloud_dir": str(record.get("cloud_dir") or ""),
        "finalize_key": str(record.get("finalize_key") or "") or None,
        "status": status,
        "stage": str(record.get("stage") or ""),
        "failure_kind": failure_kind_of(record),
        "failure_reason": str(
            record.get("failure_reason") or record.get("error") or ""
        ),
        "failure_retryable": bool(failure_retryable_of(record)),
        "landed": landed if landed in (True, False) else None,
        "created_at": _record_time_to_epoch(record.get("time"), current),
        "updated_at": current,
    }


def import_retry_index_from_history(
        repositories: Any,
        records: List[Dict[str, Any]],
        now: Optional[int] = None,
) -> int:
    """启动时一次性导入：只导失败且可重试的行与跨盘成功待修复的行。

    upsert 保证幂等（跑两次不重复）；其余行直接放弃。
    """
    current = int(now or time.time())
    imported = 0
    for record in records or []:
        if not isinstance(record, dict):
            continue
        status = retry_index_status(record, include_processing=False)
        if status is None:
            continue
        row = record_to_index_row(record, status, current)
        if row is None:
            continue
        if repositories.retry_index.upsert_row(row):
            imported += 1
    return imported


def sync_retry_index_from_history(
        repositories: Any,
        records: List[Dict[str, Any]],
        now: Optional[int] = None,
) -> Dict[str, int]:
    """history 落库后同步重试索引。

    失败/待修复行 upsert；处理中行只更新已有的索引行（不新建）；
    仍在 history 里但已不再重试相关的行，删掉它的索引行；
    history 里已没有的行（被裁剪/归档）不动——裁剪掉的失败行仍可重试。
    """
    current = int(now or time.time())
    counts = {"upserted": 0, "updated": 0, "deleted": 0}
    existing = {
        str(row.get("record_id") or "")
        for row in repositories.retry_index.list_all()
    }
    for record in records or []:
        if not isinstance(record, dict):
            continue
        status = retry_index_status(record, include_processing=True)
        record_id = retry_record_id(record)
        if status is None:
            if record_id and record_id in existing:
                counts["deleted"] += repositories.retry_index.delete_by_record_ids(
                    [record_id]
                )
            continue
        if status == INDEX_STATUS_PROCESSING and record_id not in existing:
            continue
        row = record_to_index_row(record, status, current)
        if row is None:
            continue
        if repositories.retry_index.upsert_row(row):
            if record_id in existing:
                counts["updated"] += 1
            else:
                counts["upserted"] += 1
    return counts


def index_row_to_history_dict(row: Mapping[str, Any]) -> Dict[str, Any]:
    """索引行拼成 history 形状的记录 dict，供重试入口在 history 记录
    已被裁剪/删除时使用（字段与 history 记录同名，缺失的展示字段留空）。"""
    row = dict(row or {})
    return {
        "record_id": str(row.get("record_id") or ""),
        "time": str(row.get("record_time") or ""),
        "share_url": str(row.get("share_url") or ""),
        "file_name": str(row.get("file_name") or ""),
        "source_file_name": str(row.get("source_file_name") or ""),
        "source_sha1": str(row.get("source_sha1") or ""),
        "source_md5": str(row.get("source_md5") or ""),
        "file_size": _to_int(row.get("file_size"), 0),
        "tmdb_id": row.get("tmdb_id"),
        "title": str(row.get("title") or ""),
        "type": str(row.get("type") or ""),
        "season": row.get("season"),
        "episode": row.get("episode"),
        "year": str(row.get("year") or ""),
        "status": str(row.get("status") or ""),
        "stage": str(row.get("stage") or ""),
        "failure_kind": str(row.get("failure_kind") or ""),
        "failure_reason": str(row.get("failure_reason") or ""),
        "failure_retryable": row.get("failure_retryable"),
        "transfer_mode": str(row.get("transfer_mode") or ""),
        "source_drive_key": str(row.get("source_drive_key") or ""),
        "target_drive_key": str(row.get("target_drive_key") or ""),
        "cloud_dir": str(row.get("cloud_dir") or ""),
        "finalize_key": str(row.get("finalize_key") or ""),
        "landed": row.get("landed"),
        "image": "",
    }
