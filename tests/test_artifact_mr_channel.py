"""D9 (#53), FR-12/US-B7: «сдача через MR» в артефактной матрице.

AC (спека plans/доводки-артефактной-матрицы-2026-07-30.md §D9):
  1. Роль, чьи определения принадлежат MR-занятию, при отсутствии/not_found
     лучшего снапшота — нейтральная плашка «сдача через MR», не красное «нет».
  2. Найденный (или частичный) артефакт показывает реальный статус.
  3. Роль в файловом И MR-занятии: сначала best-wins, потом канал.
  4. Модалка такой ячейки объясняет канал сдачи.
Негативные: роль без MR-занятий — плашки нет; репозиторий без наблюдений
файловой роли — пустая ячейка, не плашка.
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
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.routes import get_session
from app.services.artifact_matrix import build_artifact_matrix, build_cell_details

LLM_MODEL = "deepseek-v4-flash"
PASSWORD = "correct-horse"


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)  # до миграций: сид system_user
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


def _seed(s):
    """Занятие 5 (files, prd), занятие 9 (files, code), занятие 13 (mr, tests + code)."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson9 = Lesson(number=9, title="Код", date=datetime(2026, 7, 14).date())
    lesson13 = Lesson(
        number=13, title="Тесты", date=datetime(2026, 7, 28).date(), submission_channel="mr"
    )
    s.add_all([lesson5, lesson9, lesson13])
    s.flush()
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_code9 = ArtifactDef(lesson_id=lesson9.id, role="code", expected_pattern="src/**/*.py")
    adef_code13 = ArtifactDef(lesson_id=lesson13.id, role="code", expected_pattern="app/**/*.py")
    adef_tests = ArtifactDef(lesson_id=lesson13.id, role="tests", expected_pattern="tests/**")
    s.add_all([adef_prd, adef_code9, adef_code13, adef_tests])
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    return repo, run, {"prd": adef_prd, "code9": adef_code9, "tests": adef_tests}


# ── AC 1: роль MR-занятия без артефакта — плашка, не «нет» ────────────────


def test_mr_role_without_snapshot_marked(engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        cells = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]
    assert cells["tests"]["mr_channel"] is True
    assert cells["tests"]["summary"] == "сдача через MR"
    # негативный: файловая роль без наблюдений — пустая ячейка, не плашка
    assert cells["prd"]["mr_channel"] is False
    assert cells["prd"]["summary"] is None


def test_mr_role_not_found_marked(engine):
    with Session(engine) as s:
        repo, run, adefs = _seed(s)
        store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adefs["tests"].id,
            status=SnapshotStatus.not_found,
        )
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["tests"]
    assert cell["mr_channel"] is True
    assert cell["summary"] == "сдача через MR"


# ── AC 2: найденный артефакт — реальный статус ────────────────────────────


def test_mr_role_found_shows_real_status(engine):
    with Session(engine) as s:
        repo, run, adefs = _seed(s)
        store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adefs["tests"].id,
            status=SnapshotStatus.found, content_hash="a" * 64, file_path="tests/t.py",
            source_commit_sha="c" * 40,
        )
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["tests"]
    assert cell["mr_channel"] is False
    assert cell["status"] == SnapshotStatus.found


# ── AC 3: роль в файловом и MR-занятии — best-wins, потом канал ───────────


def test_role_in_files_and_mr_lessons(engine):
    with Session(engine) as s:
        repo, run, adefs = _seed(s)
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["code"]
    assert cell["mr_channel"] is True  # наблюдений нет, одно из занятий роли — MR

    with Session(engine) as s:
        store.register_snapshot(
            s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adefs["code9"].id,
            status=SnapshotStatus.found, content_hash="b" * 64, file_path="src/m.py",
            source_commit_sha="c" * 40,
        )
        s.commit()
        cell = build_artifact_matrix(s, llm_model=LLM_MODEL)["cells"][repo.id]["code"]
    assert cell["mr_channel"] is False  # best-wins нашёл артефакт — плашка не прячет работу


# ── AC 4 + HTTP: плашка на странице и объяснение в модалке ────────────────


def test_page_renders_mr_note(client, engine):
    with Session(engine) as s:
        _seed(s)
        s.commit()
    _login(client)
    html = client.get("/artifacts").text
    assert "сдача через MR" in html
    assert "acell-mr" in html  # свой CSS-класс, не acell-not_found


def test_modal_explains_mr_channel(client, engine):
    with Session(engine) as s:
        repo, *_ = _seed(s)
        s.commit()
        repo_id = repo.id
        details = build_cell_details(s, repo_id, "tests", llm_model=LLM_MODEL)
    assert details["mr_channel"] is True
    _login(client)
    html = client.get(f"/artifacts/{repo_id}/tests").text
    assert "запрос на слияние" in html
