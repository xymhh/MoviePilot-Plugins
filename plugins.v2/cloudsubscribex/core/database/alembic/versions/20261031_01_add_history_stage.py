"""history_records 增加 stage 阶段列。

Revision ID: 20261031_01
Revises:
Create Date: 2026-10-31
"""

from alembic import op
import sqlalchemy as sa

revision = "20261031_01"
down_revision = None
branch_labels = None
depends_on = None


def _has_stage_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if "history_records" not in inspector.get_table_names():
        return True
    return any(
        str(column.get("name") or "") == "stage"
        for column in inspector.get_columns("history_records")
    )


def upgrade() -> None:
    # 老库补列；新库 create_all 已带 stage，保持幂等。
    if not _has_stage_column():
        with op.batch_alter_table("history_records") as batch_op:
            batch_op.add_column(sa.Column(
                "stage", sa.String(length=32), nullable=False,
                server_default="",
            ))


def downgrade() -> None:
    if _has_stage_column() and "history_records" in sa.inspect(
            op.get_bind()).get_table_names():
        with op.batch_alter_table("history_records") as batch_op:
            batch_op.drop_column("stage")
