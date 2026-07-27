"""I1 (#2): скелет приложения — критерии приёмки."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# /health с тикета I2 (#13) читает счётчики из БД — его контракт проверяет
# tests/test_health.py на мигрированной БД (изменение требования: AC тикета #13).


def test_login_form_returns_html():
    response = client.get("/login")
    assert response.status_code == 200
    assert "<form" in response.text


def test_root_redirects_to_login_without_auth():
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
