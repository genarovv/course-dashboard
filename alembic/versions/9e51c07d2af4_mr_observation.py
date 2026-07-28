"""mr_observation — журнал наблюдений MR (FR-12, ADR-007, И12)

Revision ID: 9e51c07d2af4
Revises: 7c2a91e40b15
Create Date: 2026-07-28
"""

import sqlalchemy as sa

from alembic import op

revision = "9e51c07d2af4"
down_revision = "7c2a91e40b15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mr_observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sync_run_id", sa.String(36), sa.ForeignKey("sync_run.id"), nullable=False),
        sa.Column("repository_id", sa.String(36), sa.ForeignKey("repository.id"), nullable=False),
        sa.Column("mr_number", sa.Integer, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_branch", sa.String(200), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("reviewer_approved", sa.Boolean, nullable=False),
        sa.Column("markers", sa.JSON, nullable=True),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.Column("observed_at", sa.DateTime, nullable=False),
        # И12 — одно наблюдение MR за обход
        sa.UniqueConstraint("sync_run_id", "repository_id", "mr_number"),
    )


def downgrade() -> None:
    op.drop_table("mr_observation")
