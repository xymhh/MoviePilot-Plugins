"""``sync_jobs`` 的持久化访问层。

所有状态迁移都走条件更新（CAS），业务层不直接改 ``SyncJob.state``。
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .manager import DbOper, db_query, db_update
from .models import SyncJob
from .sync_job_state import (
    JOB_CANCELED,
    JOB_DONE,
    JOB_FAILED,
    JOB_FINALIZING,
    JOB_QUEUED,
    JOB_RETRY_WAIT,
    JOB_TRANSFERRING,
    JOB_VERIFYING,
    TERMINAL_STATES,
    assert_transition,
)


def _now(value: Optional[int] = None) -> int:
    return int(value if value is not None else time.time())


class SyncJobRepository(DbOper):
    """同步作业账本仓储。"""

    @db_query
    def get(self, job_key: str, db: Session = None) -> Optional[SyncJob]:
        return db.get(SyncJob, str(job_key or ""))

    @db_query
    def get_by_id(self, job_id: str, db: Session = None) -> Optional[SyncJob]:
        return db.scalar(
            select(SyncJob).where(SyncJob.job_id == str(job_id or ""))
        )

    @db_update
    def create_or_get(
            self, values: Mapping[str, Any], db: Session = None,
    ) -> tuple[Optional[SyncJob], bool]:
        """幂等入队：同一 ``job_key`` 只保留第一条，返回 (记录, 是否新建)。"""
        row = dict(values or {})
        job_key = str(row.get("job_key") or "").strip()
        if not job_key:
            raise ValueError("job_key 不能为空")
        if not str(row.get("job_id") or "").strip():
            raise ValueError("job_id 不能为空")

        timestamp = _now(row.get("created_at"))
        row.setdefault("created_at", timestamp)
        row.setdefault("updated_at", timestamp)
        row.setdefault("state", JOB_QUEUED)
        row.setdefault("attempt", 0)
        row.setdefault("max_attempts", 3)
        row.setdefault("content_key", "")
        row.setdefault("payload", {})
        if row["state"] != JOB_QUEUED:
            raise ValueError("新作业必须以 queued 状态入队")
        if int(row["attempt"] or 0) != 0:
            raise ValueError("新作业的 attempt 必须从 0 开始")
        if int(row["max_attempts"] or 0) < 1:
            raise ValueError("max_attempts 必须大于 0")

        statement = (
            sqlite_insert(SyncJob)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["job_key"])
        )
        result = db.execute(statement)
        current = db.get(SyncJob, job_key)
        return current, bool(getattr(result, "rowcount", 0) == 1)

    @db_update
    def claim(
            self,
            job_key: str,
            owner: str,
            ttl_seconds: int = 120,
            now: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """抢占作业；只有 queued/retry_wait 且租约空闲/过期时成功。"""
        timestamp = _now(now)
        ttl = max(1, int(ttl_seconds or 1))
        statement = (
            update(SyncJob)
            .where(
                SyncJob.job_key == str(job_key or ""),
                SyncJob.state.in_((JOB_QUEUED, JOB_RETRY_WAIT)),
                SyncJob.attempt < SyncJob.max_attempts,
                or_(
                    SyncJob.lease_until.is_(None),
                    SyncJob.lease_until < timestamp,
                ),
                or_(
                    SyncJob.next_retry_at == 0,
                    SyncJob.next_retry_at <= timestamp,
                ),
            )
            .values(
                state=JOB_TRANSFERRING,
                lease_owner=str(owner or ""),
                lease_until=timestamp + ttl,
                attempt=SyncJob.attempt + 1,
                next_retry_at=0,
                updated_at=timestamp,
            )
        )
        result = db.execute(statement)
        return bool(getattr(result, "rowcount", 0) == 1)

    @db_update
    def transition(
            self,
            job_key: str,
            from_state: str,
            to_state: str,
            owner: Optional[str] = None,
            now: Optional[int] = None,
            expected_updated_at: Optional[int] = None,
            db: Session = None,
            **fields: Any,
    ) -> bool:
        """按期望状态迁移；``owner`` 非空时同时校验租约持有者。

        ``expected_updated_at`` 用于 done 回查后的 CAS：只有回查时看到的
        那条记录仍未被别人改写，才允许重开/转待处理，避免把刚完成的新结果
        当成旧结果再处理一次。
        """
        assert_transition(from_state, to_state)
        timestamp = _now(now)
        values = dict(fields)
        if "state" in values or "job_key" in values:
            raise ValueError("transition 不允许覆盖 state/job_key")
        values["state"] = to_state
        values.setdefault("updated_at", timestamp)

        if to_state in TERMINAL_STATES or to_state == JOB_RETRY_WAIT:
            values.setdefault("lease_owner", None)
            values.setdefault("lease_until", None)
        if to_state in TERMINAL_STATES:
            values.setdefault("finished_at", timestamp)

        conditions = [
            SyncJob.job_key == str(job_key or ""),
            SyncJob.state == str(from_state or ""),
        ]
        if owner is not None:
            conditions.append(SyncJob.lease_owner == str(owner))
        if expected_updated_at is not None:
            conditions.append(SyncJob.updated_at == int(expected_updated_at))

        statement = update(SyncJob).where(*conditions).values(**values)
        result = db.execute(statement)
        return bool(getattr(result, "rowcount", 0) == 1)

    @db_update
    def reopen_done(
            self,
            job_key: str,
            reason: str,
            now: Optional[int] = None,
            expected_updated_at: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """把已完成作业重新放回队列（目标文件被删/移走时使用）。

        只允许 ``done -> queued``；清空结果字段并把 ``attempt`` 归零，
        让下一次 claim 重新走完整转存，而不是继承旧的失败次数。
        ``expected_updated_at`` 保证只重开回查时看到的那条 done 记录。
        """
        timestamp = _now(now)
        return self.transition(
            job_key, JOB_DONE, JOB_QUEUED,
            now=timestamp, expected_updated_at=expected_updated_at, db=db,
            attempt=0,
            next_retry_at=0,
            finished_at=None,
            lease_owner=None,
            lease_until=None,
            result_file_id="",
            result_name="",
            result_size=0,
            result_method="",
            last_error=str(reason or "")[:2000],
            last_error_at=timestamp,
        )

    @db_update
    def reopen_canceled(
            self,
            job_key: str,
            reason: str,
            now: Optional[int] = None,
            expected_updated_at: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """把用户主动停止的作业重新放回队列。

        ``canceled`` 表示「用户停掉了这一次」，不是「这个文件永远不要」：
        用户之后重新发起同步时，同一 ``job_key`` 必须能重新转存，否则该内容
        会被永久跳过。重开时把 ``attempt`` 归零 —— 这是用户的一次新意图，
        不该继承上一轮已经用掉的次数。
        """
        timestamp = _now(now)
        return self.transition(
            job_key, JOB_CANCELED, JOB_QUEUED,
            now=timestamp, expected_updated_at=expected_updated_at, db=db,
            attempt=0,
            next_retry_at=0,
            finished_at=None,
            lease_owner=None,
            lease_until=None,
            last_error=str(reason or "")[:2000],
            last_error_at=timestamp,
        )

    @db_update
    def reopen_failed(
            self,
            job_key: str,
            reason: str,
            now: Optional[int] = None,
            expected_updated_at: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """死信人工重开：``failed -> queued``，次数与退避时间全部归零。"""
        timestamp = _now(now)
        return self.transition(
            job_key, JOB_FAILED, JOB_QUEUED,
            now=timestamp, expected_updated_at=expected_updated_at, db=db,
            attempt=0,
            next_retry_at=0,
            finished_at=None,
            lease_owner=None,
            lease_until=None,
            last_error=str(reason or "")[:2000],
            last_error_at=timestamp,
        )

    @db_update
    def reopen_dead_letter(
            self,
            job_key: str,
            reason: str,
            now: Optional[int] = None,
            expected_updated_at: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """人工重开死信：兼容 ``failed`` 与旧数据里耗尽的 ``retry_wait``。"""
        row = self.get(job_key, db=db)
        if row is None or str(row.state) not in (JOB_FAILED, JOB_RETRY_WAIT):
            return False
        timestamp = _now(now)
        return self.transition(
            job_key, str(row.state), JOB_QUEUED,
            now=timestamp, expected_updated_at=expected_updated_at, db=db,
            attempt=0,
            next_retry_at=0,
            finished_at=None,
            lease_owner=None,
            lease_until=None,
            last_error=str(reason or "")[:2000],
            last_error_at=timestamp,
        )

    @db_update
    def heartbeat(
            self,
            job_key: str,
            owner: str,
            ttl_seconds: int = 120,
            now: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """续租；只允许仍在执行的作业续租。"""
        timestamp = _now(now)
        ttl = max(1, int(ttl_seconds or 1))
        statement = (
            update(SyncJob)
            .where(
                SyncJob.job_key == str(job_key or ""),
                SyncJob.lease_owner == str(owner or ""),
                SyncJob.state.in_((JOB_TRANSFERRING, JOB_VERIFYING, JOB_FINALIZING)),
            )
            .values(lease_until=timestamp + ttl, updated_at=timestamp)
        )
        result = db.execute(statement)
        return bool(getattr(result, "rowcount", 0) == 1)


    @db_query
    def has_active_history_job(
            self,
            *,
            history_id: str = "",
            target_path: str = "",
            target_name: str = "",
            now: Optional[int] = None,
            db: Session = None,
    ) -> bool:
        """历史记录是否存在仍在租约内的活动作业（执行态，不含 queued/retry_wait）。"""
        filters = []
        if str(history_id or "").strip():
            filters.append(SyncJob.history_id == str(history_id).strip())
        if str(target_path or "").strip() and str(target_name or "").strip():
            filters.append(and_(
                SyncJob.target_path == str(target_path).strip(),
                SyncJob.target_name == str(target_name).strip(),
            ))
        if not filters:
            return False
        timestamp = _now(now)
        statement = (
            select(SyncJob.job_id)
            .where(
                or_(*filters),
                SyncJob.state.in_((JOB_TRANSFERRING, JOB_VERIFYING, JOB_FINALIZING)),
                or_(SyncJob.lease_until.is_(None), SyncJob.lease_until >= timestamp),
            )
            .limit(1)
        )
        return db.scalar(statement) is not None

    @db_update
    def reclaim_expired(
            self,
            now: Optional[int] = None,
            db: Session = None,
    ) -> int:
        """回收过期租约：有余量回 ``retry_wait``，耗尽则直接 ``failed``。"""
        timestamp = _now(now)
        exhausted = (
            update(SyncJob)
            .where(
                SyncJob.state.in_((JOB_TRANSFERRING, JOB_VERIFYING, JOB_FINALIZING)),
                SyncJob.lease_until.is_not(None),
                SyncJob.lease_until < timestamp,
                SyncJob.attempt >= SyncJob.max_attempts,
            )
            .values(
                state=JOB_FAILED,
                lease_owner=None,
                lease_until=None,
                last_error="作业租约过期，且已达到最大重试次数",
                last_error_at=timestamp,
                finished_at=timestamp,
                updated_at=timestamp,
            )
        )
        retryable = (
            update(SyncJob)
            .where(
                SyncJob.state.in_((JOB_TRANSFERRING, JOB_VERIFYING, JOB_FINALIZING)),
                SyncJob.lease_until.is_not(None),
                SyncJob.lease_until < timestamp,
                SyncJob.attempt < SyncJob.max_attempts,
            )
            .values(
                state=JOB_RETRY_WAIT,
                lease_owner=None,
                lease_until=None,
                last_error="作业租约过期，等待重试",
                last_error_at=timestamp,
                updated_at=timestamp,
            )
        )
        first = db.execute(exhausted)
        second = db.execute(retryable)
        return int(
            (getattr(first, "rowcount", 0) or 0)
            + (getattr(second, "rowcount", 0) or 0)
        )

    @db_update
    def delete_terminal_before(
            self, cutoff: int, db: Session = None,
    ) -> int:
        """清理保留期外的终态作业，避免账本无限增长。"""
        statement = delete(SyncJob).where(
            SyncJob.state.in_(tuple(TERMINAL_STATES)),
            SyncJob.updated_at < int(cutoff or 0),
        )
        result = db.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    @db_query
    def list_by_state(
            self, states: tuple[str, ...], db: Session = None,
    ) -> list[SyncJob]:
        return list(
            db.scalars(
                select(SyncJob)
                .where(SyncJob.state.in_(tuple(states or ())))
                .order_by(SyncJob.updated_at.asc())
            ).all()
        )


__all__ = [
    "SyncJobRepository",
]
