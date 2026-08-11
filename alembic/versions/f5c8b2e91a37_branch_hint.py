"""D43 (#69): branch_hint — работа вне дефолтной ветки

Revision ID: f5c8b2e91a37
Revises: e9d23a5c1f04
Create Date: 2026-08-11

Сигнал, не вердикт: канон связности остаётся за дефолтной веткой. Таблица нужна,
чтобы пустая ячейка матрицы не читалась как «студент ничего не сделал», когда
работа лежит в соседней ветке (в форках ЦУ `main` защищена — слить туда работу
студент физически не может).
"""

import sqlalchemy as sa

from alembic import op

revision = "f5c8b2e91a37"
down_revision = "e9d23a5c1f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "branch_hint",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sync_run_id", sa.String(length=36), nullable=False),
        sa.Column("repository_id", sa.String(length=36), nullable=False),
        sa.Column("branch_name", sa.String(length=200), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("head_date", sa.DateTime(), nullable=True),
        sa.Column("artifacts_found", sa.Integer(), nullable=False),
        sa.Column("artifacts_in_default", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repository.id"]),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sync_run_id", "repository_id", "branch_name"),
    )
    op.create_index("ix_branch_hint_sync_run", "branch_hint", ["sync_run_id"])


def downgrade() -> None:
    op.drop_index("ix_branch_hint_sync_run", table_name="branch_hint")
    op.drop_table("branch_hint")
