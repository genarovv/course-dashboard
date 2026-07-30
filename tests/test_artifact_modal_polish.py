"""D17 (#61): модалка — уверенность акцентно, якорь в карточку, управление фокусом.

AC (спека §D17):
  1. Уверенность в модалке — словами и визуально различимо (класс по уровню,
     не мелкий серый текст).
  2. Из модалки — переход в карточку студента с якорем на нужное ребро;
     карточка размечена якорями.
  3. Фокус: модалка фокусируема (tabindex=-1), скрипт переносит фокус при
     открытии и возвращает при закрытии (структурная проверка хуков).
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

LLM_MODEL = "deepseek-v4-flash"
PASSWORD = "correct-horse"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ADMIN_PASSWORD", PASSWORD)
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
    with Session(engine) as s:
        globals()["_repo_id"] = _seed(s)
        s.commit()
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def _seed(s) -> str:
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
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash="a" * 64, target_content_hash="b" * 64, rubric_id=rubric.id,
        llm_model=LLM_MODEL, verdict="break", confidence="medium",
        points=[{"entity": "X", "quote": "ц", "why": "нет"}],
    )
    return repo.id


def _login(client):
    client.post("/login", data={"username": "admin", "password": PASSWORD})


def test_modal_confidence_badge(client):
    _login(client)
    html = client.get(f"/artifacts/{_repo_id}/prd").text
    assert "conf-medium" in html  # класс уровня — стилизуемый бейдж (AC 1)
    assert "средняя" in html  # словами


def test_modal_links_to_card_with_anchor(client):
    _login(client)
    html = client.get(f"/artifacts/{_repo_id}/prd").text
    assert f"/students/{_repo_id}#edge-prd-data_model" in html  # AC 2


def test_student_card_has_edge_anchor(client):
    _login(client)
    html = client.get(f"/students/{_repo_id}").text
    assert 'id="edge-prd-data_model"' in html  # AC 2: якорь на ребре


def test_modal_focusable_and_focus_script(client):
    _login(client)
    modal = client.get(f"/artifacts/{_repo_id}/prd").text
    assert 'tabindex="-1"' in modal  # модалка фокусируема (AC 3)
    page = client.get("/artifacts").text
    assert "htmx:afterSwap" in page  # скрипт переноса фокуса при открытии
