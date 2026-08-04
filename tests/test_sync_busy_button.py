"""D41 (решение CEO 2026-08-04 «гасить кнопку»): пока обход идёт, «обновить
сейчас» погашена, а повторный POST /sync отвечает 409, а не падает.

Боевой случай 2026-08-04: CEO нажал «обновить сейчас» второй раз, пока шёл
первый обход, и получил 500 `database is locked` — транзакция обхода держит
единственного писателя SQLite. Кнопка гасла только во вкладке, где её нажали:
перезагрузка страницы или вторая вкладка возвращали её активной.

Истина о «идёт ли обход» — серверная (SyncRun.status), иначе cron и вторая
вкладка обходят любую блокировку в браузере. Обход, начатый слишком давно,
считается мёртвым (процесс мог упасть) — иначе кнопка залипнет навсегда.
"""

from datetime import datetime, timedelta

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, SyncStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.models.sync_run import SyncRun
from app.routes import get_session
from app.services.sync_orchestrator import is_sync_running

PASSWORD = "correct-horse"


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "busy.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    eng = create_engine(f"sqlite:///{db_path}")
    yield eng
    eng.dispose()


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


def _seed_course(s):
    lesson = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    s.add(lesson)
    s.flush()
    s.add(ArtifactDef(lesson_id=lesson.id, role="prd", expected_pattern="product/prd.md"))
    store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    s.flush()


def _start_run(s, *, minutes_ago: float = 0.0) -> SyncRun:
    run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
    s.flush()
    if minutes_ago:
        run.started_at = run.started_at - timedelta(minutes=minutes_ago)
    return run


# ── серверная истина: идёт ли обход ─────────────────────────────────────────


def test_running_sync_is_detected(engine):
    with Session(engine) as s:
        _seed_course(s)
        _start_run(s)
        assert is_sync_running(s) is True


def test_finished_sync_is_not_running(engine):
    with Session(engine) as s:
        _seed_course(s)
        run = _start_run(s)
        store.update_sync_run_status(s, run.id, SyncStatus.completed)
        assert is_sync_running(s) is False


def test_long_abandoned_run_is_not_running(engine):
    """Процесс упал посреди обхода — статус остался in_progress навсегда.

    Без срока давности кнопка залипла бы до ручной правки БД.
    """
    with Session(engine) as s:
        _seed_course(s)
        _start_run(s, minutes_ago=120)
        assert is_sync_running(s) is False


# ── роут: повторный запуск не падает, а отказывает ──────────────────────────


def test_second_sync_while_running_returns_409(client, engine):
    with Session(engine) as s:
        _seed_course(s)
        _start_run(s)
        s.commit()
    _login(client)
    response = client.post("/sync")
    assert response.status_code == 409
    assert "идёт" in response.json()["error"]
    with Session(engine) as s:  # второй обход не начат
        assert len(s.scalars(select(SyncRun)).all()) == 1


# ── кнопка в обеих матрицах ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/artifacts", "/lessons"])
def test_button_disabled_while_sync_running(client, engine, path):
    with Session(engine) as s:
        _seed_course(s)
        _start_run(s)
        s.commit()
    _login(client)
    html = client.get(path).text
    assert "обход идёт" in html
    assert "disabled" in html
    assert "обновить сейчас" not in html


@pytest.mark.parametrize("path", ["/artifacts", "/lessons"])
def test_button_active_when_no_sync_running(client, engine, path):
    with Session(engine) as s:
        _seed_course(s)
        run = _start_run(s)
        store.update_sync_run_status(s, run.id, SyncStatus.completed)
        s.commit()
    _login(client)
    html = client.get(path).text
    assert "обновить сейчас" in html
    assert "confirm(" in html
