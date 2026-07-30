"""D10 (#54), FR-6/FR-7/FR-3: сигнальные блоки в артефактной матрице.

AC (спека §D10): слепая зона, хроники и auth-баннер — те же, что в матрице
занятий (общая логика matrix_builder, не копия); предупреждение при обходе
старше 48 часов. Негативные: пустой реестр — блоки не рисуются; недоступный
репозиторий не судится за тишину (наследуется от логики §5.3).
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
from app.models import GitHost, SyncOutcome, SyncTrigger
from app.models.lesson import Lesson
from app.routes import get_session
from app.services.artifact_matrix import build_artifact_matrix

LLM_MODEL = "deepseek-v4-flash"
PASSWORD = "correct-horse"


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


@pytest.fixture()
def client(engine):
    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _login(client):
    client.post("/login", data={"username": "admin", "password": PASSWORD})


def _seed(s, outcome=SyncOutcome.ok_changed, detail=None):
    lesson = Lesson(number=1, title="Старт", date=datetime(2026, 6, 1).date())
    s.add(lesson)
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=outcome, detail=detail
    )
    s.flush()
    return repo, run


def test_blind_spot_repo_listed(engine):
    with Session(engine) as s:
        repo, _ = _seed(s, outcome=SyncOutcome.repo_unavailable, detail="HTTP 404")
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert {"repo_url": "https://github.com/s/x", "detail": "HTTP 404"} in matrix["blind_spots"]


def test_auth_banner_flag(engine):
    with Session(engine) as s:
        _seed(s, outcome=SyncOutcome.auth_failed)
        s.commit()
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["auth_banner"] is True


def test_chronics_silent_repo(engine):
    """Репозиторий без наблюдений при 2 прошедших занятиях — хроника (как в FR-7)."""
    with Session(engine) as s:
        lesson2 = Lesson(number=2, title="Второе", date=datetime(2026, 6, 8).date())
        s.add(lesson2)
        repo, _ = _seed(s)
        repo.added_at = datetime(2026, 5, 25, 9, 0)  # добавлен до обоих занятий — тишина 2 занятия
        s.commit()
        matrix = build_artifact_matrix(
            s, llm_model=LLM_MODEL, today=datetime(2026, 6, 20).date()
        )
    assert any(c["repo_url"] == "https://github.com/s/x" for c in matrix["chronics"])


def test_empty_registry_no_signal_blocks(engine):
    with Session(engine) as s:
        matrix = build_artifact_matrix(s, llm_model=LLM_MODEL)
    assert matrix["registry_count"] == 0
    assert matrix["blind_spots"] == [] and matrix["chronics"] == []
    assert matrix["auth_banner"] is False


def test_stale_sync_flag(engine):
    """Обход старше 48 часов — явный флаг устаревания (US-A3: не тихо устаревшие данные)."""
    with Session(engine) as s:
        _, run = _seed(s)
        run.started_at = datetime(2026, 6, 1, 10, 0)
        s.commit()
        fresh = build_artifact_matrix(
            s, llm_model=LLM_MODEL, now=datetime(2026, 6, 2, 10, 0)
        )
        stale = build_artifact_matrix(
            s, llm_model=LLM_MODEL, now=datetime(2026, 6, 4, 10, 1)
        )
    assert fresh["stale"] is False
    assert stale["stale"] is True


def test_page_renders_signal_blocks(client, engine):
    with Session(engine) as s:
        _seed(s, outcome=SyncOutcome.repo_unavailable, detail="HTTP 404")
        repo2 = store.register_repository(
            s, repo_url="https://github.com/s/y", git_host=GitHost.GitHub
        )
        run2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        s.flush()
        store.register_sync_outcome(
            s, sync_run_id=run2.id, repository_id=repo2.id, outcome=SyncOutcome.auth_failed
        )
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "Слепая зона" in html
    assert "Обнови токен" in html
