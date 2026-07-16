"""O1 (#15): matrix_builder — override-фильтрация (FR-10, ARCHITECTURE §5.3)."""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.edge_def import EdgeDef
from app.models.lesson import Lesson
from app.services.matrix_builder import build_matrix


@pytest.fixture()
def session(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed_matrix_data(session):
    """Создаёт минимальный набор: repo, lesson, artifact_def, edge, snapshot, verdict."""
    rubric = store.register_rubric(session, type="edge", version="1.0", text="t")
    lesson = Lesson(number=1, title="l", date=datetime(2026, 1, 1).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="**/prd.md")
    session.add(adef)
    adef2 = ArtifactDef(lesson_id=lesson.id, role="data_model", expected_pattern="**/dm.md")
    session.add(adef2)
    repo = store.register_repository(session, repo_url="https://github.com/u/r", git_host=GitHost.GitHub)
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()

    snap_src = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status="found", content_hash="hs",
    )
    snap_tgt = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef2.id,
        status="found", content_hash="ht",
    )
    edge = EdgeDef(source_role="prd", target_role="data_model", rubric_id=rubric.id)
    session.add(edge)
    session.flush()

    verdict = store.register_verdict(
        session, edge_def_id=edge.id, source_snapshot_id=snap_src.id, target_snapshot_id=snap_tgt.id,
        source_content_hash="hs", target_content_hash="ht", rubric_id=rubric.id,
        llm_model="m", verdict="break", confidence="high", entities_lost=1,
    )
    session.flush()
    return repo, edge, verdict


def test_build_matrix_without_override(session):
    """Матрица без override показывает вердикт как разрыв."""
    repo, edge, verdict = _seed_matrix_data(session)
    mv = build_matrix(session)

    assert len(mv.repositories) == 1
    cells = [c for c in mv.verdict_cells if c.edge.id == edge.id and c.repository.id == repo.id]
    assert len(cells) == 1
    assert cells[0].verdict is verdict
    assert cells[0].is_overridden is False


def test_build_matrix_filters_active_override(session):
    """FR-10: активный override скрывает вердикт из матрицы."""
    repo, edge, verdict = _seed_matrix_data(session)
    store.register_override(session, coherence_verdict_id=verdict.id, reason="тест")
    session.flush()

    mv = build_matrix(session)
    cells = [c for c in mv.verdict_cells if c.edge.id == edge.id and c.repository.id == repo.id]
    assert len(cells) == 1
    assert cells[0].verdict is None
    assert cells[0].is_overridden is True


def test_build_matrix_revoked_override_shows_verdict(session):
    """FR-10: снятый override — вердикт снова виден."""
    repo, edge, verdict = _seed_matrix_data(session)
    ovr = store.register_override(session, coherence_verdict_id=verdict.id, reason="тест")
    session.flush()
    store.update_override_revoked(session, ovr.id)
    session.flush()

    mv = build_matrix(session)
    cells = [c for c in mv.verdict_cells if c.edge.id == edge.id and c.repository.id == repo.id]
    assert len(cells) == 1
    assert cells[0].verdict is verdict
    assert cells[0].is_overridden is False


def test_build_matrix_blind_spot(session):
    """Последний outcome = repo_unavailable → в blind_spot_repo_ids."""
    repo, _edge, _verdict = _seed_matrix_data(session)
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    store.register_sync_outcome(
        session, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.repo_unavailable
    )
    session.flush()

    mv = build_matrix(session)
    assert repo.id in mv.blind_spot_repo_ids
