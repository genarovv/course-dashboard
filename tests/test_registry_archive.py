"""D45 (#71): реестр в CSV — источник правды в обе стороны.

`archived_at` описан в data-model как штатный механизм («ошибочный URL
архивируется, не удаляется»), но записать его было нечем: в store только чтение
(`find_active_repositories` / `find_archived_repositories`), маршрута нет.
Вскрылось на живой задаче — двое из реестра не проходят курс (решение CEO
2026-08-11), а убрать их из матрицы оказалось невозможно.

Импорт делает CSV источником правды: чего в файле нет — уходит в архив,
что вернулось — возвращается. Снапшоты и вердикты сохраняются всегда (FR-9).
"""

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
A = "https://github.com/s1/alpha"
B = "https://github.com/s2/beta"


@pytest.fixture()
def client_and_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
    db_path = tmp_path / "registry.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")

    def override_session():
        with Session(engine) as session:
            yield session
            session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": []})
        return httpx.Response(200, json={"default_branch": "main"})

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_git_client] = lambda: GitClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    client = TestClient(app)
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def _csv(*urls):
    return "ФИО,repo_url\n" + "".join(f"Кто-то,{u}\n" for u in urls)


def _post(client, text):
    return client.post("/import-csv", content=text.encode("utf-8"))


def _repos(engine):
    with Session(engine) as s:
        return {r.repo_url: r.archived_at for r in s.scalars(select(Repository))}


def test_repository_absent_from_csv_is_archived(client_and_engine):
    """Случай С-08/С-09: человек не проходит курс — строка уходит из матрицы."""
    client, engine = client_and_engine
    _post(client, _csv(A, B))

    _post(client, _csv(A))

    repos = _repos(engine)
    assert repos[A] is None            # остался активным
    assert repos[B] is not None        # заархивирован, не удалён


def test_archived_repository_returns_when_back_in_csv(client_and_engine):
    """Вернулся в реестр — снова активен; история наблюдений при этом цела (FR-9)."""
    client, engine = client_and_engine
    _post(client, _csv(A, B))
    _post(client, _csv(A))

    _post(client, _csv(A, B))

    assert _repos(engine)[B] is None


def test_summary_reports_archived_and_restored(client_and_engine):
    """Молча терять строки матрицы нельзя — числа возвращаются вызывающему."""
    client, engine = client_and_engine
    _post(client, _csv(A, B))

    response = _post(client, _csv(A))

    assert response.json()["archived"] == 1
    assert response.json()["restored"] == 0

    back = _post(client, _csv(A, B))
    assert back.json()["restored"] == 1
    assert back.json()["archived"] == 0


def test_empty_csv_does_not_archive_everything(client_and_engine):
    """Битый или пустой файл не должен обнулять реестр одним нажатием."""
    client, engine = client_and_engine
    _post(client, _csv(A, B))

    response = _post(client, "ФИО,repo_url\n")

    assert response.status_code == 400
    assert all(v is None for v in _repos(engine).values())


def test_archiving_is_logged_by_name(client_and_engine, monkeypatch):
    """След в логе поимённо: исчезновение строки из матрицы должно быть объяснимо."""
    from app.services import csv_importer

    client, engine = client_and_engine
    _post(client, _csv(A, B))
    messages: list[str] = []
    monkeypatch.setattr(
        csv_importer.logger, "info",
        lambda msg, *args, **kw: messages.append(msg % args if args else msg),
    )

    _post(client, _csv(A))

    assert any(B in m for m in messages)
