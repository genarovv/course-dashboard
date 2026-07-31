"""D37 (итерация 5): русские имена ролей во всех экранах.

Спека: plans/доводки-ux-5-2026-07-31.md §D37. Матрица говорит «Схема данных»,
а дело защиты и карточка — слагами (data_model) — комиссия не сопоставит.
AC: ROLE_TITLES применяется в деле защиты, карточке, модалке; сырой слаг
остаётся в CSS-классах и якорях (якоря D17 — регресс-тест).
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
        with Session(engine) as s:
            yield s
            s.commit()

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(s):
    lesson5 = Lesson(number=5, title="PRD", date=datetime(2026, 6, 30).date())
    lesson6 = Lesson(number=6, title="Данные", date=datetime(2026, 7, 2).date())
    s.add_all([lesson5, lesson6])
    s.flush()
    a1 = ArtifactDef(lesson_id=lesson5.id, role="prd", expected_pattern="product/prd.md")
    a2 = ArtifactDef(lesson_id=lesson6.id, role="data_model", expected_pattern="data-model.md")
    s.add_all([a1, a2])
    rubric = store.register_rubric(s, type="edge", version="1.0", text="п")
    s.flush()
    edge = store.config_create_edge_def(
        s, source_role="prd", target_role="data_model", rubric_id=rubric.id
    )
    repo = store.register_repository(s, repo_url="https://github.com/s/x", git_host=GitHost.GitHub)
    run = store.register_sync_run(s, triggered_by=SyncTrigger.schedule)
    s.flush()
    snap_a = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=a1.id,
        status=SnapshotStatus.found, content_hash="a" * 64,
        file_path="product/prd.md", source_commit_sha="c" * 40,
    )
    snap_b = store.register_snapshot(
        s, sync_run_id=run.id, repository_id=repo.id, artifact_def_id=a2.id,
        status=SnapshotStatus.found, content_hash="b" * 64,
        file_path="data-model.md", source_commit_sha="c" * 40,
    )
    store.register_sync_outcome(
        s, sync_run_id=run.id, repository_id=repo.id, outcome=SyncOutcome.ok_changed
    )
    s.flush()
    store.register_verdict(
        s, edge_def_id=edge.id, source_snapshot_id=snap_a.id, target_snapshot_id=snap_b.id,
        source_content_hash=snap_a.content_hash, target_content_hash=snap_b.content_hash,
        rubric_id=rubric.id, llm_model=LLM_MODEL, verdict="break", confidence="high",
        points=[{"entity": "X", "quote": "ц", "why": "нет"}], notes="Потеряна X.",
    )
    s.flush()
    return repo


def test_defense_uses_russian_role_names(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    html = client.get(f"/students/{repo_id}/defense").text
    assert "PRD → Схема данных" in html  # заголовок разрыва — по-русски
    assert ">prd → data_model<" not in html  # слаг не показывается человеку
    assert "Схема данных — появился" in html  # хронология тоже по-русски


def test_card_russian_names_and_anchor_kept(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    html = client.get(f"/students/{repo_id}").text
    assert "PRD → Схема данных" in html
    assert 'id="edge-prd-data_model"' in html  # якорь D17 — контракт, слаг остаётся


def test_modal_edges_russian(client, engine):
    with Session(engine) as s:
        repo = _seed(s)
        s.commit()
        repo_id = repo.id
    client.post("/login", data={"username": "admin", "password": PASSWORD})
    html = client.get(f"/artifacts/{repo_id}/prd").text
    assert "PRD → Схема данных" in html
