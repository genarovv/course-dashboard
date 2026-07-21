"""D1 (#12): matrix_builder — матрица «репозиторий × занятие» (ARCHITECTURE §5.3).

AC тикета #12:
  1. GET / показывает матрицу «репозиторий × занятие»
  2. Каждая ячейка: статус (found/partial/not_found)
  3. Partial_reason отображается
  4. «Актуально на ЧЧ:ММ» присутствует
"""

from datetime import datetime, timedelta

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.edge_def import EdgeDef
from app.models.lesson import Lesson
from app.services.matrix_builder import build_matrix


@pytest.fixture()
def session(tmp_path):
    """Сессия поверх БД, созданной alembic upgrade head (с триггерами И5)."""
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed_matrix_data(session: Session):
    """Хелпер: создаёт минимальный набор данных для матрицы (2 repos × 2 lessons × 2 artifacts)."""
    # Lessons
    lesson1 = Lesson(number=1, title="Занятие 1", date=datetime(2026, 1, 10).date())
    lesson2 = Lesson(number=2, title="Занятие 2", date=datetime(2026, 1, 17).date())
    session.add_all([lesson1, lesson2])
    session.flush()

    # ArtifactDefs (interview на занятии 1, prd на занятии 2)
    adef_interview = ArtifactDef(
        lesson_id=lesson1.id, role="interview", expected_pattern="**/interview.md"
    )
    adef_prd = ArtifactDef(
        lesson_id=lesson2.id, role="prd", expected_pattern="**/prd.md"
    )
    session.add_all([adef_interview, adef_prd])
    session.flush()

    # Repos
    repo1 = store.register_repository(
        session, repo_url="https://github.com/student-a/repo", git_host=GitHost.GitHub
    )
    repo2 = store.register_repository(
        session, repo_url="https://gitlab.com/student-b/repo", git_host=GitHost.GitLab
    )
    session.flush()

    # SyncRun + outcomes
    run = store.register_sync_run(session, triggered_by=SyncTrigger.schedule)
    session.flush()

    store.register_sync_outcome(
        session, sync_run_id=run.id, repository_id=repo1.id, outcome=SyncOutcome.ok_changed
    )
    store.register_sync_outcome(
        session, sync_run_id=run.id, repository_id=repo2.id, outcome=SyncOutcome.ok_changed
    )
    session.flush()

    # Snapshots — repo1: interview=found, prd=found; repo2: interview=partial, prd=not_found
    snap1_interview = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo1.id, artifact_def_id=adef_interview.id,
        status=SnapshotStatus.found, content_hash="hash_a1_interview",
    )
    snap1_prd = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo1.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.found, content_hash="hash_a1_prd",
    )
    snap2_interview = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo2.id, artifact_def_id=adef_interview.id,
        status=SnapshotStatus.partial, content_hash="hash_a2_interview",
        partial_reason=["template_copy"],
    )
    snap2_prd = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo2.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.not_found,
    )
    session.flush()

    return {
        "lesson1": lesson1, "lesson2": lesson2,
        "adef_interview": adef_interview, "adef_prd": adef_prd,
        "repo1": repo1, "repo2": repo2,
        "run": run,
        "snap1_interview": snap1_interview, "snap1_prd": snap1_prd,
        "snap2_interview": snap2_interview, "snap2_prd": snap2_prd,
    }


# ── AC#1: матрица «репозиторий × занятие» ──────────────────────────────────


def test_build_matrix_returns_repo_x_lesson_grid(session):
    """AC#1: матрица содержит repos × lessons с ячейками."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    assert "repositories" in matrix
    assert "lessons" in matrix
    assert "cells" in matrix
    assert len(matrix["repositories"]) == 2
    assert len(matrix["lessons"]) == 2

    repo_ids = {r["id"] for r in matrix["repositories"]}
    assert data["repo1"].id in repo_ids
    assert data["repo2"].id in repo_ids

    lesson_numbers = [l["number"] for l in matrix["lessons"]]
    assert lesson_numbers == [1, 2]


def test_build_matrix_lessons_ordered_by_number(session):
    """AC#1: занятия упорядочены по number."""
    _seed_matrix_data(session)
    matrix = build_matrix(session)
    numbers = [l["number"] for l in matrix["lessons"]]
    assert numbers == sorted(numbers)


