"""#39 (FR-12): MrObservation — журнал наблюдений MR (data-model §1.14, И12, ADR-007)."""

from datetime import datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SyncTrigger


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    yield create_engine(f"sqlite:///{db_path}")


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def _seed(s):
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    return repo, run


def _observe(s, run, repo, number=7, **kw):
    fields = dict(
        sync_run_id=run.id, repository_id=repo.id, mr_number=number,
        title="S5: auth", source_branch="S5-auth", state="opened",
        reviewer_approved=False, markers={"prichina": {"found": False}},
        head_sha="a" * 40, updated_at=datetime(2026, 7, 28, 10, 0),
    )
    fields.update(kw)
    row = store.register_mr_observation(s, **fields)
    s.flush()
    return row


def test_migration_creates_table_and_downgrade_works(tmp_path):
    db_path = tmp_path / "m.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "mr_observation" in inspect(engine).get_table_names()
    engine.dispose()
    # явная ревизия вместо "-1": цепочка миграций растёт (прецедент #39, повтор в T2/#44)
    command.downgrade(cfg, "7c2a91e40b15")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "mr_observation" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    engine.dispose()


def test_register_and_read_back(session):
    repo, run = _seed(session)
    _observe(session, run, repo, state="merged", reviewer_approved=True)

    rows = store.find_mr_observations(session, repo.id, run.id)
    assert len(rows) == 1
    assert rows[0].state == "merged"
    assert rows[0].reviewer_approved is True
    assert rows[0].markers == {"prichina": {"found": False}}


def test_i12_unique_triple(session):
    """И12: не более одного наблюдения MR за обход."""
    repo, run = _seed(session)
    _observe(session, run, repo, number=7)
    with pytest.raises(IntegrityError):
        _observe(session, run, repo, number=7)


def test_same_mr_next_run_is_new_row(session):
    """Журнал: следующий обход — новая строка того же MR (история переживает изменения)."""
    repo, run1 = _seed(session)
    _observe(session, run1, repo, number=7, state="opened")
    run2 = store.register_sync_run(session, triggered_by=SyncTrigger.schedule)
    session.flush()
    _observe(session, run2, repo, number=7, state="merged")

    latest = store.find_latest_mr_observations(session, repo.id)
    assert len(latest) == 1
    assert latest[0].state == "merged"  # последнее наблюдение


def test_latest_observations_pick_newest_run_with_data(session):
    """Последний обход без MR-данных не затирает картину: берётся последний обход, где данные есть."""
    repo, run1 = _seed(session)
    _observe(session, run1, repo, number=7)
    _observe(session, run1, repo, number=8, state="merged")
    store.register_sync_run(session, triggered_by=SyncTrigger.schedule)  # обход без MR-шага
    session.flush()

    latest = store.find_latest_mr_observations(session, repo.id)
    assert {r.mr_number for r in latest} == {7, 8}
