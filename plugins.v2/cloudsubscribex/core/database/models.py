"""CloudSubscribe 独立 SQLite ORM 模型。"""

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Index,
    Integer,
    JSON,
    String,
    Text,
    delete,
    inspect,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class CloudSubscribeBase(DeclarativeBase):
    """私有库模型基类，提供与平台 Base 一致的通用 CRUD 能力。"""

    __abstract__ = True
    __allow_unmapped__ = True
    id: Any
    __name__: str

    def create(self, db: Session):
        db.add(self)
        return self

    @classmethod
    def get(cls, db: Session, identity: Any):
        return db.get(cls, identity)

    def update(self, db: Session, payload: Dict[str, Any]):
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)
        return self

    @classmethod
    def delete(cls, db: Session, identity: Any) -> bool:
        row = db.get(cls, identity)
        if row is None:
            return False
        db.delete(row)
        return True

    @classmethod
    def truncate(cls, db: Session) -> None:
        db.execute(delete(cls))

    @classmethod
    def list(cls, db: Session):
        return list(db.scalars(select(cls)).all())

    def to_dict(self) -> Dict[str, Any]:
        return {
            column.name: getattr(self, column.name, None)
            for column in self.__table__.columns
        }


class HistoryRecord(CloudSubscribeBase):
    __tablename__ = "history_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    sort_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    group_key: Mapped[str] = mapped_column(String(255), nullable=False)
    record_time: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    task_types: Mapped[str] = mapped_column(String(255), nullable=False, default="|")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tmdb_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    season: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    episode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subscribe_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_history_group_time", "group_key", "record_time"),
        Index(
            "ix_history_group_filter",
            "group_key", "status", "resource_type", "source",
        ),
        Index("ix_history_filter", "status", "resource_type", "source"),
        Index("ix_history_sort", "sort_index"),
    )


class OfflinePendingTask(CloudSubscribeBase):
    __tablename__ = "offline_pending_tasks"

    pending_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_offline_task_id", "task_id"),
        Index("ix_offline_status", "status"),
    )


class SyncJob(CloudSubscribeBase):
    """一次「内容 → 目标文件」的持久化作业。

    ``job_key`` 是幂等键；``job_id`` 是贯穿日志/历史/前端的追踪号。
    状态迁移与租约规则见 ``core/database/sync_job_state.py``。
    """

    __tablename__ = "sync_jobs"

    job_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True
    )
    content_key: Mapped[str] = mapped_column(
        String(160), nullable=False, default=""
    )
    source_provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    source_file_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default=""
    )
    source_name: Mapped[str] = mapped_column(
        String(1024), nullable=False, default=""
    )
    source_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    source_sha1: Mapped[str] = mapped_column(
        String(40), nullable=False, default=""
    )
    source_md5: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )
    target_provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    target_path: Mapped[str] = mapped_column(
        String(2048), nullable=False, default="/"
    )
    target_name: Mapped[str] = mapped_column(
        String(1024), nullable=False, default=""
    )
    subscribe_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    history_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    lease_owner: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    lease_until: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    result_provider: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    result_file_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default=""
    )
    result_name: Mapped[str] = mapped_column(
        String(1024), nullable=False, default=""
    )
    result_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    result_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default=""
    )
    last_error: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    last_error_at: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    payload: Mapped[Dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    finished_at: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    __table_args__ = (
        Index("ix_sync_job_state_lease", "state", "lease_until"),
        Index(
            "ix_sync_job_target",
            "target_provider", "target_path", "target_name",
        ),
        Index("ix_sync_job_history", "history_id"),
        Index("ix_sync_job_updated", "updated_at"),
    )


class CheckinHistoryRecord(CloudSubscribeBase):
    __tablename__ = "checkin_history"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    executed_at: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_checkin_provider_time", "provider", "executed_at"),
        Index("ix_checkin_provider_sort", "provider", "sort_index"),
    )


class CheckinScheduleState(CloudSubscribeBase):
    __tablename__ = "checkin_schedule_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schedule_date: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    full_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_providers: Mapped[List[str]] = mapped_column(JSON, default=list)
    completed_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PointBudgetRecord(CloudSubscribeBase):
    __tablename__ = "point_budget_records"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscribe_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_point_budget_provider", "provider"),
    )


class AccountSnapshot(CloudSubscribeBase):
    __tablename__ = "account_snapshots"

    account_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    refreshed_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)


class RuntimeRecord(CloudSubscribeBase):
    """运行期键值对（TG 监控池/游标/状态、STRM 续扫断点等），按命名空间隔离。"""

    __tablename__ = "runtime_records"

    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_key: Mapped[str] = mapped_column(String(191), primary_key=True)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CandidateLedgerEntry(CloudSubscribeBase):
    """候选资源账本：每条候选的去向，按天分桶，可按天清理。"""

    __tablename__ = "candidate_ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    ts: Mapped[str] = mapped_column(String(19), nullable=False, default="")
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_candidate_ledger_day", "day"),
    )


class SourceSha1Cache(CloudSubscribeBase):
    """源文件 SHA1 缓存：秒传未命中后本地文件删掉，哈希留着供下次零流量探测。"""

    __tablename__ = "source_sha1_cache"

    cache_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    sha1: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SourceMissCache(CloudSubscribeBase):
    """秒传未命中负缓存：同一哈希在 TTL 内不再下载整份文件去重试。"""

    __tablename__ = "source_miss_cache"

    cache_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    target_key: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    sha1: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    missed_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AuthSession(CloudSubscribeBase):
    __tablename__ = "auth_sessions"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
