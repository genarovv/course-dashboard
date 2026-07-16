"""init schema — 11 tables + DDL invariants И1,И3,И4,И6,И8,И9,И10,И11

Revision ID: d408a1d4f3e7
Revises:
Create Date: 2026-07-14 19:38:14.816027
"""
import os
import uuid
from collections.abc import Sequence

import bcrypt
import sqlalchemy as sa

from alembic import op

revision: str = 'd408a1d4f3e7'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── system_user ──────────────────────────────────────────────────────
    op.create_table(
        'system_user',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('username', sa.String(100), nullable=False),
        sa.Column('password_hash', sa.String(100), nullable=False),
        sa.Column('failed_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
    )

    # И10 (single-user): сид одной строки при миграции + отсутствие роута создания
    # пользователя (ARCHITECTURE §4). Пароль — из env CD_ADMIN_PASSWORD на момент
    # миграции; если не задан — сентинел '!' (вход невозможен, секрета в коде нет).
    admin_password = os.environ.get("CD_ADMIN_PASSWORD", "")
    password_hash = (
        bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode() if admin_password else "!"
    )
    op.execute(
        sa.text(
            "INSERT INTO system_user (id, username, password_hash, failed_attempts) "
            "VALUES (:id, :username, :password_hash, 0)"
        ).bindparams(
            id=str(uuid.uuid4()),
            username=os.environ.get("CD_ADMIN_USERNAME", "admin"),
            password_hash=password_hash,
        )
    )

    # ── lesson (И10: UNIQUE number) ─────────────────────────────────────
    op.create_table(
        'lesson',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('number', sa.Integer(), nullable=False, unique=True),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
    )

    # ── rubric ──────────────────────────────────────────────────────────
    op.create_table(
        'rubric',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('artifact_role', sa.String(20), nullable=True),
        sa.Column('version', sa.String(20), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('items', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # ── artifact_def ────────────────────────────────────────────────────
    op.create_table(
        'artifact_def',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('lesson_id', sa.String(36), sa.ForeignKey('lesson.id'), nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('expected_pattern', sa.String(200), nullable=False),
        sa.Column('template_relative_path', sa.String(200), nullable=True),
    )

    # ── edge_def (И10: UNIQUE source_role, target_role) ─────────────────
    op.create_table(
        'edge_def',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('source_role', sa.String(20), nullable=False),
        sa.Column('target_role', sa.String(20), nullable=False),
        sa.Column('rubric_id', sa.String(36), sa.ForeignKey('rubric.id'), nullable=False),
        sa.UniqueConstraint('source_role', 'target_role'),
    )

    # ── repository (И6: UNIQUE normalized_repo_url) ─────────────────────
    op.create_table(
        'repository',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('repo_url', sa.String(500), nullable=False),
        sa.Column('normalized_repo_url', sa.String(500), nullable=False, unique=True),
        sa.Column('git_host', sa.String(20), nullable=False),
        sa.Column('default_branch', sa.String(100), nullable=False, server_default='main'),
        sa.Column('added_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
    )

    # ── git_credential (И10: UNIQUE git_host) ───────────────────────────
    op.create_table(
        'git_credential',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('git_host', sa.String(20), nullable=False, unique=True),
        sa.Column('is_valid', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('checked_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # ── sync_run ────────────────────────────────────────────────────────
    op.create_table(
        'sync_run',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('started_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('triggered_by', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='in_progress'),
    )

    # ── sync_run_repository (И11: UNIQUE sync_run_id, repository_id) ────
    op.create_table(
        'sync_run_repository',
        sa.Column('sync_run_id', sa.String(36), sa.ForeignKey('sync_run.id'), primary_key=True),
        sa.Column('repository_id', sa.String(36), sa.ForeignKey('repository.id'), primary_key=True),
        sa.Column('outcome', sa.String(30), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('detail', sa.String(500), nullable=True),
        # И11 держится на составном PK — отдельный UNIQUE избыточен
    )

    # ── artifact_snapshot (И8: CHECK; И9: UNIQUE triple) ────────────────
    op.create_table(
        'artifact_snapshot',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('sync_run_id', sa.String(36), sa.ForeignKey('sync_run.id'), nullable=False),
        sa.Column('repository_id', sa.String(36), sa.ForeignKey('repository.id'), nullable=False),
        sa.Column('artifact_def_id', sa.String(36), sa.ForeignKey('artifact_def.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('partial_reason', sa.JSON(), nullable=True),
        sa.Column('file_path', sa.String(500), nullable=True),
        sa.Column('source_commit_sha', sa.String(40), nullable=True),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('observed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('sync_run_id', 'repository_id', 'artifact_def_id'),
        # И8: согласованность снапшота
        sa.CheckConstraint(
            "((status = 'partial') AND (partial_reason IS NOT NULL) AND (partial_reason != '[]')) OR "
            "((status != 'partial') AND (partial_reason IS NULL OR partial_reason = '[]'))",
            name='ck_snapshot_partial_reason',
        ),
        sa.CheckConstraint(
            "(status IN ('found', 'partial') AND content_hash IS NOT NULL) OR "
            "(status NOT IN ('found', 'partial') AND content_hash IS NULL)",
            name='ck_snapshot_content_hash',
        ),
        sa.CheckConstraint(
            "status != 'not_found' OR (file_path IS NULL AND source_commit_sha IS NULL)",
            name='ck_snapshot_not_found_empty',
        ),
    )

    # ── coherence_verdict (И3: partial unique index) ────────────────────
    op.create_table(
        'coherence_verdict',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('edge_def_id', sa.String(36), sa.ForeignKey('edge_def.id'), nullable=False),
        sa.Column('source_snapshot_id', sa.String(36), sa.ForeignKey('artifact_snapshot.id'), nullable=False),
        sa.Column('target_snapshot_id', sa.String(36), sa.ForeignKey('artifact_snapshot.id'), nullable=False),
        sa.Column('source_content_hash', sa.String(64), nullable=False),
        sa.Column('target_content_hash', sa.String(64), nullable=False),
        sa.Column('rubric_id', sa.String(36), sa.ForeignKey('rubric.id'), nullable=False),
        sa.Column('llm_model', sa.String(100), nullable=False),
        sa.Column('computed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('verdict', sa.String(20), nullable=False),
        sa.Column('deferred_reason', sa.String(20), nullable=True),
        sa.Column('confidence', sa.String(20), nullable=False),
        sa.Column('entities_checked', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('entities_found', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('entities_excluded', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('entities_lost', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('points', sa.JSON(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
    )

    # И3: partial unique index — четвёрка уникальна, deferred исключён
    op.execute(
        "CREATE UNIQUE INDEX idx_quad ON coherence_verdict "
        "(source_content_hash, target_content_hash, rubric_id, llm_model) "
        "WHERE verdict != 'deferred'"
    )

    # ── override (И1: CHECK XOR; И4: partial unique index) ──────────────
    op.create_table(
        'override',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('coherence_verdict_id', sa.String(36), sa.ForeignKey('coherence_verdict.id'), nullable=True),
        sa.Column('step_quality_card_id', sa.String(36), nullable=True),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        # И1: XOR — заполнена ровно одна из двух ссылок
        sa.CheckConstraint(
            "(coherence_verdict_id IS NOT NULL AND step_quality_card_id IS NULL) OR "
            "(coherence_verdict_id IS NULL AND step_quality_card_id IS NOT NULL)",
            name='ck_override_xor',
        ),
    )

    # И4: одна активная отметка (revoked_at IS NULL) на каждую ссылку
    op.execute(
        "CREATE UNIQUE INDEX idx_active_ovr_coherence ON override "
        "(coherence_verdict_id) WHERE revoked_at IS NULL AND coherence_verdict_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_active_ovr_step ON override "
        "(step_quality_card_id) WHERE revoked_at IS NULL AND step_quality_card_id IS NOT NULL"
    )

    # ── И5: append-only триггеры (только на artifact_snapshot и coherence_verdict) ──
    op.execute(sa.text(
        "CREATE TRIGGER trg_snapshot_no_update "
        "BEFORE UPDATE ON artifact_snapshot "
        "BEGIN "
        "SELECT RAISE(ABORT, 'append-only: UPDATE forbidden on artifact_snapshot'); "
        "END"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER trg_snapshot_no_delete "
        "BEFORE DELETE ON artifact_snapshot "
        "BEGIN "
        "SELECT RAISE(ABORT, 'append-only: DELETE forbidden on artifact_snapshot'); "
        "END"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER trg_verdict_no_update "
        "BEFORE UPDATE ON coherence_verdict "
        "BEGIN "
        "SELECT RAISE(ABORT, 'append-only: UPDATE forbidden on coherence_verdict'); "
        "END"
    ))
    op.execute(sa.text(
        "CREATE TRIGGER trg_verdict_no_delete "
        "BEFORE DELETE ON coherence_verdict "
        "BEGIN "
        "SELECT RAISE(ABORT, 'append-only: DELETE forbidden on coherence_verdict'); "
        "END"
    ))


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_verdict_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_verdict_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_no_update")
    op.drop_table('override')
    op.drop_table('coherence_verdict')
    op.drop_table('artifact_snapshot')
    op.drop_table('sync_run_repository')
    op.drop_table('sync_run')
    op.drop_table('git_credential')
    op.drop_table('repository')
    op.drop_table('edge_def')
    op.drop_table('artifact_def')
    op.drop_table('rubric')
    op.drop_table('lesson')
    op.drop_table('system_user')
