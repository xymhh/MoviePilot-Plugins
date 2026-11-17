"""统一任务中心只读聚合。

数据来自 ``retry_index``（v1.13.3 起 history 表已删）：只列失败、
处理中与跨盘成功待修复行，只做展示层映射，六个用户状态保持不变。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database.models import RetryIndex
from .failure import failure_kind_of, failure_label, status_label_of
from .state import (
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PENDING_CONFIRM,
    STATUS_PROCESSING,
    stage_label,
)
MAX_TASKS = 200
_INDEX_STATUSES = ("处理中", "失败")
# 跨盘成功待修复行（status 成功且 landed False）单独按待确认列出。
_TYPE_NOISE = {"转存", "整理", "订阅", "未知", "其他", "-", "None"}
_FILENAME_HINTS = (
    "2160p", "1080p", "720p", "480p", "web-dl", "webdl", "webrip", "bluray",
    "blu-ray", "hdr", "h.264", "h.265", "h264", "h265", "x264", "x265",
    "hevc", "aac", "dts", "proper", "remux",
)
_EPISODE_PREFIX = re.compile(r"^[Ss]\d{1,2}\s*[Ee]\d{1,3}")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_filename(value: str) -> bool:
    text = _text(value)
    if not text:
        return False
    lowered = text.lower()
    if _EPISODE_PREFIX.match(text):
        return True
    if any(hint in lowered for hint in _FILENAME_HINTS):
        return True
    return lowered.endswith((".mp4", ".mkv", ".ts", ".avi", ".rmvb", ".iso"))


def _media_type(*values: Any) -> str:
    """历史记录的 media_type 有时是「转存」（MP 的类型），不是影视类型。"""
    for value in values:
        text = _text(value)
        if text and text not in _TYPE_NOISE:
            return text
    return ""


def _display_title(title: Any, target_name: Any) -> str:
    """标题优先用剧名；只有原始文件名时退到目标名的「剧名 - SxxEyy」前半段。"""
    primary = _text(title)
    if primary and not _looks_like_filename(primary):
        return primary
    name = _text(target_name)
    if name:
        head = name.split(" - ")[0].strip()
        if head and not _looks_like_filename(head):
            return head
    return primary


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _index_timestamp(row: RetryIndex) -> int:
    """索引行的时间戳：优先 created_at/updated_at（epoch 秒），
    回落 record_time（"%Y-%m-%d %H:%M:%S" 字符串）。"""
    for value in (getattr(row, "updated_at", None), getattr(row, "created_at", None)):
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            return max(0, int(value))
        text = str(value).strip()
        try:
            return max(0, int(float(text)))
        except (TypeError, ValueError):
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(time.mktime(datetime.strptime(text, fmt).timetuple()))
            except (TypeError, ValueError, OverflowError):
                continue
    record_time = str(getattr(row, "record_time", "") or "").strip()
    if record_time:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(time.mktime(datetime.strptime(record_time, fmt).timetuple()))
            except (TypeError, ValueError, OverflowError):
                continue
    return 0


def _progress(payload: Any) -> Dict[str, int]:
    data = payload if isinstance(payload, dict) else {}
    return {
        "transferred": _int(
            data.get("stage_transferred")
            if data.get("stage_transferred") is not None
            else data.get("transferred")
        ),
        "total": _int(
            data.get("stage_total")
            if data.get("stage_total") is not None
            else data.get("total")
        ),
    }


def _record_fields(row: RetryIndex) -> Dict[str, Any]:
    """索引行映射为任务中心字段。状态口径：处理中→处理中，失败→失败，
    成功且 landed False→待确认（跨盘待修复）。"""
    status = _text(row.status)
    if status == "成功" and row.landed is False:
        display_status = STATUS_PENDING_CONFIRM
    elif status == "失败":
        display_status = STATUS_FAILED
    else:
        display_status = STATUS_PROCESSING
    return {
        "title": _display_title(
            row.title,
            row.file_name,
        ),
        "media_type": _media_type(
            row.type,
            "电视剧" if row.type == "电视剧" else "电影",
        ),
        "status": display_status,
        "stage": _text(row.stage),
        "target_provider": _text(row.target_drive_key),
        "target_path": _text(row.cloud_dir),
        "target_name": _text(row.file_name),
        "failure_kind": (
            failure_kind_of({"failure_kind": row.failure_kind, "failure_reason": row.failure_reason})
            if display_status == STATUS_FAILED else ""
        ),
        "error": _text(row.failure_reason)[:300],
        "created_at": _index_timestamp(row),
        "updated_at": _index_timestamp(row),
        "progress": {"transferred": 0, "total": _int(row.file_size)},
        "history_time": _text(row.record_time),
        "share_url": _text(row.share_url),
        "file_name": _text(row.file_name),
        "record_id": _text(row.record_id),
    }


def _blank_task(task_key: str, kind: str) -> Dict[str, Any]:
    return {
        "task_key": task_key,
        "kind": kind,
        "title": "",
        "media_type": "",
        "status": "",
        "status_label": "",
        "stage": "",
        "stage_label": "",
        "target_provider": "",
        "target_path": "",
        "target_name": "",
        "source_provider": "",
        "attempt": 0,
        "max_attempts": 0,
        "next_retry_at": 0,
        "paused": False,
        "dead_letter": False,
        "failure_kind": "",
        "failure_label": "",
        "error": "",
        "created_at": 0,
        "updated_at": 0,
        "progress": {"transferred": 0, "total": 0},
        "actions": [],
        "history_time": "",
        "share_url": "",
        "file_name": "",
        "job_id": "",
        "record_id": "",
        "_state": "",
    }


def _merge(target: Dict[str, Any], source: Dict[str, Any], prefer: bool) -> None:
    for key, value in source.items():
        if key in {"progress", "actions"}:
            continue
        if prefer and value not in ("", 0, False, None):
            target[key] = value
        elif target.get(key) in ("", 0, False, None) and value not in ("", 0, False, None):
            target[key] = value
    source_progress = source.get("progress") or {}
    if any(source_progress.values()) and (
            prefer or not any((target.get("progress") or {}).values())
    ):
        target["progress"] = dict(source_progress)


def _finalize(task: Dict[str, Any]) -> Dict[str, Any]:
    status = _text(task.get("status"))
    kind = _text(task.get("failure_kind"))
    task["status_label"] = status_label_of(status, kind)
    task["failure_label"] = failure_label(kind) if kind else ""
    task["stage_label"] = stage_label(task.get("stage"))
    actions: List[str] = []
    has_history = bool(
        task.get("record_id") or task.get("history_time")
        or task.get("history_id")
    )
    if status in {STATUS_PENDING_CONFIRM, STATUS_FAILED}:
        # 有索引行走索引重试（/history/retry）。
        if has_history:
            actions.append("retry")
    elif status in {STATUS_PROCESSING, STATUS_DOWNLOADING}:
        # 处理中行是后台重试在飞，没有可停的后处理任务，不给 stop。
        pass
    task["actions"] = actions
    task.pop("_state", None)
    task.pop("_history_fields", None)
    if task["progress"] is None:
        task["progress"] = {"transferred": 0, "total": 0}
    return task


def _matches_state(task: Dict[str, Any], state: str) -> bool:
    state = _text(state or "all").lower()
    if state in {"", "all"}:
        return True
    status = _text(task.get("status"))
    if state == "active":
        return status in {STATUS_PROCESSING, STATUS_DOWNLOADING}
    if state == "waiting":
        return status == STATUS_PROCESSING and bool(task.get("next_retry_at"))
    if state == "failed":
        return status == STATUS_FAILED
    if state == "pending":
        return status == STATUS_PENDING_CONFIRM
    if state == "paused":
        return bool(task.get("paused"))
    return True


def collect_tasks(
        db: Session,
        state_filter: str = "all",
) -> List[Dict[str, Any]]:
    """聚合重试索引里的非终态行（失败/处理中/待修复），返回按
    updated_at 倒序的任务列表。"""
    tasks: Dict[str, Dict[str, Any]] = {}

    for row in db.scalars(
            select(RetryIndex)
            .where(
                RetryIndex.status.in_(_INDEX_STATUSES)
                | (
                    (RetryIndex.status == "成功")
                    & (RetryIndex.landed.is_(False))
                )
            )
    ).all():
        task_key = f"retry_index:{_text(row.record_id)}"
        task = tasks.setdefault(task_key, _blank_task(task_key, "retry_index"))
        record_fields = _record_fields(row)
        _merge(task, record_fields, prefer=True)
        task["_history_fields"] = record_fields
        task["kind"] = "retry_index"

    ordered = sorted(
        tasks.values(),
        key=lambda item: (_int(item.get("updated_at")), item.get("task_key", "")),
        reverse=True,
    )
    result = [
        _finalize(task) for task in ordered
        if _matches_state(task, state_filter)
    ]
    return result[:MAX_TASKS]


__all__ = [
    "MAX_TASKS",
    "collect_tasks",
]
