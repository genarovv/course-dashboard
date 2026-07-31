"""D23 (итерация 5): дубли artifact_def — миграция-чистка + guard в реконсиляции.

Спека: plans/доводки-ux-5-2026-07-31.md §D23; решения CEO 2026-07-31:
«на все да» + механизм — ремонт журнала ТОЛЬКО версионированной миграцией
(e9d23a5c1f04, триггеры И5 в runtime не снимаются). Runtime-guard в reconcile
удаляет лишь дубли без наблюдений; дубли со снапшотами — warning.
Негативные: дублей нет — no-op; разные паттерны одной роли — не дубль;
нет эквивалента для вердикта — группа пропускается (провенанс не рвётся).
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.artifact_snapshot import ArtifactSnapshot
from app.services import config_manager

LLM_MODEL = "deepseek-v4-flash"
PRE_DEDUPE_REV = "c7d19a4e5b02"

YAML = """
lessons:
  - number: 5
    title: "PRD"
    date: 2026-06-30
    artifacts:
      - role: prd
        expected_pattern: "product/prd.md"
  - number: 6
    title: "Данные"
    date: 2026-07-02
    artifacts:
      - role: data_model
        expected_pattern: "data-model.md"
"""


def _make_db(tmp_path, revision):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, revision)
    return db_path, cfg


def _seed_dup(s, *, dup_hash="a"):
    """Дубль prd/product/prd.md: старый def (раннее наблюдение) + дубль; вердикт на дубль."""
    config = config_manager.parse_config(YAML)
    config_manager.reconcile(s, config)
    s.flush()
    old = s.scalar(select(ArtifactDef).where(ArtifactDef.role == "prd"))
    dup = ArtifactDef(lesson_id=old.lesson_id, role="prd", expected_pattern="product/prd.md")
    s.add(dup)
    dm = s.scalar(select(ArtifactDef).where(ArtifactDef.role == "data_model"))
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()

    def snap(adef, h, when):
        return store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
            status=SnapshotStatus.found, content_hash=h * 64,
            file_path=adef.expected_pattern, source_commit_sha="c" * 40, observed_at=when,
        )

    snap(old, "a", datetime(2026, 7, 28, 10, 0))
    dup_snap = snap(dup, dup_hash, datetime(2026, 7, 30, 10, 0))
    dm_snap = snap(dm, "b", datetime(2026, 7, 30, 10, 1))
    rubric = store.register_rubric(s, type="edge", version="1.0", text="п")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    s.flush()
    verdict = store.register_verdict(
        s, edge_def_id=edge.id,
        source_snapshot_id=dup_snap.id, target_snapshot_id=dm_snap.id,  # провенанс — на дубль
        source_content_hash=dup_snap.content_hash, target_content_hash=dm_snap.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="ok", confidence="high",
    )
    s.flush()
    return old.id, dup.id, verdict.id


# ── миграция e9d23a5c1f04 — чистка исторических дублей ────────────────────


def test_migration_dedupes_and_repoints_verdict(tmp_path):
    db_path, cfg = _make_db(tmp_path, PRE_DEDUPE_REV)
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        old_id, dup_id, verdict_id = _seed_dup(s)
        s.commit()
    engine.dispose()
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as c:
        left = c.execute(text("SELECT id FROM artifact_def WHERE role='prd'")).fetchall()
        assert [r[0] for r in left] == [old_id]  # остался старейший, дубль удалён
        assert c.execute(text(
            "SELECT COUNT(*) FROM artifact_snapshot WHERE artifact_def_id=:d"
        ), {"d": dup_id}).scalar() == 0
        src = c.execute(text(
            "SELECT s.artifact_def_id, s.content_hash, v.source_content_hash "
            "FROM coherence_verdict v JOIN artifact_snapshot s ON s.id = v.source_snapshot_id "
            "WHERE v.id = :v"
        ), {"v": verdict_id}).fetchone()
        assert src[0] == old_id  # провенанс перепривязан на эквивалент
        assert src[1] == src[2]  # тот же content_hash — четвёрка Б1 не тронута
        # триггеры И5 восстановлены
        trg = {r[0] for r in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )).fetchall()}
        assert {"trg_verdict_no_update", "trg_snapshot_no_delete"} <= trg
    engine.dispose()


def test_migration_failsafe_no_equivalent(tmp_path):
    """Вердикт на снапшот дубля с хешем, которого нет у хранимого, — группа не тронута."""
    db_path, cfg = _make_db(tmp_path, PRE_DEDUPE_REV)
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        _, dup_id, _ = _seed_dup(s, dup_hash="d")  # у old хеш «a», эквивалента «d» нет
        s.commit()
    engine.dispose()
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as c:
        assert c.execute(text(
            "SELECT COUNT(*) FROM artifact_def WHERE id=:d"
        ), {"d": dup_id}).scalar() == 1  # fail-safe: ничего не удалено
    engine.dispose()


# ── runtime-guard в reconcile (триггеры не трогает) ───────────────────────


@pytest.fixture()
def session(tmp_path):
    db_path, _ = _make_db(tmp_path, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def test_reconcile_deletes_snapshotless_dup(session):
    config = config_manager.parse_config(YAML)
    config_manager.reconcile(session, config)
    session.flush()
    prd = session.scalar(select(ArtifactDef).where(ArtifactDef.role == "prd"))
    session.add(ArtifactDef(
        lesson_id=prd.lesson_id, role="prd", expected_pattern="product/prd.md"
    ))
    session.flush()
    summary = config_manager.reconcile(session, config)
    assert summary.artifact_defs_deduped == 1  # без наблюдений — журнал не затронут
    assert len(list(session.scalars(
        select(ArtifactDef).where(ArtifactDef.role == "prd")
    ))) == 1
    assert config_manager.reconcile(session, config).artifact_defs_deduped == 0  # идемпотентно


def test_reconcile_skips_dup_with_snapshots(session):
    _seed_dup(session)
    config = config_manager.parse_config(YAML)
    summary = config_manager.reconcile(session, config)
    assert summary.artifact_defs_deduped == 0  # журнал runtime не трогает — warning
    assert len(list(session.scalars(
        select(ArtifactDef).where(ArtifactDef.role == "prd")
    ))) == 2
    assert len(list(session.scalars(select(ArtifactSnapshot)))) == 3


def test_alternative_patterns_not_a_dup(session):
    config = config_manager.parse_config(YAML)
    config_manager.reconcile(session, config)
    session.flush()
    prd = session.scalar(select(ArtifactDef).where(ArtifactDef.role == "prd"))
    session.add(ArtifactDef(
        lesson_id=prd.lesson_id, role="prd", expected_pattern="REQUIREMENTS.md"
    ))
    session.flush()
    summary = config_manager.reconcile(session, config)
    assert summary.artifact_defs_deduped == 0  # альтернативный путь — не дубль
    assert len(list(session.scalars(
        select(ArtifactDef).where(ArtifactDef.role == "prd")
    ))) == 2