# ── AC#2: статус ячейки ────────────────────────────────────────────────────


def test_cell_status_found(session):
    """AC#2: ячейка со found-снапшотом имеет статус found."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    cell = matrix["cells"][data["repo1"].id][data["lesson1"].number]
    assert cell["status"] == SnapshotStatus.found


def test_cell_status_partial(session):
    """AC#2: ячейка с partial-снапшотом имеет статус partial."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    cell = matrix["cells"][data["repo2"].id][data["lesson1"].number]
    assert cell["status"] == SnapshotStatus.partial


def test_cell_status_not_found(session):
    """AC#2: ячейка с not_found-снапшотом имеет статус not_found."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    cell = matrix["cells"][data["repo2"].id][data["lesson2"].number]
    assert cell["status"] == SnapshotStatus.not_found


# ── AC#3: partial_reason отображается ───────────────────────────────────────


def test_partial_reason_displayed(session):
    """AC#3: partial_reason присутствует в ячейке с partial."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    cell = matrix["cells"][data["repo2"].id][data["lesson1"].number]
    assert cell["status"] == SnapshotStatus.partial
    assert cell["partial_reason"] == ["template_copy"]


def test_partial_reason_none_for_found(session):
    """AC#3: у found-ячейки partial_reason = None."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    cell = matrix["cells"][data["repo1"].id][data["lesson1"].number]
    assert cell["status"] == SnapshotStatus.found
    assert cell["partial_reason"] is None


# ── AC#4: «Актуально на ЧЧ:ММ» ─────────────────────────────────────────────


def test_timestamp_present(session):
    """AC#4:矩阵 содержит timestamp «актуально на»."""
    data = _seed_matrix_data(session)
    matrix = build_matrix(session)

    assert "as_of" in matrix
    assert isinstance(matrix["as_of"], str)
    # Формат ЧЧ:ММ (HH:MM)
    parts = matrix["as_of"].split(":")
    assert len(parts) == 2
    assert all(p.isdigit() for p in parts)


# ── Edge-кейсы ──────────────────────────────────────────────────────────────


def test_empty_db_returns_empty_matrix(session):
    """Edge: нет данных → пустая матрица (0 repos, 0 lessons, пустой cells)."""
    matrix = build_matrix(session)
    assert matrix["repositories"] == []
    assert matrix["lessons"] == []
    assert matrix["cells"] == {}


def test_archived_repo_excluded(session):
    """Edge: archived_at IS NOT NULL → репозиторий не в матрице."""
    data = _seed_matrix_data(session)
    # Архивируем repo1
    data["repo1"].archived_at = datetime.utcnow()
    session.flush()

    matrix = build_matrix(session)
    repo_ids = {r["id"] for r in matrix["repositories"]}
    assert data["repo1"].id not in repo_ids
    assert data["repo2"].id in repo_ids


def test_latest_snapshot_wins(session):
    """Edge: несколько снапшотов на одну пару (repo, artifact) → берётся последний."""
    data = _seed_matrix_data(session)
    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.schedule)
    session.flush()
    # Второй снапшот для repo1/interview — теперь found→found (хеш изменился)
    new_snap = store.register_snapshot(
        session, sync_run_id=run2.id, repository_id=data["repo1"].id,
        artifact_def_id=data["adef_interview"].id,
        status=SnapshotStatus.found, content_hash="new_hash_interview",
    )
    session.flush()

    matrix = build_matrix(session)
    cell = matrix["cells"][data["repo1"].id][data["lesson1"].number]
    assert cell["content_hash"] == "new_hash_interview"


def test_unchecked_repo_no_outcome(session):
    """Edge: repo без SyncRunRepository → ячейки отсутствуют (repo не в матрице)."""
    repo3 = store.register_repository(
        session, repo_url="https://github.com/student-c/repo", git_host=GitHost.GitHub
    )
    lesson = Lesson(number=10, title="Extra", date=datetime(2026, 2, 1).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(
        lesson_id=lesson.id, role="code", expected_pattern="**/code.py"
    )
    session.add(adef)
    session.flush()

    matrix = build_matrix(session)
    repo_ids = {r["id"] for r in matrix["repositories"]}
    assert repo3.id not in repo_ids
