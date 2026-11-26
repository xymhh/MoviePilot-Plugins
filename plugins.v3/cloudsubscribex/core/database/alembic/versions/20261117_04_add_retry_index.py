"""新建最小重试索引表 retry_index。

v1.13.1（Phase 2 第 3 步）：重试入口从插件 history 表改读重试索引。
本表只存重试必需的字段（record_id 三键定位 time+share_url+file_name、
媒体身份、执行字段、status/failure_kind/landed），展示类字段一律不存；
启动时从旧 history 一次性导入失败且可重试的行与跨盘成功但 landed=False
的行（幂等）。history 表本身在 v1.13.2 才删，本版两边并行。

Revision ID: 20261117_04
Revises: 20261117_03
Create Date: 2026-11-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20261117_04"
down_revision = "20261117_03"
branch_labels = None
depends_on = None


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "retry_index" in _existing_tables():
        return
    op.create_table(
        "retry_index",
        sa.Column("record_id", sa.String(length=64), nullable=False),
        sa.Column("record_time", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("share_url", sa.String(length=2048), nullable=False, server_default=""),
        sa.Column("file_name", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("season", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("year", sa.String(length=16), nullable=True),
        sa.Column("source_file_name", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_sha1", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("source_md5", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("transfer_mode", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("source_drive_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("target_drive_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("cloud_dir", sa.String(length=2048), nullable=False, server_default=""),
        sa.Column("finalize_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("stage", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("failure_kind", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("failure_retryable", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("landed", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("record_id"),
        sa.Index("ix_retry_identity", "record_time", "share_url", "file_name"),
        sa.Index("ix_retry_status", "status"),
        sa.Index("ix_retry_created", "created_at"),
    )


def downgrade() -> None:
    if "retry_index" in _existing_tables():
        op.drop_table("retry_index")
