"""同步作业账本的业务入口。

`SyncJobLedger` 把「建键 → 幂等入队 → CAS 认领 → 完成/失败记账」收在一个
地方，`SyncHandler` 只负责决定何时调用它。它不依赖 MoviePilot，便于单测。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .sync_job_state import (
    JOB_CANCELED,
    JOB_DONE,
    JOB_FAILED,
    JOB_FINALIZING,
    JOB_RETRY_WAIT,
    JOB_TRANSFERRING,
    JOB_VERIFYING,
    build_job_key,
    content_key,
    normalize_name,
    normalize_path,
)
from .sync_jobs import SyncJobRepository


@dataclass(frozen=True)
class SyncJobLease:
    job_key: str
    job_id: str
    owner: str
    reused: bool = False


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
            return SyncJobLease(row.job_key, row.job_id, "", reused=True), ""

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


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["SyncJobLease", "SyncJobLedger"]
