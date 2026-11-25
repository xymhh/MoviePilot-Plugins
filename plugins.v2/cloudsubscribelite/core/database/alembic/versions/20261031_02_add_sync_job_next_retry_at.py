"""sync_jobs 增加 next_retry_at 退避时间列。

Revision ID: 20261031_02
Revises: 20261031_01
Create Date: 2026-10-31
"""

from alembic import op
import sqlalchemy as sa

revision = "20261031_02"
down_revision = "20261031_01"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if "sync_jobs" not in inspector.get_table_names():
        return True
    return any(
        str(column.get("name") or "") == "next_retry_at"
        for column in inspector.get_columns("sync_jobs")
    )


def upgrade() -> None:
    if not _has_column():
        with op.batch_alter_table("sync_jobs") as batch_op:
            batch_op.add_column(sa.Column(
                "next_retry_at", sa.Integer(), nullable=False,
                server_default="0",
            ))


def downgrade() -> None:
    if "sync_jobs" in sa.inspect(op.get_bind()).get_table_names() and _has_column():
        with op.batch_alter_table("sync_jobs") as batch_op:
            batch_op.drop_column("next_retry_at")
