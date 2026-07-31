"""dedupe artifact_def: чистка дублей после смены ключа реконсиляции (D23, итерация 5)

Решение CEO 2026-07-31: ремонт журнала — только версионированной миграцией.
Смена ключа (занятие, роль) → (занятие, роль, паттерн) в пакете «12 артефактов»
оставила по два одинаковых определения у 7 ролей. Остаётся определение с самым
ранним наблюдением; вердикты перепривязываются на эквивалентный снапшот
хранимого определения (тот же content_hash — четвёрка Б1 не меняется);
снапшоты дубля и сам дубль удаляются. Для этого на время миграции снимаются
ровно два append-only триггера И5 и восстанавливаются безусловно.
Группа без эквивалента для вердикта пропускается (fail-safe).

Revision ID: e9d23a5c1f04
Revises: c7d19a4e5b02
Create Date: 2026-07-31
"""

import sqlalchemy as sa

from alembic import op

revision = "e9d23a5c1f04"
down_revision = "c7d19a4e5b02"
branch_labels = None
depends_on = None

_TRG_VERDICT = (
    "CREATE TRIGGER trg_verdict_no_update BEFORE UPDATE ON coherence_verdict "
    "BEGIN SELECT RAISE(ABORT, 'append-only: UPDATE forbidden on coherence_verdict'); END"
)
_TRG_SNAPSHOT = (
    "CREATE TRIGGER trg_snapshot_no_delete BEFORE DELETE ON artifact_snapshot "
    "BEGIN SELECT RAISE(ABORT, 'append-only: DELETE forbidden on artifact_snapshot'); END"
)


def upgrade() -> None:
    bind = op.get_bind()
    dups = bind.execute(sa.text(
        "SELECT lesson_id, role, expected_pattern FROM artifact_def "
        "GROUP BY lesson_id, role, expected_pattern HAVING COUNT(*) > 1"
    )).fetchall()
    if not dups:
        return
    op.execute("DROP TRIGGER IF EXISTS trg_verdict_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_snapshot_no_delete")
    try:
        for lesson_id, role, pattern in dups:
            defs = bind.execute(sa.text(
                "SELECT d.id, (SELECT MIN(s.observed_at) FROM artifact_snapshot s "
                "WHERE s.artifact_def_id = d.id) AS first_seen "
                "FROM artifact_def d "
                "WHERE d.lesson_id = :l AND d.role = :r AND d.expected_pattern = :p "
                "ORDER BY first_seen IS NULL, first_seen, d.id"
            ), {"l": lesson_id, "r": role, "p": pattern}).fetchall()
            keep_id = defs[0][0]
            for dup_id, _ in defs[1:]:
                referenced = bind.execute(sa.text(
                    "SELECT s.id, s.repository_id, s.content_hash FROM artifact_snapshot s "
                    "WHERE s.artifact_def_id = :d AND ("
                    "EXISTS(SELECT 1 FROM coherence_verdict v WHERE v.source_snapshot_id = s.id) "
                    "OR EXISTS(SELECT 1 FROM coherence_verdict v WHERE v.target_snapshot_id = s.id))"
                ), {"d": dup_id}).fetchall()
                repoints, ok = [], True
                for snap_id, repo_id, chash in referenced:
                    equivalent = bind.execute(sa.text(
                        "SELECT id FROM artifact_snapshot WHERE artifact_def_id = :k "
                        "AND repository_id = :r AND content_hash = :h "
                        "ORDER BY observed_at DESC LIMIT 1"
                    ), {"k": keep_id, "r": repo_id, "h": chash}).scalar()
                    if equivalent is None:
                        ok = False  # fail-safe: провенанс вердикта не рвём
                        break
                    repoints.append((snap_id, equivalent))
                if not ok:
                    continue
                for snap_id, equivalent in repoints:
                    bind.execute(sa.text(
                        "UPDATE coherence_verdict SET source_snapshot_id = :e "
                        "WHERE source_snapshot_id = :s"
                    ), {"e": equivalent, "s": snap_id})
                    bind.execute(sa.text(
                        "UPDATE coherence_verdict SET target_snapshot_id = :e "
                        "WHERE target_snapshot_id = :s"
                    ), {"e": equivalent, "s": snap_id})
                bind.execute(sa.text(
                    "DELETE FROM artifact_snapshot WHERE artifact_def_id = :d"
                ), {"d": dup_id})
                bind.execute(sa.text(
                    "DELETE FROM artifact_def WHERE id = :d"
                ), {"d": dup_id})
    finally:
        op.execute(_TRG_VERDICT)
        op.execute(_TRG_SNAPSHOT)


def downgrade() -> None:
    # чистка данных необратима by design; схема не менялась
    pass
