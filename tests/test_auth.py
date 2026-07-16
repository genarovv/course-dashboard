"""S5 (#7), FR-0: login/logout/lockout — критерии приёмки."""

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app.main import app
from app.routes import get_session

PASSWORD = "correct-horse"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Приложение поверх БД из миграции с известным паролем админа."""
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
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client, password):
    return client.post(
        "/login", data={"username": "admin", "password": password}, follow_redirects=False
    )


def test_login_success_creates_session(client):
    response = _login(client, PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # сессия действует: GET / больше не редиректит на /login
    assert client.get("/", follow_redirects=False).status_code == 200


def test_login_wrong_password_5_times_locks_for_15_minutes(client):
    for _ in range(5):
        assert _login(client, "wrong").status_code == 401
    # блокировка: даже верный пароль не пускает
    response = _login(client, PASSWORD)
    assert response.status_code == 429
    assert "заблокирована" in response.text


def test_logout_ends_session(client):
    _login(client, PASSWORD)
    response = client.get("/logout", follow_redirects=False)
    assert response.status_code == 303
    # сессия завершена: GET / снова редиректит на /login
    assert client.get("/", follow_redirects=False).status_code == 303
