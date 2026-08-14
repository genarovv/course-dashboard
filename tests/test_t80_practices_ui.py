"""#80 (FR-14 этап 1): GET /practices + блок «Приёмы курса» в деле защиты.

AC 9: страница отдаёт 200 залогиненному и 303 гостю; в таблице все репозитории
реестра × все проверки конфига. Наблюдения сеются напрямую через store —
страница проекция журнала, обход ей не нужен.
"""

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, PracticeStatus, SyncTrigger
from app.routes import get_session
from app.services import config_manager


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    db_path = tmp_path / "ui.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    yield create_engine(f"sqlite:///{db_path}")


@pytest.fixture()
def client(engine):
    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _login(client):
    client.post("/login", data={"username": "admin", "password": "pw"})


def _seed(engine):
    """Два репозитория; у первого — наблюдения passed/failed, второй пуст."""
    with Session(engine) as s:
        repo_a = store.register_repository(
            s, repo_url="https://github.com/s/alpha", git_host=GitHost.GitHub
        )
        repo_b = store.register_repository(
            s, repo_url="https://github.com/s/beta", git_host=GitHost.GitHub
        )
        run = store.register_sync_run(s, triggered_by=SyncTrigger.manual)
        s.flush()
        store.register_practice_observation(
            s, sync_run_id=run.id, repository_id=repo_a.id, check_key="tests_first",
            status=PracticeStatus.passed,
            evidence=[{"kind": "mr_commit", "mr_number": 7, "sha": "a" * 40,
                       "quote": "T5: tests first"}],
        )
        store.register_practice_observation(
            s, sync_run_id=run.id, repository_id=repo_a.id, check_key="docs_sync",
            status=PracticeStatus.failed,
            evidence=[{"kind": "mr", "mr_number": 8,
                       "quote": "код без документации и без «потому что»"}],
        )
        s.commit()
        return repo_a.id, repo_b.id


def test_practices_requires_login(client):
    response = client.get("/practices", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_practices_all_repos_and_checks(client, engine):
    """AC 9: все репозитории реестра × все проверки конфига; пустые ячейки — «—»."""
    _seed(engine)
    _login(client)

    response = client.get("/practices")
    assert response.status_code == 200
    html = response.text

    assert "alpha" in html and "beta" in html
    checks = config_manager.load_config().practice_checks
    assert checks, "config.yaml обязан задавать practice_checks (FR-14 этап 1)"
    for check in checks:
        assert check.label in html
    assert "✓" in html and "✗" in html and "—" in html


def test_practices_evidence_visible(client, engine):
    """Доказательства раскрываются по строке репозитория: цитата и номер MR в HTML."""
    _seed(engine)
    _login(client)

    html = client.get("/practices").text

    assert "T5: tests first" in html
    assert "код без документации" in html


def test_practices_weak_signal_note(client, engine):
    """Ограничение спеки честно названо: порядок коммитов — след, не доказательство."""
    _seed(engine)
    _login(client)

    assert "след, не доказательство" in client.get("/practices").text


def test_matrix_links_to_practices(client, engine):
    _seed(engine)
    _login(client)

    assert "/practices" in client.get("/").text


def test_defense_shows_practice_block(client, engine):
    """Блок «Приёмы курса» в деле защиты: passed одной строкой, failed с доказательством,
    прочие проверки конфига — «нет данных»."""
    repo_a, _repo_b = _seed(engine)
    _login(client)

    html = client.get(f"/students/{repo_a}/defense").text

    assert "Приёмы курса" in html
    checks = {c.key: c for c in config_manager.load_config().practice_checks}
    assert checks["tests_first"].label in html          # passed — списком
    assert checks["docs_sync"].label in html            # failed — с доказательством
    assert "код без документации" in html
    assert "нет данных" in html                         # остальные проверки без наблюдений
