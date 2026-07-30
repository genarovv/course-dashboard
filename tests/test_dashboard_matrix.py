"""D1 (#12): GET / рендерит матрицу «репозиторий × занятие» (AC 1, 3, 4 на уровне HTTP).

AC тикета #12:
  1. GET / показывает матрицу «репозиторий × занятие»
  3. Partial_reason отображается
  4. «Актуально на ЧЧ:ММ» присутствует
(AC 2 — статусы ячеек — покрыт на уровне сервиса в test_matrix_builder.py.)
"""

from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session

PASSWORD = "correct-horse"


@pytest.fixture()
def client_and_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    def override_session():
        with Session(engine) as session:
            yield session
            session.commit()

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(engine) -> None:
    """Один репозиторий, одно занятие, partial-снапшот с template_copy."""
    with Session(engine) as session:
        lesson = Lesson(number=1, title="Занятие 1", date=datetime(2026, 1, 10).date())
        session.add(lesson)
        session.flush()
        adef = ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="**/prd.md")
        session.add(adef)
        repo = store.register_repository(
            session, repo_url="https://github.com/s01/proj", git_host=GitHost.GitHub
        )
        run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
        session.flush()
        store.register_snapshot(
            session,
            sync_run_id=run.id,
            repository_id=repo.id,
            artifact_def_id=adef.id,
            status=SnapshotStatus.partial,
            partial_reason=["template_copy"],
            file_path="product/prd.md",
            source_commit_sha="a" * 40,
            content_hash="b" * 64,
        )
        store.register_sync_outcome(
            session, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
        )
        session.commit()


def _login(client):
    return client.post(
        "/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False
    )


def test_dashboard_shows_matrix_with_repo_and_lesson(client_and_engine):
    client, engine = client_and_engine
    _seed(engine)
    _login(client)
    response = client.get("/")
    assert response.status_code == 200
    assert "https://github.com/s01/proj" in response.text  # строка матрицы (AC 1)
    assert "Занятие 1" in response.text  # колонка матрицы (AC 1)


def test_dashboard_shows_partial_reason(client_and_engine):
    client, engine = client_and_engine
    _seed(engine)
    _login(client)
    response = client.get("/")
    # AC 3; требование изменено решением CEO 2026-07-30 (D8): причины — по-русски
    assert "заготовка из шаблона" in response.text


def test_dashboard_shows_as_of_time(client_and_engine):
    client, engine = client_and_engine
    _seed(engine)
    _login(client)
    response = client.get("/")
    assert "Актуально на" in response.text  # AC 4


def test_dashboard_without_login_still_redirects(client_and_engine):
    client, _ = client_and_engine
    assert client.get("/", follow_redirects=False).status_code == 303
