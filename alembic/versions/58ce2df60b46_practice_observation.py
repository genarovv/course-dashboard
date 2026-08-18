"""FR-14 этап 1 (#80): practice_observation — журнал проверок приёмов курса

Revision ID: 58ce2df60b46
Revises: f5c8b2e91a37
Create Date: 2026-08-14

Append-only, как mr_observation: строка фиксирует «как было на момент обхода»,
текущее состояние = последняя строка по (repository, check_key). Сами проверки —
конфиг (config.yaml, practice_checks), не сущности БД, поэтому check_key — строка.

Сгенерировано autogenerate; вручную убран drop ix_branch_hint_sync_run —
индекс написан руками в f5c8b2e91a37 и моделью не объявлен, autogenerate
ошибочно считал его лишним.
"""

import sqlalchemy as sa

from alembic import op

revision = "58ce2df60b46"
down_revision = "f5c8b2e91a37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "practice_observation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sync_run_id", sa.String(length=36), nullable=False),
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("check_key", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repository.id"]),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sync_run_id", "repository_id", "check_key"),
    )


def downgrade() -> None:
    op.drop_table("practice_observation")
