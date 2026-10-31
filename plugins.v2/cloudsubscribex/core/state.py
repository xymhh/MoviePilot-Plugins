"""转存历史状态机与阶段常量。

六个中文状态是历史表、筛选和通知模板共同依赖的稳定契约，禁止改名或新增。
所有状态写入都应经过 :func:`transition`；人工标记成功属于显式入口，走
:func:`transition_manual`，不把「失败/跳过 → 成功」写进通用迁移表。
"""

from __future__ import annotations

from typing import Final

from app.log import logger

STATUS_PROCESSING: Final = "处理中"
STATUS_DOWNLOADING: Final = "下载中"
STATUS_SUCCESS: Final = "成功"
STATUS_FAILED: Final = "失败"
STATUS_SKIPPED: Final = "跳过"
STATUS_PENDING_CONFIRM: Final = "待确认"

STATUSES: Final = frozenset({
    STATUS_PROCESSING,
    STATUS_DOWNLOADING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_PENDING_CONFIRM,
})

#: 阶段使用英文常量落库，前端负责中文展示。
STAGE_QUEUED: Final = "queued"
STAGE_SEARCHING: Final = "searching"
STAGE_TRANSFERRING: Final = "transferring"
STAGE_VERIFYING: Final = "verifying"
STAGE_POSTPROCESSING: Final = "postprocessing"
STAGE_DONE: Final = "done"

STAGES: Final = frozenset({
    STAGE_QUEUED,
    STAGE_SEARCHING,
    STAGE_TRANSFERRING,
    STAGE_VERIFYING,
    STAGE_POSTPROCESSING,
    STAGE_DONE,
})

STAGE_LABELS: Final = {
    STAGE_QUEUED: "排队",
    STAGE_SEARCHING: "搜索",
    STAGE_TRANSFERRING: "转存",
    STAGE_VERIFYING: "校验",
    STAGE_POSTPROCESSING: "后处理",
    STAGE_DONE: "结束",
}

#: 通用迁移表。终态之间互不覆盖；唯一允许的终态退出是「成功 → 待确认」
#: （落盘自检降级），以及「失败 → 处理中/下载中」「跳过 → 处理中」这类
#: 明确的重新执行路径。人工标记成功不在此表，见 transition_manual。
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATUS_PROCESSING: frozenset({
        STATUS_DOWNLOADING, STATUS_SUCCESS, STATUS_FAILED,
        STATUS_SKIPPED, STATUS_PENDING_CONFIRM,
    }),
    STATUS_DOWNLOADING: frozenset({
        STATUS_PROCESSING, STATUS_SUCCESS, STATUS_FAILED,
        STATUS_SKIPPED, STATUS_PENDING_CONFIRM,
    }),
    STATUS_SUCCESS: frozenset({STATUS_PENDING_CONFIRM}),
    STATUS_PENDING_CONFIRM: frozenset({
        STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED,
    }),
    STATUS_FAILED: frozenset({STATUS_PROCESSING, STATUS_DOWNLOADING}),
    STATUS_SKIPPED: frozenset({STATUS_PROCESSING}),
}


def stage_label(stage: object) -> str:
    """返回阶段中文名；空串/未知阶段一律返回空串（页面不显示）。"""
    return STAGE_LABELS.get(str(stage or "").strip(), "")


def _log_rejected(record: dict, to: str, stage: str, reason: str, task_id: str) -> None:
    logger.warning(
        f"task_id={task_id} from={record.get('status') or ''} to={to} "
        f"stage={stage} reason={reason}"
    )


def _write(record: dict, to: str, stage: str | None) -> None:
    record["status"] = to
    if stage:
        record["stage"] = stage


def transition(
        record: dict,
        to: str,
        *,
        stage: str | None = None,
        reason: str = "",
        task_id: str = "",
) -> bool:
    """按迁移表原地更新状态；非法迁移只告警，不改记录、不抛异常。"""
    if not isinstance(record, dict):
        return False
    current = str(record.get("status") or "")
    resolved_stage = str(stage or "")
    if to not in STATUSES:
        _log_rejected(record, str(to), resolved_stage, reason, task_id)
        return False
    if current == to:
        if stage:
            record["stage"] = resolved_stage
        return True
    if to not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        _log_rejected(record, to, resolved_stage, reason, task_id)
        return False
    _write(record, to, stage)
    return True


def transition_manual(
        record: dict,
        to: str,
        *,
        stage: str | None = None,
        reason: str = "",
        task_id: str = "",
) -> bool:
    """人工定案的显式入口：允许任意既有状态 → 成功，不污染通用迁移表。"""
    if not isinstance(record, dict):
        return False
    current = str(record.get("status") or "")
    resolved_stage = str(stage or "")
    if to != STATUS_SUCCESS or current not in STATUSES:
        return transition(
            record, to, stage=stage, reason=reason, task_id=task_id
        )
    if current == to:
        if stage:
            record["stage"] = resolved_stage
        return True
    _write(record, to, stage)
    return True


__all__ = [
    "ALLOWED_TRANSITIONS",
    "STAGES",
    "STAGE_DONE",
    "STAGE_LABELS",
    "STAGE_POSTPROCESSING",
    "STAGE_QUEUED",
    "STAGE_SEARCHING",
    "STAGE_TRANSFERRING",
    "STAGE_VERIFYING",
    "STATUSES",
    "STATUS_DOWNLOADING",
    "STATUS_FAILED",
    "STATUS_PENDING_CONFIRM",
    "STATUS_PROCESSING",
    "STATUS_SKIPPED",
    "STATUS_SUCCESS",
    "stage_label",
    "transition",
    "transition_manual",
]
