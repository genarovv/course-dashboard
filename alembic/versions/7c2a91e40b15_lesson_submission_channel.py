"""lesson.submission_channel — канал сдачи занятия (FR-12 интерим, ADR-007)

Revision ID: 7c2a91e40b15
Revises: d408a1d4f3e7
Create Date: 2026-07-28
"""

import sqlalchemy as sa

from alembic import op

revision = "7c2a91e40b15"
down_revision = "d408a1d4f3e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lesson") as batch:
        batch.add_column(
            sa.Column("submission_channel", sa.String(10), nullable=False, server_default="files")
        )


def downgrade() -> None:
    with op.batch_alter_table("lesson") as batch:
        batch.drop_column("submission_channel")
