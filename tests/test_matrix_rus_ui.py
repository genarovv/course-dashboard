"""D8: сверка матрицы занятий с макетом — решения CEO 2026-07-30.

Решения (сессия сравнения живой страницы с maket 1.png):
  1. Статусы ячеек — по-русски: есть / частично / нет, причины «частично» — словами.
  2. Строка репозитория — короткое имя (хвост URL), полный адрес — в title.
  3. «Актуально на» — с датой (ДД.ММ ЧЧ:ММ), формулировка живой страницы сохранена.
  4. Колонки «Разрывы связности» и «Процесс» — вторым блоком под матрицей,
     сама матрица — только занятия (как на макете).
"""

from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.config import settings
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session
from app.services.matrix_builder import build_matrix

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
        with Session(engine) as session:
            yield session
            session.commit()

    app.dependency_overrides[get_session] = override_session
    test_client = TestClient(app)
    test_client.post("/login", data={"username": "admin", "password": PASSWORD})
    yield test_client
    app.dependency_overrides.clear()


def _seed(engine) -> None:
    """Три занятия со статусами found / partial(template_copy) / not_found + один MR."""
    with Session(engine) as session:
        repo = store.register_repository(
            session, repo_url="https://github.com/s01/proj", git_host=GitHost.GitHub
        )
        run = store.register_sync_run(session, triggered_by=SyncTrigger.manual)
        run.started_at = datetime(2026, 7, 28, 16, 7, 0)  # наивный UTC в БД (#32)
        session.flush()
        plan = [
            (1, "interview", SnapshotStatus.found, None, "docs/interview.md"),
            (2, "persona", SnapshotStatus.partial, ["template_copy"], "docs/persona.md"),
            (3, "prd", SnapshotStatus.not_found, None, None),
        ]
        for number, role, status, reasons, file_path in plan:
            lesson = Lesson(
                number=number, title=f"Занятие {number}",
                date=datetime(2026, 1, 10 + number).date(),
            )
            session.add(lesson)
            session.flush()
            adef = ArtifactDef(
                lesson_id=lesson.id, role=role, expected_pattern=f"**/{role}.md"
            )
            session.add(adef)
            session.flush()
            fields = dict(
                sync_run_id=run.id,
                repository_id=repo.id,
                artifact_def_id=adef.id,
                status=status,
                file_path=file_path,
                source_commit_sha="a" * 40 if file_path else None,
                content_hash="b" * 64 if file_path else None,
            )
            if reasons:  # явный None в JSON-колонке стал бы 'null', а не SQL NULL (И8)
                fields["partial_reason"] = reasons
            store.register_snapshot(session, **fields)
        store.register_mr_observation(
            session, sync_run_id=run.id, repository_id=repo.id, mr_number=7,
            title="MR 7", source_branch="b7", state="opened",
            reviewer_approved=True, markers={},
            head_sha="c" * 40, updated_at=datetime(2026, 7, 28, 10, 0),
        )
        store.register_sync_outcome(
            session, sync_run_id=run.id, repository_id=repo.id,
            outcome=SyncOutcome.ok_changed,
        )
        session.commit()


# ── Решение 1: статусы по-русски ───────────────────────────────────────────


def test_matrix_statuses_in_russian(engine, client):
    _seed(engine)
    html = client.get("/").text
    assert ">есть<" in html
    assert ">частично<" in html
    assert ">нет<" in html
    assert "заготовка из шаблона" in html
    # сырые значения enum в тексте ячеек не показываются
    for raw in (">found<", ">partial<", ">not_found<", "template_copy</span>"):
        assert raw not in html


# ── Решение 2: короткое имя репозитория, полный URL в title ────────────────


def test_matrix_repo_short_name_with_full_url_in_title(engine, client):
    _seed(engine)
    html = client.get("/").text
    assert ">proj</a>" in html
    assert 'title="https://github.com/s01/proj"' in html


# ── Решение 3: «Актуально на» с датой ──────────────────────────────────────


def test_as_of_contains_date_and_local_time(engine, monkeypatch):
    _seed(engine)
    monkeypatch.setattr(settings, "tz_offset_minutes", 240)  # UTC+4, Тбилиси
    with Session(engine) as session:
        matrix = build_matrix(session)
    assert matrix["as_of"] == "28.07 20:07 (UTC+4)"


# ── Решение 4: разрывы и процесс — вторым блоком под матрицей ──────────────


def test_breaks_and_process_moved_below_matrix(engine, client):
    _seed(engine)
    html = client.get("/").text
    matrix_end = html.index("</table>")  # конец первой (основной) таблицы
    assert html.index("Разрывы связности") > matrix_end
    assert html.index("Процесс") > matrix_end
    # в блоке виден процесс сдачи посеянного MR (1 принят, ждёт merge)
    assert "1 принят, ждёт merge" in html
