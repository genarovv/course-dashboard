"""D12 (#56), FR-4/BR-3: легенда матрицы артефактов.

AC (спека §D12): строка-легенда между шапкой и таблицей — все состояния ячейки
(есть / частично / нет / не наблюдался / сдача через MR), чип разрыва в двух
градациях уверенности, «помечен ложным»; пояснение правила «частично» (BR-3)
своими словами по наведению.
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

PASSWORD = "correct-horse"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    with Session(engine) as s:
        s.add(Lesson(number=1, title="Старт", date=datetime(2026, 6, 1).date()))
        repo = store.register_repository(
            s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub
        )
        run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        s.flush()
        store.register_sync_outcome(
            s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
        )
        s.commit()
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def _page(client):
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    return client.get("/artifacts").text


def test_legend_lists_all_states(client):
    """tests-change D22 (#68): формулировки легенды без жаргона («не наблюдался» →
    «нет данных», «сдача через MR» → расшифровка) — состав легенды не менялся."""
    html = _page(client)
    assert 'class="legend"' in html
    for state in ("есть", "частично", "нет", "нет данных",
                  "сдача через merge request (запрос на слияние)", "помечен ложным"):
        assert state in html


def test_legend_explains_partial_rule(client):
    """BR-3 своими словами — по наведению (title), без чтения PRD."""
    html = _page(client)
    assert "неточное имя" in html and "заготовк" in html


def test_legend_shows_both_chip_grades(client):
    html = _page(client)
    assert html.count("break-chip") >= 2  # полный и контурный образцы в легенде
    assert "chip-low" in html and "chip-high" in html
