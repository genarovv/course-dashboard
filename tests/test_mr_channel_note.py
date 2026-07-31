"""FR-12, интерим (ADR-007 вариант А): ячейка занятия с каналом сдачи «MR»
не показывает ложное «нет» — говорит «сдача через MR».

Основание: порядок сдачи занятия 11; решение совещания 2026-07-28 (развилка 2):
до готовности полного FR-12 матрица обязана быть честной.
"""

from datetime import date, datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.models import GitHost, SnapshotStatus, SyncOutcome, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.services import config_manager
from app.services.matrix_builder import build_matrix

TODAY = date(2026, 7, 28)


@pytest.fixture()
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    yield create_engine(f"sqlite:///{db_path}")


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


# ── миграция ────────────────────────────────────────────────────────────────


def test_lesson_has_submission_channel_column_default_files(engine):
    columns = {c["name"] for c in inspect(engine).get_columns("lesson")}
    assert "submission_channel" in columns
    with Session(engine) as s:
        lesson = Lesson(number=5, title="PRD", date=date(2026, 6, 30))
        s.add(lesson)
        s.flush()
        assert lesson.submission_channel == "files"  # default: файловый канал


def test_migration_downgrade_works(tmp_path):
    """Вето девопса: миграция берётся только с работающим downgrade."""
    db_path = tmp_path / "down.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "d408a1d4f3e7")  # до ревизии перед submission_channel (не «-1»: цепочка растёт)
    engine = create_engine(f"sqlite:///{db_path}")
    columns = {c["name"] for c in inspect(engine).get_columns("lesson")}
    assert "submission_channel" not in columns
    command.upgrade(cfg, "head")  # и обратно
    engine.dispose()


# ── конфиг-реконсиляция ────────────────────────────────────────────────────

YAML_MR = """
lessons:
  - number: 11
    title: "Тестирование"
    date: 2026-07-21
    submission_channel: mr
  - number: 5
    title: "PRD"
    date: 2026-06-30
"""


def test_config_sets_submission_channel(session):
    config_manager.reconcile(session, config_manager.parse_config(YAML_MR))
    lessons = {lesson.number: lesson for lesson in store.find_all_lessons(session)}
    assert lessons[11].submission_channel == "mr"
    assert lessons[5].submission_channel == "files"  # по умолчанию


def test_config_reload_updates_channel(session):
    config_manager.reconcile(session, config_manager.parse_config(YAML_MR))
    session.commit()
    updated = YAML_MR.replace('date: 2026-06-30\n', 'date: 2026-06-30\n    submission_channel: mr\n')
    summary = config_manager.reconcile(session, config_manager.parse_config(updated))
    lessons = {lesson.number: lesson for lesson in store.find_all_lessons(session)}
    assert lessons[5].submission_channel == "mr"
    assert summary.lessons_updated == 1


# ── матрица ────────────────────────────────────────────────────────────────


def _seed_checked_repo(s):
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_sync_outcome(s, sync_run_id=run.id, repository_id=repo.id,
                                outcome=SyncOutcome.ok_unchanged)
    s.flush()
    return repo, run


def test_mr_lesson_empty_cell_marked_mr_channel(session):
    config_manager.reconcile(session, config_manager.parse_config(YAML_MR))
    repo, run = _seed_checked_repo(session)

    matrix = build_matrix(session, today=TODAY)

    assert matrix["cells"][repo.id][11]["mr_channel"] is True  # честно: сдача через MR
    assert matrix["cells"][repo.id][5]["mr_channel"] is False  # файловое занятие — как раньше


def test_mr_lesson_not_found_cell_also_marked(session):
    """not_found у MR-занятия — тоже не приговор: артефакт может жить в ветке MR."""
    config_manager.reconcile(session, config_manager.parse_config(YAML_MR))
    repo, run = _seed_checked_repo(session)
    lesson11 = next(le for le in store.find_all_lessons(session) if le.number == 11)
    adef = ArtifactDef(lesson_id=lesson11.id, role="tests", expected_pattern="tests/**")
    session.add(adef)
    session.flush()
    store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status=SnapshotStatus.not_found,
    )
    session.flush()

    cell = build_matrix(session, today=TODAY)["cells"][repo.id][11]
    assert cell["status"] == SnapshotStatus.not_found
    assert cell["mr_channel"] is True


def test_mr_note_rendered_with_card_link(tmp_path, monkeypatch):
    """UI: пометка MR-канала + ссылка на карточку (AC-5 US-B7).

    tests-change D39 (итерация 5, решение CEO «на все да»): формулировка без
    двойного отрицания — «сдаётся через merge request — файлов в основной ветке нет»."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import get_session

    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    db_path = tmp_path / "ui.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        config_manager.reconcile(s, config_manager.parse_config(YAML_MR))
        repo, _run = _seed_checked_repo(s)
        s.commit()
        repo_id = repo.id

    def override_session():
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app)
        client.post("/login", data={"username": "admin", "password": "pw"})
        html = client.get("/lessons").text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert "сдаётся через merge request — файлов в основной ветке нет" in html  # ADR-007, редакция D39
    assert f"/students/{repo_id}" in html  # ссылка на карточку


def test_mr_lesson_found_shows_real_status(session):
    """Артефакт найден в default-ветке — показываем реальный статус, пометка не нужна."""
    config_manager.reconcile(session, config_manager.parse_config(YAML_MR))
    repo, run = _seed_checked_repo(session)
    lesson11 = next(le for le in store.find_all_lessons(session) if le.number == 11)
    adef = ArtifactDef(lesson_id=lesson11.id, role="tests", expected_pattern="tests/**")
    session.add(adef)
    session.flush()
    store.register_snapshot(
        session, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef.id,
        status=SnapshotStatus.found, content_hash="h" * 64, file_path="tests/t.py",
        source_commit_sha="c" * 40, observed_at=datetime(2026, 7, 22),
    )
    session.flush()

    cell = build_matrix(session, today=TODAY)["cells"][repo.id][11]
    assert cell["status"] == SnapshotStatus.found
    assert cell["mr_channel"] is False
