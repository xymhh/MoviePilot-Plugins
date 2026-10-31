"""统一任务中心只读聚合。

数据来自 ``sync_jobs`` 与 ``history_records``，只做展示层映射：
作业状态不会写回历史库，六个用户状态也保持不变。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database.models import HistoryRecord, SyncJob
from .failure import failure_kind_of, failure_label, status_label_of
from .retry_policy import DEAD_LETTER_PREFIX, is_dead_letter
from .state import (
    STAGE_POSTPROCESSING,
    STAGE_QUEUED,
    STAGE_TRANSFERRING,
    STAGE_VERIFYING,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PENDING_CONFIRM,
    STATUS_PROCESSING,
    stage_label,
)
from .database.sync_job_state import (
    JOB_CANCELED,
    JOB_DONE,
    JOB_FAILED,
    JOB_FINALIZING,
    JOB_NEEDS_ATTENTION,
    JOB_QUEUED,
    JOB_RETRY_WAIT,
    JOB_TRANSFERRING,
    JOB_VERIFYING,
)

MAX_TASKS = 200
_HISTORY_STATUSES = (STATUS_PROCESSING, STATUS_DOWNLOADING, STATUS_PENDING_CONFIRM)
_ACTIVE_JOB_STATES = frozenset({
    JOB_QUEUED, JOB_TRANSFERRING, JOB_VERIFYING, JOB_FINALIZING,
})


_JOB_STATUS_MAP = {
    JOB_QUEUED: STATUS_PROCESSING,
    JOB_TRANSFERRING: STATUS_DOWNLOADING,
    "downloading": STATUS_DOWNLOADING,
    JOB_VERIFYING: STATUS_DOWNLOADING,
    JOB_FINALIZING: STATUS_DOWNLOADING,
    JOB_FAILED: STATUS_FAILED,
    JOB_NEEDS_ATTENTION: STATUS_PENDING_CONFIRM,
    JOB_RETRY_WAIT: STATUS_PROCESSING,
}
_JOB_STAGE_MAP = {
    JOB_QUEUED: STAGE_QUEUED,
    JOB_TRANSFERRING: STAGE_TRANSFERRING,
    "downloading": STAGE_TRANSFERRING,
    JOB_VERIFYING: STAGE_VERIFYING,
    JOB_FINALIZING: STAGE_POSTPROCESSING,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _history_timestamp(record: HistoryRecord) -> int:
    payload = record.payload if isinstance(record.payload, dict) else {}
    for value in (payload.get("updated_at"), record.record_time):
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


def _record_fields(record: HistoryRecord) -> Dict[str, Any]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    return {
        "title": _text(record.title or payload.get("title")),
        "media_type": _text(record.media_type or payload.get("type")),
        "status": _text(record.status),
        "stage": _text(record.stage),
        "target_provider": _text(
            payload.get("target_drive_key") or payload.get("provider")
        ),
        "target_path": _text(payload.get("cloud_dir")),
        "target_name": _text(record.file_name or payload.get("file_name")),
        "failure_kind": failure_kind_of(payload) if record.status == STATUS_FAILED else "",
        "error": _text(payload.get("failure_reason") or payload.get("error"))[:300],
        "created_at": _history_timestamp(record),
        "updated_at": _history_timestamp(record),
        "progress": _progress(payload),
        "history_time": _text(record.record_time),
        "share_url": _text(payload.get("share_url")),
        "file_name": _text(record.file_name or payload.get("file_name")),
        "record_id": _text(record.record_id),
    }


def _job_fields(job: SyncJob) -> Dict[str, Any]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    state = _text(job.state)
    last_error = _text(job.last_error)
    kind_source = dict(payload)
    kind_source.setdefault("failure_reason", last_error)
    kind = failure_kind_of(kind_source) if state in {JOB_FAILED, JOB_RETRY_WAIT} else ""
    return {
        "title": _text(job.result_name or job.source_name),
        "status": _JOB_STATUS_MAP.get(state, ""),
        "stage": _JOB_STAGE_MAP.get(state, ""),
        "target_provider": _text(job.target_provider),
        "target_path": _text(job.target_path),
        "target_name": _text(job.target_name),
        "source_provider": _text(job.source_provider),
        "attempt": _int(job.attempt),
        "max_attempts": _int(job.max_attempts),
        "next_retry_at": _int(job.next_retry_at),
        "dead_letter": is_dead_letter(
            state, last_error, job.attempt, job.max_attempts,
        ),
        "failure_kind": kind,
        "error": last_error[:300],
        "created_at": _int(job.created_at),
        "updated_at": _int(job.updated_at),
        "progress": _progress(payload),
        "job_id": _text(job.job_id),
        "history_id": _text(job.history_id),
        "_state": state,
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
    if task.get("dead_letter") and status == STATUS_FAILED:
        kind = kind or "unknown"
    task["status_label"] = status_label_of(status, kind)
    task["failure_label"] = failure_label(kind) if kind else ""
    task["stage_label"] = stage_label(task.get("stage"))
    actions: List[str] = []
    has_history = bool(
        task.get("record_id") or task.get("history_time")
        or task.get("history_id")
    )
    if status in {STATUS_PENDING_CONFIRM, STATUS_FAILED}:
        # 有历史记录走历史重试；纯作业死信走 /tasks/retry 显式重开。
        if has_history or (task.get("dead_letter") and task.get("job_id")):
            actions.append("retry")
    elif status in {STATUS_PROCESSING, STATUS_DOWNLOADING}:
        actions.append("stop")
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
    job_state = _text(task.get("_state"))
    if state == "active":
        if job_state:
            return job_state in _ACTIVE_JOB_STATES
        return status in {STATUS_PROCESSING, STATUS_DOWNLOADING}
    if state == "waiting":
        return job_state == JOB_RETRY_WAIT or (
            status == STATUS_PROCESSING and bool(task.get("next_retry_at"))
        )
    if state == "failed":
        return status == STATUS_FAILED or bool(task.get("dead_letter"))
    if state == "pending":
        return status == STATUS_PENDING_CONFIRM
    return True


def collect_tasks(
        db: Session,
        state_filter: str = "all",
) -> List[Dict[str, Any]]:
    """聚合同步作业与非终态历史，返回按 updated_at 倒序的任务列表。"""
    tasks: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = {}

    for record in db.scalars(
            select(HistoryRecord)
            .where(HistoryRecord.status.in_(_HISTORY_STATUSES))
    ).all():
        task_key = f"history:{_text(record.record_id) or _int(record.id)}"
        task = tasks.setdefault(task_key, _blank_task(task_key, "history"))
        record_fields = _record_fields(record)
        _merge(task, record_fields, prefer=True)
        task["_history_fields"] = record_fields
        task["kind"] = "history"
        aliases[_text(record.record_id)] = task_key
        aliases[str(record.id)] = task_key

    for job in db.scalars(
            select(SyncJob)
            .where(SyncJob.state.notin_((JOB_DONE, JOB_CANCELED)))
    ).all():
        history_id = _text(job.history_id)
        task_key = aliases.get(history_id) or (
            f"history:{history_id}" if history_id else f"job:{_text(job.job_id)}"
        )
        task = tasks.setdefault(task_key, _blank_task(task_key, "job"))
        _merge(task, _job_fields(job), prefer=True)
        history_fields = task.get("_history_fields")
        if history_fields:
            for key in (
                    "title", "media_type", "status", "stage",
                    "failure_kind", "error",
            ):
                if history_fields.get(key) not in ("", 0, False, None):
                    task[key] = history_fields[key]

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
    "DEAD_LETTER_PREFIX",
    "MAX_TASKS",
    "collect_tasks",
    "is_dead_letter",
]
