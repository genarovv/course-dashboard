"""O2 (#16), FR-10: кнопка «ложный разрыв» на точке разрыва.

AC тикета #16:
  1. На точке разрыва (matrix.html и student_card.html) — кнопка «ложный разрыв»
  2. Нажатие создаёт Override
  3. Повторное нажатие снимает (revoked_at, не удаление — И5)
  4. Отмеченный разрыв не подсвечивается при следующих обходах,
     пока пара документов не изменилась (идентичность — четвёрка И3)
"""

from datetime import datetime

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from alembic import command
from app import store
from app.main import app
from app.models import GitHost, SnapshotStatus, SyncTrigger
from app.models.artifact_def import ArtifactDef
from app.models.lesson import Lesson
from app.models.override import Override
from app.routes import get_session
from app.services.matrix_builder import build_matrix

LLM_MODEL = "deepseek-v4-flash"


def _seed_break(s):
    """Ребро prd→data_model, репозиторий, снапшоты, вердикт break. Возвращает (repo, verdict, ...)."""
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    s.add_all([lesson5, lesson6])
    s.flush()
    adef_prd = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    adef_dm = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    s.add_all([adef_prd, adef_dm])
    rubric = store.register_rubric(s, type="edge", version="1.0", text="правило")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    snap_a = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_prd.id,
        status=SnapshotStatus.found, content_hash="a" * 64, file_path="product/prd.md",
        source_commit_sha="c" * 40,
    )
    snap_b = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=adef_dm.id,
        status=SnapshotStatus.found, content_hash="b" * 64, file_path="data-model.md",
        source_commit_sha="c" * 40,
    )
    store.register_sync_outcome(s, sync_run_id=run.id, repository_id=repo.id, outcome="ok_changed")
    s.flush()
    verdict = store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash="a" * 64, target_content_hash="b" * 64, rubric_id=rubric.id,
        llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "X", "quote": "цитата", "why": "потеряно"}],
    )
    s.flush()
    return repo, verdict, edge, rubric, run, adef_prd, adef_dm


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", "pw")
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
    yield TestClient(app), engine
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client):
    client.post("/login", data={"username": "admin", "password": "pw"})


# ── AC 2/3: toggle создаёт и снимает ───────────────────────────────────────


def test_toggle_creates_override(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        verdict_id = verdict.id
    _login(client)

    response = client.post(f"/verdicts/{verdict_id}/override-toggle", follow_redirects=False)

    assert response.status_code == 303
    with Session(engine) as s:
        assert store.find_active_override_for_verdict(s, verdict_id) is not None


def test_second_toggle_revokes_not_deletes(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        verdict_id = verdict.id
    _login(client)

    client.post(f"/verdicts/{verdict_id}/override-toggle")
    client.post(f"/verdicts/{verdict_id}/override-toggle")

    with Session(engine) as s:
        assert store.find_active_override_for_verdict(s, verdict_id) is None
        rows = list(s.scalars(select(Override)))
        assert len(rows) == 1  # снятие = revoked_at, не удаление (И5)
        assert rows[0].revoked_at is not None


def test_third_toggle_creates_new_row_history_kept(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        verdict_id = verdict.id
    _login(client)

    for _ in range(3):
        client.post(f"/verdicts/{verdict_id}/override-toggle")

    with Session(engine) as s:
        assert store.find_active_override_for_verdict(s, verdict_id) is not None
        assert len(list(s.scalars(select(Override)))) == 2  # история решений видна (DM §3.2)


def test_toggle_requires_auth(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        verdict_id = verdict.id

    response = client.post(f"/verdicts/{verdict_id}/override-toggle", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"  # именно гвард, а не redirect после действия
    with Session(engine) as s:
        assert store.find_active_override_for_verdict(s, verdict_id) is None  # отметка не создана


def test_toggle_unknown_verdict_404(client_env):
    client, _ = client_env
    _login(client)
    assert client.post("/verdicts/no-such/override-toggle").status_code == 404


# ── AC 1: кнопка на точке разрыва в обоих шаблонах ─────────────────────────


def test_matrix_shows_break_with_toggle_button(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        verdict_id = verdict.id
    _login(client)

    html = client.get("/lessons").text

    assert f"/verdicts/{verdict_id}/override-toggle" in html  # кнопка на точке разрыва
    assert "пометить ложным" in html  # tests-change D38: кнопка-глагол


def test_student_card_shows_toggle_button(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        repo_id, verdict_id = repo.id, verdict.id
    _login(client)

    html = client.get(f"/students/{repo_id}").text

    assert f"/verdicts/{verdict_id}/override-toggle" in html
    assert "пометить ложным" in html  # tests-change D38: кнопка-глагол


# ── AC 4: отмеченный разрыв не подсвечивается, пока пара не изменилась ─────


def test_overridden_break_not_highlighted_on_next_matrix(client_env):
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, *_ = _seed_break(s)
        s.commit()
        repo_id, verdict_id = repo.id, verdict.id
    _login(client)
    client.post(f"/verdicts/{verdict_id}/override-toggle")

    with Session(engine) as s:
        matrix = build_matrix(s, llm_model=LLM_MODEL)
    (break_card,) = matrix["breaks"][repo_id]
    assert break_card["override_active"] is True  # шаблон гасит подсветку по этому флагу

    html = client.get("/lessons").text
    assert "отмечен как ложный" in html


def test_changed_pair_highlights_again_despite_old_override(client_env):
    """AC 4: пара изменилась (новая четвёрка) → новый break подсвечен, старая отметка не действует."""
    client, engine = client_env
    with Session(engine) as s:
        repo, verdict, edge, rubric, run, adef_prd, adef_dm = _seed_break(s)
        s.commit()
        repo_id, verdict_id = repo.id, verdict.id
        edge_id, rubric_id = edge.id, rubric.id
        adef_prd_id, snap_b_id = adef_prd.id, verdict.target_snapshot_id
    _login(client)
    client.post(f"/verdicts/{verdict_id}/override-toggle")

    with Session(engine) as s:
        # студент изменил PRD → новый снапшот и новый break-вердикт на новую четвёрку
        run2 = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
        s.flush()
        snap_a2 = store.register_snapshot(
            s, sync_run_id=run2.id, repository_id=repo_id, artifact_def_id=adef_prd_id,
            status=SnapshotStatus.found, content_hash="d" * 64, file_path="product/prd.md",
            source_commit_sha="e" * 40,
        )
        s.flush()
        store.register_verdict(
            s, edge_def_id=edge_id, source_snapshot_id=snap_a2.id,
            target_snapshot_id=snap_b_id, source_content_hash="d" * 64,
            target_content_hash="b" * 64, rubric_id=rubric_id, llm_model=LLM_MODEL,
            verdict="break", confidence="high",
        )
        s.commit()

    with Session(engine) as s:
        matrix = build_matrix(s, llm_model=LLM_MODEL)
    (break_card,) = matrix["breaks"][repo_id]
    assert break_card["override_active"] is False  # новая четвёрка — отметка не переносится
