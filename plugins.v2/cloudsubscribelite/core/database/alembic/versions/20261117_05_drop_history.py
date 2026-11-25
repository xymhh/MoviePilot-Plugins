"""删除转存历史表 history_records。

v1.13.3（Phase 2 第 4 步）：转存记录页瘦身为失败记录页，失败与待修复
的唯一事实来源改为重试索引 retry_index；HistoryRecord 模型与
HistoryRepository 已同步删除，``init_db`` 的 ``create_all`` 不会再重建
本表；本迁移负责把已有用户库里的旧表删掉。

Revision ID: 20261117_05
Revises: 20261117_04
Create Date: 2026-11-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20261117_05"
down_revision = "20261117_04"
branch_labels = None
depends_on = None


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "history_records" in _existing_tables():
        op.drop_table("history_records")


def downgrade() -> None:
    if "history_records" in _existing_tables():
        return
    op.create_table(
        "history_records",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("record_id", sa.String(length=64), nullable=False),
        sa.Column("sort_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("group_key", sa.String(length=255), nullable=False),
        sa.Column("record_time", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("stage", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("resource_type", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("task_types", sa.String(length=255), nullable=False, server_default="|"),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("file_name", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("media_type", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("tmdb_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("subscribe_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id"),
        sa.Index("ix_history_group_time", "group_key", "record_time"),
        sa.Index(
            "ix_history_group_filter",
            "group_key", "status", "resource_type", "source",
        ),
        sa.Index("ix_history_filter", "status", "resource_type", "source"),
        sa.Index("ix_history_sort", "sort_index"),
    )
