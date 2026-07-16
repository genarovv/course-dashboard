"""S6 (#8), FR-1: CSV-импорт репозиториев — критерии приёмки."""

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.clients.git_client import GitClient
from app.main import app
from app.models.repository import Repository
from app.routes import get_session
from app.routes.admin import get_git_client

PASSWORD = "pw"


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

    def handler(request: httpx.Request) -> httpx.Response:
        if "dead" in request.url.path:
            return httpx.Response(404)
        if "/api/v4/" in request.url.path:  # GitLab отдаёт список
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"tree": []})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_git_client] = lambda: GitClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    client = TestClient(app)
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


CSV_1 = (
    "ФИО,repo_url\n"
    "Иванов Иван,https://github.com/ivanov/repo\n"
    "Петров Пётр,https://github.com/ivanov/Repo.git\n"  # дубликат после нормализации (И6)
    "Сидоров Сидор,https://github.com/sidorov/dead\n"
)


def test_import_creates_repositories_and_summary(client_and_engine):
    client, engine = client_and_engine
    response = client.post("/import-csv", content=CSV_1.encode())
    assert response.status_code == 200
    assert response.json() == {"available": 1, "unavailable": 1, "duplicates": 1}
    with Session(engine) as s:
        repos = s.scalars(select(Repository)).all()
        assert len(repos) == 2  # дубликат не создан
        # ФИО нигде не сохраняется (рамка CEO: именных данных в модели нет)
        assert all("Иванов" not in (r.repo_url or "") for r in repos)


def test_reimport_adds_new_without_losing_old(client_and_engine):
    client, engine = client_and_engine
    client.post("/import-csv", content=CSV_1.encode())
    csv_2 = CSV_1 + "Новикова Анна,https://gitlab.com/novikova/repo\n"
    response = client.post("/import-csv", content=csv_2.encode())
    assert response.json() == {"available": 1, "unavailable": 0, "duplicates": 3}
    with Session(engine) as s:
        urls = set(s.scalars(select(Repository.normalized_repo_url)))
        assert urls == {
            "https://github.com/ivanov/repo",
            "https://github.com/sidorov/dead",
            "https://gitlab.com/novikova/repo",
        }


def test_import_requires_auth(client_and_engine):
    client, _ = client_and_engine
    client.get("/logout")
    assert client.post("/import-csv", content=b"x").status_code == 401
