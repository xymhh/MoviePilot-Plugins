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
    text,
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


class RetryIndex(CloudSubscribeBase):
    """最小重试索引（Phase 2 v1.13.1）：只存重试必需的字段。

    展示类字段（海报、评分、来源文本、跳过原因、缓存字段）一律不存。
    record_time / type 沿用转存记录的键名（旧 history 表用 record_time
    存 time 值的既有约定）。行只分三种：失败（status=="失败"）、跨盘成功
    待修复（status=="成功" 且 landed 为 False）、处理中（重试进行中）。
    v1.13.3 起 history 表已删除，本索引是失败与待修复的唯一事实来源。
    """

    __tablename__ = "retry_index"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_time: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    share_url: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    season: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    episode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    year: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    source_file_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_sha1: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    source_md5: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    transfer_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    source_drive_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_drive_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cloud_dir: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    finalize_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    failure_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    failure_retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    landed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_retry_identity", "record_time", "share_url", "file_name"),
        Index("ix_retry_status", "status"),
        Index("ix_retry_created", "created_at"),
    )


#: 漏斗队列在途状态（阶段 1 的持久化在途登记）：同一去重键同一时间
#: 只有一行能处于在途状态，崩溃/重启也不丢登记（设计 §3.1、§5.1）。
#: 去重键：剧集 (media_type, tmdb_id, season, episode)，电影
#: (media_type, tmdb_id, 0, 0)；tmdb_id 为空时不参与唯一约束，
#: 由入队时的 file_id 代码级检查兜底。
FUNNEL_QUEUE_ACTIVE_STATUSES = ("queued", "processing", "failed")


class FunnelQueueRow(CloudSubscribeBase):
    """网盘整理漏斗队列行（v1.29.7 阶段 1）。

    生产者只写队列表（手动提交在 finalize 边界入队），worker 逐行认领
    处理；终态 ``done``/``held`` 释放去重键。``failed`` 是“本轮失败待
    重试”（next_retry_at 未到不跑），``held`` 是“需人工复核”（不再
    自动重试）。pending_key 指向后处理待终态记录（_OFFLINE_PENDING_KEY
    里的键），worker 用它驱动同一条终态提交路径。
    """

    __tablename__ = "funnel_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    episode: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    file_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha1: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    cloud_dir: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    staging_dir: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    staging_name: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    subscribe_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    is_upgrade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replaced_from: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    notification_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    skip_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_data: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    pending_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index(
            "uq_funnel_queue_active_key",
            "media_type", "tmdb_id", "season", "episode",
            unique=True,
            # sqlite_where 只接受 SQL 表达式：纯字符串会在 create_all 编译
            # DDL 时抛 AttributeError（_compiler_dispatch 缺失）。
            sqlite_where=text("status IN ('queued', 'processing', 'failed')"),
        ),
        Index("ix_funnel_queue_status", "status"),
        Index("ix_funnel_queue_pending_key", "pending_key"),
        Index("ix_funnel_queue_due", "status", "next_retry_at"),
    )
