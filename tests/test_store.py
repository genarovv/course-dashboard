"""S2 (#3): store.py — контракт «журнал vs состояние» (ARCHITECTURE §3.5)."""

import inspect
from datetime import datetime, timedelta

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SyncOutcome, SyncStatus, SyncTrigger


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


def test_contract_exactly_4_updates_no_deletes():
    """Критерий #3: ровно 4 update_*, delete_* нет вообще."""
    names = [n for n, f in inspect.getmembers(store, inspect.isfunction)]
    updates = sorted(n for n in names if n.startswith("update_"))
    assert updates == [
        "update_credential_validity",
        "update_override_revoked",
        "update_sync_run_status",
        "update_user_lockout",
    ]
    assert not [n for n in names if n.startswith("delete_")]


def test_contract_register_for_journal_entities():
    """Критерий #3: для каждой журнальной сущности — register_*."""
    names = dir(store)
    for fn in (
        "register_rubric", "register_snapshot", "register_verdict",
        "register_sync_outcome", "register_override",
    ):
        assert fn in names, fn


def test_normalize_url():
    assert store.normalize_url("https://GitHub.com/User/Repo.git") == "https://github.com/user/repo"
    assert store.normalize_url("https://github.com/user/repo/") == "https://github.com/user/repo"


def test_register_and_update_flow(session):
    repo = store.register_repository(
        session, repo_url="https://github.com/u/r.git", git_host=GitHost.GitHub
    )
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    credential = store.register_git_credential(session, git_host=GitHost.GitHub)
    session.flush()

    assert repo.normalized_repo_url == "https://github.com/u/r"
    assert run.status == SyncStatus.in_progress
    assert store.find_repository_by_normalized_url(session, "https://github.com/u/r/") is repo
    assert store.find_active_repositories(session) == [repo]
    assert store.find_credential(session, GitHost.GitHub) is credential

    store.register_sync_outcome(
        session, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    store.update_sync_run_status(session, run.id, SyncStatus.completed)
    session.flush()
    assert run.status == SyncStatus.completed
    assert run.completed_at is not None

    store.update_credential_validity(session, credential.id, is_valid=False)
    session.flush()
    assert credential.is_valid is False


def test_user_lockout_update(session):
    user = store.find_user_by_username(session, "admin")  # сид миграции (И10)
    assert user is not None
    until = datetime.utcnow() + timedelta(minutes=15)
    store.update_user_lockout(session, user.id, failed_attempts=5, locked_until=until)
    session.flush()
    assert user.failed_attempts == 5
    assert user.locked_until == until


def test_override_soft_revoke(session, monkeypatch):
    """FR-10: снятие отметки — revoked_at, не удаление."""
    from app.models.artifact_def import ArtifactDef
    from app.models.lesson import Lesson

    rubric = store.register_rubric(session, type="edge", version="1.0", text="t")
    lesson = Lesson(number=1, title="l", date=datetime(2026, 1, 1).date())
    session.add(lesson)
    session.flush()
    adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="**/prd.md")
    session.add(adef)
    repo = store.register_repository(session, repo_url="https://github.com/u/r", git_host=GitHost.GitHub)
    run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    snapshot = store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status="found", content_hash="h1",
    )
    from app.models.edge_def import EdgeDef

    edge = EdgeDef(source_role="prd", target_role="data_model", rubric_id=rubric.id)
    session.add(edge)
    session.flush()
    verdict = store.register_verdict(
        session, edge_def_id=edge.id, source_snapshot_id=snapshot.id, target_snapshot_id=snapshot.id,
        source_content_hash="h1", target_content_hash="h2", rubric_id=rubric.id,
        llm_model="m", verdict="break", confidence="high", entities_lost=1,
    )
    session.flush()

    found = store.find_verdict_by_quadruple(
        session, source_content_hash="h1", target_content_hash="h2",
        rubric_id=rubric.id, llm_model="m",
    )
    assert found is verdict

    override = store.register_override(session, coherence_verdict_id=verdict.id, reason="синоним")
    session.flush()
    assert override.revoked_at is None
    store.update_override_revoked(session, override.id)
    session.flush()
    assert override.revoked_at is not None  # строка жива — мягкое гашение
