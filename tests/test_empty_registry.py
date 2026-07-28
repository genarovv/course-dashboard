"""#31: обход пустого реестра не должен выглядеть успехом.

Инцидент 2026-07-28: после пересоздания БД без импорта POST /sync по нулю
репозиториев вернул completed — ложный успех замаскировал «обходить нечего»,
дашборд пуст. Enum SyncStatus не меняем (без миграции): предупреждение живёт
в ответе /sync, в /health и на матрице.
"""


import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost
from app.routes import get_session
from app.routes.admin import get_git_client


class FakeGitClient:
    async def get_tree(self, repo_url, git_host, ref="main"):
        return ["product/prd.md"]

    async def get_file_content(self, repo_url, git_host, file_path, ref="main"):
        return "контент"

    async def get_head_sha(self, repo_url, git_host, ref="main"):
        return "a" * 40

    async def fetch_default_branch(self, repo_url, git_host):
        return "main"  # контракт ADR-006: обход сверяет default-ветку


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("lessons: []\n", encoding="utf-8")
    monkeypatch.setattr(settings, "config_yaml_path", str(yaml_path))
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
    app.dependency_overrides[get_git_client] = lambda: FakeGitClient()
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client):
    client.post("/login", data={"username": "admin", "password": "pw"})


def test_sync_empty_registry_returns_warning(client_env):
    client, _ = client_env
    _login(client)

    body = client.post("/sync").json()

    assert body["status"] == "completed"  # статус обхода не переопределяем (без миграции)
    assert body["repositories_checked"] == 0
    assert "реестр пуст" in body["warning"].lower()  # ложный успех больше не молчит


def test_sync_nonempty_registry_no_warning(client_env):
    client, engine = client_env
    with Session(engine) as s:
        store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
        s.commit()
    _login(client)

    body = client.post("/sync").json()

    assert body["repositories_checked"] == 1
    assert body.get("warning") is None


def test_health_reports_repository_count(client_env):
    client, engine = client_env
    body = client.get("/health").json()
    assert body["repositories"] == 0  # видно и без обхода

    with Session(engine) as s:
        store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
        s.commit()
    assert client.get("/health").json()["repositories"] == 1


def test_matrix_empty_registry_shows_hint(client_env):
    client, _ = client_env
    _login(client)

    html = client.get("/").text

    assert "реестр пуст" in html.lower()
    assert "import-csv" in html  # подсказка, как загрузить
