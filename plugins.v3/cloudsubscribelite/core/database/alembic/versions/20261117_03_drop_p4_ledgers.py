"""删除 P4 两张账本表：candidate_ledger_entries 与 sync_jobs。

v1.13.0（Phase 2 第 1、2 步）删除候选账本与同步账本，模型类已同步删除，
``init_db`` 的 ``create_all`` 不会再重建这两张表；本迁移负责把已有
用户库里的旧表删掉。只删这两张表，不动 ``history_records``。

Revision ID: 20261117_03
Revises: 20261031_02
Create Date: 2026-11-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20261117_03"
down_revision = "20261031_02"
branch_labels = None
depends_on = None

_LEDGER_TABLES = ("candidate_ledger_entries", "sync_jobs")


def _existing_tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    for table in _LEDGER_TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    existing = _existing_tables()
    if "sync_jobs" not in existing:
        op.create_table(
            "sync_jobs",
            sa.Column("job_key", sa.String(length=64), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("content_key", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("source_provider", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("source_file_id", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("source_name", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column("source_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_sha1", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("source_md5", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("target_provider", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("target_path", sa.String(length=2048), nullable=False, server_default="/"),
            sa.Column("target_name", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column("subscribe_id", sa.Integer(), nullable=True),
            sa.Column("history_id", sa.String(length=64), nullable=True),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("next_retry_at", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_until", sa.Integer(), nullable=True),
            sa.Column("result_provider", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("result_file_id", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("result_name", sa.String(length=1024), nullable=False, server_default=""),
            sa.Column("result_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("result_method", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("last_error_at", sa.Integer(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("finished_at", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("job_key"),
            sa.UniqueConstraint("job_id"),
            sa.Index("ix_sync_job_state_lease", "state", "lease_until"),
            sa.Index("ix_sync_job_target", "target_provider", "target_path", "target_name"),
            sa.Index("ix_sync_job_history", "history_id"),
            sa.Index("ix_sync_job_updated", "updated_at"),
        )
    if "candidate_ledger_entries" not in existing:
        op.create_table(
            "candidate_ledger_entries",
            sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
            sa.Column("day", sa.String(length=10), nullable=False),
            sa.Column("ts", sa.String(length=19), nullable=False, server_default=""),
            sa.Column("run_id", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("kind", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("provider", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.Index("ix_candidate_ledger_day", "day"),
        )
