"""新建网盘整理漏斗队列表 funnel_queue。

v1.29.7（阶段 1）：手动提交在 finalize 边界入队，worker 逐行认领
处理；去重键 (media_type, tmdb_id, season, episode) 在途状态唯一
（partial unique index，status 在 queued/processing/failed 时），
即持久化的在途登记——崩溃/重启也不丢，同一集同一时间只有一行在途。
电影 season/episode 归一为 0；tmdb_id 为空时不参与唯一约束（SQLite
唯一索引里 NULL 互不冲突），由入队时的 file_id 代码级检查兜底。

Revision ID: 20261121_06
Revises: 20261117_05
Create Date: 2026-11-21
"""

from alembic import op
import sqlalchemy as sa


revision = "20261121_06"
down_revision = "20261117_05"
branch_labels = None
depends_on = None


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "funnel_queue" in _existing_tables():
        return
    op.create_table(
        "funnel_queue",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("media_type", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("season", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("episode", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("file_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("file_name", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha1", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("cloud_dir", sa.String(length=2048), nullable=False, server_default=""),
        sa.Column("staging_dir", sa.String(length=2048), nullable=False, server_default=""),
        sa.Column("staging_name", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("subscribe_id", sa.Integer(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("is_upgrade", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("replaced_from", sa.JSON(), nullable=True),
        sa.Column("notification_kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("skip_reason", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("media_data", sa.JSON(), nullable=True),
        sa.Column("pending_key", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.Index(
            "uq_funnel_queue_active_key",
            "media_type", "tmdb_id", "season", "episode",
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'processing', 'failed')"),
        ),
        sa.Index("ix_funnel_queue_status", "status"),
        sa.Index("ix_funnel_queue_pending_key", "pending_key"),
        sa.Index("ix_funnel_queue_due", "status", "next_retry_at"),
    )


def downgrade() -> None:
    if "funnel_queue" in _existing_tables():
        op.drop_table("funnel_queue")
