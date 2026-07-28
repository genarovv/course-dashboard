"""#41 (FR-12): MR в карточке студента + колонка «процесс» в матрице (US-B7)."""

from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, SyncTrigger
from app.routes import get_session
from app.services.evidence_chain import build_student_card
from app.services.matrix_builder import build_matrix


def _observe(s, run, repo, number, state="opened", approved=False, markers=None):
    store.register_mr_observation(
        s, sync_run_id=run.id, repository_id=repo.id, mr_number=number,
        title=f"MR {number}", source_branch=f"b{number}", state=state,
        reviewer_approved=approved, markers=markers or {},
        head_sha="a" * 40, updated_at=datetime(2026, 7, 28, 10, 0),
    )
    s.flush()


def _seed(s):
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    store.register_sync_outcome(s, sync_run_id=run.id, repository_id=repo.id, outcome="ok_unchanged")
    s.flush()
    return repo, run


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    yield create_engine(f"sqlite:///{db_path}")


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def test_card_lists_mrs_with_ready_flag(session):
    repo, run = _seed(session)
    _observe(session, run, repo, 7, state="opened", approved=True,
             markers={"prichina": {"found": True, "quote": "Причина: гонка"}})
    _observe(session, run, repo, 6, state="merged")
    _observe(session, run, repo, 5, state="opened", approved=False)

    card = build_student_card(session, repo.id, llm_model="deepseek-v4-flash")

    mrs = {m["number"]: m for m in card["mrs"]}
    assert mrs[7]["ready_for_merge"] is True   # opened + принят ревьюером = готов к кнопке
    assert mrs[5]["ready_for_merge"] is False
    assert mrs[6]["ready_for_merge"] is False  # merged — кнопка уже нажата
    assert mrs[7]["markers"]["prichina"]["found"] is True


def test_matrix_process_summary(session):
    repo, run = _seed(session)
    _observe(session, run, repo, 7, state="opened", approved=True)
    _observe(session, run, repo, 6, state="merged")
    _observe(session, run, repo, 5, state="opened")

    matrix = build_matrix(session, llm_model="deepseek-v4-flash")

    process = matrix["process"][repo.id]
    # семантика 2026-07-29: merged без вердикта = «мимо ревью», merged+вердикт = accepted
    assert process == {"ready": 1, "opened": 2, "merged": 1, "accepted": 0, "merged_no_review": 1}


def test_matrix_process_empty_without_observations(session):
    repo, _run = _seed(session)
    matrix = build_matrix(session, llm_model="deepseek-v4-flash")
    assert matrix["process"][repo.id] == {
        "ready": 0, "opened": 0, "merged": 0, "accepted": 0, "merged_no_review": 0
    }


def test_ui_renders_mr_block_and_process_column(engine):
    with Session(engine) as s:
        repo, run = _seed(s)
        _observe(s, run, repo, 7, state="opened", approved=True)
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
        card_html = client.get(f"/students/{repo_id}").text
        matrix_html = client.get("/").text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert "MR 7" in card_html
    assert "принят ревьюером" in card_html  # ярлык сменён: кнопка преподавателя ушла (2026-07-29)
    assert "Процесс" in matrix_html  # колонка процесса


def test_closed_mr_not_ready(session):
    """Сценарий 3 ADR-007: закрытый без слияния MR уходит из «готов к кнопке»."""
    repo, run = _seed(session)
    _observe(session, run, repo, 8, state="closed", approved=True)

    card = build_student_card(session, repo.id, llm_model="deepseek-v4-flash")

    (mr,) = card["mrs"]
    assert mr["ready_for_merge"] is False


def test_ui_shows_updated_at_and_missing_markers(engine):
    """AC1/AC2 US-B7: дата обновления MR видна; незаполненный маркер показан как «не найден»."""
    with Session(engine) as s:
        repo, run = _seed(s)
        _observe(s, run, repo, 7, state="opened",
                 markers={"prichina": {"found": False, "quote": None}})
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
        html = client.get(f"/students/{repo_id}").text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

    assert "2026-07-28" in html  # дата последнего обновления MR (AC1)
    assert "prichina: не найден" in html  # отсутствие маркера — явно, не ошибка (AC2)


def test_merged_without_review_is_signal(session):
    """Порядок 2026-07-29: merged без «принято» — «смержен мимо ревью» в процессе матрицы."""
    repo, run = _seed(session)
    _observe(session, run, repo, 6, state="merged", approved=False)
    _observe(session, run, repo, 5, state="merged", approved=True)

    matrix = build_matrix(session, llm_model="deepseek-v4-flash")

    process = matrix["process"][repo.id]
    assert process["merged_no_review"] == 1
    assert process["accepted"] == 1  # merged + принято = сдан
