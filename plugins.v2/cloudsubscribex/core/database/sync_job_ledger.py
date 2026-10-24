"""同步作业账本的业务入口。

`SyncJobLedger` 把「建键 → 幂等入队 → CAS 认领 → 完成/失败记账」收在一个
地方，`SyncHandler` 只负责决定何时调用它。它不依赖 MoviePilot，便于单测。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .sync_job_state import (
    JOB_CANCELED,
    JOB_DONE,
    JOB_FAILED,
    JOB_FINALIZING,
    JOB_NEEDS_ATTENTION,
    JOB_RETRY_WAIT,
    JOB_TRANSFERRING,
    JOB_VERIFYING,
    build_job_key,
    content_key,
    normalize_name,
    normalize_path,
)
from .sync_jobs import SyncJobRepository

logger = logging.getLogger(__name__)

# done 回查结果：必须区分「确认存在」「确认不存在」「同名不同内容」
# 「查不清」，不能把查询失败当成文件丢失而重复上传。
DONE_CHECK_EXISTS = "exists"
DONE_CHECK_MISSING = "missing"
DONE_CHECK_MISMATCH = "mismatch"
DONE_CHECK_UNKNOWN = "unknown"
_DONE_CHECKS = frozenset({
    DONE_CHECK_EXISTS, DONE_CHECK_MISSING,
    DONE_CHECK_MISMATCH, DONE_CHECK_UNKNOWN,
})


@dataclass(frozen=True)
class SyncJobLease:
    job_key: str
    job_id: str
    owner: str
    reused: bool = False


@dataclass(frozen=True)
class SyncJobResult:
    """回查 done 目标所需的只读快照，不把 ORM 对象带出事务。"""
    job_key: str
    job_id: str
    target_provider: str
    target_path: str
    target_name: str
    result_file_id: str = ""
    result_name: str = ""
    result_size: int = 0


class SyncJobLedger:
    """同步作业账本的业务封装。"""

    def __init__(self, repository: SyncJobRepository):
        self._repo = repository

    def begin(
            self,
            *,
            target_provider: str,
            target_path: str,
            target_name: str,
            source: Mapping[str, Any],
            source_provider: str = "",
            source_name: str = "",
            subscribe_id: Optional[int] = None,
            history_id: Optional[str] = None,
            payload: Optional[Mapping[str, Any]] = None,
            verify_done: Optional[Callable[[SyncJobResult], str]] = None,
    ) -> tuple[Optional[SyncJobLease], str]:
        """入队并认领；返回 ``(lease, reason)``，认领失败时 lease 为 None。"""
        try:
            normalized_path = normalize_path(target_path)
            normalized_name = normalize_name(target_name)
            key = build_job_key(
                target_provider, normalized_path, normalized_name,
                source, source_provider=source_provider,
            )
            fingerprint = content_key(source, provider=source_provider)
        except (TypeError, ValueError) as error:
            return None, f"作业身份无效：{error}"

        job_id = str(uuid.uuid4())
        values = {
            "job_key": key,
            "job_id": job_id,
            "content_key": fingerprint,
            "source_provider": str(source_provider or "")[:64],
            "source_file_id": str(
                source.get("file_id") or source.get("id") or ""
            )[:255],
            "source_name": str(source_name or source.get("name") or "")[:1024],
            "source_size": _safe_int(source.get("size")),
            "source_sha1": str(source.get("sha1") or "")[:40],
            "source_md5": str(source.get("md5") or "")[:32],
            "target_provider": str(target_provider or "").strip().lower()[:64],
            "target_path": normalized_path[:2048],
            "target_name": normalized_name[:1024],
            "subscribe_id": subscribe_id,
            "history_id": str(history_id or "")[:64] or None,
            "payload": dict(payload or {}),
        }
        try:
            row, _ = self._repo.create_or_get(values)
        except Exception as error:
            return None, f"作业入队失败：{error}"
        if row is None:
            return None, "作业入队后未找到记录"
        if row.state == JOB_DONE:
            check = DONE_CHECK_EXISTS
            if verify_done is not None:
                try:
                    check = str(verify_done(_result_view(row)) or "")
                except Exception as error:
                    return None, f"无法确认已完成目标，暂不复用也不重传：{error}"
                if check not in _DONE_CHECKS:
                    check = DONE_CHECK_UNKNOWN
            if check == DONE_CHECK_EXISTS:
                return SyncJobLease(row.job_key, row.job_id, "", reused=True), ""
            if check == DONE_CHECK_UNKNOWN:
                return None, "无法确认已完成目标，暂不复用也不重传"
            if check == DONE_CHECK_MISMATCH:
                try:
                    changed = self._repo.transition(
                        row.job_key, JOB_DONE, JOB_NEEDS_ATTENTION,
                        now=int(time.time()),
                        expected_updated_at=getattr(row, "updated_at", None),
                        last_error="已完成目标同名文件内容/大小不一致",
                        last_error_at=int(time.time()),
                    )
                except Exception as error:
                    return None, f"目标不一致，且标记待处理失败：{error}"
                if not changed:
                    return None, "作业状态已变化，暂不重传也不改状态"
                return None, "已完成目标同名文件内容/大小不一致，已转为待处理"
            # 确认目标文件不存在：清空结果并重新入队，由本次请求继续 claim。
            try:
                self._repo.reopen_done(
                    row.job_key, "目标文件不存在，重新入队",
                    now=int(time.time()),
                    expected_updated_at=getattr(row, "updated_at", None),
                )
            except Exception as error:
                return None, f"目标文件不存在，但重新入队失败：{error}"
            row = self._repo.get(row.job_key)
            if row is None:
                return None, "目标文件不存在，重新入队后未找到作业"
            if row.state == JOB_DONE:
                return None, "目标文件不存在，但作业仍为已完成状态"
            logger.info(
                "同步作业 done 回查：目标文件不存在，已重新入队"
                "（job=%s，target=%s/%s）",
                row.job_key, row.target_path, row.target_name,
            )

        owner = f"{row.job_id}:{uuid.uuid4().hex[:12]}"
        try:
            claimed = self._repo.claim(row.job_key, owner)
        except Exception as error:
            return None, f"作业认领失败：{error}"
        if claimed:
            return SyncJobLease(row.job_key, row.job_id, owner), ""

        current = self._repo.get(row.job_key)
        if current is not None and current.state == JOB_DONE:
            return SyncJobLease(current.job_key, current.job_id, "", reused=True), ""
        # canceled 是「用户停掉了上一次」，不是永久不可用：同内容再次被发起
        # 同步时允许重新入队，否则该文件永远转存不进来（1.7.37）。
        if current is not None and current.state == JOB_CANCELED:
            try:
                reopened = self._repo.reopen_canceled(
                    current.job_key, "用户重新发起同步，重新入队",
                    expected_updated_at=getattr(current, "updated_at", None),
                )
            except Exception as error:
                return None, f"已取消作业重新入队失败：{error}"
            if reopened:
                retry_owner = f"{current.job_id}:{uuid.uuid4().hex[:12]}"
                if self._repo.claim(current.job_key, retry_owner):
                    logger.info(
                        "同步作业已取消，重新入队并认领（job=%s，target=%s/%s）",
                        current.job_key, current.target_path, current.target_name,
                    )
                    return SyncJobLease(
                        current.job_key, current.job_id, retry_owner
                    ), ""
            current = self._repo.get(row.job_key)
        state = current.state if current is not None else "missing"
        return None, f"作业由其他 worker 持有或已终止：{state}"

    def heartbeat(self, lease: SyncJobLease, ttl_seconds: int = 120) -> bool:
        """长转存续租，避免租约过期后同一作业被另一个 worker 抢走。"""
        if lease.reused:
            return True
        return self._repo.heartbeat(
            lease.job_key, lease.owner, ttl_seconds=ttl_seconds,
        )

    def success(
            self,
            lease: SyncJobLease,
            *,
            result_name: str = "",
            result_size: int = 0,
            result_file_id: str = "",
            result_method: str = "legacy_transfer",
            now: Optional[int] = None,
    ) -> bool:
        """把已认领作业推进到 done，并落下结果字段。"""
        if lease.reused:
            return True
        timestamp = int(now if now is not None else time.time())
        if not self._repo.transition(
                lease.job_key, JOB_TRANSFERRING, JOB_VERIFYING,
                owner=lease.owner, now=timestamp,
        ):
            return False
        if not self._repo.transition(
                lease.job_key, JOB_VERIFYING, JOB_FINALIZING,
                owner=lease.owner, now=timestamp,
        ):
            return False
        return self._repo.transition(
            lease.job_key, JOB_FINALIZING, JOB_DONE,
            owner=lease.owner, now=timestamp,
            result_name=str(result_name or "")[:1024],
            result_size=_safe_int(result_size),
            result_file_id=str(result_file_id or "")[:255],
            result_method=str(result_method or "")[:32],
            last_error="",
            last_error_at=None,
        )

    def failure(
            self,
            lease: SyncJobLease,
            *,
            error: str,
            retryable: bool = True,
            canceled: bool = False,
            now: Optional[int] = None,
    ) -> bool:
        """记录失败；可重试进 ``retry_wait``，否则 ``failed``/``canceled``。"""
        if lease.reused:
            return True
        if canceled:
            to_state = JOB_CANCELED
        elif retryable:
            to_state = JOB_RETRY_WAIT
        else:
            to_state = JOB_FAILED
        timestamp = int(now if now is not None else time.time())
        return self._repo.transition(
            lease.job_key, JOB_TRANSFERRING, to_state,
            owner=lease.owner, now=timestamp,
            last_error=str(error or "")[:2000],
            last_error_at=timestamp,
        )


def _result_view(row: Any) -> SyncJobResult:
    return SyncJobResult(
        job_key=str(getattr(row, "job_key", "") or ""),
        job_id=str(getattr(row, "job_id", "") or ""),
        target_provider=str(getattr(row, "target_provider", "") or ""),
        target_path=str(getattr(row, "target_path", "") or ""),
        target_name=str(getattr(row, "target_name", "") or ""),
        result_file_id=str(getattr(row, "result_file_id", "") or ""),
        result_name=str(getattr(row, "result_name", "") or ""),
        result_size=_safe_int(getattr(row, "result_size", 0)),
    )


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DONE_CHECK_EXISTS",
    "DONE_CHECK_MISMATCH",
    "DONE_CHECK_MISSING",
    "DONE_CHECK_UNKNOWN",
    "SyncJobLease",
    "SyncJobLedger",
    "SyncJobResult",
]
