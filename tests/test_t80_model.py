"""#80 (FR-14 этап 1): practice_observation — миграция, журнал, store-доступ.

Образец — test_mr_observation.py: append-only журнал, уникальность за обход,
текущее состояние = последняя строка по (repository, check_key).
"""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, PracticeStatus, SyncTrigger


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "t80.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    return path


@pytest.fixture()
def session(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        yield s
    engine.dispose()


def _seed(s):
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
    s.flush()
    return repo, run


def test_migration_creates_practice_observation(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    columns = {c["name"] for c in inspect(engine).get_columns("practice_observation")}
    engine.dispose()
    assert {"id", "sync_run_id", "repository_id", "check_key", "status",
            "evidence", "observed_at"} <= columns


def test_migration_downgrade_drops_table(tmp_path):
    path = tmp_path / "down.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    engine = create_engine(f"sqlite:///{path}")
    assert not inspect(engine).has_table("practice_observation")
    engine.dispose()


def test_unique_check_per_run(session):
    """Одна проверка одного приёма за обход — дубль не проходит."""
    repo, run = _seed(session)
    store.register_practice_observation(
        session, sync_run_id=run.id, repository_id=repo.id, check_key="tests_first",
        status=PracticeStatus.passed, evidence=[{"kind": "mr_commit", "mr_number": 7}],
    )
    session.flush()
    store.register_practice_observation(
        session, sync_run_id=run.id, repository_id=repo.id, check_key="tests_first",
        status=PracticeStatus.failed, evidence=None,
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_last_observation_per_check_key(session):
    """Текущее состояние = последняя строка по (repository, check_key); журнал цел."""
    repo, run1 = _seed(session)
    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
    session.flush()
    store.register_practice_observation(
        session, sync_run_id=run1.id, repository_id=repo.id, check_key="tests_first",
        status=PracticeStatus.failed, evidence=None,
        observed_at=datetime(2026, 8, 10, 10, 0),
    )
    store.register_practice_observation(
        session, sync_run_id=run2.id, repository_id=repo.id, check_key="tests_first",
        status=PracticeStatus.passed, evidence=[{"kind": "mr_commit", "mr_number": 7}],
        observed_at=datetime(2026, 8, 12, 10, 0),
    )
    store.register_practice_observation(
        session, sync_run_id=run2.id, repository_id=repo.id, check_key="deps_pinned",
        status=PracticeStatus.passed, evidence=[{"kind": "path", "path": "uv.lock"}],
        observed_at=datetime(2026, 8, 12, 10, 0),
    )
    session.flush()

    last = store.find_last_practice_observation(session, repo.id, "tests_first")
    assert last.status == PracticeStatus.passed
    assert last.sync_run_id == run2.id

    latest = store.find_last_practice_observations(session, repo.id)
    assert {(o.check_key, o.status) for o in latest} == {
        ("tests_first", PracticeStatus.passed),
        ("deps_pinned", PracticeStatus.passed),
    }


def test_no_observations_empty(session):
    repo, _run = _seed(session)
    assert store.find_last_practice_observation(session, repo.id, "tests_first") is None
    assert store.find_last_practice_observations(session, repo.id) == []
