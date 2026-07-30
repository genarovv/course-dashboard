"""source_commit_date: дата головного коммита в снапшоте (D19, #65)

Дело защиты v2: хронология доказывает даты работой студента (дата коммита),
а не моментом обхода. Для старых снапшотов — NULL: UI показывает дату
наблюдения с пометкой «зафиксировано обходом».

Revision ID: c7d19a4e5b02
Revises: b3f1a92c7d10
Create Date: 2026-07-31
"""

import sqlalchemy as sa

from alembic import op

revision = "c7d19a4e5b02"
down_revision = "b3f1a92c7d10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifact_snapshot") as batch:
        batch.add_column(sa.Column("source_commit_date", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifact_snapshot") as batch:
        batch.drop_column("source_commit_date")
